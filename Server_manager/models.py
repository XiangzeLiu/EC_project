"""
请求/响应数据模型
Pydantic 数据模型定义
"""

from pydantic import BaseModel, Field


# ── 认证模块 ──────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    """登录请求"""
    username: str = Field(..., description="用户名")
    password: str = Field(..., description="密码")
    force: bool = Field(default=False, description="是否强制接管同账号旧会话")



class LoginResponse(BaseModel):
    """登录响应"""
    success: bool = Field(..., description="是否成功")
    token: str = Field(default="", description="认证令牌（后续请求需携带）")
    broker_list: list[str] = Field(default_factory=list, description="可用券商列表")
    expires_in: int = Field(default=3600, description="令牌有效期（秒）")
    detail: str = Field(default="", description="附加信息")
    se_address: str = Field(default="", description="绑定的 Trade_Server 地址")
    allowed_brokers: list[str] = Field(default_factory=list, description="账户允许的券商列表")
    config_scope: str = Field(default="", description="Client 本地配置隔离标识")


class LogoutResponse(BaseModel):
    """登出响应"""
    success: bool = Field(default=True, description="是否成功")


# ── 健康检查 ──────────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    """健康检查响应"""
    status: str = Field(default="ok", description="服务状态")
    connected: bool = Field(default=False, description="券商是否已连接")
    ib_connected: bool = Field(default=False, description="IB TWS 是否已连接")
    active_clients: int = Field(default=0, description="当前在线客户端数量")
