"""
TS WebSocket Client
连接 Trader_Server (TS) 的 WebSocket 端点

协议:
  1. 连接后发送 CONNECT + Bearer Token 认证
  2. 收到 CONNECT_ACK 后进入就绪状态
  3. 定时发送 PING 保持心跳
  4. 可发送 STATUS_QUERY 等查询请求

支持自动重连：连接断开后按指数退避策略自动重试，直到重连成功或被显式 stop()
"""

import asyncio
import ipaddress
import json
import logging
import queue
import threading
import time
import uuid
from typing import Callable
from urllib.parse import urlsplit, urlunsplit



import websockets

log = logging.getLogger("client.ts_websocket")

# 导入重连配置常量（从 constants 模块）
try:
    from ..constants import (TS_RECONNECT_BASE_INTERVAL, TS_RECONNECT_MAX_INTERVAL,
                            TS_RECONNECT_MAX_ATTEMPTS)
except ImportError:
    # 兼容单独运行时的 fallback
    TS_RECONNECT_BASE_INTERVAL = 3
    TS_RECONNECT_MAX_INTERVAL = 30
    TS_RECONNECT_MAX_ATTEMPTS = 10


class TSWebSocketClient:
    """Trader_Server WebSocket 客户端（支持自动重连）"""

    def __init__(self, host: str = "127.0.0.1", port: int = 8900,
                 token: str = "", server_id: str = "",
                 on_message_callback: Callable[[dict], None] = None,
                 on_status_callback: Callable[[str], None] = None,
                 on_latency_callback: Callable[[int], None] = None,
                 on_reconnect_prepare_callback: Callable[[int], bool] | None = None,
                 reconnect_enabled: bool = False,
                 ws_url: str = ""):

        self.host = host
        self.port = port
        self.ws_url = self.normalize_endpoint(ws_url or host, default_port=port)
        self.token = token
        self.server_id = server_id
        self.on_message = on_message_callback

        self.on_status = on_status_callback
        self.on_latency = on_latency_callback
        self.on_reconnect_prepare = on_reconnect_prepare_callback
        self._active = False
        self._connected = False
        self._reconnect_enabled = reconnect_enabled   # 是否启用自动重连
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws = None
        self._session_id: str = ""
        self._node_info: dict = {}
        # 待发送的请求队列 (由外部线程安全地添加请求)
        self._pending_requests: list[dict] = []
        self._req_lock = threading.Lock()
        # 同步请求等待队列：{req_id: Queue(maxsize=1)}
        self._response_waiters: dict[str, queue.Queue] = {}
        self._resp_lock = threading.Lock()
        # ★ 连接丢失事件：任一子协程检测到断连时 set，其他协程立即响应
        self._conn_lost: asyncio.Event | None = None
        self._send_wakeup: asyncio.Event | None = None
        self._send_lock: asyncio.Lock | None = None
        self._ping_sent_at: dict[str, float] = {}

        try:
            parsed = urlsplit(self.ws_url)
            if parsed.hostname:
                self.host = parsed.hostname
            if parsed.port:
                self.port = parsed.port
        except Exception:
            pass

    @staticmethod
    def normalize_endpoint(endpoint: str, default_port: int = 8900) -> str:
        """Return a usable ws/wss URL from a full URL, host:port, or bare host."""
        raw = (endpoint or "").strip() or "127.0.0.1"

        if raw.startswith(("ws://", "wss://", "http://", "https://")):
            parsed = urlsplit(raw)
            scheme = parsed.scheme
            if scheme == "http":
                scheme = "ws"
            elif scheme == "https":
                scheme = "wss"
            path = parsed.path if parsed.path and parsed.path != "/" else "/ws"
            return urlunsplit((scheme, parsed.netloc, path, parsed.query, parsed.fragment))

        authority, sep, path_part = raw.partition("/")
        path = f"/{path_part}" if sep and path_part else "/ws"
        if authority.lower().endswith(":443"):
            return f"wss://{authority}{path}"
        if ":" not in authority:
            try:
                ipaddress.ip_address(authority)
                is_ip = True
            except ValueError:
                is_ip = False
            if not is_ip and "." in authority and authority.lower() != "localhost":
                return f"wss://{authority}{path}"
            if default_port:
                authority = f"{authority}:{default_port}"
        return f"ws://{authority}{path}"

    @property
    def endpoint(self) -> str:
        return self.ws_url

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def node_info(self) -> dict:
        return self._node_info.copy()

    @staticmethod
    def _is_ws_open(ws) -> bool:
        """
        兼容新旧版本 websockets 的连接状态检查
        
        - websockets < 10.0: 使用 ws.open
        - websockets >= 10.0: 使用 ws.state == State.OPEN
        """
        if hasattr(ws, 'open'):
            return ws.open
        # websockets >= 10.0
        try:
            from websockets.protocol import State
            return ws.state == State.OPEN
        except Exception:
            # 兜底：假设连接正常
            return True

    def start(self):
        """启动 WebSocket 连接线程"""
        if self._active:
            return
        self._active = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """停止连接"""
        self._active = False
        self._connected = False
        if self._loop and self._loop.is_running():
            try:
                self._loop.call_soon_threadsafe(self._do_stop)
            except Exception:
                pass

    def _do_stop(self):
        """在 event loop 内执行停止"""
        if self._ws:
            # 使用 run_coroutine_threadsafe 避免未等待的协程警告
            asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)

    def _enqueue_message(self, msg: dict):
        with self._req_lock:
            self._pending_requests.append(msg)
        self._wake_sender()

    def _wake_sender(self):
        loop = self._loop
        wakeup = self._send_wakeup
        if loop and wakeup and loop.is_running():
            try:
                loop.call_soon_threadsafe(wakeup.set)
            except Exception:
                pass

    def send_query_status(self) -> str:
        """发送节点状态查询请求"""
        req_id = f"req_{int(time.time() * 1000)}"
        msg = {
            "type": "STATUS_QUERY",
            "id": req_id,
            "timestamp": int(time.time() * 1000),
            "payload": {},
        }
        self._enqueue_message(msg)
        return req_id

    def send_raw_message(self, msg_type: str, payload: dict | None = None) -> str:
        """
        发送任意类型消息
        Args:
            msg_type: 消息类型标识
            payload: 消息载荷
        Returns:
            请求 ID
        """
        req_id = f"req_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
        p = dict(payload or {})
        p.setdefault("trace_id", f"trc_{uuid.uuid4().hex[:16]}")
        msg = {
            "type": msg_type,
            "id": req_id,
            "timestamp": int(time.time() * 1000),
            "payload": p,
        }

        self._enqueue_message(msg)
        return req_id

    def request_sync(self, msg_type: str, payload: dict | None = None, timeout: float = 10.0) -> dict | None:
        """
        发送请求并同步等待同 ID 响应。

        Returns:
            响应消息 dict；超时或未连接时返回 None。
        """
        if not self._connected:
            return None

        req_id = f"req_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
        p = dict(payload or {})
        p.setdefault("trace_id", f"trc_{uuid.uuid4().hex[:16]}")
        msg = {
            "type": msg_type,
            "id": req_id,
            "timestamp": int(time.time() * 1000),
            "payload": p,
        }


        q: queue.Queue = queue.Queue(maxsize=1)
        with self._resp_lock:
            self._response_waiters[req_id] = q

        self._enqueue_message(msg)

        try:
            return q.get(timeout=timeout)
        except queue.Empty:
            return None
        finally:
            with self._resp_lock:
                self._response_waiters.pop(req_id, None)

    # ── 交易操作便捷方法 ──────────────────────────────────────


    def send_order_submit(self, symbol: str, qty: int, price: float,
                          action: str = "Buy to Open",
                          order_type: str = "limit",
                          tif: str = "Day") -> str:
        """发送下单请求"""
        return self.send_raw_message("ORDER_SUBMIT", {
            "symbol": symbol,
            "qty": qty,
            "price": price,
            "action": action,
            "order_type": order_type,
            "tif": tif,
        })

    def send_order_cancel(self, order_id: str) -> str:
        """发送撤单请求"""
        return self.send_raw_message("ORDER_CANCEL", {
            "order_id": order_id,
        })

    def send_position_query(self, symbols: list[str] | None = None) -> str:
        """发送持仓查询请求"""
        payload = {}
        if symbols:
            payload["symbols"] = symbols
        return self.send_raw_message("POSITION_QUERY", payload)

    # ── 行情操作便捷方法 ──────────────────────────────────────

    def send_quote_subscribe(self, symbols: list[str]) -> str:
        """订阅行情数据"""
        return self.send_raw_message("QUOTE_SUBSCRIBE", {
            "action": "subscribe",
            "symbols": symbols,
        })

    def send_quote_unsubscribe(self, symbols: list[str]) -> str:
        """取消订阅行情"""
        return self.send_raw_message("QUOTE_SUBSCRIBE", {
            "action": "unsubscribe",
            "symbols": symbols,
        })

    def _run_loop(self):
        """独立线程运行 asyncio 事件循环（支持自动重连）"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        reconnect_attempts = 0
        last_error = ""

        # ── 重连循环：当启用重连且未被显式 stop 时持续尝试 ──
        while True:
            try:
                if reconnect_attempts > 0 and self.on_reconnect_prepare:
                    try:
                        if not self.on_reconnect_prepare(reconnect_attempts):
                            raise ConnectionRefusedError("reconnect_prepare_failed")
                    except Exception as prep_exc:
                        last_error = f"reconnect_prepare_failed: {prep_exc}"
                        raise ConnectionRefusedError(last_error)

                # 每次连接尝试前确保 event loop 可用
                if self._loop.is_closed():
                    self._loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(self._loop)

                connected_this_run = bool(self._loop.run_until_complete(self._connect_and_run()))
                # 正常退出（async with 结束或认证失败返回）→ 检查是否继续重连
                if not self._reconnect_enabled or not self._active:
                    break
                if connected_this_run:
                    reconnect_attempts = 0
                last_error = "connection_lost"
                reconnect_attempts += 1
                if self.on_status:
                    self.on_status(f"Reconnecting ({reconnect_attempts})... | connection_lost")

            except websockets.exceptions.ConnectionClosed as e:
                # 管理端强制断开：不进行自动重连
                if getattr(e, "code", None) == 4008:
                    last_error = "Force disconnected by admin"
                    self._reconnect_enabled = False
                    self._active = False
                    if self.on_status:
                        self.on_status(last_error)
                    break

                last_error = str(e)
                if not self._reconnect_enabled or not self._active:
                    break
                reconnect_attempts += 1
                if self.on_status:
                    self.on_status(f"Reconnecting ({reconnect_attempts})... | {type(e).__name__}")

            except (ConnectionRefusedError, ConnectionResetError, OSError,
                    asyncio.TimeoutError,
                    websockets.exceptions.InvalidHandshake, websockets.exceptions.WebSocketException) as e:
                # 网络层异常 → 可重连
                last_error = str(e)
                if not self._reconnect_enabled or not self._active:
                    break
                reconnect_attempts += 1
                if self.on_status:
                    self.on_status(f"Reconnecting ({reconnect_attempts})... | {type(e).__name__}")


            except Exception as e:
                # 其他异常 → 根据配置决定是否重连
                last_error = str(e)
                if not self._reconnect_enabled or not self._active:
                    break
                reconnect_attempts += 1
                if self.on_status:
                    self.on_status(f"Reconnecting ({reconnect_attempts})... | {e}")

            finally:
                # 本次尝试结束，标记断开状态
                was_connected = self._connected
                self._connected = False
                self._ws = None

            # 指数退避等待
            delay = min(
                TS_RECONNECT_BASE_INTERVAL * (2 ** min(reconnect_attempts - 1, 3)),
                TS_RECONNECT_MAX_INTERVAL
            )
            if TS_RECONNECT_MAX_ATTEMPTS > 0 and reconnect_attempts >= TS_RECONNECT_MAX_ATTEMPTS:
                if self.on_status:
                    self.on_status(f"Reconnect failed after {reconnect_attempts} attempts: {last_error}")
                break


            # sleep 在 loop 外部进行，不阻塞 event loop
            time.sleep(delay)

        # 完全退出循环后的最终清理
        self._active = False
        self._connected = False
        if self.on_status and last_error and not self._connected and last_error != "Force disconnected by admin":
            self.on_status(f"Disconnected: {last_error}")

        try:
            if not self._loop.is_closed():
                self._loop.close()
        except Exception:
            pass

    async def _connect_and_run(self):
        """建立连接并运行主循环"""
        uri = self.ws_url

        if self.on_status:
            self.on_status(f"Connecting to {uri}...")

        # ★ 初始化连接丢失事件（每次新连接重置）
        self._conn_lost = asyncio.Event()
        self._send_wakeup = asyncio.Event()
        self._send_lock = asyncio.Lock()

        async with websockets.connect(uri) as ws:
            self._ws = ws

            if self.on_status:
                self.on_status("Connected, sending auth...")

            # 阶段1: 发送 CONNECT 认证
            connect_msg = {
                "type": "CONNECT",
                "id": f"conn_{int(time.time() * 1000)}",
                "timestamp": int(time.time() * 1000),
                "payload": {
                    "token": self.token,
                    "server_id": self.server_id,
                    "trace_id": f"trc_{uuid.uuid4().hex[:16]}"
                },
            }


            await self._send_ws_json(ws, connect_msg)

            # 阶段2: 等待 CONNECT_ACK
            ack_raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
            ack = json.loads(ack_raw)

            if ack.get("type") != "CONNECT_ACK":
                err_msg = ack.get("payload", {}).get("message", "Auth failed")
                if self.on_status:
                    self.on_status(f"Auth failed: {err_msg}")
                return

            self._connected = True
            payload = ack.get("payload", {})
            self._session_id = payload.get("session_id", "")
            self._node_info = payload.get("node_info", {})

            if self.on_status:
                self.on_status(f"Authenticated! Session: {self._session_id}")
            if self.on_message:
                self.on_message({"event": "connected", "data": ack})

            # 阶段3: 主循环 — 接收消息 + 心跳 + 处理待发请求
            # ★ return_exceptions=True: 任意子协程异常退出不会杀死其他协程
            #    例如 _receive_loop 因 WS 断开而退出时，_heartbeat_loop 和
            #    _send_pending_loop 可以继续运行直到 ws 被关闭，避免竞态
            self._send_wakeup.set()
            await self._send_latency_ping(ws)
            results = await asyncio.gather(
                self._receive_loop(ws),
                self._heartbeat_loop(ws),
                self._send_pending_loop(ws),
                return_exceptions=True,
            )
            # 记录异常日志（仅用于调试，不影响连接生命周期）
            for i, r in enumerate(results):
                if isinstance(r, Exception):
                    names = ["receive", "heartbeat", "send_pending"]
                    log.debug(f"[TS] {names[i]} exited with: {r}")
            return True

    async def _send_ws_json(self, ws, msg: dict):
        data = json.dumps(msg)
        if self._send_lock is None:
            await ws.send(data)
            return
        async with self._send_lock:
            await ws.send(data)

    async def _receive_loop(self, ws):
        """接收服务端消息并回调（任何异常仅退出本协程，同时通知其他协程）"""
        while self._active:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except (websockets.exceptions.ConnectionClosed,
                    websockets.exceptions.WebSocketException) as e:
                # ★ WS 断开 → 广播连接丢失事件，让 heartbeat/send 协程立即退出
                log.debug(f"[TS] receive_loop: WS closed: {e}")
                self._conn_lost.set()
                try:
                    await ws.close()
                except Exception:
                    pass
                return

            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue

            msg_type = msg.get("type", "")
            if msg_type == "PONG":
                ping_id = msg.get("id", "")
                sent_at = self._ping_sent_at.pop(ping_id, None)
                if sent_at is not None and self.on_latency:
                    latency_ms = max(0, int((time.perf_counter() - sent_at) * 1000))
                    try:
                        self.on_latency(latency_ms)
                    except Exception:
                        pass


            # 优先唤醒同步等待者（按请求 id 关联）
            req_id = msg.get("id", "")
            if req_id:
                with self._resp_lock:
                    waiter = self._response_waiters.get(req_id)
                if waiter:
                    try:
                        waiter.put_nowait(msg)
                    except Exception:
                        pass

            if msg_type == "ERROR":

                if self.on_status:
                    err_payload = msg.get("payload", {})
                    self.on_status(f"Error [{err_payload.get('code', '')}]: {err_payload.get('message', '')}")

            if self.on_message:
                try:
                    self.on_message(msg)
                except Exception:
                    pass  # 回调异常不应影响接收循环

    async def _send_latency_ping(self, ws) -> bool:
        ping_id = f"ping_{int(time.time() * 1000)}"
        ping = {
            "type": "PING",
            "id": ping_id,
            "timestamp": int(time.time() * 1000),
            "payload": {},
        }
        try:
            self._ping_sent_at[ping_id] = time.perf_counter()
            await self._send_ws_json(ws, ping)
            # 避免异常场景下累计过多旧 PING 记录。
            if len(self._ping_sent_at) > 8:
                for old_id in list(self._ping_sent_at)[:-8]:
                    self._ping_sent_at.pop(old_id, None)
            return True
        except Exception:
            if self._conn_lost is not None:
                self._conn_lost.set()
            return False


    async def _heartbeat_loop(self, ws):
        """每15秒发送 PING（同时监听连接丢失事件以快速响应断连）"""
        while self._active:
            if self._conn_lost is None:
                break
            try:
                # ★ 用 asyncio.wait 同时监听 sleep 和 conn_lost 事件
                #    这样 _receive_loop 检测到断连后，心跳协程最多 ~0.1s 内退出（而非等满 30 秒）
                # 创建任务以便可以取消
                sleep_task = asyncio.create_task(asyncio.sleep(15))
                event_task = asyncio.create_task(self._conn_lost.wait())
                done, pending = await asyncio.wait(
                    [sleep_task, event_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                # 取消未完成的任务
                for task in pending:
                    task.cancel()
                # 等待取消的任务完成（避免警告）
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
            except (asyncio.CancelledError, RuntimeError):
                break

            # 连接已丢失 → 立即退出（不尝试发送 PING）
            if self._conn_lost.is_set() or not self._active:
                break
            if not self._is_ws_open(ws):
                self._conn_lost.set()
                break

            if not await self._send_latency_ping(ws):
                break

    async def _send_pending_loop(self, ws):
        """Send queued requests as soon as they are enqueued."""
        while self._active:
            if self._conn_lost is None or self._send_wakeup is None:
                break
            try:
                send_task = asyncio.create_task(self._send_wakeup.wait())
                event_task = asyncio.create_task(self._conn_lost.wait())
                done, pending = await asyncio.wait(
                    [send_task, event_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
            except (asyncio.CancelledError, RuntimeError):
                break

            if self._send_wakeup:
                self._send_wakeup.clear()

            is_open = self._is_ws_open(ws)
            if self._conn_lost.is_set() or not self._active or not is_open:
                if self._conn_lost.is_set() and is_open:
                    try:
                        await ws.close()
                    except Exception:
                        pass
                break

            with self._req_lock:
                if not self._pending_requests:
                    continue
                requests = self._pending_requests[:]
                self._pending_requests.clear()

            for req in requests:
                try:
                    await self._send_ws_json(ws, req)
                except Exception:
                    with self._req_lock:
                        self._pending_requests.insert(0, req)
                    self._conn_lost.set()
                    break



# 向后兼容别名
EconomicDataClient = TSWebSocketClient
