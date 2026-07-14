from __future__ import annotations

"""WebSocket server used for long-lived Client connections."""

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable

from fastapi import WebSocket, WebSocketDisconnect

from ..config import state, verify_trade_service_login
from ..services import broker_gate

log = logging.getLogger("trader_server.ws_server")

_connections: dict[WebSocket, dict[str, Any]] = {}
_send_locks: dict[WebSocket, asyncio.Lock] = {}
_WS_HEARTBEAT_TIMEOUT = 90
_FORCE_DISCONNECT_CODE = 4008


def secrets_token(n: int = 16) -> str:
    import secrets

    return secrets.token_hex(n)


def _owner_connection_count(username: str, server_id: str) -> int:
    owner = ((username or '').strip(), (server_id or '').strip())
    return sum(
        1
        for meta in _connections.values()
        if ((meta.get('username') or '').strip(), (meta.get('server_id') or '').strip()) == owner
    )


def _pop_connection(ws: WebSocket) -> dict[str, Any]:
    conn = _connections.pop(ws, {})
    _send_locks.pop(ws, None)
    state.ws_clients = [client for client in state.ws_clients if client != ws]
    return conn


def _cleanup_connection_artifacts(conn: dict[str, Any]) -> None:
    sid = conn.get('session_id', '')
    username = conn.get('username', '')
    server_id = conn.get('server_id', state.server_id)

    if username and server_id and _owner_connection_count(username, server_id) == 0:
        broker_gate.start_grace(username, server_id)

    if sid:
        from ..services.message_log import on_disconnect
        from ..services.quote_provider import cleanup_session

        cleanup_session(sid)
        on_disconnect(sid)

    broker_gate.clear_expired()


async def handle_client_connection(ws: WebSocket):
    await ws.accept()
    _send_locks[ws] = asyncio.Lock()
    session_id = f"sess_{secrets_token(8)}"
    connected_at = time.time()
    client_host = getattr(ws.client, 'host', '?')
    client_port = getattr(ws.client, 'port', '?')
    log.info("[%s] WS connected from %s:%s", session_id, client_host, client_port)

    try:
        auth_msg = await asyncio.wait_for(ws.receive_text(), timeout=30.0)
        try:
            msg = json.loads(auth_msg)
        except json.JSONDecodeError:
            await _send_error(ws, 'INVALID_FORMAT', 'JSON payload required')
            await ws.close(code=4001)
            return

        if msg.get('type') != 'CONNECT':
            await _send_error(ws, 'AUTH_REQUIRED', 'First message must be CONNECT')
            await ws.close(code=4002)
            return

        first_payload = msg.get('payload', {}) if isinstance(msg.get('payload', {}), dict) else {}
        trace_id = str(first_payload.get('trace_id') or f"trc_{uuid.uuid4().hex[:16]}")
        client_token = first_payload.get('token', '')
        requested_server_id = str(first_payload.get('server_id') or '').strip()
        auth_ctx = await _validate_client_token(client_token, requested_server_id)

        if not auth_ctx.get('valid'):
            from ..services.message_log import on_auth

            on_auth(session_id, False, auth_ctx.get('reason', 'invalid_token'), trace_id=trace_id)
            await _send_error(ws, 'TOKEN_INVALID', 'Client token is invalid', trace_id=trace_id)
            await ws.close(code=4003)
            log.warning("[%s] Auth failed: %s", session_id, auth_ctx.get('reason', 'invalid_token'))
            return

        if not auth_ctx.get('allowed', True):
            from ..services.message_log import on_auth

            on_auth(session_id, False, auth_ctx.get('reason', 'access_denied'), trace_id=trace_id)
            await _send_error(ws, 'ACCESS_DENIED', 'Client token is not allowed for this node', trace_id=trace_id)
            await ws.close(code=4004)
            log.warning("[%s] Auth denied: %s", session_id, auth_ctx.get('reason', 'access_denied'))
            return

        username = str(auth_ctx.get('username') or '')
        server_id = str(auth_ctx.get('server_id') or state.server_id)
        gate_status = broker_gate.restore_gate(username, server_id)

        from ..services.message_log import on_auth, on_connect

        on_connect(session_id, f"{client_host}:{client_port}", trace_id=trace_id)
        on_auth(session_id, True, trace_id=trace_id)

        _connections[ws] = {
            'session_id': session_id,
            'connected_at': connected_at,
            'auth': True,
            'last_pong': time.time(),
            'username': username,
            'server_id': server_id,
            'token_type': auth_ctx.get('token_type', 'client'),
        }
        state.ws_clients.append(ws)

        ack = {
            'type': 'CONNECT_ACK',
            'id': f'ack_{session_id}',
            'timestamp': int(time.time() * 1000),
            'payload': {
                'status': 'SUCCESS',
                'session_id': session_id,
                'node_info': {
                    'server_id': state.server_id,
                    'node_name': state.node_name,
                    'region': state.region,
                    'status': state.status,
                },
                'broker_gate': gate_status,
                'heartbeat_interval': 30,
                'trace_id': trace_id,
            },
        }
        await _send_json_locked(ws, ack)
        log.info("[%s] Auth OK, connection established", session_id)

        while True:
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=_WS_HEARTBEAT_TIMEOUT)
            except asyncio.TimeoutError:
                log.warning("[%s] Heartbeat timeout (%ss)", session_id, _WS_HEARTBEAT_TIMEOUT)
                break

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await _send_error(ws, 'INVALID_FORMAT', 'Malformed JSON payload')
                continue

            msg_type = msg.get('type', '')
            if msg_type == 'PING':
                if ws in _connections:
                    _connections[ws]['last_pong'] = time.time()
                await _send_json_locked(
                    ws,
                    {
                        'type': 'PONG',
                        'id': msg.get('id', ''),
                        'timestamp': int(time.time() * 1000),
                        'payload': {},
                    },
                )
                continue

            payload = msg.get('payload', {}) if isinstance(msg.get('payload', {}), dict) else {}
            msg_trace_id = str(payload.get('trace_id') or f"trc_{uuid.uuid4().hex[:16]}")
            conn_snapshot = dict(_connections.get(ws, {}))
            asyncio.create_task(_route_and_send(ws, msg_type, msg, session_id, msg_trace_id, conn_snapshot))

    except WebSocketDisconnect:
        log.info("[%s] WS disconnected by client", session_id)
    except Exception as exc:
        log.error("[%s] WS error: %s", session_id, exc, exc_info=True)
    finally:
        conn = _pop_connection(ws)
        log.info("[%s] Connection cleaned up (remaining connections: %s)", session_id, len(_connections))
        _cleanup_connection_artifacts(conn)


async def _send_json_locked(ws: WebSocket, payload: dict[str, Any]) -> bool:
    lock = _send_locks.get(ws)
    try:
        if lock:
            async with lock:
                await ws.send_json(payload)
        else:
            await ws.send_json(payload)
        return True
    except Exception:
        return False


async def _send_text_locked(ws: WebSocket, text: str) -> bool:
    lock = _send_locks.get(ws)
    try:
        if lock:
            async with lock:
                await ws.send_text(text)
        else:
            await ws.send_text(text)
        return True
    except Exception:
        return False



async def _route_and_send(
    ws: WebSocket,
    msg_type: str,
    msg: dict[str, Any],
    session_id: str,
    trace_id: str,
    conn: dict[str, Any],
) -> None:
    response = await _route_message(msg_type, msg, session_id, trace_id, conn)
    if response:
        await _send_json_locked(ws, response)


async def _validate_client_token(token: str, server_id: str = '') -> dict[str, Any]:
    if not token:
        return {'valid': False, 'reason': 'missing_token'}
    if not state.manager_url or not state.token:
        log.warning('token validation skipped: manager_url/token missing')
        return {'valid': False, 'reason': 'manager_auth_missing'}

    url = f"{state.manager_url.rstrip('/')}/auth/verify-token"
    body = json.dumps({'token': token, 'server_id': (server_id or state.server_id)}).encode('utf-8')
    req = urllib.request.Request(url, data=body, method='POST')
    req.add_header('Content-Type', 'application/json')
    req.add_header('Authorization', f'Bearer {state.token}')

    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode('utf-8'))

        if not data.get('ok'):
            return {'valid': False, 'reason': data.get('reason', 'verify_failed')}
        if not data.get('valid'):
            return {
                'valid': False,
                'allowed': bool(data.get('allowed', False)),
                'reason': data.get('reason', 'invalid_or_expired'),
                'username': data.get('username', ''),
                'server_id': data.get('server_id', state.server_id),
                'token_type': data.get('token_type', 'client'),
            }
        return {
            'valid': True,
            'allowed': bool(data.get('allowed', True)),
            'username': data.get('username', ''),
            'server_id': data.get('server_id', state.server_id),
            'token_type': data.get('token_type', 'client'),
            'reason': data.get('reason', ''),
        }
    except urllib.error.HTTPError as exc:
        log.warning('token verify failed: HTTP %s', exc.code)
        return {'valid': False, 'reason': f'http_{exc.code}'}
    except Exception as exc:
        log.warning('token verify failed: %s', exc)
        return {'valid': False, 'reason': 'verify_failed'}


async def _send_error(ws: WebSocket, code: str, message: str, trace_id: str = ''):
    resp = {
        'type': 'ERROR',
        'id': '',
        'timestamp': int(time.time() * 1000),
        'payload': {'code': code, 'message': message, 'trace_id': trace_id},
    }
    try:
        await _send_json_locked(ws, resp)
    except Exception:
        pass


def _error_response(code: str, message: str, trace_id: str = '') -> dict[str, Any]:
    return {
        'type': 'ERROR',
        'id': '',
        'timestamp': int(time.time() * 1000),
        'payload': {'code': code, 'message': message, 'trace_id': trace_id},
    }


async def _route_message(
    msg_type: str,
    msg: dict[str, Any],
    session_id: str,
    trace_id: str = '',
    conn: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    from ..services.message_log import on_recv, on_send

    on_recv(session_id, msg_type, msg.get('payload'), trace_id=trace_id)
    handler = _MESSAGE_HANDLERS.get(msg_type)
    if handler is None:
        log.warning('Unknown message type: %s from %s', msg_type, session_id)
        err_resp = _error_response('UNKNOWN_TYPE', f'Unknown message type: {msg_type}', trace_id=trace_id)
        on_send(session_id, err_resp['type'], err_resp['payload'], False, trace_id=trace_id)
        return err_resp

    try:
        response = await handler(msg, session_id, trace_id, conn or {})
        if response is not None:
            on_send(session_id, response.get('type', 'UNKNOWN'), response.get('payload'), True, trace_id=trace_id)
        return response
    except Exception as exc:
        log.error('Handler error for %s: %s', msg_type, exc, exc_info=True)
        err_resp = _error_response('INTERNAL_ERROR', str(exc)[:100], trace_id=trace_id)
        on_send(session_id, 'ERROR', err_resp.get('payload'), False, trace_id=trace_id)
        return err_resp


async def _handle_economic_query(msg: dict[str, Any], sid: str, trace_id: str = '', conn: dict[str, Any] | None = None) -> dict[str, Any]:
    from ..services.economic_data import get_all_indicators, get_indicator

    payload = msg.get('payload', {})
    indicator = payload.get('indicator')
    log.info('[%s] ECONOMIC_DATA_QUERY: indicator=%s', sid, indicator or '(all)')
    data = get_indicator(indicator) if indicator else get_all_indicators()
    return {
        'type': 'ECONOMIC_DATA_RESPONSE',
        'id': msg.get('id', ''),
        'timestamp': int(time.time() * 1000),
        'payload': {'data': data or {}, 'status': 'ok', 'trace_id': trace_id},
    }


async def _handle_status_query(msg: dict[str, Any], sid: str, trace_id: str = '', conn: dict[str, Any] | None = None) -> dict[str, Any]:
    gate = broker_gate.get_gate_status((conn or {}).get('username', ''), (conn or {}).get('server_id', state.server_id))
    return {
        'type': 'STATUS_RESPONSE',
        'id': msg.get('id', ''),
        'timestamp': int(time.time() * 1000),
        'payload': {
            'status': 'ok',
            'node_info': {
                'server_id': state.server_id,
                'node_name': state.node_name,
                'region': state.region,
                'registration_status': state.status,
                'heartbeat_ok': state.heartbeat_ok,
                'heartbeat_fail_count': state.heartbeat_fail_count,
                'connections': len(_connections),
            },
            'broker_gate': gate,
            'trace_id': trace_id,
        },
    }


async def _handle_summary_report(msg: dict[str, Any], sid: str, trace_id: str = '', conn: dict[str, Any] | None = None) -> dict[str, Any]:
    from ..services.economic_data import generate_summary_report

    report = generate_summary_report()
    return {
        'type': 'SUMMARY_REPORT',
        'id': msg.get('id', ''),
        'timestamp': int(time.time() * 1000),
        'payload': {'report': report, 'status': 'ok', 'trace_id': trace_id},
    }


async def _handle_order_submit(msg: dict[str, Any], sid: str, trace_id: str = '', conn: dict[str, Any] | None = None) -> dict[str, Any]:
    from ..services.trading_svc import place_order

    payload = msg.get('payload', {})
    result = await place_order(
        params=payload,
        session_id=sid,
        username=(conn or {}).get('username', ''),
        server_id=(conn or {}).get('server_id', state.server_id),
        trace_id=trace_id,
    )
    return {
        'type': 'ORDER_RESPONSE',
        'id': msg.get('id', ''),
        'timestamp': int(time.time() * 1000),
        'payload': result,
    }


async def _handle_order_cancel(msg: dict[str, Any], sid: str, trace_id: str = '', conn: dict[str, Any] | None = None) -> dict[str, Any]:
    from ..services.trading_svc import cancel_order

    payload = msg.get('payload', {})
    result = await cancel_order(
        order_id=payload.get('order_id', ''),
        session_id=sid,
        username=(conn or {}).get('username', ''),
        server_id=(conn or {}).get('server_id', state.server_id),
        trace_id=trace_id,
    )
    return {
        'type': 'ORDER_CANCEL_RESPONSE',
        'id': msg.get('id', ''),
        'timestamp': int(time.time() * 1000),
        'payload': result,
    }


async def _handle_position_query(msg: dict[str, Any], sid: str, trace_id: str = '', conn: dict[str, Any] | None = None) -> dict[str, Any]:
    from ..services.trading_svc import get_positions

    payload = msg.get('payload', {})
    filters = {'symbols': payload['symbols']} if payload.get('symbols') else None
    result = await get_positions(
        filters=filters,
        session_id=sid,
        username=(conn or {}).get('username', ''),
        server_id=(conn or {}).get('server_id', state.server_id),
        trace_id=trace_id,
    )
    return {
        'type': 'POSITION_RESPONSE',
        'id': msg.get('id', ''),
        'timestamp': int(time.time() * 1000),
        'payload': result,
    }


async def _handle_order_query(msg: dict[str, Any], sid: str, trace_id: str = '', conn: dict[str, Any] | None = None) -> dict[str, Any]:
    from ..services.trading_svc import get_orders

    payload = msg.get('payload', {})
    mode = (payload.get('mode') or 'live').lower()
    if mode not in ('live', 'all'):
        mode = 'live'
    result = await get_orders(
        mode=mode,
        session_id=sid,
        username=(conn or {}).get('username', ''),
        server_id=(conn or {}).get('server_id', state.server_id),
        trace_id=trace_id,
    )
    return {
        'type': 'ORDER_LIST_RESPONSE',
        'id': msg.get('id', ''),
        'timestamp': int(time.time() * 1000),
        'payload': result,
    }


async def _handle_quote_subscribe(msg: dict[str, Any], sid: str, trace_id: str = '', conn: dict[str, Any] | None = None) -> dict[str, Any]:
    from ..services.quote_provider import handle_subscribe, handle_unsubscribe

    payload = msg.get('payload', {})
    action = payload.get('action', 'subscribe')
    symbols = payload.get('symbols', [])
    result = await handle_unsubscribe(symbols=symbols, session_id=sid) if action == 'unsubscribe' else await handle_subscribe(symbols=symbols, session_id=sid)
    if isinstance(result, dict) and 'trace_id' not in result:
        result['trace_id'] = trace_id
    return {
        'type': 'QUOTE_ACK',
        'id': msg.get('id', ''),
        'timestamp': int(time.time() * 1000),
        'payload': result,
    }


async def _handle_broker_login(msg: dict[str, Any], sid: str, trace_id: str = '', conn: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = msg.get('payload', {}) if isinstance(msg.get('payload', {}), dict) else {}
    account_username = str(payload.get('account_username') or '').strip()
    account_password = str(payload.get('account_password') or '')
    username = (conn or {}).get('username', '')
    server_id = (conn or {}).get('server_id', state.server_id)

    if not account_username or not account_password:
        status = broker_gate.get_gate_status(username, server_id)
        return {
            'type': 'BROKER_LOGIN_RESPONSE',
            'id': msg.get('id', ''),
            'timestamp': int(time.time() * 1000),
            'payload': {
                'success': False,
                'code': 'BROKER_CREDENTIALS_REQUIRED',
                'message': 'Trade service username and password are required',
                'gate': status,
                'trace_id': trace_id,
            },
        }

    if not verify_trade_service_login(account_username, account_password):
        status = broker_gate.get_gate_status(username, server_id)
        return {
            'type': 'BROKER_LOGIN_RESPONSE',
            'id': msg.get('id', ''),
            'timestamp': int(time.time() * 1000),
            'payload': {
                'success': False,
                'code': 'TRADE_SERVICE_LOGIN_FAILED',
                'message': 'Trade service login failed',
                'gate': status,
                'trace_id': trace_id,
            },
        }

    status = broker_gate.login_gate(
        username=username,
        server_id=server_id,
        account_username=account_username,
        account_password=account_password,
    )
    return {
        'type': 'BROKER_LOGIN_RESPONSE',
        'id': msg.get('id', ''),
        'timestamp': int(time.time() * 1000),
        'payload': {
            'success': True,
            'code': 'TRADE_SERVICE_LOGIN_OK',
            'message': 'Trade service login active',
            'gate': status,
            'trace_id': trace_id,
        },
    }


async def _handle_broker_status_query(msg: dict[str, Any], sid: str, trace_id: str = '', conn: dict[str, Any] | None = None) -> dict[str, Any]:
    from ..services.config_sync import get_broker_status
    status = broker_gate.get_gate_status((conn or {}).get('username', ''), (conn or {}).get('server_id', state.server_id))
    broker_detail = get_broker_status()
    return {
        'type': 'BROKER_STATUS_RESPONSE',
        'id': msg.get('id', ''),
        'timestamp': int(time.time() * 1000),
        'payload': {
            'success': True,
            'code': 'BROKER_STATUS_OK',
            'message': 'ok',
            'gate': status,
            'broker_detail': broker_detail,
            'trace_id': trace_id,
        },
    }


async def _handle_broker_logout(msg: dict[str, Any], sid: str, trace_id: str = '', conn: dict[str, Any] | None = None) -> dict[str, Any]:
    status = broker_gate.logout_gate((conn or {}).get('username', ''), (conn or {}).get('server_id', state.server_id))
    return {
        'type': 'BROKER_LOGOUT_RESPONSE',
        'id': msg.get('id', ''),
        'timestamp': int(time.time() * 1000),
        'payload': {
            'success': True,
            'code': 'BROKER_LOGOUT_OK',
            'message': 'Broker gate cleared',
            'gate': status,
            'trace_id': trace_id,
        },
    }


_MESSAGE_HANDLERS: dict[str, Callable[..., Any]] = {
    'ECONOMIC_DATA_QUERY': _handle_economic_query,
    'STATUS_QUERY': _handle_status_query,
    'SUMMARY_REPORT': _handle_summary_report,
    'ORDER_SUBMIT': _handle_order_submit,
    'ORDER_CANCEL': _handle_order_cancel,
    'POSITION_QUERY': _handle_position_query,
    'ORDER_QUERY': _handle_order_query,
    'QUOTE_SUBSCRIBE': _handle_quote_subscribe,
    'BROKER_LOGIN': _handle_broker_login,
    'BROKER_STATUS_QUERY': _handle_broker_status_query,
    'BROKER_LOGOUT': _handle_broker_logout,
}


async def broadcast_message(message: dict[str, Any] | str):
    text = json.dumps(message, ensure_ascii=False) if isinstance(message, dict) else message
    disconnected: list[WebSocket] = []
    for ws in list(_connections.keys()):
        if not await _send_text_locked(ws, text):
            disconnected.append(ws)

    for ws in disconnected:
        conn = _pop_connection(ws)
        _cleanup_connection_artifacts(conn)


async def broadcast_quote_message(message: dict[str, Any]):
    from ..services.quote_provider import session_has_subscription

    payload = message.get('payload', {}) if isinstance(message.get('payload', {}), dict) else {}
    symbol = str(payload.get('symbol') or '').strip().upper()
    if not symbol:
        return

    text = json.dumps(message, ensure_ascii=False)
    disconnected: list[WebSocket] = []
    for ws, meta in list(_connections.items()):
        sid = meta.get('session_id', '')
        if not session_has_subscription(sid, symbol):
            continue
        if not await _send_text_locked(ws, text):
            disconnected.append(ws)

    for ws in disconnected:
        conn = _pop_connection(ws)
        _cleanup_connection_artifacts(conn)



async def force_disconnect_all_clients(reason: str = 'admin_force_release') -> dict[str, Any]:
    targets = list(_connections.keys())
    total = len(targets)
    if total == 0:
        return {'ok': True, 'kicked': 0, 'message': 'no_active_clients'}

    notice = {
        'type': 'FORCE_DISCONNECT',
        'id': f"fd_{int(time.time() * 1000)}",
        'timestamp': int(time.time() * 1000),
        'payload': {
            'code': 'ADMIN_FORCE_RELEASE',
            'reason': reason,
            'message': 'Connection released by Server Manager',
        },
    }

    kicked = 0
    for ws in targets:
        conn = _connections.get(ws, {})
        if conn.get('username') and conn.get('server_id') and _owner_connection_count(conn.get('username', ''), conn.get('server_id', state.server_id)) == 1:
            broker_gate.start_grace(conn.get('username', ''), conn.get('server_id', state.server_id))
        try:
            await _send_json_locked(ws, notice)
        except Exception:
            pass
        try:
            await ws.close(code=_FORCE_DISCONNECT_CODE, reason=reason[:120])
        except Exception:
            pass
        kicked += 1

    log.warning('[WS] Force disconnected %s/%s client(s), reason=%s', kicked, total, reason)
    return {'ok': True, 'kicked': kicked, 'message': 'force_disconnected'}


def get_connection_count() -> int:
    return len(_connections)
