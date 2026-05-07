"""
请求/响应数据模型
Pydantic 数据模型定义
"""

from pydantic import BaseModel, Field
from typing import Optional


# ── 认证模块 ──────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    """登录请求"""
    username: str = Field(..., description="用户名")
    password: str = Field(..., description="密码")


class LoginResponse(BaseModel):
    """登录响应"""
    success: bool = Field(..., description="是否成功")
    token: str = Field(default="", description="认证令牌（后续请求需携带）")
    broker_list: list[str] = Field(default_factory=list, description="可用券商列表")
    expires_in: int = Field(default=3600, description="令牌有效期（秒）")
    detail: str = Field(default="", description="附加信息")


class LogoutResponse(BaseModel):
    """登出响应"""
    success: bool = Field(default=True, description="是否成功")


# ── 订单模块 ──────────────────────────────────────────────────────────────

class PlaceOrderRequest(BaseModel):
    """下单请求"""
    symbol: str = Field(..., description="股票代码（如 AAPL）")
    qty: int = Field(..., description="数量（股数）")
    price: float = Field(default=0.0, description="委托价格（市价单可填 0）")
    action: str = Field(
        ...,
        description="交易方向：Buy to Open(买入开仓) / Sell to Close(卖出平仓) / "
                    "Sell to Open(卖出开仓) / Buy to Close(买入平仓)",
    )
    order_type: str = Field(
        default="limit",
        description="订单类型：limit(限价单) / market(市价单)",
    )
    tif: str = Field(
        default="Day",
        description="有效期：Day(当日有效) / GTC(取消前有效) / IOC(立即成交或取消) / EXT / GTC_EXT",
    )


class PlaceOrderResponse(BaseModel):
    """下单响应"""
    success: bool = Field(..., description="是否成功")
    order_id: str = Field(default="", description="订单编号")
    detail: str = Field(default="", description="附加信息")


class CancelOrderResponse(BaseModel):
    """撤单响应"""
    success: bool = Field(..., description="是否成功")
    detail: str = Field(default="", description="附加信息")


# ── 行情订阅（WebSocket） ───────────────────────────────────────────────

class SubscribeRequest(BaseModel):
    """行情订阅/取消订阅消息"""
    action: str = Field(..., description="操作类型：subscribe(订阅) / unsubscribe(取消订阅)")
    symbols: list[str] = Field(..., description="股票代码列表")


# ── 健康检查 ──────────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    """健康检查响应"""
    status: str = Field(default="ok", description="服务状态")
    connected: bool = Field(default=False, description="券商是否已连接")
    ib_connected: bool = Field(default=False, description="IB TWS 是否已连接")
    active_clients: int = Field(default=0, description="当前在线客户端数量")
