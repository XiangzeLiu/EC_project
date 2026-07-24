"""
SC - Main Window
涓荤獥鍙ｏ細缁勮鎵鏈夊瓙缁勪欢锛岀鐞嗗叏灞鐘舵併佸揩鎹烽敭銆佽疆璇佽鎯呮祦
"""

import datetime
import json
import queue
import random
import re
import threading
import time
from urllib.parse import quote

# 鏃跺尯鏀寔锛圥ython 3.9+ 浣跨敤 zoneinfo锛屾洿浣庣増鏈洖閫鍒?pytz锛?
try:
    from zoneinfo import ZoneInfo
except ImportError:
    try:
        from pytz import timezone as ZoneTimezone
        # 涓?pytz 鍒涘缓鍏煎鎺ュ彛
        class ZoneInfo:
            def __init__(self, key):
                self._tz = ZoneTimezone(key)
            def localize(self, dt):
                return self._tz.localize(dt)
    except ImportError:
        # 濡傛灉閮芥病鏈夛紝浣跨敤绠鍖栫増锛堟棤 DST 鏀寔锛?
        ZoneInfo = None

import tkinter as tk
from tkinter import messagebox, ttk

from ..constants import *
from ..config import load_credentials, save_credentials
from ..network.http_client import HttpClient
from ..network.ts_websocket import TSWebSocketClient
from ..services.trading_session import TradingSession, sanitize
from .trading_panel import TradingPanel
from .positions_panel import PositionsPanel
from .orders_panel import OrdersPanel
from .log_area import LogArea
from .login_dialog import LoginDialog

SEWebSocketClient = TSWebSocketClient
DEFAULT_SE_HOST = DEFAULT_TS_HOST
DEFAULT_SE_PORT = DEFAULT_TS_PORT
SE_RECONNECT_ENABLED = TS_RECONNECT_ENABLED


_ACTION_LABELS = {
    "Buy to Open": "买开",
    "Buy to Close": "买平",
    "Sell to Open": "卖开",
    "Sell to Close": "卖平",
}

_TIF_LABELS = {
    "Day": "当日有效",
    "GTC": "撤单前有效",
}


def _default_se_target() -> str:
    return DEFAULT_TS_WS_URL or f"{DEFAULT_TS_HOST}:{DEFAULT_TS_PORT}"


def _encode_query_value(value: str) -> str:
    return quote(value or "", safe="")


def _format_order_action(action: str) -> str:
    return _ACTION_LABELS.get(str(action or "").strip(), str(action or "").strip())


def _format_tif_label(tif: str) -> str:
    return _TIF_LABELS.get(str(tif or "").strip(), str(tif or "").strip())


def _localize_user_message(msg: str) -> str:
    text = sanitize(msg).strip()
    if not text:
        return ""

    replacements = {
        "Trade service login succeeded": "\u4ea4\u6613\u670d\u52a1\u767b\u5f55\u6210\u529f",
        "Trade service login required": "\u8bf7\u5148\u767b\u5f55\u4ea4\u6613\u670d\u52a1",
        "Trade service login expired": "\u4ea4\u6613\u670d\u52a1\u767b\u5f55\u5df2\u8fc7\u671f",
        "Trade service login cleared": "\u4ea4\u6613\u670d\u52a1\u767b\u5f55\u5df2\u6e05\u9664",
        "Trade service username and password are required": "\u8bf7\u8f93\u5165\u4ea4\u6613\u670d\u52a1\u8d26\u53f7\u548c\u5bc6\u7801",
        "Trade service login request timed out": "\u4ea4\u6613\u670d\u52a1\u767b\u5f55\u8bf7\u6c42\u8d85\u65f6",
        "Trade service status query timed out": "\u4ea4\u6613\u670d\u52a1\u72b6\u6001\u67e5\u8be2\u8d85\u65f6",
        "Trade service logout request timed out": "\u4ea4\u6613\u670d\u52a1\u767b\u51fa\u8bf7\u6c42\u8d85\u65f6",
        "Trade service broker not connected": "\u4ea4\u6613\u670d\u52a1\u672a\u767b\u5f55",
        "Quote subscribe failed": "\u884c\u60c5\u8ba2\u9605\u5931\u8d25",
        "Quote unsubscribe failed": "\u884c\u60c5\u53d6\u6d88\u8ba2\u9605\u5931\u8d25",
        "Position fetch failed": "\u6301\u4ed3\u83b7\u53d6\u5931\u8d25",
        "Server disconnected": "\u7ba1\u7406\u670d\u52a1\u8fde\u63a5\u5df2\u65ad\u5f00",
        "Trade server connected": "\u4ea4\u6613\u670d\u52a1\u5668\u5df2\u8fde\u63a5",
        "Trade server disconnected": "\u4ea4\u6613\u670d\u52a1\u5668\u5df2\u65ad\u5f00",
        "Trade server reconnect cancelled": "\u5df2\u53d6\u6d88\u4ea4\u6613\u670d\u52a1\u5668\u91cd\u8fde",
        "Trade server connect failed": "\u4ea4\u6613\u670d\u52a1\u5668\u8fde\u63a5\u5931\u8d25",
        "Trade server is offline": "\u4ea4\u6613\u670d\u52a1\u5668\u5f53\u524d\u79bb\u7ebf",
        "Trade server validation failed": "\u4ea4\u6613\u670d\u52a1\u5668\u6821\u9a8c\u5931\u8d25",
        "Trade server lock failed; connection aborted": "\u4ea4\u6613\u670d\u52a1\u5668\u9501\u5b9a\u5931\u8d25\uff0c\u8fde\u63a5\u5df2\u4e2d\u6b62",
        "Trade server login pending reconnect": "\u4ea4\u6613\u670d\u52a1\u767b\u5f55\u7b49\u5f85\u91cd\u8fde",
        "System ready": "\u7cfb\u7edf\u5df2\u5c31\u7eea",
        "Connected, sending auth...": "\u5df2\u8fde\u63a5\uff0c\u6b63\u5728\u53d1\u9001\u9274\u6743\u2026",
        "Logged out": "\u5df2\u9000\u51fa\u767b\u5f55",
        "Connected": "\u5df2\u8fde\u63a5",
        "Not connected": "\u672a\u8fde\u63a5",
        "Subscribed": "\u884c\u60c5\u8ba2\u9605\u6210\u529f",
        "Unsubscribed": "\u884c\u60c5\u53d6\u6d88\u8ba2\u9605\u6210\u529f",
        "Subscribe failed": "\u884c\u60c5\u8ba2\u9605\u5931\u8d25",
        "Unsubscribe failed": "\u884c\u60c5\u53d6\u6d88\u8ba2\u9605\u5931\u8d25",
        "Order failed": "\u4e0b\u5355\u5931\u8d25",
        "Cancel failed": "\u64a4\u5355\u5931\u8d25",
    }
    if text in replacements:
        return replacements[text]

    reconnect_match = re.fullmatch(r"Reconnecting \((\d+)\)\.\.\.(.*)", text)
    if reconnect_match:
        suffix = reconnect_match.group(2).strip()
        if suffix.startswith("|"):
            suffix = f" | {suffix[1:].strip()}"
        elif suffix:
            suffix = f" {suffix}"
        return f"\u91cd\u8fde\u4e2d\uff08{reconnect_match.group(1)}\uff09\u2026{suffix}"

    reconnect_failed_match = re.fullmatch(r"Reconnect failed after (\d+) attempts: (.+)", text)
    if reconnect_failed_match:
        return (
            f"\u91cd\u8fde\u5931\u8d25\uff0c\u5df2\u5c1d\u8bd5 {reconnect_failed_match.group(1)} "
            f"\u6b21\uff1a{reconnect_failed_match.group(2)}"
        )

    connect_target_match = re.fullmatch(r"Connecting to (.+)\.\.\.", text)
    if connect_target_match:
        return f"\u6b63\u5728\u8fde\u63a5\uff1a{connect_target_match.group(1)}"

    authenticated_match = re.fullmatch(r"Authenticated! Session: (.+)", text)
    if authenticated_match:
        return f"\u9274\u6743\u6210\u529f\uff0c\u4f1a\u8bdd\uff1a{authenticated_match.group(1)}"

    startswith_replacements = (
        ("Trade service login failed:", "\u4ea4\u6613\u670d\u52a1\u767b\u5f55\u5931\u8d25\uff1a"),
        ("Trade service error [", "\u4ea4\u6613\u670d\u52a1\u5668\u9519\u8bef["),
        ("Trade server error [", "\u4ea4\u6613\u670d\u52a1\u5668\u9519\u8bef["),
        ("Trade server connection released by manager (", "\u4ea4\u6613\u670d\u52a1\u5668\u8fde\u63a5\u5df2\u88ab\u7ba1\u7406\u7aef\u91ca\u653e\uff08"),
        ("Trade server is occupied by ", "\u4ea4\u6613\u670d\u52a1\u5668\u5df2\u88ab\u5360\u7528\uff1a"),
        ("Trade server validation failed:", "\u4ea4\u6613\u670d\u52a1\u5668\u6821\u9a8c\u5931\u8d25\uff1a"),
        ("Trade server connect failed:", "\u4ea4\u6613\u670d\u52a1\u5668\u8fde\u63a5\u5931\u8d25\uff1a"),
        ("Quote subscribe failed:", "\u884c\u60c5\u8ba2\u9605\u5931\u8d25\uff1a"),
        ("Quote unsubscribe failed:", "\u884c\u60c5\u53d6\u6d88\u8ba2\u9605\u5931\u8d25\uff1a"),
        ("Position fetch failed:", "\u6301\u4ed3\u83b7\u53d6\u5931\u8d25\uff1a"),
        ("Login failed (HTTP ", "\u767b\u5f55\u5931\u8d25\uff08HTTP "),
        ("Order failed:", "\u4e0b\u5355\u5931\u8d25\uff1a"),
        ("Order submitted", "\u4e0b\u5355\u5df2\u63d0\u4ea4"),
        ("Order ", "\u8ba2\u5355 "),
        ("Disconnected:", "\u8fde\u63a5\u65ad\u5f00\uff1a"),
        ("Connection error", "\u8fde\u63a5\u9519\u8bef"),
        ("Error [", "\u4ea4\u6613\u670d\u52a1\u5668\u9519\u8bef["),
        ("Auth failed", "\u9274\u6743\u5931\u8d25"),
    )
    for prefix, repl in startswith_replacements:
        if text.startswith(prefix):
            if prefix == "Trade server connection released by manager (" and text.endswith(")"):
                inner = text[len(prefix):-1]
                return f"\u4ea4\u6613\u670d\u52a1\u5668\u8fde\u63a5\u5df2\u88ab\u7ba1\u7406\u7aef\u91ca\u653e\uff08{inner}\uff09"
            return repl + text[len(prefix):]

    text = text.replace("TS not connected", "\u4ea4\u6613\u670d\u52a1\u5668\u672a\u8fde\u63a5")
    text = text.replace("SE not connected", "\u4ea4\u6613\u670d\u52a1\u5668\u672a\u8fde\u63a5")
    text = text.replace("remote host refused connection (port may not be ready)", "\u8fdc\u7a0b\u4e3b\u673a\u62d2\u7edd\u8fde\u63a5\uff08\u7aef\u53e3\u53ef\u80fd\u5c1a\u672a\u5c31\u7eea\uff09")
    text = text.replace("broker", "\u5238\u5546")
    return text


def _localize_step_status(status: str) -> str:
    text = str(status or "").strip()
    if not text:
        return ""

    replacements = {
        "Waiting": "\u7b49\u5f85\u4e2d",
        "Connecting...": "\u8fde\u63a5\u4e2d\u2026",
        "Connected": "\u5df2\u8fde\u63a5",
        "Failed": "\u5931\u8d25",
        "Retrying...": "\u91cd\u8bd5\u4e2d\u2026",
        "Validating SE...": "\u6821\u9a8c\u4ea4\u6613\u670d\u52a1\u5668\u2026",
        "Validating...": "\u6821\u9a8c\u4e2d\u2026",
    }
    if text in replacements:
        return replacements[text]

    connect_match = re.fullmatch(r"Connecting \((\d+)/(\d+)\)\.\.\.", text)
    if connect_match:
        return f"\u8fde\u63a5\u4e2d\uff08{connect_match.group(1)}/{connect_match.group(2)}\uff09\u2026"

    se_ok_match = re.fullmatch(r"SE OK \((.+)\)", text)
    if se_ok_match:
        return f"\u4ea4\u6613\u670d\u52a1\u5668\u5c31\u7eea\uff08{se_ok_match.group(1)}\uff09"

    return _localize_user_message(text)


class TradingTerminal(tk.Tk):
    """UI helper."""

    def __init__(self):
        super().__init__()
        self.title("SC")

        # 绐楀彛灏哄涓庡眳涓?
        self.update_idletasks()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        w = min(1400, int(sw * 0.90))
        h = min(920, int(sh * 0.85))
        x = (sw - w) // 2
        y = (sh - h) // 2
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.minsize(1400, 750)
        self.configure(bg=DARK_BG)

        # 鈹鈹 棰勫垵濮嬪寲寮曠敤锛堥伩鍏嶅睘鎬ч敊璇級鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹
        self.http = HttpClient()
        self.session = None
        self.panels: dict[int, TradingPanel] = {}
        self.active_panel_id: int = 0
        self.quote_queue = queue.Queue()
        self.sub_queue = queue.Queue()
        self.current_quote: dict[str, dict] = {}
        self._quote_ui_pending: dict[str, tuple[dict, dict | None]] = {}
        self._quote_ui_flush_job: str | None = None
        self.mock_base: dict[str, float] = {}
        self._stream_active: bool = False
        self._mock_active: bool = False
        self._quote_subscribed_symbols: set[str] = set()
        self._quote_sub_lock = threading.Lock()
        self._last_pos_time: float = 0

        self._last_orders_time: float = 0
        self._last_heartbeat: float = 0
        self.positions_panel: PositionsPanel | None = None
        self.orders_panel: OrdersPanel | None = None
        self.status_var: tk.StringVar = None
        self.status_lbl: tk.Label = None
        self.time_var: tk.StringVar = None
        self._time_zone_cn: bool = True  # True=涓浗鏃堕棿, False=缇庡浗鏃堕棿
        self.log_area: LogArea | None = None
        self._last_ui_error_message = ""
        self._last_ui_error_at = 0.0
        
        # 鏃跺尯瀵硅薄锛堢敤浜?DST 鏀寔锛?
        if ZoneInfo is not None:
            try:
                self._tz_cn = ZoneInfo("Asia/Shanghai")  # 涓浗鏃跺尯
                self._tz_us = ZoneInfo(TZ_ET_NAME)        # 缇庡浗涓滈儴鏃跺尯
            except Exception:
                self._tz_cn = None
                self._tz_us = None
        else:
            self._tz_cn = None
            self._tz_us = None

        # SE 鐩磋繛缁勪欢
        self._se_client: SEWebSocketClient | None = None
        self._se_connected: bool = False
        self._se_status_var: tk.StringVar = None
        self._se_status_lbl: tk.Label | None = None
        self._se_btn: tk.Button | None = None
        self._logout_btn: tk.Button | None = None
        self._broker_status_var: tk.StringVar = tk.StringVar(value="\u4ea4\u6613\u670d\u52a1\uff1a\u672a\u767b\u5f55")
        self._broker_status_lbl: tk.Label | None = None
        self._broker_user_entry: tk.Entry | None = None
        self._broker_pass_entry: tk.Entry | None = None
        self._broker_login_btn: tk.Button | None = None
        self._session_id: str = ""
        self._se_status_connected: bool = False
        self._se_dot_phase: int = 0
        self._se_dot_job = None


        self._node_info: dict = {}
        self._se_target_address: str = ""  # 鐧诲綍鍚庡姩鎬佽幏鍙栫殑 SE 鍦板潃
        self._se_server_id: str = ""       # 褰撳墠 SE 瀵瑰簲鐨?server_id锛堢敤浜庡崰鐢?閲婃斁锛?
        self._se_connection_id: str = ""
        self._last_connected_se: str = ""   # 鏈杩戜竴娆¤繛鎺ョ殑 SE 鍦板潃
        self._login_username: str = ""      # 褰撳墠鐧诲綍鐢ㄦ埛鍚?
        self._login_password: str = ""      # 褰撳墠鐧诲綍瀵嗙爜锛堝彇娑堣繑鍥炴椂鍥炲～锛?

        # 鍒濆鍖栫晫闈㈠鍣紙鍗犳弧绐楀彛锛屽悗缁攢姣佸悗鏇挎崲涓轰富鐣岄潰锛?
        self._init_frame: tk.Frame | None = None
        self._init_ready = False   # 鏍囪锛氬叏閮ㄨ繛鎺ユ垚鍔熷悗鎵嶆瀯寤轰富鐣岄潰

        # 鈹鈹 SE 閲嶈繛鐩稿叧鐘舵?鈹鈹
        self._reconnecting: bool = False          # 鏄惁姝ｅ湪鑷姩閲嶈繛涓?
        self._reconnect_dialog: tk.Toplevel | None = None  # 閲嶈繛寮圭獥寮曠敤
        self._reconnect_cancelled: bool = False   # 鐢ㄦ埛鏄惁鍙栨秷浜嗛噸杩?
        self._reconnect_var: tk.StringVar = tk.StringVar(value="")  # 閲嶈繛鐘舵佹枃鏈?

        # 鏄剧ず鍒濆鍖栬繛鎺ョ晫闈?
        self._show_init_screen()

        # 鍚姩鏃跺厛寮瑰嚭鐧诲綍鐣岄潰锛岀敤鎴风偣鍑荤櫥褰曞悗鎵嶈繘琛岃繛鎺ラ獙璇?
        self.after(200, self._show_login_first)

    # 鈹鈹 Init Screen & Connection Flow 鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹

    def _show_init_screen(self):
        """UI helper."""
        self._init_frame = tk.Frame(self, bg=DARK_BG)
        self._init_frame.pack(fill="both", expand=True)

        # 灞呬腑瀹瑰櫒
        center = tk.Frame(self._init_frame, bg=DARK_BG)
        center.place(relx=0.5, rely=0.45, anchor="center")

        tk.Label(center, text="SC",
                 bg=DARK_BG, fg="#4ea1ff", font=FONT_TITLE).pack(pady=(0, 8))

        tk.Label(center, text="\u8fde\u63a5\u4e2d\u2026",
                 bg=DARK_BG, fg=TEXT_DIM, font=FONT_UI_SM).pack(pady=(0, 28))

        # 姝ラ鐘舵佹爣绛撅紙鏄剧ず鍦?UI 涓婏紝渚夸簬浜ゆ槗鍛樺揩閫熷垽鏂繛鎺ラ樁娈碉級
        steps = tk.Frame(center, bg=DARK_BG)
        steps.pack(fill="x", pady=(0, 12))
        self._init_steps: dict[str, tuple[tk.Label, tk.StringVar]] = {}
        for key, title, default in (
            ("auth", "\u8d26\u53f7\u767b\u5f55", "Waiting"),
            ("sm", "\u7ba1\u7406\u670d\u52a1", "Connecting..."),
            ("se", "\u4ea4\u6613\u670d\u52a1", "Waiting"),
        ):
            row = tk.Frame(steps, bg=DARK_BG)
            row.pack(fill="x", pady=1)
            tk.Label(row, text=f"{title}", bg=DARK_BG, fg=TEXT_DIM, font=FONT_UI_SM, width=12, anchor="w").pack(side="left")
            var = tk.StringVar(value=_localize_step_status(default))
            lbl = tk.Label(row, textvariable=var, bg=DARK_BG,
                           fg=ACCENT_YELLOW if key == "sm" else TEXT_MUTED,
                           font=FONT_MONO_SM, anchor="w")
            lbl.pack(side="left", padx=(6, 0))
            self._init_steps[key] = (lbl, var)

        # 搴曢儴鎻愮ず淇℃伅锛堝瓧浣撳姞澶э級
        self._init_hint_var = tk.StringVar(value="")
        tk.Label(center, textvariable=self._init_hint_var, bg=DARK_BG,
                 fg=ACCENT_RED, font=FONT_UI, wraplength=500,
                 justify="center").pack(pady=(16, 0))


        # 鎸夐挳瀹瑰櫒锛堥噸璇?+ 鍙栨秷锛?
        btn_container = tk.Frame(center, bg=DARK_BG)
        btn_container.pack(pady=(16, 0))
        # 閲嶈瘯鎸夐挳锛堝垵濮嬮殣钘忥級
        self._retry_btn = tk.Button(
            btn_container, text="\u91cd\u8bd5", font=FONT_UI_SM,
            bg=BUTTON_NEUTRAL_BG, fg=ACCENT_BLUE, activebackground=BUTTON_ACTIVE_BG,
            activeforeground=TEXT_PRIMARY, relief="flat", cursor="hand2",
            padx=20, pady=6, command=self._on_init_retry,
        )
        self._bind_button_hover(self._retry_btn, BUTTON_NEUTRAL_BG)

        # 鍙栨秷鎸夐挳锛堝垵濮嬮殣钘忥紝鐐瑰嚮杩斿洖鐧诲綍鐣岄潰锛?
        self._cancel_btn = tk.Button(
            btn_container, text="\u53d6\u6d88", font=FONT_UI_SM,
            bg=BUTTON_NEUTRAL_BG, fg=ACCENT_RED, activebackground=BUTTON_ACTIVE_BG,
            activeforeground=TEXT_PRIMARY, relief="flat", cursor="hand2",
            padx=20, pady=6, command=self._on_init_cancel,
        )
        self._bind_button_hover(self._cancel_btn, BUTTON_NEUTRAL_BG)


    def _update_init_step(self, step_key: str, status: str, color: str = None):
        """UI helper."""
        if step_key not in self._init_steps:
            return
        lbl, var = self._init_steps[step_key]
        var.set(_localize_step_status(status))
        if color:
            lbl.config(fg=color)
        self.update_idletasks()

    @staticmethod
    def _bind_button_hover(btn: tk.Button, normal_bg: str):
        btn.bind("<Enter>", lambda e: btn.config(bg=BUTTON_HOVER_BG))
        btn.bind("<Leave>", lambda e: btn.config(bg=normal_bg))

    def _start_se_status_dot_animation(self):
        """UI helper."""
        if self._se_dot_job is None:
            self._tick_se_status_dot()

    def _tick_se_status_dot(self):
        """UI helper."""
        self._se_dot_job = None

        if not self.winfo_exists():
            return

        if self._se_status_var and self._se_status_lbl and self._se_status_lbl.winfo_exists():
            phase = self._se_dot_phase % 4
            if self._se_status_connected:
                marker = '[+]'
                colors = ["#2fcf7b", "#66e6a1", ACCENT_GREEN, "#66e6a1"]
                self._se_status_var.set(f"{marker} \u5df2\u8fde\u63a5")
                self._se_status_lbl.config(fg=colors[phase])
            else:
                dots = ['.', '..', '...', '....']
                colors = ["#ff5c5c", "#ff7b7b", ACCENT_RED, "#ff7b7b"]
                self._se_status_var.set(f"{dots[phase]} \u672a\u8fde\u63a5")
                self._se_status_lbl.config(fg=colors[phase])

            self._se_dot_phase = (self._se_dot_phase + 1) % 4

        self._se_dot_job = self.after(430, self._tick_se_status_dot)

    def _set_se_connection_ui(self, connected: bool):
        """UI helper."""
        self._se_status_connected = bool(connected)
        self._se_dot_phase = 0

        if self._se_status_var and self._se_status_lbl:
            self._start_se_status_dot_animation()

        if self._se_btn:
            self._se_btn.config(text="\u65ad\u5f00\u8fde\u63a5" if connected else "\u8fde\u63a5\u4ea4\u6613\u670d\u52a1", state="normal")


    def _show_login_first(self):
        """UI helper."""
        self.session = TradingSession(self.http)
        # 棣栨鎵撳紑涓嶅～鍏咃紝鍙栨秷杩斿洖鏃跺～鍏呬笂娆¤緭鍏ョ殑鍑嵁
        login = LoginDialog(
            self, auth_fn=self.session.login,
            default_user=self._login_username,
            default_pass=self._login_password,
        )
        creds = login.credentials
        if not creds:
            # 鐢ㄦ埛鍏抽棴浜嗙櫥褰曠獥鍙?
            self.quit()
            return

        username, password = creds
        if not self.session.connected:
            # 鐧诲綍璁よ瘉澶辫触锛岄噸鏂板脊鍑虹櫥褰曟璁╃敤鎴烽噸璇曪紙淇濈暀宸茶緭鍏ョ殑璐﹀彿瀵嗙爜锛?
            self._login_username = username
            self._login_password = password
            self.after(0, lambda: self._show_login_first())
            return

        self._login_username = username
        self._login_password = password
        save_credentials(username, password)

        # 鐧诲綍鎴愬姛 鈫?鍚姩 SM 妫鏌?+ SE 楠岃瘉娴佺▼
        self.after(0, self._start_connection_flow)

    def _start_connection_flow(self):
        """
        鐧诲綍鎴愬姛鍚庣殑杩炴帴娴佺▼锛圫M妫鏌?鈫?SE鍦ㄧ嚎楠岃瘉 鈫?SE鐩磋繛锛?
        鍏ㄩ儴鎴愬姛 鈫?閿姣佸垵濮嬪寲鐣岄潰锛屾瀯寤轰富浜ゆ槗鐣岄潰
        澶辫触鏃舵彁渚?閲嶈瘯/鍙栨秷 鎸夐挳
        """
        self._init_hint_var.set("")
        self._retry_btn.pack_forget()
        if hasattr(self, '_cancel_btn'):
            self._cancel_btn.pack_forget()

        def _check_sm():
            """UI helper."""
            ok = self.http.health_check()
            if ok:
                self.after(0, lambda: self._update_init_step("sm", "Connected", ACCENT_GREEN))
                self.after(300, _validate_and_connect_se)
            else:
                self.after(0, lambda: self._on_init_failed(
                    "sm",
                    "\u65e0\u6cd5\u8fde\u63a5\u5230\u670d\u52a1\u7ba1\u7406\u5668",
                    "\u8bf7\u786e\u4fdd\u670d\u52a1\u7ba1\u7406\u5668\u5df2\u542f\u52a8\u4e14\u7f51\u7edc\u901a\u7545\u3002",
                ))

        def _validate_and_connect_se():
            """UI helper."""
            se_addr = getattr(self.session, 'se_address', '') or ''
            if se_addr:
                _validate_se(se_addr)
            else:
                # 鍗充娇鏄粯璁ゅ湴鍧锛屼篃蹇呴』鍏堥氳繃 SM 楠岃瘉鑺傜偣鍦ㄧ嚎
                _validate_se(_default_se_target())

        def _validate_se(se_address: str):
            """UI helper."""
            self._update_init_step("se", "Validating SE...", ACCENT_YELLOW)

            def _check():
                try:
                    status_code, resp_data = self.http.get(
                        f"/api/accounts/se-status?address={_encode_query_value(se_address)}",
                    )
                    if status_code == 200 and resp_data.get("ok"):
                        if resp_data.get("online"):
                            # 妫鏌ユ槸鍚﹁鍏朵粬璐︽埛鍗犵敤
                            occupied_by = (resp_data.get("occupied_by") or "").strip()
                            if occupied_by and occupied_by != self._login_username:
                                self.after(0, lambda ob=occupied_by: self._on_init_failed(
                                    "se",
                                    f"\u5b50\u670d\u52a1\u5668\u5df2\u88ab\u5360\u7528",
                                    f"\u5f53\u524d\u5b50\u670d\u52a1\u5668\u5df2\u88ab\u8d26\u6237 \u201c{ob}\u201d \u5360\u7528\uff0c\u65e0\u6cd5\u8fde\u63a5\u3002",
                                ))
                                return
                            # 鍦ㄧ嚎涓旀湭琚崰鐢紙鎴栬鑷繁鍗犵敤锛夆啋 绔嬪嵆娉ㄥ唽鍗犵敤 + 璁板綍淇℃伅 + 杩炴帴
                            node_name = resp_data.get("node_name", "")
                            self._se_target_address = se_address
                            self._se_server_id = resp_data.get("server_id", "")
                            self.after(0, lambda: self._update_init_step(
                                "se", f"SE OK ({node_name})", ACCENT_GREEN))
                            # 鈽?蹇呴』鍦ㄥ悗鍙扮嚎绋嬩腑鎵ц WS 杩炴帴锛堝惈閲嶈瘯锛夛紝
                            #   缁濅笉鑳介氳繃 after() 鎶曢掑埌 UI 绾跨▼锛屽惁鍒欓噸璇曞惊鐜細鍐荤粨鐣岄潰
                            threading.Thread(target=lambda: _connect_se(se_address), daemon=True).start()
                        else:
                            self.after(0, lambda: self._on_init_failed(
                                "se",
                                "\u5b50\u670d\u52a1\u5668\u4e0d\u5728\u7ebf",
                                "\u6240\u5206\u914d\u7684\u5b50\u670d\u52a1\u5668\u76ee\u524d\u79bb\u7ebf\uff0c\u8bf7\u8054\u7cfb\u7ba1\u7406\u5458\u3002",
                            ))
                    else:
                        msg = resp_data.get("error", "Unknown") if isinstance(resp_data, dict) else "Unknown"
                        self.after(0, lambda m=msg: self._on_init_failed(
                            "se", "\u5b50\u670d\u52a1\u5668\u9a8c\u8bc1\u5931\u8d25", ""))
                except Exception as e:
                    self.after(0, lambda: self._on_init_failed(
                        "se", "\u9a8c\u8bc1\u5b50\u670d\u52a1\u5668\u65f6\u7f51\u7edc\u9519\u8bef", ""))

            threading.Thread(target=_check, daemon=True).start()

        def _connect_se(target_addr: str):
            """
            寤虹珛 SE WebSocket 鐩磋繛锛堝甫閲嶈瘯锛岃В鍐崇鍙ｆ湭灏辩华鐨?Error 1225 闂锛?
            """
            self._update_init_step("se", "Connecting...", ACCENT_YELLOW)
            token = self.http.token
            target = target_addr or getattr(self, '_se_target_address', '') or _default_se_target()
            endpoint = TSWebSocketClient.normalize_endpoint(target, default_port=DEFAULT_TS_PORT)
            self._last_connected_se = endpoint

            max_retries = 5
            for attempt in range(1, max_retries + 1):
                self._update_init_step("se", f"Connecting ({attempt}/{max_retries})...", ACCENT_YELLOW)

                ts_client = TSWebSocketClient(
                    ws_url=endpoint, port=DEFAULT_TS_PORT, token=token, server_id=self._se_server_id,
                    on_message_callback=self._on_init_se_msg,
                    on_status_callback=self._on_init_se_status,
                    on_reconnect_prepare_callback=lambda _attempt, connection_id: self._occupy_se_node(
                        connection_id=connection_id,
                        sync=True,
                    ),
                    reconnect_enabled=False,
                )

                self._se_client = ts_client
                if not self._occupy_se_node(connection_id=ts_client.connection_id, sync=True):
                    self._se_client = None
                    self.after(0, lambda: self._on_init_failed(
                        "se",
                        "\u5360\u7528\u6ce8\u518c\u5931\u8d25",
                        "\u8282\u70b9\u5360\u7528\u6ce8\u518c\u672a\u6210\u529f\uff0c\u65e0\u6cd5\u786e\u4fdd\u72ec\u5360\u6743\u3002",
                    ))
                    return
                ts_client.start()

                # 绛夊緟杩炴帴缁撴灉锛堟渶澶?10 绉掞級
                connected = False
                for _ in range(100):
                    import time as _time
                    _time.sleep(0.1)
                    if ts_client.is_connected:
                        connected = True
                        break
                    if not ts_client.is_active:
                        break

                if connected:
                    ts_client._reconnect_enabled = TS_RECONNECT_ENABLED
                    return

                # 澶辫触娓呯悊
                self._se_client = None
                if attempt < max_retries:
                    import time as _time
                    _time.sleep(min(2 * attempt, 8))

            # 鍏ㄩ儴閲嶈瘯鑰楀敖
            self.after(0, lambda: (
                self._release_se_occupation(),
                self._on_init_failed(
                    "se",
                    "\u65e0\u6cd5\u8fde\u63a5\u5230\u5b50\u670d\u52a1\u5668",
                    "\u8fde\u63a5\u5931\u8d25\uff1a\u8fdc\u7a0b\u8ba1\u7b97\u673a\u62d2\u7edd\u8fde\u63a5\uff08Error 1225\uff09\uff0c\u53ef\u80fd\u5b50\u670d\u52a1\u5668\u7aef\u53e3\u5c1a\u672a\u5c31\u7eea\u3002",
                ),
            ))

        threading.Thread(target=_check_sm, daemon=True).start()

    def _on_init_se_status(self, msg: str):
        """UI helper."""
        def _ui():
            if "Auth failed" in msg or "error" in msg.lower() or "Connection error" in msg:
                self._release_se_occupation()
                self._on_init_failed("se", "\u65e0\u6cd5\u8fde\u63a5\u5230\u5b50\u670d\u52a1\u5668",
                    "\u8bf7\u786e\u4fdd\u5b50\u670d\u52a1\u5668\u5df2\u542f\u52a8\u5e76\u91cd\u8bd5\u3002")
                return
            if "Authenticated" in msg:
                # 鍗犵敤宸插湪 _validate_se / _retry_se_connect 楠岃瘉閫氳繃鏃舵敞鍐岋紝姝ゅ鏃犻渶閲嶅
                self._update_init_step("se", "Connected", ACCENT_GREEN)
                self._se_connected = True
                if self.session:
                    self.session.bind_se_client(self._se_client)
                # 鍏ㄩ儴姝ラ瀹屾垚锛屽欢杩熶竴灏忔鏃堕棿鍚庤繘鍏ヤ富鐣岄潰
                self.after(400, self._enter_main_interface)

        self.after(0, _ui)

    def _on_init_se_msg(self, msg: dict):
        """UI helper."""
        def _ui():
            msg_type = msg.get("type", "")
            if msg_type == "CONNECT_ACK":
                payload = msg.get("payload", {})
                node = payload.get("node_info", {})
                self._session_id = payload.get("session_id", "")
                self._node_info = node
                gate = payload.get("broker_gate")
                if self.session and isinstance(gate, dict):
                    self.session._set_broker_gate(gate)
                # 鏃ュ織绋嶅悗鍦ㄤ富鐣岄潰涓褰?
        self.after(0, _ui)

    def _on_init_failed(self, step_key: str, reason: str, hint: str = ""):
        """UI helper."""
        # 闃叉 init 鐣岄潰宸查攢姣佸悗鐨勫欢杩熷洖璋冨鑷?TclError
        if self._init_ready or not self._init_frame or not self.tk.call('winfo', 'exists', str(self._retry_btn)):
            return
        # 閲婃斁鑺傜偣鍗犵敤锛堝悓姝ワ紝纭繚閲婃斁璇锋眰鍏堜簬鍚庣画娴佺▼锛?
        self._release_se_occupation(sync=True)
        self._update_init_step(step_key, "Failed", ACCENT_RED)
        display_msg = _localize_user_message(reason)
        if hint:
            display_msg += f"\n{_localize_user_message(hint)}"
        self._init_hint_var.set(display_msg)
        # 鏄剧ず閲嶈瘯 + 鍙栨秷鎸夐挳锛堝乏鍙冲垎甯冿級
        try:
            self._retry_btn.pack(side="left", expand=True)
            if hasattr(self, '_cancel_btn'):
                self._cancel_btn.pack(side="right", expand=True)
        except tk.TclError:
            pass  # 绐楀彛宸茶鍏抽棴锛屽拷鐣?

        # 娓呯悊鍙兘鐨勯儴鍒嗚繛鎺?
        if self._se_client:
            self._se_client.stop()
            self._se_client = None
        if self.session:
            self.session.bind_se_client(None)
        self._se_connected = False


    def _on_init_retry(self):
        """UI helper."""
        self._retry_btn.pack_forget()
        if hasattr(self, '_cancel_btn'):
            self._cancel_btn.pack_forget()
        self._init_hint_var.set("")
        # 鍙噸缃?SE 姝ラ锛圫M 鍜?Auth 宸查氳繃锛屼笉闇瑕侀噸鍋氾級
        self._update_init_step("se", "Retrying...", ACCENT_YELLOW)
        # 閲嶆柊璧?SE 楠岃瘉 + 杩炴帴锛堝鐢?_start_connection_flow 涓殑鍐呴儴閫昏緫锛?
        se_addr = getattr(self.session, 'se_address', '') or ''
        if se_addr:
            self.after(200, lambda: self._retry_se_connect(se_addr))
        else:
            self.after(200, lambda: self._retry_se_connect(_default_se_target()))

    def _retry_se_connect(self, target_addr: str):
        """UI helper."""
        # 濮嬬粓鍏堥獙璇?SE 鑺傜偣鍦ㄧ嚎鐘舵侊紝涓嶅厑璁哥粫杩?SM 楠岃瘉鐩存帴杩炴帴
        self._update_init_step("se", "Validating SE...", ACCENT_YELLOW)

        def _check():
            try:
                status_code, resp_data = self.http.get(
                    f"/api/accounts/se-status?address={_encode_query_value(target_addr)}",
                )
                if status_code == 200 and resp_data.get("ok") and resp_data.get("online"):
                    # 妫鏌ユ槸鍚﹁鍏朵粬璐︽埛鍗犵敤
                    occupied_by = (resp_data.get("occupied_by") or "").strip()
                    if occupied_by and occupied_by != self._login_username:
                        self.after(0, lambda ob=occupied_by: self._on_init_failed(
                            "se",
                            "\u5b50\u670d\u52a1\u5668\u5df2\u88ab\u5360\u7528",
                            f"\u5f53\u524d\u5b50\u670d\u52a1\u5668\u5df2\u88ab\u8d26\u6237 \u201c{ob}\u201d \u5360\u7528\uff0c\u65e0\u6cd5\u8fde\u63a5\u3002",
                        ))
                        return
                    # 鍦ㄧ嚎涓旀湭琚崰鐢?鈫?绔嬪嵆娉ㄥ唽鍗犵敤 + 缁х画杩炴帴
                    node_name = resp_data.get("node_name", "")
                    self._se_target_address = target_addr
                    self._se_server_id = resp_data.get("server_id", "")
                    self.after(0, lambda: self._update_init_step(
                        "se", f"SE OK ({node_name})", ACCENT_GREEN))
                    # 鈽?鍦ㄥ悗鍙扮嚎绋嬫墽琛?WS 杩炴帴锛堝惈閲嶈瘯锛夛紝閬垮厤鍐荤粨 UI
                    threading.Thread(target=lambda: self._do_ws_connect(target_addr), daemon=True).start()
                else:
                    self.after(0, lambda: self._on_init_failed(
                        "se",
                        "\u5b50\u670d\u52a1\u5668\u4e0d\u5728\u7ebf",
                        "\u6240\u5206\u914d\u7684\u5b50\u670d\u52a1\u5668\u76ee\u524d\u79bb\u7ebf\uff0c\u8bf7\u8054\u7cfb\u7ba1\u7406\u5458\u3002",
                    ))
            except Exception as e:
                self.after(0, lambda: self._on_init_failed(
                    "se", "\u8fde\u63a5\u5b50\u670d\u52a1\u5668\u65f6\u7f51\u7edc\u9519\u8bef", ""))
        threading.Thread(target=_check, daemon=True).start()

    def _do_ws_connect(self, target_addr: str):
        """
        鎵ц WebSocket 杩炴帴锛堝甫閲嶈瘯锛岃В鍐崇鍙ｆ湭灏辩华鐨?Error 1225 闂锛?
        """
        self._update_init_step("se", "Connecting...", ACCENT_YELLOW)
        token = self.http.token
        target = target_addr or _default_se_target()
        endpoint = SEWebSocketClient.normalize_endpoint(target, default_port=DEFAULT_SE_PORT)
        self._last_connected_se = endpoint

        max_retries = 5
        for attempt in range(1, max_retries + 1):
            self._update_init_step("se", f"Connecting ({attempt}/{max_retries})...", ACCENT_YELLOW)

            se_client = SEWebSocketClient(
                ws_url=endpoint, port=DEFAULT_SE_PORT, token=token, server_id=self._se_server_id,
                on_message_callback=self._on_init_se_msg,
                on_status_callback=self._on_init_se_status,
                on_reconnect_prepare_callback=lambda _attempt, connection_id: self._occupy_se_node(
                    connection_id=connection_id,
                    sync=True,
                ),
                reconnect_enabled=False,
            )

            self._se_client = se_client
            if not self._occupy_se_node(connection_id=se_client.connection_id, sync=True):
                self._se_client = None
                self.after(0, lambda: self._on_init_failed(
                    "se",
                    "\u5360\u7528\u6ce8\u518c\u5931\u8d25",
                    "\u8282\u70b9\u5360\u7528\u6ce8\u518c\u672a\u6210\u529f\uff0c\u65e0\u6cd5\u786e\u4fdd\u72ec\u5360\u6743\u3002",
                ))
                return
            se_client.start()

            # 绛夊緟杩炴帴缁撴灉锛堟渶澶?10 绉掞級
            connected = False
            for _ in range(100):
                import time as _time
                _time.sleep(0.1)
                if se_client.is_connected:
                    connected = True
                    break
                if not se_client.is_active:
                    break

            if connected:
                se_client._reconnect_enabled = SE_RECONNECT_ENABLED
                return

            self._se_client = None
            if attempt < max_retries:
                import time as _time
                _time.sleep(min(2 * attempt, 8))

        # 鍏ㄩ儴閲嶈瘯鑰楀敖
        self.after(0, lambda: (
            self._release_se_occupation(),
            self._on_init_failed(
                "se",
                "\u65e0\u6cd5\u8fde\u63a5\u5230\u5b50\u670d\u52a1\u5668",
                "\u8fde\u63a5\u5931\u8d25\uff1a\u8fdc\u7a0b\u8ba1\u7b97\u673a\u62d2\u7edd\u8fde\u63a5\uff08Error 1225\uff09\u3002",
            ),
        ))

    def _on_init_cancel(self):
        """UI helper."""
        # 閲婃斁鑺傜偣鍗犵敤锛堝悓姝ワ紝纭繚閲婃斁璇锋眰鍏堜簬鍚庣画娴佺▼锛?
        self._release_se_occupation(sync=True)
        # 娓呯悊褰撳墠杩炴帴鐘舵?
        if self._se_client:
            self._se_client.stop()
            self._se_client = None
        if self.session:
            self.session.bind_se_client(None)
        self._se_connected = False

        self.http.token = ""
        self.session = None

        # 閲嶇疆 init screen 姝ラ鏄剧ず
        for key in ("sm", "auth", "se"):
            default = "Waiting" if key != "sm" else "Connecting..."
            color = TEXT_MUTED if key != "sm" else ACCENT_YELLOW
            self._update_init_step(key, default, color)
        self._init_hint_var.set("")
        self._retry_btn.pack_forget()
        if hasattr(self, '_cancel_btn'):
            self._cancel_btn.pack_forget()

        # 閲嶆柊寮瑰嚭鐧诲綍瀵硅瘽妗?
        self.after(0, self._show_login_first)

    def _occupy_se_node(self, connection_id: str = "", max_retries: int = 3, sync: bool = True):
        """Register node occupation in SM."""
        sid = getattr(self, '_se_server_id', '')
        if not sid:
            return False
        username = self._login_username
        requested_connection_id = (connection_id or self._se_connection_id or "").strip()
        if not requested_connection_id:
            return False

        def _do_with_retry():
            last_err = ""
            for attempt in range(1, max_retries + 1):
                try:
                    code, resp = self.http.post(
                        f"/api/nodes/{sid}/occupy",
                        {
                            "username": username,
                            "connection_id": requested_connection_id,
                        },
                    )
                    if code == 200 and (resp or {}).get("ok"):
                        self._se_connection_id = requested_connection_id
                        return True

                    err_msg = (resp or {}).get("error", "") or (resp or {}).get("message", "") or f"HTTP {code}"
                    last_err = err_msg
                    lower_msg = err_msg.lower()
                    if "occupied" in lower_msg or "not found" in lower_msg:
                        return False
                    if "unauthorized" in lower_msg or code in (401, 403):
                        return False
                except Exception as exc:
                    last_err = str(exc)

                if attempt < max_retries:
                    import time as _time
                    _time.sleep(min(1.0 * (2 ** (attempt - 1)), 5))

            return False

        if sync:
            return _do_with_retry()

        threading.Thread(target=_do_with_retry, daemon=True).start()
        return False

    def _release_se_occupation(self, sync: bool = False) -> bool:
        """Release the occupied node in SM."""
        sid = getattr(self, '_se_server_id', '')
        if not sid:
            return True
        connection_id = self._se_connection_id

        def _do() -> bool:
            try:
                code, _resp = self.http.post(
                    f"/api/nodes/{sid}/release",
                    {"connection_id": connection_id},
                )
                if code == 200:
                    if connection_id == self._se_connection_id:
                        self._se_server_id = ""
                        self._se_connection_id = ""
                    return True
                return False
            except Exception:
                return False

        if sync:
            return _do()

        threading.Thread(target=_do, daemon=True).start()
        return False

    def _enter_main_interface(self):
        """UI helper."""
        if self._init_ready:
            return
        self._init_ready = True

        # 鈹鈹 灏?SE 瀹㈡埛绔洖璋冨垏鎹负涓荤晫闈㈢増鏈紙鏀寔鏂嚎妫娴?鑷姩閲嶈繛锛夆攢鈹
        if self._se_client:
            self._se_client.on_message = self._on_se_message
            self._se_client.on_status = self._on_se_status
            if self.session:
                self.session.bind_se_client(self._se_client)


        # 閿姣佸垵濮嬪寲鐣岄潰
        if self._init_frame:
            self._init_frame.destroy()
            self._init_frame = None

        # 鏋勫缓瀹屾暣鐨勪氦鏄撲富鐣岄潰
        self._apply_style()
        self._build_ui_no_login()
        self.log_area = LogArea(self, filter_func=self._should_display_log, max_lines=160, dedupe_window_seconds=2.5)
        self._build_log_bar()
        self._setup_hotkeys()

        # 璁剧疆鐘舵佹爮
        self.status_var.set("\u25cf \u5df2\u8fde\u63a5")
        self.status_lbl.config(fg=ACCENT_GREEN)
        self._set_se_connection_ui(self._se_connected)
        self._apply_broker_gate_ui()
        self._refresh_broker_gate_async(log_errors=False)

        # 鍚姩鍚勫瓙绯荤粺
        node_name = self._node_info.get("node_name", "SE") if self._node_info else "SE"
        region = self._node_info.get("region", "") if self._node_info else ""
        self.log_area.log("\u7cfb\u7edf\u5df2\u5c31\u7eea", "ok")
        if self._se_connected:
            self.log_area.log("\u4ea4\u6613\u670d\u52a1\u5668\u5df2\u8fde\u63a5", "ok")

        self._mock_active = False
        self._stream_active = True
        self._poll()
        self._tick_clock()

        self.after(600, self._refresh_positions)
        self.after(900, self._refresh_orders)

    def _apply_style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure(
            "Treeview",
            background=PANEL_BG,
            foreground=TEXT_PRIMARY,
            fieldbackground=PANEL_BG,
            rowheight=30,
            font=FONT_MONO_SM,
            borderwidth=0,
            relief="flat",
        )
        s.configure(
            "Treeview.Heading",
            background=PANEL_ALT_BG,
            foreground=TEXT_DIM,
            font=FONT_BOLD,
            relief="flat",
            borderwidth=0,
        )
        s.map(
            "Treeview",
            background=[("selected", TREE_SELECT_BG)],
            foreground=[("selected", TEXT_PRIMARY)],
        )
        s.configure("TScrollbar", background=BORDER, troughcolor=DARK_BG, borderwidth=0)
        s.configure("TPanedwindow", background=BORDER)

        s.configure(
            "TCombobox",
            fieldbackground=INPUT_BG,
            background=INPUT_BG,
            foreground=TEXT_PRIMARY,
            arrowcolor=TEXT_PRIMARY,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            insertcolor=TEXT_PRIMARY,
            relief="flat",
        )
        s.map(
            "TCombobox",
            fieldbackground=[("readonly", INPUT_BG), ("focus", INPUT_BG)],
            selectbackground=[("readonly", INPUT_BG)],
            selectforeground=[("readonly", TEXT_PRIMARY)],
            foreground=[("readonly", TEXT_PRIMARY)],
        )


    # 鈹鈹 UI Build 鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹

    def _build_ui_no_login(self):
        """UI helper."""
        self._build_top_bar()
        self._build_trading_panels()
        self._build_body()

    def _build_top_bar(self):
        """UI helper."""
        top = tk.Frame(self, bg=TOP_BAR_BG, height=56)
        top.pack(fill="x")
        top.pack_propagate(False)

        tk.Label(top, text="SC",
                 bg=TOP_BAR_BG, fg="#4ea1ff",
                 font=FONT_TITLE).pack(side="left", padx=14)

        # 杩炴帴鐘舵?
        self.status_var = tk.StringVar(value="\u25cf \u8fde\u63a5\u4e2d\u2026")
        self.status_lbl = tk.Label(
            top,
            textvariable=self.status_var,
            bg=TOP_BAR_BG,
            fg=ACCENT_YELLOW,
            font=FONT_BOLD,
        )



        # 鈹鈹 SE (Trade_Server) 鐩磋繛鎺у埗鍖?鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹
        sep = tk.Frame(top, bg=BORDER, width=1)
        sep.pack(side="left", padx=8, fill="y", pady=6)

        self._se_status_var = tk.StringVar(value="\u4ea4\u6613\u670d\u52a1\u672a\u8fde\u63a5")
        self._se_status_lbl = tk.Label(
            top,
            textvariable=self._se_status_var,
            bg=TOP_BAR_BG,
            fg=ACCENT_RED,
            font=("Segoe UI", 11, "bold"),
        )
        self._se_status_lbl.pack(side="left", padx=(2, 8))

        self._se_btn = tk.Button(
            top,
            text="\u8fde\u63a5\u4ea4\u6613\u670d\u52a1",
            font=FONT_UI_SM,
            bg=BUTTON_NEUTRAL_BG,
            fg=ACCENT_BLUE,
            activebackground=BUTTON_ACTIVE_BG,
            activeforeground=TEXT_PRIMARY,
            relief="flat",
            cursor="hand2",
            padx=10,
            pady=2,
            command=self._toggle_se_connection,
        )
        self._se_btn.pack(side="left", padx=2)
        self._bind_button_hover(self._se_btn, BUTTON_NEUTRAL_BG)

        self._logout_btn = tk.Button(
            top,
            text="\u9000\u51fa\u767b\u5f55",
            font=FONT_UI_SM,
            bg=BUTTON_NEUTRAL_BG,
            fg=ACCENT_RED,
            activebackground=BUTTON_ACTIVE_BG,
            activeforeground=TEXT_PRIMARY,
            relief="flat",
            cursor="hand2",
            padx=10,
            pady=2,
            command=self._logout_to_login,
        )
        self._logout_btn.pack(side="left", padx=(6, 2))
        self._bind_button_hover(self._logout_btn, BUTTON_NEUTRAL_BG)

        broker_frame = tk.Frame(top, bg=TOP_BAR_BG)
        broker_frame.pack(side="left", padx=(10, 6))
        tk.Label(
            broker_frame,
            text="\u4ea4\u6613\u670d\u52a1\u767b\u5f55\uff1a",
            bg=TOP_BAR_BG,
            fg=TEXT_DIM,
            font=FONT_UI_SM,
        ).pack(side="left", padx=(0, 6))
        self._broker_status_lbl = tk.Label(
            broker_frame,
            textvariable=self._broker_status_var,
            bg=TOP_BAR_BG,
            fg=ACCENT_RED,
            font=FONT_BOLD,
        )
        self._broker_status_lbl.pack(side="left", padx=(0, 8))
        self._broker_user_entry = tk.Entry(
            broker_frame,
            width=12,
            bg=INPUT_BG,
            fg=TEXT_PRIMARY,
            insertbackground=TEXT_PRIMARY,
            font=FONT_UI_SM,
            relief="flat",
            bd=0,
        )
        self._broker_user_entry.pack(side="left", padx=(0, 4), ipady=2)
        self._broker_pass_entry = tk.Entry(
            broker_frame,
            width=12,
            bg=INPUT_BG,
            fg=TEXT_PRIMARY,
            insertbackground=TEXT_PRIMARY,
            font=FONT_UI_SM,
            relief="flat",
            bd=0,
            show="*",
        )
        self._broker_pass_entry.pack(side="left", padx=(0, 6), ipady=2)
        self._broker_pass_entry.bind("<Return>", lambda _e: self._broker_login())
        self._broker_login_btn = tk.Button(
            broker_frame,
            text="\u767b\u5f55",
            font=FONT_UI_SM,
            bg=BUTTON_NEUTRAL_BG,
            fg=ACCENT_BLUE,
            activebackground=BUTTON_ACTIVE_BG,
            activeforeground=TEXT_PRIMARY,
            relief="flat",
            cursor="hand2",
            padx=10,
            pady=2,
            command=self._broker_login,
        )
        self._broker_login_btn.pack(side="left")
        self._bind_button_hover(self._broker_login_btn, BUTTON_NEUTRAL_BG)

        self._set_se_connection_ui(self._se_connected)



        # 鏃堕棿

        self.time_var = tk.StringVar()
        tk.Label(top, textvariable=self.time_var, bg=TOP_BAR_BG,
                 fg=TEXT_DIM, font=FONT_MONO).pack(side="right", padx=(12, 4))

        # 鏃跺尯鍒囨崲鎸夐挳
        self._time_zone_btn = tk.Button(
            top,
            text="\u5317\u4eac\u65f6\u95f4",
            font=("Segoe UI", 9),
            bg=BUTTON_NEUTRAL_BG,
            fg=ACCENT_BLUE,
            activebackground=BUTTON_ACTIVE_BG,
            activeforeground=TEXT_PRIMARY,
            relief="flat",
            cursor="hand2",
            padx=6,
            pady=1,
            command=self._toggle_time_zone,
        )
        self._time_zone_btn.pack(side="right", padx=(0, 8))
        self._bind_button_hover(self._time_zone_btn, BUTTON_NEUTRAL_BG)

        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

    def _build_trading_panels(self):
        """UI helper."""
        panels_outer = tk.Frame(self, bg=DARK_BG)
        panels_outer.pack(fill="x")

        for pid in range(2):
            panel = TradingPanel(
                parent=panels_outer,
                panel_id=pid,
                on_symbol_enter_callback=self._on_symbol_enter,
                on_activate_callback=self._activate_panel,
                on_order_type_change_callback=self._on_order_type_change,
            )
            pf = panel.build(panels_outer)
            pf.pack(side="left", fill="both", expand=True,
                    padx=(0 if pid == 0 else 1, 0))

            # 缁戝畾鎸夐挳浜嬩欢鍜屾柟鍚戦敭
            panel.buy_btn.config(command=lambda i=pid: self._place_order("Buy to Open", i))
            panel.sell_btn.config(command=lambda i=pid: self._place_order("Sell to Close", i))

            # 鏂瑰悜閿粦瀹?
            panel.qty_entry.bind("<Up>", lambda e, i=pid: self._adj_qty(+500, i))
            panel.qty_entry.bind("<Down>", lambda e, i=pid: self._adj_qty(-500, i))
            panel.qty_entry.bind("<Right>", lambda e, i=pid: self._adj_qty(+100, i))
            panel.qty_entry.bind("<Left>", lambda e, i=pid: self._adj_qty(-100, i))
            panel.price_entry.bind("<Up>", lambda e, i=pid: self._adj_price(+0.05, i))
            panel.price_entry.bind("<Down>", lambda e, i=pid: self._adj_price(-0.05, i))
            panel.price_entry.bind("<Right>", lambda e, i=pid: self._adj_price(+0.01, i))
            panel.price_entry.bind("<Left>", lambda e, i=pid: self._adj_price(-0.01, i))

            # Esc 鎾ゅ崟
            for ew in (panel.sym_entry, panel.qty_entry):
                ew.bind("<Escape>", lambda e, i=pid: self._esc_cancel_orders(i))

            self.panels[pid] = panel

        # 鍏煎鏃т唬鐮佸紩鐢紙闈㈡澘0鐨勫揩鎹锋柟寮忥級
        p0 = self.panels[0]
        self.sym_var = p0.sym_var
        self.sym_entry = p0.sym_entry
        self.q_last_var = p0.q_last_var
        self.q_bid_var = p0.q_bid_var
        self.q_ask_var = p0.q_ask_var
        self.q_chg_var = p0.q_chg_var
        self.q_vol_var = p0.q_vol_var
        self.order_type_var = p0.order_type_var
        self.tif_var = p0.tif_var
        self.qty_entry = p0.qty_entry
        self.price_entry = p0.price_entry
        self.price_lbl = p0.price_lbl
        self.order_sym_var = p0.order_sym_var
        self.order_last_var = p0.order_last_var

    def _build_body(self):
        """UI helper."""
        body = tk.Frame(self, bg=DARK_BG)
        body.pack(fill="both", expand=True)

        pw = ttk.PanedWindow(body, orient="horizontal")
        pw.pack(fill="both", expand=True, padx=6, pady=(6, 0))

        # Orders (宸?
        self.orders_panel = OrdersPanel(pw, on_refresh_callback=self._refresh_orders,
                                        on_cancel_callback=self._cancel_selected_order)
        of = self.orders_panel.build()
        pw.add(of, weight=1)
        self.ord_tree = self.orders_panel.tree

        # Positions (鍙?
        self.positions_panel = PositionsPanel(pw, on_refresh_callback=self._refresh_positions,
                                              on_select_callback=self._on_pos_row_click)
        pos_f = self.positions_panel.build()
        pw.add(pos_f, weight=1)
        self.pos_tree = self.positions_panel.tree

        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

    def _should_display_log(self, msg: str, tag: str) -> bool:
        text = (msg or "").strip()
        if not text:
            return False


        if text.startswith("[SE]") or text.startswith("[System]"):
            return False

        if tag == "inf":
            return text.startswith((
                "买",
                "卖",
                "撤单",
                "[F]",
                "F键",
                "交易服务",
            ))

        return True

    def _log_user_error_once(self, msg: str, tag: str = "err", window_seconds: float = 3.0):
        text = _localize_user_message(msg)
        if not text or not self.log_area:
            return
        now = time.time()
        if text == self._last_ui_error_message and (now - self._last_ui_error_at) < window_seconds:
            return
        self._last_ui_error_message = text
        self._last_ui_error_at = now
        self.log_area.log(text, tag)

    def _log_result_message(self, ok: bool, msg: str):
        text = _localize_user_message(msg)
        if not text or not self.log_area:
            return
        if ok:
            self.log_area.log(text, "ok")
        else:
            self._log_user_error_once(text)

    def _build_log_bar(self):
        """UI helper."""
        log_frame = tk.Frame(self, bg=PANEL_BG, height=96)
        log_frame.pack(fill="x")
        log_frame.pack_propagate(False)
        self.log_area.frame = log_frame
        self.log_area.build()

    # 鈹鈹 Clock & Poll 鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹

    def _tick_clock(self):
        """UI helper."""
        try:
            if self._time_zone_cn:
                # 涓浗鏃堕棿
                if self._tz_cn is not None:
                    # 浣跨敤鏃跺尯瀵硅薄锛堟敮鎸?DST锛?
                    now = datetime.datetime.now(self._tz_cn)
                else:
                    # 鍥為锛歎TC+8
                    now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
            else:
                # 缇庡浗涓滈儴鏃堕棿
                if self._tz_us is not None:
                    # 浣跨敤鏃跺尯瀵硅薄锛堣嚜鍔ㄥ鐞?DST锛?
                    now = datetime.datetime.now(self._tz_us)
                else:
                    # 鍥為锛歎TC-5锛堜笉鑰冭檻 DST锛?
                    now = datetime.datetime.utcnow() - datetime.timedelta(hours=5)
            
            time_str = now.strftime("%Y-%m-%d  %H:%M:%S")
            self.time_var.set(f"{time_str}")
        except Exception:
            # 鍙戠敓寮傚父鏃朵娇鐢ㄦ湰鍦版椂闂?
            self.time_var.set(datetime.datetime.now().strftime("%Y-%m-%d  %H:%M:%S"))
        
        self.after(1000, self._tick_clock)

    def _toggle_time_zone(self):
        """UI helper."""
        self._time_zone_cn = not self._time_zone_cn
        # 鏇存柊鎸夐挳鏂囨湰
        self._time_zone_btn.config(text="\u5317\u4eac\u65f6\u95f4" if self._time_zone_cn else "\u7f8e\u4e1c\u65f6\u95f4")

    def _poll(self):
        """UI helper."""
        # 娑堣垂妯℃嫙琛屾儏闃熷垪
        if self.session.mock_mode:
            try:
                while True:
                    q = self.quote_queue.get_nowait()
                    sym = q["symbol"]
                    prev = self.current_quote.get(sym)
                    self.current_quote[sym] = q
                    self._refresh_strip(q, prev)
            except queue.Empty:
                pass

        now = time.time()

        # 姣?绉掓洿鏂版寔浠揚&L锛堢敤鏈湴琛屾儏缂撳瓨锛?
        if now - self._last_pos_time > POSITIONS_INTERVAL / 1000 and self.pos_tree.get_children():
            self.positions_panel.live_update_pnl(self.current_quote)
            self._last_pos_time = now

        # 姣?0绉掍粠鏈嶅姟鍣ㄥ埛鏂版寔浠?璁㈠崟
        if self._trade_controls_enabled() and not self.session.mock_mode:
            if now - self._last_orders_time > ORDERS_INTERVAL / 1000:
                self._refresh_positions()
                self._refresh_orders()
                self._last_orders_time = now

        # 蹇冭烦妫娴嬶紙姣?0绉抪ing鏈嶅姟鍣級
        if self.session.connected and not self.session.mock_mode:
            if now - self._last_heartbeat > HEARTBEAT_INTERVAL / 1000:
                self._last_heartbeat = now
                def _ping():
                    ok = self.http.health_check()
                    if not ok:
                        self.after(0, self._on_server_disconnect)
                threading.Thread(target=_ping, daemon=True).start()

        self.after(POLL_INTERVAL, self._poll)

    # 鈹鈹 Symbol Handling 鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹

    def _on_symbol_enter(self, pid: int, _=None):
        """UI helper."""
        p = self.panels[pid]
        sym = p.sym_var.get().strip().upper()
        if not sym:
            return
        p.set_symbol(sym)

        if sym in self.current_quote:
            self._refresh_strip(self.current_quote[sym], None, pid)

        # 瀹炴椂鍚屾璁㈤槄闆嗗悎锛堢洰鏍囷細symbol 鍥炶溅鍚庣珛鍗虫樉绀鸿鎯咃級
        self._sync_quote_subscriptions_async()

    def _sync_quote_subscriptions_async(self):
        """UI helper."""
        def _bg():
            if not self.session:
                return

            desired = {
                p.current_sym.strip().upper()
                for p in self.panels.values()
                if p.current_sym and p.current_sym.strip()
            }

            with self._quote_sub_lock:
                current = set(self._quote_subscribed_symbols)
                to_sub = sorted(desired - current)
                to_unsub = sorted(current - desired)

                if to_unsub:
                    ok, msg = self.session.unsubscribe_quotes(to_unsub, timeout=6.0)
                    if ok:
                        self._quote_subscribed_symbols.difference_update(to_unsub)
                    else:
                        self.after(0, lambda m=msg: self._log_user_error_once(f"行情取消订阅失败：{_localize_user_message(m)}", "warn"))

                if to_sub:
                    ok, msg = self.session.subscribe_quotes(to_sub, timeout=6.0)
                    if ok:
                        self._quote_subscribed_symbols.update(to_sub)
                    else:
                        self.after(0, lambda m=msg: self._log_user_error_once(f"行情订阅失败：{_localize_user_message(m)}", "warn"))

        threading.Thread(target=_bg, daemon=True).start()

    def _sym_key_filter(self, event, pid: int = 0):

        """
        sym_entry 閿洏杩囨护鍣細
        鍙厑璁稿瓧姣嶃佸鑸敭锛涘皬閿洏鏁板瓧璁剧疆鏁伴噺锛汧閿Е鍙戜笅鍗?
        """
        nav_keys = {
            "BackSpace", "Delete", "Left", "Right", "Home", "End",
            "Return", "Tab", "Escape", "Caps_Lock", "Shift_L", "Shift_R",
            "Control_L", "Control_R", "Alt_L", "Alt_R",
        }
        ks = event.keysym
        state = event.state

        numpad_map = {
            "1": "1000", "2": "2000", "3": "3000", "4": "4000", "5": "5000",
            "6": "6000", "7": "7000", "8": "8000", "9": "9000", "0": "1000",
        }
        ctrl_map = {"1": "100", "2": "200", "3": "300", "4": "400", "5": "500",
                     "6": "600", "7": "700", "8": "800", "9": "900"}

        # Ctrl+1-9 鈫?100-900鑲?
        if state & 0x4 and ks in ctrl_map:
            self._set_qty(ctrl_map[ks], pid)
            return "break"

        # 灏忛敭鐩樻暟瀛?鈫?1000-9000鑲?
        if ks in numpad_map and not (state & 0x4):
            self._set_qty(numpad_map[ks], pid)
            return "break"

        # 瀵艰埅閿斁琛?
        if ks in nav_keys:
            return

        # 瀛楁瘝鏀捐
        if event.char and event.char.isalpha():
            return

        return "break"

    # 鈹鈹 Panel Activation 鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹

    def _activate_panel(self, pid: int):
        """UI helper."""
        for i, p in self.panels.items():
            p.set_active(i == pid)
        self.active_panel_id = pid

    def _get_active_panel_id(self) -> int:
        """UI helper."""
        focused = self.focus_get()
        for pid, p in self.panels.items():
            if focused in (p.sym_entry, p.qty_entry, p.price_entry):
                return pid
        return self.active_panel_id

    def _get_pos_direction(self, symbol: str) -> str:
        """UI helper."""
        if not hasattr(self, 'pos_tree') or not self.pos_tree:
            return "none"
        for row in self.pos_tree.get_children():
            v = self.pos_tree.item(row, "values")
            if not v or v[0] != symbol:
                continue
            try:
                pos_val = int(float(v[3]))
                if pos_val > 0:
                    return "long"
                if pos_val < 0:
                    return "short"
            except Exception:
                pass
        return "none"

    # 鈹鈹 Hotkeys 鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹

    def _setup_hotkeys(self):
        """UI helper."""
        def _bind_f(widget):
            widget.bind("<F1>", lambda e: self._f_key_order("sell"))
            widget.bind("<F2>", lambda e: self._f_key_limit("sell"))
            widget.bind("<F3>", lambda e: self._f_key_order("buy"))
            widget.bind("<F4>", lambda e: self._f_key_limit("buy"))

        def _bind_f_sym(widget):
            """UI helper."""
            def _guard(fn):
                def _cb(e):
                    if e.keysym in ("F1", "F2", "F3", "F4"):
                        return fn()
                return _cb
            widget.bind("<F1>", _guard(lambda: self._f_key_order("sell")))
            widget.bind("<F2>", _guard(lambda: self._f_key_limit("sell")))
            widget.bind("<F3>", _guard(lambda: self._f_key_order("buy")))
            widget.bind("<F4>", _guard(lambda: self._f_key_limit("buy")))

        for p in self.panels.values():
            _bind_f(p.qty_entry)
            _bind_f(p.price_entry)
            _bind_f_sym(p.sym_entry)

        # 灏唖ym_key_filter缁戝畾鍒皊ym_entry
        for pid, p in self.panels.items():
            p.sym_entry.bind("<Key>", lambda e, i=pid: self._sym_key_filter(e, i))

    def _f_key_order(self, side: str):
        """UI helper."""
        pid = self._get_active_panel_id()
        p = self.panels[pid]
        sym = p.order_sym_var.get()
        if sym == "\u2014":
            self.log_area.log("F\u952e\u4e0b\u5355\uff1a\u8bf7\u5148\u52a0\u8f7d\u80a1\u7968\u4ee3\u7801", "err")
            return
        try:
            qty = int(p.qty_entry.get())
        except Exception:
            self.log_area.log("\u0046\u952e\u4e0b\u5355\uff1a\u6570\u91cf\u65e0\u6548", "err"); return
        if qty <= 0:
            self.log_area.log("\u0046\u952e\u4e0b\u5355\uff1a\u6570\u91cf\u5fc5\u987b\u5927\u4e8e 0", "err"); return

        direction = self._get_pos_direction(sym)
        if side == "buy":
            action = "Buy to Close" if direction == "short" else "Buy to Open"
        else:
            action = "Sell to Close" if direction == "long" else "Sell to Open"

        tif = p.tif_var.get()
        self.log_area.log(f"[F] {_format_order_action(action)} {qty}\u80a1 {sym} @ MKT | {_format_tif_label(tif)}", "inf")
        self._submit_order_bg(sym, qty, 0, action, "market", tif)

    def _f_key_limit(self, side: str):
        """UI helper."""
        pid = self._get_active_panel_id()
        p = self.panels[pid]
        sym = p.order_sym_var.get()
        if sym == "\u2014":
            self.log_area.log("F\u952e\u4e0b\u5355\uff1a\u8bf7\u5148\u52a0\u8f7d\u80a1\u7968\u4ee3\u7801", "err"); return

        direction = self._get_pos_direction(sym)
        if side == "buy":
            action = "Buy to Close" if direction == "short" else "Buy to Open"
            default_px = self.current_quote.get(sym, {}).get("ask", 0)
        else:
            action = "Sell to Close" if direction == "long" else "Sell to Open"
            default_px = self.current_quote.get(sym, {}).get("bid", 0)

        # 鍒囨崲涓篖imit妯″紡
        p.order_type_var.set("Limit")
        self._on_order_type_change(pid)

        # 濉叆榛樿浠锋牸
        p.price_entry.config(state="normal")
        p.price_entry.delete(0, "end")
        if default_px:
            p.price_entry.insert(0, f"{default_px:.2f}")

        p._pending_action = action

        # 楂樹寒price妗嗗苟鑱氱劍
        hl_color = ACCENT_GREEN if side == "buy" else ACCENT_RED
        p.price_entry.focus_set()
        p.price_entry.config(highlightthickness=2,
                             highlightbackground=hl_color,
                             highlightcolor=hl_color)
        p.price_entry.bind("<Return>", lambda e, i=pid: self._f_limit_submit(i))
        p.price_entry.bind("<Escape>", lambda e, i=pid: self._f_limit_cancel(i))

    def _f_limit_submit(self, pid: int):
        """UI helper."""
        p = self.panels[pid]
        sym = p.order_sym_var.get()
        action = p._pending_action
        if not action or sym == "\u2014":
            return
        try:
            qty = int(p.qty_entry.get())
        except Exception:
            self.log_area.log("\u0046\u952e\u4e0b\u5355\uff1a\u6570\u91cf\u65e0\u6548", "err"); return
        try:
            price = round(float(p.price_entry.get().strip()), 2)
        except Exception:
            self.log_area.log("\u0046\u952e\u4e0b\u5355\uff1a\u4ef7\u683c\u65e0\u6548", "err"); return
        if price <= 0:
            self.log_area.log("\u0046\u952e\u4e0b\u5355\uff1a\u4ef7\u683c\u5fc5\u987b\u5927\u4e8e 0", "err"); return

        tif = p.tif_var.get()
        self.log_area.log(f"[F] {_format_order_action(action)} {qty}\u80a1 {sym} @ ${price:.2f} | {_format_tif_label(tif)}", "inf")

        # 瑙ｇ粦鍥炶溅/Esc锛屾仮澶嶇姸鎬?
        p.price_entry.unbind("<Return>")
        p.price_entry.unbind("<Escape>")
        p.price_entry.config(highlightthickness=0)
        p._pending_action = None

        self._submit_order_bg(sym, qty, price, action, "limit", tif)

    def _f_limit_cancel(self, pid: int):
        """UI helper."""
        p = self.panels[pid]
        p.price_entry.unbind("<Return>")
        p.price_entry.unbind("<Escape>")
        p.price_entry.config(highlightthickness=0)
        p._pending_action = None

    # 鈹鈹 Qty / Price Adjustment 鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹

    def _set_qty(self, val: str, pid: int = 0):
        p = self.panels.get(pid, self.panels[0])
        p.qty_entry.delete(0, "end")
        p.qty_entry.insert(0, val)

    def _adj_qty(self, delta: int, pid: int = 0) -> str:
        p = self.panels.get(pid, self.panels[0])
        try:
            cur = int(p.qty_entry.get())
        except ValueError:
            cur = 0
        new = max(0, cur + delta)
        p.qty_entry.delete(0, "end")
        p.qty_entry.insert(0, str(new))
        return "break"

    def _adj_price(self, delta: float, pid: int = 0) -> str:
        p = self.panels.get(pid, self.panels[0])
        try:
            cur = round(float(p.price_entry.get()), 2)
        except ValueError:
            cur = 0.0
        new = round(max(0.0, cur + delta), 2)
        p.price_entry.delete(0, "end")
        p.price_entry.insert(0, f"{new:.2f}")
        return "break"

    # 鈹鈹 Order Type Toggle 鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹

    def _on_order_type_change(self, pid: int, _=None):
        """UI helper."""
        p = self.panels[pid]
        is_mkt = p.order_type_var.get() == "Market"
        p.price_entry.configure(state="disabled" if is_mkt else "normal",
                               bg=DARK_BG if is_mkt else INPUT_BG)
        p.price_lbl.configure(fg=TEXT_MUTED if is_mkt else TEXT_DIM)

    # 鈹鈹 Login 鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹

    def _broker_gate_state(self) -> dict:
        gate = {
            "active": False,
            "status": "not_logged_in",
            "username": "",
            "server_id": "",
            "account_username": "",
            "grace_remaining": 0,
            "updated_at": 0,
        }
        raw = getattr(self.session, "broker_gate", None) if self.session else None
        if isinstance(raw, dict):
            try:
                gate.update({
                    "active": bool(raw.get("active", False)),
                    "status": str(raw.get("status") or gate["status"]),
                    "username": str(raw.get("username") or ""),
                    "server_id": str(raw.get("server_id") or ""),
                    "account_username": str(raw.get("account_username") or ""),
                    "grace_remaining": max(0, int(raw.get("grace_remaining") or 0)),
                    "updated_at": max(0, int(raw.get("updated_at") or 0)),
                })
            except Exception:
                pass
        if self.session and getattr(self.session, "mock_mode", False):
            gate["active"] = True
            gate["status"] = "mock_mode"
            gate["account_username"] = gate["account_username"] or "SIM"
        return gate

    def _trade_controls_enabled(self) -> bool:
        if not self.session:
            return False
        if getattr(self.session, "mock_mode", False):
            return True
        return bool(self.session.connected and self._se_connected and getattr(self.session, "broker_gate_active", False))

    def _apply_broker_gate_ui(self):
        gate = self._broker_gate_state()
        enabled = self._trade_controls_enabled()

        if self._broker_status_var:
            if getattr(self.session, "mock_mode", False):
                text = "\u4ea4\u6613\u670d\u52a1\uff1a\u6a21\u62df\u6a21\u5f0f"
                color = ACCENT_BLUE
            elif gate["status"] == "grace_pending":
                text = f"\u4ea4\u6613\u670d\u52a1\uff1a\u91cd\u8fde\u7b49\u5f85\u4e2d\uff08{gate['grace_remaining']}秒）"
                color = ACCENT_YELLOW
            elif gate["active"]:
                account_name = gate["account_username"] or "\u5df2\u767b\u5f55"
                text = f"\u4ea4\u6613\u670d\u52a1\uff1a{account_name}"
                color = ACCENT_GREEN
            elif gate["status"] == "expired":
                text = "\u4ea4\u6613\u670d\u52a1\uff1a\u767b\u5f55\u5df2\u8fc7\u671f"
                color = ACCENT_RED
            else:
                text = "\u4ea4\u6613\u670d\u52a1\uff1a\u672a\u767b\u5f55"
                color = ACCENT_RED
            self._broker_status_var.set(text)
            if self._broker_status_lbl:
                self._broker_status_lbl.config(fg=color)

        if self._broker_login_btn:
            self._broker_login_btn.config(state="normal", text="\u767b\u5f55")

        for panel in self.panels.values():
            panel.set_trade_enabled(enabled)
        if self.orders_panel:
            self.orders_panel.set_enabled(enabled)
        if self.positions_panel:
            self.positions_panel.set_enabled(enabled)

    def _refresh_broker_gate_async(self, log_errors: bool = False):
        if not self.session or getattr(self.session, "mock_mode", False):
            self._apply_broker_gate_ui()
            return
        if not self.session.connected or not self._se_connected or not self._se_client or not self._se_client.is_connected:
            self._apply_broker_gate_ui()
            return

        def _bg():
            ok, _gate, msg = self.session.broker_status_query()

            def _ui():
                self._apply_broker_gate_ui()
                if (not ok) and log_errors and msg:
                    self._log_user_error_once(msg, "warn")

            self.after(0, _ui)

        threading.Thread(target=_bg, daemon=True).start()

    def _broker_login(self):
        if not self.session or not self.session.connected or not self._se_connected:
            messagebox.showwarning("\u63d0\u793a", "\u8bf7\u5148\u5b8c\u6210\u7ba1\u7406\u670d\u52a1\u4e0e\u4ea4\u6613\u670d\u52a1\u8fde\u63a5")
            return
        username = self._broker_user_entry.get().strip() if self._broker_user_entry else ""
        password = self._broker_pass_entry.get() if self._broker_pass_entry else ""
        if not username or not password:
            messagebox.showwarning("\u63d0\u793a", "\u8bf7\u8f93\u5165\u4ea4\u6613\u670d\u52a1\u8d26\u53f7\u548c\u5bc6\u7801")
            return
        if self._broker_login_btn:
            self._broker_login_btn.config(state="disabled", text="\u767b\u5f55\u4e2d\u2026")

        def _bg():
            result = self.session.broker_login(username, password)
            ok, msg = result[0], result[1]
            self.after(0, lambda: self._handle_broker_login_result(ok, msg, username))

        threading.Thread(target=_bg, daemon=True).start()

    def _handle_broker_login_result(self, ok: bool, msg: str, username: str):
        if self._broker_login_btn:
            self._broker_login_btn.config(state="normal", text="\u767b\u5f55")
        self._apply_broker_gate_ui()
        if ok:
            if self.log_area:
                self.log_area.log("交易服务登录成功", "ok")
            self.after(120, self._refresh_positions)
            self.after(220, self._refresh_orders)
        else:
            if self.log_area:
                self._log_user_error_once(f"交易服务登录失败：{_localize_user_message(msg)}")

    # 鈹鈹 Place Order 鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹

    def _place_order(self, action: str, pid: int = 0):
        p = self.panels[pid]
        sym = p.order_sym_var.get()
        if sym == "\u2014":
            messagebox.showwarning("\u63d0\u793a", "\u8bf7\u5148\u9009\u62e9\u4e00\u4e2a\u4ea4\u6613\u6807\u7684")
            return
        try:
            qty = int(p.qty_entry.get())
        except ValueError:
            messagebox.showerror("\u8f93\u5165\u9519\u8bef", "\u8bf7\u8f93\u5165\u6709\u6548\u7684\u6570\u91cf")
            return
        is_mkt = p.order_type_var.get() == "Market"
        price = 0.0
        if not is_mkt:
            try:
                price = round(float(p.price_entry.get().strip()), 2)
            except ValueError:
                messagebox.showerror("\u8f93\u5165\u9519\u8bef", "\u8bf7\u8f93\u5165\u6709\u6548\u7684\u4ef7\u683c")
                return
            if price <= 0:
                messagebox.showerror("\u8f93\u5165\u9519\u8bef", "\u4ef7\u683c\u5fc5\u987b\u5927\u4e8e 0")
                return
        tif = p.tif_var.get()
        price_str = "MKT" if is_mkt else f"${price:.2f}"
        self.log_area.log(f"{_format_order_action(action)} {qty}\u80a1 {sym} @ {price_str} | {_format_tif_label(tif)}", "inf")
        self._submit_order_bg(sym, qty, price, action,
                              "market" if is_mkt else "limit", tif)

    def _submit_order_bg(self, symbol: str, qty: int, price: float,
                         action: str, order_type: str, tif: str):
        """UI helper."""
        if not self._trade_controls_enabled():
            if self.log_area:
                self._log_user_error_once("请先登录交易服务", "warn")
            return
        def _bg():
            ok, msg = self.session.place_order(symbol, qty, price, action, order_type, tif=tif)
            self.after(0, lambda: self._log_result_message(ok, msg))
            if ok:
                self.after(250, self._refresh_orders)
                self.after(1200, self._refresh_positions)
        threading.Thread(target=_bg, daemon=True).start()

    # 鈹鈹 Esc Cancel 鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹

    def _esc_cancel_orders(self, pid: int):
        """UI helper."""
        p = self.panels[pid]
        if p._pending_action:
            self._f_limit_cancel(pid)
            return
        sym = p.order_sym_var.get()
        if sym == "\u2014":
            self.log_area.log("Esc\u64a4\u5355\uff1a\u8bf7\u5148\u52a0\u8f7d\u80a1\u7968\u4ee3\u7801", "err")
            return
        live_ids = []
        if hasattr(self, 'ord_tree') and self.ord_tree:
            for r in self.ord_tree.get_children():
                v = self.ord_tree.item(r, "values")
                if v and len(v) >= 1 and v[0] == sym:
                    live_ids.append(r)
        if not live_ids:
            self.log_area.log(f"Esc\u64a4\u5355\uff1a{sym} \u65e0\u751f\u6548\u8ba2\u5355", "inf")
            return
        self.log_area.log(f"Esc\u64a4\u5355\uff1a{sym} \u64a4\u9500 {len(live_ids)} \u7b14\u8ba2\u5355", "inf")
        def _bg():
            for oid in live_ids:
                ok, msg = self.session.cancel_order(oid)
                self.after(0, lambda m=msg, o=ok: self._log_result_message(o, m))
            self.after(1500, self._refresh_orders)
        threading.Thread(target=_bg, daemon=True).start()

    # 鈹鈹 Positions 鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹

    def _refresh_positions(self):
        if not self._trade_controls_enabled() and not getattr(self.session, "mock_mode", False):
            return
        def _bg():
            positions = self.session.get_today_activity()
            err = getattr(self.session, "_pos_error", "")
            self.after(0, lambda: self._update_positions(positions, err))
        threading.Thread(target=_bg, daemon=True).start()

    def _update_positions(self, positions: list[dict], err: str = ""):
        if err:
            self._log_user_error_once(f"Position fetch failed: {err}")
        if self.positions_panel:
            self.positions_panel.update_data(positions, self.current_quote)

    def _on_pos_row_click(self, symbol: str):
        """UI helper."""
        self.panels[0].sym_var.set(symbol)
        self._on_symbol_enter(0)

    # 鈹鈹 Orders 鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹

    def _refresh_orders(self):
        if not self._trade_controls_enabled() and not getattr(self.session, "mock_mode", False):
            return
        mode = self.orders_panel.current_mode if self.orders_panel else "live"
        def _bg():
            orders = self.session.get_orders(mode)
            self.after(0, lambda: self._update_orders(orders))
        threading.Thread(target=_bg, daemon=True).start()

    def _update_orders(self, orders: list[dict]):
        if self.orders_panel:
            mode = self.orders_panel.current_mode
            self.orders_panel.update_data(orders)

    def _cancel_selected_order(self, order_id: str):
        if not self._trade_controls_enabled():
            if self.log_area:
                self._log_user_error_once("请先登录交易服务", "warn")
            return
        def _bg():
            ok, msg = self.session.cancel_order(order_id)
            self.after(0, lambda: self._log_result_message(ok, msg))
            if ok:
                self.after(1000, self._refresh_orders)
        threading.Thread(target=_bg, daemon=True).start()

    # 鈹鈹 Quote Stream 鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹

    def _refresh_strip(self, quote: dict, prev_quote: dict | None, pid: int = None):
        """UI helper."""
        sym = quote["symbol"]
        for i, p in self.panels.items():
            if pid is not None and i != pid:
                continue
            if p.current_sym != sym:
                continue
            pl = prev_quote["last"] if prev_quote else quote["last"]
            chg = round(quote["last"] - pl, 2)

            p.q_last_var.set(f"{quote['last']:.2f}")
            p.q_bid_var.set(f"{quote['bid']:.2f}")
            p.q_ask_var.set(f"{quote['ask']:.2f}")
            p.q_chg_var.set(f"+{chg:.2f}" if chg >= 0 else f"{chg:.2f}")
            p.q_vol_var.set(f"{quote['volume']:,}")
            p.order_last_var.set(f"\u6700\u65b0: ${quote['last']:.2f}")

            # 鑷姩濉厖ask浠锋牸
            if p.price_needs_fill:
                p.fill_price_from_quote(quote["ask"],
                                         p.order_type_var.get() == "Market")

    def _start_mock_stream(self):
        """UI helper."""
        self._mock_active = True
        def _run():
            while self._mock_active:
                syms = set(p.current_sym for p in self.panels.values() if p.current_sym)
                for sym in syms:
                    if sym not in self.mock_base:
                        self.mock_base[sym] = random.uniform(20, 500)
                    self.mock_base[sym] = round(self.mock_base[sym] + random.uniform(-0.2, 0.2), 2)
                    self.quote_queue.put(_mock_quote(sym, self.mock_base[sym]))
                time.sleep(MOCK_QUOTE_INTERVAL / 1000)
        threading.Thread(target=_run, daemon=True).start()

    def _handle_ws_quote(self, quote: dict):
        """UI helper."""
        sym = quote["symbol"]
        prev = self.current_quote.get(sym)
        self.current_quote[sym] = quote
        self._quote_ui_pending[sym] = (quote, prev)
        if self._quote_ui_flush_job is None:
            self._quote_ui_flush_job = self.after(50, self._flush_quote_ui_updates)

    def _flush_quote_ui_updates(self):
        self._quote_ui_flush_job = None
        pending = list(self._quote_ui_pending.values())
        self._quote_ui_pending.clear()
        for quote, prev in pending:
            self._refresh_strip(quote, prev)

    def _on_server_disconnect(self):
        """UI helper."""
        if not self.session.connected:
            return
        self.session.connected = False
        self._stream_active = False
        self._mock_active = False
        self.status_var.set("\u25cf \u672a\u8fde\u63a5")
        self.status_lbl.config(fg=ACCENT_RED)
        self._log_user_error_once("管理服务连接已断开")
        self._apply_broker_gate_ui()

    # 鈹鈹 SE (Trade_Server) Direct Connection 鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹

    def _toggle_se_connection(self):
        """UI helper."""
        if self._se_connected:
            self._se_disconnect()
        else:
            self._se_connect()

    def _se_connect(self):
        """UI helper."""
        if self._se_client and self._se_client.is_active:
            return

        self._se_btn.config(state="disabled", text="\u6821\u9a8c\u4e2d\u2026")
        target_addr = self._se_target_address or _default_se_target()

        def _do_connect_with_retry(max_retries=5):
            token = self.http.token
            endpoint = SEWebSocketClient.normalize_endpoint(target_addr, default_port=DEFAULT_SE_PORT)
            self._last_connected_se = endpoint

            last_err = ""
            for attempt in range(1, max_retries + 1):
                self.after(0, lambda a=attempt, m=max_retries: self._se_btn.config(
                    state="disabled", text=f"\u8fde\u63a5\u4e2d\uff08{a}/{m}\uff09\u2026",
                ))

                client = SEWebSocketClient(
                    ws_url=endpoint, port=DEFAULT_SE_PORT, token=token, server_id=self._se_server_id,
                    on_message_callback=self._on_se_message,
                    on_status_callback=self._on_se_status,
                    on_reconnect_prepare_callback=lambda _attempt, connection_id: self._occupy_se_node(
                        connection_id=connection_id,
                        sync=True,
                    ),
                    reconnect_enabled=False,
                )
                self._se_client = client
                if not self._occupy_se_node(connection_id=client.connection_id, sync=True):
                    self._se_client = None
                    last_err = "trade server occupation failed"
                    return
                client.start()

                connected = False
                for _ in range(100):
                    import time as _time
                    _time.sleep(0.1)
                    if client.is_connected:
                        connected = True
                        break
                    if not client.is_active:
                        break

                if connected:
                    client._reconnect_enabled = SE_RECONNECT_ENABLED
                    return

                self._se_client = None
                last_err = "remote host refused connection (port may not be ready)"
                if attempt < max_retries:
                    import time as _time
                    _time.sleep(min(2 * attempt, 8))

            self.after(0, lambda: (
                self._log_user_error_once(f"Trade server connect failed: {last_err}"),
                self._set_se_connection_ui(False),
            ))

        def _check():
            try:
                status_code, resp_data = self.http.get(
                    f"/api/accounts/se-status?address={_encode_query_value(target_addr)}",
                )
                if status_code == 200 and resp_data.get("ok") and resp_data.get("online"):
                    occupied_by = (resp_data.get("occupied_by") or "").strip()
                    if occupied_by and occupied_by != self._login_username:
                        self.after(0, lambda ob=occupied_by: (
                            self._log_user_error_once(f"Trade server is occupied by {ob!r}"),
                            self._set_se_connection_ui(False),
                        ))
                        return
                    self._se_target_address = target_addr
                    self._se_server_id = resp_data.get("server_id", "")
                    threading.Thread(target=_do_connect_with_retry, daemon=True).start()
                else:
                    self.after(0, lambda: (
                        self._log_user_error_once("Trade server is offline"),
                        self._set_se_connection_ui(False),
                    ))
            except Exception as exc:
                self.after(0, lambda: (
                    self._log_user_error_once(f"Trade server validation failed: {exc}"),
                    self._set_se_connection_ui(False),
                ))

        threading.Thread(target=_check, daemon=True).start()

    def _se_disconnect(self):
        """UI helper."""
        # 濡傛灉姝ｅ湪閲嶈繛锛屽厛闅愯棌閲嶈繛寮圭獥
        if self._reconnecting:
            self._reconnecting = False
            if self._reconnect_dialog:
                try:
                    self._reconnect_dialog.destroy()
                    self._reconnect_dialog = None
                except tk.TclError:
                    pass
        if self._se_client:
            self._se_client.stop()
            self._se_client = None
        if self.session:
            self.session.bind_se_client(None)
        with self._quote_sub_lock:
            self._quote_subscribed_symbols.clear()
        self._se_connected = False

        # 閲婃斁鑺傜偣鍗犵敤锛堝悓姝ワ紝纭繚閲婃斁璇锋眰鍏堜簬鍚庣画娴佺▼锛?

        self._release_se_occupation(sync=True)
        self._set_se_connection_ui(False)
        self.log_area.log("交易服务器已断开", "warn")

    def _on_se_status(self, msg: str):
        """UI helper."""
        def _ui_update():
            if "Authenticated" in msg:
                self._se_connected = True
                self._set_se_connection_ui(True)
                if self.session:
                    self.session.bind_se_client(self._se_client)
                self._apply_broker_gate_ui()
                if self._init_ready and self.log_area:
                    self.log_area.log("\u4ea4\u6613\u670d\u52a1\u5668\u5df2\u8fde\u63a5", "ok")
                self._sync_quote_subscriptions_async()
                self._refresh_broker_gate_async(log_errors=False)
                if self._reconnecting and self._reconnect_dialog:
                    self._hide_reconnect_dialog()

            elif "Reconnecting" in msg or "reconnecting" in msg.lower():
                self._set_se_connection_ui(False)
                self._apply_broker_gate_ui()
                if not self._reconnecting and self._init_ready:
                    self._reconnecting = True
                    self._show_reconnect_dialog(msg)
                elif self._reconnect_dialog:
                    self._reconnect_var.set(_localize_user_message(msg))

            elif "Auth failed" in msg:
                if self._reconnecting:
                    self._cancel_reconnect()
                self._release_se_occupation()
                self._set_se_connection_ui(False)
                with self._quote_sub_lock:
                    self._quote_subscribed_symbols.clear()
                self._apply_broker_gate_ui()
                self._log_user_error_once(msg)

            elif "Connection error" in msg or (msg.startswith("Disconnected:") and not self._se_active_se()):
                if self._init_ready and self._se_connected and not self._reconnecting:
                    self._se_connected = False
                    self._set_se_connection_ui(False)
                    with self._quote_sub_lock:
                        self._quote_subscribed_symbols.clear()
                    self._apply_broker_gate_ui()
                    self._start_se_reconnect()
                elif self._reconnecting:
                    self._cancel_reconnect()
                    self._release_se_occupation()
                    self._set_se_connection_ui(False)
                    with self._quote_sub_lock:
                        self._quote_subscribed_symbols.clear()
                    self._apply_broker_gate_ui()
                else:
                    self._release_se_occupation()
                    self._set_se_connection_ui(False)
                    with self._quote_sub_lock:
                        self._quote_subscribed_symbols.clear()
                    self._apply_broker_gate_ui()
                    self._log_user_error_once(msg)

            else:
                if self._reconnecting:
                    self._reconnect_var.set(_localize_user_message(msg))

        self.after(0, _ui_update)

    def _on_se_message(self, msg: dict):
        """UI helper."""
        def _ui_update():
            msg_type = msg.get("type", "")
            payload = msg.get("payload", {}) if isinstance(msg.get("payload", {}), dict) else {}

            if msg_type == "CONNECT_ACK":
                gate = payload.get("broker_gate")
                if self.session and isinstance(gate, dict):
                    self.session._set_broker_gate(gate)
                    self._apply_broker_gate_ui()

            elif msg_type == "STATUS_RESPONSE":
                gate = payload.get("broker_gate")
                if self.session and isinstance(gate, dict):
                    self.session._set_broker_gate(gate)
                    self._apply_broker_gate_ui()

            elif msg_type in ("BROKER_LOGIN_RESPONSE", "BROKER_STATUS_RESPONSE", "BROKER_LOGOUT_RESPONSE"):
                gate = payload.get("gate")
                if self.session and isinstance(gate, dict):
                    self.session._set_broker_gate(gate)
                self._apply_broker_gate_ui()
                if msg_type == "BROKER_LOGIN_RESPONSE" and payload.get("success"):
                    self.after(120, self._refresh_positions)
                    self.after(220, self._refresh_orders)

            elif msg_type == "QUOTE_DATA":
                sym = str(payload.get("symbol", "")).strip().upper()
                if not sym:
                    return
                try:
                    bid = float(payload.get("bid", 0) or 0)
                    ask = float(payload.get("ask", 0) or 0)
                    last = float(payload.get("last", 0) or 0)
                    if last <= 0 and bid > 0 and ask > 0:
                        last = round((bid + ask) / 2, 2)
                    quote = {
                        "symbol": sym,
                        "bid": bid,
                        "ask": ask,
                        "last": last,
                        "volume": int(float(payload.get("volume", 0) or 0)),
                        "timestamp": str(payload.get("ts") or payload.get("timestamp") or ""),
                    }
                    self._handle_ws_quote(quote)
                except Exception:
                    return

            elif msg_type == "QUOTE_ACK":
                pass

            elif msg_type == "FORCE_DISCONNECT":
                reason = payload.get("reason", "admin_force_release")
                self._log_user_error_once(f"交易服务器连接已被管理端释放（{reason}）", "warn")
                self._reconnecting = False
                if self._reconnect_dialog:
                    try:
                        self._reconnect_dialog.destroy()
                    except tk.TclError:
                        pass
                    self._reconnect_dialog = None
                if self._se_client:
                    self._se_client._reconnect_enabled = False
                    self._se_client.stop()
                    self._se_client = None
                if self.session:
                    self.session.bind_se_client(None)
                self._se_connected = False
                self._set_se_connection_ui(False)
                with self._quote_sub_lock:
                    self._quote_subscribed_symbols.clear()
                self._apply_broker_gate_ui()
                messagebox.showwarning("交易服务器已释放", "当前交易服务器连接已被管理端释放，请重新连接。")

            elif msg_type == "ERROR":
                err = payload
                if err.get("code") in ("BROKER_LOGIN_REQUIRED", "BROKER_CREDENTIALS_REQUIRED"):
                    self._refresh_broker_gate_async(log_errors=False)
                self._log_user_error_once(
                    f"\u4ea4\u6613\u670d\u52a1\u5668\u9519\u8bef[{err.get('code', '')}]\uff1a"
                    f"{_localize_user_message(err.get('message', ''))}"
                )

            elif msg_type == "PONG":
                pass

            else:
                pass

        self.after(0, _ui_update)

    def _se_active_se(self) -> bool:
        """UI helper."""
        return self._se_client is not None and self._se_client.is_active

    # 鈹鈹 SE 鑷姩閲嶈繛锛堣繍琛屼腑鏂嚎鍚庯級鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹

    def _start_se_reconnect(self):
        """
        杩愯涓?SE 鏂嚎 鈫?鍚姩鑷姩閲嶈繛娴佺▼
        鍒涘缓鏂扮殑 SEWebSocketClient 骞跺惎鐢ㄩ噸杩炴ā寮忥紝鍚庡彴绾跨▼鑷姩灏濊瘯閲嶈繛
        """
        if self._reconnecting:
            return  # 宸插湪閲嶈繛涓?

        self._reconnecting = True
        self._reconnect_cancelled = False
        target_addr = self._last_connected_se or self._se_target_address or _default_se_target()
        token = self.http.token

        endpoint = SEWebSocketClient.normalize_endpoint(target_addr, default_port=DEFAULT_SE_PORT)

        # 鈽?鍏抽敭淇锛氬厛鍋滄鏃у鎴风锛岄槻姝㈠菇鐏电嚎绋嬫畫鐣?
        # 鏃у鎴风鐨勫悗鍙扮嚎绋嬪彲鑳戒粛鍦?sleep/閫閬跨瓑寰咃紝濡傛灉涓?stop()锛?
        # 瀹冮啋鏉ュ悗浼氬皾璇曢噸鏂拌繛鎺ワ紝瀵艰嚧涓や釜 WS 杩炴帴鍚屾椂绔炰簤鍚屼竴涓?SE 绔彛
        if self._se_client and self._se_client.is_active:
            self._se_client.stop()

        # 鍒涘缓鍚敤閲嶈繛鐨?SE 瀹㈡埛绔?
        se_client = SEWebSocketClient(
            ws_url=endpoint, port=DEFAULT_SE_PORT, token=token, server_id=self._se_server_id,
            on_message_callback=self._on_se_message,
            on_status_callback=self._on_se_status,
            on_reconnect_prepare_callback=lambda _attempt, connection_id: self._occupy_se_node(
                connection_id=connection_id,
                sync=True,
            ),
            reconnect_enabled=SE_RECONNECT_ENABLED,
        )
        self._se_client = se_client
        if not self._occupy_se_node(connection_id=se_client.connection_id, sync=True):
            self._se_client = None
            self._reconnecting = False
            self.after(0, lambda: self._log_user_error_once("Trade server lock failed; reconnection aborted"))
            return
        se_client.start()


    def _show_reconnect_dialog(self, initial_msg: str = ""):
        """UI helper."""
        if self._reconnect_dialog:
            return  # 宸插瓨鍦ㄥ垯涓嶉噸澶嶅垱寤?

        dlg = tk.Toplevel(self)
        self._reconnect_dialog = dlg
        dlg.title("\u4ea4\u6613\u670d\u52a1\u91cd\u8fde")
        dlg.geometry("420x240")
        dlg.resizable(False, False)
        dlg.configure(bg=DARK_BG)

        # 灞呬腑鏄剧ず
        dlg.transient(self)
        dlg.grab_set()
        dlg.protocol("WM_DELETE_WINDOW", self._cancel_reconnect)

        # 璁＄畻灞呬腑浣嶇疆
        dlg.update_idletasks()
        pw = self.winfo_width()
        ph = self.winfo_height()
        px = self.winfo_x()
        py = self.winfo_y()
        x = px + (pw - 420) // 2
        y = py + (ph - 240) // 2
        dlg.geometry(f"+{x}+{y}")

        # 鏍囬鍥炬爣
        title_frame = tk.Frame(dlg, bg=DARK_BG)
        title_frame.pack(fill="x", pady=(24, 8))
        tk.Label(title_frame, text="\u26a0\ufe0f", font=FONT_TITLE,
                 bg=DARK_BG, fg=ACCENT_YELLOW).pack()


        # 涓绘彁绀烘枃瀛?
        tk.Label(dlg, text="\u5b50\u670d\u52a1\u5668\u8fde\u63a5\u5df2\u65ad\u5f00",
                 bg=DARK_BG, fg=TEXT_PRIMARY, font=FONT_UI).pack(pady=(4, 2))
        tk.Label(dlg, text="\u6b63\u5728\u5c1d\u91cd\u65b0\u8fde\u63a5...",
                 bg=DARK_BG, fg=TEXT_DIM, font=FONT_UI_SM).pack(pady=(0, 16))

        # 鐘舵佹枃鏈紙鍔ㄦ佹洿鏂帮級
        self._reconnect_var.set(_localize_user_message(initial_msg) or "\u7b49\u5f85\u8fde\u63a5\u2026")
        status_lbl = tk.Label(dlg, textvariable=self._reconnect_var,
                               bg=DARK_BG, fg=ACCENT_YELLOW, font=FONT_MONO_SM,
                               wraplength=380)
        status_lbl.pack(pady=(0, 20))

        # 鍙栨秷鎸夐挳瀹瑰櫒
        btn_frame = tk.Frame(dlg, bg=DARK_BG)
        btn_frame.pack(pady=(0, 16))

        cancel_btn = tk.Button(
            btn_frame, text="\u53d6\u6d88\u91cd\u8fde", font=FONT_UI_SM,
            bg=BUTTON_NEUTRAL_BG, fg=ACCENT_RED, activebackground=BUTTON_ACTIVE_BG,
            activeforeground=TEXT_PRIMARY, relief="flat", cursor="hand2",
            padx=24, pady=6, command=self._cancel_reconnect,
        )
        cancel_btn.pack()
        self._bind_button_hover(cancel_btn, BUTTON_NEUTRAL_BG)


    def _hide_reconnect_dialog(self):
        """UI helper."""
        self._reconnecting = False
        if self._reconnect_dialog:
            try:
                self._reconnect_dialog.destroy()
            except tk.TclError:
                pass
            self._reconnect_dialog = None

    def _cancel_reconnect(self):
        """UI helper."""
        if not self._reconnecting and not self._reconnect_dialog:
            return

        self._reconnecting = False
        self._reconnect_cancelled = True

        # 鍋滄姝ｅ湪閲嶈繛鐨?SE 瀹㈡埛绔?
        if self._se_client:
            self._se_client.stop()
            self._se_client = None
        if self.session:
            self.session.bind_se_client(None)
        self._se_connected = False
        with self._quote_sub_lock:
            self._quote_subscribed_symbols.clear()

        # 闅愯棌寮圭獥

        if self._reconnect_dialog:
            try:
                self._reconnect_dialog.destroy()
            except tk.TclError:
                pass
            self._reconnect_dialog = None

        # 閲婃斁鑺傜偣鍗犵敤锛堝悓姝ワ紝纭繚閲婃斁璇锋眰鍏堜簬鍚庣画娴佺▼锛?
        self._release_se_occupation(sync=True)

        # 鎭㈠ UI 鐘舵?
        self._set_se_connection_ui(False)
        self.log_area.log("已取消交易服务器重连", "warn")

    def _logout_to_login(self):
        """UI helper."""
        if not messagebox.askyesno("退出登录", "是否退出当前账号并返回登录界面？"): 
            return

        # 鍋滄閲嶈繛鐘舵?寮圭獥
        self._reconnecting = False
        if self._reconnect_dialog:
            try:
                self._reconnect_dialog.destroy()
            except tk.TclError:
                pass
            self._reconnect_dialog = None

        if self.session:
            try:
                self.session.broker_logout()
            except Exception:
                pass

        # 鏂紑 SE 杩炴帴
        if self._se_client:
            self._se_client.stop()
            self._se_client = None
        self._se_connected = False
        with self._quote_sub_lock:
            self._quote_subscribed_symbols.clear()

        # 閲婃斁鑺傜偣鍗犵敤锛堝悓姝ワ紝纭繚閲婃斁璇锋眰鍏堜簬鍚庣画娴佺▼锛?

        self._release_se_occupation(sync=True)

        # 瑙ｇ粦浼氳瘽涓殑 SE client
        if self.session:
            self.session.bind_se_client(None)

        # 閫鍑?SM 鐧诲綍骞舵竻绌?token
        if self.session:
            try:
                self.session.logout()
            except Exception:
                self.http.token = ""

        # 娓呯┖褰撳墠璐﹀彿缂撳瓨锛屽厑璁稿垏鎹㈣处鍙风櫥褰?
        self._login_username = ""
        self._login_password = ""

        # 闅愯棌涓荤晫闈㈢粍浠讹紝鍥炲埌鍒濆鍖?鐧诲綍娴佺▼
        for child in self.winfo_children():
            try:
                child.pack_forget()
            except Exception:
                pass

        if self._init_frame:
            try:
                self._init_frame.destroy()
            except Exception:
                pass
            self._init_frame = None

        self._init_ready = False
        self._show_init_screen()
        self.after(100, self._show_login_first)

    # 鈹鈹 Lifecycle 鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹

    def on_close(self):

        """UI helper."""
        self._mock_active = False
        self._stream_active = False
        self._reconnecting = False  # 鍙栨秷閲嶈繛鐘舵?
        if self._se_dot_job is not None:
            try:
                self.after_cancel(self._se_dot_job)
            except Exception:
                pass
            self._se_dot_job = None

        # 闅愯棌閲嶈繛寮圭獥
        if self._reconnect_dialog:
            try:
                self._reconnect_dialog.destroy()
            except tk.TclError:
                pass
            self._reconnect_dialog = None
        # 鏂紑 SE 杩炴帴
        if self._se_client:
            self._se_client.stop()
        if self.session:
            self.session.bind_se_client(None)
        with self._quote_sub_lock:
            self._quote_subscribed_symbols.clear()
        # 閲婃斁鑺傜偣鍗犵敤锛堝悓姝ワ紝闃叉鍏抽棴绐楀彛鍚庤妭鐐硅姘镐箙閿佸畾锛?
        self._release_se_occupation(sync=True)
        # 鐧诲嚭骞舵竻鐞唗oken锛堥槻姝㈡湇鍔＄token娈嬬暀锛?
        if self.http and self.http.token:
            try:
                self.http.post("/auth/logout", {})
                if hasattr(self, 'log_area') and self.log_area:
                    self.log_area.log("已退出登录", "ok")
            except Exception as e:
                # 缃戠粶閿欒涓嶅奖鍝嶇獥鍙ｅ叧闂?
                pass
            finally:
                self.http.token = ""
        self.destroy()


# 鈹鈹 Mock Quote Helper 鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹鈹

def mock_quote(sym: str, base: float) -> dict:
    """UI helper."""
    last = round(base + random.uniform(-0.3, 0.3), 2)
    sp = random.uniform(0.01, 0.08)
    return dict(symbol=sym, bid=round(last - sp, 2), ask=round(last + sp, 2),
                last=last, volume=random.randint(100, 9999) * 100,
                timestamp=datetime.datetime.now().strftime("%H:%M:%S"))
