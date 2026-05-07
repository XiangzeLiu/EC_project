"""
Session Cache Service
Tastytrade Session 与 Account 对象的缓存与复用
减少重复 API 调用，降低延迟
"""

import asyncio
import logging

from config import session_store, log
from tastytrade_svc import _create_session_account

# Session 缓存过期时间（秒）
SESSION_TTL = 300  # 5分钟


async def get_fresh():
    """
    获取有效的 Session 和 Account 对象
    优先使用缓存，过期或不存在时重新创建

    Returns:
        (Session, Account) 元组
    """
    if session_store.get("session") and session_store.get("account"):
        return session_store["session"], session_store["account"]

    s, a = await _create_session_account()
    session_store["session"] = s
    session_store["account"] = a
    return s, a


def invalidate_cache():
    """清除 Session 缓存，强制下次重新获取"""
    session_store["session"] = None
    session_store["account"] = None
    log.info("Session cache invalidated")
