"""Trader_Server 数据模型定义（Pydantic）"""

from pydantic import BaseModel, Field
from typing import Optional


# ── 注册相关 ──────────────────────────────────────────────────────────────

class NodeRegistrationRequest(BaseModel):
    """提交给 Server_manager 的注册请求体"""
    node_name: str = Field(..., min_length=1, max_length=64,
                           description="节点名称")
    broker_type: str = Field(default="", max_length=32,
                              description="券商类型 (tastytrade / interactive_brokers)")
    host: str = Field(default="", max_length=128, description="主机地址/IP")
    capabilities: list[str] = Field(default_factory=list,
                                     description="能力列表")
    contact: str = Field(default="", max_length=128, description="联系人")
    description: str = Field(default="", max_length=512, description="描述")


class RegisterResult(BaseModel):
    """SM 返回的注册提交结果"""
    ok: bool
    request_id: Optional[str] = None
    message: str = ""
    expire_at: Optional[str] = None


# ── 审核结果（SSE 推送）─────────────────────────────────────────────────

class ApprovalResult(BaseModel):
    """SSE 推送的审核结果"""
    approved: bool
    server_id: Optional[str] = None
    token: Optional[str] = None
    reason: Optional[str] = None
    message: str = ""


# ── 心跳 ──────────────────────────────────────────────────────────────────

class HeartbeatRequest(BaseModel):
    """心跳请求体"""
    ts: int = Field(default_factory=lambda: int(__import__("time").time()))
    ip: Optional[str] = None


class HeartbeatResponse(BaseModel):
    """心跳响应"""
    status: str  # "ok" 或 "error"
    message: str = ""
    next_interval: int = 30


# ── WebSocket 消息协议 ───────────────────────────────────────────────────

class WSMessage(BaseModel):
    """统一 WebSocket 消息格式"""
    type: str                    # ORDER_SUBMIT / ORDER_CANCEL / PING / PONG ...
    id: str = ""                 # 消息唯一 ID
    timestamp: int = 0           # 时间戳
    payload: dict = {}           # 消息负载


class ConnectAckPayload(BaseModel):
    """CONNECT_ACK 消息负载"""
    status: str = "SUCCESS"
    session_id: str = ""
    node_info: dict = {}
    heartbeat_interval: int = 30


class ErrorPayload(BaseModel):
    """ERROR 消息负载"""
    code: str = "UNKNOWN_ERROR"
    message: str = ""


# ── 经济数据（业务层）─────────────────────────────────────────────────────

class EconomicDataPoint(BaseModel):
    """单个经济数据点"""
    indicator: str       # 指标名: cpi, gdp, interest_rate 等
    value: float
    unit: str = ""       # 单位: %, billion USD, etc.
    period: str = ""     # 时间周期: 2026Q1, Mar-2026, etc.
    source: str = ""     # 数据来源
    region: str = ""     # 区域
    timestamp: str = ""  # 采集时间 ISO


class EconomicDataResponse(BaseModel):
    """经济数据查询响应"""
    indicator: str
    data_points: list[EconomicDataPoint] = []
    last_updated: str = ""


# ── 交易消息协议 ────────────────────────────────────────────────

class OrderSubmitPayload(BaseModel):
    """ORDER_SUBMIT 消息负载"""
    symbol: str = ""
    qty: int = 1
    price: float = 0.0
    action: str = "Buy to Open"       # Buy to Open / Sell to Close / ...
    order_type: str = "limit"         # limit / market
    tif: str = "Day"                  # Day / GTC / IOC / ...


class OrderResponsePayload(BaseModel):
    """ORDER_RESPONSE 消息负载"""
    success: bool = False
    order_id: str = ""
    status: str = ""
    error_code: str = ""
    message: str = ""


class PositionResponsePayload(BaseModel):
    """POSITION_RESPONSE 消息负载"""
    success: bool = False
    positions: list[dict] = []        # [{symbol, quantity, direction, avg_price, ...}]
    count: int = 0
    error_code: str = ""
    message: str = ""


class QuoteSubscribePayload(BaseModel):
    """QUOTE_SUBSCRIBE 消息负载"""
    action: str = "subscribe"          # subscribe / unsubscribe
    symbols: list[str] = []


class QuoteAckPayload(BaseModel):
    """QUOTE_ACK 消息负载（订阅/取消确认）"""
    success: bool = False
    subscribed: list[str] = []         # 本次成功订阅的标的
    unsubscribed: list[str] = []
    total_subscribed: int = 0
    remaining: int = 0
    message: str = ""


class QuoteDataPayload(BaseModel):
    """QUOTE_DATA 消息负载（持续推送的行情数据）"""
    symbol: str = ""
    bid: float = 0.0
    ask: float = 0.0
    last: float = 0.0
    volume: int = 0
    ts: str = ""
    broker_type: str = ""


class BrokerStatusChangePayload(BaseModel):
    """BROKER_STATUS_CHANGE 推送负载"""
    broker_type: str = ""
    status: str = ""                   # connected / disconnected / reconnected / error / ...
    config_version: int = 0
