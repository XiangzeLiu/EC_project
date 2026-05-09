"""
SE WebSocket Client
连接 Server_economic (SE) 的 WebSocket 端点

协议:
  1. 连接后发送 CONNECT + Bearer Token 认证
  2. 收到 CONNECT_ACK 后进入就绪状态
  3. 定时发送 PING 保持心跳
  4. 可发送 STATUS_QUERY 等查询请求

支持自动重连：连接断开后按指数退避策略自动重试，直到重连成功或被显式 stop()
"""

import asyncio
import json
import threading
import time
from typing import Callable

import websockets

# 导入重连配置常量（从 constants 模块）
try:
    from ..constants import (SE_RECONNECT_BASE_INTERVAL, SE_RECONNECT_MAX_INTERVAL,
                            SE_RECONNECT_MAX_ATTEMPTS)
except ImportError:
    # 兼容单独运行时的 fallback
    SE_RECONNECT_BASE_INTERVAL = 3
    SE_RECONNECT_MAX_INTERVAL = 30
    SE_RECONNECT_MAX_ATTEMPTS = 0


class SEWebSocketClient:
    """Server_economic WebSocket 客户端（支持自动重连）"""

    def __init__(self, host: str = "127.0.0.1", port: int = 8900,
                 token: str = "",
                 on_message_callback: Callable[[dict], None] = None,
                 on_status_callback: Callable[[str], None] = None,
                 reconnect_enabled: bool = False):
        self.host = host
        self.port = port
        self.token = token
        self.on_message = on_message_callback
        self.on_status = on_status_callback
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
        if self._loop and self._loop.is_running():
            try:
                self._loop.call_soon_threadsafe(self._do_stop)
            except Exception:
                pass

    def _do_stop(self):
        """在 event loop 内执行停止"""
        if self._ws:
            asyncio.create_task(self._ws.close())

    def send_query_status(self) -> str:
        """发送节点状态查询请求"""
        req_id = f"req_{int(time.time() * 1000)}"
        msg = {
            "type": "STATUS_QUERY",
            "id": req_id,
            "timestamp": int(time.time() * 1000),
            "payload": {},
        }
        with self._req_lock:
            self._pending_requests.append(msg)
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
        req_id = f"req_{int(time.time() * 1000)}"
        msg = {
            "type": msg_type,
            "id": req_id,
            "timestamp": int(time.time() * 1000),
            "payload": payload or {},
        }
        with self._req_lock:
            self._pending_requests.append(msg)
        return req_id

    def _run_loop(self):
        """独立线程运行 asyncio 事件循环（支持自动重连）"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        reconnect_attempts = 0
        last_error = ""

        # ── 重连循环：当启用重连且未被显式 stop 时持续尝试 ──
        while True:
            try:
                # 每次连接尝试前确保 event loop 可用
                if self._loop.is_closed():
                    self._loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(self._loop)

                self._loop.run_until_complete(self._connect_and_run())
                # 正常退出（async with 结束或认证失败返回）→ 检查是否继续重连
                if not self._reconnect_enabled or not self._active:
                    break

            except (ConnectionRefusedError, ConnectionResetError, OSError,
                    asyncio.TimeoutError, websockets.exceptions.ConnectionClosed,
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
                SE_RECONNECT_BASE_INTERVAL * (2 ** min(reconnect_attempts - 1, 3)),
                SE_RECONNECT_MAX_INTERVAL
            )
            if SE_RECONNECT_MAX_ATTEMPTS > 0 and reconnect_attempts >= SE_RECONNECT_MAX_ATTEMPTS:
                if self.on_status:
                    self.on_status(f"Reconnect failed after {reconnect_attempts} attempts: {last_error}")
                break

            # sleep 在 loop 外部进行，不阻塞 event loop
            time.sleep(delay)

        # 完全退出循环后的最终清理
        self._active = False
        self._connected = False
        if self.on_status and last_error and not self._connected:
            self.on_status(f"Disconnected: {last_error}")
        try:
            if not self._loop.is_closed():
                self._loop.close()
        except Exception:
            pass

    async def _connect_and_run(self):
        """建立连接并运行主循环"""
        uri = f"ws://{self.host}:{self.port}/ws"

        if self.on_status:
            self.on_status(f"Connecting to {uri}...")

        async with websockets.connect(uri) as ws:
            self._ws = ws

            if self.on_status:
                self.on_status("Connected, sending auth...")

            # 阶段1: 发送 CONNECT 认证
            connect_msg = {
                "type": "CONNECT",
                "id": f"conn_{int(time.time() * 1000)}",
                "timestamp": int(time.time() * 1000),
                "payload": {"token": self.token},
            }
            await ws.send(json.dumps(connect_msg))

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
            await asyncio.gather(
                self._receive_loop(ws),
                self._heartbeat_loop(ws),
                self._send_pending_loop(ws),
            )

    async def _receive_loop(self, ws):
        """接收服务端消息并回调"""
        while self._active:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type", "")

            if msg_type == "ERROR":
                if self.on_status:
                    err_payload = msg.get("payload", {})
                    self.on_status(f"Error [{err_payload.get('code', '')}]: {err_payload.get('message', '')}")

            if self.on_message:
                self.on_message(msg)

    async def _heartbeat_loop(self, ws):
        """每30秒发送 PING"""
        while self._active:
            await asyncio.sleep(30)
            if not self._active or not ws.open:
                break
            ping = {
                "type": "PING",
                "id": f"ping_{int(time.time() * 1000)}",
                "timestamp": int(time.time() * 1000),
                "payload": {},
            }
            try:
                await ws.send(json.dumps(ping))
            except Exception:
                break

    async def _send_pending_loop(self, ws):
        """检查并发送待处理请求（每50ms检查一次）"""
        while self._active:
            await asyncio.sleep(0.05)
            with self._req_lock:
                if not self._pending_requests:
                    continue
                requests = self._pending_requests[:]
                self._pending_requests.clear()

            for req in requests:
                try:
                    await ws.send(json.dumps(req))
                except Exception:
                    with self._req_lock:
                        self._pending_requests.insert(0, req)
                    break


# 向后兼容别名
EconomicDataClient = SEWebSocketClient
