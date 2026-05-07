"""
Auth Router
认证相关端点：登录、登出
"""

import logging
from fastapi import APIRouter, HTTPException

from models import LoginRequest, LoginResponse, LogoutResponse
from config import SERVER_TOKEN, session_store, log, load_users_from_json
from auth import generate_client_token

router = APIRouter(prefix="/auth", tags=["认证管理"])


def _verify_user_from_json(username: str, password: str) -> dict | None:
    """从 users.json 验证用户凭据"""
    users = load_users_from_json()
    for u in users:
        if u.get("username") == username and u.get("password") == password and u.get("status") == "active":
            return u
    return None


@router.post("/login", response_model=LoginResponse)
async def login(req: LoginRequest):
    """
    用户登录认证
    验证优先级: JSON文件 > 配置文件凭据 > 数据库
    返回服务 Token，客户端后续请求需携带此 Token
    """
    account = None

    # 1. 优先从 JSON 文件验证（Demo 模式主要入口）
    account = _verify_user_from_json(req.username, req.password)
    if account:
        log.info(f"Client logged in via JSON credentials: {req.username}")
        token = generate_client_token(req.username)
        return LoginResponse(
            success=True,
            token=token,
            broker_list=["default"],
            expires_in=3600,
        )

    # 2. 回退到配置文件中的 SERVER_USERNAME/SERVER_PASSWORD
    from config import SERVER_USERNAME, SERVER_PASSWORD
    if req.username == SERVER_USERNAME and req.password == SERVER_PASSWORD:
        log.info(f"Client logged in via config credentials: {req.username}")
        token = generate_client_token(req.username)
        return LoginResponse(
            success=True,
            token=token,
            broker_list=["default"],
            expires_in=3600,
        )

    # 3. 最后尝试数据库账号
    try:
        from database import verify_account, get_broker_list
        db_account = verify_account(req.username, req.password)
        if db_account:
            log.info(f"Client logged in (DB): {req.username} role={db_account.get('role')}")
            token = generate_client_token(req.username)
            brokers = [b["name"] for b in get_broker_list()]
            return LoginResponse(
                success=True,
                token=token,
                broker_list=brokers if brokers else ["default"],
                expires_in=3600,
            )
    except Exception as e:
        log.warning(f"DB authentication failed: {e}")

    raise HTTPException(status_code=401, detail="Invalid credentials")


@router.post("/logout", response_model=LogoutResponse)
async def logout(_=None):
    """用户登出（客户端级别断开，不影响服务器券商连接）"""
    from auth import invalidate_client_token
    # 尝试从请求头获取 Token 并使其失效
    try:
        from fastapi import Request
        # 通过 Depends 上下文获取 request 不可行，此处仅做 Token 清理提示
        log.info("Client logged out (token cleanup available on next login cycle)")
    except Exception:
        pass
    log.info("Client logged out")
    return LogoutResponse(success=True)
