import os
import time
import unittest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from Client.ui_qt.action_rate_limiter import ActionRateLimiter
from Client.ui_qt import main_window as client_main_window
from Client.ui_qt.hotkey_config import (
    HOTKEY_BINDINGS,
    ORDER_HOTKEY_POLICY,
    ORDER_SUBMIT_POLICY,
    HotkeyAction,
    HotkeyBinding,
    HotkeyContext,
    RateLimitPolicy,
    validate_bindings,
)
from Client.ui_qt.main_window import TradePriceInput, TradingTerminalQt
from Client.ui_qt.shortcut_controller import ShortcutController
from Trader_Server.services import trading_svc


class FakeClock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class ActionRateLimiterTests(unittest.TestCase):
    def test_cooldown_and_identical_order_protection(self):
        clock = FakeClock()
        limiter = ActionRateLimiter(clock)
        policy = RateLimitPolicy(cooldown_ms=300, max_in_flight=2)

        first = limiter.acquire("order.submit", "panel:1", policy, signature="AAPL", identical_cooldown_ms=500)
        self.assertTrue(first.allowed)
        limiter.release(first.token)

        second = limiter.acquire("order.submit", "panel:1", policy, signature="AAPL", identical_cooldown_ms=500)
        self.assertFalse(second.allowed)
        self.assertEqual(second.reason, "cooldown")

        clock.advance(0.31)
        duplicate = limiter.acquire("order.submit", "panel:2", policy, signature="AAPL", identical_cooldown_ms=500)
        self.assertFalse(duplicate.allowed)
        self.assertEqual(duplicate.reason, "duplicate")

        clock.advance(0.20)
        allowed = limiter.acquire("order.submit", "panel:2", policy, signature="AAPL", identical_cooldown_ms=500)
        self.assertTrue(allowed.allowed)

    def test_burst_and_in_flight_limits(self):
        clock = FakeClock()
        limiter = ActionRateLimiter(clock)
        burst_policy = RateLimitPolicy(burst_limit=3, burst_window_ms=2000)
        for index in range(3):
            self.assertTrue(limiter.acquire("order.submit", str(index), burst_policy).allowed)
        self.assertEqual(limiter.acquire("order.submit", "fourth", burst_policy).reason, "burst")
        clock.advance(2.01)
        self.assertTrue(limiter.acquire("order.submit", "after-window", burst_policy).allowed)

        limiter.reset()
        in_flight_policy = RateLimitPolicy(max_in_flight=2)
        one = limiter.acquire("order.submit", "one", in_flight_policy)
        two = limiter.acquire("order.submit", "two", in_flight_policy)
        self.assertTrue(one.allowed)
        self.assertTrue(two.allowed)
        self.assertEqual(limiter.acquire("order.submit", "three", in_flight_policy).reason, "in_flight")
        limiter.release(one.token)
        self.assertTrue(limiter.acquire("order.submit", "three", in_flight_policy).allowed)


class HotkeyConfigTests(unittest.TestCase):
    def test_production_bindings_are_reserved_and_disabled(self):
        self.assertTrue(HOTKEY_BINDINGS)
        self.assertTrue(all(not binding.enabled and binding.key is None for binding in HOTKEY_BINDINGS))
        self.assertEqual(validate_bindings(HOTKEY_BINDINGS), [])

    def test_order_business_limits_are_disabled_but_key_cooldown_remains(self):
        self.assertEqual(ORDER_SUBMIT_POLICY.cooldown_ms, 0)
        self.assertEqual(ORDER_SUBMIT_POLICY.burst_limit, 0)
        self.assertEqual(ORDER_SUBMIT_POLICY.max_in_flight, 0)
        self.assertEqual(ORDER_HOTKEY_POLICY.cooldown_ms, 300)
        self.assertFalse(ORDER_HOTKEY_POLICY.allow_auto_repeat)

    def test_duplicate_keys_and_bad_parameters_are_rejected(self):
        bindings = [
            HotkeyBinding("one", "F8", HotkeyAction.ORDER_MARKET, HotkeyContext.TRADE_PANEL, True, {"side": "buy"}),
            HotkeyBinding("two", "F8", HotkeyAction.ORDER_MARKET, HotkeyContext.TRADE_PANEL, True, {"side": "sell"}),
            HotkeyBinding("qty", "F9", HotkeyAction.QUANTITY_SET, HotkeyContext.TRADE_PANEL, True, {"value": 0}),
        ]
        errors = validate_bindings(bindings)
        self.assertTrue(any("duplicate key" in error for error in errors))
        self.assertTrue(any("invalid quantity" in error for error in errors))

    def test_chinese_fonts_are_fallbacks_after_existing_english_fonts(self):
        ui_families = client_main_window.theme.ui_font().families()
        mono_families = client_main_window.theme.mono_font().families()
        self.assertEqual(ui_families[0], "Inter")
        self.assertEqual(mono_families[0], "JetBrains Mono")
        self.assertIn("Microsoft YaHei UI", ui_families[1:])
        self.assertIn("Microsoft YaHei UI", mono_families[1:])


class ShortcutControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_enabled_binding_dispatches_once(self):
        window = QWidget()
        called = []
        binding = HotkeyBinding(
            "refresh",
            "F8",
            HotkeyAction.REFRESH_ORDERS,
            HotkeyContext.MAIN_WINDOW,
            True,
        )
        controller = ShortcutController(window, [binding], called.append, lambda _binding: True)
        self.assertTrue(controller.install())
        window.show()
        window.activateWindow()
        window.setFocus()
        self.app.processEvents()

        QTest.keyClick(window, Qt.Key_F8)
        self.app.processEvents()

        controller.shutdown()
        window.close()
        self.assertEqual(called, [binding])

    def test_reserved_production_bindings_do_not_dispatch(self):
        window = QWidget()
        called = []
        controller = ShortcutController(window, HOTKEY_BINDINGS, called.append, lambda _binding: True)
        self.assertTrue(controller.install())
        window.show()
        window.activateWindow()
        window.setFocus()
        self.app.processEvents()

        QTest.keyClick(window, Qt.Key_F8)
        self.app.processEvents()

        controller.shutdown()
        window.close()
        self.assertEqual(called, [])

    def test_repeatable_adjustment_uses_controlled_timer(self):
        window = QWidget()
        called = []
        binding = HotkeyBinding(
            "quantity_up",
            "F8",
            HotkeyAction.QUANTITY_ADJUST,
            HotkeyContext.QUANTITY_CONTROL,
            True,
            {"delta": 1},
        )
        controller = ShortcutController(window, [binding], called.append, lambda _binding: True)
        self.assertTrue(controller.install())
        window.show()
        window.activateWindow()
        window.setFocus()
        self.app.processEvents()

        QTest.keyPress(window, Qt.Key_F8)
        QTest.qWait(410)
        QTest.keyRelease(window, Qt.Key_F8)
        count_after_release = len(called)
        QTest.qWait(140)

        controller.shutdown()
        window.close()
        self.assertGreaterEqual(count_after_release, 2)
        self.assertEqual(len(called), count_after_release)

    def test_price_enter_ignores_operating_system_auto_repeat(self):
        field = TradePriceInput()
        submitted = []
        field.returnPressed.connect(lambda: submitted.append(True))
        auto_repeat = QKeyEvent(
            QEvent.KeyPress,
            Qt.Key_Return,
            Qt.NoModifier,
            "",
            True,
            2,
        )
        QApplication.sendEvent(field, auto_repeat)
        self.assertEqual(submitted, [])


class FakeTradingSession:
    def __init__(self):
        self.connected = True
        self.mock_mode = False
        self.orders = []
        self.cancelled = []
        self.broker_detail = {
            "connected": True,
            "capabilities": {
                "quotes": True,
                "orders": True,
                "cancel_order": True,
                "positions": True,
                "order_query": True,
            },
            "account": {},
        }

    def has_broker_capability(self, name):
        return bool(self.broker_detail["capabilities"].get(name))

    def broker_unavailable_message(self, _capability=""):
        return "券商服务不可用"

    def place_order(self, symbol, qty, price, action, order_type, tif="Day"):
        self.orders.append((symbol, qty, price, action, order_type, tif))
        return True, "下单成功"

    def get_orders(self, mode="live"):
        return []

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return True, "撤单成功"

    def get_today_activity(self):
        return []

    def logout(self):
        self.connected = False


class ClientTradeCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = TradingTerminalQt()
        self.window._timer.stop()
        self.window._poll_timer.stop()
        self.window._build_root()
        self.window._main_ui_built = True
        self.window._init_ready = True
        self.window._se_connected = True
        self.session = FakeTradingSession()
        self.window.session = self.session
        self.window._run_bg = lambda fn: fn()
        self.window._ui = lambda fn: fn()
        slot = self.window.slots[1]
        slot.symbol.setCurrentText("AAPL")
        slot.current_symbol = "AAPL"
        slot.qty_label.setText("100")
        slot.order_type.setCurrentText("Limit")
        slot.tif.setCurrentText("Day")
        slot.price.setText("185.25")

    def tearDown(self):
        self.window._teardown_shortcuts()
        self.window.hide()
        self.window.deleteLater()
        self.app.processEvents()

    def test_existing_button_and_price_enter_payloads_stay_unchanged(self):
        self.window._place_order_from_panel("Buy to Open", 1)
        self.assertEqual(self.session.orders[-1], ("AAPL", 100, 185.25, "Buy to Open", "limit", "Day"))

        self.window._action_limiter.reset()
        self.window._on_price_enter(1)
        self.assertEqual(self.session.orders[-1], ("AAPL", 100, 185.25, "Buy to Open", "limit", "Day"))
        self.assertEqual(len(self.session.orders), 2)

    def test_header_combines_connection_and_full_broker_name(self):
        self.session.broker_detail["broker_type"] = "interactive_brokers"
        self.session.broker_detail["account"] = {"authority_level": "read-only"}
        self.window._set_ts_connection_state("online")
        self.assertEqual(self.window.status_text.text(), "ONLINE INTERACTIVE BROKERS")
        self.assertFalse(self.window.read_only_label.isHidden())
        self.assertTrue(self.window.live_orders_btn.property("online"))

        self.session.broker_detail["broker_type"] = "tastytrade"
        self.session.broker_detail["account"] = {"authority_level": "full"}
        self.window._apply_broker_status_ui()
        self.assertEqual(self.window.status_text.text(), "ONLINE TASTYTRADE")
        self.assertTrue(self.window.read_only_label.isHidden())

        self.window._set_ts_connection_state("offline")
        self.assertFalse(self.window.live_orders_btn.property("online"))

    def test_inactive_trade_panel_uses_glow_effect(self):
        first_effect = self.window.slots[1].container.graphicsEffect()
        second_effect = self.window.slots[2].container.graphicsEffect()
        self.assertTrue(first_effect.isEnabled())
        self.assertTrue(second_effect.isEnabled())
        self.assertGreater(first_effect.blurRadius(), second_effect.blurRadius())

        self.window._activate_panel(2)

        self.assertTrue(first_effect.isEnabled())
        self.assertTrue(second_effect.isEnabled())
        self.assertGreater(second_effect.blurRadius(), first_effect.blurRadius())

    def test_cancel_button_reuses_sell_style_without_changing_click_contract(self):
        self.assertEqual(self.window.cancel_order_btn.objectName(), "cancelOrderButton")
        self.assertTrue(self.window.cancel_order_btn.isEnabled())
        self.window.cancel_order_btn.setDown(True)
        self.assertTrue(self.window.cancel_order_btn.isDown())
        self.window.cancel_order_btn.setDown(False)

    def test_refresh_buttons_use_independent_cooldowns_and_force_fresh_data(self):
        clock = FakeClock()
        self.window._action_limiter = ActionRateLimiter(clock)
        order_calls = []
        position_calls = []
        self.window._order_refresh.refresh_orders = lambda **kwargs: order_calls.append(kwargs)
        self.window._order_refresh.refresh_positions = lambda **kwargs: position_calls.append(kwargs)

        for _ in range(3):
            self.window.orders_refresh_btn.click()
            self.window.positions_refresh_btn.click()

        self.assertEqual(order_calls, [{"force": True}])
        self.assertEqual(position_calls, [{"force_orders": True}])

        clock.advance(1.01)
        self.window.orders_refresh_btn.click()
        self.window.positions_refresh_btn.click()

        self.assertEqual(order_calls, [{"force": True}, {"force": True}])
        self.assertEqual(
            position_calls,
            [{"force_orders": True}, {"force_orders": True}],
        )

    def test_market_action_does_not_change_panel_order_type(self):
        self.window._submit_market_order("sell", 1)
        self.assertEqual(self.session.orders[-1], ("AAPL", 100, 0.0, "Sell to Close", "market", "Day"))
        self.assertEqual(self.window.slots[1].order_type.currentText(), "Limit")

    def test_shortcut_trade_still_obeys_broker_capability(self):
        self.session.broker_detail["capabilities"]["orders"] = False
        self.window._submit_market_order("buy", 1)
        self.assertEqual(self.session.orders, [])

    def test_pending_limit_confirm_is_one_shot(self):
        self.window.current_quote["AAPL"] = {
            "symbol": "AAPL",
            "bid": 185.10,
            "ask": 185.30,
            "last": 185.20,
            "received_monotonic": time.monotonic(),
        }
        self.window._prepare_limit_order("sell", 1, "bid")
        self.window._on_price_enter(1)
        self.window._on_price_enter(1)

        self.assertEqual(self.session.orders, [("AAPL", 100, 185.10, "Sell to Close", "limit", "Day")])
        self.assertEqual(self.window.slots[1].pending_action, "")

    def test_stale_quote_is_not_used_for_limit_preparation(self):
        self.window.current_quote["AAPL"] = {
            "symbol": "AAPL",
            "bid": 185.10,
            "ask": 185.30,
            "last": 185.20,
            "received_monotonic": time.monotonic() - 30,
        }
        self.window._prepare_limit_order("buy", 1, "ask")
        self.assertEqual(self.window.slots[1].price.text(), "")
        self.assertEqual(self.window.slots[1].pending_action, "Buy to Open")

    def test_panel_quantity_and_price_actions_are_isolated(self):
        self.window._activate_panel(2)
        self.assertEqual(self.window._active_panel_id, 2)
        self.assertTrue(self.window.slots[2].container.property("activePanel"))
        self.assertFalse(self.window.slots[1].container.property("activePanel"))

        self.window._set_qty(250, 2)
        self.window.slots[2].price.setText("10.00")
        self.window._adj_price(0.05, 2)
        self.assertEqual(self.window.slots[2].qty_value(), 250)
        self.assertEqual(self.window.slots[2].price.text(), "10.05")
        self.assertEqual(self.window.slots[1].qty_value(), 100)

        QTest.mouseClick(self.window.slots[1].container, Qt.LeftButton)
        self.assertEqual(self.window._active_panel_id, 1)

    def test_fast_duplicate_mouse_orders_are_not_limited_by_client(self):
        self.window._place_order_from_panel("Buy to Open", 1)
        self.window._place_order_from_panel("Buy to Open", 1)
        self.assertEqual(len(self.session.orders), 2)

    def test_batch_cancel_only_targets_live_orders_for_active_symbol(self):
        self.session.get_orders = lambda mode="live": [
            {"id": "aapl-live", "symbol": "AAPL", "raw_status": "Live"},
            {"id": "aapl-filled", "symbol": "AAPL", "raw_status": "Filled"},
            {"id": "msft-live", "symbol": "MSFT", "raw_status": "Live"},
        ]
        self.window._cancel_symbol_live_orders(1)
        self.assertEqual(self.session.cancelled, ["aapl-live"])

    def test_single_cancel_is_blocked_while_symbol_batch_is_running(self):
        self.window._orders_raw = [{"id": "aapl-live", "symbol": "AAPL"}]
        self.window.orders_model.set_rows([["AAPL", "SELL", "185", "100", "LMT", "Day", "Live"]])
        self.window.orders_table.selectRow(0)
        self.window._batch_canceling_symbols.add("AAPL")

        self.window._cancel_selected_order()

        self.assertEqual(self.session.cancelled, [])

    def test_temporary_key_mapping_reaches_main_window_trade_action(self):
        binding = HotkeyBinding(
            "test_market_buy",
            "F8",
            HotkeyAction.ORDER_MARKET,
            HotkeyContext.TRADE_PANEL,
            True,
            {"side": "buy"},
        )
        original_bindings = client_main_window.HOTKEY_BINDINGS
        client_main_window.HOTKEY_BINDINGS = (binding,)
        try:
            self.window._setup_shortcuts()
            self.window.show()
            self.window.activateWindow()
            self.window.slots[1].symbol.lineEdit().setFocus()
            self.app.processEvents()

            QTest.keyClick(self.window.slots[1].symbol.lineEdit(), Qt.Key_F8)
            self.app.processEvents()
        finally:
            self.window._teardown_shortcuts()
            client_main_window.HOTKEY_BINDINGS = original_bindings

        self.assertEqual(self.session.orders, [("AAPL", 100, 0.0, "Buy to Open", "market", "Day")])


class TraderServerOrderLimitTests(unittest.TestCase):
    def test_duplicate_order_window_is_disabled(self):
        order = {
            "symbol": "AAPL",
            "action": "Buy to Open",
            "qty": 1,
            "price": 100.0,
            "order_type": "limit",
            "tif": "Day",
        }
        trading_svc._ORDER_RECENT.clear()
        first = trading_svc._check_duplicate_order(order, "session-1", "trader")
        second = trading_svc._check_duplicate_order(order, "session-1", "trader")
        self.assertIsNone(first)
        self.assertIsNone(second)


if __name__ == "__main__":
    unittest.main()
