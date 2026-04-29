"""
Trading Panel (Single Side)
单个交易面板：行情条 + 下单控件
支持面板激活高亮、快捷键、价格自动填充
"""

import tkinter as tk
from tkinter import ttk


from ..constants import (
    PANEL_BG, BORDER, INPUT_BG, DARK_BG, TEXT_PRIMARY, TEXT_DIM, TEXT_MUTED,
    ACCENT_BLUE, ACCENT_GREEN, ACCENT_RED,
    FONT_UI_SM, FONT_BOLD, FONT_TICKER, FONT_MONO, FONT_MONO_SM,
)


class TradingPanel:
    """单个交易面板（左或右）"""

    def __init__(self, parent: tk.Widget, panel_id: int,
                 on_symbol_enter_callback=None,
                 on_activate_callback=None,
                 on_order_type_change_callback=None):
        """
        Args:
            panel_id: 面板ID (0=左, 1=右)
            on_symbol_enter_callback: 输入股票代码回车回调(pid,)
            on_activate_callback: 点击激活面板回调(pid,)
            on_order_type_change_callback: 订单类型变更回调(pid,)
        """
        self.panel_id = panel_id
        self._on_symbol_enter = on_symbol_enter_callback
        self._on_activate = on_activate_callback
        self._on_order_type_change = on_order_type_change_callback

        # 控件引用
        self.frame: tk.Frame = None
        self.sym_var: tk.StringVar = None
        self.sym_entry: tk.Entry = None
        self.q_last_var: tk.StringVar = None
        self.q_bid_var: tk.StringVar = None
        self.q_ask_var: tk.StringVar = None
        self.q_chg_var: tk.StringVar = None
        self.q_vol_var: tk.StringVar = None
        self.order_type_var: tk.StringVar = None
        self.tif_var: tk.StringVar = None
        self.qty_entry: tk.Entry = None
        self.price_entry: tk.Entry = None
        self.price_lbl: tk.Label = None
        self.buy_btn: tk.Button = None
        self.sell_btn: tk.Button = None
        self.order_sym_var: tk.StringVar = None
        self.order_last_var: tk.StringVar = None
        # 运行时状态
        self.current_sym: str = None
        self.price_needs_fill: bool = True
        self._pending_action: str = None  # F2/F4 待下单动作

    def build(self, parent: tk.Widget) -> tk.Frame:
        """构建交易面板"""
        pf = tk.Frame(parent, bg=PANEL_BG,
                      highlightthickness=2, highlightbackground=BORDER)
        self.frame = pf
        # 点击任意位置激活面板
        pf.bind("<Button-1>", lambda e: self._on_activate(self.panel_id))

        # ── 行情行 ────────────────────────────────────────────────────────────
        row1 = tk.Frame(pf, bg=PANEL_BG, height=56)
        row1.pack(fill="x")
        row1.pack_propagate(False)

        tk.Label(row1, text=" Symbol", bg=PANEL_BG, fg=TEXT_DIM,
                 font=FONT_BOLD).pack(side="left", padx=(8, 2))

        self.sym_var = tk.StringVar()
        self.sym_entry = tk.Entry(
            row1, textvariable=self.sym_var,
            bg=INPUT_BG, fg=TEXT_PRIMARY,
            insertbackground=TEXT_PRIMARY,
            font=FONT_TICKER, width=5,
            relief="flat", bd=4,
        )
        self.sym_entry.pack(side="left", ipady=5)
        self.sym_entry.bind("<Return>", lambda e: self._on_symbol_enter(self.panel_id))
        self.sym_entry.bind("<FocusIn>", lambda e: self._on_activate(self.panel_id))

        # 行情数据标签
        self.q_last_var = tk.StringVar(value="—")
        self.q_bid_var = tk.StringVar(value="—")
        self.q_ask_var = tk.StringVar(value="—")
        self.q_chg_var = tk.StringVar(value="—")
        self.q_vol_var = tk.StringVar(value="—")

        for var_key, lbl, fg in [
            ("q_last_var", "LAST", TEXT_PRIMARY),
            ("q_bid_var", "BID", ACCENT_GREEN),
            ("q_ask_var", "ASK", ACCENT_RED),
        ]:
            cell = tk.Frame(row1, bg=PANEL_BG)
            cell.pack(side="left", padx=10)
            tk.Label(cell, text=lbl, bg=PANEL_BG, fg=TEXT_MUTED,
                     font=("Segoe UI", 10, "bold")).pack(side="left", padx=(0, 4))
            tk.Label(cell, textvariable=getattr(self, var_key), bg=PANEL_BG, fg=fg,
                     font=("Segoe UI", 18, "bold")).pack(side="left")

        # ── 下单行 ────────────────────────────────────────────────────────────
        tk.Frame(pf, bg=BORDER, height=1).pack(fill="x")
        mid = tk.Frame(pf, bg=PANEL_BG, height=52)
        mid.pack(fill="x")
        mid.pack_propagate(False)

        # Order Type
        tk.Label(mid, text="  Type", bg=PANEL_BG, fg=TEXT_DIM,
                 font=FONT_UI_SM).pack(side="left", padx=(8, 2))
        self.order_type_var = tk.StringVar(value="Limit")
        type_cb = ttk.Combobox(mid, textvariable=self.order_type_var,
                               values=["Limit", "Market"], state="readonly",
                               width=6, font=FONT_UI_SM)
        type_cb.pack(side="left", padx=(2, 8))
        type_cb.bind("<<ComboboxSelected>>", lambda e: self._on_order_type_change(self.panel_id))

        # TIF
        tk.Label(mid, text="TIF", bg=PANEL_BG, fg=TEXT_DIM,
                 font=FONT_UI_SM).pack(side="left")
        self.tif_var = tk.StringVar(value="Day")
        ttk.Combobox(mid, textvariable=self.tif_var,
                     values=["Day", "GTC", "IOC", "EXT", "GTC_EXT"],
                     state="readonly", width=7, font=FONT_UI_SM
                     ).pack(side="left", padx=(3, 8))

        # Qty (只允许整数)
        tk.Label(mid, text="Qty", bg=PANEL_BG, fg=TEXT_DIM,
                 font=FONT_UI_SM).pack(side="left")
        _qty_vcmd = (pf.register(lambda s: s == "" or s.isdigit()), "%P")
        self.qty_entry = tk.Entry(mid, bg=INPUT_BG, fg=TEXT_PRIMARY,
                                  insertbackground=TEXT_PRIMARY, font=FONT_MONO,
                                  relief="flat", bd=3, width=5,
                                  validate="key", validatecommand=_qty_vcmd)
        self.qty_entry.insert(0, "100")
        self.qty_entry.pack(side="left", ipady=5, padx=(3, 8))
        self.qty_entry.bind("<FocusIn>", lambda e: self._on_activate(self.panel_id))

        # Price (只允许数字和小数点)
        _price_vcmd = (pf.register(
            lambda s: s == "" or (all(c.isdigit() or c == "." for c in s)
                                  and s.count(".") <= 1)), "%P")
        self.price_lbl = tk.Label(mid, text="Price", bg=PANEL_BG,
                                  fg=TEXT_DIM, font=FONT_UI_SM)
        self.price_lbl.pack(side="left")
        self.price_entry = tk.Entry(mid, bg=INPUT_BG, fg=TEXT_PRIMARY,
                                    insertbackground=TEXT_PRIMARY, font=FONT_MONO,
                                    relief="flat", bd=3, width=7,
                                    validate="key", validatecommand=_price_vcmd)
        self.price_entry.pack(side="left", ipady=5, padx=(3, 10))
        self.price_entry.bind("<FocusIn>", lambda e: self._on_activate(self.panel_id))

        # Buy / Sell buttons
        self.buy_btn = tk.Button(mid, text="\u25b2 BUY", bg=ACCENT_GREEN, fg="#0d0f14",
                                 font=("Segoe UI", 12, "bold"), relief="flat", bd=0,
                                 padx=14, pady=4, cursor="hand2")
        self.buy_btn.pack(side="left", padx=(0, 4))
        self.sell_btn = tk.Button(mid, text="\u25bc SELL", bg=ACCENT_RED, fg="#0d0f14",
                                  font=("Segoe UI", 12, "bold"), relief="flat", bd=0,
                                  padx=14, pady=4, cursor="hand2")
        self.sell_btn.pack(side="left")

        # 内部状态变量
        self.order_sym_var = tk.StringVar(value="—")
        self.order_last_var = tk.StringVar(value="")
        self.current_sym = None
        self.price_needs_fill = True

        return pf

    def set_active(self, active: bool):
        """设置面板激活/取消激活状态（边框高亮）"""
        color = ACCENT_BLUE if active else BORDER
        if self.frame:
            self.frame.config(highlightbackground=color)

    def set_symbol(self, symbol: str):
        """设置股票代码并重置下单状态"""
        sym = symbol.strip().upper()
        if not sym:
            return
        self.sym_var.set(sym)
        self.current_sym = sym
        self.order_sym_var.set(sym)
        self.order_last_var.set("")
        self.price_entry.delete(0, "end")
        self.qty_entry.delete(0, "end")
        self.qty_entry.insert(0, "100")
        self.price_needs_fill = True
        self._pending_action = None

    def fill_price_from_quote(self, ask_price: float, is_market_mode: bool):
        """用行情ask价填充价格框"""
        if not self.price_needs_fill:
            return
        self.price_entry.config(state="normal")
        self.price_entry.delete(0, "end")
        self.price_entry.insert(0, f"{ask_price:.2f}")
        if is_market_mode:
            self.price_entry.config(state="disabled")
        self.price_needs_fill = False

    def update_quote_display(self, quote: dict, prev_quote: dict | None):
        """更新行情显示"""
        last_val = quote.get("last", 0)
        prev_last = prev_quote["last"] if prev_quote else last_val
        chg = round(last_val - prev_last, 2)

        self.q_last_var.set(f"{last_val:.2f}")
        self.q_bid_var.set(f"{quote.get('bid', 0):.2f}")
        self.q_ask_var.set(f"{quote.get('ask', 0):.2f}")
        self.q_chg_var.set(f"+{chg:.2f}" if chg >= 0 else f"{chg:.2f}")
        self.q_vol_var.set(f"{quote.get('volume', 0):,}")
        self.order_last_var.set(f"Last: ${last_val:.2f}")

    def toggle_market_mode(self, is_market: bool):
        """切换Market/Limit模式，控制price输入框状态"""
        self.price_entry.configure(
            state="disabled" if is_market else "normal",
            bg=DARK_BG if is_market else INPUT_BG,
        )
        self.price_lbl.configure(fg=TEXT_MUTED if is_market else TEXT_DIM)


# 移除不再需要的占位符

