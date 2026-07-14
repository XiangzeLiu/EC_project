"""
Auth Router
认证相关端点：登录、登出
"""

import logging
from fastapi import APIRouter, HTTPException, Request


from models import LoginRequest, LoginResponse, LogoutResponse
from config import SERVER_TOKEN, session_store, log, load_users_from_json, active_client_tokens
from auth import (
    generate_client_token,
    get_client_username,
    invalidate_client_token,
    invalidate_client_tokens_by_username,
)


router = APIRouter(prefix="/auth", tags=["认证管理"])


def _verify_user_from_json(username: str, password: str) -> dict | None:
    """从 users.json 验证用户凭据"""
    users = load_users_from_json()
    for u in users:
        if u.get("username") == username and u.get("password") == password and u.get("status") == "active":
            return u
    return None


def _handle_duplicate_login(username: str, force: bool) -> None:
    """处理同账号重复登录：可选强制接管旧会话。"""
    existing_tokens = [
        t for t, info in active_client_tokens.items()
        if info.get("username") == username
    ]
    if not existing_tokens:
        return

    if force:
        kicked = invalidate_client_tokens_by_username(username)
        log.warning(f"Force takeover login: user={username}, kicked_tokens={kicked}")
        return

    log.warning(f"Duplicate login attempt for already logged-in user: {username}")
    raise HTTPException(
        status_code=409,
        detail={
            "message": "该账号已在其他地方登录",
            "code": "already_logged_in",
            "can_force": True,
        },
    )


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
        _handle_duplicate_login(req.username, req.force)

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
        _handle_duplicate_login(req.username, req.force)

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
            _handle_duplicate_login(req.username, req.force)

            log.info(f"Client logged in (DB): {req.username} role={db_account.get('role')}")

            token = generate_client_token(req.username)
            brokers = [b["name"] for b in get_broker_list()]
            _se_addr = db_account.get("se_address") or db_account.get("ts_address") or ""
            return LoginResponse(
                success=True,
                token=token,
                broker_list=brokers if brokers else ["default"],
                expires_in=3600,
                se_address=_se_addr,
            )
    except HTTPException:
        raise
    except Exception as e:
        log.warning(f"DB authentication failed: {e}")

    raise HTTPException(status_code=401, detail="Invalid credentials")


@router.post("/verify-token")
async def verify_client_token(request: Request):
    """
    供 TS 调用：验证 Client Token 是否有效，并校验该客户端是否有权限连接当前节点。

    鉴权要求：
      - 调用方必须是已注册节点（Bearer 为节点 token）
      - 请求体必须包含 client token
    """
    auth_header = request.headers.get("authorization", "")
    node_token = auth_header.replace("Bearer ", "").strip() if auth_header.startswith("Bearer ") else ""
    if not node_token:
        raise HTTPException(status_code=401, detail="Missing node token")

    from database import verify_node_token
    node = verify_node_token(node_token)
    if not node:
        raise HTTPException(status_code=401, detail="Invalid node token")

    try:
        body = await request.json()
    except Exception:
        body = {}

    client_token = (body.get("token") or "").strip()
    if not client_token:
        return {"ok": False, "valid": False, "reason": "missing_client_token"}

    from config import SERVER_TOKEN
    if client_token == SERVER_TOKEN:
        return {
            "ok": True,
            "valid": True,
            "username": "server",
            "token_type": "server",
            "server_id": node.get("server_id", ""),
            "allowed": True,
        }

    username = get_client_username(client_token)
    if not username:
        return {"ok": True, "valid": False, "reason": "invalid_or_expired"}

    node_server_id = str(node.get("server_id") or "")
    requested_server_id = str(body.get("server_id") or "").strip()
    if requested_server_id and requested_server_id != node_server_id:
        return {
            "ok": True,
            "valid": False,
            "username": username,
            "token_type": "client",
            "server_id": node_server_id,
            "allowed": False,
            "reason": "node_server_mismatch",
        }

    import node_state
    occ = node_state.manager.get_occupation_info(node_server_id)
    occupied_by = (occ or {}).get("occupied_by", "") if isinstance(occ, dict) else ""
    if occupied_by != username:
        return {
            "ok": True,
            "valid": False,
            "username": username,
            "token_type": "client",
            "server_id": node_server_id,
            "allowed": False,
            "reason": "not_occupied_by_user",
            "occupied_by": occupied_by,
        }

    return {
        "ok": True,
        "valid": True,
        "username": username,
        "token_type": "client",
        "server_id": node_server_id,
        "allowed": True,
    }


@router.post("/logout", response_model=LogoutResponse)
async def logout(request: Request):
    """用户登出（客户端级别断开，不影响服务器券商连接）"""
    auth_header = request.headers.get("authorization", "")
    token = auth_header.replace("Bearer ", "").strip() if auth_header.startswith("Bearer ") else ""

    if token:
        if invalidate_client_token(token):
            log.info("Client logged out and token invalidated")
        else:
            log.info("Client logout requested, token not found in active set")
    else:
        log.info("Client logout requested without bearer token")

    return LogoutResponse(success=True)
