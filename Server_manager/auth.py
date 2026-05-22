"""
Authentication Module
Token 验证、客户端 Token 管理
"""

import secrets
import time
import logging
from fastapi import HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from config import SERVER_TOKEN, active_client_tokens, log


security = HTTPBearer(auto_error=False)


def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)) -> bool:
    """
    FastAPI 依赖注入：验证 Bearer Token
    支持两种 Token：
    1. SERVER_TOKEN（内部/配置凭据登录）
    2. 登录时生成的客户端 Token（存储在 active_client_tokens 中）
    """
    if credentials is None:
        raise HTTPException(status_code=401, detail="Missing token")

    token = credentials.credentials
    # 检查是否为服务器内部 Token
    if token == SERVER_TOKEN:
        return True
    # 检查是否为已登录的客户端 Token
    if token in active_client_tokens:
        return True
    raise HTTPException(status_code=401, detail="Invalid or expired token")


def generate_client_token(username: str) -> str:
    """
    为登录成功的客户端生成 Token，并注册到活跃 Token 集合中
    """
    token = secrets.token_urlsafe(32)
    active_client_tokens[token] = {
        "username": username,
        "created_at": time.time(),
    }
    log.info(f"Generated client token for user: {username}")
    return token


def get_client_token_info(token: str) -> dict | None:
    """获取客户端 token 对应的用户信息"""
    return active_client_tokens.get(token)


def get_client_username(token: str) -> str:
    """从客户端 token 提取用户名，不存在时返回空字符串"""
    info = get_client_token_info(token)
    if not info:
        return ""
    return str(info.get("username") or "")



def invalidate_client_token(token: str) -> bool:
    """使客户端 Token 失效"""
    if token in active_client_tokens:
        user_info = active_client_tokens.pop(token)
        log.info(f"Invalidated client token for user: {user_info['username']}")
        return True
    return False


def invalidate_client_tokens_by_username(username: str) -> int:
    """按用户名批量使客户端 Token 失效，返回失效数量"""
    uname = (username or "").strip()
    if not uname:
        return 0

    targets = [
        t for t, info in list(active_client_tokens.items())
        if str(info.get("username") or "") == uname
    ]
    for t in targets:
        active_client_tokens.pop(t, None)

    if targets:
        log.warning(f"Force invalidated {len(targets)} token(s) for user: {uname}")
    return len(targets)

