"""
Positions Panel
持仓表格 + P&L汇总统计
支持多空分离、盈亏颜色编码
"""

import tkinter as tk
from tkinter import ttk


from ..constants import (
    PANEL_BG, BORDER, TEXT_PRIMARY, TEXT_MUTED,
    ACCENT_BLUE, ACCENT_GREEN, ACCENT_RED,
    FONT_MONO, FONT_MONO_SM, FONT_BOLD, FONT_UI_SM,
)


class PositionsPanel:
    """持仓面板组件"""

    def __init__(self, parent: tk.Widget, on_refresh_callback=None, on_select_callback=None):
        self.parent = parent
        self.on_refresh = on_refresh_callback
        self.on_row_click = on_select_callback
        self.frame: tk.Frame = None
        self.tree: ttk.Treeview = None
        # 汇总变量
        self.total_shares_var: tk.StringVar = None
        self.total_real_var: tk.StringVar = None
        self.total_unreal_var: tk.StringVar = None

    def build(self) -> tk.Frame:
        """构建持仓面板"""
        self.frame = tk.Frame(self.parent, bg=PANEL_BG)

        # Header
        hdr = tk.Frame(self.frame, bg=PANEL_BG)
        hdr.pack(fill="x", padx=6, pady=(6, 2))
        tk.Label(hdr, text="Positions & P&L", bg=PANEL_BG,
                 fg=TEXT_PRIMARY, font=FONT_BOLD).pack(side="left")

        refresh_btn = tk.Button(hdr, text="\u27f3", bg=PANEL_BG, fg=ACCENT_BLUE,
                                font=FONT_UI_SM, relief="flat", bd=0, padx=4, cursor="hand2",
                                command=self._on_refresh_clicked)
        refresh_btn.pack(side="right")

        # Totals row
        tot = tk.Frame(self.frame, bg=PANEL_BG)
        tot.pack(fill="x", padx=8, pady=(0, 4))

        self.total_shares_var = tk.StringVar(value="—")
        self.total_real_var = tk.StringVar(value="—")
        self.total_unreal_var = tk.StringVar(value="—")

        for lbl, attr in [("Today's Shares", "total_shares_var"),
                          ("Realized Today", "total_real_var"),
                          ("Unrealized P&L", "total_unreal_var")]:
            cell = tk.Frame(tot, bg=PANEL_BG)
            cell.pack(side="left", expand=True, fill="x", padx=6)
            tk.Label(cell, text=lbl, bg=PANEL_BG, fg=TEXT_MUTED,
                     font=("Segoe UI", 9, "bold")).pack(anchor="w")
            tk.Label(cell, textvariable=getattr(self, attr), bg=PANEL_BG,
                     fg=TEXT_PRIMARY, font=("Courier New", 13, "bold")).pack(anchor="w")

        # Treeview
        tree_frame = tk.Frame(self.frame, bg=PANEL_BG)
        tree_frame.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        self.tree = ttk.Treeview(
            tree_frame,
            columns=("sym", "qty_bot", "qty_sld", "pos", "posavgprc", "last", "unreal", "real", "exes"),
            show="headings", selectmode="browse",
        )

        col_defs = [
            ("sym", "Sym", 52, "w"),
            ("qty_bot", "Bot", 46, "e"),
            ("qty_sld", "Sld", 46, "e"),
            ("pos", "Pos", 46, "e"),
            ("posavgprc", "AvgPrc", 68, "e"),
            ("last", "Last", 68, "e"),
            ("unreal", "Unrealized", 82, "e"),
            ("real", "Realized", 82, "e"),
            ("exes", "Exes", 40, "e"),
        ]

        for cid, label, width, anchor in col_defs:
            self.tree.heading(cid, text=label)
            self.tree.column(cid, width=width, minwidth=28, anchor=anchor)

        self.tree.tag_configure("profit", foreground=ACCENT_GREEN)
        self.tree.tag_configure("loss", foreground=ACCENT_RED)
        self.tree.tag_configure("flat", foreground="#8090a0")

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)

        return self.frame

    def _on_refresh_clicked(self):
        if self.on_refresh:
            self.on_refresh()

    def update_data(self, positions: list[dict], current_quotes: dict,
                    on_row_click_fn=None):
        """
        刷新持仓数据

        Args:
            positions: 持仓数据列表 (来自TradingSession.get_today_activity)
            current_quotes: {symbol: quote_dict} 当前行情缓存
            on_row_click_fn: 行点击回调(symbol)
        """
        for r in self.tree.get_children():
            self.tree.delete(r)

        total_unreal = 0.0
        total_real = 0.0

        for p in positions:
            sym = p["symbol"]
            qty = int(p["qty"])
            dirn = p["direction"]
            is_long = dirn in ("Long", "L")
            avg = p["avg_open"]
            cpx = current_quotes.get(sym, {}).get("last", p.get("close_px", 0))
            real = p.get("realized_today", 0)

            if qty == 0:
                # 已平仓
                total_real += real
                self.tree.insert("", "end", iid=sym, tags=("flat",),
                                 values=(
                                     sym,
                                     int(p.get("qty_bot", 0)),
                                     int(p.get("qty_sld", 0)),
                                     0,
                                     f"{avg:.4f}" if avg else "\u2014",
                                     f"{cpx:.2f}" if cpx else "\u2014",
                                     "\u2014",
                                     f"+{real:.2f}" if real >= 0 else f"{real:.2f}",
                                     p.get("exes", 0),
                                 ))
            else:
                display_qty = qty if is_long else -qty
                unrealized = round((cpx - avg) * qty * (1 if is_long else -1), 2)
                qty_bot = p.get("qty_bot", qty if is_long else 0)
                qty_sld = p.get("qty_sld", qty if not is_long else 0)
                exes = p.get("exes", 0)
                tag = "profit" if unrealized > 0 else ("loss" if unrealized < 0 else "flat")

                self.tree.insert("", "end", iid=sym, tags=(tag,),
                                 values=(
                                     sym,
                                     int(qty_bot), int(qty_sld), display_qty,
                                     f"{avg:.4f}",
                                     f"{cpx:.2f}",
                                     f"+{unrealized:.2f}" if unrealized >= 0 else f"{unrealized:.2f}",
                                     f"+{real:.2f}" if real >= 0 else f"{real:.2f}",
                                     exes,
                                 ))
                total_unreal += unrealized
                total_real += real

        self.total_unreal_var.set(f"+${total_unreal:.2f}" if total_unreal >= 0 else f"-${abs(total_unreal):.2f}")
        self.total_real_var.set(f"+${total_real:.2f}" if total_real >= 0 else f"-${abs(total_real):.2f}")

        # 统计总股数
        total_shares = 0
        for r in self.tree.get_children():
            v = self.tree.item(r, "values")
            if v and len(v) >= 3:
                try:
                    total_shares += int(float(v[1] or 0)) + int(float(v[2] or 0))
                except Exception:
                    pass
        self.total_shares_var.set(f"{total_shares:,}")

        # 绑定行点击事件
        if on_row_click_fn:
            self.tree.bind("<<TreeviewSelect>>",
                           lambda _: self._handle_row_select(on_row_click_fn))

    def _handle_row_select(self, callback):
        sel = self.tree.selection()
        if sel:
            callback(sel[0])

    def live_update_pnl(self, current_quotes: dict):
        """用实时行情更新未实现盈亏（不重新请求服务器）"""
        tu = tr = 0.0
        for row in self.tree.get_children():
            v = self.tree.item(row, "values")
            if not v or len(v) < 9:
                continue
            sym, qty_bot, qty_sld, pos_s, avg_s, _, _, real_str, exes = v
            try:
                real = float(str(real_str).replace("+", ""))
            except Exception:
                real = 0.0

            if str(pos_s) in ("0", "-0", "0.0") or avg_s == "\u2014":
                tr += real
                continue

            cpx = current_quotes.get(sym, {}).get("last")
            if cpx is None:
                try:
                    cpx = float(v[5]) if v[5] not in ("\u2014", "", None) else None
                except Exception:
                    cpx = None
            if cpx is None:
                tr += real
                continue

            try:
                qty_signed = int(pos_s)
                abs_qty = abs(qty_signed)
                avg_f = float(avg_s)
                if qty_signed >= 0:
                    lu = round((cpx - avg_f) * abs_qty, 2)
                else:
                    lu = round((avg_f - cpx) * abs_qty, 2)
            except Exception:
                continue

            tu += lu
            tr += real
            tag = "profit" if lu > 0 else ("loss" if lu < 0 else "flat")
            self.tree.item(row, tags=(tag,),
                           values=(sym, qty_bot, qty_sld, pos_s, avg_s,
                                   f"{cpx:.2f}",
                                   f"+{lu:.2f}" if lu >= 0 else f"{lu:.2f}",
                                   real_str, exes))

        self.total_unreal_var.set(f"+${tu:.2f}" if tu >= 0 else f"-${abs(tu):.2f}")
        self.total_real_var.set(f"+${tr:.2f}" if tr >= 0 else f"-${abs(tr):.2f}")

        total_shares = 0
        for r in self.tree.get_children():
            v = self.tree.item(r, "values")
            if v and len(v) >= 3:
                try:
                    total_shares += int(float(v[1] or 0)) + int(float(v[2] or 0))
                except Exception:
                    pass
        self.total_shares_var.set(f"{total_shares:,}")
