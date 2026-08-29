from __future__ import annotations

"""Safe, provider-neutral data exposed to desktop Clients."""

from typing import Any


_ERROR_MESSAGES = {
    "BROKER_OFFLINE": "交易服务暂不可用",
    "NO_BROKER": "交易服务暂不可用",
    "BROKER_CONNECT_FAILED": "交易服务暂不可用",
    "TRADING_SERVICE_UNAVAILABLE": "交易服务暂不可用",
    "ORDER_NOT_SUPPORTED": "当前交易通道不支持下单",
    "ORDER_CANCEL_NOT_SUPPORTED": "当前交易通道不支持撤单",
    "POSITION_NOT_SUPPORTED": "当前交易通道不支持持仓查询",
    "ORDER_QUERY_NOT_SUPPORTED": "当前交易通道不支持订单查询",
    "QUOTE_NOT_SUPPORTED": "当前交易通道不支持行情订阅",
    "ORDER_ROUTE_UNAVAILABLE": "当前账户或股票不支持所选ROUTE，订单未提交，请改用SMART",
    "ORDER_INVALID_SYMBOL": "请输入正确的股票代码",
    "ORDER_INVALID_QTY": "订单数量无效",
    "ORDER_INVALID_PRICE": "订单价格无效",
    "ORDER_INVALID_ACTION": "订单方向无效",
    "ORDER_INVALID_TYPE": "订单类型无效",
    "ORDER_INVALID_TIF": "订单有效期无效",
    "DUPLICATE_ORDER_BLOCKED": "相同订单提交过于频繁",
    "ORDER_REJECTED": "订单未被接受",
    "ORDER_SUBMIT_FAILED": "下单失败，请稍后重试",
    "ORDER_CANCEL_FAILED": "撤单失败，请刷新订单确认",
    "POSITION_QUERY_FAILED": "持仓查询失败，请稍后重试",
    "ORDER_QUERY_FAILED": "订单查询失败，请稍后重试",
    "ORDER_RESPONSE_INVALID": "订单状态未知，请刷新订单确认",
    "INTERNAL_ERROR": "操作失败，请稍后重试",
}


_ERROR_CODE_ALIASES = {
    "IB_UNAVAILABLE": "BROKER_OFFLINE",
    "IB_ROUTE_UNAVAILABLE": "ORDER_ROUTE_UNAVAILABLE",
}


def safe_client_error_code(code: str, default: str = "INTERNAL_ERROR") -> str:
    normalized = str(code or "").strip().upper()
    normalized = _ERROR_CODE_ALIASES.get(normalized, normalized)
    if normalized in _ERROR_MESSAGES:
        return normalized

    fallback = str(default or "INTERNAL_ERROR").strip().upper()
    fallback = _ERROR_CODE_ALIASES.get(fallback, fallback)
    return fallback if fallback in _ERROR_MESSAGES else "INTERNAL_ERROR"


def safe_client_message(code: str, raw_message: str = "", default: str = "操作失败，请稍后重试") -> str:
    normalized_code = str(code or "").strip().upper()
    normalized_code = _ERROR_CODE_ALIASES.get(normalized_code, normalized_code)
    raw = str(raw_message or "").strip().lower()
    if "buying power" in raw or "insufficient" in raw:
        return "可用资金不足"
    if "read-only" in raw or "read only" in raw or "readonly" in raw:
        return "当前账户为只读权限"
    if "market" in raw and ("closed" in raw or "outside" in raw):
        return "当前不在可交易时段"
    if "nbbo" in raw or ("price" in raw and ("range" in raw or "too far" in raw)):
        return "订单价格超出允许范围"
    if "not cancellable" in raw or "cannot cancel" in raw:
        return "当前订单不可撤销"
    if "not found" in raw or "no security definition" in raw:
        return "未找到对应的交易对象"
    if normalized_code in _ERROR_MESSAGES:
        return _ERROR_MESSAGES[normalized_code]
    return default


def safe_order_status_message(payload: dict[str, Any]) -> str:
    status = str(payload.get("status") or "").strip().lower()
    code = str(payload.get("code") or payload.get("error_code") or "").strip()
    raw = str(payload.get("status_message") or payload.get("reject_reason") or payload.get("message") or "")
    if status == "rejected" or code:
        return safe_client_message(code or "ORDER_REJECTED", raw, "订单未被接受")
    return ""


def safe_error_payload(payload: dict[str, Any], *, default_code: str = "INTERNAL_ERROR") -> dict[str, Any]:
    result = dict(payload or {})
    if result.get("success") is not False:
        return result
    code = safe_client_error_code(
        str(result.get("code") or result.get("error_code") or ""),
        default_code,
    )
    result["code"] = code
    result["error_code"] = code
    result["message"] = safe_client_message(code, str(result.get("message") or ""))
    result.pop("status_message", None)
    result.pop("reject_reason", None)
    return result


def safe_order_record(order: dict[str, Any]) -> dict[str, Any]:
    result = dict(order or {})
    message = safe_order_status_message(result)
    raw_code = str(result.get("code") or result.get("error_code") or "").strip()
    if raw_code:
        public_code = safe_client_error_code(raw_code, "ORDER_REJECTED")
        if "code" in result:
            result["code"] = public_code
        if "error_code" in result:
            result["error_code"] = public_code
    for key in (
        "account", "account_id", "account_number", "broker_type", "gateway",
        "client_id", "reject_reason", "raw", "raw_order", "message", "error",
    ):
        result.pop(key, None)
    result["status_message"] = message
    return result
