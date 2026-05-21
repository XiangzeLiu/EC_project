"""
Quote Provider Service — 统一行情入口

职责:
  - 管理 Client 的行情订阅集合
  - 将 subscribe/unsubscribe 请求委托给当前 Broker API
  - 维护 per-client 订阅状态（支持多 Client 各自订阅不同标的）

架构:
  Client A ──► QUOTE_SUBSCRIBE(["AAPL"]) ──► SE
                                            │
                                      ┌─────▼─────┐
                                      │   Broker   │ (IB TWS)
                                      │  subscribe  │
                                      └─────┬─────┘
                                            │
  Client A ◄── QUOTE_DATA({AAPL...}) ◄────┘
  Client B ◄── (如果 Client B 也订阅了 AAPL)
"""

import logging
import time
from collections import defaultdict
from typing import Set

from ..config import state
from .config_sync import get_current_broker

log = logging.getLogger("server_economic.quote_provider")

# 全局订阅状态: session_id → set of subscribed symbols
_session_subscriptions: dict[str, Set[str]] = defaultdict(set)
# 全局聚合: 所有 session 订阅的标的合集（传给 Broker 的）
_aggregated_symbols: Set[str] = set()


async def handle_subscribe(symbols: list[str], session_id: str) -> dict:
    """
    处理订阅请求
    
    Args:
        symbols: 要订阅的标的列表
        session_id: 客户端 session_id
    
    Returns:
        {"success": bool, "subscribed": list, "message": str}
    """
    broker = get_current_broker()
    if not broker:
        return {"success": False, "subscribed": [], "message": "No active broker"}
    
    # 过滤有效标的
    valid_symbols = [s.strip().upper() for s in symbols if isinstance(s, str) and s.strip()]
    
    # 计算 delta: 新增的（本 session 之前没订过的）
    existing = _session_subscriptions[session_id]
    new_syms = [s for s in valid_symbols if s not in existing]
    
    if not new_syms:
        return {"success": True, "subscribed": [], "message": "Already subscribed"}
    
    # 更新 session 级订阅
    existing.update(new_syms)
    
    # 计算全局聚合 delta 并同步到 Broker
    added_global = [s for s in new_syms if s not in _aggregated_symbols]
    _aggregated_symbols.update(new_syms)
    
    if added_global and await _is_broker_ok(broker):
        try:
            await broker.subscribe_quotes(added_global)
            log.info(f"[{session_id}] SUBSCRIBED: {new_syms} (global new: {added_global})")
        except Exception as e:
            log.error(f"[{session_id}] SUBSCRIBE error: {e}")
            # 回滚
            existing.difference_update(new_syms)
            _aggregated_symbols.difference_update(new_syms)
            return {"success": False, "subscribed": [], "message": str(e)}
    
    return {
        "success": True,
        "subscribed": new_syms,
        "total_subscribed": len(existing),
        "message": f"Subscribed {len(new_syms)} symbols",
    }


async def handle_unsubscribe(symbols: list[str], session_id: str) -> dict:
    """
    处理取消订阅请求
    """
    broker = get_current_broker()
    
    valid_symbols = [s.strip().upper() for s in symbols if isinstance(s, str) and s.strip()]
    session_set = _session_subscriptions[session_id]
    
    to_remove = [s for s in valid_symbols if s in session_set]
    
    if not to_remove:
        return {"success": True, "unsubscribed": [], "message": "Not subscribed"}
    
    # 从 session 级移除
    session_set.difference_update(to_remove)
    
    # 检查是否有其他 session 还在订阅这些标的
    global_removed = []
    for sym in to_remove:
        still_used_by_others = any(
            sym in subs for sid, subs in _session_subscriptions.items()
            if sid != session_id
        )
        if not still_used_by_others:
            _aggregated_symbols.discard(sym)
            global_removed.append(sym)
    
    # 从 Broker 取消全局不再需要的订阅
    if global_removed and broker and await _is_broker_ok(broker):
        try:
            await broker.unsubscribe_quotes(global_removed)
            log.info(f"[{session_id}] UNSUBSCRIBED: {to_remove} (global removed: {global_removed})")
        except Exception as e:
            log.error(f"[{session_id}] UNSUBSCRIBE error: {e}")
    
    return {
        "success": True,
        "unsubscribed": to_remove,
        "remaining": len(session_set),
        "message": f"Unsubscribed {len(to_remove)} symbols",
    }


def cleanup_session(session_id: str):
    """
    Client 断开时清理其订阅状态
    由 ws_server 在连接清理时调用
    """
    global _aggregated_symbols
    
    removed = _session_subscriptions.pop(session_id, set())
    if not removed:
        return
    
    # 检查每个标的是否还有其他 session 在用
    to_unsub_from_broker = []
    for sym in removed:
        still_used = any(sym in subs for subs in _session_subscriptions.values())
        if not still_used:
            _aggregated_symbols.discard(sym)
            to_unsub_from_broker.append(sym)
    
    # 异步取消 Broker 订阅
    if to_unsub_from_broker:
        broker = get_current_broker()
        if broker:
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.ensure_future(_safe_broker_unsubscribe(broker, to_unsub_from_broker))
            except RuntimeError:
                pass
    
    log.info(f"[{session_id}] Session cleaned up, unsubscribed {len(removed)} symbols "
             f"({len(to_unsub_from_broker)} removed from broker)")


def get_session_subscription_info(session_id: str) -> dict:
    """获取指定 session 的订阅信息（用于 STATUS_QUERY）"""
    syms = _session_subscriptions.get(session_id, set())
    return {
        "subscribed_count": len(syms),
        "symbols": sorted(list(syms)),
    }


# ── 内部辅助 ──────────────────────────────────────────────────

async def _is_broker_ok(broker) -> bool:
    """快速检查 broker 是否可用"""
    try:
        return await broker.is_connected()
    except Exception:
        return False


async def _safe_broker_unsubscribe(broker, symbols: list[str]):
    """安全地取消 Broker 订阅（不抛异常）"""
    try:
        if await _is_broker_ok(broker):
            await broker.unsubscribe_quotes(symbols)
    except Exception as e:
        log.warning(f"Cleanup unsubscribe error: {e}")
