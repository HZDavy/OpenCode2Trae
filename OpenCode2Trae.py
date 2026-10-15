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
    · 设置里可以开「托盘模式」和「开机自动启动」

除了托盘图标用到 pystray（没装也不影响其它功能，只是开不了托盘模式），
其余功能都只用 Python 自带的东西。
运行：python OpenCode2Trae.py
"""

import ctypes
import hashlib
import json
import os
import queue
import re
import socket
import sys
import threading
import tkinter as tk
import urllib.request
import webbrowser
from http.client import HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from tkinter import messagebox, ttk

# 托盘图标（右上角那种"小图标"）：用 pystray 画。
# 万一这台电脑没装，程序也照样能跑，只是"托盘模式"开不了 —— 所以这里包一层。
try:
    import pystray
    from PIL import Image as PILImage
    TRAY_OK = True
except Exception:          # 没装就优雅降级
    pystray = None
    PILImage = None
    TRAY_OK = False

# 开机启动：其实是往注册表里写一行"开机时要跑什么"。
# 注册表可以理解成 Windows 的"总设置本"，这里只动当前用户自己的那一份。
try:
    import winreg
except Exception:
    winreg = None

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

APP_NAME = "OpenCode2Trae"
# 开机启动项写在注册表这个位置（HKEY_CURRENT_USER 下面，只影响当前登录的用户）
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _launch_command(tray_flag: bool) -> str:
    """开机时该执行的命令。

    · 打包成 exe：直接跑 exe 自己。
    · 跑源码：优先用 pythonw.exe（带 w 的那个不会弹出黑框），再带上脚本路径。
    tray_flag=True 时多加一个 --tray，表示"开机起来先别弹窗口，安静待在托盘里"。
    """
    if getattr(sys, "frozen", False):
        cmd = f'"{sys.executable}"'
    else:
        pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        exe = pyw if os.path.exists(pyw) else sys.executable
        cmd = f'"{exe}" "{os.path.join(BASE_DIR, "OpenCode2Trae.py")}"'
    return cmd + (" --tray" if tray_flag else "")


def autostart_set(on: bool, tray_flag: bool) -> str:
    """开/关开机启动。成功返回空字符串，失败返回原因。"""
    if winreg is None:
        return "这台电脑上没有注册表模块，改不了开机启动。"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            if on:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ,
                                  _launch_command(tray_flag))
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
        return ""
    except Exception as exc:
        return str(exc)

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


def make_badge(letter: str, color: str, size: int = 20, pad_right: int = 0) -> tk.PhotoImage:
    """画一个"品牌色圆角方块 + 白字母"的小徽章（跟 logo 一个作用，好认）。

    pad_right：图片右边额外留出的透明宽度，用来把徽章和模型名字隔开。
    为什么要做进图片里？因为如果直接给文字前面加空格，复制模型名的时候
    会把空格一起复制走，就脏了 —— 加在图片上就没这问题。
    """
    img = tk.PhotoImage(width=size + pad_right, height=size)
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
MONO = "Consolas"

# ---- 配色：照着 opencode.ai 的极简风来的 ----
# 大白底 + 近黑的字 + 一根根"头发丝"一样细的浅灰线，几乎没有多余颜色。
# 只有表示状态的小圆点（绿/橙）和一个深色日志框保留对比色。
BG = "#ffffff"          # 窗口底色：纯白
SURFACE = "#fafafa"     # 输入框/文本框底：极浅灰，用来和纯白区分
BORDER = "#e5e5e5"      # 分隔线：浅灰头发丝
TEXT = "#111111"        # 主要文字：近黑（纯黑太硬，opencode 用的是这种）
MUTED = "#737373"       # 次要说明文字：中灰
HEAD_BG = "#ffffff"     # 顶栏：白底（原来是深色，现在跟官网一样白）
HEAD_FG = "#111111"
INK = "#111111"         # "主按钮"的底色：黑底白字
GREY = "#c9c9c9"
GREEN = "#16a34a"
ORANGE = "#d97706"
LOG_BG = "#0f0f0f"      # 日志框：黑底浅字（终端的感觉）
LOG_FG = "#d4d4d4"


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
        self.tray_enabled = False      # 关了窗口以后，是收进托盘还是真的退出
        self.autostart = False         # 开机要不要自动跑
        self.tray = None               # 托盘图标对象（pystray 的）
        self.settings_win = None       # 设置小窗口（同时只开一个）
        self.tray_var = None           # 设置窗口里"托盘模式"那个勾（没开窗口时是 None）
        self.auto_var = None           # 设置窗口里"开机自启"那个勾
        self.overlay = None            # 设置窗口打开时，盖在主窗口上的那层黑色半透明"纱"
        self._overlay_bind = None      # 上面那层"纱"跟主窗口联动的绑定 id
        self.win_geom = None           # 上次记住的窗口位置和大小，形如 "1100x800+320+180"

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
        self._apply_settings_on_start()

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

        def px(v):
            """把"像素数字"按系统缩放倍数放大（字体系统会自动放大，像素不会）。"""
            return int(round(v * s))

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass

        # ---- 底子 ----
        style.configure(".", background=BG, foreground=TEXT, font=(FONT, 10),
                        focuscolor=BG)
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Hint.TLabel", background=BG, foreground=MUTED)
        style.configure("Title.TLabel", background=BG, foreground=TEXT,
                        font=(FONT, 14, "bold"))
        style.configure("Sub.TLabel", background=BG, foreground=MUTED, font=(FONT, 9))

        # ---- 按钮：白底 + 一根细灰边；点下去变浅灰 ----
        # 关键是把 clam 主题自带的"立体感"（lightcolor / darkcolor 两条高光和阴影）
        # 全部涂成和底色一样，看上去就变成扁平的了，跟官网按钮一个味道。
        style.configure("TButton", padding=(px(13), px(6)), background="#ffffff",
                        foreground=TEXT, bordercolor=BORDER, relief="solid",
                        borderwidth=1, focuscolor="#ffffff",
                        lightcolor="#ffffff", darkcolor="#ffffff", font=(FONT, 10))
        style.map("TButton",
                  background=[("disabled", "#fbfbfb"), ("pressed", "#ededed"),
                              ("active", "#f5f5f5")],
                  foreground=[("disabled", "#bdbdbd")],
                  bordercolor=[("disabled", BORDER), ("active", "#cfcfcf")],
                  lightcolor=[("pressed", "#ededed"), ("active", "#f5f5f5")],
                  darkcolor=[("pressed", "#ededed"), ("active", "#f5f5f5")])

        # 主按钮：黑底白字（一个界面里只留一两个，用于"主要动作"）
        for name in ("Primary.TButton", "Accent.TButton"):
            style.configure(name, padding=(px(15), px(6)), background=INK,
                            foreground="#ffffff", bordercolor=INK, relief="solid",
                            borderwidth=1, focuscolor=INK,
                            lightcolor=INK, darkcolor=INK, font=(FONT, 10, "bold"))
            style.map(name,
                      background=[("disabled", "#d4d4d4"), ("pressed", "#000000"),
                                  ("active", "#333333")],
                      foreground=[("disabled", "#ffffff")],
                      bordercolor=[("disabled", "#d4d4d4"), ("active", "#333333")],
                      lightcolor=[("pressed", "#000000"), ("active", "#333333")],
                      darkcolor=[("pressed", "#000000"), ("active", "#333333")])

        # 幽灵按钮：没有边框，平时像一行链接，鼠标放上去才浮出浅灰底
        style.configure("Ghost.TButton", padding=(px(9), px(6)), background=BG,
                        foreground=MUTED, bordercolor=BG, relief="flat",
                        borderwidth=0, focuscolor=BG,
                        lightcolor=BG, darkcolor=BG, font=(FONT, 10))
        style.map("Ghost.TButton",
                  background=[("active", "#f2f2f2"), ("pressed", "#e8e8e8")],
                  foreground=[("active", TEXT), ("pressed", TEXT)],
                  bordercolor=[("active", "#f2f2f2"), ("pressed", "#e8e8e8")],
                  lightcolor=[("active", "#f2f2f2"), ("pressed", "#e8e8e8")],
                  darkcolor=[("active", "#f2f2f2"), ("pressed", "#e8e8e8")])

        # ---- 输入框：白底一根细灰边，点进去边框变黑 ----
        style.configure("TEntry", fieldbackground="#ffffff", background="#ffffff",
                        foreground=TEXT, bordercolor=BORDER, insertcolor=TEXT,
                        padding=px(5), relief="solid", borderwidth=1,
                        lightcolor=BORDER, darkcolor=BORDER, arrowcolor=MUTED)
        style.map("TEntry",
                  bordercolor=[("focus", INK), ("disabled", BORDER)],
                  lightcolor=[("focus", INK)],
                  darkcolor=[("focus", INK)],
                  fieldbackground=[("readonly", SURFACE), ("disabled", SURFACE)],
                  foreground=[("disabled", MUTED)])

        # ---- 小方框（分组）----
        style.configure("TLabelframe", background=BG, bordercolor=BORDER,
                        relief="solid", borderwidth=1, padding=px(10),
                        lightcolor=BORDER, darkcolor=BORDER)
        style.configure("TLabelframe.Label", background=BG, foreground=TEXT,
                        font=(FONT, 10, "bold"))

        # ---- 表格：纯白底、浅灰表头、选中行只变浅灰不变蓝 ----
        # 行高必须跟着缩放，不然放大后的字会被行边裁掉上下（原来的老毛病）
        style.configure("Treeview", background="#ffffff", fieldbackground="#ffffff",
                        foreground=TEXT, borderwidth=0, relief="flat",
                        rowheight=int(round(24 * s)), font=(FONT, 10))
        style.map("Treeview",
                  background=[("selected", "#f0f0f0")],
                  foreground=[("selected", TEXT)])
        style.configure("Treeview.Heading", background="#ffffff", foreground=MUTED,
                        font=(FONT, 10, "bold"), relief="flat", borderwidth=0,
                        padding=(px(6), px(6)), lightcolor="#ffffff",
                        darkcolor="#ffffff")
        style.map("Treeview.Heading", background=[("active", "#f5f5f5")])

        # ---- 滚动条：细一点、浅一点，别抢眼 ----
        style.configure("Vertical.TScrollbar", background="#dcdcdc",
                        troughcolor=BG, bordercolor=BG, arrowcolor="#9a9a9a",
                        lightcolor="#dcdcdc", darkcolor="#dcdcdc",
                        relief="flat", borderwidth=0, width=px(10))
        style.configure("Horizontal.TScrollbar", background="#dcdcdc",
                        troughcolor=BG, bordercolor=BG, arrowcolor="#9a9a9a",
                        lightcolor="#dcdcdc", darkcolor="#dcdcdc",
                        relief="flat", borderwidth=0)
        style.configure("TSeparator", background=BORDER, bordercolor=BORDER,
                        lightcolor=BORDER, darkcolor=BORDER)

    # ---------- 搭界面 ----------
    def _build(self):
        s = self.ui_scale

        # 顶栏：白底黑字，底下压一根头发丝一样的细线（跟官网导航一模一样）
        head = tk.Frame(self.root, bg=HEAD_BG, height=int(round(54 * s)))
        head.pack(fill="x")
        head.pack_propagate(False)
        tk.Label(head, text="OpenCode2Trae", bg=HEAD_BG, fg=HEAD_FG,
                 font=(FONT, 14, "bold")).pack(side="left", padx=int(round(18 * s)))
        tk.Label(head, text="给 Trae 补上通行证的中转站", bg=HEAD_BG, fg=MUTED,
                 font=(FONT, 9)).pack(side="left")
        # 右上角一个"设置"小按钮（无边框灰字，跟官网那种低调的按钮一样）
        ttk.Button(head, text="设置", style="Ghost.TButton",
                   command=self._open_settings).pack(side="right",
                                                     padx=int(round(14 * s)))
        tk.Frame(self.root, bg=BORDER, height=max(1, int(round(1 * s)))).pack(fill="x")

        body = ttk.Frame(self.root, padding=(int(round(16 * s)), int(round(12 * s)),
                                             int(round(16 * s)), int(round(8 * s))))
        body.pack(fill="both", expand=True)

        # ==== 控制条 ====
        bar = ttk.Frame(body)
        bar.pack(fill="x", pady=(0, int(round(10 * s))))
        d = int(round(11 * s))
        self.light = tk.Canvas(bar, width=d, height=d, bg=BG, highlightthickness=0)
        self.dot = self.light.create_oval(1, 1, d - 1, d - 1, fill=GREY, outline="")
        self.light.pack(side="left")
        ttk.Label(bar, textvariable=self.status_var,
                  font=(FONT, 10, "bold")).pack(side="left", padx=(int(round(7 * s)), int(round(20 * s))))
        ttk.Label(bar, text="端口", style="Hint.TLabel").pack(side="left")
        ttk.Entry(bar, textvariable=self.port_var, width=7,
                  justify="center").pack(side="left", padx=(int(round(5 * s)), int(round(12 * s))))
        # 主按钮只给"启动桥接"用：全屏就它一个是黑底白字，一眼就知道该点哪个
        self.btn_start = ttk.Button(bar, text="启动桥接", style="Primary.TButton",
                                    command=self.start_bridge)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(bar, text="关闭桥接", command=self.stop_bridge,
                                   state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        ttk.Label(bar, textvariable=self.hint_var,
                  style="Hint.TLabel").pack(side="right")

        nb = ttk.Frame(body)
        nb.pack(fill="x")
        content = ttk.Frame(body)
        content.pack(fill="both", expand=True)
        content.rowconfigure(0, weight=1)
        content.columnconfigure(0, weight=1)
        self.pages = []          # [(名字, 按钮, 页面, 下划线)]，给"自己画的页签"用

        self._build_addr_tab(content)
        self._build_model_tab(content)
        self._build_help_tab(content)
        self._build_nav(nb)

        ttk.Separator(body).pack(fill="x", pady=(int(round(10 * s)), int(round(6 * s))))
        logbox = ttk.Frame(body)
        logbox.pack(fill="both")
        ttk.Label(logbox, text="运行日志", style="Hint.TLabel").pack(anchor="w")
        self.log = tk.Text(logbox, height=6, wrap="none", font=(MONO, 9),
                           bg=LOG_BG, fg=LOG_FG, insertbackground=LOG_FG,
                           borderwidth=0, highlightthickness=0, padx=10, pady=8)
        self.log.pack(fill="both", expand=True, pady=(int(round(4 * s)), 0))
        self.log.configure(state="disabled")

    # ---- 页签：自己画一排导航（官网页首那种：选中的黑字 + 底下一根黑线）----
    def _add_page(self, name, page):
        self.pages.append([name, None, page, None])

    def _build_nav(self, parent):
        s = self.ui_scale
        rail = max(1, int(round(2 * s)))       # 导航下面那根线的高度
        for i, entry in enumerate(self.pages):
            name = entry[0]
            btn = tk.Label(parent, text=name, bg=BG, fg=MUTED, font=(FONT, 10),
                           padx=int(round(12 * s)), pady=int(round(5 * s)),
                           cursor="hand2")
            btn.grid(row=0, column=i, sticky="w", padx=(0, int(round(2 * s))))
            ul = tk.Frame(parent, bg=BORDER, height=rail)
            ul.grid(row=1, column=i, sticky="ew")   # 不加间隔，线才连成一条
            btn.bind("<Button-1>", lambda _e, n=name: self._show_page(n))
            btn.bind("<Enter>", lambda _e, b=btn: b.configure(fg=TEXT))
            btn.bind("<Leave>",
                     lambda _e, b=btn, n=name: b.configure(
                         fg=TEXT if self._page_now == n else MUTED))
            entry[1], entry[3] = btn, ul
        # 把线一路铺到右边缘，看起来才像"页首那条横线"
        tk.Frame(parent, bg=BORDER, height=rail).grid(
            row=1, column=len(self.pages), sticky="ew")
        parent.columnconfigure(len(self.pages), weight=1)
        self._page_now = None
        self._show_page(self.pages[0][0])

    def _show_page(self, name):
        self._page_now = name
        for n, btn, page, ul in self.pages:
            if n == name:
                page.tkraise()
                btn.configure(fg=TEXT, font=(FONT, 10, "bold"))
                ul.configure(bg=INK)
            else:
                btn.configure(fg=MUTED, font=(FONT, 10))
                ul.configure(bg=BORDER)

    # ---- 页 1：接入地址 ----
    def _build_addr_tab(self, parent):
        s = self.ui_scale
        tab = ttk.Frame(parent, padding=int(round(14 * s)))
        tab.grid(row=0, column=0, sticky="nsew")
        self._add_page("接入地址", tab)

        key_box = ttk.Labelframe(tab, text=" 你的 API 密钥（Go 套餐的密钥） ", padding=10)
        key_box.pack(fill="x", pady=(0, 10))
        row = ttk.Frame(key_box)
        row.pack(fill="x")
        self.key_entry = ttk.Entry(row, textvariable=self.key_var, show="*")
        self.key_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="显示", width=6,
                   command=self._toggle_key).pack(side="left", padx=(6, 0))
        ttk.Button(row, text="保存", width=6, style="Primary.TButton",
                   command=self._save_key).pack(side="left", padx=4)
        ttk.Button(row, text="复制", width=6,
                   command=lambda: self.copy(self.key_var.get().strip(), "密钥")).pack(side="left")

        links = ttk.Frame(key_box)
        links.pack(fill="x", pady=(8, 0))
        # 这三个是"跳出去看看"的入口，做成没有边框的幽灵按钮，画面干净点
        ttk.Button(links, text="打开 OpenCode 官网", style="Ghost.TButton",
                   command=lambda: self.open_url(SITE_URL)).pack(side="left")
        ttk.Button(links, text="打开 API 密钥管理页", style="Ghost.TButton",
                   command=lambda: self.open_url(CONSOLE_URL)).pack(side="left", padx=6)
        ttk.Button(links, text="打开用量监控页", style="Ghost.TButton",
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
    def _build_model_tab(self, parent):
        s = self.ui_scale
        tab = ttk.Frame(parent, padding=int(round(14 * s)))
        tab.grid(row=0, column=0, sticky="nsew")
        self._add_page("模型清单", tab)

        top = ttk.Frame(tab)
        top.pack(fill="x", pady=(0, 6))
        ttk.Label(top, text="搜索", style="Hint.TLabel").pack(side="left")
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
        ttk.Label(srt, text="排序", style="Hint.TLabel").pack(side="left")
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
        # 隔一行铺一点点浅灰底：列多的时候眼睛不容易"串行"（颜色很浅，不抢眼）
        self.tree.tag_configure("odd", background="#fafafa")
        self._fill_tree()
        self._update_sort_label()

    def _badge(self, vendor: str):
        """拿到某个厂商的徽章图片（第一次用的时候现画）。"""
        if vendor not in self.badges:
            short, color = VENDORS.get(vendor, ("?", "#9aa3b2"))
            size = int(round(16 * self.ui_scale))        # 徽章缩小一点
            gap = int(round(9 * self.ui_scale))          # 右侧留白 = 与模型名拉开距离
            self.badges[vendor] = make_badge(short, color, size=size, pad_right=gap)
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
        for i, (name, mid, vendor, inp, outp, quota) in enumerate(rows):
            self.tree.insert("", "end", image=self._badge(vendor), text=name,
                             tags=("odd",) if i % 2 else (),
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
    def _build_help_tab(self, parent):
        s = self.ui_scale
        tab = ttk.Frame(parent, padding=int(round(14 * s)))
        tab.grid(row=0, column=0, sticky="nsew")
        self._add_page("使用说明", tab)
        # 底色用极浅灰、加一根细灰边，看起来像一张"纸"，跟官网的代码块一个感觉
        text = tk.Text(tab, wrap="word", font=(FONT, 10), bg=SURFACE, fg=TEXT,
                       relief="flat", padx=14, pady=12, borderwidth=0,
                       highlightthickness=1, highlightbackground=BORDER,
                       highlightcolor=BORDER, insertbackground=TEXT,
                       width=72, height=14)
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
        # 上次记住的设置：要不要托盘模式 / 要不要开机自启
        self.tray_enabled = bool(data.get("tray", False))
        self.autostart = bool(data.get("autostart", False))
        # 上次记住的窗口位置和大小（下次打开就还在老地方，不闪）
        geom = data.get("geom")
        if isinstance(geom, str) and _parse_geom(geom):
            self.win_geom = geom

    def _current_geom(self):
        """现在窗口的位置和大小，形如 "1100x800+320+180"。"""
        try:
            geom = self.root.geometry()
        except Exception:
            return None
        return geom if _parse_geom(geom) else None

    def _save_config(self):
        data = {"api_key": self.key_var.get().strip(),
                "port": self._port() or DEFAULT_PORT,
                "tray": self.tray_enabled,
                "autostart": self.autostart}
        geom = self._current_geom()
        if geom:
            data["geom"] = geom
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

    # ---------- 设置窗口 ----------
    def _open_settings(self):
        """打开"设置"小窗口（里面就是两个开关）。"""
        s = self.ui_scale
        if self.settings_win is not None and self.settings_win.winfo_exists():
            self.settings_win.deiconify()
            self.settings_win.lift()
            self.settings_win.focus_force()
            return

        self._show_overlay()          # 先给主窗口盖一层"黑色半透明纱"
        win = tk.Toplevel(self.root)
        self.settings_win = win
        win.title("设置")
        win.configure(bg=BG)
        win.resizable(False, False)
        win.transient(self.root)      # 归主窗口管：一直压在主窗口上面
        try:
            if os.path.exists(ICON_PATH):
                win.iconbitmap(default=ICON_PATH)
        except Exception:
            pass

        box = tk.Frame(win, bg=BG, padx=int(round(20 * s)), pady=int(round(16 * s)))
        box.pack(fill="both", expand=True)

        tk.Label(box, text="设置", bg=BG, fg=TEXT,
                 font=(FONT, 13, "bold")).pack(anchor="w")
        tk.Frame(box, bg=BORDER, height=max(1, int(round(1 * s)))).pack(
            fill="x", pady=(int(round(8 * s)), int(round(12 * s))))

        self.tray_var = tk.BooleanVar(value=self.tray_enabled)
        self.auto_var = tk.BooleanVar(value=self.autostart)

        def make_switch(text, var, tip, cmd):
            row = tk.Frame(box, bg=BG)
            row.pack(fill="x", pady=(0, int(round(12 * s))))
            cb = tk.Checkbutton(row, text=text, variable=var, command=cmd,
                                bg=BG, fg=TEXT, activebackground=BG, activeforeground=TEXT,
                                selectcolor="#ffffff", highlightthickness=0, bd=0,
                                font=(FONT, 10, "bold"), anchor="w", cursor="hand2")
            cb.pack(anchor="w")
            tk.Label(row, text=tip, bg=BG, fg=MUTED, font=(FONT, 9),
                     justify="left", wraplength=int(round(320 * s))).pack(
                anchor="w", padx=(int(round(22 * s)), 0))

        make_switch("托盘模式", self.tray_var,
                    "打开后：点窗口右上角的 × 不退出，只是缩小到右下角的小图标里，"
                    "桥接照常跑；想彻底退出就右键那个小图标选「退出」。",
                    self._on_toggle_tray)
        make_switch("开机自动启动", self.auto_var,
                    "打开后：每次开机程序自己起来（后台安静运行，不弹窗口），"
                    "就不用每次手动点了。",
                    self._on_toggle_autostart)

        if not TRAY_OK:
            tk.Label(box, text="（这台电脑没装托盘组件 pystray，托盘模式暂时用不了。）",
                     bg=BG, fg=ORANGE, font=(FONT, 9), justify="left",
                     wraplength=int(round(320 * s))).pack(
                anchor="w", pady=(0, int(round(10 * s))))

        tk.Frame(box, bg=BORDER, height=max(1, int(round(1 * s)))).pack(
            fill="x", pady=(int(round(4 * s)), int(round(12 * s))))
        ttk.Button(box, text="完成", style="Primary.TButton",
                   command=self._close_settings).pack(anchor="e")

        # 位置：算好了再露脸 —— 直接落在主窗口正中间，不会先闪到屏幕左上角
        win.update_idletasks()
        w = win.winfo_reqwidth()
        h = win.winfo_reqheight()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - h) // 2
        win.geometry(f"{w}x{h}+{x}+{y}")
        # Windows 会给窗口套一圈边框/标题栏，实际位置会比上面算的偏一点点，
        # 所以量一下"实际中心"和"目标中心"差多少，再挪正一次。
        win.update_idletasks()
        mx = self.root.winfo_rootx() + self.root.winfo_width() // 2
        my = self.root.winfo_rooty() + self.root.winfo_height() // 2
        sx = win.winfo_rootx() + win.winfo_width() // 2
        sy = win.winfo_rooty() + win.winfo_height() // 2
        win.geometry(f"+{x + (mx - sx)}+{y + (my - sy)}")
        win.protocol("WM_DELETE_WINDOW", self._close_settings)
        win.lift()
        try:
            win.grab_set()            # 打开设置时"锁住"主窗口：点它没反应，只能先关设置
        except Exception:
            pass
        win.focus_force()

    # ---- 主窗口上那层"黑色半透明纱"（设置打开时盖上去）----
    def _show_overlay(self):
        self._hide_overlay()
        try:
            self.root.update_idletasks()
            ov = tk.Toplevel(self.root)
            ov.overrideredirect(True)          # 不要标题栏，就是一块纯色
            ov.configure(bg="#000000")
            try:
                ov.attributes("-alpha", 0.32)  # 半透明：能看见底下的界面，但暗下去
            except Exception:
                pass
            ov.geometry(self._overlay_geom())
            ov.transient(self.root)
            ov.lift()
            self.overlay = ov
            # 主窗口要是被挪动或改变大小，"纱"跟着一起变
            self._overlay_bind = self.root.bind("<Configure>", self._sync_overlay, add="+")
        except Exception:
            self.overlay = None

    def _overlay_geom(self) -> str:
        """那块"纱"应该盖在哪儿（正好盖住主窗口）。"""
        x = self.root.winfo_rootx()
        y = self.root.winfo_rooty()
        w = self.root.winfo_width()
        h = self.root.winfo_height()
        return f"{w}x{h}+{x}+{y}"

    def _sync_overlay(self, _event=None):
        if self.overlay is None:
            return
        try:
            if not self.overlay.winfo_exists():
                return
            self.overlay.geometry(self._overlay_geom())
            self.overlay.lift()
            # "纱"抬起来之后，得把设置窗再抬回来，不然设置窗会被压在纱下面变暗
            if self.settings_win is not None and self.settings_win.winfo_exists():
                self.settings_win.lift()
        except Exception:
            pass

    def _hide_overlay(self):
        if self._overlay_bind is not None:
            try:
                self.root.unbind("<Configure>", self._overlay_bind)
            except Exception:
                pass
            self._overlay_bind = None
        if self.overlay is not None:
            try:
                self.overlay.destroy()
            except Exception:
                pass
            self.overlay = None

    def _close_settings(self):
        if self.settings_win is not None and self.settings_win.winfo_exists():
            try:
                self.settings_win.grab_release()
            except Exception:
                pass
            self.settings_win.destroy()
        self.settings_win = None
        self._hide_overlay()

    def _on_toggle_tray(self):
        want = bool(self.tray_var.get())
        if want and not TRAY_OK:
            self.tray_var.set(False)
            messagebox.showwarning(
                "用不了托盘模式",
                "这台电脑没装托盘组件 pystray，托盘模式暂时用不了。\n"
                "其它功能不受影响，可以正常用。")
            return
        self._apply_tray(want)

    def _on_toggle_autostart(self):
        self.autostart = bool(self.auto_var.get())
        self._sync_autostart()
        self._save_config()

    # ---------- 托盘图标（右下角那个小图标）----------
    def _tray_image(self):
        """托盘图标的图片。优先用 icon.png，读不到就现画一个黑底白字的小方块。"""
        try:
            if os.path.exists(ICON_PNG):
                return PILImage.open(ICON_PNG).convert("RGBA").resize((64, 64))
        except Exception:
            pass
        img = PILImage.new("RGBA", (64, 64), (17, 17, 17, 255))
        return img

    def _ensure_tray(self) -> bool:
        """把托盘图标挂上去。已经挂了就什么都不做。成功返回 True。"""
        if self.tray is not None:
            return True
        if not TRAY_OK:
            return False
        try:
            menu = pystray.Menu(
                pystray.MenuItem("显示主界面", self._tray_show, default=True),
                pystray.MenuItem("退出 OpenCode2Trae", self._tray_quit),
            )
            self.tray = pystray.Icon(APP_NAME, self._tray_image(), APP_NAME, menu)
            self.tray.run_detached()
            return True
        except Exception as exc:
            self.tray = None
            self._log_line(f"托盘图标没挂上：{exc}")
            return False

    def _remove_tray(self):
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
            self.tray = None

    # 下面两个是被"托盘菜单"点到的 —— 它们跑在托盘的线程里，
    # 而界面（Tk）只能在主线程动，所以用 root.after(0, ...) 丢回主线程做。
    def _tray_show(self, *_a):
        self.root.after(0, self._restore_from_tray)

    def _tray_quit(self, *_a):
        self.root.after(0, self._real_quit)

    def _restore_from_tray(self):
        """把窗口从托盘里"叫回来"。"""
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except Exception:
            pass

    def _hide_to_tray(self):
        """把窗口藏起来（但程序还在后台跑）。"""
        self._close_settings()      # 设置还开着就先收掉（连那层"纱"一起）
        self.root.withdraw()
        if self.tray is not None:
            try:
                self.tray.notify("程序还在后台跑，桥接没断。右键小图标可以退出。", APP_NAME)
            except Exception:
                pass

    def _apply_tray(self, on: bool):
        """开/关托盘模式。"""
        if on:
            if not self._ensure_tray():
                self.tray_enabled = False
                if self.tray_var is not None:
                    self.tray_var.set(False)
                return
            self.tray_enabled = True
            self._log_line("托盘模式已打开：关窗口 = 收到托盘，不再退出。")
        else:
            self.tray_enabled = False
            self._remove_tray()
            self._restore_from_tray()
            self._log_line("托盘模式已关闭：关窗口就是真的退出。")
        self._sync_autostart()      # 开机自启那行命令要不要带 --tray，得跟着改
        self._save_config()

    def _sync_autostart(self):
        """把"开机自启"这个开关同步到 Windows 注册表里。"""
        err = autostart_set(self.autostart, self.tray_enabled)
        if err:
            self._log_line(f"开机启动没设置成功：{err}")
        elif self.autostart:
            self._log_line("开机自动启动已打开。")
        else:
            self._log_line("开机自动启动已关闭。")

    def _apply_settings_on_start(self):
        """程序刚起来时，按上次记住的设置把托盘图标挂上 / 对齐开机自启。"""
        if self.tray_enabled and TRAY_OK:
            if not self._ensure_tray():
                self.tray_enabled = False
        if self.autostart:
            self._sync_autostart()

    def _start_hidden(self) -> bool:
        """开机自启带 --tray 起来时：别弹窗口，安静待在托盘里。

        返回"有没有真的藏起来"，好让 main() 决定要不要露脸。
        """
        if self.tray_enabled and self.tray is not None:
            self.root.withdraw()
            return True
        return False

    # ---------- 关窗口 ----------
    def _on_close(self):
        if self.tray_enabled and self.tray is not None:
            self._hide_to_tray()    # 开了托盘模式：只是收起来，不退出
            return
        self._real_quit()

    def _real_quit(self):
        self._close_settings()      # 设置还开着就一起收掉
        self._restore_from_tray()   # 万一还藏在托盘里，先露出来，确认框才看得见
        if self.server is not None:
            if not messagebox.askyesno("确认", "关掉窗口会同时关闭桥接，Trae 就断了。确定吗？"):
                if self.tray_enabled and self.tray is not None:
                    self._hide_to_tray()
                return
            self.server.stop()
            self.server = None
        self._remove_tray()
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
    · 右上角有「设置」，里面两个开关：
        「托盘模式」= 点窗口的 × 不退出，只缩到右下角小图标里（桥接照常跑）；
                     想彻底退出，就右键那个小图标选「退出」。
        「开机自动启动」= 每次开机程序自己起来，不用手动点。
    · 「接入地址」页有三个直达按钮：官网、API 密钥管理页、用量监控页（看这个月还剩多少额度）。
    · 「模型清单」页可以按「模型能力」或「价格」排序，每个模型后面还有单价和每月额度参考。
      名字前面那个彩色小方块 = 是哪家厂商（比如 o 绿 = OpenAI，A 橙 = Anthropic）。
    · 同一排的排序按钮再点一下 = 反过来排。

要注意的事
    · 本程序必须一直开着。关掉窗口 = 断线（开了「托盘模式」除外，那样只是收到托盘里）。
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


GEOM_RE = re.compile(r"^(\d+)x(\d+)([+-]\d+)([+-]\d+)$")


def _parse_geom(text):
    """把 "1100x800+320+180" 这种窗口位置字符串拆成 (宽, 高, x, y)；拆不动就返回 None。

    打个比方：这行字就像快递单上的"离左墙 320、离上墙 180、箱子 1100×800"，
    我们要把它读成 4 个数字才能照着摆窗口。
    """
    if not isinstance(text, str):
        return None
    # Tk 在坐标是负数时会写成 "1783x1529+-252+244"，先把 "+-" 归一成 "-"
    t = text.strip().replace("+-", "-")
    m = GEOM_RE.match(t)
    if not m:
        return None
    try:
        return int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
    except Exception:
        return None


def _virtual_screen():
    """整个"桌面"的范围（多显示器时是并起来的一大块），返回 (左, 上, 宽, 高)。

    单显示器时一般就是 (0, 0, 屏幕宽, 屏幕高)。
    """
    try:
        left = ctypes.windll.user32.GetSystemMetrics(76)    # 虚拟桌面左上角 X
        top = ctypes.windll.user32.GetSystemMetrics(77)     # 虚拟桌面左上角 Y
        width = ctypes.windll.user32.GetSystemMetrics(78)   # 虚拟桌面总宽
        height = ctypes.windll.user32.GetSystemMetrics(79)  # 虚拟桌面总高
        if width > 0 and height > 0:
            return left, top, width, height
    except Exception:
        pass
    return 0, 0, None, None


def _geom_visible(w: int, h: int, x: int, y: int) -> bool:
    """记忆里的那个位置，现在还看得见吗？（换过显示器 / 拔了外接屏就可能跑到屏幕外）

    只要窗口的标题栏那一条还落在桌面范围内，就算"看得见"。
    """
    vx, vy, vw, vh = _virtual_screen()
    if vw is None:
        return True     # 拿不到桌面范围就别拦着，按原样用
    # 标题栏中心点
    cx = x + w // 2
    cy = y + 12
    return (vx <= cx <= vx + vw) and (vy <= cy <= vy + vh)


def _place_window(root: tk.Tk, app):
    """决定窗口开在哪儿、多大 —— 位置算好之前先别露脸，免得"闪一下"。

    规则：
      · 第一次打开（没记忆）：乖乖停在屏幕正中间。
      · 以后打开（有记忆）：回到上次关掉时的位置和大小。
      · 记忆的位置要是跑到屏幕外了（换过显示器）：退回屏幕正中间。
    """
    root.update_idletasks()
    need_w = root.winfo_reqwidth() + 20
    need_h = root.winfo_reqheight() + 20
    # 居中按"主屏幕"算（多显示器时，主屏左上角就是 0,0）
    screen_w = root.winfo_screenwidth()
    screen_h = root.winfo_screenheight()

    # 窗口最小尺寸：内容需要多大就多大，但不能比屏幕还大
    min_w = min(need_w, int(screen_w * 0.96))
    min_h = min(need_h, int(screen_h * 0.92))
    root.minsize(min_w, min_h)

    geom = _parse_geom(app.win_geom) if app.win_geom else None
    if geom:
        w, h, x, y = geom
        # 记忆的大小不能小于内容需要的最小值，也不能比屏幕还大
        w = max(min_w, min(w, int(screen_w * 0.96)))
        h = max(min_h, min(h, int(screen_h * 0.92)))
        if _geom_visible(w, h, x, y):
            root.geometry(f"{w}x{h}+{x}+{y}")
            return
    # 头一回打开（或记忆的位置失效了）：主屏幕正中
    w = min_w
    h = min_h
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
    root.withdraw()               # 先藏起来：等位置和大小都算好了再露脸，不会闪
    _set_window_icon(root)
    app = BridgeApp(root)
    _place_window(root, app)
    hidden = app._start_hidden() if "--tray" in sys.argv else False
    if not hidden:                # 开机自启住在托盘里就别弹窗口
        root.deiconify()
    root.mainloop()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
