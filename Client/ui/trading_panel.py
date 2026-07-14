"""
Trading Panel (Single Side)
"""

import tkinter as tk
from tkinter import ttk

from ..constants import (
    PANEL_BG, BORDER, INPUT_BG, DARK_BG, TEXT_PRIMARY, TEXT_DIM, TEXT_MUTED,
    ACCENT_BLUE, ACCENT_GREEN, ACCENT_RED, FOCUS_RING,
    FONT_UI_SM, FONT_BOLD, FONT_TICKER, FONT_MONO, FONT_ACTION_BTN,
)


class TradingPanel:
    def __init__(self, parent: tk.Widget, panel_id: int,
                 on_symbol_enter_callback=None,
                 on_activate_callback=None,
                 on_order_type_change_callback=None):
        self.panel_id = panel_id
        self._on_symbol_enter = on_symbol_enter_callback
        self._on_activate = on_activate_callback
        self._on_order_type_change = on_order_type_change_callback

        self.frame: tk.Frame | None = None
        self.sym_var: tk.StringVar | None = None
        self.sym_entry: tk.Entry | None = None
        self.q_last_var: tk.StringVar | None = None
        self.q_bid_var: tk.StringVar | None = None
        self.q_ask_var: tk.StringVar | None = None
        self.q_chg_var: tk.StringVar | None = None
        self.q_vol_var: tk.StringVar | None = None
        self.order_type_var: tk.StringVar | None = None
        self.tif_var: tk.StringVar | None = None
        self.qty_entry: tk.Entry | None = None
        self.price_entry: tk.Entry | None = None
        self.price_lbl: tk.Label | None = None
        self.buy_btn: tk.Button | None = None
        self.sell_btn: tk.Button | None = None
        self.order_sym_var: tk.StringVar | None = None
        self.order_last_var: tk.StringVar | None = None
        self._order_type_combo: ttk.Combobox | None = None
        self._tif_combo: ttk.Combobox | None = None
        self.current_sym: str | None = None
        self.price_needs_fill: bool = True
        self._pending_action: str | None = None
        self._trade_enabled: bool = True

    def build(self, parent: tk.Widget) -> tk.Frame:
        pf = tk.Frame(parent, bg=PANEL_BG, highlightthickness=1, highlightbackground=BORDER)
        self.frame = pf
        pf.bind("<Button-1>", lambda _e: self._on_activate(self.panel_id))

        row1 = tk.Frame(pf, bg=PANEL_BG, height=60)
        row1.pack(fill="x")
        row1.pack_propagate(False)

        tk.Label(row1, text="\u4ee3\u7801", bg=PANEL_BG, fg=TEXT_DIM, font=FONT_BOLD).pack(side="left", padx=(8, 2))

        self.sym_var = tk.StringVar()
        self.sym_entry = tk.Entry(
            row1,
            textvariable=self.sym_var,
            bg=INPUT_BG,
            fg=TEXT_PRIMARY,
            insertbackground=TEXT_PRIMARY,
            font=FONT_TICKER,
            width=5,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=FOCUS_RING,
        )
        self.sym_entry.pack(side="left", ipady=5)
        self.sym_entry.bind("<Return>", lambda _e: self._on_symbol_enter(self.panel_id))
        self.sym_entry.bind("<FocusIn>", lambda _e: (self._on_activate(self.panel_id), self._on_entry_focus(self.sym_entry)))
        self.sym_entry.bind("<FocusOut>", lambda _e: self._on_entry_blur(self.sym_entry))

        self.q_last_var = tk.StringVar(value="—")
        self.q_bid_var = tk.StringVar(value="—")
        self.q_ask_var = tk.StringVar(value="—")
        self.q_chg_var = tk.StringVar(value="—")
        self.q_vol_var = tk.StringVar(value="—")

        for var_key, label, fg in [
            ("q_last_var", "\u6700\u65b0", TEXT_PRIMARY),
            ("q_bid_var", "\u4e70\u4ef7", ACCENT_GREEN),
            ("q_ask_var", "\u5356\u4ef7", ACCENT_RED),
        ]:
            cell = tk.Frame(row1, bg=PANEL_BG)
            cell.pack(side="left", padx=10)
            tk.Label(cell, text=label, bg=PANEL_BG, fg=TEXT_MUTED, font=FONT_BOLD).pack(side="left", padx=(0, 4))
            tk.Label(cell, textvariable=getattr(self, var_key), bg=PANEL_BG, fg=fg, font=FONT_TICKER).pack(side="left")

        tk.Frame(pf, bg=BORDER, height=1).pack(fill="x")
        mid = tk.Frame(pf, bg=PANEL_BG, height=52)
        mid.pack(fill="x")
        mid.pack_propagate(False)

        tk.Label(mid, text="\u7c7b\u578b", bg=PANEL_BG, fg=TEXT_DIM, font=FONT_UI_SM).pack(side="left", padx=(8, 2))
        self.order_type_var = tk.StringVar(value="Limit")
        self._order_type_combo = ttk.Combobox(
            mid,
            textvariable=self.order_type_var,
            values=["Limit", "Market"],
            state="readonly",
            width=6,
            font=FONT_UI_SM,
        )
        self._order_type_combo.pack(side="left", padx=(2, 8))
        self._order_type_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_order_type_change(self.panel_id))

        tk.Label(mid, text="\u6709\u6548\u671f", bg=PANEL_BG, fg=TEXT_DIM, font=FONT_UI_SM).pack(side="left")
        self.tif_var = tk.StringVar(value="Day")
        self._tif_combo = ttk.Combobox(
            mid,
            textvariable=self.tif_var,
            values=["Day", "GTC", "IOC", "EXT", "GTC_EXT"],
            state="readonly",
            width=7,
            font=FONT_UI_SM,
        )
        self._tif_combo.pack(side="left", padx=(3, 8))

        tk.Label(mid, text="\u6570\u91cf", bg=PANEL_BG, fg=TEXT_DIM, font=FONT_UI_SM).pack(side="left")
        qty_vcmd = (pf.register(lambda s: s == "" or s.isdigit()), "%P")
        self.qty_entry = tk.Entry(
            mid,
            bg=INPUT_BG,
            fg=TEXT_PRIMARY,
            insertbackground=TEXT_PRIMARY,
            font=FONT_MONO,
            relief="flat",
            bd=0,
            width=5,
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=FOCUS_RING,
            validate="key",
            validatecommand=qty_vcmd,
        )
        self.qty_entry.insert(0, "100")
        self.qty_entry.pack(side="left", ipady=5, padx=(3, 8))
        self.qty_entry.bind("<FocusIn>", lambda _e: (self._on_activate(self.panel_id), self._on_entry_focus(self.qty_entry)))
        self.qty_entry.bind("<FocusOut>", lambda _e: self._on_entry_blur(self.qty_entry))

        price_vcmd = (pf.register(lambda s: s == "" or (all(c.isdigit() or c == "." for c in s) and s.count(".") <= 1)), "%P")
        self.price_lbl = tk.Label(mid, text="\u4ef7\u683c", bg=PANEL_BG, fg=TEXT_DIM, font=FONT_UI_SM)
        self.price_lbl.pack(side="left")
        self.price_entry = tk.Entry(
            mid,
            bg=INPUT_BG,
            fg=TEXT_PRIMARY,
            insertbackground=TEXT_PRIMARY,
            font=FONT_MONO,
            relief="flat",
            bd=0,
            width=7,
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=FOCUS_RING,
            validate="key",
            validatecommand=price_vcmd,
        )
        self.price_entry.pack(side="left", ipady=5, padx=(3, 10))
        self.price_entry.bind("<FocusIn>", lambda _e: (self._on_activate(self.panel_id), self._on_entry_focus(self.price_entry)))
        self.price_entry.bind("<FocusOut>", lambda _e: self._on_entry_blur(self.price_entry))

        self.buy_btn = tk.Button(mid, text="\u4e70\u5165", bg=ACCENT_GREEN, fg=TEXT_PRIMARY, font=FONT_ACTION_BTN, relief="flat", bd=0, activebackground=ACCENT_GREEN, activeforeground=TEXT_PRIMARY, padx=14, pady=4, cursor="hand2")
        self.buy_btn.pack(side="left", padx=(0, 4))
        self.sell_btn = tk.Button(mid, text="\u5356\u51fa", bg=ACCENT_RED, fg=TEXT_PRIMARY, font=FONT_ACTION_BTN, relief="flat", bd=0, activebackground=ACCENT_RED, activeforeground=TEXT_PRIMARY, padx=14, pady=4, cursor="hand2")
        self.sell_btn.pack(side="left")
        self._bind_button_hover(self.buy_btn, ACCENT_GREEN, "#2d9a6a")
        self._bind_button_hover(self.sell_btn, ACCENT_RED, "#d95a5a")

        self.order_sym_var = tk.StringVar(value="—")
        self.order_last_var = tk.StringVar(value="")
        self.current_sym = None
        self.price_needs_fill = True
        self._trade_enabled = True
        return pf

    def set_active(self, active: bool):
        color = ACCENT_BLUE if active else BORDER
        if self.frame:
            self.frame.config(highlightbackground=color)

    def set_symbol(self, symbol: str):
        sym = symbol.strip().upper()
        if not sym:
            return
        self.sym_var.set(sym)
        self.current_sym = sym
        self.order_sym_var.set(sym)
        self.order_last_var.set("")
        self.price_entry.config(state="normal")
        self.price_entry.delete(0, "end")
        self.qty_entry.delete(0, "end")
        self.qty_entry.insert(0, "100")
        self.price_needs_fill = True
        self._pending_action = None
        if not self._trade_enabled:
            self.set_trade_enabled(False)
        elif self.order_type_var.get() == "Market":
            self.price_entry.config(state="disabled")

    def set_trade_enabled(self, enabled: bool):
        self._trade_enabled = bool(enabled)
        entry_state = "normal" if enabled else "disabled"
        combo_state = "readonly" if enabled else "disabled"
        if self.sym_entry:
            self.sym_entry.config(state=entry_state)
        if self.qty_entry:
            self.qty_entry.config(state=entry_state)
        if self._order_type_combo:
            self._order_type_combo.config(state=combo_state)
        if self._tif_combo:
            self._tif_combo.config(state=combo_state)
        if self.buy_btn:
            self.buy_btn.config(state="normal" if enabled else "disabled")
        if self.sell_btn:
            self.sell_btn.config(state="normal" if enabled else "disabled")
        if self.price_entry:
            if enabled:
                is_market = bool(self.order_type_var and self.order_type_var.get() == "Market")
                self.price_entry.config(state="disabled" if is_market else "normal", bg=DARK_BG if is_market else INPUT_BG)
            else:
                self.price_entry.config(state="disabled", bg=DARK_BG)
        if self.price_lbl:
            self.price_lbl.configure(fg=TEXT_DIM if enabled else TEXT_MUTED)

    def fill_price_from_quote(self, ask_price: float, is_market_mode: bool):
        if not self.price_needs_fill or not self._trade_enabled:
            return
        self.price_entry.config(state="normal")
        self.price_entry.delete(0, "end")
        self.price_entry.insert(0, f"{ask_price:.2f}")
        if is_market_mode:
            self.price_entry.config(state="disabled")
        self.price_needs_fill = False

    def update_quote_display(self, quote: dict, prev_quote: dict | None):
        last_val = quote.get("last", 0)
        prev_last = prev_quote["last"] if prev_quote else last_val
        chg = round(last_val - prev_last, 2)
        self.q_last_var.set(f"{last_val:.2f}")
        self.q_bid_var.set(f"{quote.get('bid', 0):.2f}")
        self.q_ask_var.set(f"{quote.get('ask', 0):.2f}")
        self.q_chg_var.set(f"+{chg:.2f}" if chg >= 0 else f"{chg:.2f}")
        self.q_vol_var.set(f"{quote.get('volume', 0):,}")
        self.order_last_var.set("Last: $" + f"{last_val:.2f}")

    def toggle_market_mode(self, is_market: bool):
        if not self._trade_enabled:
            self.price_entry.configure(state="disabled", bg=DARK_BG)
            self.price_lbl.configure(fg=TEXT_MUTED)
            return
        self.price_entry.configure(state="disabled" if is_market else "normal", bg=DARK_BG if is_market else INPUT_BG)
        self.price_lbl.configure(fg=TEXT_MUTED if is_market else TEXT_DIM)

    @staticmethod
    def _bind_button_hover(btn: tk.Button, normal_bg: str, hover_bg: str):
        btn.bind("<Enter>", lambda _e: btn.config(bg=hover_bg))
        btn.bind("<Leave>", lambda _e: btn.config(bg=normal_bg))

    @staticmethod
    def _on_entry_focus(entry: tk.Entry):
        entry.config(highlightbackground=FOCUS_RING)

    @staticmethod
    def _on_entry_blur(entry: tk.Entry):
        entry.config(highlightbackground=BORDER)
