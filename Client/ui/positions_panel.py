"""
Positions Panel
"""

import tkinter as tk
from tkinter import ttk

from ..constants import (
    PANEL_BG, PANEL_ALT_BG, BORDER, TEXT_PRIMARY, TEXT_DIM, TEXT_MUTED,
    ACCENT_GREEN, ACCENT_RED,
    BUTTON_NEUTRAL_BG, BUTTON_HOVER_BG, BUTTON_ACTIVE_BG,
    FONT_MONO, FONT_BOLD, FONT_UI_SM,
)


class PositionsPanel:
    def __init__(self, parent: tk.Widget, on_refresh_callback=None, on_select_callback=None):
        self.parent = parent
        self.on_refresh = on_refresh_callback
        self.on_row_click = on_select_callback
        self.frame: tk.Frame | None = None
        self.tree: ttk.Treeview | None = None
        self.total_shares_var: tk.StringVar | None = None
        self.total_real_var: tk.StringVar | None = None
        self.total_unreal_var: tk.StringVar | None = None
        self._refresh_btn: tk.Button | None = None
        self._enabled = True

    def build(self) -> tk.Frame:
        self.frame = tk.Frame(self.parent, bg=PANEL_BG)
        hdr = tk.Frame(self.frame, bg=PANEL_BG)
        hdr.pack(fill="x", padx=6, pady=(6, 2))
        tk.Label(hdr, text="\u6301\u4ed3\u4e0e\u76c8\u4e8f", bg=PANEL_BG, fg=TEXT_PRIMARY, font=FONT_BOLD).pack(side="left")

        self._refresh_btn = tk.Button(hdr, text="⟳", bg=BUTTON_NEUTRAL_BG, fg="#b7bcc6", font=FONT_UI_SM, relief="flat", bd=0, padx=6, cursor="hand2", activebackground=BUTTON_ACTIVE_BG, activeforeground="#b7bcc6", command=self._on_refresh_clicked)
        self._refresh_btn.pack(side="right")
        self._bind_button_hover(self._refresh_btn, BUTTON_NEUTRAL_BG)

        totals = tk.Frame(self.frame, bg=PANEL_BG)
        totals.pack(fill="x", padx=8, pady=(0, 4))
        self.total_shares_var = tk.StringVar(value="—")
        self.total_real_var = tk.StringVar(value="—")
        self.total_unreal_var = tk.StringVar(value="—")
        for label, attr in [("\u4eca\u65e5\u80a1\u6570", "total_shares_var"), ("\u4eca\u65e5\u5df2\u5b9e\u73b0", "total_real_var"), ("\u5f53\u524d\u672a\u5b9e\u73b0", "total_unreal_var")]:
            cell = tk.Frame(totals, bg=PANEL_ALT_BG, padx=8, pady=6, highlightthickness=1, highlightbackground=BORDER)
            cell.pack(side="left", expand=True, fill="x", padx=4)
            tk.Label(cell, text=label, bg=PANEL_ALT_BG, fg=TEXT_MUTED, font=FONT_UI_SM).pack(anchor="w")
            tk.Label(cell, textvariable=getattr(self, attr), bg=PANEL_ALT_BG, fg=TEXT_PRIMARY, font=FONT_MONO).pack(anchor="w")

        tree_frame = tk.Frame(self.frame, bg=PANEL_BG)
        tree_frame.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.tree = ttk.Treeview(tree_frame, columns=("sym", "qty_bot", "qty_sld", "pos", "posavgprc", "last", "unreal", "real", "exes"), show="headings", selectmode="browse")
        for cid, label, width, anchor in [("sym", "\u4ee3\u7801", 52, "w"), ("qty_bot", "\u4e70\u5165", 46, "e"), ("qty_sld", "\u5356\u51fa", 46, "e"), ("pos", "\u6301\u4ed3", 46, "e"), ("posavgprc", "\u5747\u4ef7", 68, "e"), ("last", "\u6700\u65b0", 68, "e"), ("unreal", "\u6d6e\u76c8\u4e8f", 82, "e"), ("real", "\u5df2\u5b9e\u73b0", 82, "e"), ("exes", "\u6210\u4ea4", 40, "e")]:
            self.tree.heading(cid, text=label)
            self.tree.column(cid, width=width, minwidth=28, anchor=anchor)

        self.tree.tag_configure("profit", foreground=ACCENT_GREEN)
        self.tree.tag_configure("loss", foreground=ACCENT_RED)
        self.tree.tag_configure("flat", foreground=TEXT_DIM)

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        return self.frame

    def set_enabled(self, enabled: bool):
        self._enabled = bool(enabled)
        if self._refresh_btn:
            self._refresh_btn.config(state="normal" if enabled else "disabled")

    def _on_refresh_clicked(self):
        if self._enabled and self.on_refresh:
            self.on_refresh()

    def update_data(self, positions: list[dict], current_quotes: dict, on_row_click_fn=None):
        for row in self.tree.get_children():
            self.tree.delete(row)

        total_unreal = 0.0
        total_real = 0.0
        for position in positions:
            sym = position["symbol"]
            qty = int(position["qty"])
            direction = position["direction"]
            is_long = direction in ("Long", "L")
            avg = position["avg_open"]
            close_px = current_quotes.get(sym, {}).get("last", position.get("close_px", 0))
            realized = position.get("realized_today", 0)

            if qty == 0:
                total_real += realized
                self.tree.insert("", "end", iid=sym, tags=("flat",), values=(sym, int(position.get("qty_bot", 0)), int(position.get("qty_sld", 0)), 0, f"{avg:.4f}" if avg else "—", f"{close_px:.2f}" if close_px else "—", "—", f"+{realized:.2f}" if realized >= 0 else f"{realized:.2f}", position.get("exes", 0)))
                continue

            display_qty = qty if is_long else -qty
            unrealized = round((close_px - avg) * qty * (1 if is_long else -1), 2)
            qty_bot = position.get("qty_bot", qty if is_long else 0)
            qty_sld = position.get("qty_sld", qty if not is_long else 0)
            exes = position.get("exes", 0)
            tag = "profit" if unrealized > 0 else ("loss" if unrealized < 0 else "flat")
            self.tree.insert("", "end", iid=sym, tags=(tag,), values=(sym, int(qty_bot), int(qty_sld), display_qty, f"{avg:.4f}", f"{close_px:.2f}", f"+{unrealized:.2f}" if unrealized >= 0 else f"{unrealized:.2f}", f"+{realized:.2f}" if realized >= 0 else f"{realized:.2f}", exes))
            total_unreal += unrealized
            total_real += realized

        self.total_unreal_var.set(("+$%.2f" % total_unreal) if total_unreal >= 0 else ("-$%.2f" % abs(total_unreal)))
        self.total_real_var.set(("+$%.2f" % total_real) if total_real >= 0 else ("-$%.2f" % abs(total_real)))

        total_shares = 0
        for row in self.tree.get_children():
            values = self.tree.item(row, "values")
            if values and len(values) >= 3:
                try:
                    total_shares += int(float(values[1] or 0)) + int(float(values[2] or 0))
                except Exception:
                    pass
        self.total_shares_var.set(f"{total_shares:,}")

        if on_row_click_fn:
            self.tree.bind("<<TreeviewSelect>>", lambda _e: self._handle_row_select(on_row_click_fn))

    def _handle_row_select(self, callback):
        if not self._enabled:
            return
        selection = self.tree.selection()
        if selection:
            callback(selection[0])

    @staticmethod
    def _bind_button_hover(btn: tk.Button, normal_bg: str):
        btn.bind("<Enter>", lambda _e: btn.config(bg=BUTTON_HOVER_BG))
        btn.bind("<Leave>", lambda _e: btn.config(bg=normal_bg))

    def live_update_pnl(self, current_quotes: dict):
        total_unreal = 0.0
        total_real = 0.0
        for row in self.tree.get_children():
            values = self.tree.item(row, "values")
            if not values or len(values) < 9:
                continue
            sym, qty_bot, qty_sld, pos_s, avg_s, _, _, real_str, exes = values
            try:
                realized = float(str(real_str).replace("+", ""))
            except Exception:
                realized = 0.0

            if str(pos_s) in ("0", "-0", "0.0") or avg_s == "—":
                total_real += realized
                continue

            close_px = current_quotes.get(sym, {}).get("last")
            if close_px is None:
                try:
                    close_px = float(values[5]) if values[5] not in ("—", "", None) else None
                except Exception:
                    close_px = None
            if close_px is None:
                total_real += realized
                continue

            try:
                qty_signed = int(pos_s)
                abs_qty = abs(qty_signed)
                avg_f = float(avg_s)
                unrealized = round((close_px - avg_f) * abs_qty, 2) if qty_signed >= 0 else round((avg_f - close_px) * abs_qty, 2)
            except Exception:
                continue

            total_unreal += unrealized
            total_real += realized
            tag = "profit" if unrealized > 0 else ("loss" if unrealized < 0 else "flat")
            self.tree.item(row, tags=(tag,), values=(sym, qty_bot, qty_sld, pos_s, avg_s, f"{close_px:.2f}", f"+{unrealized:.2f}" if unrealized >= 0 else f"{unrealized:.2f}", real_str, exes))

        self.total_unreal_var.set(("+$%.2f" % total_unreal) if total_unreal >= 0 else ("-$%.2f" % abs(total_unreal)))
        self.total_real_var.set(("+$%.2f" % total_real) if total_real >= 0 else ("-$%.2f" % abs(total_real)))

        total_shares = 0
        for row in self.tree.get_children():
            values = self.tree.item(row, "values")
            if values and len(values) >= 3:
                try:
                    total_shares += int(float(values[1] or 0)) + int(float(values[2] or 0))
                except Exception:
                    pass
        self.total_shares_var.set(f"{total_shares:,}")
