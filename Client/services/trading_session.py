"""
Trading Session
核心业务逻辑层：认证、持仓查询、订单管理、下单、撤单
不包含任何UI代码，纯数据处理
"""

import datetime
import re
import threading
import time
from dataclasses import dataclass, field
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


ALL_ORDERS_CACHE_SECONDS = 30.0


@dataclass(slots=True)
class QueryResult:
    success: bool
    data: list[dict] = field(default_factory=list)
    error_code: str = ""
    message: str = ""

# ── User-visible message sanitization ──────────────────────────────────────────
_TRADING_PROVIDER_RE = re.compile(
    r"\b(?:tastytrade|tastyworks|interactive(?:\s+|_)brokers?|ibkr|ib|tt|ib\s+gateway|gateway|tws)\b",
    re.I,
)
_URL_RE = re.compile(r"\b(?:https?|wss?)://[^\s\]\[<>()]+", re.I)
_IP_PORT_RE = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?(?![\w.])")
_IPV6_RE = re.compile(r"\[(?=[0-9a-f:]*:)[0-9a-f:]+\](?::\d{1,5})?", re.I)
_DOMAIN_RE = re.compile(
    r"(?<![\w@])(?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d{1,5})?(?:/[\w./?%&=+-]*)?",
    re.I,
)
_WINDOWS_PATH_RE = re.compile(r"\b[a-z]:\\[^\r\n]+", re.I)
_INTERNAL_ID_RE = re.compile(
    r"\b(?:sess|trc|conn|connection|session|trace)[-_:= ]+[a-z0-9_-]+\b",
    re.I,
)
_ACCOUNT_ID_RE = re.compile(r"\bU\d{5,}\b", re.I)
_LABELED_ACCOUNT_RE = re.compile(r"\b(?:account|acct|client_id|账户)[\s:=#-]+[a-z0-9-]+\b", re.I)


_TECHNICAL_ERROR_RE = re.compile(
    r"(?:traceback|exception|error|warning|failed|failure|timeout|timed out|connection|"
    r"websocket|http|urlopen|jsondecode|json decode|none ?type|"
    r"attributeerror|valueerror|keyerror|typeerror|oserror|winerror|"
    r"errno|ssl|socket|name resolution|connection refused|connection reset|"
    r"object has no attribute|invalid literal)",
    re.I,
)


def sanitize(text: str) -> str:
    """Remove provider, infrastructure, and internal identifiers from UI text."""
    value = str(text or "")
    value = _TRADING_PROVIDER_RE.sub("交易服务", value)
    value = _URL_RE.sub("[服务地址]", value)
    value = _IP_PORT_RE.sub("[服务地址]", value)
    value = _IPV6_RE.sub("[服务地址]", value)
    value = _DOMAIN_RE.sub("[服务地址]", value)
    value = _WINDOWS_PATH_RE.sub("[本地路径]", value)
    value = _INTERNAL_ID_RE.sub("[内部标识]", value)
    value = _LABELED_ACCOUNT_RE.sub("[账户]", value)
    value = _ACCOUNT_ID_RE.sub("[账户]", value)
    return value


def safe_user_message(text: str, fallback: str = "操作失败，请稍后重试") -> str:
    """Return readable business text without leaking technical exceptions."""
    value = sanitize(text).strip()
    if not value or _TECHNICAL_ERROR_RE.search(value):
        return fallback
    return value


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
        self._order_query_error: dict[str, str] = {}
        self._order_cache_lock = threading.Lock()
        self._order_fetch_lock = threading.Lock()
        self._order_cache_generation = 0
        self._order_cache_fetch_serial = 0
        self._all_orders_cache: list[dict] = []
        self._all_orders_cache_at = 0.0
        self._symbol_order_options_lock = threading.Lock()
        self._symbol_order_options: dict[str, dict] = {}
        self.last_login_error: dict = {}
        self.auth_expires_in: int = 0
        self.auth_deadline_monotonic: float = 0.0
        self.broker_detail = self._default_broker_detail()
        # 登录后从 SM 获取的 TS 地址
        self.se_address: str = ""

        # TS 直连客户端（由 UI 在连上/断开时绑定）
        self._se_client: TSWebSocketClient | None = None

    def bind_se_client(self, se_client: TSWebSocketClient | None):
        """绑定/解绑 SE 直连客户端"""
        if se_client is None or se_client is not self._se_client:
            self.clear_symbol_order_options()
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
    def _default_broker_detail() -> dict:
        return {
            "connected": False,
            "capabilities": {
                "quotes": False,
                "orders": False,
                "cancel_order": False,
                "positions": False,
                "order_query": False,
            },
            "read_only": False,
            "account": {},
            "order_options": {},
            "error": {},
        }

    def _normalize_broker_detail(self, detail: dict | None = None) -> dict:
        base = self._default_broker_detail()
        if isinstance(detail, dict):
            for key in ("connected", "capabilities", "read_only", "account", "order_options", "error"):
                if key in detail:
                    base[key] = detail[key]
        base["connected"] = bool(base.get("connected"))
        base["capabilities"] = dict(base.get("capabilities") or {})
        legacy_account = dict(base.get("account") or {})
        authority = str(legacy_account.get("authority_level") or "unknown").strip().lower()
        base["read_only"] = bool(base.get("read_only")) or authority in {
            "read-only", "read_only", "readonly"
        }
        base["account"] = {"authority_level": "read-only" if base["read_only"] else "full"}
        base["order_options"] = dict(base.get("order_options") or {})
        base["error"] = dict(base.get("error") or {})
        return base

    def set_broker_detail(self, detail: dict | None = None) -> dict:
        was_connected = bool(getattr(self, "broker_detail", {}).get("connected"))
        self.broker_detail = self._normalize_broker_detail(detail)
        if was_connected != self.broker_detail["connected"] or not self.broker_detail["connected"]:
            self.clear_symbol_order_options()
        return self.broker_detail

    def clear_symbol_order_options(self) -> None:
        with self._symbol_order_options_lock:
            self._symbol_order_options.clear()

    def symbol_order_options(self, symbol: str) -> dict:
        normalized = str(symbol or "").strip().upper()
        with self._symbol_order_options_lock:
            return dict(self._symbol_order_options.get(normalized) or {})

    def _store_symbol_order_options(self, payload: dict) -> None:
        options_by_symbol = payload.get("symbol_order_options")
        if not isinstance(options_by_symbol, dict):
            return
        normalized_options: dict[str, dict] = {}
        for symbol, raw in options_by_symbol.items():
            if not isinstance(raw, dict):
                continue
            normalized_symbol = str(symbol or raw.get("symbol") or "").strip().upper()
            if not normalized_symbol:
                continue
            default_route = str(raw.get("default_route") or "SMART").strip().upper() or "SMART"
            routes: list[str] = []
            for route in raw.get("routes") or []:
                value = str(route or "").strip().upper()
                if value and value not in routes:
                    routes.append(value)
            if default_route not in routes:
                routes.insert(0, default_route)
            supported_tifs: list[str] = []
            for tif in raw.get("supported_tifs") or []:
                value = str(tif or "").strip()
                if value and value not in supported_tifs:
                    supported_tifs.append(value)
            normalized_option = {
                "symbol": normalized_symbol,
                "default_route": default_route,
                "routes": routes,
                "route_editable": bool(raw.get("route_editable", False)),
                "hidden_order": bool(raw.get("hidden_order", False)),
                "routes_validated": bool(raw.get("routes_validated", False)),
            }
            if "supported_tifs" in raw:
                normalized_option["supported_tifs"] = supported_tifs
            normalized_options[normalized_symbol] = normalized_option
        if normalized_options:
            with self._symbol_order_options_lock:
                self._symbol_order_options.update(normalized_options)

    def has_broker_capability(self, capability: str) -> bool:
        if self.mock_mode:
            return True
        return bool(
            self.connected
            and self._can_use_se()
            and self.broker_detail.get("connected")
            and (self.broker_detail.get("capabilities") or {}).get(capability, False)
        )

    def broker_unavailable_message(self, capability: str = "") -> str:
        if not self._can_use_se():
            return "交易服务器未连接"
        if not self.broker_detail.get("connected"):
            error = self.broker_detail.get("error") or {}
            return sanitize(error.get("message") or "交易服务未连接")
        if capability and not (self.broker_detail.get("capabilities") or {}).get(capability, False):
            if capability in {"orders", "cancel_order"}:
                return "当前账户为只读权限，不支持下单或撤单"
            return "当前交易通道不支持该功能"
        return "交易服务不可用"

    def can_trade(self) -> bool:
        return self.has_broker_capability("orders")

    def broker_status_query(self) -> tuple[bool, dict, str]:
        if not self._can_use_se():
            return False, self.broker_detail, "交易服务器未连接"
        resp = self._request_se("BROKER_STATUS_QUERY", {}, timeout=8.0)
        if not isinstance(resp, dict):
            return False, self.broker_detail, "交易状态查询超时"
        payload = resp.get("payload", {}) if isinstance(resp.get("payload", {}), dict) else {}
        detail = payload.get("broker_detail")
        if isinstance(detail, dict):
            self.set_broker_detail(detail)
        ok = bool(payload.get("success", True))
        return ok, self.broker_detail, sanitize(payload.get("message", "ok"))

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
            self._store_symbol_order_options(payload)
            return True, sanitize(payload.get("message", "行情订阅成功"))
        return False, sanitize(payload.get("message", "行情订阅失败"))

    def refresh_quote(
        self,
        symbol: str,
        price_source: str,
        timeout: float = 5.0,
    ) -> tuple[bool, dict, str]:
        """Request a broker-confirmed quote for one immediate order."""
        if not self._can_use_se():
            return False, {}, "交易服务未连接"
        normalized_symbol = str(symbol or "").strip().upper()
        source = str(price_source or "").strip().lower()
        if not normalized_symbol or source not in {"bid", "ask"}:
            return False, {}, "行情刷新参数无效"
        bounded_timeout = max(0.1, float(timeout or 0.0))
        timeout_ms = max(100, int(bounded_timeout * 1000))
        payload = {
            "action": "subscribe",
            "symbols": [normalized_symbol],
            "force_refresh": True,
            "price_source": source,
            "timeout_ms": timeout_ms,
            "deadline_ms": int(time.time() * 1000) + timeout_ms,
        }
        resp = self._request_se("QUOTE_SUBSCRIBE", payload, timeout=bounded_timeout)
        response_payload = (resp or {}).get("payload", {}) if isinstance(resp, dict) else {}
        quote = response_payload.get("quote")
        if response_payload.get("success") and response_payload.get("quote_confirmed") and isinstance(quote, dict):
            self._store_symbol_order_options(response_payload)
            return True, dict(quote), sanitize(response_payload.get("message", "行情刷新成功"))
        return False, {}, sanitize(response_payload.get("message", "行情刷新失败"))

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
        self.last_login_error = {}
        status, resp = self.http.post("/auth/login", {
            "username": username,
            "password": password,
            "force": bool(force),
        })
        if status == 200:
            self.http.token = resp.get("token", "")
            try:
                self.auth_expires_in = max(0, int(resp.get("expires_in") or 0))
            except (TypeError, ValueError):
                self.auth_expires_in = 0
            self.auth_deadline_monotonic = (
                time.monotonic() + self.auth_expires_in
                if self.auth_expires_in > 0
                else 0.0
            )
            self.se_address = resp.get("se_address", "") or ""
            self.set_broker_detail(None)
            self.connected = True
            self.mock_mode = False
            return True, "已连接"
        if status == 0:
            return False, "服务不可用，请检查服务是否已启动"

        detail = resp.get("detail", f"Login failed (HTTP {status})")
        if isinstance(detail, dict):
            self.last_login_error = dict(detail)
            msg = detail.get("message") or detail.get("detail") or f"Login failed (HTTP {status})"
        else:
            self.last_login_error = {"message": str(detail), "status": status}
            msg = detail
        return False, sanitize(msg)

    def logout(self):
        """鐧诲嚭"""
        if self.connected:
            self.http.post("/auth/logout", {})
        self.clear_local_auth()

    def clear_local_auth(self) -> None:
        """Clear Client-side authentication without waiting for SM."""
        self.http.token = ""
        self.auth_expires_in = 0
        self.auth_deadline_monotonic = 0.0
        self.set_broker_detail(None)
        self.invalidate_order_cache()
        self.clear_symbol_order_options()
        self.connected = False
        self.mock_mode = False

    def auth_seconds_remaining(self) -> float | None:
        if self.auth_deadline_monotonic <= 0:
            return None
        return max(0.0, self.auth_deadline_monotonic - time.monotonic())

    def is_auth_expired(self) -> bool:
        remaining = self.auth_seconds_remaining()
        return remaining is not None and remaining <= 0

    def invalidate_order_cache(self) -> None:
        with self._order_cache_lock:
            self._order_cache_generation += 1
            self._all_orders_cache = []
            self._all_orders_cache_at = 0.0

    def _request_raw_orders(self, mode: str, *, force: bool = False) -> list[dict] | None:
        self._order_query_error = {}
        normalized = "all" if str(mode or "").lower() == "all" else "live"
        if normalized == "all":
            with self._order_cache_lock:
                request_fetch_serial = self._order_cache_fetch_serial
                age = time.monotonic() - self._all_orders_cache_at if self._all_orders_cache_at else 0.0
                if not force and self._all_orders_cache_at and age < ALL_ORDERS_CACHE_SECONDS:
                    return list(self._all_orders_cache)

            with self._order_fetch_lock:
                with self._order_cache_lock:
                    age = time.monotonic() - self._all_orders_cache_at if self._all_orders_cache_at else 0.0
                    cache_is_fresh = self._all_orders_cache_at and age < ALL_ORDERS_CACHE_SECONDS
                    fetched_after_start = self._order_cache_fetch_serial > request_fetch_serial
                    if cache_is_fresh and (not force or fetched_after_start):
                        return list(self._all_orders_cache)
                    cache_generation = self._order_cache_generation

                resp = self._request_se("ORDER_QUERY", {"mode": "all"}, timeout=12.0)
                payload = (resp or {}).get("payload", {}) if isinstance(resp, dict) else {}
                if not payload.get("success"):
                    self._set_order_query_error(resp, payload)
                    return None
                raw = list(payload.get("orders", []) or [])
                with self._order_cache_lock:
                    if cache_generation == self._order_cache_generation:
                        self._all_orders_cache = raw
                        self._all_orders_cache_at = time.monotonic()
                        self._order_cache_fetch_serial += 1
                return list(raw)

        resp = self._request_se("ORDER_QUERY", {"mode": "live"}, timeout=12.0)
        payload = (resp or {}).get("payload", {}) if isinstance(resp, dict) else {}
        if not payload.get("success"):
            self._set_order_query_error(resp, payload)
            return None
        return list(payload.get("orders", []) or [])

    def _set_order_query_error(self, response: object, payload: dict) -> None:
        if not isinstance(response, dict):
            self._order_query_error = {
                "code": "ORDER_QUERY_TIMEOUT",
                "message": "订单查询超时或交易服务器不可用",
            }
            return
        self._order_query_error = {
            "code": str(payload.get("code") or payload.get("error_code") or "ORDER_QUERY_FAILED"),
            "message": sanitize(payload.get("message") or "订单查询失败"),
        }

    def get_today_activity(self, *, force_orders: bool = False) -> list[dict]:
        """
        鑾峰彇浠婃棩娲诲姩鏁版嵁(鎸佷粨+宸插钩浠?

        Returns:
            鎸佷粨瀛楀吀鍒楄〃锛屾瘡涓寘鍚?symbol, qty, direction, avg_open,
            close_px, unrealized, realized_today, qty_bot, qty_sld, exes
        """
        return self.query_today_activity(force_orders=force_orders).data

    def query_today_activity(self, *, force_orders: bool = False) -> QueryResult:
        self._pos_error = ""
        if self.mock_mode:
            return QueryResult(True, self._mock_positions())
        if not self.connected:
            message = "交易服务未连接"
            self._pos_error = message
            return QueryResult(False, error_code="CLIENT_DISCONNECTED", message=message)
        try:
            if not self.has_broker_capability("positions"):
                self._pos_error = self.broker_unavailable_message("positions")
                return QueryResult(False, error_code="POSITION_QUERY_NOT_SUPPORTED", message=self._pos_error)

            resp_pos = self._request_se("POSITION_QUERY", {}, timeout=12.0)
            if not isinstance(resp_pos, dict):
                self._pos_error = "交易服务请求超时或暂不可用"
                return QueryResult(False, error_code="POSITION_QUERY_TIMEOUT", message=self._pos_error)

            payload_pos = resp_pos.get("payload", {}) or {}
            if not payload_pos.get("success"):
                err_code = payload_pos.get("code", "") or payload_pos.get("error_code", "")
                if err_code == "BROKER_OFFLINE":
                    self._pos_error = "交易服务未连接"
                elif err_code == "NO_BROKER":
                    self._pos_error = "未加载交易服务配置"
                else:
                    self._pos_error = sanitize(payload_pos.get("message", "持仓查询失败"))
                return QueryResult(
                    False,
                    error_code=str(err_code or "POSITION_QUERY_FAILED"),
                    message=self._pos_error,
                )

            pos_rows = payload_pos.get("positions", []) or []
            orders_raw = self._request_raw_orders("all", force=force_orders)
            if orders_raw is None:
                error = dict(self._order_query_error)
                self._pos_error = error.get("message") or "订单历史查询失败，持仓数据未更新"
                return QueryResult(
                    False,
                    error_code=error.get("code") or "ORDER_HISTORY_QUERY_FAILED",
                    message=self._pos_error,
                )

            return QueryResult(True, self._calc_today_activity(pos_rows, orders_raw))
        except Exception:
            self._pos_error = "持仓查询失败，请稍后刷新"
            return QueryResult(False, error_code="POSITION_QUERY_FAILED", message=self._pos_error)

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

    def get_orders(self, mode: str = "live", *, force: bool = False) -> list[dict]:
        """
        鑾峰彇璁㈠崟鍒楄〃

        Args:
            mode: "live" 鑾峰彇娲昏穬璁㈠崟 / "all" 鑾峰彇鎵€鏈夎鍗?

        Returns:
            璁㈠崟瀛楀吀鍒楄〃
        """
        return self.query_orders(mode, force=force).data

    def query_orders(self, mode: str = "live", *, force: bool = False) -> QueryResult:
        if self.mock_mode:
            return QueryResult(True, [])
        if not self.connected:
            return QueryResult(False, error_code="CLIENT_DISCONNECTED", message="交易服务未连接")
        try:
            if not self.has_broker_capability("order_query"):
                return QueryResult(
                    False,
                    error_code="ORDER_QUERY_NOT_SUPPORTED",
                    message=self.broker_unavailable_message("order_query"),
                )

            normalized_mode = str(mode or "live").lower()
            se_mode = "live" if normalized_mode == "live" else "all"
            raw = self._request_raw_orders(se_mode, force=force)
            if raw is None:
                error = dict(self._order_query_error)
                return QueryResult(
                    False,
                    error_code=error.get("code") or "ORDER_QUERY_FAILED",
                    message=error.get("message") or "订单查询失败",
                )

            result = []
            ET = self._ET
            for o in raw:
                try:
                    if normalized_mode != "live":
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
                    rs = {
                        "Routed": "Routing",
                        "In Flight": "Routing",
                        "Cancel Requested": "Cancelling",
                        "Partially Filled": "Partial",
                    }.get(rs, rs)
                    st = STATUS_MAP.get(rs, rs)
                    ot = o.get("type", "鈥?")
                    tif = o.get("tif", "鈥?")

                    result.append(dict(
                        id=o.get("id", ""),
                        symbol=sym, action=act,
                        qty=qty, price=px, status=st,
                        raw_status=rs, otype=ot, tif=tif,
                        status_message=sanitize(
                            o.get("status_message") or o.get("reject_reason") or ""
                        ),
                        can_cancel=bool(o.get("can_cancel", rs in LIVE_STATUSES)),
                    ))
                except Exception:
                    continue
            if normalized_mode == "filled":
                result = [item for item in result if item.get("raw_status") == "Filled"]
            elif normalized_mode == "inactive":
                result = [
                    item for item in result
                    if item.get("raw_status") in {"Cancelled", "Rejected", "Expired"}
                ]
            elif normalized_mode == "live":
                result = [
                    item for item in result
                    if item.get("raw_status") in LIVE_STATUSES
                ]
            return QueryResult(True, result)
        except Exception:
            return QueryResult(
                False,
                error_code="ORDER_QUERY_FAILED",
                message="订单查询失败，请稍后刷新",
            )

    def cancel_order(self, order_id: str) -> tuple[bool, str]:
        """鎾ら攢璁㈠崟"""
        if not self.connected:
            return False, "未连接"
        try:
            if not self.has_broker_capability("cancel_order"):
                return False, self.broker_unavailable_message("cancel_order")

            resp = self._request_se("ORDER_CANCEL", {"order_id": order_id}, timeout=10.0)
            if not isinstance(resp, dict):
                return False, "\u64a4\u5355\u72b6\u6001\u672a\u77e5\uff0c\u8bf7\u5237\u65b0\u8ba2\u5355\u786e\u8ba4"
            payload = (resp or {}).get("payload", {}) if isinstance(resp, dict) else {}
            if payload.get("success"):
                self.invalidate_order_cache()
                return True, f"订单已撤销：{str(order_id)[-6:]}"
            return False, sanitize(payload.get("message", "撤单失败"))
        except Exception:
            return False, "撤单失败，请刷新订单确认"

    def place_order(self, symbol: str, qty: int, price: float,
                    action: str, order_type: str = "limit", tif: str = "Day",
                    route: str = "", hidden: bool = False) -> tuple[bool, str]:
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
            if not self.has_broker_capability("orders"):
                return False, self.broker_unavailable_message("orders")

            resp = self._request_se("ORDER_SUBMIT", {
                "symbol": symbol,
                "qty": qty,
                "price": price,
                "action": action,
                "order_type": order_type,
                "tif": tif,
                "route": route,
                "hidden": bool(hidden),
            }, timeout=12.0)
            if not isinstance(resp, dict):
                return False, "\u8ba2\u5355\u72b6\u6001\u672a\u77e5\uff0c\u8bf7\u5237\u65b0\u8ba2\u5355\u786e\u8ba4"
            payload = (resp or {}).get("payload", {}) if isinstance(resp, dict) else {}
            if payload.get("success"):
                self.invalidate_order_cache()
                oid = payload.get("order_id", "")
                return True, f"下单已提交，订单号：{str(oid)[-8:]}"
            return False, sanitize(payload.get("message", "下单失败"))
        except Exception:
            return False, "下单失败，请稍后重试"

    def enable_mock_mode(self):
        """启用模拟模式"""
        self.mock_mode = True
        self.connected = True
