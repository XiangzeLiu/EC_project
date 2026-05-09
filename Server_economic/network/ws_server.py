"""
WebSocket Server — 供 Client 客户端建立长连接

职责:
  - 监听 Client 的 WebSocket 连接请求
  - Bearer Token 认证（复用 SM 分发的 token 或 SE 自签发）
  - 心跳 PING/PONG 保活
  - 消息路由分发
  - 连接生命周期管理

消息协议 (§4.4):
  type: CONNECT / PING / PONG / ORDER_SUBMIT / ORDER_CANCEL /
       POSITION_QUERY / ECONOMIC_DATA_QUERY / ...
"""

import asyncio
import json
import logging
import time

from fastapi import WebSocket, WebSocketDisconnect

from ..config import state

log = logging.getLogger("server_economic.ws_server")

# 活跃连接: {websocket: {"session_id": ..., "connected_at": ..., "auth": ...}}
_connections: dict[WebSocket, dict] = {}

# 心跳超时阈值（秒）
_WS_HEARTBEAT_TIMEOUT = 90


async def handle_client_connection(ws: WebSocket):
    """
    处理单个 Client WebSocket 连接的完整生命周期

    流程:
      1. 接受连接
      2. 等待认证消息（CONNECT + Token）
      3. 进入主循环：接收/分发消息
      4. 心跳超时检测
      5. 断开清理
    """
    await ws.accept()
    session_id = f"sess_{secrets_token(8)}"
    connected_at = time.time()
    authed = False

    log.info(f"[{session_id}] WS connected from {ws.client.host}:{ws.client.port}")

    try:
        # 阶段1: 等待认证（30s 超时）
        auth_msg = await asyncio.wait_for(
            ws.receive_text(), timeout=30.0
        )

        try:
            msg = json.loads(auth_msg)
        except json.JSONDecodeError:
            await _send_error(ws, "INVALID_FORMAT", "JSON 解析失败")
            await ws.close(code=4001)
            return

        if msg.get("type") != "CONNECT":
            await _send_error(ws, "AUTH_REQUIRED", "首条消息必须是 CONNECT")
            await ws.close(code=4002)
            return

        # 验证 Token
        client_token = msg.get("payload", {}).get("token", "")
        if not _validate_client_token(client_token):
            await _send_error(ws, "TOKEN_INVALID", "认证令牌无效或已过期")
            await ws.close(code=4003)
            log.warning(f"[{session_id}] Auth failed: invalid token")
            return

        authed = True
        _connections[ws] = {
            "session_id": session_id,
            "connected_at": connected_at,
            "auth": True,
            "last_pong": time.time(),
        }

        # 发送连接确认
        ack = {
            "type": "CONNECT_ACK",
            "id": f"ack_{session_id}",
            "timestamp": int(time.time() * 1000),
            "payload": {
                "status": "SUCCESS",
                "session_id": session_id,
                "node_info": {
                    "server_id": state.server_id,
                    "node_name": state.node_name,
                    "region": state.region,
                    "status": state.status,
                },
                "heartbeat_interval": 30,
            },
        }
        await ws.send_json(ack)
        log.info(f"[{session_id}] Auth OK, connection established")

        # 阶段2: 主循环 — 接收并处理消息
        while True:
            try:
                raw = await asyncio.wait_for(
                    ws.receive_text(), timeout=_WS_HEARTBEAT_TIMEOUT
                )
            except asyncio.TimeoutError:
                # 心跳超时，断开
                log.warning(f"[{session_id}] Heartbeat timeout ({_WS_HEARTBEAT_TIMEOUT}s)")
                break

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await _send_error(ws, "INVALID_FORMAT", "非 JSON 消息")
                continue

            msg_type = msg.get("type", "")
            ts = int(time.time() * 1000)

            # 心跳处理
            if msg_type == "PING":
                _connections[ws]["last_pong"] = time.time()
                await ws.send_json({
                    "type": "PONG", "id": msg.get("id", ""),
                    "timestamp": ts, "payload": {},
                })
                continue

            # 业务消息路由
            response = await _route_message(msg_type, msg, session_id)
            if response:
                await ws.send_json(response)

    except WebSocketDisconnect:
        log.info(f"[{session_id}] WS disconnected by client")
    except Exception as e:
        log.error(f"[{session_id}] WS error: {e}", exc_info=True)
    finally:
        # 清理连接
        _connections.pop(ws, None)
        state.ws_clients = [w for w in state.ws_clients if w != ws]
        log.info(
            f"[{session_id}] Connection cleaned up "
            f"(remaining connections: {len(_connections)})"
        )


# ── 内部函数 ────────────────────────────────────────────────────────────

def secrets_token(n: int = 16) -> str:
    """生成随机 Token 片段"""
    import secrets
    return secrets.token_hex(n)


def _validate_client_token(token: str) -> bool:
    """
    验证 Client 提供的 Token

    当前策略：
      - 空Token 允许通过（开发模式）
      - 后续可对接 Server_manager 的客户端认证 API
    """
    if not token:
        log.debug("Empty token accepted (dev mode)")
        return True
    # TODO: 调用 Server_manager POST /auth/verify-token 验证
    return True


def _send_error(ws: WebSocket, code: str, message: str):
    """发送错误响应"""
    resp = {
        "type": "ERROR",
        "id": "",
        "timestamp": int(time.time() * 1000),
        "payload": {"code": code, "message": message},
    }
    try:
        asyncio.create_task(ws.send_json(resp))
    except Exception:
        pass


async def _route_message(msg_type: str, msg: dict, session_id: str) -> dict | None:
    """
    根据消息类型路由到对应处理器

    Args:
        msg_type: 消息类型标识
        msg: 完整消息字典
        session_id: 会话 ID

    Returns:
        响应消息字典或 None（不需要回复的消息返回 None）
    """
    handler = _MESSAGE_HANDLERS.get(msg_type)
    if handler:
        try:
            return await handler(msg, session_id)
        except Exception as e:
            log.error(f"Handler error for {msg_type}: {e}")
            return _error_response("INTERNAL_ERROR", str(e)[:100])

    log.warning(f"Unknown message type: {msg_type} from {session_id}")
    return _error_response("UNKNOWN_TYPE", f"不支持的消息类型: {msg_type}")


# ── 消息处理器 ────────────────────────────────────────────────────────

async def _handle_economic_query(msg: dict, sid: str) -> dict:
    """查询经济指标数据"""
    from ..services.economic_data import get_indicator, get_all_indicators

    payload = msg.get("payload", {})
    indicator = payload.get("indicator")
    log.info(f"[{sid}] ECONOMIC_DATA_QUERY: indicator={indicator or '(all)'}")

    if indicator:
        data = get_indicator(indicator)
    else:
        data = get_all_indicators()

    log.info(f"[{sid}] ECONOMIC_DATA_RESPONSE: {len(data) if isinstance(data, dict) else 1} indicators returned")
    return {
        "type": "ECONOMIC_DATA_RESPONSE",
        "id": msg.get("id", ""),
        "timestamp": int(time.time() * 1000),
        "payload": {"data": data or {}, "status": "ok"},
    }


async def _handle_status_query(msg: dict, sid: str) -> dict:
    """查询节点状态"""
    log.info(f"[{sid}] STATUS_QUERY")
    return {
        "type": "STATUS_RESPONSE",
        "id": msg.get("id", ""),
        "timestamp": int(time.time() * 1000),
        "payload": {
            "status": "ok",
            "node_info": {
                "server_id": state.server_id,
                "node_name": state.node_name,
                "region": state.region,
                "registration_status": state.status,
                "heartbeat_ok": state.heartbeat_ok,
                "heartbeat_fail_count": state.heartbeat_fail_count,
                "connections": len(_connections),
            },
        },
    }


async def _handle_summary_report(msg: dict, sid: str) -> dict:
    """获取经济数据摘要报告"""
    from ..services.economic_data import generate_summary_report
    log.info(f"[{sid}] SUMMARY_REPORT")
    report = generate_summary_report()

    return {
        "type": "SUMMARY_REPORT",
        "id": msg.get("id", ""),
        "timestamp": int(time.time() * 1000),
        "payload": {"report": report, "status": "ok"},
    }


def _error_response(code: str, message: str) -> dict:
    return {
        "type": "ERROR",
        "id": "",
        "timestamp": int(time.time() * 1000),
        "payload": {"code": code, "message": message},
    }


_MESSAGE_HANDLERS: dict[str, callable] = {
    "ECONOMIC_DATA_QUERY": _handle_economic_query,
    "STATUS_QUERY": _handle_status_query,
    "SUMMARY_REPORT": _handle_summary_report,
}


# ── 广播工具 ────────────────────────────────────────────────────────────

async def broadcast_message(message: dict | str):
    """向所有已连接的 Client 广播消息"""
    if isinstance(message, dict):
        text = json.dumps(message, ensure_ascii=False)
    else:
        text = message

    disconnected = []
    for ws in list(_connections.keys()):
        try:
            await ws.send_text(text)
        except Exception:
            disconnected.append(ws)

    for ws in disconnected:
        _connections.pop(ws, None)


def get_connection_count() -> int:
    """获取当前活跃连接数"""
    return len(_connections)
