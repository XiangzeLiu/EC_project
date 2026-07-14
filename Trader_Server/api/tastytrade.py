"""
Tastytrade 券商适配器

从 origin_demo/server.py 移植核心逻辑:
  - Session 缓存与复用 (get_fresh 模式)
  - ACTION/TIF 枚举映射
  - Leg 构建 + 下单/撤单/持仓查询
  - Order 序列化
"""

import asyncio
import datetime
import logging
from decimal import Decimal
from typing import Any

from .base import BaseBrokerAPI

log = logging.getLogger("trader_server.api.tastytrade")

# SDK 导入标记
_SDK_AVAILABLE = False
_DX_AVAILABLE = False
try:
    from tastytrade import Session, DXLinkStreamer
    from tastytrade.account import Account
    from tastytrade.instruments import Equity
    from tastytrade.order import (
        NewOrder, OrderAction, OrderTimeInForce, OrderType,
    )
    try:
        from tastytrade.dxfeed import Quote as DXQuote
        _DX_AVAILABLE = True
    except ImportError:
        DXQuote = None
    _SDK_AVAILABLE = True
except ImportError:
    DXQuote = None
    log.warning("Tastytrade SDK not available, TastytradeBroker will be non-functional")


# ── 映射表（与 origin_demo 一致）─────────────────────────────────────

if _SDK_AVAILABLE:
    ACTION_MAP = {
        "Buy to Open":   OrderAction.BUY_TO_OPEN,
        "Sell to Close": OrderAction.SELL_TO_CLOSE,
        "Sell to Open":  OrderAction.SELL_TO_OPEN,
        "Buy to Close":  OrderAction.BUY_TO_CLOSE,
    }

    TIF_MAP = {
        "Day":     OrderTimeInForce.DAY,
        "GTC":     OrderTimeInForce.GTC,
        "IOC":     OrderTimeInForce.IOC,
        "EXT":     OrderTimeInForce.EXT,
        "GTC_EXT": OrderTimeInForce.GTC_EXT,
    }
else:
    ACTION_MAP = {}
    TIF_MAP = {}



class TastytradeBroker(BaseBrokerAPI):
    """
    Tastytrade 券商 API 适配器
    
    凭证格式 (credentials dict):
        secret (str): 必填 - Tastytrade Session Secret
        token (str):  必填 - Tastytrade Session Token
        account_number (str): 可选 - 账号号，留空则使用默认账户

    兼容四要素凭证模型：
        account_username/account_password/token/secret
    （其中 account_username/account_password 用于上游统一，不参与 TT Session 构建）
    """

    @classmethod
    def credential_profiles(cls) -> list[tuple[str, ...]]:
        return [("token", "secret")]

    @staticmethod
    def _classify_connect_exception(exc: Exception) -> tuple[str, str, bool]:
        message = str(exc or "")[:240]
        lower = message.lower()
        if any(flag in lower for flag in ("invalid_grant", "invalid jwt", "invalid credentials", "invalid token", "unauthorized", "401")):
            return "BROKER_AUTH_INVALID", message, False
        if "forbidden" in lower or "403" in lower:
            return "BROKER_AUTH_FORBIDDEN", message, False
        if "no accounts found" in lower:
            return "BROKER_ACCOUNT_MISSING", message, False
        return "BROKER_CONNECT_FAILED", message, True
    def __init__(self):
        super().__init__(broker_type="tastytrade")

        # Session 缓存（复用 origin_demo 的 session_store 模式）
        self._session: Any | None = None
        self._account: Any | None = None
        self._equity_cache: dict[str, Any] = {}

        # TT DX 行情流状态
        self._quote_streamer: Any | None = None
        self._quote_streamer_cm: Any | None = None
        self._quote_task: asyncio.Task | None = None
        self._subscribed_symbols: set[str] = set()
        self._quote_lock = asyncio.Lock()



    async def connect(self, credentials: dict) -> bool:
        """浣跨敤 secret+token 鍒涘缓 TT Session 骞惰幏鍙?Account"""
        self._connected = False
        self._session = None
        self._account = None
        self._equity_cache = {}

        if not _SDK_AVAILABLE:
            self.set_connection_error("BROKER_SDK_MISSING", "Tastytrade SDK not installed", retryable=False)
            log.error("Tastytrade SDK not installed")
            return False

        normalized = self.normalize_credentials(credentials)
        valid, reason = self.validate_credentials(normalized)
        if not valid:
            self.set_connection_error("BROKER_CREDENTIALS_INVALID", reason, retryable=False)
            log.error(f"Tastytrade credentials invalid: {reason}")
            return False

        self._credentials = normalized
        secret = normalized.get("secret", "")
        token = normalized.get("token", "")
        acct_num = normalized.get("account_number", "")

        try:
            self._session = Session(secret, token)
            accts = await Account.get(self._session)
            if acct_num:
                self._account = next(
                    (a for a in accts if str(a.account_number) == acct_num),
                    accts[0] if accts else None,
                )
            else:
                self._account = accts[0] if accts else None

            if not self._account:
                self.set_connection_error("BROKER_ACCOUNT_MISSING", "No accounts found for this session", retryable=False)
                log.error("No accounts found for this session")
                return False

            self._connected = True
            self.clear_connection_error()
            account_num = getattr(self._account, "account_number", "?")
            log.info(f"TastytradeBroker connected, account={account_num}")
            return True

        except Exception as e:
            code, message, retryable = self._classify_connect_exception(e)
            self.set_connection_error(code, message, retryable=retryable)
            log.error(f"TastytradeBroker connect failed [{code}]: {message}")
            self._session = None
            self._account = None
            return False
    async def disconnect(self) -> None:
        """断开连接，清除缓存"""
        await self._stop_quote_stream()
        self._session = None
        self._account = None
        self._equity_cache.clear()
        self._connected = False
        log.info("TastytradeBroker disconnected")


    async def is_connected(self) -> bool:
        """检查 Session 是否有效"""
        if not self._connected or not self._session:
            return False
        # TT Session 对象有 is_active 或类似属性
        return hasattr(self._session, "session_token") and bool(getattr(
            self._session, "session_token", None
        ))

    async def reconnect(self) -> bool:
        """重新创建 Session 连接"""
        return await self.connect(self._credentials)

    def _ensure_session(self) -> tuple[Any, Any]:
        """
        确保返回有效的 (session, account)，类似 origin_demo.get_fresh()
        
        Raises:
            RuntimeError: 未连接时抛出
        """
        if not self._connected or not self._session or not self._account:
            raise RuntimeError("TastytradeBroker not connected. Call connect() first.")
        return self._session, self._account

    async def place_order(self, order_params: dict) -> dict:
        """下单"""
        s, a = await self._get_fresh()

        symbol = order_params["symbol"]
        qty = order_params.get("qty", 1)
        price = float(order_params.get("price", 0.0))
        action_str = order_params.get("action", "Buy to Open")
        order_type_str = order_params.get("order_type", "limit")
        tif_str = order_params.get("tif", "Day")

        act = ACTION_MAP.get(action_str, OrderAction.BUY_TO_OPEN)
        tif_enum = TIF_MAP.get(tif_str, OrderTimeInForce.DAY)
        is_buy = "Buy" in action_str

        equity = await self._get_equity(s, symbol)
        leg = equity.build_leg(Decimal(str(qty)), act)

        if order_type_str == "market":
            order = NewOrder(time_in_force=tif_enum, order_type=OrderType.MARKET, legs=[leg])
        else:
            signed = Decimal(str(price)) * (-1 if is_buy else 1)
            order = NewOrder(
                time_in_force=tif_enum, order_type=OrderType.LIMIT,
                legs=[leg], price=signed,
            )

        resp = await a.place_order(s, order, dry_run=False)
        order_id = str(resp.order.id) if resp and resp.order else ""
        log.info(f"Order placed: {action_str} {qty} {symbol} @ {price}")
        return {"success": True, "order_id": order_id}

    async def cancel_order(self, order_id: str) -> dict:
        """撤单"""
        s, a = await self._get_fresh()
        await a.delete_order(s, order_id)
        log.info(f"Order cancelled: {order_id}")
        return {"success": True}

    async def get_positions(self, filters: dict | None = None) -> list[dict]:
        """获取持仓列表"""
        s, a = await self._get_fresh()
        raw_positions = await a.get_positions(s)

        result = []
        for p in raw_positions:
            result.append({
                "symbol": p.symbol,
                "quantity": float(p.quantity),
                "direction": getattr(p, "quantity_direction", "Long"),
                "average_open_price": float(p.average_open_price or 0),
                "close_price": float(p.close_price or 0),
                "realized_today": float(getattr(p, "realized_today", 0) or 0),
            })

        # 可选过滤
        if filters and "symbols" in filters:
            sym_set = set(filters["symbols"])
            result = [p for p in result if p["symbol"] in sym_set]

        log.info(f"Positions retrieved: {len(result)} items")
        return result

    async def get_orders(self, mode: str = "live") -> list[dict]:
        """查询订单列表"""
        s, a = await self._get_fresh()
        mode = (mode or "live").lower()
        if mode == "all":
            raw = await a.get_order_history(s)
        else:
            raw = await a.get_live_orders(s)
        return [self.serialize_order(o) for o in raw]

    async def subscribe_quotes(self, symbols: list[str]) -> None:
        """订阅 TT 行情（DXLink）"""
        if not _SDK_AVAILABLE or not _DX_AVAILABLE or not DXQuote:
            raise RuntimeError("Tastytrade DX quote stream is unavailable")

        valid = {str(s).strip().upper() for s in (symbols or []) if str(s).strip()}
        if not valid:
            return

        async with self._quote_lock:
            _, _ = await self._get_fresh()
            await self._ensure_quote_streamer_locked()

            new_syms = sorted(valid - self._subscribed_symbols)
            if not new_syms:
                return

            await self._streamer_subscribe(new_syms)
            self._subscribed_symbols.update(new_syms)
            log.info(f"TT quote subscribed: {new_syms}")



    async def unsubscribe_quotes(self, symbols: list[str]) -> None:
        valid = {str(s).strip().upper() for s in (symbols or []) if str(s).strip()}
        if not valid:
            return

        async with self._quote_lock:
            remove_syms = sorted(valid & self._subscribed_symbols)
            if not remove_syms:
                return

            if self._quote_streamer and hasattr(self._quote_streamer, "unsubscribe"):
                await self._streamer_unsubscribe(remove_syms)



            self._subscribed_symbols.difference_update(remove_syms)
            log.info(f"TT quote unsubscribed: {remove_syms}")

    async def _ensure_quote_streamer_locked(self) -> None:
        if self._quote_streamer is None:
            self._quote_streamer = await self._create_quote_streamer()

        if self._quote_task is None or self._quote_task.done():
            self._quote_task = asyncio.create_task(self._quote_consume_loop())

    async def _create_quote_streamer(self):
        if hasattr(DXLinkStreamer, "create"):
            streamer = await self._maybe_await(DXLinkStreamer.create(self._session))
        else:
            streamer = await self._maybe_await(DXLinkStreamer(self._session))

        if hasattr(streamer, "__aenter__") and hasattr(streamer, "__aexit__"):
            self._quote_streamer_cm = streamer
            entered = await self._maybe_await(streamer.__aenter__())
            return entered if entered is not None else streamer

        self._quote_streamer_cm = None
        return streamer

    async def _stop_quote_stream(self) -> None:
        async with self._quote_lock:
            self._subscribed_symbols.clear()

            if self._quote_task and not self._quote_task.done():
                self._quote_task.cancel()
                try:
                    await self._quote_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass

            self._quote_task = None

            if self._quote_streamer_cm is not None:
                try:
                    await self._maybe_await(self._quote_streamer_cm.__aexit__(None, None, None))
                except Exception as e:
                    log.warning(f"TT quote streamer context close failed: {e}")
                self._quote_streamer_cm = None
            elif self._quote_streamer is not None:
                try:
                    close_fn = getattr(self._quote_streamer, "close", None)
                    if close_fn:
                        await self._maybe_await(close_fn())
                except Exception as e:
                    log.warning(f"TT quote streamer close failed: {e}")

            self._quote_streamer = None

    async def _quote_consume_loop(self) -> None:
        while self._connected and self._quote_streamer is not None:
            try:
                if hasattr(self._quote_streamer, "get_event"):
                    event = await self._streamer_get_event()
                    quote = self._normalize_quote_event(event)
                    if quote and self._quote_callback:
                        self._quote_callback(quote)
                    continue

                if hasattr(self._quote_streamer, "listen"):
                    stream = await self._streamer_listen()
                    async for event in stream:
                        if not self._connected:
                            return
                        quote = self._normalize_quote_event(event)
                        if quote and self._quote_callback:
                            self._quote_callback(quote)
                    continue

                log.error("TT quote streamer has no supported consume API")
                return
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning(f"TT quote consume loop error: {e}")
                await asyncio.sleep(0.5)

    async def _streamer_subscribe(self, symbols: list[str]) -> None:
        fn = getattr(self._quote_streamer, "subscribe", None)
        if not fn:
            raise RuntimeError("TT quote streamer missing subscribe()")

        last_err: Exception | None = None
        for args in ((DXQuote, symbols), (symbols,)):
            try:
                await self._maybe_await(fn(*args))
                return
            except TypeError as e:
                last_err = e
        raise last_err or RuntimeError("TT quote subscribe failed")

    async def _streamer_unsubscribe(self, symbols: list[str]) -> None:
        fn = getattr(self._quote_streamer, "unsubscribe", None)
        if not fn:
            return

        last_err: Exception | None = None
        for args in ((DXQuote, symbols), (symbols,)):
            try:
                await self._maybe_await(fn(*args))
                return
            except TypeError as e:
                last_err = e
        if last_err:
            raise last_err

    async def _streamer_get_event(self):
        fn = self._quote_streamer.get_event
        last_err: Exception | None = None
        for args in ((DXQuote,), tuple()):
            try:
                return await self._maybe_await(fn(*args))
            except TypeError as e:
                last_err = e
        raise last_err or RuntimeError("TT quote get_event failed")

    async def _streamer_listen(self):
        fn = self._quote_streamer.listen
        last_err: Exception | None = None
        for args in ((DXQuote,), tuple()):
            try:
                stream = fn(*args)
                return await self._maybe_await(stream)
            except TypeError as e:
                last_err = e
        raise last_err or RuntimeError("TT quote listen failed")



    @staticmethod
    async def _maybe_await(value):
        if asyncio.iscoroutine(value):
            return await value
        return value

    @staticmethod
    def _normalize_quote_event(event: Any) -> dict | None:

        try:
            symbol = str(getattr(event, "event_symbol", "") or getattr(event, "symbol", "")).strip().upper()
            if not symbol:
                return None

            bid = float(getattr(event, "bid_price", None) or getattr(event, "bid", 0) or 0)
            ask = float(getattr(event, "ask_price", None) or getattr(event, "ask", 0) or 0)
            last = float(getattr(event, "last_price", None) or getattr(event, "price", 0) or 0)
            if last <= 0 and bid > 0 and ask > 0:
                last = round((bid + ask) / 2, 4)

            volume = int(
                float(
                    getattr(event, "day_volume", None)
                    or getattr(event, "volume", None)
                    or 0
                )
            )

            if bid <= 0 and ask <= 0 and last <= 0:
                return None

            return {
                "symbol": symbol,
                "bid": bid,
                "ask": ask,
                "last": last,
                "volume": volume,
                "ts": datetime.datetime.now().strftime("%H:%M:%S"),
            }
        except Exception:
            return None

    # ── 内部辅助 ───────────────────────────────────────────────


    async def _get_fresh(self) -> tuple[Any, Any]:
        """
        确保返回有效的 session/account（带自动重建能力）
        
        与 origin_demo server.py 第143-153行 get_fresh() 逻辑一致:
          - 有缓存且有效 → 直接返回
          - 无缓存或失效 → 用凭证重新创建
        """
        if self._session and self._account and await self.is_connected():
            return self._session, self._account

        # fallback: 重新连接
        ok = await self.reconnect()
        if not ok:
            raise RuntimeError("Failed to refresh Tastytrade session")
        return self._session, self._account

    async def _get_equity(self, session: Any, symbol: str) -> Any:
        sym = (symbol or "").strip().upper()
        if not sym:
            raise ValueError("symbol is required")
        cached = self._equity_cache.get(sym)
        if cached is not None:
            return cached
        equity = await Equity.get(session, sym)
        self._equity_cache[sym] = equity
        return equity

    @staticmethod
    def serialize_order(order_obj: Any) -> dict:
        """
        序列化 Order 对象为字典
        
        与 origin_demo server.py 第485-517行 _serialize_order() 逻辑一致。
        用于订单详情展示和 P&L 计算。
        """
        try:
            leg = order_obj.legs[0] if order_obj.legs else None
            
            legs_data = []
            for l in (order_obj.legs or []):
                fills_data = []
                for f in (getattr(l, "fills", []) or []):
                    fills_data.append({
                        "fill_price": str(getattr(f, "fill_price", 0) or 0),
                        "quantity":   str(getattr(f, "quantity", 0) or 0),
                        "filled_at":  str(getattr(f, "filled_at", "") or ""),
                    })
                legs_data.append({
                    "symbol":   str(l.symbol),
                    "action":   str(l.action),
                    "quantity": str(l.quantity),
                    "fills":    fills_data,
                })

            return {
                "id":         str(order_obj.id),
                "symbol":     leg.symbol if leg else "\u2014",
                "action":     str(leg.action) if leg else "\u2014",
                "qty":        str(leg.quantity) if leg else "\u2014",
                "price":      f"{abs(float(order_obj.price)):.2f}" if order_obj.price else "MKT",
                "type":       str(order_obj.order_type).split(".")[-1] if order_obj.order_type else "\u2014",
                "tif":        str(order_obj.time_in_force).split(".")[-1] if hasattr(order_obj, "time_in_force") else "\u2014",
                "status":     str(order_obj.status).split(".")[-1] if order_obj.status else "\u2014",
                "updated_at": str(getattr(order_obj, "updated_at", "") or ""),
                "legs":       legs_data,
            }
        except Exception as e:
            log.warning(f"serialize_order error: {e}")
            return {}

