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

# 跑源码时：程序目录 = 脚本所在目录
# 打包成 exe 后：__file__ 指向临时解包目录，所以配置要落在 exe 自己旁边才不会丢
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
    RES_DIR = getattr(sys, "_MEIPASS", BASE_DIR)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    RES_DIR = BASE_DIR


def _can_write(folder: str) -> bool:
    """试一下这个文件夹能不能写东西。
    写不了就别硬塞（比如 exe 放在 C 盘根目录，系统不让写），换个别的地方。"""
    probe = os.path.join(folder, ".o2t-write-test")
    try:
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
        return True
    except Exception:
        return False


CONFIG_NAME = "opencode2trae-config.json"
# 首选：放在程序旁边（绿色软件，方便找）
CONFIG_BESIDE = os.path.join(BASE_DIR, CONFIG_NAME)
# 备选：程序旁边写不了就放这儿（用户自己的目录，一定有写权限）
CONFIG_USERDIR = os.path.join(
    os.environ.get("APPDATA") or os.path.expanduser("~"), "OpenCode2Trae", CONFIG_NAME)
CONFIG_PATH = CONFIG_BESIDE if _can_write(BASE_DIR) else CONFIG_USERDIR
ICON_PATH = os.path.join(RES_DIR, "OpenCode2Trae.ico")
ICON_PNG = os.path.join(RES_DIR, "icon.png")

UPSTREAM_HOST = "opencode.ai"
UPSTREAM_PREFIX = "/inference/go"
BRIDGE_ID = "opencode2trae"
BRIDGE_VERSION = "2.1"
SITE_URL = "https://opencode.ai"           # 官网
CONSOLE_URL = "https://opencode.ai/console/account"   # API 密钥管理 / 账号页面
USAGE_URL = "https://opencode.ai/console"           # 用量监控页面
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


# ---------------------------------------------------------------------------
# 厂商徽章（品牌色 + 首字母）
# ---------------------------------------------------------------------------
# 说明：真正的厂商 logo 是各家公司的版权图，不能直接塞进程序里，所以这里用
# "品牌色圆角小方块 + 首字母"来表示是哪一家，效果和 logo 一样好认。
VENDORS = {
    "Anthropic":    ("A", "#D97757"),   # Claude
    "xAI":          ("X", "#1A1A1A"),   # Grok
    "智谱 AI":       ("Z", "#3859FF"),   # GLM
    "OpenAI":       ("O", "#10A37F"),   # GPT
    "Google":       ("G", "#4285F4"),   # Gemini
    "Moonshot":     ("K", "#2C2C2C"),   # Kimi
    "美团":          ("L", "#F5C518"),   # LongCat
    "阶跃星辰":       ("S", "#0A66FF"),   # Step
    "Cat Jam":      ("C", "#FF6B9D"),
    "小米":          ("M", "#FF6900"),   # MiMo
    "MiniMax":      ("M", "#E7354A"),
    "Muse":         ("M", "#7C3AED"),   # Muse Spark
    "阿里通义":       ("Q", "#615CED"),   # Qwen
    "DeepSeek":     ("D", "#4D6BFE"),
    "腾讯混元":       ("H", "#0052D9"),   # Hunyuan
    "Space Bunny":  ("B", "#FF4D8D"),
}

# 5x7 的点阵字模（# = 点亮），用来在徽章里画字母
_FONT5x7 = {
    "A": [".###.", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"],
    "B": ["####.", "#...#", "#...#", "####.", "#...#", "#...#", "####."],
    "C": [".###.", "#...#", "#....", "#....", "#....", "#...#", ".###."],
    "D": ["####.", "#...#", "#...#", "#...#", "#...#", "#...#", "####."],
    "G": [".###.", "#...#", "#....", "#.###", "#...#", "#...#", ".###."],
    "H": ["#...#", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"],
    "K": ["#...#", "#..#.", "#.#..", "##...", "#.#..", "#..#.", "#...#"],
    "L": ["#....", "#....", "#....", "#....", "#....", "#....", "#####"],
    "M": ["#...#", "##.##", "#.#.#", "#...#", "#...#", "#...#", "#...#"],
    "O": [".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."],
    "Q": [".###.", "#...#", "#...#", "#...#", "#.#.#", "#..#.", ".##.#"],
    "S": [".####", "#....", "#....", ".###.", "....#", "....#", "####."],
    "X": ["#...#", "#...#", ".#.#.", "..#..", ".#.#.", "#...#", "#...#"],
    "Z": ["#####", "....#", "...#.", "..#..", ".#...", "#....", "#####"],
}


def make_badge(letter: str, color: str, size: int = 20) -> tk.PhotoImage:
    """画一个"品牌色圆角方块 + 白字母"的小徽章（跟 logo 一个作用，好认）。"""
    img = tk.PhotoImage(width=size, height=size)
    radius = max(2, size // 5)
    for x in range(size):
        for y in range(size):
            near_x, near_y = min(x, size - 1 - x), min(y, size - 1 - y)
            if near_x < radius and near_y < radius:
                dx, dy = radius - near_x - 0.5, radius - near_y - 0.5
                if dx * dx + dy * dy > (radius - 0.5) ** 2:
                    continue          # 圆角外面，留透明
            img.put(color, to=(x, y, x + 1, y + 1))
    rows = _FONT5x7.get((letter or "?").upper())
    if rows:
        # 点阵字随徽章大小一起缩放（徽章 20px 时 scale=2，35px 时 scale=4）
        scale = max(1, (size - 4) // 7)
        ox = (size - 5 * scale) // 2
        oy = (size - 7 * scale) // 2
        for r, row in enumerate(rows):
            for c, ch in enumerate(row):
                if ch == "#":
                    px, py = ox + c * scale, oy + r * scale
                    img.put("#ffffff", to=(px, py, px + scale, py + scale))
    return img


# ---------------------------------------------------------------------------
# 每个模型的官方信息（数据来自 https://opencode.ai/docs/go/ 的 Go 套餐用量表）
#   格式：模型 ID -> (厂商, 输入 $/百万字, 输出 $/百万字, 每月额度 $)
#   价格写 None = 官方没公布；写 0 = 免费；额度写 None = 不限额
# ---------------------------------------------------------------------------
MODEL_INFO = {
    "claude-haiku-5-5":           ("Anthropic",  0.10,  0.50,  15),
    "grok-4.7":                   ("xAI",        2.00,  6.00,  15),
    "grok-4.6":                   ("xAI",        2.00,  6.00,  15),
    "glm-5.3-flash":              ("智谱 AI",     0.15,  0.50,  60),
    "glm-5.3":                    ("智谱 AI",     1.40,  4.40,  15),
    "glm-5.2":                    ("智谱 AI",     1.40,  4.40,  60),
    "gpt-6-luna":                 ("OpenAI",     0.10,  0.50,  15),
    "gpt-5.6-luna":               ("OpenAI",     0.20,  1.20,  15),
    "gemini-4-argon-contributor": ("Google",     None,  None,  None),
    "kimi-k3":                    ("Moonshot",   3.00, 15.00,  15),
    "kimi-k2.7-code":             ("Moonshot",   0.95,  4.00,  60),
    "kimi-k2.6":                  ("Moonshot",   0.95,  4.00,  60),
    "longcat-2.0":                ("美团",        0.30,  1.20,  60),
    "longcat-2.5-preview-free":   ("美团",        0.00,  0.00,  0),
    "step-5-preview-free":        ("阶跃星辰",     0.00,  0.00,  0),
    "cat-jam-free":               ("Cat Jam",    0.00,  0.00,  0),
    "mimo-v2.6-flash":            ("小米",        0.14,  0.28,  60),
    "mimo-v2.6-pro":              ("小米",        0.435, 0.87,  15),
    "mimo-v2.5":                  ("小米",        0.14,  0.28,  60),
    "mimo-v2.5-pro":              ("小米",        0.435, 0.87,  15),
    "minimax-m3":                 ("MiniMax",    0.30,  1.20,  60),
    "minimax-m2.7":               ("MiniMax",    0.30,  1.20,  60),
    "muse-spark-1.3-contributor": ("Muse",       0.10,  0.20,  60),
    "muse-spark-1.2-contributor": ("Muse",       0.10,  0.20,  60),
    "qwen3.8-max":                ("阿里通义",     2.00,  6.00,  15),
    "qwen3.8-flash":              ("阿里通义",     0.15,  0.47,  30),
    "qwen3.7-plus":               ("阿里通义",     0.40,  1.60,  60),
    "deepseek-v4.1-flash":        ("DeepSeek",   0.15,  0.60,  60),
    "deepseek-v4-pro":            ("DeepSeek",   0.66,  1.98,  15),
    "hy4-preview":                ("腾讯混元",     0.834, 2.501, 30),
    "hy3":                        ("腾讯混元",     0.14,  0.58,  60),
    "space-bunny":                ("Space Bunny", 0.15,  0.60,  30),
}


def money_text(value) -> str:
    """把价格变成好看的字符串。"""
    if value is None:
        return "—"
    if value == 0:
        return "免费"
    return "$" + (f"{value:.4f}".rstrip("0").rstrip("."))


def quota_text(value) -> str:
    """把"每月额度"变成好看的字符串。"""
    if value is None:
        return "—"
    if value == 0:
        return "免费不限量"
    return f"${value:.0f}"


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
        self.badges = {}               # 厂商徽章图片（缓存起来，不然会被回收掉）
        self.sort_mode = "cap"         # cap = 能力排序，price = 价格排序
        self.sort_desc = False         # 是不是"反过来排"

        self.port_var = tk.StringVar(value=str(DEFAULT_PORT))
        self.key_var = tk.StringVar()
        self.status_var = tk.StringVar(value="未运行")
        self.hint_var = tk.StringVar(value="")

        self.addr_openai_base = tk.StringVar()
        self.addr_openai_full = tk.StringVar()
        self.addr_claude_base = tk.StringVar()
        self.addr_claude_full = tk.StringVar()
        self.search_var = tk.StringVar()
        self.sort_var = tk.StringVar()

        self.ui_scale = self._dpi_scale()
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
    def _dpi_scale(self) -> float:
        """Windows 的缩放倍数（96 DPI = 1.0，本机 175% 就是 1.75）。

        为什么需要它？字体大小写的是"点"，系统会自动把它放大；但"行高、列宽"
        这种像素数字，系统不会帮你放大。结果就是：字变大了、装字的框还是原来
        那么大 —— 字被上下切掉。所以要自己乘一下这个倍数。
        """
        try:
            return max(1.0, self.root.winfo_fpixels("1i") / 96.0)
        except Exception:
            return 1.0

    def _setup_style(self):
        s = self.ui_scale
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
        # 行高必须跟着缩放，不然放大后的字会被行边裁掉上下（原来的老毛病）
        style.configure("Treeview", rowheight=int(round(24 * s)), font=(FONT, 10))
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
        ttk.Button(links, text="打开用量监控页",
                   command=lambda: self.open_url(USAGE_URL)).pack(side="left")

        ttk.Label(key_box, style="Hint.TLabel",
                  text="粘贴进来点保存，下次打开自动带出，方便随时复制。\n"
                       "（密钥在本机明文存着，只存在你自己电脑的配置文件里：一般就放在程序旁边；\n"
                       "如果程序放在 C 盘根目录这种写不进去的地方，会改放到用户目录下）"
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
        top.pack(fill="x", pady=(0, 6))
        ttk.Label(top, text="搜索").pack(side="left")
        ttk.Entry(top, textvariable=self.search_var, width=20).pack(side="left", padx=(4, 14))
        ttk.Button(top, text="复制模型 ID",
                   command=self._copy_id).pack(side="left")
        ttk.Button(top, text="复制模型名称",
                   command=self._copy_name).pack(side="left", padx=4)
        ttk.Button(top, text="复制「名称 + ID」两行",
                   command=self._copy_both).pack(side="left")
        ttk.Label(top, style="Hint.TLabel",
                  text=f"共 {len(MODELS)} 个").pack(side="right")

        srt = ttk.Frame(tab)
        srt.pack(fill="x", pady=(0, 8))
        ttk.Label(srt, text="排序").pack(side="left")
        ttk.Button(srt, text="按模型能力排序",
                   command=lambda: self._set_sort("cap")).pack(side="left", padx=(4, 4))
        ttk.Button(srt, text="按价格排序",
                   command=lambda: self._set_sort("price")).pack(side="left")
        ttk.Label(srt, textvariable=self.sort_var,
                  style="Hint.TLabel").pack(side="left", padx=10)

        # 底部说明：先占好位置（side="bottom"），这样表格再长也挤不掉这几行字
        total = sum(v[3] for v in MODEL_INFO.values()
                    if isinstance(v[3], (int, float)) and v[3] > 0)
        no_price = sum(1 for _, mid in MODELS
                       if MODEL_INFO.get(mid, (0, None, None, None))[1] is None)
        extra = f"；有 {no_price} 个官方还没公布价格" if no_price else ""
        foot = ttk.Label(tab, style="Hint.TLabel", justify="left", anchor="w",
                         text=f"「模型名称」随便写（只给你自己看）；「模型 ID」必须和表里一模一样，Trae 才对得上。\n"
                              f"本套餐 {len(MODELS)} 个模型，每月付费额度合计约 ${total:.0f}{extra}。"
                              f"价格来自 OpenCode 官方 Go 套餐说明；DeepSeek 分高低峰，表里填的是低峰价。\n"
                              f"小提示：双击某一行 = 复制它的模型 ID。"
                              f"如果 Trae 提示「不支持图片输入」，把高级配置里的「支持图片输入」关掉即可。")
        foot.pack(side="bottom", fill="x", pady=(8, 0))
        # 窗口一窄就自动折行，文字不会被横向切掉
        tab.bind("<Configure>",
                 lambda e: foot.configure(wraplength=max(int(360 * self.ui_scale),
                                                        e.width - 24)))

        wrap = ttk.Frame(tab)
        wrap.pack(fill="both", expand=True)
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        s = self.ui_scale
        # 列宽也按缩放倍数放大，这样在 175% 下和 100% 下的观感完全一致
        cols = ("vendor", "id", "inp", "outp", "quota")
        self.tree = ttk.Treeview(wrap, columns=cols, show="tree headings", height=16)
        self.tree.heading("#0", text="模型名称")
        self.tree.column("#0", width=int(215 * s), anchor="w", minwidth=int(180 * s))
        self.tree.heading("vendor", text="厂商")
        self.tree.column("vendor", width=int(92 * s), anchor="w", minwidth=int(76 * s))
        self.tree.heading("id", text="模型 ID")
        self.tree.column("id", width=int(235 * s), anchor="w", minwidth=int(180 * s))
        self.tree.heading("inp", text="输入 $/百万字")
        self.tree.column("inp", width=int(108 * s), anchor="e", minwidth=int(92 * s))
        self.tree.heading("outp", text="输出 $/百万字")
        self.tree.column("outp", width=int(108 * s), anchor="e", minwidth=int(92 * s))
        self.tree.heading("quota", text="每月额度 $")
        self.tree.column("quota", width=int(112 * s), anchor="e", minwidth=int(96 * s))
        ysb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        xsb = ttk.Scrollbar(wrap, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        xsb.grid(row=1, column=0, sticky="ew")
        self.tree.bind("<Double-1>", lambda _e: self._copy_id())
        self._fill_tree()
        self._update_sort_label()

    def _badge(self, vendor: str):
        """拿到某个厂商的徽章图片（第一次用的时候现画）。"""
        if vendor not in self.badges:
            short, color = VENDORS.get(vendor, ("?", "#9aa3b2"))
            size = int(round(20 * self.ui_scale))
            self.badges[vendor] = make_badge(short, color, size=size)
        return self.badges[vendor]

    def _sort_key(self, row):
        """排序用的"尺子"。row = (名称, ID, 厂商, 输入价, 输出价, 额度)。"""
        _, _, _, _, outp, quota = row
        if self.sort_mode == "price":
            # 按输出价格排；没价格的排最后
            return (1, 0.0) if outp is None else (0, outp)
        # 能力排序：官方给的每月额度越小，说明这个模型越贵越强 → 排越前面
        if quota is None:            # 官方没额度数据
            return (2, 0.0)
        if quota == 0:               # 免费不限量的排在中后段
            return (1, 0.0)
        return (0, quota)

    def _set_sort(self, mode: str):
        """点排序按钮：同一按钮点第二下就反过来排。"""
        if self.sort_mode == mode:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_mode = mode
            self.sort_desc = False
        self._fill_tree()
        self._update_sort_label()

    def _update_sort_label(self):
        if self.sort_mode == "cap":
            how = "由弱到强" if self.sort_desc else "由强到弱"
            text = f"当前：模型能力排序（{how}，再点一次换方向）"
        else:
            how = "由贵到便宜" if self.sort_desc else "由便宜到贵"
            text = f"当前：价格排序（{how}，再点一次换方向）"
        self.sort_var.set(text)

    def _fill_tree(self):
        keyword = self.search_var.get().strip().lower()
        self.tree.delete(*self.tree.get_children())
        rows = []
        for name, mid in MODELS:
            if keyword and keyword not in name.lower() and keyword not in mid.lower():
                continue
            vendor, inp, outp, quota = MODEL_INFO.get(mid, ("—", None, None, None))
            rows.append((name, mid, vendor, inp, outp, quota))
        rows.sort(key=self._sort_key, reverse=self.sort_desc)
        for name, mid, vendor, inp, outp, quota in rows:
            self.tree.insert("", "end", image=self._badge(vendor), text=name,
                             values=(vendor, mid, money_text(inp),
                                     money_text(outp), quota_text(quota)))

    def _selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "先在下面列表里点一行，再点复制。")
            return None
        return sel[0]

    def _copy_id(self):
        item = self._selected()
        if item:
            self.copy(self.tree.item(item, "values")[1], "模型 ID")

    def _copy_name(self):
        item = self._selected()
        if item:
            self.copy(self.tree.item(item, "text"), "模型名称")

    def _copy_both(self):
        item = self._selected()
        if item:
            name = self.tree.item(item, "text")
            mid = self.tree.item(item, "values")[1]
            self.copy(f"{name}\n{mid}", "「名称 + ID」")

    # ---- 页 3：使用说明 ----
    def _build_help_tab(self, nb):
        tab = ttk.Frame(nb, padding=12)
        nb.add(tab, text="使用说明")
        text = tk.Text(tab, wrap="word", font=(FONT, 10), bg=CARD, relief="flat",
                       padx=12, pady=10, borderwidth=1, width=72, height=14)
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

    def _config_paths(self):
        """配置可能放的位置，按优先级排（去重）。"""
        return list(dict.fromkeys((CONFIG_PATH, CONFIG_BESIDE, CONFIG_USERDIR)))

    def _load_config(self):
        data = None
        for path in self._config_paths():
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
            except Exception:
                continue
            if isinstance(loaded, dict):
                data = loaded
                break
        if data is None:
            return
        if isinstance(data.get("api_key"), str):
            self.key_var.set(data["api_key"])
        port = data.get("port")
        if isinstance(port, int) and 1 <= port <= 65535:
            self.port_var.set(str(port))

    def _save_config(self):
        data = {"api_key": self.key_var.get().strip(), "port": self._port() or DEFAULT_PORT}
        err = None
        for path in self._config_paths():
            try:
                folder = os.path.dirname(path)
                if folder:
                    os.makedirs(folder, exist_ok=True)
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, ensure_ascii=False, indent=2)
                return
            except Exception as exc:
                err = exc          # 这个位置写不了，换下一个
        self._log_line(f"配置没保存成功：{err}")

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

顺手的几个小功能
    · 「接入地址」页有三个直达按钮：官网、API 密钥管理页、用量监控页（看这个月还剩多少额度）。
    · 「模型清单」页可以按「模型能力」或「价格」排序，每个模型后面还有单价和每月额度参考。
      名字前面那个彩色小方块 = 是哪家厂商（比如 o 绿 = OpenAI，A 橙 = Anthropic）。
    · 同一排的排序按钮再点一下 = 反过来排。

要注意的事
    · 本程序必须一直开着。关掉窗口 = 断线。
    · 它是本机自用的中转站，不联网存储你的数据，密钥只写在你电脑的配置文件里
      （一般放在程序旁边；程序放在 C 盘根目录这种写不进去的地方时，会自动改放到用户目录）。
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


def _fit_window(root: tk.Tk):
    """按内容实际需要的大小开窗口，并且不允许再拖小，避免内容被截断。"""
    root.update_idletasks()
    need_w = root.winfo_reqwidth() + 20
    need_h = root.winfo_reqheight() + 20
    screen_w = root.winfo_screenwidth()
    screen_h = root.winfo_screenheight()
    # 屏幕小的时候按屏幕收着点，别把窗口顶到屏幕外面去
    w = min(need_w, int(screen_w * 0.96))
    h = min(need_h, int(screen_h * 0.92))
    root.minsize(w, h)
    x = max(0, (screen_w - w) // 2)
    y = max(0, (screen_h - h) // 3)
    root.geometry(f"{w}x{h}+{x}+{y}")


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    root.title("OpenCode2Trae")
    _set_window_icon(root)
    BridgeApp(root)
    _fit_window(root)
    root.mainloop()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
