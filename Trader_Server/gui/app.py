"""
Trader_Server Desktop GUI — 子服务端控制面板（桌面版）

功能:
  - 节点注册流程（Ping → Submit → SSE Wait）
  - 实时状态仪表盘（心跳、连接数、版本）
  - 经济指标数据展示
  - 注册凭证管理 / 重新注册
  - 表单锁定机制

启动方式:
    python -m Trader_Server.gui.app
    python Trader_Server/gui/app.py
"""

import ctypes
import json
import socket
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk


from .api_client import SEApiClient


# ── Theme ────────────────────────────────────────────────────

BG_DARK      = "#0d1117"
BG_SECONDARY = "#161b22"
BG_CARD      = "#1c2128"
BORDER_COLOR = "#30363d"

TEXT_PRIMARY = "#e6edf3"
TEXT_SECONDARY= "#8b949e"
TEXT_MUTED   = "#6e7681"

ACCENT_BLUE  = "#58a6ff"
ACCENT_GREEN = "#3fb950"
ACCENT_YELLOW= "#d29922"
ACCENT_RED   = "#f85149"
ACCENT_PURPLE= "#a371f7"

FONT_TITLE   = ("Segoe UI", 15, "bold")
FONT_BOLD    = ("Segoe UI", 11, "bold")
FONT_NORMAL  = ("Segoe UI", 11)
FONT_MONO    = ("Courier New", 11)
FONT_MONO_SM = ("Courier New", 10)

STATUS_COLORS = {
    "uninitialized": TEXT_MUTED,
    "registering": ACCENT_YELLOW,
    "approved":     ACCENT_GREEN,
    "running":      ACCENT_GREEN,
    "online":       ACCENT_GREEN,
    "error":        ACCENT_RED,
    "rejected":     ACCENT_RED,
}

STATUS_LABELS = {
    "uninitialized": "未初始化",
    "registering":   "注册中",
    "approved":      "已批准",
    "running":       "运行中",
    "online":        "在线",
    "error":         "错误",
    "rejected":      "已拒绝",
}


class SEControlPanel(tk.Tk):
    """Trader_Server 控制面板 — 桌面版主窗口"""

    def __init__(self):
        super().__init__()

        self._ui_scale = self._setup_dpi_scaling()
        self._apply_scaled_fonts()

        self.title("Trader_Server — 控制面板")
        self.configure(bg=BG_DARK)

        # ── 窗口尺寸与居中（4K 友好）──────────────────────
        self.update_idletasks()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        self._screen_w = sw
        self._screen_h = sh

        w = min(int(sw * 0.86), self._s(2300))
        h = min(int(sh * 0.88), self._s(1400))
        w = max(w, self._s(1280))
        h = max(h, self._s(760))

        x = max((sw - w) // 2, 0)
        y = max((sh - h) // 2, 0)
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.minsize(self._s(1100), self._s(680))


        # ── API 客户端 ─────────────────────────────────
        self.api = SEApiClient()

        # ── 全局状态 ───────────────────────────────────
        self._registered: bool = False
        self._poll_job: str | None = None
        self._uptime_start: float = time.time()
        self._sse_thread: threading.Thread | None = None
        self._sse_cancelled: bool = False
        self._register_in_progress: bool = False
        self._wait_overlay: tk.Toplevel | None = None
        self._wait_modal: tk.Toplevel | None = None
        self._wait_title_label: tk.Label | None = None
        self._wait_req_label: tk.Label | None = None
        self._wait_cancel_btn: tk.Button | None = None
        self._cancel_in_progress: bool = False
        self._current_request_id: str = ""

        self._current_manager_url: str = ""
        self._abandoned_request_ids: dict[str, float] = {}




        # ── 构建 UI ────────────────────────────────────
        self._build_top_bar()
        self._build_main_area()

        # ── 启动轮询 ──────────────────────────────────
        self._refresh_status()
        self._schedule_poll()
        self._update_uptime_loop()

    def _s(self, px: int) -> int:
        return max(1, int(px * self._ui_scale))

    def _setup_dpi_scaling(self) -> float:
        """启用高 DPI 感知并返回 UI 缩放系数。"""
        try:
            if tk.TkVersion >= 8.6:
                try:
                    ctypes.windll.shcore.SetProcessDpiAwareness(1)
                except Exception:
                    try:
                        ctypes.windll.user32.SetProcessDPIAware()
                    except Exception:
                        pass
        except Exception:
            pass

        scale = 1.0
        try:
            dpi = float(self.winfo_fpixels("1i"))
            scale = max(1.0, min(2.0, dpi / 96.0))
            self.tk.call("tk", "scaling", dpi / 72.0)
        except Exception:
            pass

        return scale

    def _apply_scaled_fonts(self):
        """根据 DPI 缩放全局字体与 ttk 控件尺寸。"""
        global FONT_TITLE, FONT_BOLD, FONT_NORMAL, FONT_MONO, FONT_MONO_SM

        FONT_TITLE = ("Segoe UI", self._s(21), "bold")
        FONT_BOLD = ("Segoe UI", self._s(16), "bold")
        FONT_NORMAL = ("Segoe UI", self._s(16))
        FONT_MONO = ("Consolas", self._s(15))
        FONT_MONO_SM = ("Consolas", self._s(14))




        self.option_add("*Font", FONT_NORMAL)

        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TCombobox", padding=self._s(5), fieldbackground=INPUT_BG)
        style.configure("Treeview", rowheight=self._s(30), font=FONT_MONO_SM)
        style.configure("Treeview.Heading", font=FONT_BOLD)

    # ════════════════════════════════════════════════════
    #  UI 构建方法
    # ════════════════════════════════════════════════════


    def _build_top_bar(self):
        """顶部导航栏：标题 + 状态徽章 + 运行时间 + 刷新按钮"""
        bar = tk.Frame(self, bg=BG_SECONDARY, height=self._s(58))
        bar.pack(fill="x")
        bar.pack_propagate(False)

        # 左侧：图标 + 标题
        tk.Label(
            bar, text="⚙  Trader_Server", font=FONT_TITLE,
            fg=TEXT_PRIMARY, bg=BG_SECONDARY, anchor="w",
        ).pack(side="left", padx=(self._s(20), 0))

        # 右侧：状态 + 时间 + 刷新
        right_frame = tk.Frame(bar, bg=BG_SECONDARY)
        right_frame.pack(side="right", padx=(0, self._s(20)))

        self.status_var = tk.StringVar(value="--")
        self.status_lbl = tk.Label(
            right_frame, textvariable=self.status_var,
            font=FONT_BOLD, fg=TEXT_MUTED, bg="#21262d",
            padx=self._s(12), pady=self._s(4), relief="flat",
        )
        self.status_lbl.pack(side="left", padx=(0, self._s(16)))

        self.uptime_var = tk.StringVar(value="--:--:--")
        tk.Label(right_frame, textvariable=self.uptime_var, font=FONT_MONO_SM,
                 fg=TEXT_MUTED, bg=BG_SECONDARY).pack(side="left")

        tk.Button(
            right_frame, text="↻", font=FONT_NORMAL,
            command=self._on_manual_refresh,
            width=3, relief="flat",
            bg=BG_CARD, fg=TEXT_PRIMARY, activebackground=BORDER_COLOR,
        ).pack(side="left", padx=(self._s(12), 0))


    def _build_main_area(self):
        """主区域：可拖拽左右分栏（4K 友好）"""
        main = tk.Frame(self, bg=BG_DARK)
        main.pack(fill="both", expand=True, padx=self._s(8), pady=self._s(8))

        paned = tk.PanedWindow(
            main, orient="horizontal", sashwidth=self._s(8),
            bg=BG_DARK, bd=0, relief="flat", showhandle=False,
        )
        paned.pack(fill="both", expand=True)

        # ── 左侧：注册面板 ──────────────────────────────
        left = tk.Frame(paned, bg=BG_SECONDARY, width=self._s(420))
        left.pack_propagate(False)
        self._build_register_panel(left)

        # ── 右侧：内容区 ────────────────────────────────
        right = tk.Frame(paned, bg=BG_DARK)
        self._build_content_panel(right)

        paned.add(left, minsize=self._s(360), stretch="never")
        paned.add(right, minsize=self._s(700), stretch="always")


    def _build_register_panel(self, parent):
        """左侧：注册表单 + 日志 + 凭证面板"""
        # 标题
        tk.Label(parent, text="节点注册",
                 font=FONT_BOLD, fg=TEXT_SECONDARY, bg=BG_SECONDARY,
                 anchor="w").pack(fill="x", padx=16, pady=(16, 12))

        # ── 注册表单 ───────────────────────────────────
        form = tk.Frame(parent, bg=BG_SECONDARY)
        form.pack(fill="x", padx=16)

        # SM 地址
        tk.Label(form, text="管理服务器地址 *", anchor="w",
                 font=FONT_NORMAL, fg=TEXT_SECONDARY, bg=BG_SECONDARY
                 ).pack(fill="x", pady=(0, 4))
        self.fm_mgr_url = self._make_entry(form, "http://127.0.0.1:8800")

        # 节点名
        tk.Label(form, text="节点名称 *", anchor="w",
                 font=FONT_NORMAL, fg=TEXT_SECONDARY, bg=BG_SECONDARY
                 ).pack(fill="x", pady=(8, 4))
        self.fm_node_name = self._make_entry(form, "trader-node-01")

        # 券商类型
        tk.Label(form, text="券商类型 *", anchor="w",
                 font=FONT_NORMAL, fg=TEXT_SECONDARY, bg=BG_SECONDARY
                 ).pack(fill="x", pady=(8, 4))
        self.fm_region = ttk.Combobox(
            form, values=["IB", "TT", "Test"],
            state="readonly", font=FONT_NORMAL, width=38,
        )
        self.fm_region.set("TT")
        self.fm_region.pack(fill="x", pady=(0, 4))

        # 主机地址（自动检测）
        tk.Label(form, text="主机地址", anchor="w",
                 font=FONT_NORMAL, fg=TEXT_SECONDARY, bg=BG_SECONDARY
                 ).pack(fill="x", pady=(8, 4))
        self.fm_host = self._make_entry(form, self._detect_host(), readonly=True)

        # 分隔线
        tk.Frame(parent, bg=BORDER_COLOR, height=1).pack(fill="x", padx=16, pady=14)

        # 注册按钮
        btn_frame = tk.Frame(parent, bg=BG_SECONDARY)
        btn_frame.pack(fill="x", padx=16)
        self.btn_register = tk.Button(
            btn_frame, text="提交注册",
            font=FONT_BOLD, cursor="hand2",
            command=self._do_register,
            bg=ACCENT_BLUE, fg="#fff", activebackground="#1f6feb",
            relief="flat", pady=8,
        )
        self.btn_register.pack(fill="x")

        # 进度条
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(
            parent, mode='indeterminate', length=300,
        )
        # 不 pack，需要时才显示

        # 分隔线
        tk.Frame(parent, bg=BORDER_COLOR, height=1).pack(fill="x", padx=16, pady=14)

        # ── 日志区域 ───────────────────────────────────
        tk.Label(parent, text="注册日志",
                 font=FONT_BOLD, fg=TEXT_SECONDARY, bg=BG_SECONDARY,
                 anchor="w").pack(fill="x", padx=16, pady=(0, 6))
        log_frame = tk.Frame(parent, bg=BG_DARK)
        log_frame.pack(fill="both", expand=True, padx=16, pady=(0, 12))
        self.log_text = tk.Text(
            log_frame, bg=BG_DARK, fg=TEXT_SECONDARY,
            font=FONT_MONO_SM, relief="flat", bd=0,
            wrap="word", height=10, padx=8, pady=6,
        )
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.config(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # 日志 tag 颜色
        self.log_text.tag_configure("ok", foreground=ACCENT_GREEN)
        self.log_text.tag_configure("err", foreground=ACCENT_RED)
        self.log_text.tag_configure("warn", foreground=ACCENT_YELLOW)
        self.log_text.tag_configure("info", foreground=TEXT_SECONDARY)

        # ── 凭证面板（默认隐藏）────────────────────────
        self.cred_frame = tk.Frame(parent, bg=BG_SECONDARY)
        self.cred_frame.pack(fill="x", padx=16, pady=(0, 16))

        tk.Label(self.cred_frame, text="凭证信息",
                 font=FONT_BOLD, fg=ACCENT_GREEN, bg=BG_SECONDARY,
                 anchor="w").pack(fill="x", pady=(0, 8))
        self.cred_info = {}
        for label in ["服务端ID", "令牌", "管理服务器地址", "状态"]:
            row = tk.Frame(self.cred_frame, bg=BG_CARD)
            row.pack(fill="x", pady=2)
            tk.Label(row, text=label, font=FONT_MONO_SM, fg=TEXT_SECONDARY,
                     bg=BG_CARD, anchor="w", width=14).pack(side="left", padx=6, pady=4)
            val_label = tk.Label(row, text="-", font=FONT_MONO_SM, fg=TEXT_PRIMARY,
                                 bg=BG_CARD, anchor="w")
            val_label.pack(side="left", fill="x", expand=True, padx=(0, 6), pady=4)
            self.cred_info[label] = val_label

        # 重新注册按钮
        tk.Button(
            self.cred_frame, text="重新注册（清除凭证）",
            font=FONT_NORMAL, cursor="hand2",
            command=self._do_reregister,
            bg=ACCENT_YELLOW, fg="#000", activebackground="#9a6700",
            relief="flat", pady=6,
        ).pack(pady=(10, 0))

        # 默认隐藏凭证面板
        self.cred_frame.pack_forget()

    def _build_content_panel(self, parent):
        """右侧：Tab 栏 + 仪表盘/运行日志"""
        # Tab 栏
        tab_bar = tk.Frame(parent, bg=BG_DARK)
        tab_bar.pack(fill="x", pady=(0, 10))

        tab_bg = tk.Frame(tab_bar, bg=BG_CARD, padx=2, pady=2)
        tab_bg.pack()
        self._tab_var = tk.StringVar(value="dashboard")

        for text, value in [("仪表盘", "dashboard"), ("运行日志", "logs")]:
            rb = tk.Radiobutton(
                tab_bg, text=text, variable=self._tab_var, value=value,
                font=FONT_NORMAL, fg=TEXT_SECONDARY, selectcolor=BG_CARD,
                activebackground=BG_CARD, activeforeground=TEXT_PRIMARY,
                bg=BG_CARD, relief="flat", indicatoron=False,
                command=self._on_tab_change,
                width=12, padx=14, pady=4,
            )
            rb.pack(side="left", padx=2)

        # 内容容器
        self.tab_container = tk.Frame(parent, bg=BG_DARK)
        self.tab_container.pack(fill="both", expand=True)

        # ── Dashboard Tab ───────────────────────────────
        self.dashboard_frame = tk.Frame(self.tab_container, bg=BG_DARK)
        self._build_dashboard_cards(self.dashboard_frame)

        # ── Logs Tab (替代原来的经济指标) ────────────────
        self.logs_frame = tk.Frame(self.tab_container, bg=BG_DARK)
        self._build_log_panel(self.logs_frame)

        # 默认显示 dashboard
        self.dashboard_frame.pack(fill="both", expand=True)

    def _build_dashboard_cards(self, parent):
        """仪表盘卡片网格（自适应布局）"""
        self.card_vars = {}
        card_data = [
            ("node_name", "节点名称", "-"),
            ("region", "券商类型", "-"),
            ("server_id", "服务端ID", "-"),
            ("heartbeat", "心跳状态", "--"),
            ("connections", "Client连接", "0"),
            ("broker", "券商状态", "-"),
        ]

        header = tk.Frame(parent, bg=BG_DARK)
        header.pack(fill="x", padx=self._s(10), pady=(self._s(8), self._s(2)))
        tk.Label(
            header,
            text="运行概览",
            font=FONT_BOLD,
            fg=TEXT_PRIMARY,
            bg=BG_DARK,
            anchor="w",
        ).pack(side="left")

        # 上部卡片：2 行自适应列数
        top_grid = tk.Frame(parent, bg=BG_DARK)
        top_grid.pack(fill="x", expand=False, padx=self._s(8), pady=(self._s(4), self._s(6)))

        cols = 4 if self._screen_w >= 3000 else 3
        for i, (key, label, default) in enumerate(card_data):
            row_i, col_i = divmod(i, cols)
            card = self._make_card(top_grid, label, default, key)
            card.grid(row=row_i, column=col_i, padx=self._s(6), pady=self._s(6), sticky="ew")
            top_grid.columnconfigure(col_i, weight=1)


        # 下部: 心跳统计子区域
        sub_frame = tk.Frame(parent, bg=BG_CARD, bd=1, relief="solid", highlightthickness=1, highlightbackground=BORDER_COLOR)
        sub_frame.pack(fill="x", padx=self._s(8), pady=self._s(8))

        tk.Label(sub_frame, text="⚡ 心跳与连接健康",
                 font=FONT_BOLD, fg=TEXT_PRIMARY, bg=BG_CARD,
                 anchor="w").pack(fill="x", padx=self._s(14), pady=(self._s(10), self._s(6)))

        hb_grid = tk.Frame(sub_frame, bg=BG_CARD)
        hb_grid.pack(fill="x", padx=self._s(14), pady=(0, self._s(10)))
        hb_items = [
            ("hb_total", "总次数", "-", "info"),
            ("hb_ok", "成功", "-", "ok"),
            ("hb_fail", "失败", "-", "err"),
            ("hb_interval", "间隔", "20s", "info"),
        ]
        for i, (key, label, default, cls) in enumerate(hb_items):
            c = self._make_small_stat(hb_grid, label, default, key, color_cls=cls)
            c.grid(row=0, column=i, padx=self._s(6), sticky="ew")
            hb_grid.columnconfigure(i, weight=1)


    def _make_small_stat(self, parent, label, default, key, color_cls=None):
        """小型统计项（用于底部统计栏）"""
        frame = tk.Frame(parent, bg=BG_CARD)
        lbl = tk.Label(frame, text=label.upper(),
                       font=("Segoe UI", self._s(11)), fg=TEXT_MUTED, bg=BG_CARD, anchor="w")

        lbl.pack(anchor="w", padx=self._s(10), pady=(self._s(8), self._s(2)))

        color_map = {
            "ok": ACCENT_GREEN, "err": ACCENT_RED,
            "info": ACCENT_BLUE, None: TEXT_PRIMARY,
        }
        fg = color_map.get(color_cls, TEXT_PRIMARY)
        val_var = tk.StringVar(value=default)
        self.card_vars[key] = val_var
        val_lbl = tk.Label(frame, textvariable=val_var,
                           font=("Segoe UI", self._s(15), "bold"), fg=fg, bg=BG_CARD)

        val_lbl.pack(anchor="w", padx=self._s(10), pady=(self._s(2), self._s(8)))
        self.card_vars[key + "_label"] = val_lbl
        return frame


    def _build_log_panel(self, parent):
        """运行日志面板 — 显示 Client 与 SE 之间的消息交互"""
        # 工具栏
        toolbar = tk.Frame(parent, bg=BG_SECONDARY)
        toolbar.pack(fill="x")

        tk.Label(toolbar, text="\u25CF 运行日志",
                 font=FONT_BOLD, fg=TEXT_PRIMARY, bg=BG_SECONDARY,
                 anchor="w").pack(side="left", padx=14, pady=10)

        # 统计徽章
        self.log_stats_var = tk.StringVar(value="- 条记录")
        tk.Label(toolbar, textvariable=self.log_stats_var,
                 font=FONT_MONO_SM, fg=TEXT_MUTED, bg=BG_SECONDARY,
                 ).pack(side="left", padx=8)

        # 刷新按钮
        refresh_btn = tk.Button(
            toolbar, text="刷新", font=FONT_NORMAL,
            command=self._load_logs, cursor="hand2",
            bg=ACCENT_BLUE, fg="#fff", activebackground="#1f6feb",
            relief="flat", padx=12, pady=4,
        )
        refresh_btn.pack(side="right", padx=(0, 14))

        clear_btn = tk.Button(
            toolbar, text="清空", font=FONT_NORMAL,
            command=self._clear_logs, cursor="hand2",
            bg=ACCENT_YELLOW, fg="#000", activebackground="#9a6700",
            relief="flat", padx=12, pady=4,
        )
        clear_btn.pack(side="right", padx=6)

        # 日志主体 — Treeview 表格
        cols = ("时间", "级别", "Session", "摘要")

        tree_wrap = tk.Frame(parent, bg=BG_DARK)
        tree_wrap.pack(fill="both", expand=True, padx=self._s(8), pady=self._s(8))

        style = ttk.Style(self)
        style.configure("LogTree.Treeview",
                        background=BG_CARD, foreground=TEXT_PRIMARY,
                        fieldbackground=BG_CARD, borderwidth=0,
                        font=FONT_MONO_SM, rowheight=self._s(30))
        style.configure("LogTree.Treeview.Heading",
                        background=BG_DARK, foreground=TEXT_SECONDARY,
                        font=FONT_BOLD, relief="flat")
        style.map("LogTree.Treeview", background=[("selected", BORDER_COLOR)])

        self.log_tree = ttk.Treeview(
            tree_wrap, columns=cols, show="headings", height=18, style="LogTree.Treeview"
        )
        self.log_tree.tag_configure("recv", foreground=ACCENT_BLUE)
        self.log_tree.tag_configure("send", foreground=ACCENT_GREEN)
        self.log_tree.tag_configure("conn", foreground=TEXT_SECONDARY)
        self.log_tree.tag_configure("err", foreground=ACCENT_RED)

        col_widths = {"时间": self._s(120), "级别": self._s(70), "Session": self._s(180), "摘要": self._s(860)}
        for col in cols:
            self.log_tree.heading(col, text=col)
            self.log_tree.column(col, anchor="w", width=col_widths.get(col, self._s(150)), minwidth=self._s(70))

        ysb = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.log_tree.yview)
        self.log_tree.configure(yscrollcommand=ysb.set)

        self.log_tree.pack(side="left", fill="both", expand=True)
        ysb.pack(side="right", fill="y")


        # 详情区域
        detail_frame = tk.Frame(parent, bg=BG_CARD, bd=1, relief="solid")
        detail_frame.pack(fill="x", padx=8, pady=(0, 8))
        tk.Label(detail_frame, text="消息详情",
                 font=FONT_NORMAL, fg=TEXT_MUTED, bg=BG_CARD,
                 anchor="w").pack(anchor="w", padx=10, pady=4)
        self.log_detail = tk.Text(
            detail_frame, bg=BG_DARK, fg=TEXT_SECONDARY,
            font=FONT_MONO_SM, relief="flat", bd=0,
            wrap="word", height=5, padx=10, pady=6,
            state="disabled",
        )
        self.log_detail.pack(fill="x")
        self.log_tree.bind("<<TreeviewSelect>>", self._on_log_select)

    # ════════════════════════════════════════════════════
    #  辅助 UI 方法
    # ════════════════════════════════════════════════════

    @staticmethod
    def _make_entry(parent, default="", readonly=False):
        """创建深色主题输入框"""
        e = tk.Entry(
            parent, font=FONT_NORMAL, relief="flat",
            bg=INPUT_BG if not readonly else BG_CARD,
            fg=TEXT_PRIMARY if not readonly else TEXT_MUTED,
            insertbackground=TEXT_PRIMARY,
        )
        e.insert(0, default)
        if readonly:
            e.configure(state="disabled")
        e.pack(fill="x", pady=(0, 6))
        return e

    @staticmethod
    def _detect_host() -> str:
        """自动检测本机 IP 地址"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return f"{ip}:8900"
        except Exception:
            return "127.0.0.1:8900"

    def _make_card(self, parent, label, default, key, color_cls=None):
        """创建状态卡片"""
        frame = tk.Frame(
            parent,
            bg=BG_CARD,
            relief="solid",
            bd=1,
            highlightthickness=1,
            highlightbackground=BORDER_COLOR,
            padx=self._s(1),
            pady=self._s(1),
        )

        lbl = tk.Label(frame, text=label.upper(), font=("Segoe UI", self._s(12)),
                       fg=TEXT_MUTED, bg=BG_CARD, anchor="w")

        lbl.pack(anchor="w", padx=self._s(10), pady=(self._s(7), self._s(1)))


        color = {
            "ok": ACCENT_GREEN, "err": ACCENT_RED,
            "info": ACCENT_BLUE, None: TEXT_PRIMARY,
        }.get(color_cls, TEXT_PRIMARY)
        val_var = tk.StringVar(value=default)
        self.card_vars[key] = val_var
        val_lbl = tk.Label(frame, textvariable=val_var, font=("Segoe UI", self._s(17), "bold"),
                           fg=color, bg=BG_CARD, anchor="w", justify="left")

        val_lbl.pack(anchor="w", padx=self._s(10), pady=(self._s(1), self._s(7)))

        self.card_vars[key + "_label"] = val_lbl

        return frame


    @staticmethod
    def _log(widget, msg, level="info"):
        """向日志文本框追加一条带颜色的时间戳消息"""
        if not widget:
            return
        ts = time.strftime("%H:%M:%S")
        widget.config(state="normal")
        widget.insert("end", f"[{ts}]  {msg}\n", level)
        widget.see("end")
        widget.config(state="disabled")

    def _lock_form(self, locked: bool):
        """锁定或解锁注册表单"""
        state = "disabled" if locked else "normal"
        self.fm_mgr_url.configure(state=state)
        self.fm_node_name.configure(state=state)
        self.fm_region.configure(state="readonly" if not locked else state)

        if locked:
            self.btn_register.configure(
                text="已注册（已锁定）",
                bg=TEXT_MUTED, fg=BG_DARK,
                state="disabled", cursor="",
            )
        else:
            self.btn_register.configure(
                text="提交注册",
                bg=ACCENT_BLUE, fg="#fff",
                state="normal", cursor="hand2",
            )

    def _toast(self, title, message, kind="info"):
        """弹出通知（用 messagebox 替代）"""
        icon_map = {"ok": "info", "err": "error", "warn": "warning", "info": "info"}
        if kind == "err":
            messagebox.showerror(title, message)
        elif kind == "warn":
            messagebox.showwarning(title, message)
        else:
            messagebox.showinfo(title, message)

    # ════════════════════════════════════════════════════
    #  Tab / 刷新逻辑
    # ════════════════════════════════════════════════════

    def _on_tab_change(self):
        tab = self._tab_var.get()
        self.dashboard_frame.pack_forget()
        self.logs_frame.pack_forget()
        if tab == "dashboard":
            self.dashboard_frame.pack(fill="both", expand=True)
        else:
            self.logs_frame.pack(fill="both", expand=True)
            self._load_logs()

    def _on_manual_refresh(self):
        """手动刷新按钮"""
        self._refresh_status()
        if self._tab_var.get() == "logs":
            self._load_logs()

    def _schedule_poll(self):
        """安排定时状态轮询"""
        if self._poll_job:
            self.after_cancel(self._poll_job)
        self._poll_job = self.after(8000, self._poll_tick)

    def _poll_tick(self):
        """轮询 tick"""
        self._refresh_status()
        self._schedule_poll()

    def _update_uptime_loop(self):
        """每秒更新运行时间"""
        elapsed = int(time.time() - self._uptime_start)
        h, r = divmod(elapsed, 3600)
        m, s = divmod(r, 60)
        self.uptime_var.set(f"{h}h {m}m {s}s")
        self.after(1000, self._update_uptime_loop)

    # ════════════════════════════════════════════════════
    #  数据加载
    # ════════════════════════════════════════════════════

    def _refresh_status(self):
        """获取并渲染节点状态"""
        data = self.api.get_status()
        if not data or not isinstance(data, dict):
            return

        reg = data.get("registration", {})
        hb = data.get("heartbeat", {})

        # ── 状态徽章 ───────────────────────────────────
        status_val = reg.get("status", "uninitialized")
        label = STATUS_LABELS.get(status_val, status_val.upper())
        color = STATUS_COLORS.get(status_val, TEXT_MUTED)
        self.status_var.set(label)
        self.status_lbl.configure(fg=color, bg="#21262d" if status_val != "running" else "#0d2818")

        # ── 卡片 ───────────────────────────────────────
        self.card_vars["node_name"].set(reg.get("node_name") or "-")
        self.card_vars["region"].set(reg.get("region") or "-")
        self.card_vars["server_id"].set(reg.get("server_id") or "-")
        self.card_vars["heartbeat"].set("OK" if hb.get("ok") else "--")
        self.card_vars["connections"].set(str(data.get("connections", 0)))
        # 券商状态
        broker_st = data.get("broker_status") or "-"
        self.card_vars["broker"].set(broker_st)
        broker_lbl = self.card_vars.get("broker_label")
        if broker_lbl:
            if "connected" in str(broker_st).lower():
                broker_lbl.configure(fg=ACCENT_GREEN)
            elif broker_st in ("-", "--"):
                broker_lbl.configure(fg=TEXT_MUTED)
            else:
                broker_lbl.configure(fg=ACCENT_YELLOW)

        # 心跳卡片颜色更新
        hb_ok = hb.get("ok", False)
        hb_lbl = self.card_vars.get("heartbeat_label")
        if hb_lbl:
            hb_lbl.configure(fg=ACCENT_GREEN if hb_ok else ACCENT_RED)
        self.card_vars["hb_total"].set(str(hb.get("total", 0)))
        self.card_vars["hb_ok"].set(str(hb.get("ok_count", hb.get("ok", 0))))
        self.card_vars["hb_fail"].set(str(hb.get("fail", 0)))
        interval_s = str(hb.get("interval", 20)) + "s"
        self.card_vars["hb_interval"].set(interval_s)

        # ── 注册状态判断 → 锁定/解锁 ──────────────────
        has_creds = bool(reg.get("has_credentials"))
        sid = reg.get("server_id", "")
        is_registered = bool(
            sid and has_creds and status_val in ("approved", "running", "online")
        )


        if is_registered and not self._registered:
            self._registered = True
            self._lock_form(True)
            # 显示凭证面板
            self.cred_info["服务端ID"].config(text=sid)
            self.cred_info["令牌"].config(text="(已保存)")
            self.cred_info["管理服务器地址"].config(text=reg.get("manager_url") or "-")
            st_color = ACCENT_GREEN
            st_text = "在线（心跳正常）"
            if not hb_ok:
                if status_val == "approved":
                    st_text = "已批准，等待心跳启动"
                    st_color = ACCENT_YELLOW
                else:
                    st_text = status_val or "未知"
                    st_color = TEXT_MUTED
            self.cred_info["状态"].config(text=st_text, fg=st_color)
            self.cred_frame.pack(fill="x", before=self.log_text.master)
            if self._register_in_progress:
                self._log(self.log_text, "*** 注册成功并已锁定 ***", "ok")
            else:
                self._log(self.log_text, "*** 检测到本地已保存凭证，表单已锁定（非本次新注册）***", "warn")

        elif not is_registered and self._registered:
            self._registered = False
            self._lock_form(False)
            self.cred_frame.pack_forget()

    def _load_logs(self):
        """加载消息日志到表格"""
        data = self.api.get_logs(150)
        if not data or not isinstance(data, dict) or not data.get("ok"):
            return

        # 清空旧数据
        for item in self.log_tree.get_children():
            self.log_tree.delete(item)

        logs = data.get("logs", [])
        stats = data.get("stats", {})
        total = stats.get("total", 0)
        errs = stats.get("errors", 0)
        conns = stats.get("connections", 0)
        recvs = stats.get("requests", 0)
        sends = stats.get("responses", 0)
        self.log_stats_var.set(
            f"{total} 条 | 连接:{conns} 请求:{recvs} 响应:{sends} 错误:{errs}"
        )

        if not logs:
            self.log_tree.insert("", "end", values=("-", "-", "-", "(暂无日志记录)"))
            return

        level_icon_map = {
            "recv": "\u2193",
            "send": "\u2191",
            "conn": "\u2699",
            "err": "\u2717",
            "info": "\u2139",
        }

        for entry in logs:
            ts = entry.get("timestamp", "-")
            lvl = entry.get("level", "info")
            sid = (entry.get("session_id") or "-")[:18]
            summary = entry.get("summary", "-")
            tag = lvl if lvl in ("recv", "send", "conn", "err") else ""
            icon = level_icon_map.get(lvl, "")
            self.log_tree.insert("", "end", values=(ts, icon + lvl[0].upper(), sid, summary), tags=(tag,) if tag else ())

    def _clear_logs(self):
        """清空服务端日志"""
        r = self.api.post("/api/logs/clear", {})
        if r and r.get("ok"):
            self._log(self.log_text, "日志已清空", "ok")
            self._load_logs()
        else:
            self._toast("操作失败", "无法清空日志", "err")

    def _on_log_select(self, event):
        """选中日志行时显示详情 JSON"""
        sel = self.log_tree.selection()
        if not sel:
            return
        # 需要从原始数据中找 detail — 这里简化处理，显示提示
        self.log_detail.config(state="normal")
        self.log_detail.delete("1.0", "end")
        item = self.log_tree.item(sel[0])
        vals = item.get("values", [])
        if len(vals) >= 4:
            self.log_detail.insert("1.0",
                f"时间: {vals[0]}\n"
                f"级别: {vals[1]}\n"
                f"Session: {vals[2]}\n"
                f"摘要: {vals[3]}"
            )
        self.log_detail.config(state="disabled")

    # ════════════════════════════════════════════════════
    #  注册流程
    # ════════════════════════════════════════════════════

    def _do_register(self):
        """执行三步注册流程（在后台线程中）"""
        if self._register_in_progress:
            self._toast("请稍候", "当前已有注册流程在进行中，请勿重复提交", "warn")
            return
        if self._registered:
            self._toast("已注册", "当前节点已完成注册，无需重复提交", "warn")
            self._lock_form(True)
            return


        mgr_url = self.fm_mgr_url.get().strip()
        node_name = self.fm_node_name.get().strip()
        region = self.fm_region.get()
        host = self.fm_host.get().strip()


        # 校验
        if not mgr_url:
            self._toast("错误", "请输入管理服务器地址", "err"); return
        if not node_name:
            self._toast("错误", "请输入节点名称", "err"); return
        if not region:
            self._toast("错误", "请选择区域", "err"); return

        payload = {"manager_url": mgr_url, "node_name": node_name,
                   "region": region, "host": host}

        self._current_manager_url = mgr_url
        self._sse_cancelled = False
        self._current_request_id = ""

        self._log(self.log_text, "======== 注册开始 ========" , "ok")

        self._log(self.log_text, f"[1/3] 参数: {json.dumps(payload)}", "info")

        # 锁定 UI（表单 + 按钮）
        self._lock_form(True)
        self.btn_register.configure(text="等待审批中...", bg=ACCENT_YELLOW, state="disabled")
        self.progress_bar.pack(fill="x", padx=16, pady=4)
        self.progress_bar.start(10)
        # 立即弹出等待层（先显示“提交中”）
        self._show_wait_modal("", "正在提交注册申请，请稍候...")

        # 在线程中运行注册（避免阻塞 UI）
        self._register_in_progress = True

        t = threading.Thread(target=lambda: self._register_thread(payload), daemon=True)
        t.start()


    def _register_thread(self, payload):
        """注册流程的后台线程"""
        mgr_url = payload["manager_url"]

        # Step 0: 本地 SE API 健康检查（避免误判为 SM 拒绝连接）
        self.after(0, lambda: self._log(self.log_text, "[0/3] 检查本地子节点服务...", "info"))
        local_result = self.api.ping_local()
        if local_result and local_result.get("ok"):
            self.after(0, lambda r=local_result: self._handle_local_ping(r))
        else:
            # 本地 API 不可用时，走直连 SM 兜底路径（避免无法注册）
            err_type = (local_result or {}).get("error_type", "") if isinstance(local_result, dict) else ""
            if err_type == "SE_LOCAL_UNREACHABLE":
                self.after(0, lambda: self._log(self.log_text, "[0/3] 本地服务不可达，自动切换直连 SM 注册", "warn"))
                self._register_thread_direct_fallback(payload)
                self.after(0, self._finish_register_ui)
                return
            self.after(0, lambda r=local_result: self._handle_local_ping(r))
            self.after(0, self._finish_register_ui)
            return


        # Step 1: Ping SM
        self.after(0, lambda: self._log(self.log_text, "[1/3] 测试管理端连通性...", "info"))
        ping_result = self.api.ping_sm(mgr_url)
        self.after(0, lambda r=ping_result: self._handle_ping(r))
        if not ping_result or not ping_result.get("ok"):
            self.after(0, self._finish_register_ui)
            return

        # Step 2: Submit
        self.after(0, lambda: self._log(self.log_text, "[2/3] 提交注册请求...", "info"))

        submit_result = self.api.submit_registration(payload)
        self.after(0, lambda r=submit_result: self._handle_submit(r))
        if not submit_result or not submit_result.get("ok"):
            self.after(0, self._finish_register_ui)
            return

        req_id = submit_result.get("request_id", "")
        self._current_request_id = req_id
        self.after(0, lambda: self._log(self.log_text, f"[2/3] 提交成功 request_id={req_id}", "ok"))

        # Step 3: SSE Wait
        self.after(0, lambda: self._log(self.log_text, "[3/3] 等待审批 (SSE)...", "warn"))
        self.after(0, lambda r=req_id: self._show_wait_modal(r))
        self._sse_cancelled = False
        for event in self.api.sse_await_approval(req_id):
            if self._sse_cancelled:
                break
            self.after(0, lambda e=event, r=req_id: self._handle_sse_event(e, r))
            approved_val = event.get("approved")
            if approved_val is True or approved_val is False or event.get("reason"):
                break



        # 完成：恢复 UI
        self.after(0, self._finish_register_ui)

    def _register_thread_direct_fallback(self, payload):
        """本地 API 不可用时，GUI 线程直连 SM 执行注册流程"""
        self.after(0, lambda: self._log(self.log_text, "[FALLBACK] 本地 API 不可用，切换直连 SM 注册", "warn"))
        try:
            try:
                from ..config import state
                from ..services.registration import test_connection, submit_registration, await_approval
            except Exception:
                from Server_economic.config import state
                from Server_economic.services.registration import test_connection, submit_registration, await_approval

            state.manager_url = payload.get("manager_url", "") or state.manager_url
            state.node_name = payload.get("node_name", "") or state.node_name
            state.region = payload.get("region", "") or state.region

            self.after(0, lambda: self._log(self.log_text, "[1/3] 测试管理端连通性...", "info"))
            ok, msg = test_connection()
            if ok:
                self.after(0, lambda: self._log(self.log_text, "[1/3] 成功 (fallback)", "ok"))
            else:
                self.after(0, lambda m=msg: self._handle_ping({"ok": False, "error": m}))
                return

            self.after(0, lambda: self._log(self.log_text, "[2/3] 提交注册请求...", "info"))
            result = submit_registration(
                node_name=payload.get("node_name"),
                region=payload.get("region"),
                host=payload.get("host"),
            )
            if not result:
                self.after(0, lambda: self._handle_submit({"ok": False, "error": "Registration submission failed"}))
                return

            req_id = result.get("request_id", "")
            self._current_request_id = req_id
            self.after(0, lambda r=req_id: self._log(self.log_text, f"[2/3] 提交成功 request_id={r}", "ok"))
            self.after(0, lambda: self._log(self.log_text, "[3/3] 等待审批 (SSE)...", "warn"))
            self.after(0, lambda r=req_id: self._show_wait_modal(r))

            event = await_approval(request_id=req_id, timeout=3600, shutdown_check=lambda: self._sse_cancelled)
            if self._sse_cancelled:
                return
            if not event:
                event = {"approved": False, "reason": "SSE等待失败或超时"}
            self.after(0, lambda e=event, r=req_id: self._handle_sse_event(e, r))


        except Exception as e:
            self.after(0, lambda err=str(e): self._log(self.log_text, f"[FALLBACK] 失败: {err}", "err"))


    def _show_wait_modal(self, request_id: str = "", title: str = "注册申请已提交，正在等待管理员审批"):
        """等待审批弹窗（模态+黑色透明遮罩，锁定主界面）。"""
        # 若弹窗已存在，直接更新内容，避免闪烁
        if self._wait_modal is not None and self._wait_modal.winfo_exists():
            if self._wait_title_label is not None:
                self._wait_title_label.config(text=title)
            if self._wait_req_label is not None:
                self._wait_req_label.config(
                    text=(f"request_id: {request_id}" if request_id else "request_id: 提交中...")
                )
            if self._wait_cancel_btn is not None:
                if request_id:
                    self._wait_cancel_btn.config(text="取消本次申请", state="normal")
                    self._wait_modal.protocol("WM_DELETE_WINDOW", self._cancel_current_registration)
                else:
                    self._wait_cancel_btn.config(text="提交中...", state="disabled")
                    self._wait_modal.protocol("WM_DELETE_WINDOW", lambda: None)
            self._wait_modal.lift()
            return


        self.update_idletasks()
        x = self.winfo_rootx()
        y = self.winfo_rooty()
        w = self.winfo_width()
        h = self.winfo_height()

        # 透明遮罩层
        overlay = tk.Toplevel(self)
        overlay.overrideredirect(True)
        overlay.configure(bg="#000")
        overlay.geometry(f"{max(w,1)}x{max(h,1)}+{x}+{y}")
        overlay.transient(self)
        try:
            overlay.attributes("-alpha", 0.35)
            overlay.attributes("-topmost", True)
        except Exception:
            pass
        self._wait_overlay = overlay

        # 中央弹窗
        top = tk.Toplevel(self)
        top.title("等待审批")
        top.configure(bg=BG_SECONDARY)
        top.resizable(False, False)
        top.transient(self)
        try:
            top.attributes("-topmost", True)
        except Exception:
            pass

        self._wait_title_label = tk.Label(
            top,
            text=title,
            bg=BG_SECONDARY,
            fg=TEXT_PRIMARY,
            font=FONT_BOLD,
            padx=16,
            pady=12,
        )
        self._wait_title_label.pack(fill="x")

        self._wait_req_label = tk.Label(
            top,
            text=(f"request_id: {request_id}" if request_id else "request_id: 提交中..."),
            bg=BG_SECONDARY,
            fg=TEXT_MUTED,
            font=FONT_MONO_SM,
            padx=16,
            pady=4,
        )
        self._wait_req_label.pack(fill="x")

        btn_row = tk.Frame(top, bg=BG_SECONDARY)
        btn_row.pack(fill="x", padx=16, pady=(8, 14))

        self._wait_cancel_btn = tk.Button(
            btn_row,
            text=("取消本次申请" if request_id else "提交中..."),
            command=self._cancel_current_registration,
            bg=ACCENT_YELLOW,
            fg="#000",
            relief="flat",
            padx=12,
            pady=6,
            cursor="hand2",
            state=("normal" if request_id else "disabled"),
        )
        self._wait_cancel_btn.pack(side="right")

        top.protocol("WM_DELETE_WINDOW", self._cancel_current_registration if request_id else (lambda: None))
        self._wait_modal = top

        self._cancel_in_progress = False

        top.update_idletasks()
        mw = top.winfo_width()
        mh = top.winfo_height()
        cx = x + (w - mw) // 2
        cy = y + (h - mh) // 2
        top.geometry(f"+{max(cx,0)}+{max(cy,0)}")

        try:
            top.grab_set()
            top.focus_force()
        except Exception:
            pass

    def _close_wait_modal(self):
        if self._wait_modal is not None:
            try:
                self._wait_modal.grab_release()
            except Exception:
                pass
            try:
                self._wait_modal.destroy()
            except Exception:
                pass
            self._wait_modal = None
        if self._wait_overlay is not None:
            try:
                self._wait_overlay.destroy()
            except Exception:
                pass
            self._wait_overlay = None
        self._wait_title_label = None
        self._wait_req_label = None
        self._wait_cancel_btn = None
        self._cancel_in_progress = False


    def _remember_abandoned_request(self, request_id: str):
        rid = (request_id or "").strip()
        if not rid:
            return
        now = time.time()
        self._abandoned_request_ids[rid] = now
        # 防止长期运行时集合无限增长（保留最近 200 条）
        if len(self._abandoned_request_ids) > 200:
            for old_rid in list(self._abandoned_request_ids.keys())[:80]:
                self._abandoned_request_ids.pop(old_rid, None)

    def _purge_abandoned_requests(self, keep_seconds: int = 86400):
        now = time.time()
        for rid, ts in list(self._abandoned_request_ids.items()):
            if now - ts > keep_seconds:
                self._abandoned_request_ids.pop(rid, None)

    def _cancel_request_on_sm(self, request_id: str) -> dict:

        """通知 SM 取消/废弃 request。优先走本地 API，不通时直连。"""
        r = self.api.cancel_registration(request_id, self._current_manager_url)
        if r and isinstance(r, dict) and r.get("ok"):
            return r

        try:
            try:
                from ..config import state
                from ..services.registration import cancel_registration_request
            except Exception:
                from Server_economic.config import state
                from Server_economic.services.registration import cancel_registration_request

            if self._current_manager_url:
                state.manager_url = self._current_manager_url
            return cancel_registration_request(
                request_id=request_id,
                reason="node_cancelled_by_user",
                force_discard_approved=True,
            )
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _discard_abandoned_approval(self, request_id: str):
        """晚到的 approved 结果：二次通知 SM 丢弃，避免建立管理状态。"""
        result = self._cancel_request_on_sm(request_id)
        if result.get("ok"):
            self._log(self.log_text, f"[OK] 已丢弃废弃申请: {request_id}", "warn")
            self._toast("已丢弃废弃申请", f"request_id={request_id}\n已通知 SM 丢弃该申请", "warn")
        else:
            err = result.get("error", "未知错误")
            self._log(self.log_text, f"[!] 丢弃废弃申请失败: {err}", "err")
        self._abandoned_request_ids.pop(request_id, None)

    def _cancel_current_registration_worker(self, request_id: str):
        result = self._cancel_request_on_sm(request_id)
        if result.get("ok"):
            self._remember_abandoned_request(request_id)
            self._sse_cancelled = True
            self.after(0, lambda: self._log(self.log_text, f"[CANCEL] 已废弃申请: {request_id}", "warn"))
            self.after(0, lambda: self._toast("已取消", "已通知 SM 废弃该申请，可重新发起注册", "ok"))
            self.after(0, self._close_wait_modal)
            self.after(0, self._finish_register_ui)
            return

        err = result.get("error", "未知错误")
        self.after(0, lambda e=err: self._log(self.log_text, f"[CANCEL] 取消失败，继续等待审批: {e}", "err"))
        self.after(0, lambda e=err: self._toast("取消失败", f"通知 SM 取消失败：{e}\n当前仍在等待审批", "err"))

        def _restore_cancel_btn():
            self._cancel_in_progress = False
            if self._wait_cancel_btn is not None:
                self._wait_cancel_btn.configure(text="取消本次申请", state="normal")

        self.after(0, _restore_cancel_btn)

    def _cancel_current_registration(self):
        """用户主动取消等待审批（异步执行，避免阻塞 UI）。"""
        rid = (self._current_request_id or "").strip()
        if not rid:
            self._close_wait_modal()
            self._finish_register_ui()
            return

        if self._cancel_in_progress:
            return

        if not messagebox.askyesno(
            "取消注册",
            "确定要取消当前注册申请吗？\n取消后将回到可重新注册状态。",
        ):
            return

        self._cancel_in_progress = True
        if self._wait_cancel_btn is not None:
            self._wait_cancel_btn.configure(text="取消中...", state="disabled")

        self._log(self.log_text, f"[CANCEL] 用户发起取消: {rid}", "warn")
        threading.Thread(
            target=lambda r=rid: self._cancel_current_registration_worker(r),
            daemon=True,
        ).start()

    def _handle_local_ping(self, result):

        """处理本地 SE API 健康检查结果"""

        if result and result.get("ok"):
            self._log(self.log_text, "[0/3] 本地服务可用", "ok")
        else:
            err = (
                result.get("error")
                if isinstance(result, dict) and result.get("error")
                else ("无响应" if not result else "本地服务响应异常")
            )
            self._log(self.log_text, f"[0/3] 失败: {err}", "err")
            self._toast("本地服务不可用", err, "err")
            self._reset_register_ui()


    def _handle_ping(self, result):
        """处理 SM Ping 结果"""
        if result and result.get("ok"):
            latency = result.get("latency", "?")
            self._log(self.log_text, f"[1/3] 成功 ({latency}ms)", "ok")
        else:
            err = result.get("error", "未知") if result else "无响应"
            self._log(self.log_text, f"[1/3] 失败: {err}", "err")
            self._toast("SM 连通性测试失败", err, "err")
            self._reset_register_ui()


    def _handle_submit(self, result):
        """处理 Submit 结果"""
        if result and result.get("ok"):
            req_id = result.get("request_id", "")
            self._log(self.log_text, f"[2/3] 提交成功, id={req_id}", "ok")
        else:
            err = result.get("error", "未知") if result else "无响应"
            self._log(self.log_text, f"[2/3] 失败: {err}", "err")
            self._toast("提交失败", err, "err")
            self._reset_register_ui()

    def _handle_sse_event(self, event, request_id: str = ""):
        """处理 SSE 事件"""
        rid = request_id or self._current_request_id

        # 该请求已被用户取消：若晚到 approved，则通知 SM 丢弃
        if rid and rid in self._abandoned_request_ids:
            if event.get("approved"):
                self._log(self.log_text, f"[!] 收到已废弃申请的通过结果，正在丢弃: {rid}", "warn")
                self._discard_abandoned_approval(rid)
            else:
                self._abandoned_request_ids.pop(rid, None)
            return


        if event.get("approved"):
            sid = event.get("server_id", "")
            self._log(self.log_text, f"*** 已批准! server_id={sid} ***", "ok")
            # 先关闭模态层并释放 grab，避免 messagebox 被遮挡导致“假死”
            self._close_wait_modal()
            # 标记为已注册并立即锁表单，避免竞态下被误解锁
            self._registered = True
            self._lock_form(True)
            self.cred_info["服务端ID"].config(text=sid or "-")
            self.cred_info["令牌"].config(text="(已保存)")
            self.cred_info["管理服务器地址"].config(text=self._current_manager_url or self.fm_mgr_url.get().strip() or "-")
            self.cred_info["状态"].config(text="已批准，正在建立管理连接", fg=ACCENT_YELLOW)
            self.cred_frame.pack(fill="x", before=self.log_text.master)
            self._toast("注册审批通过", "注册已获批准，正在建立管理连接与心跳", "ok")
            # 延后刷新，避免与弹窗叠加造成主线程卡顿
            self.after(50, self._refresh_status)
        elif event.get("approved") is False or event.get("reason"):
            reason = event.get("reason", "") or "管理员未通过审批"
            # 先关闭模态层并释放 grab，避免错误弹窗被遮挡
            self._close_wait_modal()
            # 区分：真正的拒绝 vs SSE 流错误
            if reason.startswith("SSE") or "stream error" in reason.lower() or "连接中断" in reason:
                self._log(self.log_text, f"[!] 连接中断: {reason}", "err")
                self._toast("连接中断", f"与服务器连接断开:\n{reason}", "err")
            else:
                self._log(self.log_text, f"[X] 注册被拒绝: {reason}", "err")
                self._toast("注册被拒绝", f"管理员拒绝了注册请求:\n{reason}", "err")
            # 拒绝/错误后标记为需要解锁，_finish_register_ui 会恢复表单
            self._registered = False




    def _reset_register_ui(self):
        """重置注册 UI（失败后恢复）"""
        self._register_in_progress = False
        self._close_wait_modal()
        self._purge_abandoned_requests()
        self._lock_form(False)
        self.btn_register.configure(text="提交注册", bg=ACCENT_BLUE, state="normal")
        self.progress_bar.stop()
        self.progress_bar.pack_forget()

    def _finish_register_ui(self):
        """注册流程结束后的 UI 清理"""
        self._register_in_progress = False
        self._close_wait_modal()
        self._purge_abandoned_requests()
        self.progress_bar.stop()
        self.progress_bar.pack_forget()
        self._current_request_id = ""
        if not self._registered:
            self._lock_form(False)
            self.btn_register.configure(text="提交注册", bg=ACCENT_BLUE, state="normal")




    # ── 重新注册 ─────────────────────────────────────────

    def _do_reregister(self):
        """重新注册：清除凭证并解锁表单"""
        if not messagebox.askyesno(
            "重新注册",
            "此操作将清除当前已保存的凭证。\n您需要重新提交注册并等待审批。\n\n是否继续？",
        ):
            return
        result = self.api.clear_credentials()
        if result and result.get("ok"):
            cleared = result.get("cleared", [])
            detail = ", ".join(cleared) if cleared else "无已保存文件"
            self._log(self.log_text, f"======== 凭证已清除 ({detail}) ========", "warn")
            self._toast("已清除", f"已删除文件: {detail}\n请重新填写表单并提交注册", "ok")
            self._registered = False
            self._lock_form(False)
            self.cred_frame.pack_forget()
            self.log_text.config(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.config(state="disabled")
            # 刷新状态，让顶部徽章回到"未初始化"
            self._refresh_status()
        else:
            err = result.get("error", "失败") if result else "无响应"
            self._log(self.log_text, f"[!] 清除凭证失败: {err}", "err")
            self._toast("操作失败", err, "err")

    # ── 关闭处理 ─────────────────────────────────────────

    def on_close(self):
        """窗口关闭时清理资源"""
        if self._register_in_progress:
            if not messagebox.askyesno("退出确认", "当前正在等待审批，确定要退出吗？"):
                return
            self._sse_cancelled = True
            rid = (self._current_request_id or "").strip()
            if rid:
                self._remember_abandoned_request(rid)
                threading.Thread(
                    target=lambda r=rid: self._cancel_request_on_sm(r),
                    daemon=True,
                ).start()

        self._close_wait_modal()
        self._sse_cancelled = True
        if self._poll_job:
            self.after_cancel(self._poll_job)
        self.destroy()




# ── 输入背景色常量（延迟定义，避免循环引用）─────────────
INPUT_BG = "#1c2030"


def main():
    app = SEControlPanel()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()


if __name__ == "__main__":
    main()
