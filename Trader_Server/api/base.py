"""
券商 API 抽象基类

定义所有券商适配器必须实现的统一接口。
每个具体券商实现（Tastytrade / IB 等）需继承此类并实现所有抽象方法。
"""

from abc import ABC, abstractmethod
from typing import Callable, Any
import logging

log = logging.getLogger("trader_server.api.base")


class BaseBrokerAPI(ABC):
    """
    券商适配器统一接口
    
    所有券商实现必须遵循以下生命周期:
        connect(credentials) → [is_connected() → 操作 → disconnect()]
    """

    def __init__(self, broker_type: str):
        self.broker_type = broker_type
        self._connected = False
        self._credentials = {}
        self._quote_callback: Callable | None = None

    @classmethod
    def credential_profiles(cls) -> list[tuple[str, ...]]:
        """
        返回支持的凭证组合（任意一个满足即可）。

        默认空列表表示不做统一校验，由具体适配器自行处理。
        示例：[("token", "secret"), ("account_username", "account_password", "token", "secret")]
        """
        return []

    def normalize_credentials(self, credentials: dict | None) -> dict:
        """
        统一凭证字段名，减少上游分支判断。

        - username -> account_username
        - password -> account_password
        """
        data = dict(credentials or {})
        if "username" in data and "account_username" not in data:
            data["account_username"] = data.get("username")
        if "password" in data and "account_password" not in data:
            data["account_password"] = data.get("password")
        return data

    def validate_credentials(self, credentials: dict | None) -> tuple[bool, str]:
        """
        使用 credential_profiles 做通用校验。
        """
        data = self.normalize_credentials(credentials)
        profiles = self.credential_profiles()
        if not profiles:
            return True, ""

        for profile in profiles:
            if all(str(data.get(k, "")).strip() for k in profile):
                return True, ""

        expected = " or ".join("+".join(p) for p in profiles)
        return False, f"Missing required credentials, expected: {expected}"

    # ── 生命周期 ──────────────────────────────────────────────

    @abstractmethod
    async def connect(self, credentials: dict) -> bool:
        """
        使用凭证连接到券商 API
        
        Args:
            credentials: 券商特定的凭证字典
            
        Returns:
            连接是否成功
        """
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """断开与券商 API 的连接，释放资源"""
        ...

    async def reconnect(self) -> bool:
        """
        使用缓存的凭证重新连接
        
        默认实现：disconnect + connect。子类可覆写以优化重连逻辑。
        """
        await self.disconnect()
        return await self.connect(self._credentials)

    @abstractmethod
    async def is_connected(self) -> bool:
        """检查当前是否处于已连接状态"""
        ...

    # ── 交易操作 ──────────────────────────────────────────────

    @abstractmethod
    async def place_order(self, order_params: dict) -> dict:
        """
        下单
        
        Args:
            order_params: {
                symbol (str), qty (int), price (float),
                action (str): "Buy to Open" / "Sell to Close" / ...,
                order_type (str): "limit" | "market",
                tif (str): "Day" | "GTC" | ...
            }
            
        Returns:
            {"success": bool, "order_id": str, ...} 或抛异常
        """
        ...

    @abstractmethod
    async def cancel_order(self, order_id: str) -> dict:
        """撤单。Returns: {"success": bool, ...}"""
        ...

    @abstractmethod
    async def get_positions(self, filters: dict | None = None) -> list[dict]:
        """
        获取持仓列表
        
        Args:
            filters: 可选过滤条件，如 {"symbols": ["AAPL"]}
            
        Returns:
            [{"symbol", "quantity", "direction", "average_open_price", "close_price", ...}, ...]
        """
        ...

    async def get_orders(self, mode: str = "live") -> list[dict]:
        """
        查询订单列表（可选能力，默认不支持）

        Args:
            mode: "live" | "all"
        """
        raise NotImplementedError("Order query not supported by this broker adapter")

    # ── 行情 ──────────────────────────────────────────────────

    @abstractmethod
    async def subscribe_quotes(self, symbols: list[str]) -> None:
        """订阅行情数据"""
        ...

    @abstractmethod
    async def unsubscribe_quotes(self, symbols: list[str]) -> None:
        """取消订阅行情数据"""
        ...

    def set_quote_callback(self, callback: Callable[[dict], None]) -> None:
        """
        注册行情数据回调函数
        
        当有新行情数据时，调用 callback(quote_dict)
        quote_dict 格式: {"symbol", "bid", "ask", "last", "volume", "ts"}
        """
        self._quote_callback = callback

    def _on_quote_data(self, quote: dict) -> None:
        """内部方法：当收到行情数据时调用回调"""
        if self._quote_callback:
            try:
                self._quote_callback(quote)
            except Exception as e:
                log.warning(f"Quote callback error: {e}")

    # ── 辅助 ──────────────────────────────────────────────────

    def __repr__(self) -> str:
        status = "CONNECTED" if self._connected else "DISCONNECTED"
        return f"<{self.__class__.__name__} type={self.broker_type} status={status}>"
