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
    """独立登录弹窗"""

    def __init__(self, parent: tk.Widget):
        super().__init__(parent)

        self.title("Login")
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
            container, text="Sign in to continue",
            bg=DARK_BG, fg=TEXT_DIM, font=FONT_UI_SM,
        ).pack(anchor="w", pady=(0, 18))

        form = tk.Frame(container, bg=DARK_BG)
        form.pack(fill="x")

        # Username
        tk.Label(
            form, text="Username", bg=DARK_BG, fg=TEXT_DIM,
            font=FONT_UI_SM, anchor="w",
        ).pack(fill="x", pady=(0, 4))
        self.username_entry = tk.Entry(
            form, bg=INPUT_BG, fg=TEXT_PRIMARY,
            insertbackground=TEXT_PRIMARY, font=FONT_MONO,
            relief="flat", bd=3,
        )
        self.username_entry.pack(fill="x", ipady=7, pady=(0, 16))
        self.username_entry.insert(0, "test_name")

        # Password
        tk.Label(
            form, text="Password", bg=DARK_BG, fg=TEXT_DIM,
            font=FONT_UI_SM, anchor="w",
        ).pack(fill="x", pady=(0, 4))
        self.password_entry = tk.Entry(
            form, bg=INPUT_BG, fg=TEXT_PRIMARY,
            insertbackground=TEXT_PRIMARY, font=FONT_MONO,
            relief="flat", bd=3, show="\u25cf",
        )
        self.password_entry.pack(fill="x", ipady=7, pady=(0, 22))
        self.password_entry.insert(0, "test_password")

        # 按钮
        btn_frame = tk.Frame(form, bg=DARK_BG)
        btn_frame.pack(fill="x")

        cancel_btn = tk.Button(
            btn_frame, text="Cancel",
            bg=BORDER, fg=TEXT_MUTED, font=("Segoe UI", 11),
            relief="flat", bd=0, padx=20, pady=6,
            cursor="hand2", command=self._on_cancel,
        )
        cancel_btn.pack(side="left")

        login_btn = tk.Button(
            btn_frame, text="Login",
            bg=ACCENT_BLUE, fg=DARK_BG, font=("Segoe UI", 11, "bold"),
            relief="flat", bd=0, padx=26, pady=6,
            cursor="hand2", command=self._on_login,
        )
        login_btn.pack(side="right")

        # 回车提交
        self.password_entry.bind("<Return>", lambda e: self._on_login())
        self.username_entry.bind("<Return>", lambda e: self.password_entry.focus_set())

        # 初始焦点
        self.username_entry.select_range(0, "end")
        self.username_entry.focus_set()

    def _on_login(self):
        username = self.username_entry.get().strip()
        password = self.password_entry.get().strip()
        if not username or not password:
            messagebox.showwarning("Warning", "Please enter username and password", parent=self)
            return
        self._result = (username, password)
        self.destroy()

    def _on_cancel(self):
        self._result = None
        self.destroy()

    @property
    def credentials(self) -> tuple[str, str] | None:
        return self._result
