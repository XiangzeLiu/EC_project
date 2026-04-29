"""
Orders Panel
订单表格，支持 Live/All 模式切换、右键撤单
"""

import tkinter as tk
from tkinter import ttk


from ..constants import (
    PANEL_BG, BORDER, TEXT_PRIMARY, TEXT_DIM,
    ACCENT_BLUE, ACCENT_GREEN, ACCENT_RED,
    FONT_MONO_SM, FONT_BOLD, FONT_UI_SM,
    LIVE_STATUSES,
)


class OrdersPanel:
    """订单面板组件"""

    def __init__(self, parent: tk.Widget,
                 on_refresh_callback=None,
                 on_cancel_callback=None):
        self.parent = parent
        self.on_refresh = on_refresh_callback
        self.on_cancel_order = on_cancel_callback

        self.frame: tk.Frame = None
        self.tree: ttk.Treeview = None
        self.mode_var: tk.StringVar = None
        self.count_var: tk.StringVar = None
        self._mode_tab_buttons: dict[str, tk.Button] = {}
        self._context_menu: tk.Menu = None

    def build(self) -> tk.Frame:
        """构建订单面板"""
        self.frame = tk.Frame(self.parent, bg=PANEL_BG)

        # Header
        hdr = tk.Frame(self.frame, bg=PANEL_BG)
        hdr.pack(fill="x", padx=6, pady=(6, 2))

        self.mode_var = tk.StringVar(value="live")
        self._mode_tab_buttons = {}

        for lbl, mode in [("\u25cf Live", "live"), ("All", "all")]:
            btn = tk.Button(hdr, text=lbl,
                            bg=ACCENT_BLUE if mode == "live" else PANEL_BG,
                            fg="#0d0f14" if mode == "live" else TEXT_PRIMARY,
                            font=FONT_UI_SM, relief="flat", bd=0,
                            padx=12, pady=4, cursor="hand2",
                            command=lambda m=mode: self.switch_mode(m))
            btn.pack(side="left", padx=2)
            self._mode_tab_buttons[mode] = btn

        self.count_var = tk.StringVar(value="No orders")
        tk.Label(hdr, textvariable=self.count_var, bg=PANEL_BG,
                 fg=TEXT_PRIMARY, font=FONT_UI_SM).pack(side="left", padx=8)

        refresh_btn = tk.Button(hdr, text="\u27f3", bg=PANEL_BG, fg=ACCENT_BLUE,
                                font=FONT_UI_SM, relief="flat", bd=0, padx=4,
                                cursor="hand2", command=self._on_refresh_clicked)
        refresh_btn.pack(side="right")

        # Treeview area
        tree_frame = tk.Frame(self.frame, bg=PANEL_BG)
        tree_frame.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        self.tree = ttk.Treeview(tree_frame,
                                 columns=("sym", "action", "qty", "price", "type", "tif", "status"),
                                 show="headings", selectmode="browse")

        col_defs = [
            ("sym", "Symbol", 72, "w"),
            ("action", "Side", 56, "c"),
            ("qty", "Qty", 56, "e"),
            ("price", "Price", 78, "e"),
            ("type", "Type", 58, "c"),
            ("tif", "TIF", 48, "c"),
            ("status", "Status", 100, "c"),
        ]
        for cid, label, w, anc in col_defs:
            self.tree.heading(cid, text=label)
            self.tree.column(cid, width=w, minwidth=28, anchor=anc)

        self.tree.tag_configure("buy", foreground=ACCENT_GREEN)
        self.tree.tag_configure("sell", foreground=ACCENT_RED)
        self.tree.tag_configure("inactive", foreground="#5a6070")

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)

        # 右键菜单
        self._context_menu = tk.Menu(self.frame, tearoff=0,
                                      bg=PANEL_BG, fg=TEXT_PRIMARY,
                                      activebackground=ACCENT_RED,
                                      activeforeground="#0d0f14",
                                      font=FONT_UI_SM)
        self._context_menu.add_command(label="\u2715  Cancel Order",
                                       command=self._cancel_selected)
        self.tree.bind("<Button-3>", self._on_right_click)

        return self.frame

    @property
    def current_mode(self) -> str:
        return self.mode_var.get()

    def switch_mode(self, mode: str):
        """切换Live/All模式"""
        self.mode_var.set(mode)
        for m, btn in self._mode_tab_buttons.items():
            btn.configure(bg=ACCENT_BLUE if m == mode else PANEL_BG,
                          fg="#0d0f14" if m == mode else TEXT_DIM)
        if self.on_refresh:
            self.on_refresh()

    def update_data(self, orders: list[dict]):
        """刷新订单数据"""
        for r in self.tree.get_children():
            self.tree.delete(r)
        if not orders:
            self.count_var.set("No orders")
            return
        self.count_var.set(f"{len(orders)} order(s)")

        mode = self.mode_var.get()
        for o in orders:
            rs = o.get("raw_status", o.get("status", ""))
            is_active = any(s in rs for s in LIVE_STATUSES)
            is_buy = o.get("action") == "BUY"
            tag = ("buy" if is_buy else "sell") if (mode == "live" or is_active) else "inactive"
            tif = o.get("tif", "Day")
            self.tree.insert("", "end", iid=o.get("id", ""), tags=(tag,),
                             values=(
                                 o.get("symbol"), o.get("action"),
                                 o.get("qty"), o.get("price"),
                                 o.get("otype"), tif, o.get("status"),
                             ))

    def _on_refresh_clicked(self):
        if self.on_refresh:
            self.on_refresh()

    def _on_right_click(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.selection_set(row)
            if self.mode_var.get() == "live":
                self._context_menu.post(event.x_root, event.y_root)

    def _cancel_selected(self):
        sel = self.tree.selection()
        if sel and self.on_cancel_order:
            self.on_cancel_order(sel[0])

    def get_selected_live_orders_for_symbol(self, symbol: str) -> list:
        """获取指定symbol的所有活跃订单ID"""
        result = []
        for r in self.tree.get_children():
            v = self.tree.item(r, "values")
            if v and len(v) >= 1 and v[0] == symbol:
                result.append(r)  # iid = order id
        return result
