"""
Trader_Server - 多券商 API 适配层

提供统一的券商接口抽象，支持 Tastytrade / Interactive Brokers 等多种券商。
通过 BrokerFactory 工厂函数根据 broker_type 创建对应实例。

使用方式:
    from api import BrokerFactory, BaseBrokerAPI

    broker = BrokerFactory.create("tastytrade", credentials={...})
    await broker.connect(credentials)
    positions = await broker.get_positions()
"""

from .base import BaseBrokerAPI
from .factory import BrokerFactory
from .tastytrade import TastytradeBroker
from .interactive_brokers import IBBroker

__all__ = [
    "BaseBrokerAPI",
    "BrokerFactory",
    "TastytradeBroker",
    "IBBroker",
]
