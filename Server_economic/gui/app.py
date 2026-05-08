"""
Server_economic Desktop GUI — 子服务端控制面板（桌面版）

功能:
  - 节点注册流程（Ping → Submit → SSE Wait）
  - 实时状态仪表盘（心跳、连接数、版本）
  - 经济指标数据展示
  - 注册凭证管理 / 重新注册
  - 表单锁定机制

启动方式:
    python -m Server_economic.gui.app
    python Server_economic/gui/app.py
"""

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
    """Server_economic 控制面板 — 桌面版主窗口"""

    def __init__(self):
        super().__init__()
        self.title("Server_economic — 控制面板")
        self.configure(bg=BG_DARK)

        # ── 窗口尺寸与居中 ──────────────────────────────
        self.update_idletasks()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        w = min(1200, int(sw * 0.85))
        h = min(750, int(sh * 0.82))
        x = (sw - w) // 2
        y = (sh - h) // 2
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.minsize(1000, 600)

        # ── API 客户端 ─────────────────────────────────
        self.api = SEApiClient()

        # ── 全局状态 ───────────────────────────────────
        self._registered: bool = False
        self._poll_job: str | None = None
        self._uptime_start: float = time.time()
        self._sse_thread: threading.Thread | None = None
        self._sse_cancelled: bool = False

        # ── 构建 UI ────────────────────────────────────
        self._build_top_bar()
        self._build_main_area()

        # ── 启动轮询 ──────────────────────────────────
        self._refresh_status()
        self._schedule_poll()
        self._update_uptime_loop()

    # ════════════════════════════════════════════════════
    #  UI 构建方法
    # ════════════════════════════════════════════════════

    def _build_top_bar(self):
        """顶部导航栏：标题 + 状态徽章 + 运行时间 + 刷新按钮"""
        bar = tk.Frame(self, bg=BG_SECONDARY, height=48)
        bar.pack(fill="x")
        bar.pack_propagate(False)

        # 左侧：图标 + 标题
        tk.Label(
            bar, text="\u2699  Server_economic", font=FONT_TITLE,
            fg=TEXT_PRIMARY, bg=BG_SECONDARY, anchor="w",
        ).pack(side="left", padx=(20, 0))

        # 右侧：状态 + 时间 + 刷新
        right_frame = tk.Frame(bar, bg=BG_SECONDARY)
        right_frame.pack(side="right", padx=(0, 20))

        self.status_var = tk.StringVar(value="--")
        self.status_lbl = tk.Label(
            right_frame, textvariable=self.status_var,
            font=FONT_BOLD, fg=TEXT_MUTED, bg="#21262d",
            padx=12, pady=3, relief="flat",
        )
        self.status_lbl.pack(side="left", padx=(0, 16))

        self.uptime_var = tk.StringVar(value="--:--:--")
        tk.Label(right_frame, textvariable=self.uptime_var, font=FONT_MONO_SM,
                 fg=TEXT_MUTED, bg=BG_SECONDARY).pack(side="left")

        tk.Button(
            right_frame, text="\u21BB", font=FONT_NORMAL,
            command=self._on_manual_refresh,
            width=3, relief="flat",
            bg=BG_CARD, fg=TEXT_PRIMARY, activebackground=BORDER_COLOR,
        ).pack(side="left", padx=(10, 0))

    def _build_main_area(self):
        """主区域：左侧注册面板 + 右侧内容区"""
        main = tk.Frame(self, bg=BG_DARK)
        main.pack(fill="both", expand=True, padx=4, pady=4)

        # ── 左侧：注册面板 ──────────────────────────────
        left = tk.Frame(main, bg=BG_SECONDARY, width=360)
        left.pack(side="left", fill="y", padx=(0, 4))
        left.pack_propagate(False)
        self._build_register_panel(left)

        # ── 右侧：内容区 ────────────────────────────────
        right = tk.Frame(main, bg=BG_DARK)
        right.pack(side="left", fill="both", expand=True)
        self._build_content_panel(right)

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
        self.fm_node_name = self._make_entry(form, "economic-node-01")

        # 区域
        tk.Label(form, text="区域 *", anchor="w",
                 font=FONT_NORMAL, fg=TEXT_SECONDARY, bg=BG_SECONDARY
                 ).pack(fill="x", pady=(8, 4))
        self.fm_region = ttk.Combobox(
            form, values=["CN", "US", "EU", "APAC", "JP", "SG"],
            state="readonly", font=FONT_NORMAL, width=38,
        )
        self.fm_region.set("CN")
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
        """右侧：Tab 栏 + 仪表盘/经济指标"""
        # Tab 栏
        tab_bar = tk.Frame(parent, bg=BG_DARK)
        tab_bar.pack(fill="x", pady=(0, 10))

        tab_bg = tk.Frame(tab_bar, bg=BG_CARD, padx=2, pady=2)
        tab_bg.pack()
        self._tab_var = tk.StringVar(value="dashboard")

        for text, value in [("仪表盘", "dashboard"), ("经济指标", "indicators")]:
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

        # ── Indicators Tab ──────────────────────────────
        self.indicators_frame = tk.Frame(self.tab_container, bg=BG_DARK)
        self._build_indicators_table(self.indicators_frame)

        # 默认显示 dashboard
        self.dashboard_frame.pack(fill="both", expand=True)

    def _build_dashboard_cards(self, parent):
        """仪表盘卡片网格"""
        self.card_vars = {}
        card_data = [
            ("node_name",    "节点名称",  "-"),
            ("region",       "区域",      "-"),
            ("server_id",    "服务端ID", "-"),
            ("heartbeat",    "心跳状态",   "--"),
            ("connections",  "连接数",    "0"),
            ("version",      "版本",      "-"),
        ]

        grid = tk.Frame(parent, bg=BG_DARK)
        grid.pack(fill="both", expand=True)

        for i, (key, label, default) in enumerate(card_data):
            row_i, col_i = divmod(i, 3)
            card = self._make_card(grid, label, default, key)
            card.grid(row=row_i, column=col_i, padx=6, pady=6, sticky="nsew")
            grid.columnconfigure(col_i, weight=1)
            grid.rowconfigure(row_i, weight=1)

        # 心跳统计子区域
        tk.Label(parent, text="心跳统计",
                 font=FONT_BOLD, fg=TEXT_SECONDARY, bg=BG_DARK,
                 anchor="w").pack(fill="x", padx=6, pady=(20, 8))

        hb_grid = tk.Frame(parent, bg=BG_DARK)
        hb_grid.pack(fill="x", padx=6)
        hb_items = [
            ("hb_total",    "总次数",  "-", "info"),
            ("hb_ok",       "成功",    "-", "ok"),
            ("hb_fail",     "失败",    "-", "err"),
            ("hb_interval", "间隔",   "30s", "info"),
        ]
        for i, (key, label, default, cls) in enumerate(hb_items):
            c = self._make_card(hb_grid, label, default, key, color_cls=cls)
            c.grid(row=0, column=i, padx=4, sticky="ew")
            hb_grid.columnconfigure(i, weight=1)

    def _build_indicators_table(self, parent):
        """经济指标数据表"""
        columns = ("指标名称", "数值", "单位", "周期")
        self.indi_tree = ttk.Treeview(
            parent, columns=columns, show="headings", height=12,
        )
        style = ttk.Style()
        style.theme_use("default")
        style.configure("Treeview",
                        background=BG_CARD, foreground=TEXT_PRIMARY,
                        fieldbackground=BG_CARD, borderwidth=0,
                        font=FONT_MONO_SM,)
        style.configure("Treeview.Heading",
                        background=BG_DARK, foreground=TEXT_SECONDARY,
                        font=FONT_BOLD, relief="flat")
        style.map("Treeview", background=[("selected", BORDER_COLOR)])

        for col in columns:
            self.indi_tree.heading(col, text=col)
            self.indi_tree.column(col, anchor="center", width=160)

        self.indi_tree.pack(fill="both", expand=True, padx=4, pady=4)

        # 滚动条
        ysb = ttk.Scrollbar(parent, orient="vertical", command=self.indi_tree.yview)
        self.indi_tree.configure(yscrollcommand=ysb.set)

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
        frame = tk.Frame(parent, bg=BG_CARD, relief="solid", bd=1)

        lbl = tk.Label(frame, text=label.upper(), font=("Segoe UI", 9),
                       fg=TEXT_MUTED, bg=BG_CARD, anchor="w")
        lbl.pack(anchor="w", padx=12, pady=(10, 2))

        color = {
            "ok": ACCENT_GREEN, "err": ACCENT_RED,
            "info": ACCENT_BLUE, None: TEXT_PRIMARY,
        }.get(color_cls, TEXT_PRIMARY)
        val_var = tk.StringVar(value=default)
        self.card_vars[key] = val_var
        val_lbl = tk.Label(frame, textvariable=val_var, font=("Segoe UI", 18, "bold"),
                           fg=color, bg=BG_CARD)
        val_lbl.pack(anchor="w", padx=12, pady=(2, 10))
        # 存储标签引用以便后续动态改色
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
        self.indicators_frame.pack_forget()
        if tab == "dashboard":
            self.dashboard_frame.pack(fill="both", expand=True)
        else:
            self.indicators_frame.pack(fill="both", expand=True)
            self._load_indicators()

    def _on_manual_refresh(self):
        """手动刷新按钮"""
        self._refresh_status()
        if self._tab_var.get() == "indicators":
            self._load_indicators()

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
        self.card_vars["version"].set(data.get("version") or "-")

        # 心跳卡片颜色更新
        hb_ok = hb.get("ok", False)
        hb_lbl = self.card_vars.get("heartbeat_label")
        if hb_lbl:
            hb_lbl.configure(fg=ACCENT_GREEN if hb_ok else ACCENT_RED)
        self.card_vars["hb_total"].set(str(hb.get("total", 0)))
        self.card_vars["hb_ok"].set(str(hb.get("ok_count", hb.get("ok", 0))))
        self.card_vars["hb_fail"].set(str(hb.get("fail", 0)))
        interval_s = str(hb.get("interval", 30)) + "s"
        self.card_vars["hb_interval"].set(interval_s)

        # ── 注册状态判断 → 锁定/解锁 ──────────────────
        has_creds = bool(reg.get("has_credentials"))
        sid = reg.get("server_id", "")
        is_registered = bool(sid and status_val in (
            "approved", "running", "online"
        ) or (sid and has_creds))

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
            self._log(self.log_text, "*** 注册成功并已锁定 ***", "ok")
        elif not is_registered and self._registered:
            self._registered = False
            self._lock_form(False)
            self.cred_frame.pack_forget()

    def _load_indicators(self):
        """加载经济指标数据到表格"""
        data = self.api.get_economic_data()
        if not data or not isinstance(data, dict):
            return

        # 清空旧数据
        for item in self.indi_tree.get_children():
            self.indi_tree.delete(item)

        indi = data.get("data", {})
        if not indi:
            self.indi_tree.insert("", "end", values=("-", "--", "--", "--"))
            return

        for name, v in sorted(indi.items()):
            val = v.get("value", "-")
            unit = v.get("unit", "-")
            period = v.get("period", "-")
            self.indi_tree.insert("", "end", values=(name, val, unit, period))

    # ════════════════════════════════════════════════════
    #  注册流程
    # ════════════════════════════════════════════════════

    def _do_register(self):
        """执行三步注册流程（在后台线程中）"""
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

        self._log(self.log_text, "======== 注册开始 ========" , "ok")
        self._log(self.log_text, f"[1/3] 参数: {json.dumps(payload)}", "info")

        # 锁定 UI（表单 + 按钮）
        self._lock_form(True)
        self.btn_register.configure(text="等待审批中...", bg=ACCENT_YELLOW, state="disabled")
        self.progress_bar.pack(fill="x", padx=16, pady=4)
        self.progress_bar.start(10)

        # 在线程中运行注册（避免阻塞 UI）
        t = threading.Thread(target=lambda: self._register_thread(payload), daemon=True)
        t.start()

    def _register_thread(self, payload):
        """注册流程的后台线程"""
        mgr_url = payload["manager_url"]

        # Step 1: Ping
        self.after(0, lambda: self._log(self.log_text, "[1/3] 测试连通性...", "info"))
        ping_result = self.api.ping_sm(mgr_url)
        self.after(0, lambda r=ping_result: self._handle_ping(r))
        if not ping_result or not ping_result.get("ok"):
            return

        # Step 2: Submit
        self.after(0, lambda: self._log(self.log_text, "[2/3] 提交注册请求...", "info"))
        submit_result = self.api.submit_registration(payload)
        self.after(0, lambda r=submit_result: self._handle_submit(r))
        if not submit_result or not submit_result.get("ok"):
            return

        req_id = submit_result.get("request_id", "")
        self.after(0, lambda: self._log(self.log_text, f"[2/3] 提交成功 request_id={req_id}", "ok"))

        # Step 3: SSE Wait
        self.after(0, lambda: self._log(self.log_text, "[3/3] 等待审批 (SSE)...", "warn"))
        self._sse_cancelled = False
        for event in self.api.sse_await_approval(req_id):
            if self._sse_cancelled:
                break
            self.after(0, lambda e=event: self._handle_sse_event(e))
            if event.get("approved") is not False or event.get("reason"):
                break

        # 完成：恢复 UI
        self.after(0, self._finish_register_ui)

    def _handle_ping(self, result):
        """处理 Ping 结果"""
        if result and result.get("ok"):
            latency = result.get("latency", "?")
            self._log(self.log_text, f"[1/3] 成功 ({latency}ms)", "ok")
        else:
            err = result.get("error", "未知") if result else "无响应"
            self._log(self.log_text, f"[1/3] 失败: {err}", "err")
            self._toast("连接测试失败", err, "err")
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

    def _handle_sse_event(self, event):
        """处理 SSE 事件"""
        if event.get("approved"):
            sid = event.get("server_id", "")
            self._log(self.log_text, f"*** 已批准! server_id={sid} ***", "ok")
            self._toast("注册审批通过", "注册已获批准！", "ok")
            # 立即刷新状态 → 触发表单锁定
            self._refresh_status()
        elif event.get("reason"):
            reason = event.get("reason", "")
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
        self._lock_form(False)
        self.btn_register.configure(text="提交注册", bg=ACCENT_BLUE, state="normal")
        self.progress_bar.stop()
        self.progress_bar.pack_forget()

    def _finish_register_ui(self):
        """注册流程结束后的 UI 清理"""
        self.progress_bar.stop()
        self.progress_bar.pack_forget()
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
