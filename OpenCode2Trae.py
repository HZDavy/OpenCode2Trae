#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenCode2Trae

把 OpenCode Go 接到 Trae 用的中转站（带界面版）

它是干什么的？
    Trae 的自定义模型"不能自己加请求头"，而 OpenCode Go 要求每个请求必须带上
    x-opencode-session 这个头才给正常回应（否则报 400 / 500）。
    这个小程序在你电脑上开一个"中转站"：接住 Trae 的请求 -> 补上这个头 ->
    转给 OpenCode。问题就没了。

它还能干什么？
    · 一键启动 / 关闭中转站
    · 记住你的 API 密钥，方便随时复制
    · 内置 Go 套餐全部 32 个模型的名称和 ID，点一下就复制
    · 同时支持 OpenAI 格式 和 Claude（Anthropic）格式

只用了 Python 自带的功能，不需要安装任何第三方库。
运行：python OpenCode2Trae.py
"""

import ctypes
import hashlib
import json
import os
import queue
import socket
import sys
import threading
import tkinter as tk
import urllib.request
import webbrowser
from http.client import HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from tkinter import messagebox, ttk

# ---------------------------------------------------------------------------
# 基本设置
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "opencode2trae-config.json")
ICON_PATH = os.path.join(BASE_DIR, "OpenCode2Trae.ico")
ICON_PNG = os.path.join(BASE_DIR, "icon.png")

UPSTREAM_HOST = "opencode.ai"
UPSTREAM_PREFIX = "/inference/go"
BRIDGE_ID = "opencode2trae"
BRIDGE_VERSION = "2.0"
SITE_URL = "https://opencode.ai"           # 官网
CONSOLE_URL = "https://opencode.ai/auth"   # 登录 / 充值 / 复制 API 密钥 的地方
DEFAULT_PORT = 8787
LISTEN_HOST = "127.0.0.1"

# 透传时丢弃的逐跳头（由本机这一跳自己管理）
DROP_HEADERS = {
    "host", "connection", "keep-alive", "proxy-connection", "transfer-encoding",
    "content-length", "upgrade", "te", "trailer", "expect",
}

# Go 套餐全部 32 个模型（显示名, 模型 ID）—— 模型 ID 已逐个实测确认
MODELS = [
    ("Claude Haiku 5.5", "claude-haiku-5-5"),
    ("Grok 4.7", "grok-4.7"),
    ("Grok 4.6", "grok-4.6"),
    ("GLM-5.3-Flash", "glm-5.3-flash"),
    ("GLM-5.3", "glm-5.3"),
    ("GLM-5.2", "glm-5.2"),
    ("GPT 6 Luna", "gpt-6-luna"),
    ("GPT 5.6 Luna", "gpt-5.6-luna"),
    ("Gemini 4 Argon Contributor", "gemini-4-argon-contributor"),
    ("Kimi K3", "kimi-k3"),
    ("Kimi K2.7 Code", "kimi-k2.7-code"),
    ("Kimi K2.6", "kimi-k2.6"),
    ("LongCat-2.0", "longcat-2.0"),
    ("LongCat 2.5 Preview Free", "longcat-2.5-preview-free"),
    ("Step 5 Preview Free", "step-5-preview-free"),
    ("Cat Jam Free", "cat-jam-free"),
    ("MiMo-V2.6-Flash", "mimo-v2.6-flash"),
    ("MiMo-V2.6-Pro", "mimo-v2.6-pro"),
    ("MiMo-V2.5", "mimo-v2.5"),
    ("MiMo-V2.5-Pro", "mimo-v2.5-pro"),
    ("MiniMax M3", "minimax-m3"),
    ("MiniMax M2.7", "minimax-m2.7"),
    ("Muse Spark 1.3 Contributor", "muse-spark-1.3-contributor"),
    ("Muse Spark 1.2 Contributor", "muse-spark-1.2-contributor"),
    ("Qwen3.8 Max", "qwen3.8-max"),
    ("Qwen3.8 Flash", "qwen3.8-flash"),
    ("Qwen3.7 Plus", "qwen3.7-plus"),
    ("DeepSeek V4.1 Flash", "deepseek-v4.1-flash"),
    ("DeepSeek V4 Pro", "deepseek-v4-pro"),
    ("Hy4 preview", "hy4-preview"),
    ("Hy3", "hy3"),
    ("Space Bunny", "space-bunny"),
]


def models_payload() -> dict:
    """给 Trae 看的模型清单（它有时候会先探一下这个地址）。"""
    return {
        "object": "list",
        "data": [
            {"id": mid, "object": "model", "created": 0, "owned_by": "opencode-go"}
            for _, mid in MODELS
        ],
    }


def pick_session_id(body: bytes) -> str:
    """按"会话"给一个稳定的 session id，这样上游的路由和缓存才不会各说各话。"""
    try:
        payload = json.loads(body or b"{}")
    except Exception:
        return "trae-shared"
    for message in payload.get("messages") or []:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:24]
        return "trae-" + digest
    return "trae-shared"


def map_upstream(path: str):
    """把本机收到的路径翻译成 OpenCode 的真实路径。认不出来就返回 None。"""
    p = path.split("?", 1)[0]
    if not p.startswith("/"):
        p = "/" + p
    p = p.rstrip("/") or "/"

    # OpenAI 兼容格式
    if p in ("/v1/chat/completions", "/chat/completions"):
        return UPSTREAM_PREFIX + "/openai/v1/chat/completions"
    # Claude / Anthropic 格式
    if p in ("/anthropic/v1/messages", "/anthropic/messages", "/v1/messages", "/messages"):
        return UPSTREAM_PREFIX + "/anthropic/v1/messages"
    # 其它 anthropic 子路径原样转过去（比如 count_tokens）
    if p.startswith("/anthropic/"):
        return UPSTREAM_PREFIX + p
    return None


# ---------------------------------------------------------------------------
# 桥接内核
# ---------------------------------------------------------------------------

class BridgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # 关掉默认的噪音日志，用自己的

    # ---- 小工具 ----
    def _log(self, msg: str):
        try:
            self.server.log_fn(msg)
        except Exception:
            pass

    def _send(self, status: int, content_type: str, payload: bytes):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if payload:
            self.wfile.write(payload)
            self.wfile.flush()

    def _send_json(self, status: int, obj: dict):
        self._send(status, "application/json", json.dumps(obj).encode("utf-8"))

    # ---- GET ----
    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path in ("/bridge/ping", "/bridge/info"):
            self._send_json(200, {
                "bridge": BRIDGE_ID,
                "version": BRIDGE_VERSION,
                "formats": ["openai", "anthropic"],
                "models": len(MODELS),
            })
            return
        if path.endswith("/models"):
            self._send_json(200, models_payload())
            return
        self._send_json(404, {"error": {"message": "bridge: 这里只转发对话请求"}})

    # ---- POST ----
    def do_POST(self):
        upstream_path = map_upstream(self.path)
        if upstream_path is None:
            self._send_json(404, {"error": {
                "message": "bridge: 没认出来这个地址。OpenAI 用 /v1/chat/completions，"
                           "Claude 用 /anthropic/v1/messages"}})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(length) if length > 0 else b""

        session_id = pick_session_id(body)
        try:
            model = (json.loads(body) or {}).get("model", "?")
        except Exception:
            model = "?"

        is_anthropic = "/anthropic/" in upstream_path

        outgoing = {
            "Content-Type": self.headers.get("Content-Type", "application/json"),
            "Accept": self.headers.get("Accept", "text/event-stream"),
            "User-Agent": "opencode2trae/2.0",
            "x-opencode-session": session_id,   # ← 关键：就是这一行救活了 Trae
        }
        for name in ("Authorization", "OpenAI-Organization", "x-api-key",
                     "anthropic-version", "anthropic-beta"):
            value = self.headers.get(name)
            if value:
                outgoing[name] = value

        # Anthropic 端点习惯用 x-api-key；如果 Trae 只给了 Bearer，就顺手翻译一份
        if is_anthropic and "x-api-key" not in outgoing:
            auth = outgoing.get("Authorization", "")
            if auth.startswith("Bearer "):
                outgoing["x-api-key"] = auth[7:].strip()
        if is_anthropic and "anthropic-version" not in outgoing:
            outgoing["anthropic-version"] = "2023-06-01"

        kind = "claude" if is_anthropic else "openai"
        self._log(f"-> [{kind}] model={model} session={session_id} 上行 {len(body)} 字节")

        connection = None
        try:
            connection = HTTPSConnection(UPSTREAM_HOST, timeout=900)
            connection.request("POST", upstream_path, body=body, headers=outgoing)
            response = connection.getresponse()

            content_type = response.getheader("Content-Type", "application/json")
            self.send_response(response.status)
            self.send_header("Content-Type", content_type)
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

            if response.status != 200:
                raw = response.read()
                self._log(f"<- HTTP {response.status} {raw[:200].decode('utf-8', 'replace')}")
                self.wfile.write(raw)
                self.wfile.flush()
                return

            # 流式转发：Trae 是边生成边显示，必须一块一块往回倒，不能攒完再发
            total = 0
            while True:
                chunk = response.read(2048)
                if not chunk:
                    break
                total += len(chunk)
                self.wfile.write(chunk)
                self.wfile.flush()
            self._log(f"<- 200 流式返回 {total} 字节")

        except Exception as exc:
            self._log(f"!! {type(exc).__name__}: {exc}")
            try:
                self._send_json(502, {"error": {"message": f"bridge: {exc}"}})
            except Exception:
                pass
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass


class BridgeServer:
    """可随时开关的中转站。启动后跑在后台线程里，界面不会卡。"""

    def __init__(self, host: str, port: int, log):
        self.host = host
        self.port = port
        self.log = log
        self.httpd = None
        self.thread = None

    def start(self):
        httpd = ThreadingHTTPServer((self.host, self.port), BridgeHandler)
        httpd.daemon_threads = True
        httpd.log_fn = self.log
        self.httpd = httpd
        self.thread = threading.Thread(
            target=httpd.serve_forever, kwargs={"poll_interval": 0.3}, daemon=True
        )
        self.thread.start()

    def stop(self):
        httpd, self.httpd = self.httpd, None
        if httpd is None:
            return
        try:
            httpd.shutdown()
        except Exception:
            pass
        try:
            httpd.server_close()
        except Exception:
            pass
        if self.thread is not None:
            self.thread.join(timeout=3)
            self.thread = None


# ---------------------------------------------------------------------------
# 界面
# ---------------------------------------------------------------------------

FONT = "Microsoft YaHei UI"
BG = "#f4f5f7"
HEAD_BG = "#1f2430"
CARD = "#ffffff"
GREY = "#b9bcc4"
GREEN = "#2eae5b"
ORANGE = "#e0912f"


class BridgeApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.logq = queue.Queue()
        self.server = None
        self.external = False          # 端口上是不是已经有别的桥接在跑
        self.key_shown = False

        self.port_var = tk.StringVar(value=str(DEFAULT_PORT))
        self.key_var = tk.StringVar()
        self.status_var = tk.StringVar(value="未运行")
        self.hint_var = tk.StringVar(value="")

        self.addr_openai_base = tk.StringVar()
        self.addr_openai_full = tk.StringVar()
        self.addr_claude_base = tk.StringVar()
        self.addr_claude_full = tk.StringVar()
        self.search_var = tk.StringVar()

        self._setup_style()
        self._build()
        self._load_config()
        self._refresh_addresses()

        self.port_var.trace_add("write", lambda *_: self._refresh_addresses())
        self.search_var.trace_add("write", lambda *_: self._fill_tree())
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._pump_log()
        self.root.after(150, self._check_port)

    # ---------- 样式 ----------
    def _setup_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(".", background=BG, font=(FONT, 10))
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG)
        style.configure("TLabelframe", background=BG)
        style.configure("TLabelframe.Label", background=BG, font=(FONT, 10, "bold"))
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(16, 7), font=(FONT, 10))
        style.configure("TButton", padding=(10, 5))
        style.configure("Treeview", rowheight=24, font=(FONT, 10))
        style.configure("Treeview.Heading", font=(FONT, 10, "bold"))
        style.configure("Hint.TLabel", foreground="#6b7280")

    # ---------- 搭界面 ----------
    def _build(self):
        head = tk.Frame(self.root, bg=HEAD_BG, height=54)
        head.pack(fill="x")
        head.pack_propagate(False)
        tk.Label(head, text="OpenCode2Trae", bg=HEAD_BG, fg="white",
                 font=(FONT, 13, "bold")).pack(side="left", padx=16)
        tk.Label(head, text="给 Trae 补上通行证的中转站", bg=HEAD_BG, fg="#9aa3b2",
                 font=(FONT, 9)).pack(side="left")

        body = ttk.Frame(self.root, padding=(14, 10, 14, 6))
        body.pack(fill="both", expand=True)

        # ==== 控制条 ====
        bar = ttk.Frame(body)
        bar.pack(fill="x", pady=(0, 10))
        self.light = tk.Canvas(bar, width=14, height=14, bg=BG, highlightthickness=0)
        self.dot = self.light.create_oval(2, 2, 12, 12, fill=GREY, outline="")
        self.light.pack(side="left")
        ttk.Label(bar, textvariable=self.status_var,
                  font=(FONT, 10, "bold")).pack(side="left", padx=(6, 18))
        ttk.Label(bar, text="端口").pack(side="left")
        ttk.Entry(bar, textvariable=self.port_var, width=7,
                  justify="center").pack(side="left", padx=(4, 12))
        self.btn_start = ttk.Button(bar, text="启动桥接", command=self.start_bridge)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(bar, text="关闭桥接", command=self.stop_bridge,
                                   state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        ttk.Label(bar, textvariable=self.hint_var,
                  style="Hint.TLabel").pack(side="right")

        nb = ttk.Notebook(body)
        nb.pack(fill="both", expand=True)

        self._build_addr_tab(nb)
        self._build_model_tab(nb)
        self._build_help_tab(nb)

        ttk.Separator(body).pack(fill="x", pady=(8, 4))
        logbox = ttk.Frame(body)
        logbox.pack(fill="both")
        ttk.Label(logbox, text="运行日志").pack(anchor="w")
        self.log = tk.Text(logbox, height=6, wrap="none", font=("Consolas", 9),
                           bg="#1f2430", fg="#cfd6e4", insertbackground="#cfd6e4",
                           borderwidth=0)
        self.log.pack(fill="both", expand=True, pady=(3, 0))
        self.log.configure(state="disabled")

    # ---- 页 1：接入地址 ----
    def _build_addr_tab(self, nb):
        tab = ttk.Frame(nb, padding=12)
        nb.add(tab, text="接入地址")

        key_box = ttk.Labelframe(tab, text=" 你的 API 密钥（Go 套餐的密钥） ", padding=10)
        key_box.pack(fill="x", pady=(0, 10))
        row = ttk.Frame(key_box)
        row.pack(fill="x")
        self.key_entry = ttk.Entry(row, textvariable=self.key_var, show="*")
        self.key_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="显示", width=6,
                   command=self._toggle_key).pack(side="left", padx=(6, 0))
        ttk.Button(row, text="保存", width=6,
                   command=self._save_key).pack(side="left", padx=4)
        ttk.Button(row, text="复制", width=6,
                   command=lambda: self.copy(self.key_var.get().strip(), "密钥")).pack(side="left")

        links = ttk.Frame(key_box)
        links.pack(fill="x", pady=(8, 0))
        ttk.Button(links, text="打开 OpenCode 官网",
                   command=lambda: self.open_url(SITE_URL)).pack(side="left")
        ttk.Button(links, text="打开 API 密钥管理页",
                   command=lambda: self.open_url(CONSOLE_URL)).pack(side="left", padx=6)

        ttk.Label(key_box, style="Hint.TLabel",
                  text="粘贴进来点保存，下次打开自动带出，方便随时复制。\n"
                       "（密钥在本机明文存着，只存在本程序旁边的配置文件里）"
                  ).pack(anchor="w", pady=(6, 0))

        oa = ttk.Labelframe(tab, text=" ① OpenAI 格式（GPT / Gemini / Grok / DeepSeek / Qwen / Kimi…） ",
                            padding=10)
        oa.pack(fill="x", pady=(0, 10))
        self._addr_row(oa, "基础地址", self.addr_openai_base)
        self._addr_row(oa, "完整地址", self.addr_openai_full)
        ttk.Label(oa, style="Hint.TLabel",
                  text="在 Trae 里「API 格式」选 OpenAI Chat Completions 格式。\n"
                       "「自定义请求地址」填基础地址，并关掉「完整 URL」开关；"
                       "若你开了那个开关，就填完整地址。"
                  ).pack(anchor="w", pady=(6, 0))

        cl = ttk.Labelframe(tab, text=" ② Claude / Anthropic 格式（Claude Haiku 5.5） ", padding=10)
        cl.pack(fill="x")
        self._addr_row(cl, "基础地址", self.addr_claude_base)
        self._addr_row(cl, "完整地址", self.addr_claude_full)
        ttk.Label(cl, style="Hint.TLabel",
                  text="在 Trae 里「API 格式」选 Anthropic Messages 格式，地址同上填法。"
                  ).pack(anchor="w", pady=(6, 0))

    def _addr_row(self, parent, title, var):
        f = ttk.Frame(parent)
        f.pack(fill="x", pady=2)
        ttk.Label(f, text=title, width=8).pack(side="left")
        e = ttk.Entry(f, textvariable=var, state="readonly")
        e.pack(side="left", fill="x", expand=True, padx=(4, 6))
        ttk.Button(f, text="复制", width=6,
                   command=lambda: self.copy(var.get(), title)).pack(side="left")

    # ---- 页 2：模型清单 ----
    def _build_model_tab(self, nb):
        tab = ttk.Frame(nb, padding=12)
        nb.add(tab, text="模型清单")

        top = ttk.Frame(tab)
        top.pack(fill="x", pady=(0, 8))
        ttk.Label(top, text="搜索").pack(side="left")
        ttk.Entry(top, textvariable=self.search_var, width=22).pack(side="left", padx=(4, 14))
        ttk.Button(top, text="复制模型 ID",
                   command=lambda: self._copy_row(1)).pack(side="left")
        ttk.Button(top, text="复制模型名称",
                   command=lambda: self._copy_row(0)).pack(side="left", padx=4)
        ttk.Button(top, text="复制「名称 + ID」两行",
                   command=self._copy_both).pack(side="left")
        ttk.Label(top, style="Hint.TLabel",
                  text=f"共 {len(MODELS)} 个").pack(side="right")

        wrap = ttk.Frame(tab)
        wrap.pack(fill="both", expand=True)
        cols = ("name", "id")
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings")
        self.tree.heading("name", text="模型名称（Trae 里「模型展示名称」随便写，这里给你参考）")
        self.tree.heading("id", text="模型 ID（必须照抄这个）")
        self.tree.column("name", width=300, anchor="w")
        self.tree.column("id", width=360, anchor="w")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.tree.bind("<Double-1>", lambda _e: self._copy_row(1))
        self._fill_tree()

        ttk.Label(tab, style="Hint.TLabel",
                  text="小提示：双击某一行 = 复制它的模型 ID。"
                       "如果 Trae 提示「不支持图片输入」，把高级配置里的「支持图片输入」关掉即可。"
                  ).pack(anchor="w", pady=(8, 0))

    def _fill_tree(self):
        keyword = self.search_var.get().strip().lower()
        self.tree.delete(*self.tree.get_children())
        for name, mid in MODELS:
            if keyword and keyword not in name.lower() and keyword not in mid.lower():
                continue
            self.tree.insert("", "end", values=(name, mid))

    def _selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "先在下面列表里点一行，再点复制。")
            return None
        return self.tree.item(sel[0], "values")

    def _copy_row(self, index: int):
        values = self._selected()
        if values:
            self.copy(values[index], "模型 ID" if index else "模型名称")

    def _copy_both(self):
        values = self._selected()
        if values:
            self.copy(f"{values[0]}\n{values[1]}", "「名称 + ID」")

    # ---- 页 3：使用说明 ----
    def _build_help_tab(self, nb):
        tab = ttk.Frame(nb, padding=12)
        nb.add(tab, text="使用说明")
        text = tk.Text(tab, wrap="word", font=(FONT, 10), bg=CARD, relief="flat",
                       padx=12, pady=10, borderwidth=1)
        text.pack(fill="both", expand=True)
        text.insert("end", HELP_TEXT)
        text.configure(state="disabled")

    # ---------- 地址随端口变化 ----------
    def _refresh_addresses(self):
        port = self._port()
        if port is None:
            return
        base = f"http://{LISTEN_HOST}:{port}"
        self.addr_openai_base.set(base + "/v1")
        self.addr_openai_full.set(base + "/v1/chat/completions")
        self.addr_claude_base.set(base + "/anthropic/v1")
        self.addr_claude_full.set(base + "/anthropic/v1/messages")

    def _port(self):
        try:
            value = int(self.port_var.get().strip())
        except ValueError:
            return None
        return value if 1 <= value <= 65535 else None

    # ---------- 开关 ----------
    def start_bridge(self):
        if self.server is not None:
            return
        port = self._port()
        if port is None:
            messagebox.showerror("端口不对", "端口要填 1~65535 之间的数字。")
            return
        if self._port_busy(port):
            info = self._probe_bridge(port)
            if info and info.get("bridge") == BRIDGE_ID:
                messagebox.showinfo("已经在运行",
                                    f"端口 {port} 上已经有一个本程序的桥接在跑，直接用就行。")
                self._mark_external(port, info)
            else:
                messagebox.showwarning(
                    "端口被占用",
                    f"端口 {port} 上已经有别的程序在监听（可能是你之前开的另一个桥接）。\n\n"
                    f"想用本程序来管开关，就先把那个程序关掉；\n"
                    f"或者把上面的端口改成别的（比如 {port + 1}）再点启动，"
                    f"然后「接入地址」页的地址会自动跟着变。\n\n"
                    f"不改也行：只要那个程序也能补上通行证，OpenAI 格式照样能用。"
                )
            return
        try:
            server = BridgeServer(LISTEN_HOST, port, self._log_line)
            server.start()
        except OSError as exc:
            messagebox.showerror("启动失败",
                                 f"端口 {port} 用不了：{exc}\n\n换个端口再试，或先关掉占用它的程序。")
            return
        self.server = server
        self.external = False
        self._set_status(f"运行中 · 端口 {port}", GREEN)
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self._log_line(f"桥接已启动：http://{LISTEN_HOST}:{port}")
        self._log_line(f"转发目标：https://{UPSTREAM_HOST}{UPSTREAM_PREFIX}/...")
        self._log_line("两种格式都支持：OpenAI 走 /v1，Claude 走 /anthropic/v1")
        self._save_config()

    def stop_bridge(self):
        if self.server is None:
            return
        self.server.stop()
        self.server = None
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self._log_line("桥接已关闭")
        self.root.after(150, self._check_port)

    def _check_port(self):
        """看看端口上是不是已经有别的桥接在跑（比如命令行那个）。"""
        if self.server is not None:
            return
        port = self._port()
        if port is None:
            return

        def work():
            if not self._port_busy(port):
                self.root.after(0, lambda: self._set_status("未运行", GREY))
                return
            info = self._probe_bridge(port)
            self.root.after(0, lambda: self._mark_external(port, info))

        threading.Thread(target=work, daemon=True).start()

    def _mark_external(self, port, info):
        self.external = True
        if info and info.get("bridge") == BRIDGE_ID:
            self._set_status(f"已在运行 · 端口 {port}", GREEN)
            self._log_line(f"检测到另一个本程序在端口 {port} 上运行，可直接用。")
        else:
            self._set_status(f"端口 {port} 已被占用", ORANGE)
            self._log_line(f"端口 {port} 上有别的程序在监听，不一定是本程序。"
                           f"想用本程序管开关就换个端口重开。")

    def _set_status(self, text: str, color: str):
        self.status_var.set(text)
        self.light.itemconfigure(self.dot, fill=color)
        self.external = color == ORANGE

    # ---------- 探测小工具 ----------
    @staticmethod
    def _port_busy(port, timeout=0.6):
        s = socket.socket()
        s.settimeout(timeout)
        try:
            s.connect((LISTEN_HOST, port))
            return True
        except OSError:
            return False
        finally:
            s.close()

    @staticmethod
    def _probe_bridge(port):
        try:
            with urllib.request.urlopen(f"http://{LISTEN_HOST}:{port}/bridge/ping",
                                        timeout=2) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None

    # ---------- 杂项 ----------
    def _toggle_key(self):
        self.key_shown = not self.key_shown
        self.key_entry.configure(show="" if self.key_shown else "*")

    def _save_key(self):
        self._save_config()
        self._log_line("密钥已保存到配置文件")
        self.copy(self.key_var.get().strip(), "密钥")

    def open_url(self, url: str):
        """用系统默认浏览器打开网址。"""
        self._log_line(f"打开浏览器：{url}")
        try:
            webbrowser.open(url)
        except Exception as exc:
            self._log_line(f"打不开浏览器：{exc}")
            messagebox.showwarning("打不开", f"没打开浏览器，你手动复制这个地址吧：\n\n{url}")

    def copy(self, text: str, what: str = "内容"):
        text = (text or "").strip()
        if not text:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update()
        self.hint_var.set(f"已复制{what}")
        self.root.after(2500, lambda: self.hint_var.set(""))

    def _log_line(self, msg: str):
        self.logq.put(msg)

    def _pump_log(self):
        try:
            while True:
                msg = self.logq.get_nowait()
                self.log.configure(state="normal")
                self.log.insert("end", msg + "\n")
                self.log.see("end")
                lines = int(self.log.index("end-1c").split(".")[0])
                if lines > 400:
                    self.log.delete("1.0", "100.0")
                self.log.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(200, self._pump_log)

    def _load_config(self):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            return
        if isinstance(data.get("api_key"), str):
            self.key_var.set(data["api_key"])
        port = data.get("port")
        if isinstance(port, int) and 1 <= port <= 65535:
            self.port_var.set(str(port))

    def _save_config(self):
        data = {"api_key": self.key_var.get().strip(), "port": self._port() or DEFAULT_PORT}
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
        except Exception as exc:
            self._log_line(f"配置没保存成功：{exc}")

    def _on_close(self):
        if self.server is not None:
            if not messagebox.askyesno("确认", "关掉窗口会同时关闭桥接，Trae 就断了。确定吗？"):
                return
            self.server.stop()
            self.server = None
        self._save_config()
        self.root.destroy()


HELP_TEXT = """OpenCode2Trae 是一个"中转站"程序，专门解决 Trae 连 OpenCode Go 报 500 / 4028 的问题。

为什么需要它？
    OpenCode Go 要求每个请求都带一张"通行证"（一个叫 x-opencode-session 的头），
    而 Trae 的自定义模型没有地方能填这个头，所以请求被拦下、报错。
    本程序在本机把这张通行证补上，再转给 OpenCode。

怎么用（一共 6 步）
    1. 点最上面的「启动桥接」。左上角的灯变绿 = 桥通了。
    2. 切到「接入地址」页，把你的 Go 密钥粘进密钥框，点「保存」。（可选，方便以后复制）
    3. 打开 Trae → 模型设置 → 添加自定义模型。
    4. 先选「API 格式」：
         想用 GPT / Gemini / Grok / DeepSeek / Qwen / Kimi / GLM 等 → 选 OpenAI Chat Completions 格式
         想用 Claude → 选 Anthropic Messages 格式
    5. 「自定义请求地址」填「接入地址」页里对应的那条；关掉「完整 URL」开关时填基础地址，
       打开时填完整地址。
    6. 「模型 ID」到「模型清单」页复制（双击行 = 复制 ID）；「API 密钥」填你自己的密钥。

要注意的事
    · 本程序必须一直开着。关掉窗口 = 断线。
    · 它是本机自用的中转站，不联网存储你的数据，密钥只写在本程序旁边的配置文件里。
    · 如果某个模型报「不支持图片输入」，去高级配置把「支持图片输入」关掉。
    · 如果报 401 之类的鉴权错误，那是密钥的问题，不是桥接坏了。
"""


def _set_window_icon(root: tk.Tk):
    """任务栏 / 标题栏图标。优先用 .ico，读不到就退回 png。"""
    try:
        if os.path.exists(ICON_PATH):
            root.iconbitmap(default=ICON_PATH)
            return
    except Exception:
        pass
    try:
        if os.path.exists(ICON_PNG):
            root.iconphoto(True, tk.PhotoImage(file=ICON_PNG))
    except Exception:
        pass


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    root.title("OpenCode2Trae")
    _set_window_icon(root)
    root.geometry("980x740")
    root.minsize(880, 660)
    BridgeApp(root)
    root.mainloop()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
