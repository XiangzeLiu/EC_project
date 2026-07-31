from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from Client.services.trading_session import TradingSession
from Client.ui_qt.main_window import TradingTerminalQt
from Trader_Server.api.interactive_brokers import IBBroker
from Trader_Server.api.tastytrade import TastytradeBroker


class OrderCacheTests(unittest.TestCase):
    def test_all_order_queries_share_cache_until_forced(self):
        session = TradingSession(SimpleNamespace())
        response = {
            "payload": {
                "success": True,
                "orders": [{"id": "1", "status": "Filled"}],
            }
        }
        session._request_se = Mock(return_value=response)

        first = session._request_raw_orders("all")
        second = session._request_raw_orders("all")
        forced = session._request_raw_orders("all", force=True)

        self.assertEqual(first, second)
        self.assertEqual(forced, first)
        self.assertEqual(session._request_se.call_count, 2)

    def test_order_normalization_preserves_cancel_policy(self):
        session = TradingSession(SimpleNamespace())
        session.connected = True
        session.has_broker_capability = Mock(return_value=True)
        session._request_raw_orders = Mock(return_value=[{
            "id": "1",
            "symbol": "AAPL",
            "action": "Buy to Open",
            "qty": "10",
            "price": "100.00",
            "status": "Routed",
            "type": "LIMIT",
            "tif": "Day",
            "can_cancel": False,
        }])

        orders = session.get_orders("live")

        self.assertEqual(orders[0]["raw_status"], "Routing")
        self.assertFalse(orders[0]["can_cancel"])


class TastytradeOrderEventTests(unittest.TestCase):
    def test_account_order_event_is_normalized_and_forwarded(self):
        broker = TastytradeBroker()
        broker._account = SimpleNamespace(account_number="TT-1")
        received = []
        broker.set_order_event_callback(received.append)
        alert = SimpleNamespace(
            account_number="TT-1",
            id=123,
            underlying_symbol="AAPL",
            status="Live",
            size=100,
            cancellable=True,
            updated_at="2026-07-31T10:00:00Z",
            legs=[SimpleNamespace(fills=[SimpleNamespace(quantity=40)])],
        )

        broker._handle_order_alert(alert)

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["status"], "Partial")
        self.assertEqual(received[0]["filled_qty"], 40)
        self.assertTrue(received[0]["can_cancel"])

    def test_other_account_alert_is_ignored(self):
        broker = TastytradeBroker()
        broker._account = SimpleNamespace(account_number="TT-1")
        received = []
        broker.set_order_event_callback(received.append)

        broker._handle_order_alert(SimpleNamespace(
            account_number="TT-2",
            id=1,
            underlying_symbol="AAPL",
            status="Live",
            size=1,
            cancellable=True,
            updated_at="",
            legs=[],
        ))

        self.assertEqual(received, [])


class ClientOrderEventTests(unittest.TestCase):
    def test_filled_event_invalidates_orders_and_positions(self):
        queued = []
        invalidated = []
        window = SimpleNamespace(
            session=SimpleNamespace(invalidate_order_cache=lambda: invalidated.append(True)),
            _accept_broker_event=lambda payload: True,
            _queue_event_refresh=lambda **kwargs: queued.append(kwargs),
            _refresh_positions=lambda **kwargs: None,
        )

        TradingTerminalQt._handle_se_message_ui(window, {
            "type": "ORDER_STATUS_UPDATE",
            "payload": {"event_id": "evt-1", "status": "Filled"},
        })

        self.assertEqual(invalidated, [True])
        self.assertEqual(queued, [{
            "orders": True,
            "positions": True,
            "force_positions": True,
        }])

    def test_position_event_only_refreshes_positions(self):
        queued = []
        window = SimpleNamespace(
            session=None,
            _accept_broker_event=lambda payload: True,
            _queue_event_refresh=lambda **kwargs: queued.append(kwargs),
        )

        TradingTerminalQt._handle_se_message_ui(window, {
            "type": "POSITION_INVALIDATED",
            "payload": {"event_id": "evt-2", "reason": "execution"},
        })

        self.assertEqual(queued, [{"positions": True}])


class IBOrderEventTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _order_item(order_id: int, order_ref: str) -> dict:
        return {
            "order_id": order_id,
            "order": SimpleNamespace(
                account="U1",
                orderRef=order_ref,
                totalQuantity=10,
            ),
            "contract": SimpleNamespace(symbol="AAPL"),
            "status": "Submitted",
            "filled": 0,
            "remaining": 10,
            "updated_at": "2026-07-31T10:00:00Z",
        }

    async def test_only_ts_owned_ib_orders_are_forwarded(self):
        broker = IBBroker()
        broker._account_id = "U1"
        broker._order_event_queue = asyncio.Queue()
        broker._ib_app = SimpleNamespace(known_orders={
            1: self._order_item(1, "EC:BUY_OPEN"),
            2: self._order_item(2, ""),
        })
        received = []
        broker.set_order_event_callback(received.append)

        task = asyncio.create_task(broker._forward_order_events())
        await broker._order_event_queue.put({"order_id": 1})
        await broker._order_event_queue.put({"order_id": 2})
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["order_id"], "1")
        self.assertTrue(received[0]["can_cancel"])

    async def test_external_ib_execution_invalidates_account_positions(self):
        broker = IBBroker()
        broker._account_id = "U1"
        broker._position_event_queue = asyncio.Queue()
        broker._ib_app = SimpleNamespace()
        received = []
        broker.set_position_event_callback(received.append)

        task = asyncio.create_task(broker._forward_position_events())
        await broker._position_event_queue.put({
            "account_id": "U1",
            "symbol": "MSFT",
            "order_id": "99",
            "updated_at": "2026-07-31T10:00:00Z",
        })
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(received[0]["reason"], "execution")
        self.assertEqual(received[0]["symbol"], "MSFT")


if __name__ == "__main__":
    unittest.main()
