from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from PySide6.QtCore import QObject, Signal

from Client.constants import (
    DEFAULT_TS_HOST,
    DEFAULT_TS_PORT,
    DEFAULT_TS_WS_URL,
    TS_RECONNECT_ENABLED,
)
from Client.network.ts_websocket import TSWebSocketClient


def _default_ts_target() -> str:
    return DEFAULT_TS_WS_URL or f"{DEFAULT_TS_HOST}:{DEFAULT_TS_PORT}"


class TSConnectionCoordinator(QObject):
    """Owns the Client-to-TS connection lifecycle and node occupation."""

    validation_started = Signal(int)
    connection_failed = Signal(int, str, str, bool)
    status_received = Signal(int, str)
    message_received = Signal(int, object)
    latency_received = Signal(int, int)
    state_changed = Signal(int, str, object)

    def __init__(
        self,
        *,
        http_client: Any,
        session_provider: Callable[[], Any],
        username_provider: Callable[[], str],
        reconnect_allowed_provider: Callable[[], bool],
        background_runner: Callable[[Callable[[], None]], None],
        websocket_factory: Callable[..., Any] = TSWebSocketClient,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._http = http_client
        self._session_provider = session_provider
        self._username_provider = username_provider
        self._reconnect_allowed_provider = reconnect_allowed_provider
        self._background_runner = background_runner
        self._websocket_factory = websocket_factory
        self._lock = threading.RLock()
        self._generation = 0
        self._client: Any | None = None
        self._connected = False
        self._target_address = ""
        self._server_id = ""
        self._connection_id = ""
        self._last_endpoint = ""

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def client(self) -> Any | None:
        with self._lock:
            return self._client

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    @connected.setter
    def connected(self, value: bool) -> None:
        with self._lock:
            self._connected = bool(value)

    @property
    def target_address(self) -> str:
        with self._lock:
            return self._target_address

    @target_address.setter
    def target_address(self, value: str) -> None:
        with self._lock:
            self._target_address = str(value or "").strip()

    @property
    def server_id(self) -> str:
        with self._lock:
            return self._server_id

    @property
    def connection_id(self) -> str:
        with self._lock:
            return self._connection_id

    @property
    def last_endpoint(self) -> str:
        with self._lock:
            return self._last_endpoint

    def client_is_active(self) -> bool:
        client = self.client
        return bool(client and getattr(client, "is_active", False))

    def _begin_attempt(self) -> int:
        with self._lock:
            self._generation += 1
            previous_client = self._client
            self._client = None
            self._connection_id = ""
            self._connected = False
            generation = self._generation
        if previous_client:
            try:
                previous_client.stop(wait=False)
            except TypeError:
                previous_client.stop()
            except Exception:
                pass
        return generation

    def _is_current(self, generation: int) -> bool:
        with self._lock:
            return generation == self._generation

    def validate_and_connect(self, target_address: str) -> None:
        generation = self._begin_attempt()
        target = str(target_address or self.target_address or _default_ts_target()).strip()
        self.validation_started.emit(generation)
        try:
            status_code, response = self._http.get(
                f"/api/accounts/se-status?address={quote(target, safe='')}"
            )
            response = response or {}
            if not self._is_current(generation):
                return
            if status_code != 200 or not response.get("ok"):
                self.connection_failed.emit(generation, "交易服务器校验失败", "", True)
                return
            if not response.get("online"):
                self.connection_failed.emit(
                    generation,
                    "交易服务器当前离线",
                    "所分配的交易服务器目前离线，请联系管理员。",
                    True,
                )
                return
            occupied_by = str(response.get("occupied_by") or "").strip()
            if occupied_by and occupied_by != self._username_provider():
                self.connection_failed.emit(
                    generation,
                    "交易服务器已被占用",
                    f"当前交易服务器已被账户“{occupied_by}”占用，无法连接。",
                    True,
                )
                return
            server_id = str(response.get("server_id") or "").strip()
            if not server_id:
                self.connection_failed.emit(
                    generation,
                    "交易服务器校验失败",
                    "未返回服务器标识。",
                    True,
                )
                return
            with self._lock:
                if generation != self._generation:
                    return
                self._server_id = server_id
                self._target_address = target
            self.connect_validated(target, generation=generation)
        except Exception as exc:
            if self._is_current(generation):
                self.connection_failed.emit(
                    generation,
                    "交易服务器校验失败",
                    str(exc),
                    True,
                )

    def connect_validated(self, target_address: str, *, generation: int | None = None) -> None:
        if generation is None:
            generation = self._begin_attempt()
        if not self._is_current(generation):
            return
        target = str(target_address or self.target_address or _default_ts_target()).strip()
        endpoint = self._websocket_factory.normalize_endpoint(target, default_port=DEFAULT_TS_PORT)
        with self._lock:
            if generation != self._generation:
                return
            server_id = self._server_id
            self._last_endpoint = endpoint
        client = self._websocket_factory(
            ws_url=endpoint,
            port=DEFAULT_TS_PORT,
            token=self._http.token,
            server_id=server_id,
            on_message_callback=self._message_handler(generation),
            on_status_callback=self._status_handler(generation),
            on_latency_callback=self._latency_handler(generation),
            on_reconnect_prepare_callback=(
                lambda attempt, connection_id, gen=generation: self.prepare_reconnect(
                    gen, attempt, connection_id
                )
            ),
            on_state_callback=self._state_handler(generation),
            reconnect_enabled=TS_RECONNECT_ENABLED,
        )
        with self._lock:
            if generation != self._generation:
                return
            self._client = client
            self._connection_id = str(client.connection_id or "")
        occupied = self.occupy(
            connection_id=client.connection_id,
            sync=True,
            expected_generation=generation,
        )
        if not occupied:
            if not self._is_current(generation):
                self._release_identity(server_id, str(client.connection_id or ""))
                self._stop_client(client, wait=False)
                return
            with self._lock:
                if generation == self._generation and self._client is client:
                    self._client = None
                    self._connection_id = ""
            self.connection_failed.emit(
                generation,
                "交易服务器锁定失败",
                "节点占用注册未成功，无法确保独占权。",
                False,
            )
            return
        if not self._is_current(generation):
            self._release_identity(server_id, str(client.connection_id or ""))
            self._stop_client(client, wait=False)
            return
        client.start()

    def _status_handler(self, generation: int) -> Callable[[str], None]:
        def handler(message: str) -> None:
            if self._is_current(generation):
                self.status_received.emit(generation, str(message or ""))

        return handler

    def _message_handler(self, generation: int) -> Callable[[dict], None]:
        def handler(message: dict) -> None:
            if self._is_current(generation):
                self.message_received.emit(generation, dict(message or {}))

        return handler

    def _latency_handler(self, generation: int) -> Callable[[int], None]:
        def handler(latency_ms: int) -> None:
            if self._is_current(generation) and self.connected:
                self.latency_received.emit(generation, int(latency_ms))

        return handler

    def _state_handler(self, generation: int) -> Callable[[str, dict], None]:
        def handler(state: str, detail: dict) -> None:
            if not self._is_current(generation):
                return
            normalized = str(state or "")
            if normalized == "authenticated":
                self.connected = True
            elif normalized in {
                "reconnecting",
                "auth_failed",
                "retry_exhausted",
                "force_disconnected",
            }:
                self.connected = False
            self.state_changed.emit(generation, normalized, dict(detail or {}))

        return handler

    def prepare_reconnect(self, generation: int, attempt: int, connection_id: str) -> bool:
        del attempt
        if not self._is_current(generation) or not self._reconnect_allowed_provider():
            return False
        session = self._session_provider()
        if not session or not session.connected:
            return False
        with self._lock:
            target = self._target_address or self._last_endpoint or _default_ts_target()
            current_server_id = self._server_id
        try:
            status_code, response = self._http.get(
                f"/api/accounts/se-status?address={quote(target, safe='')}"
            )
        except Exception:
            return False
        response = response or {}
        if status_code != 200 or not response.get("ok") or not response.get("online"):
            return False
        occupied_by = str(response.get("occupied_by") or "").strip()
        if occupied_by and occupied_by != self._username_provider():
            return False
        server_id = str(response.get("server_id") or "").strip()
        if current_server_id and server_id and server_id != current_server_id:
            return False
        with self._lock:
            if generation != self._generation:
                return False
            if server_id:
                self._server_id = server_id
                self._target_address = target
            if not self._server_id:
                return False
        return self.occupy(
            connection_id=connection_id,
            sync=True,
            expected_generation=generation,
        )

    def occupy(
        self,
        connection_id: str = "",
        *,
        max_retries: int = 3,
        sync: bool = True,
        expected_generation: int | None = None,
    ) -> bool:
        with self._lock:
            generation = self._generation if expected_generation is None else expected_generation
            server_id = self._server_id
            requested_connection_id = str(connection_id or self._connection_id or "").strip()
        if not server_id or not requested_connection_id or not self._is_current(generation):
            return False
        username = self._username_provider()

        def do_with_retry() -> bool:
            for attempt in range(1, max_retries + 1):
                if not self._is_current(generation):
                    return False
                try:
                    code, response = self._http.post(f"/api/nodes/{server_id}/occupy", {
                        "username": username,
                        "connection_id": requested_connection_id,
                    })
                    if code == 200 and (response or {}).get("ok"):
                        with self._lock:
                            if generation != self._generation or server_id != self._server_id:
                                return False
                            self._connection_id = requested_connection_id
                        return True
                    error = str(
                        (response or {}).get("error")
                        or (response or {}).get("message")
                        or f"HTTP {code}"
                    )
                    lowered = error.lower()
                    if (
                        "occupied" in lowered
                        or "not found" in lowered
                        or "unauthorized" in lowered
                        or code in (401, 403)
                    ):
                        return False
                except Exception:
                    pass
                if attempt < max_retries:
                    time.sleep(min(1.0 * (2 ** (attempt - 1)), 5))
            return False

        if sync:
            return do_with_retry()
        self._background_runner(do_with_retry)
        return False

    def release(self, *, sync: bool = False, clear_server_id: bool = True) -> bool:
        with self._lock:
            generation = self._generation
            server_id = self._server_id
            connection_id = self._connection_id
        if not server_id:
            return True
        if not connection_id:
            with self._lock:
                if clear_server_id and generation == self._generation:
                    self._server_id = ""
            return True

        def do_release() -> bool:
            try:
                code, _response = self._http.post(f"/api/nodes/{server_id}/release", {
                    "connection_id": connection_id,
                })
                if code == 200:
                    with self._lock:
                        if (
                            clear_server_id
                            and generation == self._generation
                            and connection_id == self._connection_id
                        ):
                            self._server_id = ""
                            self._connection_id = ""
                    return True
            except Exception:
                pass
            return False

        if sync:
            return do_release()
        self._background_runner(do_release)
        return False

    def _release_identity(self, server_id: str, connection_id: str) -> bool:
        if not server_id or not connection_id:
            return False
        try:
            code, _response = self._http.post(f"/api/nodes/{server_id}/release", {
                "connection_id": connection_id,
            })
            return code == 200
        except Exception:
            return False

    @staticmethod
    def _stop_client(client: Any, *, wait: bool) -> None:
        try:
            client.stop(wait=wait)
        except TypeError:
            client.stop()
        except Exception:
            pass

    def disconnect(self, *, wait: bool = False) -> None:
        with self._lock:
            self._generation += 1
            client = self._client
            self._client = None
            self._connected = False
            self._connection_id = ""
        if client:
            self._stop_client(client, wait=wait)

    def abort(self, *, release: bool, wait: bool = False) -> None:
        with self._lock:
            self._generation += 1
            client = self._client
            self._client = None
            self._connected = False
        if release:
            self.release(sync=wait)
        else:
            with self._lock:
                self._connection_id = ""
        if client:
            self._stop_client(client, wait=wait)

    def shutdown(self, *, release: bool, wait: bool = True) -> None:
        if release:
            self.release(sync=wait)
        self.disconnect(wait=wait)

    def reset(self) -> None:
        self.disconnect(wait=False)
        with self._lock:
            self._target_address = ""
            self._server_id = ""
            self._connection_id = ""
            self._last_endpoint = ""
