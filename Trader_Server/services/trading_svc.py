from __future__ import annotations

"""Trading service entry points used by the WebSocket server."""

import logging
import time
import uuid
from typing import Any

from .config_sync import ensure_broker_connected, get_broker_status, get_current_broker
from .client_security import safe_client_message, safe_order_record

log = logging.getLogger("trader_server.trading_svc")

ORDER_DUPLICATE_WINDOW_SECONDS = 0.0
_ORDER_RECENT: dict[str, float] = {}
_VALID_ACTIONS = {"Buy to Open", "Buy to Close", "Sell to Open", "Sell to Close"}
_VALID_ORDER_TYPES = {"limit", "market"}
_VALID_TIFS = {"Day", "GTC", "IOC", "EXT", "GTC_EXT"}



def _mk_trace_id(trace_id: str = "") -> str:
    return trace_id or f"trc_{uuid.uuid4().hex[:16]}"


def _ok(
    data: dict[str, Any] | None = None,
    code: str = "OK",
    message: str = "ok",
    trace_id: str = "",
    source: str = "TS",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "success": True,
        "code": code,
        "message": safe_client_message(code, message),
        "retryable": False,
        "source": source,
        "trace_id": _mk_trace_id(trace_id),
    }
    if data:
        payload.update(data)
    return payload


def _error(
    code: str,
    message: str,
    trace_id: str = "",
    retryable: bool = False,
    source: str = "TS",
) -> dict[str, Any]:
    return {
        "success": False,
        "code": code,
        "error_code": code,
        "message": message,
        "retryable": retryable,
        "source": source,
        "trace_id": _mk_trace_id(trace_id),
    }


def _ensure_capability(broker: Any, capability: str, code: str, message: str, trace_id: str = "") -> dict[str, Any] | None:
    caps_fn = getattr(broker, "effective_capabilities", None)
    if not callable(caps_fn):
        caps_fn = getattr(broker, "capabilities", None)
    caps = caps_fn() if callable(caps_fn) else {}
    if caps and not bool(caps.get(capability, False)):
        return _error(code, message, trace_id=trace_id, retryable=False)
    return None

def _validate_order_params(params: dict[str, Any], trace_id: str = "") -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    symbol = str(params.get("symbol") or "").strip().upper()
    action = str(params.get("action") or "").strip()
    order_type = str(params.get("order_type") or "limit").strip().lower() or "limit"
    tif = str(params.get("tif") or "Day").strip() or "Day"
    try:
        qty = int(params.get("qty") or 0)
    except (TypeError, ValueError):
        return None, _error("ORDER_INVALID_QTY", "Order quantity must be an integer", trace_id=trace_id)

    try:
        price = float(params.get("price") or 0.0)
    except (TypeError, ValueError):
        return None, _error("ORDER_INVALID_PRICE", "Order price must be a number", trace_id=trace_id)

    if not symbol or not symbol.replace(".", "").replace("-", "").isalnum():
        return None, _error("ORDER_INVALID_SYMBOL", "Order symbol is required", trace_id=trace_id)
    if qty <= 0:
        return None, _error("ORDER_INVALID_QTY", "Order quantity must be greater than 0", trace_id=trace_id)
    if action not in _VALID_ACTIONS:
        return None, _error("ORDER_INVALID_ACTION", "Order action is invalid", trace_id=trace_id)
    if order_type not in _VALID_ORDER_TYPES:
        return None, _error("ORDER_INVALID_TYPE", "Order type is invalid", trace_id=trace_id)
    if tif not in _VALID_TIFS:
        return None, _error("ORDER_INVALID_TIF", "Time in force is invalid", trace_id=trace_id)
    if order_type == "limit" and price <= 0:
        return None, _error("ORDER_INVALID_PRICE", "Limit order price must be greater than 0", trace_id=trace_id)
    if order_type == "market":
        price = 0.0

    normalized: dict[str, Any] = {
        "symbol": symbol,
        "action": action,
        "qty": qty,
        "price": price,
        "order_type": order_type,
        "tif": tif,
    }
    if "route" in params:
        normalized["route"] = str(params.get("route") or "").strip().upper()
    if "hidden" in params:
        normalized["hidden"] = bool(params.get("hidden", False))
    return normalized, None


def _check_duplicate_order(order: dict[str, Any], session_id: str, username: str, trace_id: str = "") -> dict[str, Any] | None:
    if ORDER_DUPLICATE_WINDOW_SECONDS <= 0:
        return None
    now = time.monotonic()
    stale_before = now - max(ORDER_DUPLICATE_WINDOW_SECONDS * 4, 10.0)
    for key, ts in list(_ORDER_RECENT.items()):
        if ts < stale_before:
            _ORDER_RECENT.pop(key, None)

    identity = username or session_id or "anonymous"
    key = "|".join([
        identity,
        order["symbol"],
        order["action"],
        str(order["qty"]),
        f"{order['price']:.6f}",
        order["order_type"],
        order["tif"],
    ])
    last = _ORDER_RECENT.get(key)
    if last is not None and now - last < ORDER_DUPLICATE_WINDOW_SECONDS:
        return _error(
            "DUPLICATE_ORDER_BLOCKED",
            "Duplicate order blocked by TS safety window",
            trace_id=trace_id,
            retryable=False,
        )
    _ORDER_RECENT[key] = now
    return None


async def _get_ready_broker(username: str, server_id: str, trace_id: str) -> tuple[Any | None, dict[str, Any] | None]:
    if not await ensure_broker_connected():
        public_status = get_broker_status(public=True)
        message = str((public_status.get("error") or {}).get("message") or "Broker not connected")
        return None, _error("BROKER_OFFLINE", message, trace_id=trace_id, retryable=True)

    broker = get_current_broker()
    if not broker:
        return None, _error("NO_BROKER", "No active broker connection", trace_id=trace_id, retryable=True)
    return broker, None


async def place_order(params: dict[str, Any], session_id: str, username: str = "", server_id: str = "", trace_id: str = "") -> dict[str, Any]:
    trace_id = _mk_trace_id(trace_id)
    broker, err = await _get_ready_broker(username, server_id, trace_id)
    if err:
        return err

    cap_err = _ensure_capability(broker, "orders", "ORDER_NOT_SUPPORTED", "does not support order placement", trace_id)
    if cap_err:
        return cap_err

    order, validation_err = _validate_order_params(params, trace_id=trace_id)
    if validation_err:
        return validation_err

    duplicate_err = _check_duplicate_order(order, session_id=session_id, username=username, trace_id=trace_id)
    if duplicate_err:
        log.warning("[%s][%s] PLACE_ORDER duplicate blocked: %s", session_id, trace_id, order)
        return duplicate_err

    symbol = order["symbol"]
    action = order["action"]
    qty = order["qty"]
    price = order["price"]
    order_type = order["order_type"]
    tif = order["tif"]

    route = str(order.get("route") or "")
    hidden = bool(order.get("hidden", False))

    log.info("[%s][%s] PLACE_ORDER: %s %sx %s @%s (%s/%s)", session_id, trace_id, action, qty, symbol, price, order_type, tif)
    log.info(
        "[%s][%s][ORDER_DIAG][TS_SUBMIT] username=%s server_id=%s symbol=%s action=%s qty=%s price=%s order_type=%s tif=%s route=%s hidden=%s",
        session_id,
        trace_id,
        username or "",
        server_id or "",
        symbol,
        action,
        qty,
        price,
        order_type,
        tif,
        route,
        hidden,
    )

    try:
        result = await broker.place_order(order)
        order_id = result.get("order_id", "")
        if result.get("success") is False:
            code = str(result.get("code") or "ORDER_REJECTED")
            message = str(
                result.get("status_message")
                or result.get("message")
                or "Order rejected by broker"
            )
            log.warning(
                "[%s][%s] PLACE_ORDER rejected: order_id=%s code=%s",
                session_id,
                trace_id,
                order_id,
                code,
            )
            log.warning(
                "[%s][%s][ORDER_DIAG][TS_REJECT] order_id=%s code=%s message=%s symbol=%s action=%s qty=%s price=%s order_type=%s tif=%s route=%s hidden=%s",
                session_id,
                trace_id,
                order_id,
                code,
                message,
                symbol,
                action,
                qty,
                price,
                order_type,
                tif,
                route,
                hidden,
            )
            return _error(code, message, trace_id=trace_id)
        log.info("[%s][%s] PLACE_ORDER OK: order_id=%s", session_id, trace_id, order_id)
        return _ok({"order_id": order_id, "status": result.get("status", "submitted")}, code="ORDER_OK", trace_id=trace_id)
    except NotImplementedError as exc:
        log.warning("[%s][%s] PLACE_ORDER not supported: %s", session_id, trace_id, exc)
        return _error("ORDER_NOT_SUPPORTED", str(exc), trace_id=trace_id)
    except Exception as exc:
        log.error("[%s][%s] PLACE_ORDER ERROR: %s", session_id, trace_id, exc)
        return _error("ORDER_SUBMIT_FAILED", str(exc)[:200], trace_id=trace_id)


async def cancel_order(order_id: str, session_id: str, username: str = "", server_id: str = "", trace_id: str = "") -> dict[str, Any]:
    trace_id = _mk_trace_id(trace_id)
    broker, err = await _get_ready_broker(username, server_id, trace_id)
    if err:
        return err

    cap_err = _ensure_capability(broker, "cancel_order", "ORDER_CANCEL_NOT_SUPPORTED", "does not support order cancellation", trace_id)
    if cap_err:
        return cap_err

    log.info("[%s][%s] CANCEL_ORDER: %s", session_id, trace_id, order_id)

    try:
        await broker.cancel_order(order_id)
        log.info("[%s][%s] CANCEL_ORDER OK: %s", session_id, trace_id, order_id)
        return _ok({"order_id": order_id}, code="ORDER_CANCEL_OK", trace_id=trace_id)
    except NotImplementedError as exc:
        return _error("ORDER_CANCEL_NOT_SUPPORTED", str(exc), trace_id=trace_id)
    except Exception as exc:
        log.error("[%s][%s] CANCEL_ORDER ERROR: %s", session_id, trace_id, exc)
        return _error("ORDER_CANCEL_FAILED", str(exc)[:200], trace_id=trace_id)


async def get_positions(filters: dict[str, Any] | None = None, session_id: str = "", username: str = "", server_id: str = "", trace_id: str = "") -> dict[str, Any]:
    trace_id = _mk_trace_id(trace_id)
    broker, err = await _get_ready_broker(username, server_id, trace_id)
    if err:
        return err

    cap_err = _ensure_capability(broker, "positions", "POSITION_NOT_SUPPORTED", "does not support position query", trace_id)
    if cap_err:
        return cap_err

    log.info("[%s][%s] POSITION_QUERY", session_id, trace_id)

    try:
        positions = await broker.get_positions(filters=filters)
        log.info("[%s][%s] POSITION_QUERY OK: %s items", session_id, trace_id, len(positions))
        return _ok({"positions": positions, "count": len(positions)}, code="POSITION_OK", trace_id=trace_id)
    except NotImplementedError as exc:
        return _error("POSITION_NOT_SUPPORTED", str(exc), trace_id=trace_id)
    except Exception as exc:
        log.error("[%s][%s] POSITION_QUERY ERROR: %s", session_id, trace_id, exc)
        return _error("POSITION_QUERY_FAILED", str(exc)[:200], trace_id=trace_id)


async def get_orders(mode: str = "live", session_id: str = "", username: str = "", server_id: str = "", trace_id: str = "") -> dict[str, Any]:
    trace_id = _mk_trace_id(trace_id)
    broker, err = await _get_ready_broker(username, server_id, trace_id)
    if err:
        return err

    cap_err = _ensure_capability(broker, "order_query", "ORDER_QUERY_NOT_SUPPORTED", "does not support order query", trace_id)
    if cap_err:
        return cap_err

    mode = (mode or "live").lower()
    if mode not in {"live", "all"}:
        mode = "live"

    log.info("[%s][%s] ORDER_QUERY: mode=%s", session_id, trace_id, mode)

    try:
        orders = [safe_order_record(item) for item in await broker.get_orders(mode=mode)]
        return _ok({"orders": orders, "count": len(orders), "mode": mode}, code="ORDER_QUERY_OK", trace_id=trace_id)
    except NotImplementedError as exc:
        return _error("ORDER_QUERY_NOT_SUPPORTED", str(exc), trace_id=trace_id)
    except Exception as exc:
        log.error("[%s][%s] ORDER_QUERY ERROR: %s", session_id, trace_id, exc)
        return _error("ORDER_QUERY_FAILED", str(exc)[:200], trace_id=trace_id)
