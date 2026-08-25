from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal

from Client.constants import LIVE_STATUSES, ORDERS_ACTIVE_INTERVAL, ORDERS_INTERVAL, POSITIONS_INTERVAL


class OrderRefreshCoordinator(QObject):
    """Coordinates order and position refreshes without owning their UI."""

    orders_ready = Signal(object)
    positions_ready = Signal(object, str)
    orders_failed = Signal(str)
    positions_failed = Signal(str)
    _orders_fetched = Signal(object)
    _positions_fetched = Signal(object)

    def __init__(
        self,
        *,
        session_provider: Callable[[], Any],
        generation_provider: Callable[[], int],
        background_runner: Callable[[Callable[[], None]], None],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._session_provider = session_provider
        self._generation_provider = generation_provider
        self._background_runner = background_runner
        self._state_lock = threading.Lock()
        self._epoch = 0
        self._request_serial = 0
        self._in_flight: dict[str, tuple[int, int] | None] = {"orders": None, "positions": None}
        self._pending = {"orders": False, "positions": False}
        self._force_pending = {"orders": False, "positions": False}
        self._event_flags = {"orders": False, "positions": False, "force_positions": False}
        self._seen_events: dict[str, float] = {}
        self._order_mode = "live"
        self._latest_orders: list[dict] = []
        self._last_orders_at = 0.0
        self._last_positions_at = 0.0

        self._event_timer = QTimer(self)
        self._event_timer.setSingleShot(True)
        self._event_timer.timeout.connect(self._flush_event_refresh)
        self._action_timer = QTimer(self)
        self._action_timer.setSingleShot(True)
        self._action_timer.setInterval(800)
        self._action_timer.timeout.connect(lambda: self.refresh_orders(force=True))
        self._orders_fetched.connect(self._finish_orders_fetch)
        self._positions_fetched.connect(self._finish_positions_fetch)

    @property
    def order_mode(self) -> str:
        return self._order_mode

    def set_order_mode(self, mode: str, *, refresh: bool = True) -> None:
        normalized = str(mode or "").lower()
        self._order_mode = normalized if normalized in {"live", "filled", "inactive", "all"} else "live"
        if refresh:
            self.refresh_orders(force=True)

    def poll(self, *, connected: bool, order_query_enabled: bool, positions_enabled: bool) -> None:
        if not connected:
            return
        now = time.monotonic()
        if positions_enabled and now - self._last_positions_at > POSITIONS_INTERVAL / 1000:
            self.refresh_positions()
        if order_query_enabled and now - self._last_orders_at > self._orders_interval_ms() / 1000:
            self.refresh_orders()

    def _orders_interval_ms(self) -> int:
        if self._order_mode == "live" and any(
            str(order.get("raw_status") or "") in LIVE_STATUSES for order in self._latest_orders
        ):
            return ORDERS_ACTIVE_INTERVAL
        return ORDERS_INTERVAL

    def _next_token(self) -> tuple[int, int]:
        self._request_serial += 1
        return self._epoch, self._request_serial

    def refresh_orders(self, *, force: bool = False) -> None:
        session = self._session_provider()
        if session is None:
            return
        self._last_orders_at = time.monotonic()
        with self._state_lock:
            if self._in_flight["orders"] is not None:
                self._pending["orders"] = True
                self._force_pending["orders"] |= force
                return
            token = self._next_token()
            self._in_flight["orders"] = token
        mode = self._order_mode
        generation = self._generation_provider()
        self._background_runner(
            lambda: self._fetch_orders(session, mode, generation, force, token)
        )

    def _fetch_orders(
        self,
        session: Any,
        mode: str,
        generation: int,
        force: bool,
        token: tuple[int, int],
    ) -> None:
        try:
            query = getattr(session, "query_orders", None)
            if callable(query):
                query_result = query(mode, force=force)
                success = bool(query_result.success)
                orders = list(query_result.data or [])
                error = str(query_result.message or "")
            else:
                success = True
                orders = session.get_orders(mode, force=force)
                error = ""
        except Exception:
            success = False
            orders = []
            error = "订单查询失败，请稍后刷新"
        self._orders_fetched.emit({
            "success": success,
            "orders": orders,
            "error": error,
            "generation": generation,
            "token": token,
        })

    def _finish_orders_fetch(self, result: dict) -> None:
        completion = self._complete_request("orders", result.get("token"))
        if completion is None:
            return
        rerun, force = completion
        if result.get("generation") == self._generation_provider():
            if result.get("success"):
                orders = list(result.get("orders") or [])
                self._latest_orders = orders
                self.orders_ready.emit(orders)
            else:
                self.orders_failed.emit(str(result.get("error") or "订单查询失败"))
        if rerun:
            QTimer.singleShot(0, lambda: self.refresh_orders(force=force))

    def refresh_positions(self, *, force_orders: bool = False) -> None:
        session = self._session_provider()
        if session is None:
            return
        self._last_positions_at = time.monotonic()
        with self._state_lock:
            if self._in_flight["positions"] is not None:
                self._pending["positions"] = True
                self._force_pending["positions"] |= force_orders
                return
            token = self._next_token()
            self._in_flight["positions"] = token
        generation = self._generation_provider()
        self._background_runner(
            lambda: self._fetch_positions(session, generation, force_orders, token)
        )

    def _fetch_positions(
        self,
        session: Any,
        generation: int,
        force_orders: bool,
        token: tuple[int, int],
    ) -> None:
        try:
            query = getattr(session, "query_today_activity", None)
            if callable(query):
                query_result = query(force_orders=force_orders)
                success = bool(query_result.success)
                positions = list(query_result.data or [])
                error = str(query_result.message or "")
            else:
                success = True
                positions = session.get_today_activity(force_orders=force_orders)
                error = str(getattr(session, "_pos_error", "") or "")
        except Exception:
            success = False
            positions = []
            error = "持仓查询失败，请稍后刷新"
        self._positions_fetched.emit({
            "success": success,
            "positions": positions,
            "error": error,
            "generation": generation,
            "token": token,
        })

    def _finish_positions_fetch(self, result: dict) -> None:
        completion = self._complete_request("positions", result.get("token"))
        if completion is None:
            return
        rerun, force_orders = completion
        if result.get("generation") == self._generation_provider():
            if result.get("success"):
                self.positions_ready.emit(list(result.get("positions") or []), "")
            else:
                self.positions_failed.emit(str(result.get("error") or "持仓查询失败"))
        if rerun:
            QTimer.singleShot(0, lambda: self.refresh_positions(force_orders=force_orders))

    def _complete_request(self, kind: str, token: Any) -> tuple[bool, bool] | None:
        with self._state_lock:
            if token != self._in_flight[kind]:
                return None
            self._in_flight[kind] = None
            rerun = self._pending[kind]
            force = self._force_pending[kind]
            self._pending[kind] = False
            self._force_pending[kind] = False
            return rerun, force

    def handle_order_status_event(self, payload: dict) -> bool:
        if not self._accept_event(payload):
            return False
        session = self._session_provider()
        if session is not None:
            session.invalidate_order_cache()
        status = str(payload.get("status") or "")
        try:
            filled_qty = float(payload.get("filled_qty") or 0)
        except (TypeError, ValueError):
            filled_qty = 0.0
        position_changed = status in {"Partial", "Filled"} or filled_qty > 0
        self._queue_event_refresh(
            orders=True,
            positions=position_changed,
            force_positions=position_changed,
        )
        if status == "Filled":
            epoch = self._epoch
            QTimer.singleShot(1000, lambda: self._filled_follow_up(epoch))
        return True

    def handle_action_result(self) -> None:
        """Refresh immediately, then keep one restartable consistency check."""
        self.refresh_orders(force=True)
        self._action_timer.start()

    def handle_position_event(self, payload: dict) -> bool:
        if not self._accept_event(payload):
            return False
        self._queue_event_refresh(positions=True)
        return True

    def _filled_follow_up(self, epoch: int) -> None:
        if epoch == self._epoch:
            self.refresh_positions(force_orders=True)

    def _accept_event(self, payload: dict) -> bool:
        event_id = str(payload.get("event_id") or "").strip()
        if not event_id:
            return True
        now = time.monotonic()
        cutoff = now - 120.0
        self._seen_events = {
            key: seen_at for key, seen_at in self._seen_events.items() if seen_at >= cutoff
        }
        if event_id in self._seen_events:
            return False
        self._seen_events[event_id] = now
        return True

    def _queue_event_refresh(
        self,
        *,
        orders: bool = False,
        positions: bool = False,
        force_positions: bool = False,
    ) -> None:
        self._event_flags["orders"] |= orders
        self._event_flags["positions"] |= positions
        self._event_flags["force_positions"] |= force_positions
        self._event_timer.start(300)

    def _flush_event_refresh(self) -> None:
        flags = dict(self._event_flags)
        self._event_flags = {"orders": False, "positions": False, "force_positions": False}
        if flags["orders"]:
            self.refresh_orders(force=True)
        if flags["positions"]:
            self.refresh_positions(force_orders=flags["force_positions"])

    def reset(self) -> None:
        with self._state_lock:
            self._epoch += 1
            self._in_flight = {"orders": None, "positions": None}
            self._pending = {"orders": False, "positions": False}
            self._force_pending = {"orders": False, "positions": False}
        self._event_flags = {"orders": False, "positions": False, "force_positions": False}
        self._event_timer.stop()
        self._action_timer.stop()
        self._seen_events.clear()
        self._latest_orders = []
        self._last_orders_at = 0.0
        self._last_positions_at = 0.0
