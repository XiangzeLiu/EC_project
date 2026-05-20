"""
Login Dialog
独立登录弹窗：验证凭据后关闭，主窗口才完全展示
"""

import tkinter as tk


from ..constants import (
    DARK_BG, PANEL_BG, PANEL_ALT_BG, BORDER, INPUT_BG,
    TEXT_PRIMARY, TEXT_DIM, TEXT_MUTED,
    ACCENT_BLUE, ACCENT_RED, FOCUS_RING,
    BUTTON_NEUTRAL_BG, BUTTON_HOVER_BG, BUTTON_ACTIVE_BG,
    FONT_UI_SM, FONT_BOLD, FONT_TITLE, FONT_MONO,
)



class LoginDialog(tk.Toplevel):
    """独立登录弹窗 — 内置验证逻辑，失败时显示错误并保持打开"""

    def __init__(self, parent: tk.Widget, auth_fn=None, default_user: str = "", default_pass: str = ""):
        """
        Args:
            parent: 父窗口
            auth_fn: 验证函数 (username, password) -> (success: bool, message: str)
                     若不提供则仅收集凭据不做验证
        """
        super().__init__(parent)
        self._auth_fn = auth_fn
        self._default_user = default_user
        self._default_pass = default_pass

        self.title("\u767b\u5f55")
        self.resizable(False, False)
        self.configure(bg=DARK_BG)

        # 结果回调
        self._result: tuple[str, str] | None = None

        # 遮挡父窗口
        self.transient(parent)
        self.grab_set()

        # 关闭按钮行为
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

        # 先构建 UI，再根据内容自适应尺寸并居中
        self._build_ui()
        self.update_idletasks()
        self._center_window()

        self.wait_window(self)

    def _center_window(self):
        """根据实际内容尺寸居中显示"""
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        w = self.winfo_reqwidth()
        h = self.winfo_reqheight()
        x = (sw - w) // 2
        y = (sh - h) // 2 - 50
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _build_ui(self):
        container = tk.Frame(self, bg=DARK_BG, padx=28, pady=24)
        container.pack(fill="both", expand=True)

        card = tk.Frame(
            container,
            bg=PANEL_BG,
            padx=26,
            pady=22,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        card.pack(fill="both", expand=True)

        # 标题
        tk.Label(
            card, text="SC",
            bg=PANEL_BG, fg="#4ea1ff", font=FONT_TITLE,
        ).pack(pady=(0, 8))

        tk.Label(
            card, text="\u4ea4\u6613\u7ec8\u7aef\u767b\u5f55",
            bg=PANEL_BG, fg=TEXT_DIM, font=FONT_BOLD,
        ).pack(pady=(0, 16))

        form = tk.Frame(card, bg=PANEL_BG)
        form.pack(fill="x")

        # Username
        tk.Label(
            form, text="\u7528\u6237\u540d", bg=PANEL_BG, fg=TEXT_DIM,
            font=FONT_UI_SM, anchor="w",
        ).pack(fill="x", pady=(0, 4))
        self.username_entry = tk.Entry(
            form, bg=INPUT_BG, fg=TEXT_PRIMARY,
            insertbackground=TEXT_PRIMARY, font=FONT_MONO,
            relief="flat", bd=0, highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=FOCUS_RING,
        )
        self.username_entry.pack(fill="x", ipady=8, pady=(0, 14))
        self.username_entry.bind("<FocusIn>", lambda e: self.username_entry.config(highlightbackground=FOCUS_RING))
        self.username_entry.bind("<FocusOut>", lambda e: self.username_entry.config(highlightbackground=BORDER))
        if self._default_user:
            self.username_entry.insert(0, self._default_user)

        # Password
        tk.Label(
            form, text="\u5bc6\u7801", bg=PANEL_BG, fg=TEXT_DIM,
            font=FONT_UI_SM, anchor="w",
        ).pack(fill="x", pady=(0, 4))
        self.password_entry = tk.Entry(
            form, bg=INPUT_BG, fg=TEXT_PRIMARY,
            insertbackground=TEXT_PRIMARY, font=FONT_MONO,
            relief="flat", bd=0, show="\u25cf", highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=FOCUS_RING,
        )
        self.password_entry.pack(fill="x", ipady=8, pady=(0, 20))
        self.password_entry.bind("<FocusIn>", lambda e: self.password_entry.config(highlightbackground=FOCUS_RING))
        self.password_entry.bind("<FocusOut>", lambda e: self.password_entry.config(highlightbackground=BORDER))
        if self._default_pass:
            self.password_entry.insert(0, self._default_pass)

        # 按钮
        btn_frame = tk.Frame(form, bg=PANEL_BG)
        btn_frame.pack(fill="x")
        self._btn_frame = btn_frame


        cancel_btn = tk.Button(
            btn_frame, text="\u53d6\u6d88",
            bg=BUTTON_NEUTRAL_BG, fg=TEXT_MUTED, font=FONT_UI_SM,
            relief="flat", bd=0, padx=18, pady=6,
            cursor="hand2", command=self._on_cancel,
            activebackground=BUTTON_ACTIVE_BG, activeforeground=TEXT_PRIMARY,
        )
        cancel_btn.pack(side="left")

        login_btn = tk.Button(
            btn_frame, text="\u767b\u5f55",
            bg=ACCENT_BLUE, fg=DARK_BG, font=FONT_BOLD,
            relief="flat", bd=0, padx=24, pady=6,
            cursor="hand2", command=self._on_login,
            activebackground=FOCUS_RING, activeforeground=DARK_BG,
        )
        login_btn.pack(side="right")

        self._bind_button_hover(cancel_btn, BUTTON_NEUTRAL_BG, BUTTON_HOVER_BG)
        self._bind_button_hover(login_btn, ACCENT_BLUE, FOCUS_RING)

        # 回车提交
        self.password_entry.bind("<Return>", lambda e: self._on_login())
        self.username_entry.bind("<Return>", lambda e: self.password_entry.focus_set())

        # 错误提示（初始隐藏）
        self.error_var = tk.StringVar(value="")
        self.error_lbl = tk.Label(
            card, textvariable=self.error_var,
            bg=PANEL_ALT_BG, fg=ACCENT_RED, font=FONT_UI_SM, wraplength=360,
            justify="left", padx=10, pady=8,
            highlightthickness=1, highlightbackground=BORDER,
        )
        # 占位但不可见（pack 后用 pack_forget 隐藏）
        self._error_packed = False

        # 初始焦点
        self.username_entry.select_range(0, "end")
        self.username_entry.focus_set()

    def _bind_button_hover(self, btn: tk.Button, normal_bg: str, hover_bg: str):
        btn.bind("<Enter>", lambda e: btn.config(bg=hover_bg))
        btn.bind("<Leave>", lambda e: btn.config(bg=normal_bg))


    def _show_error(self, message: str):
        """在表单下方显示错误信息"""
        self.error_var.set(message)
        if not self._error_packed:
            self.error_lbl.pack(fill="x", pady=(12, 0), before=self._get_btn_frame())
            self._error_packed = True
        self.update_idletasks()
        self._center_window()

    def _hide_error(self):
        """隐藏错误信息"""
        if self._error_packed:
            self.error_lbl.pack_forget()
            self._error_packed = False
            self.error_var.set("")

    def _get_btn_frame(self):
        """获取按钮容器的引用"""
        return getattr(self, "_btn_frame", None)


    def _on_login(self):
        username = self.username_entry.get().strip()
        password = self.password_entry.get().strip()
        if not username or not password:
            self._show_error("\u8bf7\u8f93\u5165\u7528\u6237\u540d\u548c\u5bc6\u7801")
            return

        # 如果提供了验证函数，先验证再关闭
        if self._auth_fn is not None:
            ok, msg = self._auth_fn(username, password)
            if not ok:
                self._show_error(f"\u767b\u5f55\u5931\u8d25\uff1a{msg}")
                return

        # 验证通过或无验证函数，正常关闭
        self._hide_error()
        self._result = (username, password)
        self.destroy()

    def _on_cancel(self):
        self._result = None
        self.destroy()

    @property
    def credentials(self) -> tuple[str, str] | None:
        return self._result
