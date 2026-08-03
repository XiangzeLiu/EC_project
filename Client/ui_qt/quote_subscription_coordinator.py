from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, Signal


class QuoteSubscriptionCoordinator(QObject):
    """Serializes quote subscription changes and reconciles them to UI intent."""

    symbol_result = Signal(object)
    sync_failed = Signal(str)
    subscriptions_changed = Signal(object)

    def __init__(
        self,
        *,
        session_provider: Callable[[], Any],
        generation_provider: Callable[[], int],
        connected_provider: Callable[[], bool],
        background_runner: Callable[[Callable[[], None]], None],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._session_provider = session_provider
        self._generation_provider = generation_provider
        self._connected_provider = connected_provider
        self._background_runner = background_runner
        self._lock = threading.Lock()
        self._epoch = 0
        self._serial = 0
        self._panel_serials: dict[int, int] = {}
        self._panel_symbols: dict[int, str] = {}
        self._subscribed_symbols: set[str] = set()
        self._pending_confirms: dict[int, dict[str, Any]] = {}
        self._reconcile_requested = False
        self._force_resubscribe = False
        self._worker_running = False
        self._shutdown = False

    @property
    def desired_symbols(self) -> set[str]:
        with self._lock:
            return set(self._panel_symbols.values())

    @property
    def subscribed_symbols(self) -> set[str]:
        with self._lock:
            return set(self._subscribed_symbols)

    def request_symbol(self, panel_id: int, symbol: str) -> int:
        normalized = str(symbol or "").strip().upper()
        if not normalized:
            self.clear_panel(panel_id)
            return 0
        with self._lock:
            self._serial += 1
            serial = self._serial
            self._panel_serials[panel_id] = serial
            self._panel_symbols.pop(panel_id, None)
            self._pending_confirms[panel_id] = {
                "panel_id": panel_id,
                "symbol": normalized,
                "serial": serial,
                "epoch": self._epoch,
                "generation": self._generation_provider(),
            }
            self._reconcile_requested = True
        self._ensure_worker()
        return serial

    def clear_panel(self, panel_id: int) -> None:
        with self._lock:
            self._serial += 1
            self._panel_serials[panel_id] = self._serial
            self._pending_confirms.pop(panel_id, None)
            self._panel_symbols.pop(panel_id, None)
            self._reconcile_requested = True
        self._ensure_worker()

    def reconcile(self, *, force_resubscribe: bool = False) -> None:
        with self._lock:
            if force_resubscribe:
                self._subscribed_symbols.clear()
                self._force_resubscribe = True
            self._reconcile_requested = True
        self._ensure_worker()

    def reset(self, *, clear_desired: bool) -> None:
        with self._lock:
            self._epoch += 1
            self._pending_confirms.clear()
            self._subscribed_symbols.clear()
            self._reconcile_requested = False
            self._force_resubscribe = False
            if clear_desired:
                self._panel_symbols.clear()
                self._panel_serials.clear()
        self._safe_emit(self.subscriptions_changed, set())

    def shutdown(self) -> None:
        with self._lock:
            self._shutdown = True
            self._epoch += 1
            self._pending_confirms.clear()
            self._reconcile_requested = False

    def _ensure_worker(self) -> None:
        should_start = False
        with self._lock:
            if not self._shutdown and not self._worker_running:
                self._worker_running = True
                should_start = True
        if should_start:
            self._background_runner(self._worker_loop)

    def _worker_loop(self) -> None:
        while True:
            with self._lock:
                if self._shutdown:
                    self._worker_running = False
                    return
                if self._pending_confirms:
                    panel_id = next(iter(self._pending_confirms))
                    operation = ("confirm", self._pending_confirms.pop(panel_id))
                elif self._reconcile_requested:
                    self._reconcile_requested = False
                    operation = (
                        "reconcile",
                        {
                            "epoch": self._epoch,
                            "generation": self._generation_provider(),
                            "desired": set(self._panel_symbols.values()),
                            "subscribed": set(self._subscribed_symbols),
                            "force": self._force_resubscribe,
                        },
                    )
                    self._force_resubscribe = False
                else:
                    self._worker_running = False
                    return

            if operation[0] == "confirm":
                self._execute_confirm(operation[1])
            else:
                self._execute_reconcile(operation[1])

    def _request_is_current(self, request: dict[str, Any]) -> bool:
        return bool(
            request["epoch"] == self._epoch
            and request["generation"] == self._generation_provider()
            and request["serial"] == self._panel_serials.get(request["panel_id"])
        )

    def _execute_confirm(self, request: dict[str, Any]) -> None:
        with self._lock:
            if not self._request_is_current(request):
                return
        session = self._session_provider()
        if session is None or not self._connected_provider():
            self._finish_confirm(request, False, "\u4ea4\u6613\u670d\u52a1\u5668\u672a\u8fde\u63a5")
            return
        try:
            success, message = session.subscribe_quotes([request["symbol"]], timeout=6.0)
        except Exception as exc:
            success, message = False, str(exc) or "\u884c\u60c5\u8ba2\u9605\u5931\u8d25"
        self._finish_confirm(request, bool(success), str(message or ""))

    def _finish_confirm(self, request: dict[str, Any], success: bool, message: str) -> None:
        result: dict[str, Any] | None = None
        subscriptions: set[str] | None = None
        with self._lock:
            same_connection = bool(
                request["epoch"] == self._epoch
                and request["generation"] == self._generation_provider()
            )
            current = same_connection and self._request_is_current(request)
            if same_connection and success:
                self._subscribed_symbols.add(request["symbol"])
                subscriptions = set(self._subscribed_symbols)
            if current:
                if success:
                    self._panel_symbols[request["panel_id"]] = request["symbol"]
                result = {
                    **request,
                    "success": success,
                    "message": message,
                }
            if same_connection:
                self._reconcile_requested = True
        if subscriptions is not None:
            self._safe_emit(self.subscriptions_changed, subscriptions)
        if result is not None:
            self._safe_emit(self.symbol_result, result)

    def _execute_reconcile(self, operation: dict[str, Any]) -> None:
        with self._lock:
            if not self._operation_is_current(operation):
                return
        if not self._connected_provider():
            return
        session = self._session_provider()
        if session is None:
            return

        desired = set(operation["desired"])
        subscribed = set(operation["subscribed"])
        to_unsubscribe = sorted(subscribed - desired)
        to_subscribe = sorted(desired if operation["force"] else desired - subscribed)
        errors: list[str] = []

        if to_unsubscribe:
            try:
                success, message = session.unsubscribe_quotes(to_unsubscribe, timeout=6.0)
            except Exception as exc:
                success, message = False, str(exc)
            if success:
                with self._lock:
                    if self._operation_is_current(operation):
                        self._subscribed_symbols.difference_update(to_unsubscribe)
            else:
                errors.append(str(message or "\u884c\u60c5\u53d6\u6d88\u8ba2\u9605\u5931\u8d25"))

        if to_subscribe:
            try:
                success, message = session.subscribe_quotes(to_subscribe, timeout=6.0)
            except Exception as exc:
                success, message = False, str(exc)
            if success:
                with self._lock:
                    if self._operation_is_current(operation):
                        self._subscribed_symbols.update(to_subscribe)
            else:
                errors.append(str(message or "\u884c\u60c5\u8ba2\u9605\u5931\u8d25"))

        with self._lock:
            current = self._operation_is_current(operation)
            subscriptions = set(self._subscribed_symbols)
        if current:
            self._safe_emit(self.subscriptions_changed, subscriptions)
            if errors:
                self._safe_emit(self.sync_failed, "; ".join(errors))

    def _operation_is_current(self, operation: dict[str, Any]) -> bool:
        return bool(
            operation["epoch"] == self._epoch
            and operation["generation"] == self._generation_provider()
        )

    @staticmethod
    def _safe_emit(signal: Any, *args: Any) -> None:
        try:
            signal.emit(*args)
        except RuntimeError:
            pass
