from __future__ import annotations

import asyncio
import queue
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

from Client.network.ts_websocket import TSWebSocketClient
from Client.services.trading_session import TradingSession
from Client.ui_qt.quote_subscription_coordinator import QuoteSubscriptionCoordinator
from Client.ui_qt.ts_connection_coordinator import TSConnectionCoordinator


class TSWebSocketRequestSafetyTests(unittest.TestCase):
    @staticmethod
    def _connected_client() -> TSWebSocketClient:
        client = TSWebSocketClient(reconnect_enabled=True)
        client._connected = True
        client._connection_id = "conn-1"
        client._wake_sender = lambda: None
        return client

    def test_timed_out_request_is_removed_from_pending_queue(self):
        client = self._connected_client()

        result = client.request_sync("ORDER_QUERY", {}, timeout=0.01)

        self.assertIsNone(result)
        self.assertEqual(client._pending_requests, [])
        self.assertEqual(client._response_waiters, {})

    def test_connection_invalidation_wakes_waiting_request(self):
        client = self._connected_client()
        result: list[object] = []

        thread = threading.Thread(
            target=lambda: result.append(client.request_sync("ORDER_SUBMIT", {}, timeout=2.0)),
        )
        thread.start()
        deadline = time.monotonic() + 1.0
        while not client._pending_requests and time.monotonic() < deadline:
            time.sleep(0.005)

        started = time.monotonic()
        client._connected = False
        client._invalidate_connection_requests()
        thread.join(timeout=0.5)

        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(result, [None])
        self.assertEqual(client._pending_requests, [])


class TSWebSocketSendFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_failure_does_not_requeue_request(self):
        client = TSWebSocketClient(reconnect_enabled=True)
        client._active = True
        client._connected = True
        client._connection_id = "conn-1"
        client._conn_lost = asyncio.Event()
        client._send_wakeup = asyncio.Event()
        client._send_wakeup.set()
        client._pending_requests = [("conn-1", {"id": "req-1", "type": "ORDER_SUBMIT"})]
        client._response_waiters["req-1"] = queue.Queue(maxsize=1)
        client._send_ws_json = AsyncMock(side_effect=ConnectionError("closed"))
        client._is_ws_open = lambda _ws: True

        await asyncio.wait_for(client._send_pending_loop(object(), "conn-1"), timeout=0.5)

        self.assertFalse(client._connected)
        self.assertTrue(client._conn_lost.is_set())
        self.assertEqual(client._pending_requests, [])
        self.assertIsNone(client._response_waiters["req-1"].get_nowait())

    async def test_cancelled_request_is_skipped_without_blocking_next_request(self):
        client = TSWebSocketClient(reconnect_enabled=True)
        client._active = True
        client._connected = True
        client._connection_id = "conn-1"
        client._conn_lost = asyncio.Event()
        client._send_wakeup = asyncio.Event()
        client._send_wakeup.set()
        first = {"id": "req-1", "type": "ORDER_QUERY"}
        second = {"id": "req-2", "type": "ORDER_QUERY"}
        client._pending_requests = [("conn-1", first), ("conn-1", second)]
        client._cancel_request("req-1")
        client._send_ws_json = AsyncMock()
        client._is_ws_open = lambda _ws: True

        task = asyncio.create_task(client._send_pending_loop(object(), "conn-1"))
        deadline = time.monotonic() + 0.5
        while client._send_ws_json.await_count < 1 and time.monotonic() < deadline:
            await asyncio.sleep(0.005)
        client._active = False
        client._send_wakeup.set()
        await asyncio.wait_for(task, timeout=0.5)

        client._send_ws_json.assert_awaited_once_with(ANY, second)


class QuoteDispatchCoalescingTests(unittest.TestCase):
    def test_quote_messages_are_coalesced_before_the_qt_message_signal(self):
        coordinator = TSConnectionCoordinator(
            http_client=SimpleNamespace(),
            session_provider=lambda: None,
            username_provider=lambda: "trader",
            reconnect_allowed_provider=lambda: True,
            background_runner=lambda callback: callback(),
        )
        routed_messages = []
        coordinator.message_received.connect(lambda _generation, message: routed_messages.append(message))
        handler = coordinator._message_handler(coordinator.generation)

        for price in range(100):
            handler({
                "type": "QUOTE_DATA",
                "payload": {
                    "symbol": "AAPL",
                    "bid": price,
                    "ask": price + 1,
                    "last": price + 0.5,
                },
            })

        updates = coordinator.drain_quote_updates()
        self.assertEqual(routed_messages, [])
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["bid"], 99)
        self.assertEqual(updates[0]["_client_connection_generation"], coordinator.generation)
        self.assertEqual(coordinator.drain_quote_updates(), [])


class TradingSessionUnknownOrderStateTests(unittest.TestCase):
    class _NoResponseSE:
        is_connected = True

        @staticmethod
        def request_sync(_msg_type, _payload, timeout=10.0):
            del timeout
            return None

    @classmethod
    def _session(cls) -> TradingSession:
        session = TradingSession(object())
        session.connected = True
        session.bind_se_client(cls._NoResponseSE())
        session.set_broker_detail({
            "broker_type": "test",
            "connected": True,
            "capabilities": {
                "orders": True,
                "cancel_order": True,
                "positions": True,
                "order_query": True,
                "quotes": True,
            },
        })
        return session

    def test_order_submit_timeout_is_reported_as_unknown(self):
        ok, message = self._session().place_order("AAPL", 1, 100.0, "Buy to Open")

        self.assertFalse(ok)
        self.assertIn("\u72b6\u6001\u672a\u77e5", message)

    def test_order_cancel_timeout_is_reported_as_unknown(self):
        ok, message = self._session().cancel_order("order-1")

        self.assertFalse(ok)
        self.assertIn("\u72b6\u6001\u672a\u77e5", message)


class QuoteSubscriptionCoordinatorTests(unittest.TestCase):
    class _Session:
        def __init__(self):
            self.subscribe_calls: list[tuple[str, ...]] = []
            self.unsubscribe_calls: list[tuple[str, ...]] = []
            self.block_symbol = ""
            self.subscribe_started = threading.Event()
            self.subscribe_release = threading.Event()

        def subscribe_quotes(self, symbols, timeout=6.0):
            del timeout
            normalized = tuple(symbols)
            self.subscribe_calls.append(normalized)
            if self.block_symbol and self.block_symbol in normalized:
                self.subscribe_started.set()
                self.subscribe_release.wait(timeout=1.0)
            return True, "ok"

        def unsubscribe_quotes(self, symbols, timeout=6.0):
            del timeout
            self.unsubscribe_calls.append(tuple(symbols))
            return True, "ok"

    @staticmethod
    def _runner(callback):
        threading.Thread(target=callback, daemon=True).start()

    @staticmethod
    def _wait_for(predicate, timeout=1.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.005)
        return bool(predicate())

    def _coordinator(self, session):
        return QuoteSubscriptionCoordinator(
            session_provider=lambda: session,
            generation_provider=lambda: 1,
            connected_provider=lambda: True,
            background_runner=self._runner,
        )

    def test_switching_symbol_unsubscribes_previous_symbol(self):
        session = self._Session()
        coordinator = self._coordinator(session)

        coordinator.request_symbol(1, "AAPL")
        self.assertTrue(self._wait_for(lambda: coordinator.subscribed_symbols == {"AAPL"}))
        coordinator.request_symbol(1, "MU")

        self.assertTrue(self._wait_for(lambda: coordinator.subscribed_symbols == {"MU"}))
        self.assertEqual(coordinator.desired_symbols, {"MU"})
        self.assertIn(("AAPL",), session.unsubscribe_calls)

    def test_stale_confirm_is_reconciled_after_newer_symbol(self):
        session = self._Session()
        session.block_symbol = "AAPL"
        coordinator = self._coordinator(session)

        coordinator.request_symbol(1, "AAPL")
        self.assertTrue(session.subscribe_started.wait(timeout=0.5))
        coordinator.request_symbol(1, "NVDA")
        session.subscribe_release.set()

        self.assertTrue(self._wait_for(lambda: coordinator.subscribed_symbols == {"NVDA"}))
        self.assertEqual(coordinator.desired_symbols, {"NVDA"})
        self.assertIn(("AAPL",), session.unsubscribe_calls)

    def test_shared_symbol_is_removed_only_after_both_panels_leave(self):
        session = self._Session()
        coordinator = self._coordinator(session)

        coordinator.request_symbol(1, "AAPL")
        coordinator.request_symbol(2, "AAPL")
        self.assertTrue(self._wait_for(lambda: coordinator.desired_symbols == {"AAPL"}))
        coordinator.clear_panel(1)
        self.assertTrue(self._wait_for(lambda: coordinator.subscribed_symbols == {"AAPL"}))
        coordinator.clear_panel(2)

        self.assertTrue(self._wait_for(lambda: coordinator.subscribed_symbols == set()))
        self.assertIn(("AAPL",), session.unsubscribe_calls)

    def test_reconnect_restores_only_desired_symbols(self):
        session = self._Session()
        coordinator = self._coordinator(session)
        coordinator.request_symbol(1, "AAPL")
        self.assertTrue(self._wait_for(lambda: coordinator.subscribed_symbols == {"AAPL"}))
        initial_subscribe_count = len(session.subscribe_calls)

        coordinator.reset(clear_desired=False)
        coordinator.reconcile(force_resubscribe=True)

        self.assertTrue(self._wait_for(lambda: len(session.subscribe_calls) > initial_subscribe_count))
        self.assertEqual(coordinator.desired_symbols, {"AAPL"})
        self.assertEqual(coordinator.subscribed_symbols, {"AAPL"})

if __name__ == "__main__":
    unittest.main()
