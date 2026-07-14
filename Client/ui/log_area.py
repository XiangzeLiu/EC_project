"""
Log Area Component
Bottom log display with filtering, deduplication, and color tags.
"""

import datetime
import time

import tkinter as tk

from ..constants import PANEL_BG, BORDER, TEXT_DIM, ACCENT_GREEN, ACCENT_RED, ACCENT_BLUE, ACCENT_YELLOW, FONT_MONO_SM


class LogArea:
    """Bottom log view."""

    def __init__(
        self,
        parent: tk.Widget,
        filter_func=None,
        max_lines: int = 200,
        dedupe_window_seconds: float = 2.0,
    ):
        self.frame = tk.Frame(parent, bg=PANEL_BG)
        self._text_widget: tk.Text | None = None
        self._filter_func = filter_func
        self._max_lines = max(50, int(max_lines))
        self._dedupe_window_seconds = max(0.0, float(dedupe_window_seconds))
        self._last_message = ""
        self._last_tag = ""
        self._last_logged_at = 0.0

    def build(self) -> tk.Frame:
        """Build and return the log frame."""
        self._text_widget = tk.Text(
            self.frame,
            bg=PANEL_BG, fg=TEXT_DIM,
            font=FONT_MONO_SM,
            relief="flat", bd=0,
            state="disabled", wrap="word",
            padx=10, pady=6,
            highlightthickness=1, highlightbackground=BORDER,
        )
        vsb = tk.Scrollbar(
            self.frame,
            orient="vertical",
            command=self._text_widget.yview,
            bg=PANEL_BG,
            troughcolor=PANEL_BG,
            activebackground=BORDER,
            relief="flat",
        )
        self._text_widget.configure(yscrollcommand=vsb.set)

        vsb.pack(side="right", fill="y")
        self._text_widget.pack(fill="both", expand=True)

        self._text_widget.tag_configure("ok", foreground=ACCENT_GREEN)
        self._text_widget.tag_configure("err", foreground=ACCENT_RED)
        self._text_widget.tag_configure("inf", foreground=ACCENT_BLUE)
        self._text_widget.tag_configure("warn", foreground=ACCENT_YELLOW)
        return self.frame

    def log(self, msg: str, tag: str = "inf"):
        """Append one log line."""
        if not self._text_widget:
            return
        if self._filter_func and not self._filter_func(msg, tag):
            return
        if self._should_skip_duplicate(msg, tag):
            return
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self._text_widget.config(state="normal")
        self._text_widget.insert("end", f"[{ts}]  {msg}\n", tag)
        self._trim_lines()
        self._text_widget.see("end")
        self._text_widget.config(state="disabled")
        self._last_message = msg
        self._last_tag = tag
        self._last_logged_at = time.time()

    def _should_skip_duplicate(self, msg: str, tag: str) -> bool:
        if self._dedupe_window_seconds <= 0:
            return False
        if msg != self._last_message or tag != self._last_tag:
            return False
        if not self._should_dedupe(msg):
            return False
        return (time.time() - self._last_logged_at) < self._dedupe_window_seconds

    @staticmethod
    def _should_dedupe(msg: str) -> bool:
        return (
            msg.startswith("[")
            or msg.startswith("Position fetch failed:")
            or msg.startswith("\u6301\u4ed3\u83b7\u53d6\u5931\u8d25\uff1a")
            or msg == "Server disconnected"
            or msg == "\u7ba1\u7406\u670d\u52a1\u8fde\u63a5\u5df2\u65ad\u5f00"
        )

    def _trim_lines(self):
        if not self._text_widget:
            return
        line_count = int(self._text_widget.index("end-1c").split(".")[0])
        if line_count <= self._max_lines:
            return
        overflow = line_count - self._max_lines
        self._text_widget.delete("1.0", f"{overflow + 1}.0")
