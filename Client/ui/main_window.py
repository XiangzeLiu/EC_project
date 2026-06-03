"""
SC - Main Window
主窗口：组装所有子组件，管理全局状态、快捷键、轮询、行情流
"""

import datetime
import json
import queue
import random
import re
import threading
import time

# 时区支持（Python 3.9+ 使用 zoneinfo，更低版本回退到 pytz）
try:
    from zoneinfo import ZoneInfo
except ImportError:
    try:
        from pytz import timezone as ZoneTimezone
        # 为 pytz 创建兼容接口
        class ZoneInfo:
            def __init__(self, key):
                self._tz = ZoneTimezone(key)
            def localize(self, dt):
                return self._tz.localize(dt)
    except ImportError:
        # 如果都没有，使用简化版（无 DST 支持）
        ZoneInfo = None

import tkinter as tk
from tkinter import messagebox, ttk

from ..constants import *
from ..config import load_credentials, save_credentials
from ..network.http_client import HttpClient
from ..network.ws_client import QuoteStream
from ..network.se_websocket import SEWebSocketClient
from ..services.trading_session import TradingSession, sanitize
from .trading_panel import TradingPanel
from .positions_panel import PositionsPanel
from .orders_panel import OrdersPanel
from .log_area import LogArea
from .login_dialog import LoginDialog


class TradingTerminal(tk.Tk):
    """交易终端主窗口"""

    def __init__(self):
        super().__init__()
        self.title("SC")

        # 窗口尺寸与居中
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

        # ── 预初始化引用（避免属性错误）───────────────────────────────
        self.http = HttpClient()
        self.session = None
        self.panels: dict[int, TradingPanel] = {}
        self.active_panel_id: int = 0
        self.quote_queue = queue.Queue()
        self.sub_queue = queue.Queue()
        self.current_quote: dict[str, dict] = {}
        self.mock_base: dict[str, float] = {}
        self._stream_active: bool = False
        self._mock_active: bool = False
        self._ws_stream: QuoteStream | None = None
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
        self._time_zone_cn: bool = True  # True=中国时间, False=美国时间
        self.log_area: LogArea | None = None
        
        # 时区对象（用于 DST 支持）
        if ZoneInfo is not None:
            try:
                self._tz_cn = ZoneInfo("Asia/Shanghai")  # 中国时区
                self._tz_us = ZoneInfo(TZ_ET_NAME)        # 美国东部时区
            except Exception:
                self._tz_cn = None
                self._tz_us = None
        else:
            self._tz_cn = None
            self._tz_us = None

        # SE 直连组件
        self._se_client: SEWebSocketClient | None = None
        self._se_connected: bool = False
        self._se_status_var: tk.StringVar = None
        self._se_status_lbl: tk.Label | None = None
        self._se_btn: tk.Button | None = None
        self._logout_btn: tk.Button | None = None
        self._session_id: str = ""
        self._se_status_connected: bool = False
        self._se_dot_phase: int = 0
        self._se_dot_job = None


        self._node_info: dict = {}
        self._se_target_address: str = ""  # 登录后动态获取的 SE 地址
        self._se_server_id: str = ""       # 当前 SE 对应的 server_id（用于占用/释放）
        self._last_connected_se: str = ""   # 最近一次连接的 SE 地址
        self._login_username: str = ""      # 当前登录用户名
        self._login_password: str = ""      # 当前登录密码（取消返回时回填）

        # 初始化界面容器（占满窗口，后续销毁后替换为主界面）
        self._init_frame: tk.Frame | None = None
        self._init_ready = False   # 标记：全部连接成功后才构建主界面

        # ── SE 重连相关状态 ──
        self._reconnecting: bool = False          # 是否正在自动重连中
        self._reconnect_dialog: tk.Toplevel | None = None  # 重连弹窗引用
        self._reconnect_cancelled: bool = False   # 用户是否取消了重连
        self._reconnect_var: tk.StringVar = tk.StringVar(value="")  # 重连状态文本

        # 显示初始化连接界面
        self._show_init_screen()

        # 启动时先弹出登录界面，用户点击登录后才进行连接验证
        self.after(200, self._show_login_first)

    # ── Init Screen & Connection Flow ─────────────────────────────────────

    def _show_init_screen(self):
        """显示初始化连接界面（占满窗口，连接成功后销毁）"""
        self._init_frame = tk.Frame(self, bg=DARK_BG)
        self._init_frame.pack(fill="both", expand=True)

        # 居中容器
        center = tk.Frame(self._init_frame, bg=DARK_BG)
        center.place(relx=0.5, rely=0.45, anchor="center")

        tk.Label(center, text="SC",
                 bg=DARK_BG, fg="#4ea1ff", font=FONT_TITLE).pack(pady=(0, 8))

        tk.Label(center, text="\u8fde\u63a5\u4e2d\u2026",
                 bg=DARK_BG, fg=TEXT_DIM, font=FONT_UI_SM).pack(pady=(0, 28))

        # 步骤状态标签（显示在 UI 上，便于交易员快速判断连接阶段）
        steps = tk.Frame(center, bg=DARK_BG)
        steps.pack(fill="x", pady=(0, 12))
        self._init_steps: dict[str, tuple[tk.Label, tk.StringVar]] = {}
        for key, title, default in (
            ("auth", "账户认证", "Waiting"),
            ("sm", "服务管理器", "Connecting..."),
            ("se", "子服务器(SE)", "Waiting"),
        ):
            row = tk.Frame(steps, bg=DARK_BG)
            row.pack(fill="x", pady=1)
            tk.Label(row, text=f"{title}", bg=DARK_BG, fg=TEXT_DIM, font=FONT_UI_SM, width=12, anchor="w").pack(side="left")
            var = tk.StringVar(value=default)
            lbl = tk.Label(row, textvariable=var, bg=DARK_BG,
                           fg=ACCENT_YELLOW if key == "sm" else TEXT_MUTED,
                           font=FONT_MONO_SM, anchor="w")
            lbl.pack(side="left", padx=(6, 0))
            self._init_steps[key] = (lbl, var)

        # 底部提示信息（字体加大）
        self._init_hint_var = tk.StringVar(value="")
        tk.Label(center, textvariable=self._init_hint_var, bg=DARK_BG,
                 fg=ACCENT_RED, font=FONT_UI, wraplength=500,
                 justify="center").pack(pady=(16, 0))


        # 按钮容器（重试 + 取消）
        btn_container = tk.Frame(center, bg=DARK_BG)
        btn_container.pack(pady=(16, 0))
        # 重试按钮（初始隐藏）
        self._retry_btn = tk.Button(
            btn_container, text="\u91cd\u8bd5", font=FONT_UI_SM,
            bg=BUTTON_NEUTRAL_BG, fg=ACCENT_BLUE, activebackground=BUTTON_ACTIVE_BG,
            activeforeground=TEXT_PRIMARY, relief="flat", cursor="hand2",
            padx=20, pady=6, command=self._on_init_retry,
        )
        self._bind_button_hover(self._retry_btn, BUTTON_NEUTRAL_BG)

        # 取消按钮（初始隐藏，点击返回登录界面）
        self._cancel_btn = tk.Button(
            btn_container, text="\u53d6\u6d88", font=FONT_UI_SM,
            bg=BUTTON_NEUTRAL_BG, fg=ACCENT_RED, activebackground=BUTTON_ACTIVE_BG,
            activeforeground=TEXT_PRIMARY, relief="flat", cursor="hand2",
            padx=20, pady=6, command=self._on_init_cancel,
        )
        self._bind_button_hover(self._cancel_btn, BUTTON_NEUTRAL_BG)


    def _update_init_step(self, step_key: str, status: str, color: str = None):
        """更新初始化步骤的状态文本和颜色"""
        if step_key not in self._init_steps:
            return
        lbl, var = self._init_steps[step_key]
        var.set(status)
        if color:
            lbl.config(fg=color)
        self.update_idletasks()

    @staticmethod
    def _bind_button_hover(btn: tk.Button, normal_bg: str):
        btn.bind("<Enter>", lambda e: btn.config(bg=BUTTON_HOVER_BG))
        btn.bind("<Leave>", lambda e: btn.config(bg=normal_bg))

    def _start_se_status_dot_animation(self):
        """启动 SE 状态小点动画（仅启动一次定时器）"""
        if self._se_dot_job is None:
            self._tick_se_status_dot()

    def _tick_se_status_dot(self):
        """SE 状态小点动画帧更新"""
        self._se_dot_job = None

        if not self.winfo_exists():
            return

        if self._se_status_var and self._se_status_lbl and self._se_status_lbl.winfo_exists():
            phase = self._se_dot_phase % 4
            if self._se_status_connected:
                dot = "●"
                colors = ["#2fcf7b", "#66e6a1", ACCENT_GREEN, "#66e6a1"]
                self._se_status_var.set(f"{dot} Connect")
                self._se_status_lbl.config(fg=colors[phase])
            else:
                dots = ["●", "◉", "◎", "◉"]
                colors = ["#ff5c5c", "#ff7b7b", ACCENT_RED, "#ff7b7b"]
                self._se_status_var.set(f"{dots[phase]} Disconnect")
                self._se_status_lbl.config(fg=colors[phase])

            self._se_dot_phase = (self._se_dot_phase + 1) % 4

        self._se_dot_job = self.after(430, self._tick_se_status_dot)

    def _set_se_connection_ui(self, connected: bool):
        """统一更新 SE 状态文案/颜色与按钮文案"""
        self._se_status_connected = bool(connected)
        self._se_dot_phase = 0

        if self._se_status_var and self._se_status_lbl:
            self._start_se_status_dot_animation()

        if self._se_btn:
            self._se_btn.config(text="Disconnect" if connected else "Connect", state="normal")


    def _show_login_first(self):
        """启动时首先弹出登录对话框（用户输入账户密码后才启动连接流程）"""
        self.session = TradingSession(self.http)
        # 首次打开不填充，取消返回时填充上次输入的凭据
        login = LoginDialog(
            self, auth_fn=self.session.login,
            default_user=self._login_username,
            default_pass=self._login_password,
        )
        creds = login.credentials
        if not creds:
            # 用户关闭了登录窗口
            self.quit()
            return

        username, password = creds
        if not self.session.connected:
            # 登录认证失败，重新弹出登录框让用户重试（保留已输入的账号密码）
            self._login_username = username
            self._login_password = password
            self.after(0, lambda: self._show_login_first())
            return

        self._login_username = username
        self._login_password = password
        self._update_init_step("auth", f"OK ({username})", ACCENT_GREEN)
        save_credentials(username, password)

        # 登录成功 → 启动 SM 检查 + SE 验证流程
        self.after(0, self._start_connection_flow)

    def _start_connection_flow(self):
        """
        登录成功后的连接流程（SM检查 → SE在线验证 → SE直连）
        全部成功 → 销毁初始化界面，构建主交易界面
        失败时提供 重试/取消 按钮
        """
        self._update_init_step("sm", "Connecting...", ACCENT_YELLOW)
        self._init_hint_var.set("")
        self._retry_btn.pack_forget()
        if hasattr(self, '_cancel_btn'):
            self._cancel_btn.pack_forget()

        def _check_sm():
            """Step 1: 检查 SM 是否可达"""
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
            """Step 2: 验证 SE 在线状态 + 连接 SE"""
            se_addr = getattr(self.session, 'se_address', '') or ''
            if se_addr:
                _validate_se(se_addr)
            else:
                # 即使是默认地址，也必须先通过 SM 验证节点在线
                _validate_se(DEFAULT_SE_HOST)

        def _validate_se(se_address: str):
            """验证 SE 地址对应的子服务器是否在线（含占用检查）"""
            self._update_init_step("se", "Validating SE...", ACCENT_YELLOW)

            def _check():
                try:
                    status_code, resp_data = self.http.get(
                        f"/api/accounts/se-status?address={se_address}",
                    )
                    if status_code == 200 and resp_data.get("ok"):
                        if resp_data.get("online"):
                            # 检查是否被其他账户占用
                            occupied_by = (resp_data.get("occupied_by") or "").strip()
                            if occupied_by and occupied_by != self._login_username:
                                self.after(0, lambda ob=occupied_by: self._on_init_failed(
                                    "se",
                                    f"\u5b50\u670d\u52a1\u5668\u5df2\u88ab\u5360\u7528",
                                    f"\u5f53\u524d\u5b50\u670d\u52a1\u5668\u5df2\u88ab\u8d26\u6237 \u201c{ob}\u201d \u5360\u7528\uff0c\u65e0\u6cd5\u8fde\u63a5\u3002",
                                ))
                                return
                            # 在线且未被占用（或被自己占用）→ 立即注册占用 + 记录信息 + 连接
                            node_name = resp_data.get("node_name", "")
                            self._se_target_address = se_address
                            self._se_server_id = resp_data.get("server_id", "")
                            # 诊断：记录 server_id，便于排查占用注册失败
                            _log = getattr(self, 'log_area', None)
                            if _log:
                                _log.log(f"[SE] se-status 返回: server_id={self._se_server_id}, node={node_name}, occupied_by={resp_data.get('occupied_by', '')}", "inf")
                            # ⚡ 同步注册节点占用（阻塞等待成功，消除竞态窗口）
                            occ_ok = self._occupy_se_node(sync=True)
                            if not occ_ok:
                                # 占用失败，不继续连接（_occupy_se_node 内部已记录详细日志）
                                self.after(0, lambda: self._on_init_failed(
                                    "se",
                                    "\u5360\u7528\u6ce8\u518c\u5931\u8d25",
                                    "\u8282\u70b9\u5360\u7528\u6ce8\u518c\u672a\u6210\u529f\uff0c\u65e0\u6cd5\u786e\u4fdd\u72ec\u5360\u6743\u3002",
                                ))
                                return
                            self.after(0, lambda: self._update_init_step(
                                "se", f"SE OK ({node_name})", ACCENT_GREEN))
                            # ★ 必须在后台线程中执行 WS 连接（含重试），
                            #   绝不能通过 after() 投递到 UI 线程，否则重试循环会冻结界面
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
            建立 SE WebSocket 直连（带重试，解决端口未就绪的 Error 1225 问题）
            """
            self._update_init_step("se", "Connecting...", ACCENT_YELLOW)
            token = self.http.token
            target = target_addr or getattr(self, '_se_target_address', '') or DEFAULT_SE_HOST
            if ':' in target:
                hp = target.rsplit(':', 1)
                host, port = hp[0], int(hp[1]) if hp[1].isdigit() else DEFAULT_SE_PORT
            else:
                host, port = target, DEFAULT_SE_PORT
            self._last_connected_se = f"{host}:{port}"

            max_retries = 5
            for attempt in range(1, max_retries + 1):
                self._update_init_step("se", f"Connecting ({attempt}/{max_retries})...", ACCENT_YELLOW)

                se_client = SEWebSocketClient(
                    host=host, port=port, token=token, server_id=self._se_server_id,
                    on_message_callback=self._on_init_se_msg,
                    on_status_callback=self._on_init_se_status,
                    reconnect_enabled=False,
                )

                self._se_client = se_client
                se_client.start()

                # 等待连接结果（最多 10 秒）
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
                    # 连接成功！停止旧客户端,创建启用重连的新客户端
                    se_client.stop()
                    se_client = SEWebSocketClient(
                        host=host, port=port, token=token, server_id=self._se_server_id,
                        on_message_callback=self._on_init_se_msg,
                        on_status_callback=self._on_init_se_status,
                        reconnect_enabled=SE_RECONNECT_ENABLED,
                    )
                    self._se_client = se_client
                    se_client.start()
                    # 等待新客户端连接就绪
                    for _ in range(50):
                        _time.sleep(0.1)
                        if se_client.is_connected:
                            break
                    return

                # 失败清理
                self._se_client = None
                if attempt < max_retries:
                    import time as _time
                    _time.sleep(min(2 * attempt, 8))

            # 全部重试耗尽
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
        """SE 连接状态回调（来自后台线程）"""
        def _ui():
            if "Auth failed" in msg or "error" in msg.lower() or "Connection error" in msg:
                self._release_se_occupation()
                self._on_init_failed("se", "\u65e0\u6cd5\u8fde\u63a5\u5230\u5b50\u670d\u52a1\u5668",
                    "\u8bf7\u786e\u4fdd\u5b50\u670d\u52a1\u5668\u5df2\u542f\u52a8\u5e76\u91cd\u8bd5\u3002")
                return
            if "Authenticated" in msg:
                # 占用已在 _validate_se / _retry_se_connect 验证通过时注册，此处无需重复
                self._update_init_step("se", "Connected", ACCENT_GREEN)
                self._se_connected = True
                if self.session:
                    self.session.bind_se_client(self._se_client)
                # 全部步骤完成，延迟一小段时间后进入主界面
                self.after(400, self._enter_main_interface)

        self.after(0, _ui)

    def _on_init_se_msg(self, msg: dict):
        """SE 消息回调（连接建立后的首条消息）"""
        def _ui():
            msg_type = msg.get("type", "")
            if msg_type == "CONNECT_ACK":
                payload = msg.get("payload", {})
                node = payload.get("node_info", {})
                self._session_id = payload.get("session_id", "")
                self._node_info = node
                # 日志稍后在主界面中记录
        self.after(0, _ui)

    def _on_init_failed(self, step_key: str, reason: str, hint: str = ""):
        """某一步骤失败，停止流程并显示重试/取消按钮"""
        # 防止 init 界面已销毁后的延迟回调导致 TclError
        if self._init_ready or not self._init_frame or not self.tk.call('winfo', 'exists', str(self._retry_btn)):
            return
        # 释放节点占用（同步，确保释放请求先于后续流程）
        self._release_se_occupation(sync=True)
        self._update_init_step(step_key, "Failed", ACCENT_RED)
        display_msg = reason
        if hint:
            display_msg += f"\n{hint}"
        self._init_hint_var.set(display_msg)
        # 显示重试 + 取消按钮（左右分布）
        try:
            self._retry_btn.pack(side="left", expand=True)
            if hasattr(self, '_cancel_btn'):
                self._cancel_btn.pack(side="right", expand=True)
        except tk.TclError:
            pass  # 窗口已被关闭，忽略

        # 清理可能的部分连接
        if self._se_client:
            self._se_client.stop()
            self._se_client = None
        if self.session:
            self.session.bind_se_client(None)
        self._se_connected = False


    def _on_init_retry(self):
        """用户点击重试：重新尝试 SE 验证+连接（不重新输入账号密码）"""
        self._retry_btn.pack_forget()
        if hasattr(self, '_cancel_btn'):
            self._cancel_btn.pack_forget()
        self._init_hint_var.set("")
        # 只重置 SE 步骤（SM 和 Auth 已通过，不需要重做）
        self._update_init_step("se", "Retrying...", ACCENT_YELLOW)
        # 重新走 SE 验证 + 连接（复用 _start_connection_flow 中的内部逻辑）
        se_addr = getattr(self.session, 'se_address', '') or ''
        if se_addr:
            self.after(200, lambda: self._retry_se_connect(se_addr))
        else:
            self.after(200, lambda: self._retry_se_connect(DEFAULT_SE_HOST))

    def _retry_se_connect(self, target_addr: str):
        """重试 SE 连接（从验证开始）"""
        # 始终先验证 SE 节点在线状态，不允许绕过 SM 验证直接连接
        self._update_init_step("se", "Validating SE...", ACCENT_YELLOW)

        def _check():
            try:
                status_code, resp_data = self.http.get(
                    f"/api/accounts/se-status?address={target_addr}",
                )
                if status_code == 200 and resp_data.get("ok") and resp_data.get("online"):
                    # 检查是否被其他账户占用
                    occupied_by = (resp_data.get("occupied_by") or "").strip()
                    if occupied_by and occupied_by != self._login_username:
                        self.after(0, lambda ob=occupied_by: self._on_init_failed(
                            "se",
                            "\u5b50\u670d\u52a1\u5668\u5df2\u88ab\u5360\u7528",
                            f"\u5f53\u524d\u5b50\u670d\u52a1\u5668\u5df2\u88ab\u8d26\u6237 \u201c{ob}\u201d \u5360\u7528\uff0c\u65e0\u6cd5\u8fde\u63a5\u3002",
                        ))
                        return
                    # 在线且未被占用 → 立即注册占用 + 继续连接
                    node_name = resp_data.get("node_name", "")
                    self._se_target_address = target_addr
                    self._se_server_id = resp_data.get("server_id", "")
                    _log2 = getattr(self, 'log_area', None)
                    if _log2:
                        _log2.log(f"[SE] se-status 返回: server_id={self._se_server_id}, node={node_name}, occupied_by={resp_data.get('occupied_by', '')}", "inf")
                    # ⚡ 同步注册节点占用（阻塞等待成功，消除竞态窗口）
                    occ_ok = self._occupy_se_node(sync=True)
                    if not occ_ok:
                        self.after(0, lambda: self._on_init_failed(
                            "se",
                            "\u5360\u7528\u6ce8\u518c\u5931\u8d25",
                            "\u8282\u70b9\u5360\u7528\u6ce8\u518c\u672a\u6210\u529f\uff0c\u65e0\u6cd5\u786e\u4fdd\u72ec\u5360\u6743\u3002",
                        ))
                        return
                    self.after(0, lambda: self._update_init_step(
                        "se", f"SE OK ({node_name})", ACCENT_GREEN))
                    # ★ 在后台线程执行 WS 连接（含重试），避免冻结 UI
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
        执行 WebSocket 连接（带重试，解决端口未就绪的 Error 1225 问题）
        """
        self._update_init_step("se", "Connecting...", ACCENT_YELLOW)
        token = self.http.token
        target = target_addr or DEFAULT_SE_HOST
        if ':' in target:
            hp = target.rsplit(':', 1)
            host, port = hp[0], int(hp[1]) if hp[1].isdigit() else DEFAULT_SE_PORT
        else:
            host, port = target, DEFAULT_SE_PORT

        max_retries = 5
        for attempt in range(1, max_retries + 1):
            self._update_init_step("se", f"Connecting ({attempt}/{max_retries})...", ACCENT_YELLOW)

            se_client = SEWebSocketClient(
                host=host, port=port, token=token, server_id=self._se_server_id,
                on_message_callback=self._on_init_se_msg,
                on_status_callback=self._on_init_se_status,
                reconnect_enabled=False,
            )

            self._se_client = se_client
            se_client.start()

            # 等待连接结果（最多 10 秒）
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
                # 连接成功！停止旧客户端,创建启用重连的新客户端
                se_client.stop()
                se_client = SEWebSocketClient(
                    host=host, port=port, token=token, server_id=self._se_server_id,
                    on_message_callback=self._on_init_se_msg,
                    on_status_callback=self._on_init_se_status,
                    reconnect_enabled=SE_RECONNECT_ENABLED,
                )
                self._se_client = se_client
                se_client.start()
                # 等待新客户端连接就绪
                for _ in range(50):
                    _time.sleep(0.1)
                    if se_client.is_connected:
                        break
                return

            self._se_client = None
            if attempt < max_retries:
                import time as _time
                _time.sleep(min(2 * attempt, 8))

        # 全部重试耗尽
        self.after(0, lambda: (
            self._release_se_occupation(),
            self._on_init_failed(
                "se",
                "\u65e0\u6cd5\u8fde\u63a5\u5230\u5b50\u670d\u52a1\u5668",
                "\u8fde\u63a5\u5931\u8d25\uff1a\u8fdc\u7a0b\u8ba1\u7b97\u673a\u62d2\u7edd\u8fde\u63a5\uff08Error 1225\uff09\u3002",
            ),
        ))

    def _on_init_cancel(self):
        """用户点击取消：返回登录界面，可修改账户密码重新登录"""
        # 释放节点占用（同步，确保释放请求先于后续流程）
        self._release_se_occupation(sync=True)
        # 清理当前连接状态
        if self._se_client:
            self._se_client.stop()
            self._se_client = None
        if self.session:
            self.session.bind_se_client(None)
        self._se_connected = False

        self.http.token = ""
        self.session = None

        # 重置 init screen 步骤显示
        for key in ("sm", "auth", "se"):
            default = "Waiting" if key != "sm" else "Connecting..."
            color = TEXT_MUTED if key != "sm" else ACCENT_YELLOW
            self._update_init_step(key, default, color)
        self._init_hint_var.set("")
        self._retry_btn.pack_forget()
        if hasattr(self, '_cancel_btn'):
            self._cancel_btn.pack_forget()

        # 重新弹出登录对话框
        self.after(0, self._show_login_first)

    def _occupy_se_node(self, max_retries: int = 3, sync: bool = True):
        """
        向 SM 注册节点占用。

        Args:
            max_retries: 最大重试次数（默认3次）
            sync: 是否同步阻塞等待结果（默认True）
                 True  → 阻塞当前线程直到成功或全部重试耗尽（推荐用于初始化流程）
                 False → 异步发射，不等待结果（仅用于后台保活场景）

        Returns:
            bool: 占用是否成功（sync=False 时恒返回 False 表示未知）
        """
        sid = getattr(self, '_se_server_id', '')
        if not sid:
            log_area = getattr(self, 'log_area', None)
            if log_area:
                log_area.log("[SE] 占用注册失败: server_id 为空（se-status 可能未返回有效节点）", "err")
            return False
        username = self._login_username

        def _do_with_retry():
            """带重试的同步占用逻辑"""
            last_err = ""
            for attempt in range(1, max_retries + 1):
                try:
                    code, resp = self.http.post(
                        f"/api/nodes/{sid}/occupy",
                        {"username": username},
                    )
                    log_area = getattr(self, 'log_area', None)

                    if code == 200 and (resp or {}).get("ok"):
                        if log_area:
                            log_area.log(f"[SE] ✓ 节点占用成功: {username} → {sid}" +
                                        (f" (第{attempt}次尝试)" if attempt > 1 else ""), "ok")
                        return True

                    # 分析失败原因，决定是否值得重试
                    err_msg = (resp or {}).get("error", "") or (resp or {}).get("message", "") or f"HTTP {code}"
                    last_err = err_msg

                    # 不值得重试的情况（直接放弃）
                    if "occupied" in err_msg.lower():
                        if log_area:
                            log_area.log(f"[SE] 节点已被占用，无法抢占: {err_msg}", "warn")
                        return False
                    if "not found" in err_msg.lower():
                        if log_area:
                            log_area.log(f"[SE] 节点不存在于SM: {err_msg}", "err")
                        return False
                    if "Unauthorized" in err_msg or code == 401 or code == 403:
                        if log_area:
                            log_area.log(f"[SE] 认证失效: {err_msg}", "err")
                        return False

                    # 值得重试的情况：临时状态问题（offline/approved 等）
                    if log_area and attempt < max_retries:
                        log_area.log(f"[SE] 占用注册暂未成功 ({attempt}/{max_retries}): {err_msg}, 重试中...", "warn")

                except Exception as e:
                    last_err = str(e)
                    log_area = getattr(self, 'log_area', None)
                    if log_area and attempt < max_retries:
                        log_area.log(f"[SE] 占用请求异常 ({attempt}/{max_retries}): {e}, 重试中...", "warn")

                # 等待后重试（指数退避：1s, 2s, 4s）
                if attempt < max_retries:
                    import time as _time
                    _time.sleep(min(1.0 * (2 ** (attempt - 1)), 5))

            # 全部重试耗尽
            log_area = getattr(self, 'log_area', None)
            if log_area:
                log_area.log(f"[SE] ✗ 节点占用最终失败（{max_retries}次均失败）: {last_err}", "err")
            return False

        if sync:
            # 同步模式：在当前线程执行并等待结果
            return _do_with_retry()
        else:
            # 异步模式：发射到后台线程
            threading.Thread(target=_do_with_retry, daemon=True).start()
            return False

    def _release_se_occupation(self, sync: bool = False) -> bool:
        """断开/取消时，释放节点占用。sync=True 时阻塞等待结果。"""
        sid = getattr(self, '_se_server_id', '')
        if not sid:
            return True

        def _do() -> bool:
            try:
                code, resp = self.http.post(f"/api/nodes/{sid}/release", {})
                log_area = getattr(self, 'log_area', None)
                if log_area:
                    if code == 200:
                        log_area.log(f"[SE] 节点占用已释放: {sid}", "ok")
                    else:
                        log_area.log(f"[SE] 节点占用释放失败(HTTP {code}): {sid}", "warn")
                if code == 200:
                    self._se_server_id = ""
                    return True
                return False
            except Exception:
                return False

        if sync:
            return _do()

        threading.Thread(target=_do, daemon=True).start()
        return False

    def _enter_main_interface(self):
        """所有连接成功 → 销毁初始化界面，构建完整主界面"""
        if self._init_ready:
            return
        self._init_ready = True

        # ── 将 SE 客户端回调切换为主界面版本（支持断线检测+自动重连）──
        if self._se_client:
            self._se_client.on_message = self._on_se_message
            self._se_client.on_status = self._on_se_status
            if self.session:
                self.session.bind_se_client(self._se_client)


        # 销毁初始化界面
        if self._init_frame:
            self._init_frame.destroy()
            self._init_frame = None

        # 构建完整的交易主界面
        self._apply_style()
        self._build_ui_no_login()
        self.log_area = LogArea(self)
        self._build_log_bar()
        self._setup_hotkeys()

        # 设置状态栏
        self.status_var.set("\u25cf Connected")
        self.status_lbl.config(fg=ACCENT_GREEN)
        self._set_se_connection_ui(self._se_connected)

        # 启动各子系统
        node_name = self._node_info.get("node_name", "SE") if self._node_info else "SE"
        region = self._node_info.get("region", "") if self._node_info else ""
        self.log_area.log(f"[System] All systems ready | SM={self.http.base_url} | SE={node_name}({region})", "ok")
        if self._se_connected:
            self.log_area.log("[SE] Direct connection established", "ok")

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


    # ── UI Build ───────────────────────────────────────────────────────────

    def _build_ui_no_login(self):
        """构建完整UI（登录已通过，不含登录表单）"""
        self._build_top_bar()
        self._build_trading_panels()
        self._build_body()

    def _build_top_bar(self):
        """顶部栏：标题 + 状态 + 时间（登录已完成）"""
        top = tk.Frame(self, bg=TOP_BAR_BG, height=56)
        top.pack(fill="x")
        top.pack_propagate(False)

        tk.Label(top, text="SC",
                 bg=TOP_BAR_BG, fg="#4ea1ff",
                 font=FONT_TITLE).pack(side="left", padx=14)

        # 连接状态
        self.status_var = tk.StringVar(value="\u25cf Connecting\u2026")
        self.status_lbl = tk.Label(
            top,
            textvariable=self.status_var,
            bg=TOP_BAR_BG,
            fg=ACCENT_YELLOW,
            font=FONT_BOLD,
        )



        # ── SE (Server_economic) 直连控制区 ───────────────────────────
        sep = tk.Frame(top, bg=BORDER, width=1)
        sep.pack(side="left", padx=8, fill="y", pady=6)

        self._se_status_var = tk.StringVar(value="SE Disconnect")
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
            text="Connect",
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
            text="退出登录",
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

        self._set_se_connection_ui(self._se_connected)



        # 时间

        self.time_var = tk.StringVar()
        tk.Label(top, textvariable=self.time_var, bg=TOP_BAR_BG,
                 fg=TEXT_DIM, font=FONT_MONO).pack(side="right", padx=(12, 4))

        # 时区切换按钮
        self._time_zone_btn = tk.Button(
            top,
            text="CN Time",
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
        """双交易面板区域"""
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

            # 绑定按钮事件和方向键
            panel.buy_btn.config(command=lambda i=pid: self._place_order("Buy to Open", i))
            panel.sell_btn.config(command=lambda i=pid: self._place_order("Sell to Close", i))

            # 方向键绑定
            panel.qty_entry.bind("<Up>", lambda e, i=pid: self._adj_qty(+500, i))
            panel.qty_entry.bind("<Down>", lambda e, i=pid: self._adj_qty(-500, i))
            panel.qty_entry.bind("<Right>", lambda e, i=pid: self._adj_qty(+100, i))
            panel.qty_entry.bind("<Left>", lambda e, i=pid: self._adj_qty(-100, i))
            panel.price_entry.bind("<Up>", lambda e, i=pid: self._adj_price(+0.05, i))
            panel.price_entry.bind("<Down>", lambda e, i=pid: self._adj_price(-0.05, i))
            panel.price_entry.bind("<Right>", lambda e, i=pid: self._adj_price(+0.01, i))
            panel.price_entry.bind("<Left>", lambda e, i=pid: self._adj_price(-0.01, i))

            # Esc 撤单
            for ew in (panel.sym_entry, panel.qty_entry):
                ew.bind("<Escape>", lambda e, i=pid: self._esc_cancel_orders(i))

            self.panels[pid] = panel

        # 兼容旧代码引用（面板0的快捷方式）
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
        """主体区域：订单面板 + 持仓面板"""
        body = tk.Frame(self, bg=DARK_BG)
        body.pack(fill="both", expand=True)

        pw = ttk.PanedWindow(body, orient="horizontal")
        pw.pack(fill="both", expand=True, padx=6, pady=(6, 0))

        # Orders (左)
        self.orders_panel = OrdersPanel(pw, on_refresh_callback=self._refresh_orders,
                                        on_cancel_callback=self._cancel_selected_order)
        of = self.orders_panel.build()
        pw.add(of, weight=1)
        self.ord_tree = self.orders_panel.tree

        # Positions (右)
        self.positions_panel = PositionsPanel(pw, on_refresh_callback=self._refresh_positions,
                                              on_select_callback=self._on_pos_row_click)
        pos_f = self.positions_panel.build()
        pw.add(pos_f, weight=1)
        self.pos_tree = self.positions_panel.tree

        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

    def _build_log_bar(self):
        """底部日志栏"""
        log_frame = tk.Frame(self, bg=PANEL_BG, height=96)
        log_frame.pack(fill="x")
        log_frame.pack_propagate(False)
        self.log_area.frame = log_frame
        self.log_area.build()

    # ── Clock & Poll ────────────────────────────────────────────────────────

    def _tick_clock(self):
        """更新时间显示（支持中国/美国时区切换，包含夏令时）"""
        try:
            if self._time_zone_cn:
                # 中国时间
                if self._tz_cn is not None:
                    # 使用时区对象（支持 DST）
                    now = datetime.datetime.now(self._tz_cn)
                else:
                    # 回退：UTC+8
                    now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
            else:
                # 美国东部时间
                if self._tz_us is not None:
                    # 使用时区对象（自动处理 DST）
                    now = datetime.datetime.now(self._tz_us)
                else:
                    # 回退：UTC-5（不考虑 DST）
                    now = datetime.datetime.utcnow() - datetime.timedelta(hours=5)
            
            time_str = now.strftime("%Y-%m-%d  %H:%M:%S")
            self.time_var.set(f"{time_str}")
        except Exception:
            # 发生异常时使用本地时间
            self.time_var.set(datetime.datetime.now().strftime("%Y-%m-%d  %H:%M:%S"))
        
        self.after(1000, self._tick_clock)

    def _toggle_time_zone(self):
        """切换中国/美国时间显示"""
        self._time_zone_cn = not self._time_zone_cn
        # 更新按钮文本
        self._time_zone_btn.config(text="CN Time" if self._time_zone_cn else "US Time")

    def _poll(self):
        """150ms 主轮询循环"""
        # 消费模拟行情队列
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

        # 每3秒更新持仓P&L（用本地行情缓存）
        if now - self._last_pos_time > POSITIONS_INTERVAL / 1000 and self.pos_tree.get_children():
            self.positions_panel.live_update_pnl(self.current_quote)
            self._last_pos_time = now

        # 每30秒从服务器刷新持仓+订单
        if self.session.connected and not self.session.mock_mode:
            if now - self._last_orders_time > ORDERS_INTERVAL / 1000:
                self._refresh_positions()
                self._refresh_orders()
                self._last_orders_time = now

        # 心跳检测（每10秒ping服务器）
        if self.session.connected and not self.session.mock_mode:
            if now - self._last_heartbeat > HEARTBEAT_INTERVAL / 1000:
                self._last_heartbeat = now
                def _ping():
                    ok = self.http.health_check()
                    if not ok:
                        self.after(0, self._on_server_disconnect)
                threading.Thread(target=_ping, daemon=True).start()

        self.after(POLL_INTERVAL, self._poll)

    # ── Symbol Handling ────────────────────────────────────────────────────

    def _on_symbol_enter(self, pid: int, _=None):
        """输入股票代码回车处理"""
        p = self.panels[pid]
        sym = p.sym_var.get().strip().upper()
        if not sym:
            return
        p.set_symbol(sym)

        if sym in self.current_quote:
            self._refresh_strip(self.current_quote[sym], None, pid)

        # 实时同步订阅集合（目标：symbol 回车后立即显示行情）
        self._sync_quote_subscriptions_async()

    def _sync_quote_subscriptions_async(self):
        """在后台线程同步当前 UI 需要的订阅集合到 SE"""
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
                        self.after(0, lambda m=msg: self.log_area.log(f"[SE] Unsubscribe failed: {m}", "warn"))

                if to_sub:
                    ok, msg = self.session.subscribe_quotes(to_sub, timeout=6.0)
                    if ok:
                        self._quote_subscribed_symbols.update(to_sub)
                    else:
                        self.after(0, lambda m=msg: self.log_area.log(f"[SE] Subscribe failed: {m}", "warn"))

        threading.Thread(target=_bg, daemon=True).start()

    def _sym_key_filter(self, event, pid: int = 0):

        """
        sym_entry 键盘过滤器：
        只允许字母、导航键；小键盘数字设置数量；F键触发下单
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

        # Ctrl+1-9 → 100-900股
        if state & 0x4 and ks in ctrl_map:
            self._set_qty(ctrl_map[ks], pid)
            return "break"

        # 小键盘数字 → 1000-9000股
        if ks in numpad_map and not (state & 0x4):
            self._set_qty(numpad_map[ks], pid)
            return "break"

        # 导航键放行
        if ks in nav_keys:
            return

        # 字母放行
        if event.char and event.char.isalpha():
            return

        return "break"

    # ── Panel Activation ───────────────────────────────────────────────────

    def _activate_panel(self, pid: int):
        """高亮激活面板，其他恢复暗色边框"""
        for i, p in self.panels.items():
            p.set_active(i == pid)
        self.active_panel_id = pid

    def _get_active_panel_id(self) -> int:
        """获取当前焦点所在的面板ID"""
        focused = self.focus_get()
        for pid, p in self.panels.items():
            if focused in (p.sym_entry, p.qty_entry, p.price_entry):
                return pid
        return self.active_panel_id

    def _get_pos_direction(self, symbol: str) -> str:
        """查询指定标的持仓方向: long/short/none"""
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

    # ── Hotkeys ────────────────────────────────────────────────────────────

    def _setup_hotkeys(self):
        """绑定F1-F4快捷键到各面板控件"""
        def _bind_f(widget):
            widget.bind("<F1>", lambda e: self._f_key_order("sell"))
            widget.bind("<F2>", lambda e: self._f_key_limit("sell"))
            widget.bind("<F3>", lambda e: self._f_key_order("buy"))
            widget.bind("<F4>", lambda e: self._f_key_limit("buy"))

        def _bind_f_sym(widget):
            """sym框F键需要过滤字母输入冲突"""
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

        # 将sym_key_filter绑定到sym_entry
        for pid, p in self.panels.items():
            p.sym_entry.bind("<Key>", lambda e, i=pid: self._sym_key_filter(e, i))

    def _f_key_order(self, side: str):
        """F1=市价卖出 F3=市价买入 — 根据持仓智能选择action"""
        pid = self._get_active_panel_id()
        p = self.panels[pid]
        sym = p.order_sym_var.get()
        if sym == "\u2014":
            self.log_area.log("F\u952e\u4e0b\u5355\uff1a\u8bf7\u5148\u52a0\u8f7d\u80a1\u7968\u4ee3\u7801", "err")
            return
        try:
            qty = int(p.qty_entry.get())
        except Exception:
            self.log_area.log("F\u952e\u4e0b\u5355\uff1aqty \u65e0\u6548", "err"); return
        if qty <= 0:
            self.log_area.log("F\u952e\u4e0b\u5355\uff1aqty \u5fc5\u987b\u5927\u4e8e0", "err"); return

        direction = self._get_pos_direction(sym)
        if side == "buy":
            action = "Buy to Close" if direction == "short" else "Buy to Open"
        else:
            action = "Sell to Close" if direction == "long" else "Sell to Open"

        tif = p.tif_var.get()
        self.log_area.log(f"[F] {action} {qty} {sym} @ MKT | {tif}", "inf")
        self._submit_order_bg(sym, qty, 0, action, "market", tif)

    def _f_key_limit(self, side: str):
        """F2=Limit卖就绪 F4=Limit买就绪 — 填入默认价格，焦点到price框"""
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

        # 切换为Limit模式
        p.order_type_var.set("Limit")
        self._on_order_type_change(pid)

        # 填入默认价格
        p.price_entry.config(state="normal")
        p.price_entry.delete(0, "end")
        if default_px:
            p.price_entry.insert(0, f"{default_px:.2f}")

        p._pending_action = action

        # 高亮price框并聚焦
        hl_color = ACCENT_GREEN if side == "buy" else ACCENT_RED
        p.price_entry.focus_set()
        p.price_entry.config(highlightthickness=2,
                             highlightbackground=hl_color,
                             highlightcolor=hl_color)
        p.price_entry.bind("<Return>", lambda e, i=pid: self._f_limit_submit(i))
        p.price_entry.bind("<Escape>", lambda e, i=pid: self._f_limit_cancel(i))

    def _f_limit_submit(self, pid: int):
        """price框回车：提交Limit单"""
        p = self.panels[pid]
        sym = p.order_sym_var.get()
        action = p._pending_action
        if not action or sym == "\u2014":
            return
        try:
            qty = int(p.qty_entry.get())
        except Exception:
            self.log_area.log("F\u952e\u4e0b\u5355\uff1aqty \u65e8\u6548", "err"); return
        try:
            price = round(float(p.price_entry.get().strip()), 2)
        except Exception:
            self.log_area.log("F\u952e\u4e0b\u5355\uff1aprice \u65e8\u6548", "err"); return
        if price <= 0:
            self.log_area.log("F\u952e\u4e0b\u5355\uff1aprice \u5fc5\u987b\u5927\u4e8e0", "err"); return

        tif = p.tif_var.get()
        self.log_area.log(f"[F] {action} {qty} ${price:.2f} | {tif}", "inf")

        # 解绑回车/Esc，恢复状态
        p.price_entry.unbind("<Return>")
        p.price_entry.unbind("<Escape>")
        p.price_entry.config(highlightthickness=0)
        p._pending_action = None

        self._submit_order_bg(sym, qty, price, action, "limit", tif)

    def _f_limit_cancel(self, pid: int):
        """Esc取消F2/F4待下单状态"""
        p = self.panels[pid]
        p.price_entry.unbind("<Return>")
        p.price_entry.unbind("<Escape>")
        p.price_entry.config(highlightthickness=0)
        p._pending_action = None

    # ── Qty / Price Adjustment ────────────────────────────────────────────

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

    # ── Order Type Toggle ─────────────────────────────────────────────────

    def _on_order_type_change(self, pid: int, _=None):
        """切换Market/Limit时控制price框可用性"""
        p = self.panels[pid]
        is_mkt = p.order_type_var.get() == "Market"
        p.price_entry.configure(state="disabled" if is_mkt else "normal",
                               bg=DARK_BG if is_mkt else INPUT_BG)
        p.price_lbl.configure(fg=TEXT_MUTED if is_mkt else TEXT_DIM)

    # ── Login ──────────────────────────────────────────────────────────────

    # ── Place Order ────────────────────────────────────────────────────────

    def _place_order(self, action: str, pid: int = 0):
        p = self.panels[pid]
        sym = p.order_sym_var.get()
        if sym == "\u2014":
            messagebox.showwarning("Warning", "Please select a symbol first")
            return
        try:
            qty = int(p.qty_entry.get())
        except ValueError:
            messagebox.showerror("Error", "Please enter a valid quantity")
            return
        is_mkt = p.order_type_var.get() == "Market"
        price = 0.0
        if not is_mkt:
            try:
                price = round(float(p.price_entry.get().strip()), 2)
            except ValueError:
                messagebox.showerror("Error", "Please enter a valid price")
                return
            if price <= 0:
                messagebox.showerror("Error", "Price must be greater than 0")
                return
        tif = p.tif_var.get()
        price_str = "MKT" if is_mkt else f"${price:.2f}"
        self.log_area.log(f"{action} {qty} {sym} @ {price_str} | {tif}", "inf")
        self._submit_order_bg(sym, qty, price, action,
                              "market" if is_mkt else "limit", tif)

    def _submit_order_bg(self, symbol: str, qty: int, price: float,
                         action: str, order_type: str, tif: str):
        """在后台线程中提交订单"""
        def _bg():
            ok, msg = self.session.place_order(symbol, qty, price, action, order_type, tif=tif)
            self.after(0, lambda: self.log_area.log(sanitize(msg), "ok" if ok else "err"))
            if ok:
                self.after(1500, self._refresh_positions)
                self.after(1500, self._refresh_orders)
        threading.Thread(target=_bg, daemon=True).start()

    # ── Esc Cancel ─────────────────────────────────────────────────────────

    def _esc_cancel_orders(self, pid: int):
        """Esc：先取消待下单状态，否则撤当前symbol所有live订单"""
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
                self.after(0, lambda m=msg, o=ok: self.log_area.log(sanitize(m), "ok" if o else "err"))
            self.after(1500, self._refresh_orders)
        threading.Thread(target=_bg, daemon=True).start()

    # ── Positions ──────────────────────────────────────────────────────────

    def _refresh_positions(self):
        def _bg():
            positions = self.session.get_today_activity()
            err = getattr(self.session, "_pos_error", "")
            self.after(0, lambda: self._update_positions(positions, err))
        threading.Thread(target=_bg, daemon=True).start()

    def _update_positions(self, positions: list[dict], err: str = ""):
        if err:
            self.log_area.log(sanitize(f"Position fetch failed: {err}"), "err")
        if self.positions_panel:
            self.positions_panel.update_data(positions, self.current_quote)

    def _on_pos_row_click(self, symbol: str):
        """点击持仓行，将symbol填入面板0"""
        self.panels[0].sym_var.set(symbol)
        self._on_symbol_enter(0)

    # ── Orders ─────────────────────────────────────────────────────────────

    def _refresh_orders(self):
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
        def _bg():
            ok, msg = self.session.cancel_order(order_id)
            self.after(0, lambda: self.log_area.log(sanitize(msg), "ok" if ok else "err"))
            if ok:
                self.after(1000, self._refresh_orders)
        threading.Thread(target=_bg, daemon=True).start()

    # ── Quote Stream ───────────────────────────────────────────────────────

    def _refresh_strip(self, quote: dict, prev_quote: dict | None, pid: int = None):
        """更新行情条显示"""
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
            p.order_last_var.set(f"Last: ${quote['last']:.2f}")

            # 自动填充ask价格
            if p.price_needs_fill:
                p.fill_price_from_quote(quote["ask"],
                                         p.order_type_var.get() == "Market")

    def _start_mock_stream(self):
        """启动模拟行情线程"""
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

    def _start_real_stream(self):
        """启动真实WebSocket行情流"""
        self._mock_active = False
        self._stream_active = True
        self._ws_stream = QuoteStream(
            http_client=self.http,
            on_quote_callback=self._handle_ws_quote,
            on_status_callback=lambda msg: self.log_area.log(msg, "ok"),
        )
        self._ws_stream.start()

    def _handle_ws_quote(self, quote: dict):
        """WebSocket行情回调（通过after推送到主线程）"""
        sym = quote["symbol"]
        prev = self.current_quote.get(sym)
        self.current_quote[sym] = quote
        self.after(0, lambda _q=quote, _p=prev: self._refresh_strip(_q, _p))

    def _on_server_disconnect(self):
        """服务器断线处理"""
        if not self.session.connected:
            return
        self.session.connected = False
        self._stream_active = False
        self._mock_active = False
        self.status_var.set("\u25cf Not connected")
        self.status_lbl.config(fg=ACCENT_RED)
        self.log_area.log("Server disconnected", "err")

    # ── SE (Server_economic) Direct Connection ──────────────────────────────

    def _toggle_se_connection(self):
        """切换 SE WebSocket 连接"""
        if self._se_connected:
            self._se_disconnect()
        else:
            self._se_connect()

    def _se_connect(self):
        """建立到 Server_economic 的 WebSocket 连接（含验证和占用注册）"""
        if self._se_client and self._se_client.is_active:
            return

        self._se_btn.config(state="disabled", text="Validating...")
        self.log_area.log("[SE] Validating node status...", "inf")

        target_addr = self._se_target_address or DEFAULT_SE_HOST

        def _do_connect_with_retry(max_retries=5):
            """
            带重试的 WS 连接（解决 Error 1225 / WSAECONNREFUSED 问题）。
            
            场景：SM 标记 online 但 SE 的 WS 端口尚未就绪
                  （SE 进程刚启动，心跳先于 ws.serve() 完成）
                  
            策略：每尝试创建 client → start → 等 10s 看是否 connected
                 失败则指数退避重试（2s/4s/6s/8s），共最多 5 次。
            """
            token = self.http.token
            if ':' in target_addr:
                hp = target_addr.rsplit(':', 1)
                host, port = hp[0], int(hp[1]) if hp[1].isdigit() else DEFAULT_SE_PORT
            else:
                host, port = target_addr, DEFAULT_SE_PORT

            last_err = ""

            for attempt in range(1, max_retries + 1):
                # 更新按钮状态
                self.after(0, lambda a=attempt, m=max_retries: self._se_btn.config(
                    state="disabled", text=f"Connecting ({a}/{m})...",
                ))

                # 创建客户端（暂不启用内部自动重连，由本函数控制重试）
                client = SEWebSocketClient(
                    host=host, port=port, token=token,
                    on_message_callback=self._on_se_message,
                    on_status_callback=self._on_se_status,
                    reconnect_enabled=False,
                )
                self._se_client = client
                client.start()

                # 等待连接结果（最多 10 秒）
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
                    # ★ 连接成功！恢复自动重连能力（后续断线可自动恢复）
                    client._reconnect_enabled = SE_RECONNECT_ENABLED
                    self.after(0, lambda a=attempt: (
                        self.log_area.log(
                            f"[SE] \u2713 \u8fde\u63a5\u6210\u529f" +
                            (f" (\u7b2c{a}\u6b21\u5c1d\u8bd5)" if a > 1 else ""),
                            "ok",
                        ),
                    ))
                    return

                # 本次失败，清理
                self._se_client = None
                last_err = "\u8fdc\u7a0b\u8ba1\u7b97\u673a\u62d2\u7edd\u8fde\u63a5 (\u7aef\u53e3\u53ef\u80fd\u5c1a\u672a\u5c31\u7eea)"

                if attempt < max_retries:
                    self.after(0, lambda a=attempt, m=max_retries: (
                        self.log_area.log(
                            f"[SE] WS \u8fde\u63a5\u672a\u5c31\u7eea ({a}/{m})\uff0c\u7b49\u5f85\u540e\u91cd\u8bd5...",
                            "warn",
                        ),
                    ))
                    import time as _time
                    _time.sleep(min(2 * attempt, 8))

            # 全部重试耗尽
            self.after(0, lambda: (
                self.log_area.log(f"[SE] \u2717 \u8fde\u63a5\u5931\u8d25\uff08{max_retries}\u6b21\u5747\u5931\u8d25\uff09: {last_err}", "err"),
                self._set_se_connection_ui(False),
            ))

        def _check():
            try:
                status_code, resp_data = self.http.get(
                    f"/api/accounts/se-status?address={target_addr}",
                )
                if status_code == 200 and resp_data.get("ok") and resp_data.get("online"):
                    # 检查占用
                    occupied_by = (resp_data.get("occupied_by") or "").strip()
                    if occupied_by and occupied_by != self._login_username:
                        self.after(0, lambda ob=occupied_by: (
                            self.log_area.log(f"[SE] 节点已被账户 '{ob}' 占用，无法连接", "err"),
                            self._set_se_connection_ui(False),
                        ))
                        return
                    # ★ 关键修复：从 se-status 响应中提取 server_id（Disconnect 后重连时必须重新获取）
                    node_name = resp_data.get("node_name", "")
                    self._se_target_address = target_addr
                    self._se_server_id = resp_data.get("server_id", "")
                    # 在线且未被占用 → 同步注册占用 + 连接
                    occ_ok = self._occupy_se_node(sync=True)
                    if not occ_ok:
                        self.after(0, lambda: (
                            self.log_area.log("[SE] 节点占用注册失败，无法连接", "err"),
                            self._set_se_connection_ui(False),
                        ))
                        return
                    # ★ 在后台线程执行 WS 连接（含重试），避免冻结 UI
                    threading.Thread(target=_do_connect_with_retry, daemon=True).start()
                else:
                    self.after(0, lambda: (
                        self.log_area.log("[SE] 子服务器不在线，无法连接", "err"),
                        self._set_se_connection_ui(False),
                    ))
            except Exception as e:
                self.after(0, lambda: (
                    self.log_area.log(f"[SE] 验证失败: {e}", "err"),
                    self._set_se_connection_ui(False),
                ))
        threading.Thread(target=_check, daemon=True).start()

    def _se_disconnect(self):
        """断开 SE 连接"""
        # 如果正在重连，先隐藏重连弹窗
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

        # 释放节点占用（同步，确保释放请求先于后续流程）

        self._release_se_occupation(sync=True)
        self._set_se_connection_ui(False)
        self.log_area.log("[SE] Disconnected", "inf")

    def _on_se_status(self, msg: str):
        """SE 连接状态变化回调（来自后台线程，需用 after 切回主线程）"""
        def _ui_update():
            if "Authenticated" in msg:
                self._se_connected = True
                self._set_se_connection_ui(True)
                if self.session:
                    self.session.bind_se_client(self._se_client)
                self.log_area.log(f"[SE] {msg}", "ok")
                self._sync_quote_subscriptions_async()

                # ★ 重连成功后自动恢复节点占用（防止占用丢失）


                # 场景：SE 掉线→SM 标记离线并释放占用→SE 恢复→Client 重连成功
                # 注意：此处运行在 UI 线程中，使用 async 模式避免冻结界面
                # （主连接流程中的 occupy 已是同步的，此处为辅助保活）
                if self._se_server_id:
                    self._occupy_se_node(sync=False)
                
                # 自动查询一次状态验证连接
                if self._se_client and self._se_client.is_connected:
                    self._se_client.send_query_status()
                # ── 重连成功 → 隐藏重连弹窗 ──
                if self._reconnecting and self._reconnect_dialog:
                    self._hide_reconnect_dialog()

            elif "Reconnecting" in msg or "reconnecting" in msg.lower():
                # ── 运行中自动重连中 → 显示/更新重连弹窗 ──
                self._set_se_connection_ui(False)
                if not self._reconnecting and self._init_ready:
                    self._reconnecting = True
                    self._show_reconnect_dialog(msg)
                elif self._reconnect_dialog:
                    # 更新弹窗状态文本
                    self._reconnect_var.set(msg)

            elif "Auth failed" in msg:
                # ★ 仅匹配认证失败，不匹配 SE 返回的 "Error [xxx]: ..." 消息
                # 原来的 "error" in msg.lower() 会误匹配 SE 的 ERROR 类型响应消息，
                # 导致正在重连时收到一个业务错误就 _cancel_reconnect()
                if self._reconnecting:
                    # 重连过程中认证失败（如 token 过期）→ 停止重连，提示用户
                    self._cancel_reconnect()
                self._release_se_occupation()
                self._set_se_connection_ui(False)
                with self._quote_sub_lock:
                    self._quote_subscribed_symbols.clear()
                self.log_area.log(f"[SE] {msg}", "err")


            elif "Connection error" in msg or (msg.startswith("Disconnected:") and not self._se_active_se()):
                if self._init_ready and self._se_connected and not self._reconnecting:
                    # 运行中断线且尚未进入重连 → 触发自动重连流程
                    self._se_connected = False
                    self._set_se_connection_ui(False)
                    with self._quote_sub_lock:
                        self._quote_subscribed_symbols.clear()
                    self.log_area.log("[SE] 子服务器连接断开，正在尝试重新连接...", "warn")

                    self._start_se_reconnect()
                elif self._reconnecting:
                    # 重连彻底失败（达到最大次数或 stop 后的 Disconnected 通知）
                    self._cancel_reconnect()
                    self._release_se_occupation()
                    self._set_se_connection_ui(False)
                    with self._quote_sub_lock:
                        self._quote_subscribed_symbols.clear()
                else:
                    # 初始化阶段失败或手动断开
                    self._release_se_occupation()
                    self._set_se_connection_ui(False)
                    with self._quote_sub_lock:
                        self._quote_subscribed_symbols.clear()
                    self.log_area.log(f"[SE] {msg}", "err")


            else:
                # 其他状态消息（Connecting、Connected 等）
                if self._reconnecting:
                    self._reconnect_var.set(msg)
                self.log_area.log(f"[SE] {msg}", "inf")

        self.after(0, _ui_update)

    def _on_se_message(self, msg: dict):
        """SE 消息回调（后台线程 → after 到主线程）"""
        def _ui_update():
            msg_type = msg.get("type", "")

            if msg_type == "CONNECT_ACK":
                payload = msg.get("payload", {})
                node = payload.get("node_info", {})
                self.log_area.log(
                    f"[SE] Connected to node: {node.get('node_name', '?')} "
                    f"(id={node.get('server_id', '?')}, region={node.get('region', '?')})",
                    "ok"
                )
            elif msg_type == "STATUS_RESPONSE":
                info = msg.get("payload", {}).get("node_info", {})
                self.log_area.log(
                    f"[SE] Status: {info.get('registration_status', '?')} | "
                    f"heartbeat={'OK' if info.get('heartbeat_ok') else 'FAIL'} | "
                    f"clients={info.get('connections', 0)}",
                    "inf"
                )
            elif msg_type == "QUOTE_DATA":
                payload = msg.get("payload", {}) if isinstance(msg.get("payload", {}), dict) else {}
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

                payload = msg.get("payload", {}) if isinstance(msg.get("payload", {}), dict) else {}
                reason = payload.get("reason", "admin_force_release")
                self.log_area.log(f"[SE] 连接被管理端强制断开 ({reason})", "warn")

                # 停止自动重连并断开当前连接，保持为可手动重连状态
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
                messagebox.showwarning("连接已被管理端断开", "当前 SE 连接已被管理员强制断开。\n如需继续使用，请点击 Connect 重新连接。")

            elif msg_type == "ERROR":
                err = msg.get("payload", {})
                self.log_area.log(f"[SE] Error [{err.get('code', '')}]: {err.get('message', '')}", "err")

            elif msg_type == "PONG":
                pass  # 心跳响应，静默
            else:
                self.log_area.log(f"[SE] Recv {msg_type}: {str(msg)[:120]}", "inf")

        self.after(0, _ui_update)

    def _se_active_se(self) -> bool:
        """检查 SE client 是否仍然活跃"""
        return self._se_client is not None and self._se_client.is_active

    # ── SE 自动重连（运行中断线后）───────────────────────────────────────

    def _start_se_reconnect(self):
        """
        运行中 SE 断线 → 启动自动重连流程
        创建新的 SEWebSocketClient 并启用重连模式，后台线程自动尝试重连
        """
        if self._reconnecting:
            return  # 已在重连中

        self._reconnecting = True
        self._reconnect_cancelled = False
        target_addr = self._last_connected_se or self._se_target_address or DEFAULT_SE_HOST
        token = self.http.token

        if ':' in target_addr:
            hp = target_addr.rsplit(':', 1)
            host, port = hp[0], int(hp[1]) if hp[1].isdigit() else DEFAULT_SE_PORT
        else:
            host, port = target_addr, DEFAULT_SE_PORT

        # ★ 关键修复：先停止旧客户端，防止幽灵线程残留
        # 旧客户端的后台线程可能仍在 sleep/退避等待，如果不 stop()，
        # 它醒来后会尝试重新连接，导致两个 WS 连接同时竞争同一个 SE 端口
        if self._se_client and self._se_client.is_active:
            self.log_area.log("[SE] Stopping old connection before reconnect...", "inf")
            self._se_client.stop()

        # 创建启用重连的 SE 客户端
        se_client = SEWebSocketClient(
            host=host, port=port, token=token,
            on_message_callback=self._on_se_message,
            on_status_callback=self._on_se_status,
            reconnect_enabled=SE_RECONNECT_ENABLED,
        )
        self._se_client = se_client
        se_client.start()

        self.log_area.log(f"[SE] Auto-reconnecting to {host}:{port}...", "inf")

    def _show_reconnect_dialog(self, initial_msg: str = ""):
        """显示重连弹窗（覆盖主界面的模态式提示）"""
        if self._reconnect_dialog:
            return  # 已存在则不重复创建

        dlg = tk.Toplevel(self)
        self._reconnect_dialog = dlg
        dlg.title("SE 重连")
        dlg.geometry("420x240")
        dlg.resizable(False, False)
        dlg.configure(bg=DARK_BG)

        # 居中显示
        dlg.transient(self)
        dlg.grab_set()
        dlg.protocol("WM_DELETE_WINDOW", self._cancel_reconnect)

        # 计算居中位置
        dlg.update_idletasks()
        pw = self.winfo_width()
        ph = self.winfo_height()
        px = self.winfo_x()
        py = self.winfo_y()
        x = px + (pw - 420) // 2
        y = py + (ph - 240) // 2
        dlg.geometry(f"+{x}+{y}")

        # 标题图标
        title_frame = tk.Frame(dlg, bg=DARK_BG)
        title_frame.pack(fill="x", pady=(24, 8))
        tk.Label(title_frame, text="\u26a0\ufe0f", font=FONT_TITLE,
                 bg=DARK_BG, fg=ACCENT_YELLOW).pack()


        # 主提示文字
        tk.Label(dlg, text="\u5b50\u670d\u52a1\u5668\u8fde\u63a5\u5df2\u65ad\u5f00",
                 bg=DARK_BG, fg=TEXT_PRIMARY, font=FONT_UI).pack(pady=(4, 2))
        tk.Label(dlg, text="\u6b63\u5728\u5c1d\u91cd\u65b0\u8fde\u63a5...",
                 bg=DARK_BG, fg=TEXT_DIM, font=FONT_UI_SM).pack(pady=(0, 16))

        # 状态文本（动态更新）
        self._reconnect_var.set(initial_msg or "\u7b49\u5f85\u8fde\u63a5...")
        status_lbl = tk.Label(dlg, textvariable=self._reconnect_var,
                               bg=DARK_BG, fg=ACCENT_YELLOW, font=FONT_MONO_SM,
                               wraplength=380)
        status_lbl.pack(pady=(0, 20))

        # 取消按钮容器
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
        """隐藏重连弹窗（重连成功时调用）"""
        self._reconnecting = False
        if self._reconnect_dialog:
            try:
                self._reconnect_dialog.destroy()
            except tk.TclError:
                pass
            self._reconnect_dialog = None

    def _cancel_reconnect(self):
        """用户取消重连：停止客户端、释放占用、恢复 UI"""
        if not self._reconnecting and not self._reconnect_dialog:
            return

        self._reconnecting = False
        self._reconnect_cancelled = True

        # 停止正在重连的 SE 客户端
        if self._se_client:
            self._se_client.stop()
            self._se_client = None
        if self.session:
            self.session.bind_se_client(None)
        self._se_connected = False
        with self._quote_sub_lock:
            self._quote_subscribed_symbols.clear()

        # 隐藏弹窗

        if self._reconnect_dialog:
            try:
                self._reconnect_dialog.destroy()
            except tk.TclError:
                pass
            self._reconnect_dialog = None

        # 释放节点占用（同步，确保释放请求先于后续流程）
        self._release_se_occupation(sync=True)

        # 恢复 UI 状态
        self._set_se_connection_ui(False)
        self.log_area.log("[SE] 用户取消了重连，子服务器连接已释放", "warn")

    def _logout_to_login(self):
        """退出当前账户并返回登录界面"""
        if not messagebox.askyesno("退出登录", "确认退出当前账户并返回登录界面？"):
            return

        # 停止重连状态/弹窗
        self._reconnecting = False
        if self._reconnect_dialog:
            try:
                self._reconnect_dialog.destroy()
            except tk.TclError:
                pass
            self._reconnect_dialog = None

        # 断开 SE 连接
        if self._se_client:
            self._se_client.stop()
            self._se_client = None
        self._se_connected = False
        with self._quote_sub_lock:
            self._quote_subscribed_symbols.clear()

        # 释放节点占用（同步，确保释放请求先于后续流程）

        self._release_se_occupation(sync=True)

        # 解绑会话中的 SE client
        if self.session:
            self.session.bind_se_client(None)

        # 退出 SM 登录并清空 token
        if self.session:
            try:
                self.session.logout()
            except Exception:
                self.http.token = ""

        # 清空当前账号缓存，允许切换账号登录
        self._login_username = ""
        self._login_password = ""

        # 隐藏主界面组件，回到初始化+登录流程
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

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def on_close(self):

        """窗口关闭"""
        self._mock_active = False
        self._stream_active = False
        self._reconnecting = False  # 取消重连状态
        if self._se_dot_job is not None:
            try:
                self.after_cancel(self._se_dot_job)
            except Exception:
                pass
            self._se_dot_job = None

        # 隐藏重连弹窗
        if self._reconnect_dialog:
            try:
                self._reconnect_dialog.destroy()
            except tk.TclError:
                pass
            self._reconnect_dialog = None
        # 断开 SE 连接
        if self._se_client:
            self._se_client.stop()
        if self.session:
            self.session.bind_se_client(None)
        with self._quote_sub_lock:
            self._quote_subscribed_symbols.clear()
        if self._ws_stream:

            self._ws_stream.stop()
        # 释放节点占用（同步，防止关闭窗口后节点被永久锁定）
        self._release_se_occupation(sync=True)
        # 登出并清理token（防止服务端token残留）
        if self.http and self.http.token:
            try:
                self.http.post("/auth/logout", {})
                if hasattr(self, 'log_area') and self.log_area:
                    self.log_area.log("[System] Logged out successfully", "ok")
            except Exception as e:
                # 网络错误不影响窗口关闭
                pass
            finally:
                self.http.token = ""
        self.destroy()


# ── Mock Quote Helper ────────────────────────────────────────────────────────

def mock_quote(sym: str, base: float) -> dict:
    """生成模拟行情数据"""
    last = round(base + random.uniform(-0.3, 0.3), 2)
    sp = random.uniform(0.01, 0.08)
    return dict(symbol=sym, bid=round(last - sp, 2), ask=round(last + sp, 2),
                last=last, volume=random.randint(100, 9999) * 100,
                timestamp=datetime.datetime.now().strftime("%H:%M:%S"))
