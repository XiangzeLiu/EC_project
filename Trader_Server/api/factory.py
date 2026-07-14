"""
券商适配器工厂

根据 broker_type 字符串创建对应的 BaseBrokerAPI 实例。
所有 Trader_Server 内部代码通过此工厂获取券商实例，不直接 import 具体实现。

使用方式:
    from api.factory import BrokerFactory

    broker = BrokerFactory.create("tastytrade")
    await broker.connect({"secret": "...", "token": "..."})
"""

import logging
import time
from typing import Type

from .base import BaseBrokerAPI
from .tastytrade import TastytradeBroker
from .interactive_brokers import IBBroker

log = logging.getLogger("trader_server.api.factory")

class TestBroker(BaseBrokerAPI):
    """本地测试用券商适配器（不依赖外部 SDK）"""

    def __init__(self):
        super().__init__(broker_type="Test")
        self._orders: list[dict] = []

    async def connect(self, credentials: dict) -> bool:
        self._credentials = dict(credentials or {})
        self._connected = True
        return True

    async def disconnect(self) -> None:
        self._connected = False

    async def is_connected(self) -> bool:
        return self._connected

    async def place_order(self, order_params: dict) -> dict:
        order_id = f"test_{int(time.time() * 1000)}"
        self._orders.append({
            "order_id": order_id,
            "symbol": order_params.get("symbol", "TEST"),
            "qty": int(order_params.get("qty", 0) or 0),
            "status": "filled",
            "created_at": int(time.time() * 1000),
        })
        return {"success": True, "order_id": order_id}

    async def cancel_order(self, order_id: str) -> dict:
        return {"success": True, "order_id": order_id, "status": "cancelled"}

    async def get_positions(self, filters: dict | None = None) -> list[dict]:
        return []

    async def get_orders(self, mode: str = "live") -> list[dict]:
        return list(self._orders)

    async def subscribe_quotes(self, symbols: list[str]) -> None:
        return None

    async def unsubscribe_quotes(self, symbols: list[str]) -> None:
        return None


# 注册表：broker_type（统一小写）→ 实现类
_BROKER_REGISTRY: dict[str, type[BaseBrokerAPI]] = {
    "tt": TastytradeBroker,
    "tastytrade": TastytradeBroker,
    "ib": IBBroker,
    "interactive_brokers": IBBroker,
    "test": TestBroker,
}


class BrokerFactory:
    """
    券商 API 工厂
    
    提供统一的创建接口，支持运行时查询可用类型和按需实例化。
    """

    @classmethod
    def create(cls, broker_type: str) -> BaseBrokerAPI:
        """
        根据类型创建对应的券商适配器实例
        
        Args:
            broker_type: 券商标识，如 "tastytrade", "interactive_brokers"
            
        Returns:
            已初始化但尚未连接的 BaseBrokerAPI 子类实例
            
        Raises:
            ValueError: 不支持的券商类型
        """
        broker_type = (broker_type or "").strip().lower()
        
        impl_class = _BROKER_REGISTRY.get(broker_type)
        if not impl_class:
            supported = ", ".join(_BROKER_REGISTRY.keys())
            raise ValueError(
                f"Unsupported broker_type: '{broker_type}'. "
                f"Supported types: [{supported}]"
            )

        instance = impl_class()
        log.info(f"[BrokerFactory] created {instance.__class__.__name__} (type={broker_type})")
        return instance

    @classmethod
    def get_supported_types(cls) -> list[str]:
        """返回所有支持的券商类型列表"""
        return list(_BROKER_REGISTRY.keys())

    @classmethod
    def is_supported(cls, broker_type: str) -> bool:
        """检查是否支持指定券商类型"""
        return (broker_type or "").strip().lower() in _BROKER_REGISTRY

    @classmethod
    def get_adapter_spec(cls, broker_type: str) -> dict:
        """返回适配器规范信息（用于接入新券商时自检）"""
        instance = cls.create(broker_type)
        return {
            "broker_type": instance.broker_type,
            "class": instance.__class__.__name__,
            "credential_profiles": instance.credential_profiles(),
            "capabilities": instance.capabilities(),
            "supports_order_query": bool(instance.capabilities().get("order_query", False)),
        }

    @classmethod
    def register(cls, broker_type: str, impl_class: Type[BaseBrokerAPI]) -> None:
        """
        注册新的券商实现（用于未来扩展）

        Args:
            broker_type: 券商标识字符串
            impl_class: 继承自 BaseBrokerAPI 的类
        """
        key = (broker_type or "").strip().lower()
        if not key:
            raise ValueError("broker_type is required")
        if not issubclass(impl_class, BaseBrokerAPI):
            raise TypeError("impl_class must inherit BaseBrokerAPI")
        _BROKER_REGISTRY[key] = impl_class
        log.info(f"[BrokerFactory] registered custom broker: {key} -> {impl_class.__name__}")
