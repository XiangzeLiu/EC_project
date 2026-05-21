"""
Tastytrade 券商适配器

从 origin_demo/server.py 移植核心逻辑:
  - Session 缓存与复用 (get_fresh 模式)
  - ACTION/TIF 枚举映射
  - Leg 构建 + 下单/撤单/持仓查询
  - Order 序列化
"""

import logging
from decimal import Decimal
from typing import Any

from .base import BaseBrokerAPI

log = logging.getLogger("server_economic.api.tastytrade")

# SDK 导入标记
_SDK_AVAILABLE = False
try:
    from tastytrade import Session
    from tastytrade.account import Account
    from tastytrade.instruments import Equity
    from tastytrade.order import (
        NewOrder, OrderAction, OrderTimeInForce, OrderType,
    )
    _SDK_AVAILABLE = True
except ImportError:
    log.warning("Tastytrade SDK not available, TastytradeBroker will be non-functional")


# ── 映射表（与 origin_demo 一致）─────────────────────────────────────

ACTION_MAP = {
    "Buy to Open":   OrderAction.BUY_TO_OPEN,
    "Sell to Close": OrderAction.SELL_TO_CLOSE,
    "Sell to Open":  OrderAction.SELL_TO_OPEN,
    "Buy to Close":   OrderAction.BUY_TO_CLOSE,
}

TIF_MAP = {
    "Day":     OrderTimeInForce.DAY,
    "GTC":     OrderTimeInForce.GTC,
    "IOC":     OrderTimeInForce.IOC,
    "EXT":     OrderTimeInForce.EXT,
    "GTC_EXT": OrderTimeInForce.GTC_EXT,
}


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
        return [
            ("token", "secret"),
            ("account_username", "account_password", "token", "secret"),
        ]

    def __init__(self):
        super().__init__(broker_type="tastytrade")

        # Session 缓存（复用 origin_demo 的 session_store 模式）
        self._session: Any | None = None
        self._account: Any | None = None

    async def connect(self, credentials: dict) -> bool:
        """使用 secret+token 创建 TT Session 并获取 Account"""
        if not _SDK_AVAILABLE:
            log.error("Tastytrade SDK not installed")
            return False

        normalized = self.normalize_credentials(credentials)
        valid, reason = self.validate_credentials(normalized)
        if not valid:
            log.error(f"Tastytrade credentials invalid: {reason}")
            return False

        self._credentials = normalized
        secret = normalized.get("secret", "")
        token = normalized.get("token", "")
        acct_num = normalized.get("account_number", "")

        try:
            self._session = Session(secret, token)
            # Account.get() 是异步的，需要在事件循环中运行
            accts = await Account.get(self._session)
            if acct_num:
                self._account = next(
                    (a for a in accts if str(a.account_number) == acct_num),
                    accts[0] if accts else None,
                )
            else:
                self._account = accts[0] if accts else None

            if not self._account:
                log.error("No accounts found for this session")
                return False

            self._connected = True
            account_num = getattr(self._account, "account_number", "?")
            log.info(f"TastytradeBroker connected, account={account_num}")
            return True

        except Exception as e:
            log.error(f"TastytradeBroker connect failed: {e}")
            self._session = None
            self._account = None
            return False

    async def disconnect(self) -> None:
        """断开连接，清除缓存"""
        self._session = None
        self._account = None
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

        equity = await Equity.get(s, symbol)
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
        """
        Tastytrade 行情订阅
        
        注意：Tastytrade 使用 DXLinkStreamer，此处仅做占位记录。
        实际行情建议通过 IB TWS 获取。
        """
        log.info(f"TT quote subscribe requested (not implemented via TT DX): symbols={symbols}")

    async def unsubscribe_quotes(self, symbols: list[str]) -> None:
        log.info(f"TT quote unsubscribe: symbols={symbols}")

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
