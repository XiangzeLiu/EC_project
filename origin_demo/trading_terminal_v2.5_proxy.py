"""
Trading Terminal  (精简版)
─────────────────────────
· 所有面板内嵌在主窗口，无浮动子窗口
· 去掉 Level 2 和 Time & Sales
· 保留：行情条 / 下单 / 持仓 / 订单 / 日志
依赖：pip install 交易SDK websockets
"""

import tkinter as tk
from tkinter import ttk, messagebox
import threading
import queue
import time
import datetime
import random
import asyncio
import json
import os
import re
from decimal import Decimal
import urllib.request
import urllib.error
import threading as _threading
try:
    from zoneinfo import ZoneInfo
except ImportError:
    try:
        from backports.zoneinfo import ZoneInfo
    except ImportError:
        import datetime as _dt
        class ZoneInfo:
            """Fallback: 仅支持 UTC 和 America/New_York（固定 ET offset）"""
            _OFFSETS = {"America/New_York": -5, "UTC": 0}
            def __init__(self, key):
                self._key = key
                self._offset = _dt.timedelta(hours=self._OFFSETS.get(key, 0))
            def utcoffset(self, dt): return self._offset
            def tzname(self, dt):   return self._key
            def fromutc(self, dt):  return dt + self._offset
            def __repr__(self):     return f"ZoneInfo('{self._key}')"

# ── SDK ───────────────────────────────────────────────────────────────────────
SDK_AVAILABLE = False
SDK_ERROR = ""
try:
    from tastytrade import Session, DXLinkStreamer
    from tastytrade.account import Account
    from tastytrade.instruments import Equity
    try:
        from tastytrade.dxfeed import Quote as DXQuote
    except ImportError:
        DXQuote = None
    from tastytrade.order import (
        NewOrder, OrderAction, OrderTimeInForce, OrderType, Leg, InstrumentType,
    )
    SDK_AVAILABLE = True
except Exception as _e:
    SDK_ERROR = str(_e)

# ── Colors & Fonts ────────────────────────────────────────────────────────────
DARK_BG      = "#0d0f14"
PANEL_BG     = "#13161e"
BORDER       = "#1e2330"
ACCENT_BLUE  = "#4f9eff"
ACCENT_GREEN = "#00d68f"
ACCENT_RED   = "#ff4d6a"
GOLD         = "#f5c418"
TEXT_PRIMARY = "#e8ecf4"
TEXT_DIM     = "#6b7590"
TEXT_MUTED   = "#3a3f52"

FONT_MONO    = ("Courier New", 10)
FONT_MONO_SM = ("Courier New", 9)
FONT_UI_SM   = ("Segoe UI", 9)
FONT_BOLD    = ("Segoe UI", 9, "bold")
FONT_TICKER  = ("Courier New", 15, "bold")

# ── Config ────────────────────────────────────────────────────────────────────
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tt_config.json")

def load_config():
    try:
        with open(CONFIG_PATH) as f: return json.load(f)
    except: return {}

def save_config(d):
    try:
        with open(CONFIG_PATH, "w") as f: json.dump(d, f)
    except: pass

# ── Sanitize ──────────────────────────────────────────────────────────────────
_BROKER_RE = re.compile(r'\b(tastytrade|tastyworks|tastytrade\.com|tastyworks\.com)\b', re.I)
def sanitize(t): return _BROKER_RE.sub("broker", str(t))

# ── Mock quote ────────────────────────────────────────────────────────────────
MOCK_BASE = {}
def mock_quote(sym, base):
    last = round(base + random.uniform(-0.3, 0.3), 2)
    sp   = random.uniform(0.01, 0.08)
    return dict(symbol=sym, bid=round(last-sp,2), ask=round(last+sp,2),
                last=last, volume=random.randint(100,9999)*100,
                timestamp=datetime.datetime.now().strftime("%H:%M:%S"))


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  TradingSession  (业务层，不含 UI)                                       ║
# ╚══════════════════════════════════════════════════════════════════════════╝
# ── 代理服务器地址 ────────────────────────────────────────────────────────────
PROXY_HOST = "47.86.245.60"
PROXY_PORT = 8800
PROXY_BASE = f"http://{PROXY_HOST}:{PROXY_PORT}"


class TradingSession:
    def __init__(self):
        self.connected  = False
        self.mock_mode  = False
        self._token     = ""   # server token
        self._acct_num  = ""
        self._ET        = ZoneInfo("America/New_York")
        self._pos_error = ""

    # ── HTTP 工具 ──────────────────────────────────────────────────────────────
    def _http(self, method, path, body=None):
        url = PROXY_BASE + path
        data = json.dumps(body).encode() if body else None
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            try:    body = json.loads(e.read().decode())
            except: body = {}
            return e.code, body
        except Exception as e:
            return 0, {"detail": str(e)}

    # ── 登录 ──────────────────────────────────────────────────────────────────
    def login(self, username, password):
        status, resp = self._http("POST", "/auth/login",
                                  {"username": username, "password": password})
        if status == 200:
            self._token    = resp.get("token", "")
            self._acct_num = resp.get("acct_num", "")
            self.connected = True
            self.mock_mode = False
            return True, "Connected"
        if status == 0:
            return False, "Server not available, please check server is running"
        msg = resp.get("detail", f"Login failed (HTTP {status})")
        return False, sanitize(msg)

    # ── Today activity (持仓 + 已平仓) ────────────────────────────────────────
    def get_today_activity(self):
        if self.mock_mode:
            return [
                dict(symbol="AAPL", qty=100, direction="Long",  avg_open=185.20, close_px=189.42, unrealized=422.0,  realized_today=155.0),
                dict(symbol="BIL",  qty=0,   direction="—",     avg_open=91.56,  close_px=91.56,  unrealized=0.0,    realized_today=-8.0),
                dict(symbol="NVDA", qty=50,  direction="Long",  avg_open=890.00, close_px=875.20, unrealized=-740.0, realized_today=-120.0),
            ]
        if not self.connected: return []
        try:
            # 从代理服务器获取持仓
            _, pos_resp = self._http("GET", "/positions")
            pos_rows = pos_resp.get("positions", [])

            # 从代理服务器获取订单历史
            _, ord_resp = self._http("GET", "/orders/history")
            orders_raw = ord_resp.get("orders", [])

            return self._calc_today_activity(pos_rows, orders_raw)
        except Exception as e:
            self._pos_error = sanitize(str(e)); return []

    def _calc_today_activity(self, pos_rows_raw, orders_raw):
        """从代理服务器返回的原始数据计算持仓和P&L"""
        pos_map = {}

        # Step 1: open positions
        for p in pos_rows_raw:
            try:
                sym  = p.get("symbol", "")
                qty  = float(p.get("quantity", 0))
                avg  = float(p.get("average_open_price", 0) or 0)
                cpx  = float(p.get("close_price", 0) or 0)
                dirn = p.get("direction", "Long")
                real = float(p.get("realized_today", 0) or 0)
                pos_map[sym] = dict(
                    symbol=sym, qty=qty, direction=dirn,
                    avg_open=avg, close_px=cpx,
                    unrealized=round((cpx-avg)*qty*(1 if dirn=="Long" else -1), 2),
                    realized_today=real,
                    qty_bot=0, qty_sld=0, exes=0)
            except Exception:
                continue

        # Step 2: scan today's order history for fills
        # 用美东 ET 时间，交易时段 04:00-20:00
        ET            = self._ET
        now_et        = datetime.datetime.now(ET)
        today_et      = now_et.date()
        session_start = datetime.datetime.combine(today_et, datetime.time(4, 0),  tzinfo=ET)
        session_end   = datetime.datetime.combine(today_et, datetime.time(20, 0), tzinfo=ET)
        ledger = {}

        try:
            orders = orders_raw  # 已从代理服务器获取
            for o in orders:
                try:
                    status = str(o.get("status", "") if isinstance(o, dict) else getattr(o, "status", "")).lower()
                    if "fill" not in status: continue
                    legs = o.get("legs", []) if isinstance(o, dict) else getattr(o, "legs", [])
                    if not legs: continue

                    # 时间过滤：统一转 ET，判断是否在今日交易时段内
                    if isinstance(o, dict):
                        o_ts_str = o.get("updated_at") or o.get("created_at") or ""
                        try:
                            o_ts = datetime.datetime.fromisoformat(o_ts_str.replace("Z","+00:00")) if o_ts_str else None
                        except: o_ts = None
                    else:
                        o_ts = (getattr(o, "updated_at", None) or
                                getattr(o, "created_at", None) or
                                getattr(o, "received_at", None))
                    if o_ts:
                        if hasattr(o_ts, "tzinfo") and o_ts.tzinfo is None:
                            o_ts = o_ts.replace(tzinfo=datetime.timezone.utc)
                        o_ts_et = o_ts.astimezone(ET)
                        if not (session_start <= o_ts_et <= session_end):
                            continue
                    # o_ts 为 None 时默认保留不过滤

                    for leg in legs:
                        sym    = leg.get("symbol","") if isinstance(leg, dict) else leg.symbol
                        act    = str(leg.get("action","") if isinstance(leg, dict) else leg.action)
                        # 精确匹配 action 类型，避免 "Sell to Open" 被误判为买入
                        is_buy_to_open   = "Buy"  in act and "Open"  in act
                        is_sell_to_close = "Sell" in act and "Close" in act
                        is_sell_to_open  = "Sell" in act and "Open"  in act
                        is_buy_to_close  = "Buy"  in act and "Close" in act
                        leg_qty = float(leg.get("quantity", 0) if isinstance(leg, dict) else getattr(leg, "quantity", 0) or 0)
                        fills   = (leg.get("fills", []) if isinstance(leg, dict) else getattr(leg, "fills", [])) or []

                        def record(fqty, fp):
                            """把一笔成交归入正确的 ledger 桶"""
                            if fqty <= 0 or fp <= 0: return
                            if sym not in ledger:
                                ledger[sym] = {
                                    "long_buys": [],   # Buy to Open
                                    "long_sells": [],  # Sell to Close
                                    "short_sells": [], # Sell to Open（开空）
                                    "short_buys": [],  # Buy to Close（平空）
                                    "exes": 0}
                            ledger[sym]["exes"] += 1
                            if   is_buy_to_open:   ledger[sym]["long_buys"].append((fqty, fp))
                            elif is_sell_to_close: ledger[sym]["long_sells"].append((fqty, fp))
                            elif is_sell_to_open:  ledger[sym]["short_sells"].append((fqty, fp))
                            elif is_buy_to_close:  ledger[sym]["short_buys"].append((fqty, fp))

                        if fills:
                            for fill in fills:
                                try:
                                    # fill 是 dict（来自代理服务器）
                                    if isinstance(fill, dict):
                                        fp   = float(fill.get("fill_price", 0) or 0)
                                        fqty = float(fill.get("quantity", 0) or leg_qty)
                                        fat_s = fill.get("filled_at", "")
                                        fat = None
                                        if fat_s:
                                            try:
                                                fat = datetime.datetime.fromisoformat(
                                                    fat_s.replace("Z", "+00:00"))
                                            except: fat = None
                                    else:
                                        fp   = float(getattr(fill, "fill_price", 0) or 0)
                                        fqty = leg_qty / len(fills) if len(fills) > 1 else leg_qty
                                        fat  = getattr(fill, "filled_at", None)
                                    if fat:
                                        if hasattr(fat, "tzinfo") and fat.tzinfo is None:
                                            fat = fat.replace(tzinfo=datetime.timezone.utc)
                                        fat_et = fat.astimezone(ET)
                                        if not (session_start <= fat_et <= session_end):
                                            continue
                                    record(fqty, fp)
                                except: continue
                        elif leg_qty > 0:
                            px = o.get("price", 0) if isinstance(o, dict) else getattr(o, "price", 0)
                            fp = float(px or 0)
                            record(leg_qty, fp)
                except Exception:
                    continue
        except Exception:
            pass

        # Step 3: 多空分开计算 P&L
        for sym, trades in ledger.items():
            exes = trades["exes"]

            # ── 多头部分：Buy to Open / Sell to Close ──
            long_buys  = trades["long_buys"]
            long_sells = trades["long_sells"]
            lbq  = sum(q for q, _ in long_buys)
            lbc  = sum(q * p for q, p in long_buys)
            long_avg = lbc / lbq if lbq > 0 else 0
            lsq  = sum(q for q, _ in long_sells)
            lsp  = sum(q * p for q, p in long_sells)
            long_realized = round(lsp - long_avg * lsq, 2) if long_avg > 0 else 0

            # ── 空头部分：Sell to Open / Buy to Close ──
            short_sells = trades["short_sells"]
            short_buys  = trades["short_buys"]
            ssq  = sum(q for q, _ in short_sells)
            ssc  = sum(q * p for q, p in short_sells)
            short_avg = ssc / ssq if ssq > 0 else 0
            sbq  = sum(q for q, _ in short_buys)
            sbp  = sum(q * p for q, p in short_buys)
            # 空头盈亏 = (开空均价 - 平空均价) × 平空数量
            short_realized = round((short_avg * sbq) - sbp, 2) if short_avg > 0 else 0

            realized = round(long_realized + short_realized, 2)

            # Bot = 实际买入股数（开多 + 平空），Sld = 实际卖出股数（平多 + 开空）
            qty_bot = lbq + sbq
            qty_sld = lsq + ssq
            net_qty = lbq - lsq  # 多头净仓（不含空头）

            if sym in pos_map:
                # Update existing open position with today's trade data
                pos_map[sym]["qty_bot"]  = qty_bot
                pos_map[sym]["qty_sld"]  = qty_sld
                pos_map[sym]["exes"]     = exes
                if pos_map[sym]["realized_today"] == 0 and realized != 0:
                    pos_map[sym]["realized_today"] = realized
            else:
                # Closed position — still show it
                # 均价：有多头开仓用多头均价，否则用空头均价
                display_avg = long_avg if long_avg > 0 else short_avg
                pos_map[sym] = dict(
                    symbol=sym, qty=0, direction="—",
                    avg_open=round(display_avg, 4), close_px=0.0,
                    unrealized=0.0, realized_today=realized,
                    qty_bot=qty_bot, qty_sld=qty_sld, exes=exes)

        return list(pos_map.values())

    # ── Orders ────────────────────────────────────────────────────────────────
    def get_orders(self, mode="live"):
        if self.mock_mode or not self.connected: return []
        try:
            path = "/orders/live" if mode == "live" else "/orders/history"
            _, resp = self._http("GET", path)
            raw = resp.get("orders", [])

            STATUS_MAP = {
                "Live":"Live","Received":"Received","Routing":"Routing",
                "Filled":"Filled","Cancelled":"Cancelled","Rejected":"Rejected",
                "Partial":"Partial","Cancelling":"Cancelling","Expired":"Expired",
            }
            LIVE_ST = {"Received","Routing","Live","Cancelling","Partial"}
            ET      = self._ET
            result  = []
            if not raw:
                pass  # empty orders list from server
            for o in raw:
                try:
                    # 调试：打印第一条订单的原始数据
                    if raw and o == raw[0]:
                        import sys
                        print(f"[OrderDebug] first order: {o}", file=sys.stderr)
                    if mode == "all":
                        o_ts_str = o.get("updated_at","")
                        if o_ts_str:
                            try:
                                o_ts     = datetime.datetime.fromisoformat(o_ts_str.replace("Z","+00:00"))
                                et_today = datetime.datetime.now(ET).date()
                                s_start  = datetime.datetime.combine(et_today, datetime.time(4,0),  tzinfo=ET)
                                s_end    = datetime.datetime.combine(et_today, datetime.time(20,0), tzinfo=ET)
                                if not (s_start <= o_ts.astimezone(ET) <= s_end):
                                    continue
                            except: pass

                    sym = o.get("symbol","—")
                    act = "BUY" if "Buy" in o.get("action","") else "SELL"
                    qty = str(o.get("qty","—"))
                    px  = o.get("price","MKT")
                    rs  = o.get("status","—")
                    st  = STATUS_MAP.get(rs, rs)
                    ot  = o.get("type","—")
                    tif = o.get("tif","—")
                    if mode == "live" and rs not in LIVE_ST: continue
                    result.append(dict(id=o.get("id",""), symbol=sym, action=act,
                                       qty=qty, price=px, status=st,
                                       raw_status=rs, otype=ot, tif=tif))
                except: continue
            return result
        except Exception as e:
            return []

    def cancel_order(self, oid):
        if not self.connected: return False, "Not connected"
        try:
            status, resp = self._http("DELETE", f"/orders/{oid}")
            if status in (200, 201, 204):
                return True, f"Order {str(oid)[-6:]} cancelled"
            return False, sanitize(resp.get("detail", f"Cancel failed (HTTP {status})"))
        except Exception as e:
            return False, sanitize(f"撤单失败: {e}")

    def place_order(self, symbol, qty, price, action, order_type="limit", tif="Day"):
        if self.mock_mode:
            time.sleep(0.3)
            return True, f"[SIM] {action} {qty} {symbol} @ {'Market' if order_type=='market' else f'${price}'} | {tif}"
        if not self.connected: return False, "Not connected"
        try:
            status, resp = self._http("POST", "/orders/place", {
                "symbol":     symbol,
                "qty":        qty,
                "price":      price,
                "action":     action,
                "order_type": order_type,
                "tif":        tif,
            })
            if status in (200, 201):
                oid = resp.get("order_id", "")
                return True, f"Order submitted — ID: {str(oid)[-8:]}"
            return False, sanitize(resp.get("detail", f"Order failed (HTTP {status})"))
        except Exception as e:
            return False, sanitize(f"Order failed: {e}")


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  TradingTerminal  (主窗口，单窗口内嵌布局)                                ║
# ╚══════════════════════════════════════════════════════════════════════════╝
class TradingTerminal(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("● Trading Terminal")
        # 启动后自适应居中，宽度刚好容纳双面板，高度适中
        self.update_idletasks()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        w  = min(910, int(sw * 0.90))
        h  = min(820,  int(sh * 0.85))
        x  = (sw - w) // 2
        y  = (sh - h) // 2
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.minsize(1100, 650)
        self.configure(bg=DARK_BG)

        self.ts            = TradingSession()
        self.quote_queue   = queue.Queue()
        self.sub_queue     = queue.Queue()
        self.current_sym   = None
        self.current_quote = {}
        self._stream_active    = False
        self._mock_active     = False
        self._price_needs_fill = True
        self._ev_diag_done    = False
        self.panels           = {}   # 两套面板变量，key=0/1
        self.active_panel     = 0   # 当前焦点面板

        self._apply_style()
        self._build_ui()
        self._setup_hotkeys()
        self._load_credentials()
        self._start_mock_stream()
        self._poll()

        if SDK_AVAILABLE:
            self._log("SDK loaded", "ok")
        else:
            self._log("Running in simulation mode", "inf")

    # ── Style ─────────────────────────────────────────────────────────────────
    def _apply_style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure("Treeview", background=PANEL_BG, foreground=TEXT_PRIMARY,
                    fieldbackground=PANEL_BG, rowheight=22,
                    font=FONT_MONO_SM, borderwidth=0, relief="flat")
        s.configure("Treeview.Heading", background=DARK_BG, foreground=TEXT_DIM,
                    font=FONT_BOLD, relief="flat")
        s.map("Treeview", background=[("selected","#1e2b45")],
                          foreground=[("selected", ACCENT_BLUE)])
        s.configure("TScrollbar", background=BORDER, troughcolor=DARK_BG, borderwidth=0)
        s.configure("TPanedwindow", background=BORDER)

    # ── Build entire UI ───────────────────────────────────────────────────────
    def _build_ui(self):
        # ══ Top bar ══════════════════════════════════════════════════════════
        top = tk.Frame(self, bg="#080a0e", height=48)
        top.pack(fill="x"); top.pack_propagate(False)

        tk.Label(top, text="◈ TRADING TERMINAL",
                 bg="#080a0e", fg=ACCENT_BLUE,
                 font=("Courier New",12,"bold")).pack(side="left", padx=14)

        # Login fields
        lf = tk.Frame(top, bg="#080a0e"); lf.pack(side="left", padx=8)
        for lbl, attr, show in [("Username","secret_entry","●"),("Password","token_entry","●")]:
            tk.Label(lf, text=lbl, bg="#080a0e", fg=TEXT_DIM,
                     font=FONT_UI_SM).pack(side="left", padx=(6,2))
            e = tk.Entry(lf, bg="#1c2030", fg=TEXT_PRIMARY,
                         insertbackground=TEXT_PRIMARY, font=FONT_MONO,
                         relief="flat", bd=3, show=show, width=16)
            e.pack(side="left", ipady=3)
            setattr(self, attr, e)
        tk.Button(lf, text="Connect →", bg=ACCENT_BLUE, fg=DARK_BG,
                  font=("Segoe UI",9,"bold"), relief="flat", bd=0,
                  padx=10, pady=3, cursor="hand2",
                  command=self._do_login).pack(side="left", padx=6)

        self.status_var = tk.StringVar(value="● Not connected")
        self.status_lbl = tk.Label(top, textvariable=self.status_var,
                                   bg="#080a0e", fg=ACCENT_RED, font=FONT_UI_SM)
        self.status_lbl.pack(side="left", padx=4)

        self.time_var = tk.StringVar()
        tk.Label(top, textvariable=self.time_var, bg="#080a0e",
                 fg=TEXT_DIM, font=FONT_MONO).pack(side="right", padx=12)
        self._tick_clock()

        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        # ══ 双面板：左右各一套行情+下单区域 ═══════════════════════════════════
        panels_outer = tk.Frame(self, bg=DARK_BG)
        panels_outer.pack(fill="x")

        for pid in range(2):
            p = {}  # 本面板的所有变量和控件

            # 外框：左右各占一半，中间分隔线
            pf = tk.Frame(panels_outer, bg=PANEL_BG,
                          highlightthickness=2,
                          highlightbackground=BORDER)
            pf.pack(side="left", fill="both", expand=True,
                    padx=(0 if pid==0 else 1, 0))
            p["frame"] = pf
            # 点击面板任意位置激活高亮
            pf.bind("<Button-1>", lambda e, i=pid: self._activate_panel(i))

            # ── 行情行 ────────────────────────────────────────────────────────
            row1 = tk.Frame(pf, bg=PANEL_BG, height=48)
            row1.pack(fill="x"); row1.pack_propagate(False)

            tk.Label(row1, text=" Symbol", bg=PANEL_BG, fg=TEXT_DIM,
                     font=FONT_BOLD).pack(side="left", padx=(8,2))
            p["sym_var"]   = tk.StringVar()
            p["sym_entry"] = tk.Entry(row1, textvariable=p["sym_var"],
                                      bg="#1c2030", fg=TEXT_PRIMARY,
                                      insertbackground=TEXT_PRIMARY,
                                      font=FONT_TICKER, width=5,
                                      relief="flat", bd=4)
            p["sym_entry"].pack(side="left", ipady=3)
            p["sym_entry"].bind("<Return>", lambda e, i=pid: self._on_symbol_enter(i))
            p["sym_entry"].bind("<Key>",     lambda e, i=pid: self._sym_key_filter(e, i))
            p["sym_entry"].bind("<FocusIn>", lambda e, i=pid: self._activate_panel(i))

            p["q_last_var"] = tk.StringVar(value="—")
            p["q_bid_var"]  = tk.StringVar(value="—")
            p["q_ask_var"]  = tk.StringVar(value="—")
            p["q_chg_var"]  = tk.StringVar(value="—")
            p["q_vol_var"]  = tk.StringVar(value="—")
            for var_key, lbl, fg in [
                ("q_last_var", "LAST", TEXT_PRIMARY),
                ("q_bid_var",  "BID",  ACCENT_GREEN),
                ("q_ask_var",  "ASK",  ACCENT_RED),
            ]:
                cell = tk.Frame(row1, bg=PANEL_BG); cell.pack(side="left", padx=10)
                tk.Label(cell, text=lbl, bg=PANEL_BG, fg=TEXT_MUTED,
                         font=("Segoe UI",9,"bold")).pack(side="left", padx=(0,4))
                tk.Label(cell, textvariable=p[var_key], bg=PANEL_BG, fg=fg,
                         font=("Segoe UI",14,"bold")).pack(side="left")

            # ── 下单行 ────────────────────────────────────────────────────────
            tk.Frame(pf, bg=BORDER, height=1).pack(fill="x")
            mid = tk.Frame(pf, bg=PANEL_BG, height=44)
            mid.pack(fill="x"); mid.pack_propagate(False)

            tk.Label(mid, text="  Type", bg=PANEL_BG, fg=TEXT_DIM,
                     font=FONT_UI_SM).pack(side="left", padx=(8,2))
            p["order_type_var"] = tk.StringVar(value="Limit")
            type_cb = ttk.Combobox(mid, textvariable=p["order_type_var"],
                                   values=["Limit","Market"], state="readonly",
                                   width=6, font=FONT_UI_SM)
            type_cb.pack(side="left", padx=(2,8))
            type_cb.bind("<<ComboboxSelected>>", lambda e, i=pid: self._on_order_type_change(i))

            tk.Label(mid, text="TIF", bg=PANEL_BG, fg=TEXT_DIM,
                     font=FONT_UI_SM).pack(side="left")
            p["tif_var"] = tk.StringVar(value="Day")
            ttk.Combobox(mid, textvariable=p["tif_var"],
                         values=["Day","GTC","IOC","EXT","GTC_EXT"],
                         state="readonly", width=7, font=FONT_UI_SM
                         ).pack(side="left", padx=(3,8))

            tk.Label(mid, text="Qty", bg=PANEL_BG, fg=TEXT_DIM,
                     font=FONT_UI_SM).pack(side="left")
            # qty 只允许整数
            _qty_vcmd = (self.register(
                lambda s: s == "" or (s.isdigit())), "%P")
            p["qty_entry"] = tk.Entry(mid, bg="#1c2030", fg=TEXT_PRIMARY,
                                      insertbackground=TEXT_PRIMARY, font=FONT_MONO,
                                      relief="flat", bd=3, width=5,
                                      validate="key", validatecommand=_qty_vcmd)
            p["qty_entry"].insert(0,"100")
            p["qty_entry"].pack(side="left", ipady=3, padx=(3,8))

            # price 只允许数字和小数点
            _price_vcmd = (self.register(
                lambda s: s == "" or (all(c.isdigit() or c == "." for c in s)
                                      and s.count(".") <= 1)), "%P")
            p["price_lbl"] = tk.Label(mid, text="Price", bg=PANEL_BG,
                                      fg=TEXT_DIM, font=FONT_UI_SM)
            p["price_lbl"].pack(side="left")
            p["price_entry"] = tk.Entry(mid, bg="#1c2030", fg=TEXT_PRIMARY,
                                        insertbackground=TEXT_PRIMARY, font=FONT_MONO,
                                        relief="flat", bd=3, width=7,
                                        validate="key", validatecommand=_price_vcmd)
            p["price_entry"].pack(side="left", ipady=3, padx=(3,10))
            p["qty_entry"].bind("<FocusIn>",   lambda e, i=pid: self._activate_panel(i))
            p["price_entry"].bind("<FocusIn>", lambda e, i=pid: self._activate_panel(i))
            # Esc：优先取消待下单状态，否则撤当前 symbol 所有 live 订单
            for _ew in (p["sym_entry"], p["qty_entry"]):
                _ew.bind("<Escape>", lambda e, i=pid: self._esc_cancel_orders(i))
            # 小键盘热键绑在三个输入框上，此时三个控件均已创建
            # 热键只在 sym_entry 生效（通过 _sym_key_filter 处理）

            p["buy_btn"] = tk.Button(mid, text="▲ BUY", bg=ACCENT_GREEN, fg=DARK_BG,
                      font=("Segoe UI",10,"bold"), relief="flat", bd=0,
                      padx=10, pady=3, cursor="hand2",
                      command=lambda i=pid: self._place_order("Buy to Open", i))
            p["buy_btn"].pack(side="left", padx=(0,4))
            p["sell_btn"] = tk.Button(mid, text="▼ SELL", bg=ACCENT_RED, fg=DARK_BG,
                      font=("Segoe UI",10,"bold"), relief="flat", bd=0,
                      padx=10, pady=3, cursor="hand2",
                      command=lambda i=pid: self._place_order("Sell to Close", i))
            p["sell_btn"].pack(side="left")

            p["order_sym_var"]  = tk.StringVar(value="—")
            p["order_last_var"] = tk.StringVar(value="")
            p["current_sym"]    = None
            p["price_needs_fill"] = True

            # 热键绑定
            p["qty_entry"].bind("<Up>",    lambda e, i=pid: self._adj_qty(+500, i))
            p["qty_entry"].bind("<Down>",  lambda e, i=pid: self._adj_qty(-500, i))
            p["qty_entry"].bind("<Right>", lambda e, i=pid: self._adj_qty(+100, i))
            p["qty_entry"].bind("<Left>",  lambda e, i=pid: self._adj_qty(-100, i))
            p["price_entry"].bind("<Up>",    lambda e, i=pid: self._adj_price(+0.05, i))
            p["price_entry"].bind("<Down>",  lambda e, i=pid: self._adj_price(-0.05, i))
            p["price_entry"].bind("<Right>", lambda e, i=pid: self._adj_price(+0.01, i))
            p["price_entry"].bind("<Left>",  lambda e, i=pid: self._adj_price(-0.01, i))

            self.panels[pid] = p

        # 兼容旧代码引用
        self.sym_var        = self.panels[0]["sym_var"]
        self.sym_entry      = self.panels[0]["sym_entry"]
        self.q_last_var     = self.panels[0]["q_last_var"]
        self.q_bid_var      = self.panels[0]["q_bid_var"]
        self.q_ask_var      = self.panels[0]["q_ask_var"]
        self.q_chg_var      = self.panels[0]["q_chg_var"]
        self.q_vol_var      = self.panels[0]["q_vol_var"]
        self.order_type_var = self.panels[0]["order_type_var"]
        self.tif_var        = self.panels[0]["tif_var"]
        self.qty_entry      = self.panels[0]["qty_entry"]
        self.price_entry    = self.panels[0]["price_entry"]
        self.price_lbl      = self.panels[0]["price_lbl"]
        self.order_sym_var  = self.panels[0]["order_sym_var"]
        self.order_last_var = self.panels[0]["order_last_var"]
        self.session_var    = tk.StringVar(value="Market Hours")

        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        # ══ Main body: Positions (top) + Orders (bottom) + Log (footer) ══════
        body = tk.Frame(self, bg=DARK_BG)
        body.pack(fill="both", expand=True)

        # PanedWindow: positions | orders  (左右各半，可拖动)
        pw = ttk.PanedWindow(body, orient="horizontal")
        pw.pack(fill="both", expand=True, padx=6, pady=(6,0))

        # ── Orders pane (左) ─────────────────────────────────────────────────
        ord_frame = tk.Frame(pw, bg=PANEL_BG)
        pw.add(ord_frame, weight=1)

        ord_hdr = tk.Frame(ord_frame, bg=PANEL_BG)
        ord_hdr.pack(fill="x", padx=6, pady=(6,2))

        self._order_mode = tk.StringVar(value="live")
        self._order_tab_btns = {}
        for lbl, mode in [("● Live","live"),("All","all")]:
            btn = tk.Button(ord_hdr, text=lbl,
                            bg=ACCENT_BLUE if mode=="live" else PANEL_BG,
                            fg=DARK_BG if mode=="live" else TEXT_PRIMARY,
                            font=FONT_UI_SM, relief="flat", bd=0,
                            padx=10, pady=3, cursor="hand2",
                            command=lambda m=mode: self._switch_order_mode(m))
            btn.pack(side="left", padx=2)
            self._order_tab_btns[mode] = btn

        self.orders_count_var = tk.StringVar(value="No orders")
        tk.Label(ord_hdr, textvariable=self.orders_count_var,
                 bg=PANEL_BG, fg=TEXT_PRIMARY, font=FONT_UI_SM).pack(side="left", padx=8)
        tk.Button(ord_hdr, text="⟳", bg=PANEL_BG, fg=ACCENT_BLUE,
                  font=FONT_UI_SM, relief="flat", bd=0, padx=4, cursor="hand2",
                  command=self._refresh_orders).pack(side="right")

        of = tk.Frame(ord_frame, bg=PANEL_BG)
        of.pack(fill="both", expand=True, padx=6, pady=(0,6))
        self.ord_tree = ttk.Treeview(of,
            columns=("sym","action","qty","price","type","tif","status"),
            show="headings", selectmode="browse")
        for cid, lb, ww, anc in [
            ("sym",    "Symbol", 60, "w"),
            ("action", "Side",   48, "c"),
            ("qty",    "Qty",    48, "e"),
            ("price",  "Price",  68, "e"),
            ("type",   "Type",   50, "c"),
            ("tif",    "TIF",    40, "c"),
            ("status", "Status", 90, "c"),
        ]:
            self.ord_tree.heading(cid, text=lb)
            self.ord_tree.column(cid, width=ww, minwidth=28, anchor=anc)

        # ── Positions pane (右) ───────────────────────────────────────────────
        pos_frame = tk.Frame(pw, bg=PANEL_BG)
        pw.add(pos_frame, weight=1)

        pos_hdr = tk.Frame(pos_frame, bg=PANEL_BG)
        pos_hdr.pack(fill="x", padx=6, pady=(6,2))
        tk.Label(pos_hdr, text="Positions & P&L", bg=PANEL_BG,
                 fg=TEXT_PRIMARY, font=FONT_BOLD).pack(side="left")
        tk.Button(pos_hdr, text="⟳", bg=PANEL_BG, fg=ACCENT_BLUE,
                  font=FONT_UI_SM, relief="flat", bd=0, padx=4, cursor="hand2",
                  command=self._refresh_positions).pack(side="right")

        # Totals row
        tot = tk.Frame(pos_frame, bg=PANEL_BG)
        tot.pack(fill="x", padx=8, pady=(0,4))
        self.total_shares_var = tk.StringVar(value="—")
        self.total_real_var   = tk.StringVar(value="—")
        self.total_unreal_var = tk.StringVar(value="—")
        for lbl, attr in [("Today's Shares", "total_shares_var"),
                           ("Realized Today", "total_real_var"),
                           ("Unrealized P&L", "total_unreal_var")]:
            c = tk.Frame(tot, bg=PANEL_BG); c.pack(side="left", expand=True, fill="x", padx=6)
            tk.Label(c, text=lbl, bg=PANEL_BG, fg=TEXT_MUTED,
                     font=("Segoe UI",7,"bold")).pack(anchor="w")
            tk.Label(c, textvariable=getattr(self,attr), bg=PANEL_BG,
                     fg=TEXT_PRIMARY, font=("Courier New",11,"bold")).pack(anchor="w")

        pf = tk.Frame(pos_frame, bg=PANEL_BG)
        pf.pack(fill="both", expand=True, padx=6, pady=(0,6))
        # Columns: sym, qty_bot, qty_sld, pos, avg_prc, last, unreal, real
        self.pos_tree = ttk.Treeview(pf,
            columns=("sym","qty_bot","qty_sld","pos","posavgprc","last","unreal","real","exes"),
            show="headings", selectmode="browse")
        for cid, lb, ww, anc in [
            ("sym",      "Sym",       52, "w"),
            ("qty_bot",  "Bot",       46, "e"),
            ("qty_sld",  "Sld",       46, "e"),
            ("pos",      "Pos",       46, "e"),
            ("posavgprc","AvgPrc",    68, "e"),
            ("last",     "Last",      68, "e"),
            ("unreal",   "Unrealized",82, "e"),
            ("real",     "Realized",  82, "e"),
            ("exes",     "Exes",      40, "e"),
        ]:
            self.pos_tree.heading(cid, text=lb)
            self.pos_tree.column(cid, width=ww, minwidth=28, anchor=anc)
        self.pos_tree.tag_configure("profit", foreground=ACCENT_GREEN)
        self.pos_tree.tag_configure("loss",   foreground=ACCENT_RED)
        self.pos_tree.tag_configure("flat",   foreground="#8090a0")
        vsb = ttk.Scrollbar(pf, orient="vertical", command=self.pos_tree.yview)
        self.pos_tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.pos_tree.pack(fill="both", expand=True)
        self.pos_tree.bind("<<TreeviewSelect>>", self._on_pos_select)


        self.ord_tree.tag_configure("buy",      foreground=ACCENT_GREEN)
        self.ord_tree.tag_configure("sell",     foreground=ACCENT_RED)
        self.ord_tree.tag_configure("inactive", foreground="#5a6070")
        vsb2 = ttk.Scrollbar(of, orient="vertical", command=self.ord_tree.yview)
        self.ord_tree.configure(yscrollcommand=vsb2.set)
        vsb2.pack(side="right", fill="y")
        self.ord_tree.pack(fill="both", expand=True)
        self._ord_menu = tk.Menu(self, tearoff=0, bg=PANEL_BG, fg=TEXT_PRIMARY,
                                 activebackground=ACCENT_RED, activeforeground=DARK_BG,
                                 font=FONT_UI_SM)
        self._ord_menu.add_command(label="✕  Cancel Order", command=self._cancel_selected_order)
        self.ord_tree.bind("<Button-3>", self._ord_right_click)

        # ══ Log bar (footer) ══════════════════════════════════════════════════
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")
        log_frame = tk.Frame(self, bg=PANEL_BG, height=80)
        log_frame.pack(fill="x"); log_frame.pack_propagate(False)
        self.log_text = tk.Text(log_frame, bg=PANEL_BG, fg=TEXT_DIM,
                                font=("Courier New",9), relief="flat", bd=0,
                                state="disabled", wrap="word", padx=10, pady=4)
        self.log_text.pack(fill="both", expand=True)
        self.log_text.tag_configure("ok",  foreground=ACCENT_GREEN)
        self.log_text.tag_configure("err", foreground=ACCENT_RED)
        self.log_text.tag_configure("inf", foreground=ACCENT_BLUE)

    # ── Clock ─────────────────────────────────────────────────────────────────
    def _get_active_panel_id(self):
        """优先用焦点判断激活面板，fallback 用边框高亮"""
        focused = self.focus_get()
        for pid, p in self.panels.items():
            if focused in (p.get("sym_entry"), p.get("qty_entry"), p.get("price_entry")):
                return pid
        # fallback：用边框高亮
        for pid, p in self.panels.items():
            if "frame" not in p: continue
            if p["frame"].cget("highlightbackground") == ACCENT_BLUE:
                return pid
        return 0

    def _get_pos_direction(self, sym):
        """从 positions 表读当前标的持仓方向，返回 'long'/'short'/'none'"""
        for row in self.pos_tree.get_children():
            v = self.pos_tree.item(row, "values")
            if not v or v[0] != sym: continue
            try:
                pos = int(float(v[3]))  # Pos 列
                if pos > 0: return "long"
                if pos < 0: return "short"
            except: pass
        return "none"

    def _f_key_order(self, side):
        """F1=卖 F3=买，根据持仓方向决定 action，Market 单直接下"""
        pid = self._get_active_panel_id()
        p   = self.panels[pid]
        sym = p["order_sym_var"].get()
        if sym == "—":
            self._log("F键下单：请先加载股票代码", "err"); return
        try:
            qty = int(p["qty_entry"].get())
        except:
            self._log("F键下单：qty 无效", "err"); return
        if qty <= 0:
            self._log("F键下单：qty 必须大于0", "err"); return

        direction = self._get_pos_direction(sym)

        if side == "buy":
            # 有空头仓位 → 平空；否则 → 开多
            action = "Buy to Close" if direction == "short" else "Buy to Open"
        else:
            # 有多头仓位 → 平多；否则 → 开空
            action = "Sell to Close" if direction == "long" else "Sell to Open"

        tif = p["tif_var"].get()
        self._log(f"[F] {action} {qty} {sym} @ MKT | {tif}", "inf")
        def _bg():
            ok, msg = self.ts.place_order(sym, qty, 0, action, "market", tif=tif)
            self.after(0, lambda: self._log(sanitize(msg), "ok" if ok else "err"))
            if ok:
                self.after(1500, self._refresh_positions)
                self.after(1500, self._refresh_orders)
        threading.Thread(target=_bg, daemon=True).start()

    def _f_key_limit(self, side):
        """F2=Limit卖就绪 F4=Limit买就绪：切换为Limit，填入ask/bid，焦点到price框，回车下单"""
        pid = self._get_active_panel_id()
        p   = self.panels[pid]
        sym = p["order_sym_var"].get()
        if sym == "—":
            self._log("F键下单：请先加载股票代码", "err"); return

        direction = self._get_pos_direction(sym)
        if side == "buy":
            action = "Buy to Close" if direction == "short" else "Buy to Open"
            # 买单默认填 ask 价
            default_px = self.current_quote.get(sym, {}).get("ask", 0)
        else:
            action = "Sell to Close" if direction == "long" else "Sell to Open"
            # 卖单默认填 bid 价
            default_px = self.current_quote.get(sym, {}).get("bid", 0)

        # 切换为 Limit 模式
        p["order_type_var"].set("Limit")
        self._on_order_type_change(pid)

        # 填入默认价格
        p["price_entry"].config(state="normal")
        p["price_entry"].delete(0, "end")
        if default_px:
            p["price_entry"].insert(0, f"{default_px:.2f}")

        # 存储待发 action，回车触发下单
        p["_pending_action"] = action

        # 焦点移到 price 框，买入绿色/卖出红色
        hl_color = ACCENT_GREEN if side == "buy" else ACCENT_RED
        p["price_entry"].focus_set()
        p["price_entry"].config(highlightthickness=2,
                                highlightbackground=hl_color,
                                highlightcolor=hl_color)
        p["price_entry"].bind("<Return>", lambda e, i=pid: self._f_limit_submit(i))
        p["price_entry"].bind("<Escape>", lambda e, i=pid: self._f_limit_cancel(i))

    def _f_limit_submit(self, pid):
        """price 框回车：用当前 price/qty 发出 Limit 单"""
        p   = self.panels[pid]
        sym = p["order_sym_var"].get()
        action = p.get("_pending_action")
        if not action or sym == "—": return
        try:
            qty = int(p["qty_entry"].get())
        except:
            self._log("F键下单：qty 无效", "err"); return
        try:
            price = round(float(p["price_entry"].get().strip()), 2)
        except:
            self._log("F键下单：price 无效", "err"); return
        if price <= 0:
            self._log("F键下单：price 必须大于0", "err"); return

        tif = p["tif_var"].get()
        self._log(f"[F] {action} {qty} {sym} @ ${price:.2f} | {tif}", "inf")

        # 下单完成后解绑回车，恢复 price 框和按钮
        p["price_entry"].unbind("<Return>")
        p["price_entry"].unbind("<Escape>")
        p["price_entry"].config(highlightthickness=0)
        p["_pending_action"] = None

        def _bg():
            ok, msg = self.ts.place_order(sym, qty, price, action, "limit", tif=tif)
            self.after(0, lambda: self._log(sanitize(msg), "ok" if ok else "err"))
            if ok:
                self.after(1500, self._refresh_positions)
                self.after(1500, self._refresh_orders)
        threading.Thread(target=_bg, daemon=True).start()

    def _esc_cancel_orders(self, pid):
        """Esc：若有待下单状态先取消，否则撤当前 symbol 所有 live 订单"""
        p   = self.panels[pid]
        # 优先处理 F2/F4 待下单状态
        if p.get("_pending_action"):
            self._f_limit_cancel(pid)
            return
        sym = p["order_sym_var"].get()
        if sym == "—":
            self._log("Esc撤单：请先加载股票代码", "err"); return
        # ord_tree iid = order id，values[0] = symbol
        live_ids = []
        for r in self.ord_tree.get_children():
            v = self.ord_tree.item(r, "values")
            if v and len(v) >= 1 and v[0] == sym:
                live_ids.append(r)  # iid = order id
        if not live_ids:
            self._log(f"Esc撤单：{sym} 无生效订单", "inf"); return
        self._log(f"Esc撤单：{sym} 撤销 {len(live_ids)} 笔订单", "inf")
        def _bg():
            for oid in live_ids:
                ok, msg = self.ts.cancel_order(oid)
                self.after(0, lambda m=msg, o=ok: self._log(sanitize(m), "ok" if o else "err"))
            self.after(1500, self._refresh_orders)
        threading.Thread(target=_bg, daemon=True).start()

    def _f_limit_cancel(self, pid):
        """Escape 取消 F2/F4 待下单状态，恢复 price 框"""
        p = self.panels[pid]
        p["price_entry"].unbind("<Return>")
        p["price_entry"].unbind("<Escape>")
        p["price_entry"].config(highlightthickness=0)
        p["_pending_action"] = None

    def _activate_panel(self, pid):
        """高亮激活面板边框，其他面板恢复暗色"""
        for i, p in self.panels.items():
            if "frame" not in p: continue
            p["frame"].config(
                highlightbackground=ACCENT_BLUE if i == pid else BORDER)

    def _setup_hotkeys(self):
        # 小键盘热键直接绑定在各面板控件上（见 _build_ui 面板循环）
        # F1=Market卖 F3=Market买 F2=Limit卖就绪 F4=Limit买就绪
        # 只在面板内三个输入框有焦点时响应，qty/price框直接触发，sym框过滤
        def _bind_f(widget, pid_ref=None):
            widget.bind("<F1>", lambda e: self._f_key_order("sell"))
            widget.bind("<F2>", lambda e: self._f_key_limit("sell"))
            widget.bind("<F3>", lambda e: self._f_key_order("buy"))
            widget.bind("<F4>", lambda e: self._f_key_limit("buy"))

        def _bind_f_sym(widget):
            """sym框：F键触发但要过滤掉字母输入冲突，用keysym判断"""
            def _guard(fn):
                def _cb(e):
                    if e.keysym in ("F1","F2","F3","F4"):
                        return fn()
                return _cb
            widget.bind("<F1>", _guard(lambda: self._f_key_order("sell")))
            widget.bind("<F2>", _guard(lambda: self._f_key_limit("sell")))
            widget.bind("<F3>", _guard(lambda: self._f_key_order("buy")))
            widget.bind("<F4>", _guard(lambda: self._f_key_limit("buy")))

        for p in self.panels.values():
            _bind_f(p["qty_entry"])
            _bind_f(p["price_entry"])
            _bind_f_sym(p["sym_entry"])

    def _sym_key_filter(self, event, pid=0):
        """sym_entry 只允许字母、导航键，热键控制qty"""
        nav_keys = {"BackSpace", "Delete", "Left", "Right", "Home", "End",
                    "Return", "Tab", "Escape", "Caps_Lock", "Shift_L", "Shift_R",
                    "Control_L", "Control_R", "Alt_L", "Alt_R"}

        ks    = event.keysym
        state = event.state

        # Windows 小键盘数字 keysym 是纯数字字符 "1"~"9","0"
        numpad_map = {
            "1":"1000","2":"2000","3":"3000","4":"4000","5":"5000",
            "6":"6000","7":"7000","8":"8000","9":"9000","0":"1000",
        }
        # Ctrl+1-9 → 100-900股
        ctrl_map = {"1":"100","2":"200","3":"300","4":"400","5":"500",
                    "6":"600","7":"700","8":"800","9":"900"}

        # Ctrl+1-9（优先判断）
        if state & 0x4 and ks in ctrl_map:
            self._set_qty(ctrl_map[ks], pid)
            return "break"

        # 小键盘/主键盘数字（无 Ctrl）→ 1000-9000
        if ks in numpad_map and not (state & 0x4):
            self._set_qty(numpad_map[ks], pid)
            return "break"

        # 导航键放行
        if ks in nav_keys:
            return

        # 字母放行
        if event.char and event.char.isalpha():
            return

        # 其余全拦截
        return "break"

    def _numpad_set_qty(self, val: str, pid=0):
        """小键盘设置 qty，返回 break 阻止字符写入输入框"""
        self._set_qty(val, pid)
        return "break"

    def _set_qty(self, val: str, pid=0):
        p = self.panels.get(pid, self.panels[0])
        p["qty_entry"].delete(0, "end")
        p["qty_entry"].insert(0, val)

    def _adj_qty(self, delta: int, pid=0):
        p = self.panels.get(pid, self.panels[0])
        try:
            cur = int(p["qty_entry"].get())
        except ValueError:
            cur = 0
        new = max(0, cur + delta)
        p["qty_entry"].delete(0, "end")
        p["qty_entry"].insert(0, str(new))
        return "break"

    def _adj_price(self, delta: float, pid=0):
        p = self.panels.get(pid, self.panels[0])
        try:
            cur = round(float(p["price_entry"].get()), 2)
        except ValueError:
            cur = 0.0
        new = round(max(0.0, cur + delta), 2)
        p["price_entry"].delete(0, "end")
        p["price_entry"].insert(0, f"{new:.2f}")
        return "break"

    def _tick_clock(self):
        self.time_var.set(datetime.datetime.now().strftime("%Y-%m-%d  %H:%M:%S"))
        self.after(1000, self._tick_clock)

    # ── Order type toggle ─────────────────────────────────────────────────────
    def _on_order_type_change(self, pid=0, _=None):
        p      = self.panels[pid]
        is_mkt = p["order_type_var"].get() == "Market"
        p["price_entry"].configure(state="disabled" if is_mkt else "normal",
                                   bg="#0d0f14" if is_mkt else "#1c2030")
        p["price_lbl"].configure(fg=TEXT_MUTED if is_mkt else TEXT_DIM)

    # ── Symbol ────────────────────────────────────────────────────────────────
    def _on_symbol_enter(self, pid=0, _=None):
        p   = self.panels[pid]
        sym = p["sym_var"].get().strip().upper()
        if not sym: return
        p["sym_var"].set(sym)
        p["current_sym"]    = sym
        p["order_sym_var"].set(sym)
        p["order_last_var"].set("")

        # 清空 price，重置 qty=100
        p["price_entry"].delete(0, "end")
        p["qty_entry"].delete(0, "end")
        p["qty_entry"].insert(0, "100")
        p["price_needs_fill"] = True
        self._ev_diag_done = False

        if self._stream_active:
            self.sub_queue.put(sym)
        if sym not in MOCK_BASE:
            MOCK_BASE[sym] = random.uniform(20, 500)
        if sym in self.current_quote:
            self._refresh_strip(self.current_quote[sym], None, pid)
        pass  # Selected sym — log suppressed

    # ── Login ─────────────────────────────────────────────────────────────────
    def _load_credentials(self):
        cfg = load_config()
        if cfg.get("username"): self.secret_entry.insert(0, cfg["username"])
        if cfg.get("password"): self.token_entry.insert(0, cfg["password"])

    def _do_login(self):
        s = self.secret_entry.get().strip()
        t = self.token_entry.get().strip()
        if not s or not t:
            messagebox.showwarning("提示","请填写 Username 和 Password"); return
        self._log("Connecting…","inf")
        def _bg():
            ok, msg = self.ts.login(s, t)
            self.after(0, lambda: self._post_login(ok, msg, s, t))
        threading.Thread(target=_bg, daemon=True).start()

    def _post_login(self, ok, msg, s="", t=""):
        self.status_var.set("● Connected" if ok else "● Not connected")
        self.status_lbl.config(fg=ACCENT_GREEN if ok else ACCENT_RED)
        if ok:
            if not self.ts.mock_mode and s and t:
                save_config({"username":s,"password":t})
                self._start_real_stream(s, t)  # s/t 仅保留接口兼容，实际用 proxy token
            self.after(600, self._refresh_positions)
            self.after(900, self._refresh_orders)
            pass  # diagnose_fills removed in proxy mode
        self._log(sanitize(msg), "ok" if ok else "err")

    def _diagnose_fills(self):
        """Find Filled orders and inspect fills field"""
        def _bg():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                async def _check():
                    return "Diagnostics disabled in proxy mode"
                    for o in orders:
                        try:
                            status = str(getattr(o, "status", "")).lower()
                            if "fill" not in status: continue
                            if not o.legs: continue
                            leg   = o.legs[0]
                            fills = getattr(leg, "fills", None)
                            fill_fields = ""
                            if fills:
                                f0 = fills[0]
                                fill_fields = str({k: getattr(f0, k, "N/A")
                                                   for k in dir(f0)
                                                   if not k.startswith("_")
                                                   and not callable(getattr(f0, k, None))})[:400]
                            return (f"sym={leg.symbol} status={o.status} "
                                    f"fills_count={len(fills) if fills else 0} "
                                    f"fill_fields={fill_fields or 'EMPTY'}")
                        except Exception as ex:
                            continue
                    return "No Filled orders found in history"
                result = loop.run_until_complete(_check())
                loop.close()
                pass  # Fill Diag suppressed
            except Exception as e:
                self.after(0, lambda _e=e: self._log(sanitize(f"[Diag Error] {_e}"), "err"))
        threading.Thread(target=_bg, daemon=True).start()

    # ── Place order ───────────────────────────────────────────────────────────
    def _place_order(self, action, pid=0):
        p   = self.panels[pid]
        sym = p["order_sym_var"].get()
        if sym == "—":
            messagebox.showwarning("Warning","Please select a symbol first"); return
        try:
            qty = int(p["qty_entry"].get())
        except ValueError:
            messagebox.showerror("Error","Please enter a valid quantity"); return
        is_mkt = p["order_type_var"].get() == "Market"
        price  = 0.0
        if not is_mkt:
            try:
                price = round(float(p["price_entry"].get().strip()), 2)
            except ValueError:
                messagebox.showerror("Error","Please enter a valid price"); return
            if price <= 0:
                messagebox.showerror("Error","Price must be greater than 0"); return
        tif       = p["tif_var"].get()
        price_str = "MKT" if is_mkt else f"${price:.2f}"
        self._log(f"{action} {qty} {sym} @ {price_str} | {tif}", "inf")
        def _bg():
            ok, msg = self.ts.place_order(sym, qty, price, action,
                                           "market" if is_mkt else "limit",
                                           tif=tif)
            self.after(0, lambda: self._log(sanitize(msg), "ok" if ok else "err"))
            if ok:
                self.after(1500, self._refresh_positions)
                self.after(1500, self._refresh_orders)
        threading.Thread(target=_bg, daemon=True).start()

    # ── Positions ─────────────────────────────────────────────────────────────
    def _refresh_positions(self):
        self.ts._pos_error = ""
        def _bg():
            pos = self.ts.get_today_activity()
            err = getattr(self.ts,"_pos_error","")
            self.after(0, lambda: self._update_positions(pos, err))
        threading.Thread(target=_bg, daemon=True).start()

    def _update_positions(self, positions, err=""):
        if err: self._log(sanitize(f"Position fetch failed: {err}"), "err")
        for r in self.pos_tree.get_children(): self.pos_tree.delete(r)
        tu = tr = 0.0
        for p in positions:
            sym     = p["symbol"]
            qty     = int(p["qty"])
            dirn    = p["direction"]
            is_long = dirn in ("Long", "L")
            avg     = p["avg_open"]
            cpx     = self.current_quote.get(sym,{}).get("last", p["close_px"])
            real    = p["realized_today"]

            if qty == 0:
                # Closed today — show with flat style, no unrealized
                tr += real
                self.pos_tree.insert("","end", iid=sym, tags=("flat",),
                    values=(sym,
                            int(p.get("qty_bot", 0)),
                            int(p.get("qty_sld", 0)),
                            0,
                            f"{avg:.4f}" if avg else "—",
                            f"{cpx:.2f}" if cpx else "—",
                            "—",
                            f"+{real:.2f}" if real>=0 else f"{real:.2f}",
                            p.get("exes", 0)))
            else:
                # Open position
                # 空头：Pos 显示负数，盈亏 = (avg - cpx) * qty
                display_qty = qty if is_long else -qty
                lu = round((cpx - avg) * qty * (1 if is_long else -1), 2)
                qty_bot = p.get("qty_bot", qty if is_long else 0)
                qty_sld = p.get("qty_sld", qty if not is_long else 0)
                exes    = p.get("exes", 0)
                tag     = "profit" if lu > 0 else ("loss" if lu < 0 else "flat")
                self.pos_tree.insert("","end", iid=sym, tags=(tag,),
                    values=(sym,
                            int(qty_bot), int(qty_sld), display_qty,
                            f"{avg:.4f}", f"{cpx:.2f}",
                            f"+{lu:.2f}" if lu>=0 else f"{lu:.2f}",
                            f"+{real:.2f}" if real>=0 else f"{real:.2f}",
                            exes))
                tu += lu
                tr += real

        self.total_unreal_var.set(f"+${tu:.2f}" if tu>=0 else f"-${abs(tu):.2f}")
        self.total_real_var.set(f"+${tr:.2f}"   if tr>=0 else f"-${abs(tr):.2f}")

        total_sh = 0
        for r in self.pos_tree.get_children():
            v = self.pos_tree.item(r, "values")
            if not v or len(v) < 3: continue
            try: total_sh += int(float(v[1] or 0)) + int(float(v[2] or 0))
            except: pass
        self.total_shares_var.set(f"{total_sh:,}")

    def _live_update_positions(self):
        tu = tr = 0.0
        for row in self.pos_tree.get_children():
            v = self.pos_tree.item(row,"values")
            if not v or len(v) < 9: continue
            sym, qty_bot, qty_sld, pos_s, avg_s, _, _, real_str, exes = v
            try:
                real = float(str(real_str).replace("+",""))
            except: real = 0.0
            # Closed position row — just add realized to total, skip price update
            if str(pos_s) in ("0", "-0", "0.0") or avg_s == "—":
                tr += real
                continue
            cpx = self.current_quote.get(sym,{}).get("last")
            if cpx is None:
                # 无实时行情，用 treeview 里已有的 last 值参与汇总
                try:
                    cpx = float(v[5]) if v[5] not in ("—", "", None) else None
                except: cpx = None
            if cpx is None:
                tr += real
                continue
            try:
                qty_signed = int(pos_s)          # 正=多头 负=空头
                abs_qty    = abs(qty_signed)
                avg        = float(avg_s)
                # 多头：(cpx-avg)*qty  空头：(avg-cpx)*qty
                if qty_signed >= 0:
                    lu = round((cpx - avg) * abs_qty, 2)
                else:
                    lu = round((avg - cpx) * abs_qty, 2)
            except:
                continue
            tu += lu; tr += real
            tag = "profit" if lu>0 else ("loss" if lu<0 else "flat")
            self.pos_tree.item(row, tags=(tag,),
                values=(sym, qty_bot, qty_sld, pos_s, avg_s, f"{cpx:.2f}",
                        f"+{lu:.2f}" if lu>=0 else f"{lu:.2f}", real_str, exes))
        self.total_unreal_var.set(f"+${tu:.2f}" if tu>=0 else f"-${abs(tu):.2f}")
        self.total_real_var.set(f"+${tr:.2f}"   if tr>=0 else f"-${abs(tr):.2f}")

        total_sh = 0
        for r in self.pos_tree.get_children():
            v = self.pos_tree.item(r, "values")
            if not v or len(v) < 3: continue
            try: total_sh += int(float(v[1] or 0)) + int(float(v[2] or 0))
            except: pass
        self.total_shares_var.set(f"{total_sh:,}")
    def _on_pos_select(self, _=None):
        sel = self.pos_tree.selection()
        if not sel: return
        # 点击持仓行，跳转到面板0
        self.panels[0]["sym_var"].set(sel[0])
        self._on_symbol_enter(0)

    # ── Orders ────────────────────────────────────────────────────────────────
    def _switch_order_mode(self, mode):
        self._order_mode.set(mode)
        for m, btn in self._order_tab_btns.items():
            btn.configure(bg=ACCENT_BLUE if m==mode else PANEL_BG,
                          fg=DARK_BG    if m==mode else TEXT_DIM)
        self._refresh_orders()

    def _refresh_orders(self):
        mode = self._order_mode.get()
        def _bg():
            orders = self.ts.get_orders(mode)
            self.after(0, lambda: self._update_orders(orders, mode))
        threading.Thread(target=_bg, daemon=True).start()

    def _update_orders(self, orders, mode):
        for r in self.ord_tree.get_children(): self.ord_tree.delete(r)
        if not orders:
            self.orders_count_var.set("No orders"); return
        self.orders_count_var.set(f"{len(orders)} order(s)")
        LIVE_ST = {"Received","Routing","In Flight","Live","Cancelling","Partially Filled"}
        for o in orders:
            rs        = o.get("raw_status", o["status"])
            is_active = any(s in rs for s in LIVE_ST)
            is_buy    = o["action"] == "BUY"
            tag = ("buy" if is_buy else "sell") if (mode=="live" or is_active) else "inactive"
            tif = o.get("tif", "Day")
            self.ord_tree.insert("","end", iid=o["id"], tags=(tag,),
                values=(o["symbol"], o["action"],
                        o["qty"], o["price"], o["otype"], tif, o["status"]))

    def _ord_right_click(self, e):
        row = self.ord_tree.identify_row(e.y)
        if row:
            self.ord_tree.selection_set(row)
            if self._order_mode.get() == "live":
                self._ord_menu.post(e.x_root, e.y_root)

    def _cancel_selected_order(self):
        sel = self.ord_tree.selection()
        if not sel: return
        oid = sel[0]
        def _bg():
            ok, msg = self.ts.cancel_order(oid)
            self.after(0, lambda: self._log(sanitize(msg), "ok" if ok else "err"))
            if ok: self.after(1000, self._refresh_orders)
        threading.Thread(target=_bg, daemon=True).start()

    # ── Quote strip update ────────────────────────────────────────────────────
    def _refresh_strip(self, q, prev=None, pid=None):
        sym = q["symbol"]
        for i, p in self.panels.items():
            if pid is not None and i != pid: continue
            if p["current_sym"] != sym: continue
            pl  = prev["last"] if prev else q["last"]
            chg = round(q["last"] - pl, 2)
            p["q_last_var"].set(f"{q['last']:.2f}")
            p["q_bid_var"].set(f"{q['bid']:.2f}")
            p["q_ask_var"].set(f"{q['ask']:.2f}")
            p["q_chg_var"].set(f"+{chg:.2f}" if chg>=0 else f"{chg:.2f}")
            p["q_vol_var"].set(f"{q['volume']:,}")
            p["order_last_var"].set(f"Last: ${q['last']:.2f}")
            if p.get("price_needs_fill", True):
                p["price_entry"].config(state="normal")
                p["price_entry"].delete(0, "end")
                p["price_entry"].insert(0, f"{q['ask']:.2f}")
                if p["order_type_var"].get() == "Market":
                    p["price_entry"].config(state="disabled")
                p["price_needs_fill"] = False

    def _on_server_disconnect(self):
        """服务器断线处理"""
        if not self.ts.connected: return  # 已经处理过了
        self.ts.connected  = False
        self._stream_active = False
        self._mock_active   = False
        self.status_var.set("● Not connected")
        self.status_lbl.config(fg=ACCENT_RED)
        self._log("Server disconnected", "err")

    # ── Log ───────────────────────────────────────────────────────────────────
    def _log(self, msg, tag="inf"):
        msg = sanitize(msg)
        ts  = datetime.datetime.now().strftime("%H:%M:%S")
        self.log_text.config(state="normal")
        self.log_text.insert("end", f"[{ts}]  {msg}\n", tag)
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    # ── Poll (150ms tick) — 只负责持仓刷新，行情由 after(0) 直推 ──────────
    def _poll(self):
        # mock 模式下仍从 queue 消费
        if self.ts.mock_mode:
            try:
                while True:
                    q    = self.quote_queue.get_nowait()
                    sym  = q["symbol"]
                    prev = self.current_quote.get(sym)
                    self.current_quote[sym] = q
                    self._refresh_strip(q, prev)
            except queue.Empty: pass

        now = time.time()
        if not hasattr(self,"_lpt"): self._lpt = 0
        if now-self._lpt > 3.0 and self.pos_tree.get_children():
            self._live_update_positions(); self._lpt = now

        if not hasattr(self,"_lat"): self._lat = 0
        if self.ts.connected and not self.ts.mock_mode and now-self._lat > 30.0:
            self._refresh_positions(); self._refresh_orders(); self._lat = now

        # 心跳检测：每10秒 ping 服务器，断线自动更新状态
        if not hasattr(self,"_hbt"): self._hbt = 0
        if self.ts.connected and not self.ts.mock_mode and now-self._hbt > 10.0:
            self._hbt = now
            def _ping():
                status, _ = self.ts._http("GET", "/health")
                if status == 0:  # 连接失败
                    self.after(0, self._on_server_disconnect)
            threading.Thread(target=_ping, daemon=True).start()

        self.after(150, self._poll)

    # ── Mock stream ───────────────────────────────────────────────────────────
    def _start_mock_stream(self):
        self._mock_active = True
        def _run():
            while self._mock_active:
                syms = set(p["current_sym"] for p in self.panels.values() if p["current_sym"])
                for sym in syms:
                    if sym not in MOCK_BASE: MOCK_BASE[sym] = random.uniform(20,500)
                    MOCK_BASE[sym] = round(MOCK_BASE[sym]+random.uniform(-0.2,0.2), 2)
                    self.quote_queue.put(mock_quote(sym, MOCK_BASE[sym]))
                time.sleep(0.5)
        threading.Thread(target=_run, daemon=True).start()

    # ── Real stream：连接代理服务器 WebSocket ────────────────────────────────
    def _start_real_stream(self, secret, token):
        self._mock_active   = False
        self._stream_active = True
        def _run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:    loop.run_until_complete(self._proxy_stream())
            except Exception as e:
                self.after(0, lambda: self._log(sanitize(f"Stream disconnected: {e}"), "err"))
                self._stream_active = False
                # 心跳会检测并更新状态，这里不进入 mock 模式
            finally:
                try: loop.close()
                except: pass
        threading.Thread(target=_run, daemon=True).start()

    async def _proxy_stream(self):
        """连接代理服务器 WebSocket 接收行情"""
        import websockets
        uri = f"ws://{PROXY_HOST}:{PROXY_PORT}/quotes?token={self.ts._token}"
        subscribed = set()

        async with websockets.connect(uri) as ws:
            self.after(0, lambda: self._log("行情Connected ◉ LIVE", "ok"))

            async def sub_watcher():
                """监听订阅/取消订阅请求，发给服务器"""
                while self._stream_active:
                    # 当前两个面板的活跃标的
                    active = {p["current_sym"] for p in self.panels.values()
                              if p["current_sym"]}
                    # 队列里的新订阅
                    while not self.sub_queue.empty():
                        try: active.add(self.sub_queue.get_nowait())
                        except: break

                    # 取消不再需要的标的
                    to_unsub = subscribed - active
                    if to_unsub:
                        await ws.send(json.dumps({"action":"unsubscribe","symbols":list(to_unsub)}))
                        subscribed.difference_update(to_unsub)

                    # 订阅新增标的
                    to_sub = active - subscribed
                    if to_sub:
                        await ws.send(json.dumps({"action":"subscribe","symbols":list(to_sub)}))
                        subscribed.update(to_sub)

                    await asyncio.sleep(0.1)

            async def recv_loop():
                """接收行情推送"""
                async for msg in ws:
                    if not self._stream_active: break
                    try:
                        q_raw = json.loads(msg)
                        sym   = q_raw.get("symbol","")
                        bid   = float(q_raw.get("bid", 0))
                        ask   = float(q_raw.get("ask", 0))
                        last  = float(q_raw.get("last", 0))
                        vol   = int(q_raw.get("volume", 0))
                        if not sym or (bid == 0 and ask == 0): continue
                        q = dict(symbol=sym, bid=bid, ask=ask, last=last, volume=vol,
                                 timestamp=datetime.datetime.now().strftime("%H:%M:%S"))
                        prev = self.current_quote.get(sym)
                        self.current_quote[sym] = q
                        self.after(0, lambda _q=q, _p=prev: self._refresh_strip(_q, _p))
                    except: continue

            await asyncio.gather(sub_watcher(), recv_loop())

    # ── Close ─────────────────────────────────────────────────────────────────
    def on_close(self):
        self._mock_active   = False
        self._stream_active = False
        self.destroy()


if __name__ == "__main__":
    app = TradingTerminal()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()
