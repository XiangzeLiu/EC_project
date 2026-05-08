"""Server_economic 数据模型定义（Pydantic）"""

from pydantic import BaseModel, Field
from typing import Optional


# ── 注册相关 ──────────────────────────────────────────────────────────────

class NodeRegistrationRequest(BaseModel):
    """提交给 Server_manager 的注册请求体"""
    node_name: str = Field(..., min_length=1, max_length=64,
                           description="节点名称")
    region: str = Field(default="", max_length=32, description="区域")
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
