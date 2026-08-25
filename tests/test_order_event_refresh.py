from __future__ import annotations

import asyncio
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from Client.services.trading_session import QueryResult, TradingSession
from Client.ui_qt.main_window import DataTableModel, ORDER_STATUS_COLORS, TradingTerminalQt
from PySide6.QtCore import Qt
from Client.ui_qt.order_refresh_coordinator import OrderRefreshCoordinator
from Trader_Server.api import interactive_brokers as ib_module
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

    @staticmethod
    def _blocking_session():
        session = TradingSession(SimpleNamespace())
        started = threading.Event()
        release = threading.Event()

        def request(*args, **kwargs):
            started.set()
            if not release.wait(2.0):
                raise TimeoutError("test request was not released")
            return {
                "payload": {
                    "success": True,
                    "orders": [{"id": "blocked", "status": "Filled"}],
                }
            }

        session._request_se = Mock(side_effect=request)
        return session, started, release

    def test_cache_invalidation_does_not_wait_for_order_network_request(self):
        session, started, release = self._blocking_session()
        worker = threading.Thread(target=lambda: session._request_raw_orders("all"))
        worker.start()
        self.assertTrue(started.wait(1.0))

        invalidation_started = time.monotonic()
        session.invalidate_order_cache()
        invalidation_elapsed = time.monotonic() - invalidation_started

        release.set()
        worker.join(1.0)
        self.assertFalse(worker.is_alive())
        self.assertLess(invalidation_elapsed, 0.2)

    def test_in_flight_response_cannot_repopulate_invalidated_cache(self):
        session, started, release = self._blocking_session()
        worker = threading.Thread(target=lambda: session._request_raw_orders("all"))
        worker.start()
        self.assertTrue(started.wait(1.0))

        session.invalidate_order_cache()
        release.set()
        worker.join(1.0)

        self.assertEqual(session._all_orders_cache, [])
        self.assertEqual(session._all_orders_cache_at, 0.0)

    def test_concurrent_force_queries_reuse_one_newer_fetch(self):
        session, started, release = self._blocking_session()
        barrier = threading.Barrier(3)
        results = []

        def query():
            barrier.wait()
            results.append(session._request_raw_orders("all", force=True))

        workers = [threading.Thread(target=query) for _ in range(2)]
        for worker in workers:
            worker.start()
        barrier.wait()
        self.assertTrue(started.wait(1.0))
        time.sleep(0.05)
        release.set()
        for worker in workers:
            worker.join(1.0)

        self.assertEqual(session._request_se.call_count, 1)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])

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

    def test_order_query_distinguishes_timeout_from_successful_empty_result(self):
        session = TradingSession(SimpleNamespace())
        session.connected = True
        session.has_broker_capability = Mock(return_value=True)
        session._request_se = Mock(side_effect=[None, {"payload": {"success": True, "orders": []}}])

        failed = session.query_orders("live")
        empty = session.query_orders("live")

        self.assertFalse(failed.success)
        self.assertEqual(failed.error_code, "ORDER_QUERY_TIMEOUT")
        self.assertTrue(empty.success)
        self.assertEqual(empty.data, [])

    def test_live_query_filters_rejected_order_returned_by_ts(self):
        session = TradingSession(SimpleNamespace())
        session.connected = True
        session.has_broker_capability = Mock(return_value=True)
        session._request_raw_orders = Mock(return_value=[{
            "id": "7",
            "symbol": "AAPL",
            "action": "Buy to Open",
            "qty": "1",
            "price": "100.00",
            "status": "Rejected",
            "type": "Limit",
            "tif": "Day",
            "status_message": "price outside allowed range",
            "can_cancel": False,
        }])

        result = session.query_orders("live")

        self.assertTrue(result.success)
        self.assertEqual(result.data, [])

    def test_position_query_failure_is_structured_and_clears_stale_error_on_success(self):
        session = TradingSession(SimpleNamespace())
        session.connected = True
        session.has_broker_capability = Mock(return_value=True)
        session._request_se = Mock(side_effect=[None, {
            "payload": {"success": True, "positions": []},
        }, {
            "payload": {"success": True, "orders": []},
        }])

        failed = session.query_today_activity()
        succeeded = session.query_today_activity()

        self.assertFalse(failed.success)
        self.assertTrue(succeeded.success)
        self.assertEqual(session._pos_error, "")


class OrderTablePresentationTests(unittest.TestCase):
    def test_filled_status_uses_success_green(self):
        self.assertEqual(ORDER_STATUS_COLORS["Filled"], ORDER_STATUS_COLORS["Live"])

    def test_table_cells_are_centered_and_status_color_is_applied(self):
        model = DataTableModel(["状态"])
        model.set_rows([["Rejected"]], [[ORDER_STATUS_COLORS["Rejected"]]])
        index = model.index(0, 0)

        self.assertEqual(model.data(index, Qt.TextAlignmentRole), Qt.AlignCenter)
        self.assertEqual(
            model.data(index, Qt.ForegroundRole).name().upper(),
            ORDER_STATUS_COLORS["Rejected"].upper(),
        )

    def test_order_update_only_colors_status_column(self):
        captured = []
        window = SimpleNamespace(
            _orders_raw=[],
            orders_model=SimpleNamespace(
                set_rows=lambda rows, colors=None: captured.append((rows, colors)),
            ),
            order_count_label=SimpleNamespace(setText=lambda text: None),
        )

        TradingTerminalQt._update_orders(window, [{
            "symbol": "AAPL",
            "action": "BUY",
            "price": "100.00",
            "qty": "1",
            "otype": "Limit",
            "tif": "Day",
            "status": "Live",
            "raw_status": "Live",
        }])

        self.assertEqual(captured[0][1][0][:6], [None] * 6)
        self.assertEqual(captured[0][1][0][6], ORDER_STATUS_COLORS["Live"])

    def test_position_update_keeps_signed_short_quantity_and_positive_profit(self):
        rows = []
        window = SimpleNamespace(
            _positions_raw=[],
            _latest_quote_snapshot=lambda _symbol: {},
            positions_model=SimpleNamespace(set_rows=lambda value: rows.extend(value)),
            metric_shares=(None, SimpleNamespace(setText=lambda _value: None)),
            metric_realized=(None, SimpleNamespace(setText=lambda _value: None)),
            metric_unrealized=(None, SimpleNamespace(setText=lambda _value: None)),
        )

        TradingTerminalQt._update_positions(
            window,
            [
                {
                    "symbol": "SHORT",
                    "qty": -5,
                    "direction": "Short",
                    "avg_open": 100,
                    "close_px": 90,
                    "realized_today": 0,
                }
            ],
        )

        self.assertEqual(rows[0][3], -5)
        self.assertEqual(rows[0][6], "+50.00")


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

    def test_rejected_alert_includes_generic_status_message(self):
        broker = TastytradeBroker()
        broker._account = SimpleNamespace(account_number="TT-1")
        received = []
        broker.set_order_event_callback(received.append)

        broker._handle_order_alert(SimpleNamespace(
            account_number="TT-1",
            id=7,
            underlying_symbol="AAPL",
            status="Rejected",
            reject_reason="price outside allowed range",
            size=1,
            cancellable=False,
            updated_at="",
            legs=[],
        ))

        self.assertEqual(received[0]["status"], "Rejected")
        self.assertEqual(received[0]["status_message"], "price outside allowed range")

    def test_rejected_order_serialization_preserves_reason(self):
        order = SimpleNamespace(
            id=7,
            status="Rejected",
            reject_reason="price outside allowed range",
            cancellable=False,
            updated_at="2026-08-01T00:00:00Z",
            price=100,
            order_type="Limit",
            time_in_force="Day",
            legs=[SimpleNamespace(
                symbol="AAPL",
                action="Buy to Open",
                quantity=1,
                fills=[],
            )],
        )

        serialized = TastytradeBroker.serialize_order(order)

        self.assertEqual(serialized["status"], "Rejected")
        self.assertEqual(serialized["status_message"], "price outside allowed range")
        self.assertFalse(serialized["can_cancel"])

    def test_immediate_rejected_response_is_not_reported_as_success(self):
        response = SimpleNamespace(order=SimpleNamespace(
            id=7,
            status="Rejected",
            reject_reason="price outside allowed range",
        ))

        result = TastytradeBroker._normalize_place_order_response(response)

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "ORDER_REJECTED")
        self.assertEqual(result["status_message"], "price outside allowed range")


class ClientOrderEventTests(unittest.TestCase):
    def test_main_window_delegates_order_event_to_coordinator(self):
        received = []
        window = SimpleNamespace(
            _order_refresh=SimpleNamespace(
                handle_order_status_event=lambda payload: received.append(payload),
            ),
        )

        TradingTerminalQt._handle_se_message_ui(window, {
            "type": "ORDER_STATUS_UPDATE",
            "payload": {"event_id": "evt-1", "status": "Filled"},
        })

        self.assertEqual(received, [{"event_id": "evt-1", "status": "Filled"}])

    def test_main_window_delegates_position_event_to_coordinator(self):
        received = []
        window = SimpleNamespace(
            _order_refresh=SimpleNamespace(
                handle_position_event=lambda payload: received.append(payload),
            ),
        )

        TradingTerminalQt._handle_se_message_ui(window, {
            "type": "POSITION_INVALIDATED",
            "payload": {"event_id": "evt-2", "reason": "execution"},
        })

        self.assertEqual(received, [{"event_id": "evt-2", "reason": "execution"}])

    def test_failed_order_response_still_refreshes_order_table(self):
        action_refreshes = []
        logged = []
        tips = []
        window = SimpleNamespace(
            _action_limiter=SimpleNamespace(release=lambda token: None),
            _order_refresh=SimpleNamespace(handle_action_result=lambda: action_refreshes.append(True)),
            _se_generation=1,
            _log_user_error_once=lambda message: logged.append(message),
            _append_log=lambda *args, **kwargs: None,
            _show_weak_tip=lambda message, level: tips.append((message, level)),
        )

        TradingTerminalQt._handle_order_result(
            window,
            False,
            "订单被券商拒绝",
            generation=1,
        )

        self.assertEqual(logged, ["订单被券商拒绝"])
        self.assertEqual(tips, [("订单被券商拒绝", "err")])
        self.assertEqual(action_refreshes, [True])

    def test_successful_order_response_shows_success_tip_and_refreshes_order_table(self):
        action_refreshes = []
        logged = []
        tips = []
        window = SimpleNamespace(
            _action_limiter=SimpleNamespace(release=lambda token: None),
            _order_refresh=SimpleNamespace(handle_action_result=lambda: action_refreshes.append(True)),
            _se_generation=1,
            _log_user_error_once=lambda message: None,
            _append_log=lambda message, tag: logged.append((message, tag)),
            _show_weak_tip=lambda message, level: tips.append((message, level)),
        )

        TradingTerminalQt._handle_order_result(
            window,
            True,
            "订单提交成功",
            generation=1,
        )

        self.assertEqual(logged, [("订单提交成功", "ok")])
        self.assertEqual(tips, [("订单提交成功", "ok")])
        self.assertEqual(action_refreshes, [True])

    def test_rejected_order_cancel_message_explains_no_cancel_is_needed(self):
        messages = []
        window = SimpleNamespace(
            session=SimpleNamespace(broker_unavailable_message=lambda capability: "available"),
            _broker_capability_enabled=lambda capability: True,
            _selected_order=lambda: {
                "id": "7",
                "raw_status": "Rejected",
                "status_message": "[6099] Passive Limit Price Too Far From NBBO",
                "can_cancel": False,
            },
            _log_user_error_once=lambda message, *args, **kwargs: messages.append(message),
        )

        TradingTerminalQt._cancel_selected_order(window)

        self.assertEqual(
            messages,
            ["订单未被接受，无需撤销：[6099] Passive Limit Price Too Far From NBBO"],
        )


class OrderRefreshCoordinatorTests(unittest.TestCase):
    def test_manual_refresh_burst_coalesces_to_one_follow_up(self):
        jobs = []
        session = SimpleNamespace(
            query_orders=Mock(return_value=QueryResult(True, [])),
            query_today_activity=Mock(return_value=QueryResult(True, [])),
        )
        coordinator = OrderRefreshCoordinator(
            session_provider=lambda: session,
            generation_provider=lambda: 1,
            background_runner=jobs.append,
        )

        for _ in range(10):
            coordinator.refresh_orders(force=True)
            coordinator.refresh_positions(force_orders=True)

        self.assertEqual(len(jobs), 2)
        self.assertEqual(coordinator._pending, {"orders": True, "positions": True})

        with patch(
            "Client.ui_qt.order_refresh_coordinator.QTimer.singleShot",
            side_effect=lambda delay, callback: callback(),
        ):
            jobs[0]()
            jobs[1]()

        self.assertEqual(len(jobs), 4)
        jobs[2]()
        jobs[3]()
        self.assertEqual(session.query_orders.call_count, 2)
        self.assertEqual(session.query_today_activity.call_count, 2)

    def test_filled_event_is_deduplicated_and_schedules_follow_up(self):
        invalidated = []
        timer_starts = []
        session = SimpleNamespace(invalidate_order_cache=lambda: invalidated.append(True))
        coordinator = OrderRefreshCoordinator(
            session_provider=lambda: session,
            generation_provider=lambda: 1,
            background_runner=lambda fn: None,
        )
        coordinator._event_timer = SimpleNamespace(
            start=lambda delay: timer_starts.append(delay),
            stop=lambda: None,
        )

        with patch(
            "Client.ui_qt.order_refresh_coordinator.QTimer.singleShot"
        ) as single_shot:
            accepted = coordinator.handle_order_status_event(
                {"event_id": "evt-1", "status": "Filled"}
            )
            duplicate = coordinator.handle_order_status_event(
                {"event_id": "evt-1", "status": "Filled"}
            )

        self.assertTrue(accepted)
        self.assertFalse(duplicate)
        self.assertEqual(invalidated, [True])
        self.assertEqual(timer_starts, [300])
        self.assertEqual(
            coordinator._event_flags,
            {"orders": True, "positions": True, "force_positions": True},
        )
        single_shot.assert_called_once()
        self.assertEqual(single_shot.call_args.args[0], 1000)

    def test_inactive_order_event_does_not_refresh_positions(self):
        session = SimpleNamespace(invalidate_order_cache=lambda: None)
        coordinator = OrderRefreshCoordinator(
            session_provider=lambda: session,
            generation_provider=lambda: 1,
            background_runner=lambda fn: None,
        )
        coordinator._event_timer = SimpleNamespace(start=lambda _delay: None, stop=lambda: None)

        coordinator.handle_order_status_event({"event_id": "evt-cancelled", "status": "Cancelled"})

        self.assertEqual(
            coordinator._event_flags,
            {"orders": True, "positions": False, "force_positions": False},
        )

    def test_cancelled_order_with_fills_refreshes_positions(self):
        session = SimpleNamespace(invalidate_order_cache=lambda: None)
        coordinator = OrderRefreshCoordinator(
            session_provider=lambda: session,
            generation_provider=lambda: 1,
            background_runner=lambda fn: None,
        )
        coordinator._event_timer = SimpleNamespace(start=lambda _delay: None, stop=lambda: None)

        coordinator.handle_order_status_event(
            {"event_id": "evt-partial-cancel", "status": "Cancelled", "filled_qty": 3}
        )

        self.assertEqual(
            coordinator._event_flags,
            {"orders": True, "positions": True, "force_positions": True},
        )

    def test_action_results_share_one_restartable_follow_up_timer(self):
        coordinator = OrderRefreshCoordinator(
            session_provider=lambda: SimpleNamespace(),
            generation_provider=lambda: 1,
            background_runner=lambda fn: None,
        )
        immediate = []
        timer_starts = []
        coordinator.refresh_orders = lambda force=False: immediate.append(force)
        coordinator._action_timer = SimpleNamespace(
            start=lambda: timer_starts.append(True),
            stop=lambda: None,
        )

        coordinator.handle_action_result()
        coordinator.handle_action_result()

        self.assertEqual(immediate, [True, True])
        self.assertEqual(timer_starts, [True, True])

    def test_stale_generation_clears_in_flight_and_runs_pending_refresh(self):
        generation = [1]
        background_jobs = []
        received = []
        session = SimpleNamespace(
            get_orders=Mock(return_value=[{"id": "1", "status": "Live"}]),
        )
        coordinator = OrderRefreshCoordinator(
            session_provider=lambda: session,
            generation_provider=lambda: generation[0],
            background_runner=background_jobs.append,
        )
        coordinator.orders_ready.connect(received.append)

        coordinator.refresh_orders()
        generation[0] = 2
        coordinator.refresh_orders(force=True)

        with patch(
            "Client.ui_qt.order_refresh_coordinator.QTimer.singleShot",
            side_effect=lambda delay, callback: callback(),
        ):
            background_jobs[0]()

        self.assertEqual(received, [])
        self.assertEqual(len(background_jobs), 2)
        background_jobs[1]()
        self.assertEqual(received, [[{"id": "1", "status": "Live"}]])

    def test_failed_order_refresh_preserves_last_successful_rows(self):
        results = [
            QueryResult(True, [{"id": "1", "status": "Live"}]),
            QueryResult(False, message="订单查询超时"),
        ]
        session = SimpleNamespace(query_orders=Mock(side_effect=results))
        coordinator = OrderRefreshCoordinator(
            session_provider=lambda: session,
            generation_provider=lambda: 1,
            background_runner=lambda job: job(),
        )
        ready = []
        failed = []
        coordinator.orders_ready.connect(ready.append)
        coordinator.orders_failed.connect(failed.append)

        coordinator.refresh_orders()
        coordinator.refresh_orders(force=True)

        self.assertEqual(ready, [[{"id": "1", "status": "Live"}]])
        self.assertEqual(failed, ["订单查询超时"])
        self.assertEqual(coordinator._latest_orders, [{"id": "1", "status": "Live"}])

    def test_failed_position_refresh_does_not_publish_empty_rows(self):
        session = SimpleNamespace(
            query_today_activity=Mock(return_value=QueryResult(False, message="持仓查询超时")),
        )
        coordinator = OrderRefreshCoordinator(
            session_provider=lambda: session,
            generation_provider=lambda: 1,
            background_runner=lambda job: job(),
        )
        ready = []
        failed = []
        coordinator.positions_ready.connect(lambda rows, error: ready.append((rows, error)))
        coordinator.positions_failed.connect(failed.append)

        coordinator.refresh_positions()

        self.assertEqual(ready, [])
        self.assertEqual(failed, ["持仓查询超时"])

    def test_batch_cancel_aborts_when_latest_order_query_fails(self):
        captured = []
        session = SimpleNamespace(
            query_orders=Mock(return_value=QueryResult(False, message="订单查询超时")),
            cancel_order=Mock(),
        )
        window = SimpleNamespace(
            _ui=lambda callback: callback(),
            _handle_batch_cancel_result=lambda *args: captured.append(args),
        )

        TradingTerminalQt._cancel_symbol_live_orders_bg(
            window,
            "AAPL",
            "token",
            1,
            session,
        )

        session.cancel_order.assert_not_called()
        self.assertEqual(captured[0][1:3], (0, 0))
        self.assertEqual(captured[0][-1], "订单查询超时")


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

        with patch.object(ib_module, "_POSITION_EVENT_COALESCE_SECONDS", 0.01):
            task = asyncio.create_task(broker._forward_position_events())
            await broker._position_event_queue.put({
                "reason": "execution",
                "account_id": "U1",
                "symbol": "MSFT",
                "order_id": "99",
                "updated_at": "2026-07-31T10:00:00Z",
            })
            for _ in range(50):
                if received:
                    break
                await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertEqual(received[0]["reason"], "execution")
        self.assertEqual(received[0]["symbol"], "MSFT")

    async def test_ib_portfolio_events_ignore_snapshot_and_market_only_changes(self):
        if not ib_module._IB_AVAILABLE:
            self.skipTest("ibapi is not installed")
        queue = asyncio.Queue()
        app = ib_module._IBApp(asyncio.get_running_loop(), asyncio.Queue(), asyncio.Queue(), queue)
        contract = SimpleNamespace(conId=1, symbol="AAPL")
        app._account_updates_persistent = True
        app._account_updates_account = "U1"
        app._account_updates_initializing = "U1"

        app._on_portfolio("U1", contract, 10, 100, 1000, 90, 50, 0)
        app._on_account_download_end("U1")
        app._on_portfolio("U1", contract, 10, 101, 1010, 90, 60, 0)

        self.assertTrue(queue.empty())

    async def test_ib_portfolio_position_signature_changes_emit_events(self):
        if not ib_module._IB_AVAILABLE:
            self.skipTest("ibapi is not installed")
        queue = asyncio.Queue()
        app = ib_module._IBApp(asyncio.get_running_loop(), asyncio.Queue(), asyncio.Queue(), queue)
        contract = SimpleNamespace(conId=1, symbol="AAPL")
        app._account_updates_initializing = "U1"
        app._on_portfolio("U1", contract, 10, 100, 1000, 90, 50, 0)
        app._on_account_download_end("U1")

        app._on_portfolio("U1", contract, 11, 100, 1100, 90, 60, 0)
        app._on_portfolio("U1", contract, 11, 100, 1100, 91, 60, 0)
        app._on_portfolio("U1", contract, 11, 100, 1100, 91, 60, 1)

        self.assertEqual(queue.qsize(), 3)

    async def test_ib_exec_details_always_emit_position_invalidation(self):
        if not ib_module._IB_AVAILABLE:
            self.skipTest("ibapi is not installed")
        queue = asyncio.Queue()
        app = ib_module._IBApp(asyncio.get_running_loop(), asyncio.Queue(), asyncio.Queue(), queue)
        app._on_exec_details(
            0,
            SimpleNamespace(symbol="AAPL"),
            SimpleNamespace(acctNumber="U1", orderId=7, time="20260731 10:00:00 UTC"),
        )

        event = await asyncio.wait_for(queue.get(), timeout=0.1)
        self.assertEqual(event["reason"], "execution")
        self.assertEqual(event["order_id"], "7")

    async def test_ib_position_events_are_coalesced(self):
        broker = IBBroker()
        broker._account_id = "U1"
        broker._position_event_queue = asyncio.Queue()
        broker._ib_app = SimpleNamespace()
        received = []
        broker.set_position_event_callback(received.append)

        with patch.object(ib_module, "_POSITION_EVENT_COALESCE_SECONDS", 0.02):
            task = asyncio.create_task(broker._forward_position_events())
            for event in (
                {"reason": "portfolio", "account_id": "U1", "symbol": "AAPL"},
                {"reason": "portfolio", "account_id": "U1", "symbol": "MSFT"},
                {"reason": "execution", "account_id": "U1", "symbol": "MSFT", "order_id": "7"},
            ):
                await broker._position_event_queue.put(event)
            for _ in range(50):
                if received:
                    break
                await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["reason"], "execution")
        self.assertEqual(received[0]["symbol"], "")


if __name__ == "__main__":
    unittest.main()
