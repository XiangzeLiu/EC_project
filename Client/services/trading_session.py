"""
Trading Session
核心业务逻辑层：认证、持仓查询、订单管理、下单、撤单
不包含任何UI代码，纯数据处理
"""

import datetime
import re
import threading
import time
from decimal import Decimal

try:
    from zoneinfo import ZoneInfo
except ImportError:
    try:
        from backports.zoneinfo import ZoneInfo
    except ImportError:
        import datetime as _dt

        class ZoneInfo:
            _OFFSETS = {"America/New_York": -5, "UTC": 0}

            def __init__(self, key: str):
                self._key = key
                self._offset = _dt.timedelta(hours=self._OFFSETS.get(key, 0))

            def utcoffset(self, dt): return self._offset
            def tzname(self, dt): return self._key
            def fromutc(self, dt): return dt + self._offset
            def __repr__(self): return f"ZoneInfo('{self._key}')"

from ..network.http_client import HttpClient
from ..constants import STATUS_MAP, LIVE_STATUSES, TZ_ET_NAME, SESSION_START_H, SESSION_END_H


# ── Sanitize ────────────────────────────────────────────────────────────────────
_BROKER_RE = re.compile(r'\b(tastytrade|tastyworks|tastytrade\.com|tastyworks\.com)\b', re.I)


def sanitize(text: str) -> str:
    """过滤日志中的敏感信息"""
    return _BROKER_RE.sub("broker", str(text))


class TradingSession:
    """
    交易会话管理器
    封装所有与服务器交互的业务逻辑
    """

    def __init__(self, http_client: HttpClient):
        self.http = http_client
        self.connected = False
        self.mock_mode = False
        self._ET = ZoneInfo(TZ_ET_NAME)
        self._pos_error = ""

    # ── Auth ────────────────────────────────────────────────────────────────────

    def login(self, username: str, password: str) -> tuple[bool, str]:
        """
        用户登录认证
        临时测试账号: test_name / test_password

        Returns:
            (success, message) 元组
        """
        # ── 临时测试凭据校验 ──
        _TEST_USER = "test_name"
        _TEST_PASS = "test_password"
        if username == _TEST_USER and password == _TEST_PASS:
            self.http.token = "mock_token_" + username
            self.connected = True
            self.mock_mode = True
            return True, "Connected (demo mode)"

        # ── 真实服务器认证 ──
        status, resp = self.http.post("/auth/login", {
            "username": username,
            "password": password,
        })
        if status == 200:
            self.http.token = resp.get("token", "")
            self.connected = True
            self.mock_mode = False
            return True, "Connected"
        if status == 0:
            return False, "Server not available, please check server is running"
        msg = resp.get("detail", f"Login failed (HTTP {status})")
        return False, sanitize(msg)

    def logout(self):
        """登出"""
        if self.connected:
            self.http.post("/auth/logout", {})
        self.http.token = ""
        self.connected = False
        self.mock_mode = False

    # ── Positions (Today Activity) ─────────────────────────────────────────────

    def get_today_activity(self) -> list[dict]:
        """
        获取今日活动数据(持仓+已平仓)

        Returns:
            持仓字典列表，每个包含 symbol, qty, direction, avg_open,
            close_px, unrealized, realized_today, qty_bot, qty_sld, exes
        """
        if self.mock_mode:
            return self._mock_positions()
        if not self.connected:
            return []
        try:
            _, pos_resp = self.http.get("/positions")
            pos_rows = pos_resp.get("positions", [])
            _, ord_resp = self.http.get("/orders/history")
            orders_raw = ord_resp.get("orders", [])
            return self._calc_today_activity(pos_rows, orders_raw)
        except Exception as e:
            self._pos_error = sanitize(str(e))
            return []

    def _mock_positions(self) -> list[dict]:
        """模拟模式下的预定义持仓数据"""
        return [
            dict(symbol="AAPL", qty=100, direction="Long", avg_open=185.20,
                 close_px=189.42, unrealized=422.0, realized_today=155.0),
            dict(symbol="BIL", qty=0, direction="—", avg_open=91.56,
                 close_px=91.56, unrealized=0.0, realized_today=-8.0),
            dict(symbol="NVDA", qty=50, direction="Long", avg_open=890.00,
                 close_px=875.20, unrealized=-740.0, realized_today=-120.0),
        ]

    def _calc_today_activity(self, pos_rows_raw: list, orders_raw: list) -> list[dict]:
        """
        从原始数据计算今日持仓和P&L
        多空分离计算：多头(Buy to Open/Sell to Close)、空头(Sell to Open/Buy to Close)
        """
        pos_map = {}

        # Step 1: open positions
        for p in pos_rows_raw:
            try:
                sym = p.get("symbol", "")
                qty = float(p.get("quantity", 0))
                avg = float(p.get("average_open_price", 0) or 0)
                cpx = float(p.get("close_price", 0) or 0)
                dirn = p.get("direction", "Long")
                real = float(p.get("realized_today", 0) or 0)
                pos_map[sym] = dict(
                    symbol=sym, qty=qty, direction=dirn,
                    avg_open=avg, close_px=cpx,
                    unrealized=round((cpx - avg) * qty * (1 if dirn == "Long" else -1), 2),
                    realized_today=real,
                    qty_bot=0, qty_sld=0, exes=0,
                )
            except Exception:
                continue

        # Step 2: scan today's order history for fills
        ET = self._ET
        now_et = datetime.datetime.now(ET)
        today_et = now_et.date()
        session_start = datetime.datetime.combine(today_et, datetime.time(SESSION_START_H, 0), tzinfo=ET)
        session_end = datetime.datetime.combine(today_et, datetime.time(SESSION_END_H, 0), tzinfo=ET)
        ledger = {}

        try:
            for o in orders_raw:
                try:
                    status = str(o.get("status", "") if isinstance(o, dict) else getattr(o, "status", "")).lower()
                    if "fill" not in status:
                        continue
                    legs = o.get("legs", []) if isinstance(o, dict) else getattr(o, "legs", [])
                    if not legs:
                        continue

                    # 时间过滤
                    o_ts_str = ""
                    if isinstance(o, dict):
                        o_ts_str = o.get("updated_at") or o.get("created_at") or ""
                    try:
                        o_ts = datetime.datetime.fromisoformat(o_ts_str.replace("Z", "+00:00")) if o_ts_str else None
                    except Exception:
                        o_ts = None

                    if o_ts:
                        if hasattr(o_ts, "tzinfo") and o_ts.tzinfo is None:
                            o_ts = o_ts.replace(tzinfo=datetime.timezone.utc)
                        o_ts_et = o_ts.astimezone(ET)
                        if not (session_start <= o_ts_et <= session_end):
                            continue

                    for leg in legs:
                        sym = leg.get("symbol", "") if isinstance(leg, dict) else leg.symbol
                        act = str(leg.get("action", "") if isinstance(leg, dict) else leg.action)

                        is_buy_to_open = "Buy" in act and "Open" in act
                        is_sell_to_close = "Sell" in act and "Close" in act
                        is_sell_to_open = "Sell" in act and "Open" in act
                        is_buy_to_close = "Buy" in act and "Close" in act

                        leg_qty = float(leg.get("quantity", 0) if isinstance(leg, dict) else getattr(leg, "quantity", 0) or 0)
                        fills = (leg.get("fills", []) if isinstance(leg, dict) else getattr(leg, "fills", [])) or []

                        def record(fqty: float, fp: float):
                            if fqty <= 0 or fp <= 0:
                                return
                            if sym not in ledger:
                                ledger[sym] = {
                                    "long_buys": [], "long_sells": [],
                                    "short_sells": [], "short_buys": [],
                                    "exes": 0,
                                }
                            ledger[sym]["exes"] += 1
                            if is_buy_to_open:
                                ledger[sym]["long_buys"].append((fqty, fp))
                            elif is_sell_to_close:
                                ledger[sym]["long_sells"].append((fqty, fp))
                            elif is_sell_to_open:
                                ledger[sym]["short_sells"].append((fqty, fp))
                            elif is_buy_to_close:
                                ledger[sym]["short_buys"].append((fqty, fp))

                        if fills:
                            for fill in fills:
                                try:
                                    if isinstance(fill, dict):
                                        fp = float(fill.get("fill_price", 0) or 0)
                                        fqty = float(fill.get("quantity", 0) or leg_qty)
                                        fat_s = fill.get("filled_at", "")
                                    else:
                                        fp = float(getattr(fill, "fill_price", 0) or 0)
                                        fqty = leg_qty / len(fills) if len(fills) > 1 else leg_qty
                                        fat = getattr(fill, "filled_at", None)
                                        if fat:
                                            fat_s = str(fat)
                                        else:
                                            fat_s = ""

                                    if fat_s:
                                        try:
                                            fat = datetime.datetime.fromisoformat(fat_s.replace("Z", "+00:00"))
                                        except Exception:
                                            fat = None
                                        if fat:
                                            if hasattr(fat, "tzinfo") and fat.tzinfo is None:
                                                fat = fat.replace(tzinfo=datetime.timezone.utc)
                                            fat_et = fat.astimezone(ET)
                                            if not (session_start <= fat_et <= session_end):
                                                continue
                                    record(fqty, fp)
                                except Exception:
                                    continue
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

            # 多头部分
            long_buys = trades["long_buys"]
            long_sells = trades["long_sells"]
            lbq = sum(q for q, _ in long_buys)
            lbc = sum(q * p for q, p in long_buys)
            long_avg = lbc / lbq if lbq > 0 else 0
            lsq = sum(q for q, _ in long_sells)
            lsp = sum(q * p for q, p in long_sells)
            long_realized = round(lsp - long_avg * lsq, 2) if long_avg > 0 else 0

            # 空头部分
            short_sells = trades["short_sells"]
            short_buys = trades["short_buys"]
            ssq = sum(q for q, _ in short_sells)
            ssc = sum(q * p for q, p in short_sells)
            short_avg = ssc / ssq if ssq > 0 else 0
            sbq = sum(q for q, _ in short_buys)
            sbp = sum(q * p for q, p in short_buys)
            short_realized = round((short_avg * sbq) - sbp, 2) if short_avg > 0 else 0

            realized = round(long_realized + short_realized, 2)

            qty_bot = lbq + sbq
            qty_sld = lsq + ssq

            if sym in pos_map:
                pos_map[sym]["qty_bot"] = qty_bot
                pos_map[sym]["qty_sld"] = qty_sld
                pos_map[sym]["exes"] = exes
                if pos_map[sym]["realized_today"] == 0 and realized != 0:
                    pos_map[sym]["realized_today"] = realized
            else:
                display_avg = long_avg if long_avg > 0 else short_avg
                pos_map[sym] = dict(
                    symbol=sym, qty=0, direction="—",
                    avg_open=round(display_avg, 4), close_px=0.0,
                    unrealized=0.0, realized_today=realized,
                    qty_bot=qty_bot, qty_sld=qty_sld, exes=exes,
                )

        return list(pos_map.values())

    # ── Orders ──────────────────────────────────────────────────────────────────

    def get_orders(self, mode: str = "live") -> list[dict]:
        """
        获取订单列表

        Args:
            mode: "live" 获取活跃订单 / "all" 获取所有订单

        Returns:
            订单字典列表
        """
        if self.mock_mode or not self.connected:
            return []
        try:
            path = "/orders/live" if mode == "live" else "/orders/history"
            _, resp = self.http.get(path)
            raw = resp.get("orders", [])
            result = []

            ET = self._ET
            for o in raw:
                try:
                    if mode == "all":
                        o_ts_str = o.get("updated_at", "")
                        if o_ts_str:
                            try:
                                o_ts = datetime.datetime.fromisoformat(o_ts_str.replace("Z", "+00:00"))
                                et_today = datetime.datetime.now(ET).date()
                                s_start = datetime.datetime.combine(et_today, datetime.time(SESSION_START_H, 0), tzinfo=ET)
                                s_end = datetime.datetime.combine(et_today, datetime.time(SESSION_END_H, 0), tzinfo=ET)
                                if not (s_start <= o_ts.astimezone(ET) <= s_end):
                                    continue
                            except Exception:
                                pass

                    sym = o.get("symbol", "—")
                    act = "BUY" if "Buy" in o.get("action", "") else "SELL"
                    qty = str(o.get("qty", "—"))
                    px = o.get("price", "MKT")
                    rs = o.get("status", "—")
                    st = STATUS_MAP.get(rs, rs)
                    ot = o.get("type", "—")
                    tif = o.get("tif", "—")

                    if mode == "live" and rs not in LIVE_STATUSES:
                        continue

                    result.append(dict(
                        id=o.get("id", ""),
                        symbol=sym, action=act,
                        qty=qty, price=px, status=st,
                        raw_status=rs, otype=ot, tif=tif,
                    ))
                except Exception:
                    continue
            return result
        except Exception:
            return []

    def cancel_order(self, order_id: str) -> tuple[bool, str]:
        """撤销订单"""
        if not self.connected:
            return False, "Not connected"
        try:
            status, resp = self.http.delete(f"/orders/{order_id}")
            if status in (200, 201, 204):
                return True, f"Order {str(order_id)[-6:]} cancelled"
            return False, sanitize(resp.get("detail", f"Cancel failed (HTTP {status})"))
        except Exception as e:
            return False, sanitize(f"撤单失败: {e}")

    def place_order(self, symbol: str, qty: int, price: float,
                    action: str, order_type: str = "limit", tif: str = "Day") -> tuple[bool, str]:
        """
        下单

        Args:
            symbol: 股票代码
            qty: 数量
            price: 价格(Market单为0)
            action: 动作 (Buy to Open/Sell to Close等)
            order_type: limit/market
            tif: Time In Force

        Returns:
            (success, message) 元组
        """
        if self.mock_mode:
            time.sleep(0.3)
            price_str = "Market" if order_type == "market" else f"${price}"
            return True, f"[SIM] {action} {qty} {symbol} @ {price_str} | {tif}"
        if not self.connected:
            return False, "Not connected"
        try:
            status, resp = self.http.post("/orders/place", {
                "symbol": symbol,
                "qty": qty,
                "price": price,
                "action": action,
                "order_type": order_type,
                "tif": tif,
            })
            if status in (200, 201):
                oid = resp.get("order_id", "")
                return True, f"Order submitted — ID: {str(oid)[-8:]}"
            return False, sanitize(resp.get("detail", f"Order failed (HTTP {status})"))
        except Exception as e:
            return False, sanitize(f"Order failed: {e}")

    def enable_mock_mode(self):
        """启用模拟模式"""
        self.mock_mode = True
        self.connected = True
