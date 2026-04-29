"""
Log Area Component
底部日志显示区域，支持颜色编码
"""

import datetime

import tkinter as tk

from ..constants import PANEL_BG, TEXT_DIM, ACCENT_GREEN, ACCENT_RED, ACCENT_BLUE, FONT_MONO_SM


class LogArea:
    """日志区域组件"""

    def __init__(self, parent: tk.Widget):
        self.frame = tk.Frame(parent, bg=PANEL_BG)
        self._text_widget: tk.Text | None = None

    def build(self) -> tk.Frame:
        """构建并返回日志区域Frame"""
        self._text_widget = tk.Text(
            self.frame,
            bg=PANEL_BG, fg=TEXT_DIM,
            font=("Courier New", 11),
            relief="flat", bd=0,
            state="disabled", wrap="word",
            padx=10, pady=4,
        )
        self._text_widget.pack(fill="both", expand=True)

        # 配置tag样式
        self._text_widget.tag_configure("ok", foreground=ACCENT_GREEN)
        self._text_widget.tag_configure("err", foreground=ACCENT_RED)
        self._text_widget.tag_configure("inf", foreground=ACCENT_BLUE)
        return self.frame

    def log(self, msg: str, tag: str = "inf"):
        """写入一条日志"""
        if not self._text_widget:
            return
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self._text_widget.config(state="normal")
        self._text_widget.insert("end", f"[{ts}]  {msg}\n", tag)
        self._text_widget.see("end")
        self._text_widget.config(state="disabled")
