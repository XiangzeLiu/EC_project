"""
消息日志服务 — 记录 Client 与 SE 之间的消息交互

功能:
  - 记录每个 Client 的连接/断开事件
  - 记录收到的请求消息 (ORDER_SUBMIT / POSITION_QUERY / QUOTE_SUBSCRIBE 等)
  - 记录发送的响应消息 (ORDER_RESPONSE / QUOTE_DATA / ERROR 等)
  - 提供 /api/logs 端点供 GUI 面板读取

数据格式:
  {
    "timestamp": "15:30:45",
    "level": "info|recv|send|conn|err",
    "session_id": "sess_xxx",
    "summary": "ORDER_SUBMIT AAPL Buy 10 @185.0",
    "detail": { ... }   // 可选，完整 payload
  }
"""

import logging
import threading
from collections import deque
from datetime import datetime
from typing import Any

log = logging.getLogger("server_economic.msg_log")

# 全局单例
_MAX_ENTRIES = 500  # 最多保留 500 条
_entries: deque[dict] = deque(maxlen=_MAX_ENTRIES)
_lock = threading.Lock()


def _now_str() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def add(level: str, session_id: str, summary: str, detail: dict | None = None, trace_id: str = ""):
    """追加一条日志"""
    entry = {
        "timestamp": _now_str(),
        "level": level,
        "session_id": session_id or "-",
        "trace_id": trace_id or "-",
        "summary": summary,
        "detail": detail or {},
    }
    with _lock:
        _entries.append(entry)
    # 同时输出到标准日志（仅 summary）
    level_map = {"recv": "info", "send": "info", "conn": "info", "err": "warning"}
    lv = level_map.get(level, "info")
    trace_tag = f"[{trace_id}]" if trace_id else ""
    getattr(log, lv)(f"[{session_id}]{trace_tag} {summary}")


# ── 便捷方法 ────────────────────────────────────────────────────

def on_connect(session_id: str, client_info: str = "", trace_id: str = ""):
    add("conn", session_id, f"Client 已连接 {client_info}", trace_id=trace_id)


def on_disconnect(session_id: str, trace_id: str = ""):
    add("conn", session_id, "Client 断开连接", trace_id=trace_id)


def on_recv(session_id: str, msg_type: str, payload: dict | None = None, trace_id: str = ""):
    """记录收到 Client 的请求"""
    # 根据类型生成可读摘要
    sym = ""
    if payload and isinstance(payload, dict):
        sym = payload.get("symbol", "") or payload.get("symbols", "")
        if isinstance(sym, list):
            sym = ", ".join(sym[:5])
            if len(payload.get("symbols", [])) > 5:
                sym += f" (+{len(payload['symbols'])-5} more)"

    summaries = {
        "CONNECT": f"认证连接请求",
        "PING": f"心跳 PING",
        "ORDER_SUBMIT": f"下单请求 {sym}" if sym else "下单请求",
        "ORDER_CANCEL": f"撤单请求 {payload.get('order_id','')}" if (payload and payload.get('order_id')) else "撤单请求",
        "POSITION_QUERY": f"持仓查询 {sym}" if sym else "持仓查询",
        "QUOTE_SUBSCRIBE": _quote_summary(payload),
        "ECONOMIC_DATA_QUERY": "经济数据查询",
        "STATUS_QUERY": "状态查询",
        "SUMMARY_REPORT": "摘要报告请求",
    }
    summary = summaries.get(msg_type, f"[{msg_type}]")
    add("recv", session_id, summary, payload, trace_id=trace_id)


def _quote_summary(payload: dict | None) -> str:
    if not payload:
        return "行情订阅"
    action = payload.get("action", "subscribe")
    symbols = payload.get("symbols", [])
    label = "订阅" if action == "subscribe" else "取消订阅"
    if isinstance(symbols, list) and symbols:
        syms = ", ".join(symbols[:4])
        if len(symbols) > 4:
            syms += f" (+{len(symbols)-4})"
        return f"行情{label} [{syms}]"
    return f"行情{label}"


def on_send(session_id: str, msg_type: str, payload: dict | None = None, success: bool = True, trace_id: str = ""):
    """记录发给 Client 的响应"""
    success_map = {
        "ORDER_RESPONSE": lambda p: ("下单成功" if (p and p.get("success")) else "下单失败"),
        "ORDER_CANCEL_RESPONSE": lambda p: ("撤单成功" if (p and p.get("success")) else "撤单失败"),
        "POSITION_RESPONSE": lambda p: (f"持仓 {p.get('count',0)} 条" if (p and p.get('success')) else "持仓查询失败"),
        "QUOTE_ACK": lambda p: (f"行情已确认 ({p.get('total_subscribed',0)} 个)" if (p and p.get('success')) else "行情操作失败"),
        "QUOTE_DATA": lambda p: (f"行情推送 {p.get('symbol','')}") if p else "行情推送",
        "ERROR": lambda p: f"错误: {p.get('message','unknown') if p else '?'}",
        "STATUS_RESPONSE": lambda _: "状态响应",
        "SUMMARY_RESPONSE": lambda _: "摘要响应",
        "ECONOMIC_DATA_RESPONSE": lambda _: "经济数据响应",
    }

    fn = success_map.get(msg_type)
    summary = fn(payload) if fn else f"[{msg_type}]"
    lvl = "send" if (msg_type != "ERROR" and success) else "err"
    add(lvl, session_id, summary, payload, trace_id=trace_id)


def on_auth(session_id: str, success: bool, reason: str = "", trace_id: str = ""):
    if success:
        add("conn", session_id, "认证通过 ✓", trace_id=trace_id)
    else:
        add("err", session_id, f"认证失败: {reason}", trace_id=trace_id)


# ── 读取接口 ────────────────────────────────────────────────────

def get_recent(limit: int = 100) -> list[dict]:
    """获取最近 N 条日志（新→旧）"""
    with _lock:
        data = list(_entries)
    return data[-limit:][::-1]  # 最新的在前


def get_stats() -> dict:
    """获取统计摘要"""
    with _lock:
        data = list(_entries)
    total = len(data)
    conns = sum(1 for e in data if e["level"] == "conn")
    recvs = sum(1 for e in data if e["level"] == "recv")
    sends = sum(1 for e in data if e["level"] == "send")
    errs = sum(1 for e in data if e["level"] == "err")
    sessions = set(e["session_id"] for e in data if e["session_id"] != "-")
    return {
        "total": total,
        "connections": conns,
        "requests": recvs,
        "responses": sends,
        "errors": errs,
        "active_sessions": len(sessions),
    }


def clear():
    """清空日志"""
    with _lock:
        _entries.clear()
