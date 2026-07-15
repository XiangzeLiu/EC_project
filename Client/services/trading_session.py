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
from ..network.ts_websocket import TSWebSocketClient
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
        self.broker_gate = self._default_broker_gate()
        # 登录后从 SM 获取的 TS 地址
        self.se_address: str = ""

        # TS 直连客户端（由 UI 在连上/断开时绑定）
        self._se_client: TSWebSocketClient | None = None

    def bind_se_client(self, se_client: TSWebSocketClient | None):
        """绑定/解绑 SE 直连客户端"""
        self._se_client = se_client

    def _can_use_se(self) -> bool:
        return bool(self._se_client and self._se_client.is_connected)

    def _request_se(self, msg_type: str, payload: dict, timeout: float = 10.0) -> dict | None:
        if not self._can_use_se():
            return None
        try:
            return self._se_client.request_sync(msg_type, payload, timeout=timeout)
        except Exception:
            return None

    @staticmethod
    def _default_broker_gate() -> dict:
        return {
            "active": False,
            "status": "not_logged_in",
            "username": "",
            "server_id": "",
            "account_username": "",
            "grace_remaining": 0,
            "updated_at": 0,
        }

    def _normalize_broker_gate(self, gate: dict | None = None) -> dict:
        base = self._default_broker_gate()
        if isinstance(gate, dict):
            base.update({
                "active": bool(gate.get("active", False)),
                "status": gate.get("status", base["status"]),
                "username": gate.get("username", base["username"]),
                "server_id": gate.get("server_id", base["server_id"]),
                "account_username": gate.get("account_username", base["account_username"]),
                "grace_remaining": gate.get("grace_remaining", 0),
                "updated_at": gate.get("updated_at", 0),
            })
        try:
            base["grace_remaining"] = max(0, int(base.get("grace_remaining") or 0))
        except Exception:
            base["grace_remaining"] = 0
        try:
            base["updated_at"] = int(base.get("updated_at") or 0)
        except Exception:
            base["updated_at"] = 0
        base["active"] = bool(base.get("active"))
        base["status"] = str(base.get("status") or "not_logged_in")
        base["username"] = str(base.get("username") or "")
        base["server_id"] = str(base.get("server_id") or "")
        base["account_username"] = str(base.get("account_username") or "")
        return base

    def _set_broker_gate(self, gate: dict | None = None) -> dict:
        self.broker_gate = self._normalize_broker_gate(gate)
        return self.broker_gate

    @property
    def broker_gate_active(self) -> bool:
        return bool(self.broker_gate.get("active"))

    def _broker_gate_message(self) -> str:
        status = str(self.broker_gate.get("status") or "not_logged_in")
        if status == "grace_pending":
            return f"交易服务登录等待重连（剩余{self.broker_gate.get('grace_remaining', 0)}秒）"
        if status == "expired":
            return "交易服务登录已过期"
        return "请先登录交易服务"

    def can_trade(self) -> bool:
        return bool(self.connected and self._can_use_se() and self.broker_gate_active)

    def broker_login(
        self,
        account_username: str,
        account_password: str,
        challenge_token: str = "",
        otp: str = "",
    ) -> tuple[bool, str, dict]:
        account_username = (account_username or "").strip()
        account_password = account_password or ""
        if not account_username or not account_password:
            return False, "Broker username and password are required", {}
        request_payload = {
            "account_username": account_username,
            "account_password": account_password,
        }
        if challenge_token:
            request_payload["challenge_token"] = challenge_token
        if otp:
            request_payload["otp"] = otp
        resp = self._request_se("BROKER_LOGIN", request_payload, timeout=20.0)
        if not isinstance(resp, dict):
            return False, "Broker login request timed out", {}
        payload = resp.get("payload", {}) if isinstance(resp.get("payload", {}), dict) else {}
        gate = payload.get("gate")
        if isinstance(gate, dict):
            self._set_broker_gate(gate)
        ok = bool(payload.get("success"))
        return ok, sanitize(payload.get("message", "ok")), payload

    def broker_status_query(self) -> tuple[bool, dict, str]:
        if not self._can_use_se():
            return False, self.broker_gate, "交易服务器未连接"
        resp = self._request_se("BROKER_STATUS_QUERY", {}, timeout=8.0)
        if not isinstance(resp, dict):
            return False, self.broker_gate, "交易服务状态查询超时"
        payload = resp.get("payload", {}) if isinstance(resp.get("payload", {}), dict) else {}
        gate = payload.get("gate")
        if isinstance(gate, dict):
            self._set_broker_gate(gate)
        ok = bool(payload.get("success", True))
        return ok, self.broker_gate, sanitize(payload.get("message", "ok"))

    def broker_logout(self) -> tuple[bool, str]:
        if not self._can_use_se():
            self._set_broker_gate(None)
            return True, "交易服务登录已清除"
        resp = self._request_se("BROKER_LOGOUT", {}, timeout=8.0)
        if not isinstance(resp, dict):
            return False, "交易服务登出请求超时"
        payload = resp.get("payload", {}) if isinstance(resp.get("payload", {}), dict) else {}
        gate = payload.get("gate")
        if isinstance(gate, dict):
            self._set_broker_gate(gate)
        else:
            self._set_broker_gate(None)
        ok = bool(payload.get("success", True))
        return ok, sanitize(payload.get("message", "ok"))

    def subscribe_quotes(self, symbols: list[str], timeout: float = 6.0) -> tuple[bool, str]:
        """通过 SE 订阅行情"""
        if not self._can_use_se():
            return False, "交易服务器未连接"
        resp = self._request_se("QUOTE_SUBSCRIBE", {
            "action": "subscribe",
            "symbols": symbols,
        }, timeout=timeout)
        payload = (resp or {}).get("payload", {}) if isinstance(resp, dict) else {}
        if payload.get("success"):
            return True, sanitize(payload.get("message", "行情订阅成功"))
        return False, sanitize(payload.get("message", "行情订阅失败"))

    def unsubscribe_quotes(self, symbols: list[str], timeout: float = 6.0) -> tuple[bool, str]:
        """通过 SE 取消行情订阅"""
        if not self._can_use_se():
            return False, "交易服务器未连接"
        resp = self._request_se("QUOTE_SUBSCRIBE", {
            "action": "unsubscribe",
            "symbols": symbols,
        }, timeout=timeout)
        payload = (resp or {}).get("payload", {}) if isinstance(resp, dict) else {}
        if payload.get("success"):
            return True, sanitize(payload.get("message", "行情取消订阅成功"))
        return False, sanitize(payload.get("message", "行情取消订阅失败"))

    # ── Auth ────────────────────────────────────────────────────────────────────



    def login(self, username: str, password: str, force: bool = False) -> tuple[bool, str]:
        """
        鐢ㄦ埛鐧诲綍璁よ瘉 鈥?閫氳繃 Server_manager REST 鎺ュ彛楠岃瘉

        Returns:
            (success, message) 鍏冪粍
        """
        status, resp = self.http.post("/auth/login", {
            "username": username,
            "password": password,
            "force": bool(force),
        })
        if status == 200:
            self.http.token = resp.get("token", "")
            self.se_address = resp.get("se_address", "") or ""
            self._set_broker_gate(None)
            self.connected = True
            self.mock_mode = False
            return True, "已连接"
        if status == 0:
            return False, "服务不可用，请检查服务是否已启动"

        detail = resp.get("detail", f"Login failed (HTTP {status})")
        if isinstance(detail, dict):
            msg = detail.get("message") or detail.get("detail") or f"Login failed (HTTP {status})"
        else:
            msg = detail
        return False, sanitize(msg)

    def logout(self):
        """鐧诲嚭"""
        if self.connected:
            self.http.post("/auth/logout", {})
        self.http.token = ""
        self._set_broker_gate(None)
        self.connected = False
        self.mock_mode = False

    def get_today_activity(self) -> list[dict]:
        """
        鑾峰彇浠婃棩娲诲姩鏁版嵁(鎸佷粨+宸插钩浠?

        Returns:
            鎸佷粨瀛楀吀鍒楄〃锛屾瘡涓寘鍚?symbol, qty, direction, avg_open,
            close_px, unrealized, realized_today, qty_bot, qty_sld, exes
        """
        if self.mock_mode:
            return self._mock_positions()
        if not self.connected:
            return []
        try:
            if not self.can_trade():
                self._pos_error = self._broker_gate_message() if self._can_use_se() else "交易服务器未连接"
                return []

            resp_pos = self._request_se("POSITION_QUERY", {}, timeout=12.0)
            if not isinstance(resp_pos, dict):
                self._pos_error = "\u4ea4\u6613\u670d\u52a1\u8bf7\u6c42\u8d85\u65f6\u6216\u5238\u5546\u670d\u52a1\u4e0d\u53ef\u7528"
                return []

            payload_pos = resp_pos.get("payload", {}) or {}
            if not payload_pos.get("success"):
                err_code = payload_pos.get("code", "") or payload_pos.get("error_code", "")
                if err_code == "BROKER_LOGIN_REQUIRED":
                    self._pos_error = self._broker_gate_message()
                elif err_code == "BROKER_OFFLINE":
                    self._pos_error = "\u4ea4\u6613\u670d\u52a1\u672a\u767b\u5f55"
                elif err_code == "NO_BROKER":
                    self._pos_error = "\u672a\u52a0\u8f7d\u5238\u5546\u914d\u7f6e"
                else:
                    self._pos_error = sanitize(payload_pos.get("message", "持仓查询失败"))
                return []

            pos_rows = payload_pos.get("positions", []) or []
            orders_raw = []
            resp_ord = self._request_se("ORDER_QUERY", {"mode": "all"}, timeout=12.0)
            if isinstance(resp_ord, dict):
                payload_ord = resp_ord.get("payload", {}) or {}
                if payload_ord.get("success"):
                    orders_raw = payload_ord.get("orders", []) or []

            return self._calc_today_activity(pos_rows, orders_raw)
        except Exception as exc:
            self._pos_error = sanitize(str(exc))
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
        鑾峰彇璁㈠崟鍒楄〃

        Args:
            mode: "live" 鑾峰彇娲昏穬璁㈠崟 / "all" 鑾峰彇鎵€鏈夎鍗?

        Returns:
            璁㈠崟瀛楀吀鍒楄〃
        """
        if self.mock_mode or not self.connected:
            return []
        try:
            if not self.can_trade():
                return []

            se_mode = "live" if mode == "live" else "all"
            se_resp = self._request_se("ORDER_QUERY", {"mode": se_mode}, timeout=12.0)
            payload = (se_resp or {}).get("payload", {}) if isinstance(se_resp, dict) else {}
            if not payload.get("success"):
                return []
            raw = payload.get("orders", []) or []

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

                    sym = o.get("symbol", "鈥?")
                    act = "BUY" if "Buy" in o.get("action", "") else "SELL"
                    qty = str(o.get("qty", "鈥?"))
                    px = o.get("price", "MKT")
                    rs = o.get("status", "鈥?")
                    st = STATUS_MAP.get(rs, rs)
                    ot = o.get("type", "鈥?")
                    tif = o.get("tif", "鈥?")

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
        """鎾ら攢璁㈠崟"""
        if not self.connected:
            return False, "未连接"
        try:
            if not self.can_trade():
                return False, self._broker_gate_message() if self._can_use_se() else "交易服务器未连接"

            resp = self._request_se("ORDER_CANCEL", {"order_id": order_id}, timeout=10.0)
            payload = (resp or {}).get("payload", {}) if isinstance(resp, dict) else {}
            if payload.get("success"):
                return True, f"订单已撤销：{str(order_id)[-6:]}"
            return False, sanitize(payload.get("message", "撤单失败"))
        except Exception as exc:
            return False, sanitize(f"鎾ゅ崟澶辫触: {exc}")

    def place_order(self, symbol: str, qty: int, price: float,
                    action: str, order_type: str = "limit", tif: str = "Day") -> tuple[bool, str]:
        """
        涓嬪崟

        Args:
            symbol: 鑲＄エ浠ｇ爜
            qty: 鏁伴噺
            price: 浠锋牸(Market鍗曚负0)
            action: 鍔ㄤ綔 (Buy to Open/Sell to Close绛?
            order_type: limit/market
            tif: Time In Force

        Returns:
            (success, message) 鍏冪粍
        """
        if self.mock_mode:
            time.sleep(0.3)
            price_str = "Market" if order_type == "market" else "$" + str(price)
            return True, f"[模拟] {action} {qty} {symbol} @ {price_str} | {tif}"
        if not self.connected:
            return False, "未连接"
        try:
            if not self.can_trade():
                return False, self._broker_gate_message() if self._can_use_se() else "交易服务器未连接"

            resp = self._request_se("ORDER_SUBMIT", {
                "symbol": symbol,
                "qty": qty,
                "price": price,
                "action": action,
                "order_type": order_type,
                "tif": tif,
            }, timeout=12.0)
            payload = (resp or {}).get("payload", {}) if isinstance(resp, dict) else {}
            if payload.get("success"):
                oid = payload.get("order_id", "")
                return True, f"下单已提交，订单号：{str(oid)[-8:]}"
            return False, sanitize(payload.get("message", "下单失败"))
        except Exception as exc:
            return False, sanitize(f"下单失败：{exc}")

    def enable_mock_mode(self):
        """启用模拟模式"""
        self.mock_mode = True
        self.connected = True
