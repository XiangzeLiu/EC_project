"""
Trading Service — 统一交易操作入口

职责:
  - 作为 ws_server handler 与 api/ 层之间的桥梁
  - 统一错误处理和日志记录
  - 操作来源追踪（记录 client session_id）
  
所有 Client 发来的交易指令都经过此模块转发给当前 Broker API 实例。
"""

import logging
import time
import uuid

from ..config import state
from .config_sync import get_current_broker, ensure_broker_connected

log = logging.getLogger("server_economic.trading_svc")


async def place_order(params: dict, session_id: str, trace_id: str = "") -> dict:
    """
    下单
    
    Args:
        params: {"symbol", "qty", "price", "action", "order_type", "tif"}
        session_id: 来源客户端 session_id（用于日志追踪）
    
    Returns:
        标准响应字典
    """
    if not await ensure_broker_connected():
        return _error("BROKER_OFFLINE", "Broker not connected", trace_id=trace_id, retryable=True)

    broker = get_current_broker()
    if not broker:
        return _error("NO_BROKER", "No active broker connection", trace_id=trace_id, retryable=True)
    
    symbol = params.get("symbol", "")
    action = params.get("action", "")
    qty = params.get("qty", 1)
    price = params.get("price", 0.0)
    order_type = params.get("order_type", "limit")
    tif = params.get("tif", "Day")
    
    trace_id = _mk_trace_id(trace_id)
    log.info(
        f"[{session_id}][{trace_id}] PLACE_ORDER: {action} {qty}x {symbol} "
        f"@{price} ({order_type}/{tif})"
    )
    
    try:
        result = await broker.place_order({
            "symbol": symbol,
            "qty": int(qty),
            "price": float(price),
            "action": action,
            "order_type": order_type,
            "tif": tif,
        })
        order_id = result.get("order_id", "")
        log.info(f"[{session_id}][{trace_id}] PLACE_ORDER OK: order_id={order_id}")
        return _ok({
            "order_id": order_id,
            "status": "filled",  # TODO: 实际应查询订单状态
        }, code="ORDER_OK", trace_id=trace_id)
    except NotImplementedError as e:
        log.warning(f"[{session_id}][{trace_id}] PLACE_ORDER not supported: {e}")
        return _error("ORDER_NOT_SUPPORTED", str(e), trace_id=trace_id)
    except Exception as e:
        log.error(f"[{session_id}][{trace_id}] PLACE_ORDER ERROR: {e}")
        return _error("ORDER_SUBMIT_FAILED", str(e)[:200], trace_id=trace_id)


async def cancel_order(order_id: str, session_id: str, trace_id: str = "") -> dict:
    """
    撤单
    """
    if not await ensure_broker_connected():
        return _error("BROKER_OFFLINE", "Broker not connected", trace_id=trace_id, retryable=True)

    broker = get_current_broker()
    if not broker:
        return _error("NO_BROKER", "No active broker connection", trace_id=trace_id, retryable=True)
    
    trace_id = _mk_trace_id(trace_id)
    log.info(f"[{session_id}][{trace_id}] CANCEL_ORDER: {order_id}")
    
    try:
        await broker.cancel_order(order_id)
        log.info(f"[{session_id}][{trace_id}] CANCEL_ORDER OK: {order_id}")
        return _ok({"order_id": order_id}, code="ORDER_CANCEL_OK", trace_id=trace_id)
    except NotImplementedError as e:
        return _error("ORDER_CANCEL_NOT_SUPPORTED", str(e), trace_id=trace_id)
    except Exception as e:
        log.error(f"[{session_id}][{trace_id}] CANCEL_ORDER ERROR: {e}")
        return _error("ORDER_CANCEL_FAILED", str(e)[:200], trace_id=trace_id)


async def get_positions(filters: dict | None = None, session_id: str = "", trace_id: str = "") -> dict:
    """
    查询持仓
    """
    if not await ensure_broker_connected():
        return _error("BROKER_OFFLINE", "Broker not connected", trace_id=trace_id, retryable=True)

    broker = get_current_broker()
    if not broker:
        return _error("NO_BROKER", "No active broker connection", trace_id=trace_id, retryable=True)
    
    trace_id = _mk_trace_id(trace_id)
    log.info(f"[{session_id}][{trace_id}] POSITION_QUERY")
    
    try:
        positions = await broker.get_positions(filters=filters)
        log.info(f"[{session_id}][{trace_id}] POSITION_QUERY OK: {len(positions)} items")
        return _ok({
            "positions": positions,
            "count": len(positions),
        }, code="POSITION_OK", trace_id=trace_id)
    except NotImplementedError as e:
        return _error("POSITION_NOT_SUPPORTED", str(e), trace_id=trace_id)
    except Exception as e:
        log.error(f"[{session_id}][{trace_id}] POSITION_QUERY ERROR: {e}")
        return _error("POSITION_QUERY_FAILED", str(e)[:200], trace_id=trace_id)


async def get_orders(mode: str = "live", session_id: str = "", trace_id: str = "") -> dict:
    """
    查询订单列表

    Args:
        mode: "live" | "all"
    """
    if not await ensure_broker_connected():
        return _error("BROKER_OFFLINE", "Broker not connected", trace_id=trace_id, retryable=True)

    broker = get_current_broker()
    if not broker:
        return _error("NO_BROKER", "No active broker connection", trace_id=trace_id, retryable=True)

    mode = (mode or "live").lower()
    if mode not in ("live", "all"):
        mode = "live"

    trace_id = _mk_trace_id(trace_id)
    log.info(f"[{session_id}][{trace_id}] ORDER_QUERY: mode={mode}")

    try:
        orders = await broker.get_orders(mode=mode)
        return _ok({
            "orders": orders,
            "count": len(orders),
            "mode": mode,
        }, code="ORDER_QUERY_OK", trace_id=trace_id)
    except NotImplementedError as e:
        return _error("ORDER_QUERY_NOT_SUPPORTED", str(e), trace_id=trace_id)
    except Exception as e:
        log.error(f"[{session_id}][{trace_id}] ORDER_QUERY ERROR: {e}")
        return _error("ORDER_QUERY_FAILED", str(e)[:200], trace_id=trace_id)


# ── 内部辅助 ──────────────────────────────────────────────────

def _mk_trace_id(trace_id: str = "") -> str:
    return trace_id or f"trc_{uuid.uuid4().hex[:16]}"


def _ok(data: dict | None = None, code: str = "OK", message: str = "ok",
        trace_id: str = "", source: str = "SE") -> dict:
    payload = {
        "success": True,
        "code": code,
        "message": message,
        "retryable": False,
        "source": source,
        "trace_id": _mk_trace_id(trace_id),
    }
    if data:
        payload.update(data)
    return payload


def _error(code: str, message: str, trace_id: str = "", retryable: bool = False,
           source: str = "SE") -> dict:
    """构建标准错误响应（兼容旧字段 error_code）"""
    return {
        "success": False,
        "code": code,
        "error_code": code,
        "message": message,
        "retryable": retryable,
        "source": source,
        "trace_id": _mk_trace_id(trace_id),
    }
