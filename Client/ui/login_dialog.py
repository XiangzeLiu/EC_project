"""
Login Dialog
独立登录弹窗：验证凭据后关闭，主窗口才完全展示
"""

import tkinter as tk
from tkinter import messagebox, ttk

from ..constants import (
    DARK_BG, PANEL_BG, BORDER, INPUT_BG,
    TEXT_PRIMARY, TEXT_DIM, TEXT_MUTED,
    ACCENT_BLUE, ACCENT_GREEN, ACCENT_RED,
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
        container = tk.Frame(self, bg=DARK_BG, padx=50, pady=36)
        container.pack(fill="both", expand=True)

        # 标题
        tk.Label(
            container, text="\u25cf TRADING TERMINAL",
            bg=DARK_BG, fg=ACCENT_BLUE, font=FONT_TITLE,
        ).pack(pady=(0, 28))

        tk.Label(
            container, text="\u8bf7\u767b\u5f55\u4ee5\u7ee7\u7eed",
            bg=DARK_BG, fg=TEXT_DIM, font=FONT_UI_SM,
        ).pack(anchor="w", pady=(0, 18))

        form = tk.Frame(container, bg=DARK_BG)
        form.pack(fill="x")

        # Username
        tk.Label(
            form, text="\u7528\u6237\u540d", bg=DARK_BG, fg=TEXT_DIM,
            font=FONT_UI_SM, anchor="w",
        ).pack(fill="x", pady=(0, 4))
        self.username_entry = tk.Entry(
            form, bg=INPUT_BG, fg=TEXT_PRIMARY,
            insertbackground=TEXT_PRIMARY, font=FONT_MONO,
            relief="flat", bd=3,
        )
        self.username_entry.pack(fill="x", ipady=7, pady=(0, 16))
        if self._default_user:
            self.username_entry.insert(0, self._default_user)

        # Password
        tk.Label(
            form, text="\u5bc6\u7801", bg=DARK_BG, fg=TEXT_DIM,
            font=FONT_UI_SM, anchor="w",
        ).pack(fill="x", pady=(0, 4))
        self.password_entry = tk.Entry(
            form, bg=INPUT_BG, fg=TEXT_PRIMARY,
            insertbackground=TEXT_PRIMARY, font=FONT_MONO,
            relief="flat", bd=3, show="\u25cf",
        )
        self.password_entry.pack(fill="x", ipady=7, pady=(0, 22))
        if self._default_pass:
            self.password_entry.insert(0, self._default_pass)

        # 按钮
        btn_frame = tk.Frame(form, bg=DARK_BG)
        btn_frame.pack(fill="x")

        cancel_btn = tk.Button(
            btn_frame, text="\u53d6\u6d88",
            bg=BORDER, fg=TEXT_MUTED, font=("Segoe UI", 11),
            relief="flat", bd=0, padx=20, pady=6,
            cursor="hand2", command=self._on_cancel,
        )
        cancel_btn.pack(side="left")

        login_btn = tk.Button(
            btn_frame, text="\u767b\u5f55",
            bg=ACCENT_BLUE, fg=DARK_BG, font=("Segoe UI", 11, "bold"),
            relief="flat", bd=0, padx=26, pady=6,
            cursor="hand2", command=self._on_login,
        )
        login_btn.pack(side="right")

        # 回车提交
        self.password_entry.bind("<Return>", lambda e: self._on_login())
        self.username_entry.bind("<Return>", lambda e: self.password_entry.focus_set())

        # 错误提示（初始隐藏）
        self.error_var = tk.StringVar(value="")
        self.error_lbl = tk.Label(
            container, textvariable=self.error_var,
            bg=DARK_BG, fg=ACCENT_RED, font=FONT_UI_SM, wraplength=340,
            justify="left",
        )
        # 占位但不可见（pack 后用 pack_forget 隐藏）
        self._error_packed = False

        # 初始焦点
        self.username_entry.select_range(0, "end")
        self.username_entry.focus_set()

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
        for child in self.winfo_children():
            for sub in child.winfo_children():
                if isinstance(sub, tk.Frame) and any(isinstance(c, tk.Button) for c in sub.winfo_children()):
                    return sub
        return None

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
