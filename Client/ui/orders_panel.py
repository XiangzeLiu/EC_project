"""
Orders Panel
"""

import tkinter as tk
from tkinter import ttk

from ..constants import (
    PANEL_BG, TEXT_PRIMARY, TEXT_DIM, TEXT_MUTED,
    ACCENT_BLUE, ACCENT_GREEN, ACCENT_RED,
    BUTTON_NEUTRAL_BG, BUTTON_HOVER_BG, BUTTON_ACTIVE_BG,
    FONT_UI_SM,
    LIVE_STATUSES,
)


class OrdersPanel:
    def __init__(self, parent: tk.Widget, on_refresh_callback=None, on_cancel_callback=None):
        self.parent = parent
        self.on_refresh = on_refresh_callback
        self.on_cancel_order = on_cancel_callback
        self.frame: tk.Frame | None = None
        self.tree: ttk.Treeview | None = None
        self.mode_var: tk.StringVar | None = None
        self.count_var: tk.StringVar | None = None
        self._mode_tab_buttons: dict[str, tk.Button] = {}
        self._context_menu: tk.Menu | None = None
        self._refresh_btn: tk.Button | None = None
        self._enabled = True

    def build(self) -> tk.Frame:
        self.frame = tk.Frame(self.parent, bg=PANEL_BG)
        hdr = tk.Frame(self.frame, bg=PANEL_BG)
        hdr.pack(fill="x", padx=6, pady=(6, 2))

        self.mode_var = tk.StringVar(value="live")
        self._mode_tab_buttons = {}
        for label, mode in [("\u6d3b\u52a8", "live"), ("\u5168\u90e8", "all")]:
            selected = mode == "live"
            btn = tk.Button(
                hdr,
                text=label,
                bg=ACCENT_BLUE if selected else BUTTON_NEUTRAL_BG,
                fg=TEXT_PRIMARY,
                font=FONT_UI_SM,
                relief="flat",
                bd=0,
                padx=12,
                pady=4,
                cursor="hand2",
                activebackground=BUTTON_ACTIVE_BG,
                activeforeground=TEXT_PRIMARY,
                command=lambda m=mode: self.switch_mode(m),
            )
            btn.pack(side="left", padx=2)
            self._bind_button_hover(btn, ACCENT_BLUE if selected else BUTTON_NEUTRAL_BG)
            self._mode_tab_buttons[mode] = btn

        self.count_var = tk.StringVar(value="\u65e0\u8ba2\u5355")
        tk.Label(hdr, textvariable=self.count_var, bg=PANEL_BG, fg=TEXT_PRIMARY, font=FONT_UI_SM).pack(side="left", padx=8)

        self._refresh_btn = tk.Button(
            hdr,
            text="⟳",
            bg=BUTTON_NEUTRAL_BG,
            fg=ACCENT_BLUE,
            font=FONT_UI_SM,
            relief="flat",
            bd=0,
            padx=6,
            activebackground=BUTTON_ACTIVE_BG,
            activeforeground=ACCENT_BLUE,
            cursor="hand2",
            command=self._on_refresh_clicked,
        )
        self._bind_button_hover(self._refresh_btn, BUTTON_NEUTRAL_BG)
        self._refresh_btn.pack(side="right")

        tree_frame = tk.Frame(self.frame, bg=PANEL_BG)
        tree_frame.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.tree = ttk.Treeview(tree_frame, columns=("sym", "action", "qty", "price", "type", "tif", "status"), show="headings", selectmode="browse")
        for cid, label, width, anchor in [
            ("sym", "\u4ee3\u7801", 72, "w"),
            ("action", "\u65b9\u5411", 56, "c"),
            ("qty", "\u6570\u91cf", 56, "e"),
            ("price", "\u4ef7\u683c", 78, "e"),
            ("type", "\u7c7b\u578b", 58, "c"),
            ("tif", "\u6709\u6548\u671f", 48, "c"),
            ("status", "\u72b6\u6001", 100, "c"),
        ]:
            self.tree.heading(cid, text=label)
            self.tree.column(cid, width=width, minwidth=28, anchor=anchor)

        self.tree.tag_configure("buy", foreground=ACCENT_GREEN)
        self.tree.tag_configure("sell", foreground=ACCENT_RED)
        self.tree.tag_configure("inactive", foreground=TEXT_MUTED)

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)

        self._context_menu = tk.Menu(self.frame, tearoff=0, bg=PANEL_BG, fg=TEXT_PRIMARY, activebackground=ACCENT_RED, activeforeground=TEXT_PRIMARY, font=FONT_UI_SM)
        self._context_menu.add_command(label="\u64a4\u9500\u8ba2\u5355", command=self._cancel_selected)
        self.tree.bind("<Button-3>", self._on_right_click)
        return self.frame

    @property
    def current_mode(self) -> str:
        return self.mode_var.get()

    def set_enabled(self, enabled: bool):
        self._enabled = bool(enabled)
        btn_state = "normal" if enabled else "disabled"
        for mode, btn in self._mode_tab_buttons.items():
            selected = self.mode_var.get() == mode
            btn.configure(state=btn_state, bg=ACCENT_BLUE if selected else BUTTON_NEUTRAL_BG, fg=TEXT_PRIMARY if enabled else TEXT_MUTED)
        if self._refresh_btn:
            self._refresh_btn.config(state=btn_state)

    def switch_mode(self, mode: str):
        if not self._enabled:
            return
        self.mode_var.set(mode)
        for key, btn in self._mode_tab_buttons.items():
            selected = key == mode
            btn.configure(bg=ACCENT_BLUE if selected else BUTTON_NEUTRAL_BG, fg=TEXT_PRIMARY if selected else TEXT_DIM)
        if self.on_refresh:
            self.on_refresh()

    def update_data(self, orders: list[dict]):
        for row in self.tree.get_children():
            self.tree.delete(row)
        if not orders:
            self.count_var.set("\u65e0\u8ba2\u5355")
            return
        self.count_var.set(f"{len(orders)} \u7b14\u8ba2\u5355")
        mode = self.mode_var.get()
        for order in orders:
            raw_status = order.get("raw_status", order.get("status", ""))
            is_active = any(status in raw_status for status in LIVE_STATUSES)
            is_buy = order.get("action") == "BUY"
            tag = ("buy" if is_buy else "sell") if (mode == "live" or is_active) else "inactive"
            self.tree.insert("", "end", iid=order.get("id", ""), tags=(tag,), values=(order.get("symbol"), order.get("action"), order.get("qty"), order.get("price"), order.get("otype"), order.get("tif", "Day"), order.get("status")))

    def _on_refresh_clicked(self):
        if self._enabled and self.on_refresh:
            self.on_refresh()

    def _on_right_click(self, event):
        if not self._enabled:
            return
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.selection_set(row)
            if self.mode_var.get() == "live":
                self._context_menu.post(event.x_root, event.y_root)

    def _cancel_selected(self):
        if not self._enabled:
            return
        selection = self.tree.selection()
        if selection and self.on_cancel_order:
            self.on_cancel_order(selection[0])

    def get_selected_live_orders_for_symbol(self, symbol: str) -> list:
        result = []
        for row in self.tree.get_children():
            values = self.tree.item(row, "values")
            if values and len(values) >= 1 and values[0] == symbol:
                result.append(row)
        return result

    @staticmethod
    def _bind_button_hover(btn: tk.Button, normal_bg: str):
        btn.bind("<Enter>", lambda _e: btn.config(bg=BUTTON_HOVER_BG))
        btn.bind("<Leave>", lambda _e: btn.config(bg=normal_bg))
