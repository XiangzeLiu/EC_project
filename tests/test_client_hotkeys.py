import json
import os
import tempfile
import time
import unittest
from dataclasses import replace


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QWidget

from Client.ui_qt.action_rate_limiter import ActionRateLimiter
from Client.ui_qt import main_window as client_main_window
from Client.ui_qt.hotkey_config_store import hotkey_config_path, load_hotkey_config, save_hotkey_config
from Client.ui_qt.hotkey_config import (
    DEFAULT_QUANTITY_HOTKEY_IDS,
    DEFAULT_HOTKEY_CONFIG,
    HOTKEY_BINDINGS,
    MAX_QUANTITY_HOTKEY_RULES,
    ORDER_HOTKEY_POLICY,
    ORDER_SUBMIT_POLICY,
    HotkeyAction,
    HotkeyBinding,
    HotkeyContext,
    HotkeyRuntimeConfig,
    OrderHotkeyRule,
    QuantityHotkey,
    RateLimitPolicy,
    format_hotkey_validation_errors,
    validate_bindings,
    validate_hotkey_config,
)
from Client.ui_qt.main_window import TradePriceInput, TradingTerminalQt
from Client.ui_qt.shortcut_controller import ShortcutController, validate_shortcut_sequences
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
    def test_scoped_hotkey_configs_are_stored_and_loaded_independently(self):
        first_scope = "1" * 64
        second_scope = "2" * 64
        with tempfile.TemporaryDirectory() as tmp:
            old_config_dir = os.environ.get("SC_CLIENT_CONFIG_DIR")
            os.environ["SC_CLIENT_CONFIG_DIR"] = tmp
            try:
                first_config = replace(DEFAULT_HOTKEY_CONFIG, default_route="ARCA")
                second_config = replace(DEFAULT_HOTKEY_CONFIG, default_route="SMART")
                first_path = save_hotkey_config(first_config, scope_id=first_scope)
                second_path = save_hotkey_config(second_config, scope_id=second_scope)

                self.assertNotEqual(first_path, second_path)
                self.assertEqual(first_path.name, "hotkey.json")
                self.assertEqual(first_path.parent.parent.name, "profiles")
                self.assertEqual(
                    load_hotkey_config(scope_id=first_scope).config.default_route,
                    "ARCA",
                )
                self.assertEqual(
                    load_hotkey_config(scope_id=second_scope).config.default_route,
                    "SMART",
                )
            finally:
                if old_config_dir is None:
                    os.environ.pop("SC_CLIENT_CONFIG_DIR", None)
                else:
                    os.environ["SC_CLIENT_CONFIG_DIR"] = old_config_dir

    def test_default_order_hotkeys_match_confirmed_table(self):
        rules = DEFAULT_HOTKEY_CONFIG.order_hotkeys
        self.assertEqual(len(rules), 12)
        self.assertEqual(
            [
                (rule.key, rule.side, rule.order_type, rule.tif, rule.route, rule.price_offset, rule.hidden, rule.enabled)
                for rule in rules
            ],
            [
                ("Shift+F1", "buy", "limit", "Day", "DEFAULT", 0.0, False, False),
                ("Shift+F2", "sell", "limit", "Day", "DEFAULT", 0.0, False, False),
                ("Shift+F3", "buy", "limit", "GTC", "DEFAULT", 0.0, False, False),
                ("Shift+F4", "sell", "limit", "GTC", "DEFAULT", 0.0, False, False),
                ("Shift+F5", "buy", "limit", "EXT", "DEFAULT", 0.0, False, False),
                ("Shift+F6", "sell", "limit", "EXT", "DEFAULT", 0.0, False, False),
                ("Shift+F7", "buy", "limit", "GTC_EXT", "DEFAULT", 0.0, False, False),
                ("Shift+F8", "sell", "limit", "GTC_EXT", "DEFAULT", 0.0, False, False),
                ("Shift+F9", "buy", "limit", "IOC", "DEFAULT", 0.0, False, False),
                ("Shift+F10", "sell", "limit", "IOC", "DEFAULT", 0.0, False, False),
                ("Shift+F11", "buy", "market", "Day", "DEFAULT", 0.0, False, False),
                ("Shift+F12", "sell", "market", "Day", "DEFAULT", 0.0, False, False),
            ],
        )

    def test_quantity_defaults_are_fixed_and_custom_rules_round_trip(self):
        default_quantities = DEFAULT_HOTKEY_CONFIG.quantity_hotkeys
        self.assertEqual(
            tuple(item.id for item in default_quantities),
            DEFAULT_QUANTITY_HOTKEY_IDS,
        )
        self.assertEqual(
            tuple(item.key for item in default_quantities),
            tuple(f"Num+{index}" for index in range(1, 10)),
        )

        custom = QuantityHotkey(
            id="quantity_custom_1",
            key="F8",
            quantity=350,
            enabled=True,
        )
        config = replace(
            DEFAULT_HOTKEY_CONFIG,
            quantity_hotkeys=default_quantities + (custom,),
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "hotkey.json")
            save_hotkey_config(config, path=path)
            loaded = load_hotkey_config(path=path)
            with open(path, encoding="utf-8") as fh:
                payload = json.load(fh)

        self.assertTrue(loaded.used_local_config)
        self.assertEqual(loaded.config, config)
        self.assertEqual(payload["version"], 4)
        self.assertEqual(payload["quantity_hotkeys"][-1]["id"], "quantity_custom_1")

    def test_quantity_rules_require_defaults_and_enforce_total_limit(self):
        missing_default = replace(
            DEFAULT_HOTKEY_CONFIG,
            quantity_hotkeys=DEFAULT_HOTKEY_CONFIG.quantity_hotkeys[1:],
        )
        errors = validate_hotkey_config(missing_default)
        self.assertTrue(any("必须保留" in error for error in errors))
        self.assertTrue(any("Num+1" in error for error in errors))

        changed_default = replace(
            DEFAULT_HOTKEY_CONFIG.quantity_hotkeys[0],
            key="F8",
        )
        changed_config = replace(
            DEFAULT_HOTKEY_CONFIG,
            quantity_hotkeys=(changed_default,) + DEFAULT_HOTKEY_CONFIG.quantity_hotkeys[1:],
        )
        self.assertTrue(
            any("固定按键" in error for error in validate_hotkey_config(changed_config))
        )

        custom_rules = tuple(
            QuantityHotkey(
                id=f"quantity_custom_{index}",
                key=f"Ctrl+F{index}",
                quantity=index * 10,
                enabled=True,
            )
            for index in range(1, 13)
        )
        too_many = replace(
            DEFAULT_HOTKEY_CONFIG,
            quantity_hotkeys=DEFAULT_HOTKEY_CONFIG.quantity_hotkeys + custom_rules,
        )
        self.assertEqual(len(too_many.quantity_hotkeys), MAX_QUANTITY_HOTKEY_RULES + 1)
        self.assertTrue(
            any("最多 20 条" in error for error in validate_hotkey_config(too_many))
        )

    def test_old_structured_config_version_falls_back_without_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "hotkey.json")
            save_hotkey_config(DEFAULT_HOTKEY_CONFIG, path=path)
            with open(path, encoding="utf-8") as fh:
                payload = json.load(fh)
            payload["version"] = 3
            for item in payload["quantity_hotkeys"]:
                item.pop("id", None)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)

            loaded = load_hotkey_config(path=path)

        self.assertFalse(loaded.used_local_config)
        self.assertTrue(loaded.errors)
        self.assertEqual(loaded.config, DEFAULT_HOTKEY_CONFIG)

    def test_production_bindings_keep_order_rules_disabled(self):
        self.assertTrue(HOTKEY_BINDINGS)
        order_rules = [binding for binding in HOTKEY_BINDINGS if binding.action == HotkeyAction.ORDER_PREPARE_RULE]
        fixed_or_qty = [binding for binding in HOTKEY_BINDINGS if binding.action != HotkeyAction.ORDER_PREPARE_RULE]
        self.assertTrue(order_rules)
        self.assertTrue(all(not binding.enabled for binding in order_rules))
        self.assertTrue(all(binding.enabled and binding.key for binding in fixed_or_qty))
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
        self.assertTrue(any("快捷键冲突" in error for error in errors))
        self.assertTrue(any("invalid quantity" in error for error in errors))

    def test_hotkey_validation_messages_hide_internal_details(self):
        messages = format_hotkey_validation_errors([
            "order_rule_1 已启用但没有快捷键",
            "enabled binding has no key: order_rule_1",
            "快捷键无效：order_rule_1 = 'Ctrl+Foo'",
        ])
        joined = "；".join(messages)
        self.assertIn("已启用的快捷键不能为空", joined)
        self.assertIn("快捷键格式无效", joined)
        self.assertNotIn("order_rule_1", joined)
        self.assertNotIn("enabled binding", joined)
        self.assertNotIn("Ctrl+Foo", joined)

    def test_route_configuration_rejects_invalid_characters(self):
        config = HotkeyRuntimeConfig(
            default_route="AR CA",
            quantity_hotkeys=DEFAULT_HOTKEY_CONFIG.quantity_hotkeys,
            order_hotkeys=(OrderHotkeyRule(id="bad-route", key=None, route="AR/CA"),),
        )

        errors = validate_hotkey_config(config)

        self.assertTrue(any("默认 ROUTE 格式无效" in error for error in errors))
        self.assertTrue(any("bad-route ROUTE 格式无效" in error for error in errors))

    def test_chinese_fonts_are_fallbacks_after_existing_english_fonts(self):
        ui_families = client_main_window.theme.ui_font().families()
        mono_families = client_main_window.theme.mono_font().families()
        self.assertEqual(ui_families[0], "Inter")
        self.assertEqual(mono_families[0], "JetBrains Mono")
        self.assertEqual(client_main_window.theme.FONT_CJK_FALLBACKS[0], "SimHei")
        self.assertEqual(ui_families[1], "SimHei")
        self.assertEqual(mono_families[1], "SimHei")
        self.assertIn("Microsoft YaHei UI", ui_families[1:])
        self.assertIn("Microsoft YaHei UI", mono_families[1:])

    def test_hotkey_json_overrides_defaults_and_saves_user_fields_only(self):
        binding = HotkeyBinding(
            "market_buy",
            None,
            HotkeyAction.ORDER_MARKET,
            HotkeyContext.TRADE_PANEL,
            False,
            {"side": "buy"},
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "hotkey.json")
            save_hotkey_config((
                HotkeyBinding(
                    "market_buy",
                    "F8",
                    HotkeyAction.ORDER_MARKET,
                    HotkeyContext.TRADE_PANEL,
                    True,
                    {"side": "buy"},
                ),
            ), path=path)

            loaded = load_hotkey_config((binding,), path=path)

            self.assertTrue(loaded.used_local_config)
            self.assertEqual(loaded.errors, ())
            self.assertEqual(loaded.bindings[0].key, "F8")
            self.assertTrue(loaded.bindings[0].enabled)
            with open(path, encoding="utf-8") as fh:
                saved = fh.read()
            self.assertIn('"id": "market_buy"', saved)
            self.assertNotIn("order.market", saved)
            self.assertNotIn("active_trade_panel", saved)

    def test_bad_hotkey_json_falls_back_to_defaults(self):
        default = HotkeyBinding(
            "market_buy",
            None,
            HotkeyAction.ORDER_MARKET,
            HotkeyContext.TRADE_PANEL,
            False,
            {"side": "buy"},
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "hotkey.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{bad json")

            loaded = load_hotkey_config((default,), path=path)

            self.assertFalse(loaded.used_local_config)
            self.assertTrue(loaded.errors)
            self.assertEqual(loaded.bindings, (default,))

    def test_qt_key_conflicts_are_rejected_before_save(self):
        conflicting = replace(
            DEFAULT_HOTKEY_CONFIG.order_hotkeys[0],
            key="Space",
            enabled=True,
        )
        config = replace(
            DEFAULT_HOTKEY_CONFIG,
            order_hotkeys=(conflicting,) + DEFAULT_HOTKEY_CONFIG.order_hotkeys[1:],
        )
        errors = validate_shortcut_sequences(client_main_window.bindings_from_config(config))
        self.assertTrue(any("快捷键冲突" in error for error in errors))

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "hotkey.json")
            with self.assertRaises(ValueError):
                save_hotkey_config(config, path=path)
            self.assertFalse(os.path.exists(path))

    def test_disabled_order_rule_cannot_reserve_an_existing_key(self):
        conflicting = replace(
            DEFAULT_HOTKEY_CONFIG.order_hotkeys[0],
            key="Num+1",
            enabled=False,
        )
        config = replace(
            DEFAULT_HOTKEY_CONFIG,
            order_hotkeys=(conflicting,) + DEFAULT_HOTKEY_CONFIG.order_hotkeys[1:],
        )

        errors = validate_shortcut_sequences(client_main_window.bindings_from_config(config))

        self.assertTrue(any("快捷键冲突" in error for error in errors))

    def test_invalid_qt_key_in_local_config_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "hotkey.json")
            save_hotkey_config(DEFAULT_HOTKEY_CONFIG, path=path)
            with open(path, encoding="utf-8") as fh:
                payload = json.load(fh)
            payload["order_hotkeys"][0]["enabled"] = True
            payload["order_hotkeys"][0]["key"] = "Ctrl+"
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)

            loaded = load_hotkey_config(path=path)

            self.assertFalse(loaded.used_local_config)
            self.assertTrue(loaded.errors)
            self.assertEqual(loaded.config, DEFAULT_HOTKEY_CONFIG)
            self.assertEqual(loaded.bindings, HOTKEY_BINDINGS)


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
        self.config_scope = "a" * 64
        self.mock_mode = False
        self.auth_remaining = None
        self.local_auth_cleared = False
        self.orders = []
        self.order_details = []
        self.order_queries = []
        self.refresh_quote_calls = []
        self.refresh_quote_result = (False, {}, "行情刷新超时，IOC订单未提交")
        self.broker_status_queries = 0
        self.cancelled = []
        self.symbol_options = {}
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
            "order_options": {
                "default_route": "SMART",
                "routes": ["SMART"],
                "route_editable": False,
                "hidden_order": False,
                "supported_tifs": ["Day", "GTC", "IOC", "EXT", "GTC_EXT"],
            },
        }

    def has_broker_capability(self, name):
        return bool(self.broker_detail["capabilities"].get(name))

    def broker_unavailable_message(self, _capability=""):
        return "券商服务不可用"

    def place_order(self, symbol, qty, price, action, order_type, tif="Day", route="", hidden=False):
        self.orders.append((symbol, qty, price, action, order_type, tif))
        self.order_details.append({"route": route, "hidden": hidden})
        return True, "下单成功"

    def get_orders(self, mode="live", *, force=False):
        self.order_queries.append((mode, force))
        return []

    def broker_status_query(self):
        self.broker_status_queries += 1
        return True, self.broker_detail, "ok"

    def refresh_quote(self, symbol, price_source, timeout=5.0):
        self.refresh_quote_calls.append((symbol, price_source, timeout))
        return self.refresh_quote_result

    def symbol_order_options(self, symbol):
        return dict(self.symbol_options.get(str(symbol).strip().upper()) or {})

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return True, "撤单成功"

    def get_today_activity(self):
        return []

    def logout(self):
        self.connected = False

    def auth_seconds_remaining(self):
        return self.auth_remaining

    def is_auth_expired(self):
        return self.auth_remaining is not None and self.auth_remaining <= 0

    def bind_se_client(self, _client):
        pass

    def clear_local_auth(self):
        self.local_auth_cleared = True
        self.connected = False


class ClientTradeCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._old_config_dir = os.environ.get("SC_CLIENT_CONFIG_DIR")
        self._config_dir = tempfile.TemporaryDirectory()
        os.environ["SC_CLIENT_CONFIG_DIR"] = self._config_dir.name
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
        self.window._order_refresh._background_runner = self.window._run_bg
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
        if self._old_config_dir is None:
            os.environ.pop("SC_CLIENT_CONFIG_DIR", None)
        else:
            os.environ["SC_CLIENT_CONFIG_DIR"] = self._old_config_dir
        self._config_dir.cleanup()

    def test_existing_button_and_price_enter_payloads_stay_unchanged(self):
        self.window._place_order_from_panel("Buy to Open", 1)
        self.assertEqual(self.session.orders[-1], ("AAPL", 100, 185.25, "Buy to Open", "limit", "Day"))

        self.window._action_limiter.reset()
        self.window._on_price_enter(1)
        self.assertEqual(self.session.orders[-1], ("AAPL", 100, 185.25, "Buy to Open", "limit", "Day"))
        self.assertEqual(len(self.session.orders), 2)

    def test_enter_target_starts_empty_and_mouse_buy_selects_buy(self):
        slot = self.window.slots[1]
        slot.set_trade_enabled(True)

        self.assertEqual(slot.enter_target, "NONE")
        self.window._handle_enter_target(1)
        self.assertEqual(self.session.orders, [])

        slot.buy.click()

        self.assertEqual(self.session.orders[-1][3], "Buy to Open")
        self.assertEqual(slot.enter_target, "BUY")
        self.assertTrue(slot.buy.property("enterSelected"))
        self.assertFalse(slot.sell.property("enterSelected"))

    def test_mouse_sell_selects_sell_and_enter_reuses_direction(self):
        slot = self.window.slots[1]
        slot.set_trade_enabled(True)

        slot.sell.click()
        self.assertEqual(slot.enter_target, "SELL")
        self.assertFalse(slot.buy.property("enterSelected"))
        self.assertTrue(slot.sell.property("enterSelected"))

        self.window._action_limiter.reset()
        self.window._handle_enter_target(1)
        self.assertEqual(self.session.orders[-1][3], "Sell to Close")

    def test_symbol_target_queries_without_submitting_order(self):
        slot = self.window.slots[1]
        requested = []
        self.window._quote_subscriptions.request_symbol = (
            lambda panel_id, symbol: requested.append((panel_id, symbol)) or 1
        )
        slot.symbol.setCurrentText("MSFT")
        slot.current_symbol = ""

        self.window._set_enter_target(1, "SYMBOL")
        self.window._handle_enter_target(1)

        self.assertEqual(requested, [(1, "MSFT")])
        self.assertEqual(self.session.orders, [])
        self.assertFalse(slot.buy.property("enterSelected"))
        self.assertFalse(slot.sell.property("enterSelected"))

    def test_fixed_enter_binding_dispatches_symbol_query_inside_trade_panel(self):
        slot = self.window.slots[1]
        requested = []
        self.window._quote_subscriptions.request_symbol = (
            lambda panel_id, symbol: requested.append((panel_id, symbol)) or 1
        )
        slot.symbol.setCurrentText("MSFT")
        slot.current_symbol = ""

        self.window._setup_shortcuts()
        self.window.show()
        self.window.activateWindow()
        slot.symbol.lineEdit().setFocus()
        self.app.processEvents()
        QTest.keyClick(slot.symbol.lineEdit(), Qt.Key_Return)
        self.app.processEvents()
        self.window._teardown_shortcuts()

        self.assertEqual(requested, [(1, "MSFT")])
        self.assertEqual(self.session.orders, [])

    def test_pending_shortcut_selects_direction_and_enter_confirms_it(self):
        self.window.current_quote["AAPL"] = {
            "symbol": "AAPL",
            "bid": 185.10,
            "ask": 185.30,
            "received_monotonic": time.monotonic(),
            "connection_generation": self.window._se_generation,
        }
        slot = self.window.slots[1]

        self.window._prepare_limit_order("sell", 1, "bid")

        self.assertEqual(slot.pending_action, "Sell to Close")
        self.assertEqual(slot.enter_target, "SELL")
        self.assertTrue(slot.sell.property("enterSelected"))
        self.assertFalse(slot.buy.property("enterSelected"))

        self.window._handle_enter_target(1)
        self.assertEqual(self.session.orders[-1][3], "Sell to Close")
        self.assertEqual(slot.pending_action, "")

    def test_ioc_hotkey_does_not_leave_enter_selection(self):
        self.window.current_quote["AAPL"] = {
            "symbol": "AAPL",
            "bid": 185.10,
            "ask": 185.30,
            "received_monotonic": time.monotonic(),
            "connection_generation": self.window._se_generation,
        }
        slot = self.window.slots[1]

        self.window._prepare_configured_order({
            "side": "buy",
            "order_type": "limit",
            "tif": "IOC",
            "route": "DEFAULT",
        }, 1)

        self.assertEqual(slot.pending_action, "")
        self.assertEqual(slot.enter_target, "NONE")
        self.assertFalse(slot.buy.property("enterSelected"))
        self.assertFalse(slot.sell.property("enterSelected"))

    def test_enter_target_isolated_between_trade_panels(self):
        self.window._set_enter_target(1, "BUY")
        self.window._set_enter_target(2, "SELL")

        self.assertEqual(self.window.slots[1].enter_target, "BUY")
        self.assertEqual(self.window.slots[2].enter_target, "SELL")
        self.assertTrue(self.window.slots[1].buy.property("enterSelected"))
        self.assertTrue(self.window.slots[2].sell.property("enterSelected"))

    def test_limit_hotkey_default_price_uses_bid_but_ioc_buy_keeps_ask(self):
        self.window.current_quote["AAPL"] = {
            "symbol": "AAPL",
            "bid": 185.10,
            "ask": 185.30,
            "last": 185.20,
            "received_monotonic": time.monotonic(),
            "connection_generation": self.window._se_generation,
        }

        self.window._prepare_configured_order(
            {
                "side": "buy",
                "order_type": "limit",
                "tif": "Day",
                "route": "DEFAULT",
            },
            1,
        )
        self.assertEqual(self.window.slots[1].price.text(), "185.10")

        self.window._cancel_pending_order(1)
        self.window._prepare_configured_order(
            {
                "side": "buy",
                "order_type": "limit",
                "tif": "IOC",
                "route": "DEFAULT",
            },
            1,
        )
        self.assertEqual(self.session.orders[-1][2], 185.30)

    def test_trade_panels_use_quantity_input_without_step_buttons(self):
        for slot in self.window.slots.values():
            self.assertIsNotNone(slot.qty_label)
            self.assertTrue(slot.qty_label.isEnabled())
            self.assertEqual(
                slot.qty_box.findChildren(QPushButton, "qtyStepButton"),
                [],
            )

        first = self.window.slots[1]
        first.qty_label.setText("275")
        first.qty_label.editingFinished.emit()
        self.assertEqual(first.qty_value(), 275)

    def test_trade_panels_display_bid_and_ask_without_last(self):
        for slot in self.window.slots.values():
            captions = {
                label.text()
                for label in slot.container.findChildren(QLabel)
            }
            self.assertIn("BID", captions)
            self.assertIn("ASK", captions)
            self.assertNotIn("LAST", captions)

        first = self.window.slots[1]
        first.update_quote({"last": 185.20, "bid": 185.10, "ask": 185.30})
        self.assertEqual(first.bid.text(), "185.10")
        self.assertEqual(first.ask.text(), "185.30")
        first.clear_quote()
        self.assertEqual(first.bid.text(), "--")
        self.assertEqual(first.ask.text(), "--")

    def test_symbol_edit_does_not_auto_query_until_explicit_confirmation(self):
        slot = self.window.slots[1]
        requested = []
        self.window._quote_subscriptions.request_symbol = (
            lambda panel_id, symbol: requested.append((panel_id, symbol)) or 1
        )
        slot.set_trade_enabled(True)
        slot.symbol.setCurrentText("MSFT")
        self.app.processEvents()
        QTest.qWait(420)

        self.assertEqual(requested, [])
        self.assertEqual(slot.current_symbol, "")
        self.assertFalse(slot.symbol_pending)
        self.assertEqual(slot.bid.text(), "--")
        self.assertEqual(slot.ask.text(), "--")
        self.assertFalse(slot.buy.isEnabled())
        self.assertFalse(slot.sell.isEnabled())

        slot.symbol.lineEdit().setFocus()
        QTest.keyClick(slot.symbol.lineEdit(), Qt.Key_Return)
        self.assertEqual(requested, [(1, "MSFT")])

    def test_quote_query_button_is_styled_and_queries_its_own_panel(self):
        left = self.window.slots[1]
        right = self.window.slots[2]
        requested = []
        self.window._quote_subscriptions.request_symbol = (
            lambda panel_id, symbol: requested.append((panel_id, symbol)) or 1
        )

        for slot in (left, right):
            self.assertIsNotNone(slot.quote_query_button)
            self.assertEqual(slot.quote_query_button.objectName(), "quoteQueryButton")
            self.assertEqual(slot.quote_query_button.size().width(), 40)
            self.assertEqual(slot.quote_query_button.size().height(), 44)
            self.assertEqual(slot.quote_query_button.focusPolicy(), Qt.NoFocus)
            self.assertFalse(slot.quote_query_button.isCheckable())
            self.assertFalse(slot.quote_query_button.icon().isNull())
        self.assertIn("QPushButton#quoteQueryButton:hover", client_main_window.theme.APP_QSS)
        self.assertIn("QPushButton#quoteQueryButton:pressed", client_main_window.theme.APP_QSS)

        right.symbol.setCurrentText("MU")
        right.quote_query_button.click()
        self.assertEqual(requested, [(2, "MU")])
        self.assertEqual(left.symbol_text(), "AAPL")
        self.assertEqual(right.symbol_text(), "MU")

    def test_header_hides_provider_name_and_preserves_read_only(self):
        self.session.broker_detail["broker_type"] = "interactive_brokers"
        self.session.broker_detail["account"] = {"authority_level": "read-only"}
        self.window._set_ts_connection_state("online")
        self.assertEqual(self.window.status_text.text(), "ONLINE")
        self.assertFalse(self.window.read_only_label.isHidden())
        self.assertTrue(self.window.live_orders_btn.property("online"))

        self.session.broker_detail["broker_type"] = "tastytrade"
        self.session.broker_detail["account"] = {"authority_level": "full"}
        self.window._apply_broker_status_ui()
        self.assertEqual(self.window.status_text.text(), "ONLINE")
        self.assertTrue(self.window.read_only_label.isHidden())

        self.window._set_ts_connection_state("offline")
        self.assertFalse(self.window.live_orders_btn.property("online"))

    def test_header_has_no_brand_logo_and_order_tabs_keep_updated_style(self):
        logo = self.window.findChild(QLabel, "mainLogo")
        self.assertIsNone(logo)
        self.assertEqual(self.window.live_orders_btn.text(), "● 进行中")
        self.assertTrue(self.window.live_orders_btn.property("selected"))

    def test_trade_tables_use_dark_scrollbar_style(self):
        self.assertEqual(self.window.orders_table.objectName(), "tradeDataTable")
        self.assertEqual(self.window.positions_table.objectName(), "tradeDataTable")
        for table in (self.window.orders_table, self.window.positions_table):
            self.assertNotEqual(table.focusPolicy(), Qt.NoFocus)
            self.assertIsInstance(
                table.itemDelegate(),
                client_main_window.NoFocusRectItemDelegate,
            )
        scrollbar_qss = client_main_window.theme.SCROLLBAR_QSS
        self.assertIn("QScrollBar:vertical", scrollbar_qss)
        self.assertIn("QScrollBar::handle:horizontal:hover", scrollbar_qss)
        self.assertIn("background: #5A6675", scrollbar_qss)
        self.assertIn("QPushButton:focus", client_main_window.theme.APP_QSS)
        self.assertIn("QCheckBox:focus", client_main_window.theme.APP_QSS)
        self.assertIn("QAbstractItemView::item:focus", client_main_window.theme.APP_QSS)
        self.assertIn(scrollbar_qss, client_main_window.theme.APP_QSS)
        self.assertIn(scrollbar_qss, client_main_window.theme.COMBO_POPUP_QSS)

    def test_root_background_and_window_state_layout_refresh_are_enabled(self):
        root = self.window.centralWidget()
        self.assertTrue(root.testAttribute(Qt.WA_StyledBackground))
        self.assertEqual(
            root.palette().color(root.backgroundRole()).name().upper(),
            client_main_window.theme.TERM_BG,
        )

        self.window.show()
        self.app.processEvents()
        self.window.resize(1500, 900)
        self.window._finalize_window_state_layout()
        self.app.processEvents()

        self.assertEqual(root.geometry(), self.window.contentsRect())

    def test_settings_combos_use_the_same_dark_popup_as_main_controls(self):
        self.window._open_settings_overlay()
        overlay = self.window._settings_overlay
        settings_combo = overlay._order_rows[0]["side"]
        self.assertEqual(settings_combo.view().objectName(), "comboPopup")
        self.assertIn(client_main_window.theme.INPUT_BG, settings_combo.view().styleSheet())
        self.assertEqual(self.window.slots[1].order_type.view().objectName(), "comboPopup")
        self.assertIn(client_main_window.theme.TEXT_PRIMARY, self.window.slots[1].order_type.view().styleSheet())
        self.assertIn("QListView#comboPopup::item:disabled", client_main_window.theme.COMBO_POPUP_QSS)

    def test_logout_button_keeps_header_order_and_vertical_alignment(self):
        self.window.resize(1500, 900)
        self.window.show()
        self.app.processEvents()

        header = self.window.logout_btn.parentWidget()
        settings_center_y = self.window.settings_btn.mapTo(
            header,
            self.window.settings_btn.rect().center(),
        ).y()
        logout_center_y = self.window.logout_btn.mapTo(
            header,
            self.window.logout_btn.rect().center(),
        ).y()
        clock_center_y = self.window._clock.mapTo(
            header,
            self.window._clock.rect().center(),
        ).y()
        header_widgets = [
            header.layout().itemAt(index).widget()
            for index in range(header.layout().count())
            if header.layout().itemAt(index).widget() is not None
        ]

        self.assertLess(header_widgets.index(self.window.settings_btn), header_widgets.index(self.window.logout_btn))
        self.assertLess(header_widgets.index(self.window.logout_btn), header_widgets.index(self.window._clock.parentWidget()))
        self.assertLessEqual(abs(logout_center_y - settings_center_y), 1)
        self.assertLessEqual(abs(logout_center_y - clock_center_y), 1)
        self.assertEqual(self.window.logout_btn.objectName(), "logoutButton")
        self.assertEqual(self.window.logout_btn.font().families()[0], client_main_window.theme.FONT_UI)
        self.assertIn("SimHei", self.window.logout_btn.font().families())

    def test_logout_and_enter_selection_styles_are_scoped(self):
        qss = client_main_window.theme.APP_QSS

        self.assertIn("QPushButton#logoutButton", qss)
        self.assertIn(f"background: {client_main_window.theme.ACCENT_YELLOW};", qss)
        self.assertIn('QPushButton#buyButton[enterSelected="true"]', qss)
        self.assertIn('QPushButton#sellButton[enterSelected="true"]', qss)
        self.assertIn("border: 1.5px solid #F5A623;", qss)

    def test_ioc_is_disabled_for_tt_and_remains_available_for_ib(self):
        tt_tifs = ["Day", "GTC", "EXT", "GTC_EXT"]
        self.session.broker_detail["order_options"]["supported_tifs"] = tt_tifs
        self.window._apply_broker_status_ui()

        for slot in self.window.slots.values():
            ioc_index = slot.tif.findText("IOC")
            self.assertGreaterEqual(ioc_index, 0)
            self.assertFalse(slot.tif.model().item(ioc_index).isEnabled())
            self.assertEqual(slot.tif.currentText(), "Day")

        self.window._open_settings_overlay()
        overlay = self.window._settings_overlay
        self.assertFalse(any(
            row["tif"].currentText() == "IOC"
            for row in overlay._order_rows
        ))
        for row in overlay._order_rows:
            combo = row["tif"]
            ioc_index = combo.findText("IOC")
            self.assertFalse(combo.model().item(ioc_index).isEnabled())
        self.assertIn("不支持 IOC", overlay.order_capability_note.text())
        self.window._close_settings_overlay()

        self.session.broker_detail["order_options"]["supported_tifs"] = [*tt_tifs, "IOC"]
        self.window._apply_broker_status_ui()
        for slot in self.window.slots.values():
            ioc_index = slot.tif.findText("IOC")
            self.assertTrue(slot.tif.model().item(ioc_index).isEnabled())

        self.window._open_settings_overlay()
        overlay = self.window._settings_overlay
        for row in overlay._order_rows:
            combo = row["tif"]
            ioc_index = combo.findText("IOC")
            self.assertTrue(combo.model().item(ioc_index).isEnabled())
        self.assertNotIn("不支持 IOC", overlay.order_capability_note.text())

    def test_hide_is_last_row_control_and_reaches_submit_payload_when_supported(self):
        self.session.broker_detail.update({
            "broker_type": "interactive_brokers",
            "order_options": {
                "default_route": "SMART",
                "routes": ["SMART", "ARCA", "NYSE"],
                "route_editable": True,
                "hidden_order": True,
                "supported_tifs": ["Day", "GTC", "IOC", "EXT", "GTC_EXT"],
            },
        })
        self.window._apply_broker_status_ui()
        slot = self.window.slots[1]
        self.assertTrue(slot.hidden_order.isEnabled())
        self.assertEqual(slot.hidden_order.text(), "")
        self.assertEqual(slot.hidden_order_caption.text(), "HIDE")
        self.window.show()
        self.app.processEvents()
        hide_center = slot.hidden_order.mapTo(slot.container, slot.hidden_order.rect().center())
        qty_center = slot.qty_box.mapTo(slot.container, slot.qty_box.rect().center())
        qty_right = slot.qty_box.mapTo(slot.container, slot.qty_box.rect().topRight()).x()
        self.assertGreater(hide_center.x(), qty_right)
        self.assertLess(
            abs(hide_center.y() - qty_center.y()),
            18,
        )
        self.assertLessEqual(slot.hidden_order.parentWidget().width(), 52)
        slot.hidden_order.setChecked(True)

        self.window._place_order_from_panel("Buy to Open", 1)

        self.assertTrue(self.session.order_details[-1]["hidden"])

    def test_each_trade_panel_uses_fixed_global_route_candidates(self):
        self.session.broker_detail.update({
            "broker_type": "interactive_brokers",
            "order_options": {
                "default_route": "SMART",
                "routes": ["SMART", "ARCA", "NYSE"],
                "route_editable": True,
                "hidden_order": True,
            },
        })
        self.session.symbol_options = {
            "AAPL": {
                "default_route": "SMART",
                "routes": ["SMART", "ARCA"],
                "route_editable": True,
                "hidden_order": True,
                "routes_validated": True,
            },
            "MU": {
                "default_route": "SMART",
                "routes": ["SMART", "NYSE"],
                "route_editable": True,
                "hidden_order": True,
                "routes_validated": True,
            },
        }
        second = self.window.slots[2]
        second.symbol.setCurrentText("MU")
        second.current_symbol = "MU"

        self.window._apply_broker_status_ui()

        first_routes = [
            self.window.slots[1].route.itemText(index)
            for index in range(self.window.slots[1].route.count())
        ]
        second_routes = [second.route.itemText(index) for index in range(second.route.count())]
        self.assertEqual(first_routes, ["SMART", "ARCA", "NYSE"])
        self.assertEqual(second_routes, ["SMART", "ARCA", "NYSE"])

    def test_panel_keeps_selected_candidate_until_symbol_route_validation(self):
        self.window._hotkey_config = replace(DEFAULT_HOTKEY_CONFIG, default_route="ARCA")
        self.session.broker_detail.update({
            "broker_type": "interactive_brokers",
            "order_options": {
                "default_route": "SMART",
                "routes": ["SMART", "ARCA", "NYSE"],
                "route_editable": True,
                "hidden_order": True,
            },
        })
        self.session.symbol_options["AAPL"] = {
            "default_route": "SMART",
            "routes": ["SMART", "NYSE"],
            "route_editable": True,
            "hidden_order": True,
            "routes_validated": True,
        }
        slot = self.window.slots[1]
        slot.route.addItem("ARCA")
        slot.route.setCurrentText("ARCA")

        self.window._apply_order_options_to_slot(slot)

        self.assertEqual(slot.route.currentText(), "ARCA")

    def test_invalid_symbol_route_is_blocked_before_order_submit(self):
        self.session.broker_detail.update({
            "broker_type": "interactive_brokers",
            "order_options": {
                "default_route": "SMART",
                "routes": ["SMART", "ARCA"],
                "route_editable": True,
                "hidden_order": True,
            },
        })
        self.session.symbol_options["AAPL"] = {
            "default_route": "SMART",
            "routes": ["SMART", "NYSE"],
            "route_editable": True,
            "hidden_order": True,
            "routes_validated": True,
        }
        tips = []
        self.window._show_weak_tip = lambda message, level="inf", duration_ms=3000: tips.append((message, level))

        self.window._place_order(
            "Buy to Open",
            1,
            order_type_override="market",
            price_override=0.0,
            route_override="ARCA",
            source="hotkey",
        )

        self.assertEqual(self.session.orders, [])
        self.assertEqual(
            tips,
            [("AAPL 当前股票或IB账户不支持所选ROUTE ARCA，订单未提交，请改用SMART", "warn")],
        )

    def test_non_smart_route_is_blocked_until_symbol_routes_are_validated(self):
        self.session.broker_detail.update({
            "broker_type": "interactive_brokers",
            "order_options": {
                "default_route": "SMART",
                "routes": ["SMART", "ARCA", "NYSE"],
                "route_editable": True,
                "hidden_order": True,
            },
        })
        tips = []
        self.window._show_weak_tip = lambda message, level="inf", duration_ms=3000: tips.append((message, level))

        self.window._place_order(
            "Buy to Open",
            1,
            order_type_override="market",
            price_override=0.0,
            route_override="ARCA",
            source="hotkey",
        )

        self.assertEqual(self.session.orders, [])
        self.assertEqual(len(tips), 1)
        self.assertIn("请改用SMART", tips[0][0])

        self.window._place_order(
            "Buy to Open",
            1,
            order_type_override="market",
            price_override=0.0,
            route_override="SMART",
            source="hotkey",
        )

        self.assertEqual(self.session.order_details[-1]["route"], "SMART")

    def test_valid_symbol_route_reaches_shared_order_submit_path(self):
        self.session.broker_detail.update({
            "broker_type": "interactive_brokers",
            "order_options": {
                "default_route": "SMART",
                "routes": ["SMART", "ARCA"],
                "route_editable": True,
                "hidden_order": True,
            },
        })
        self.session.symbol_options["AAPL"] = {
            "default_route": "SMART",
            "routes": ["SMART", "ARCA"],
            "route_editable": True,
            "hidden_order": True,
            "routes_validated": True,
        }

        self.window._place_order(
            "Buy to Open",
            1,
            order_type_override="market",
            price_override=0.0,
            route_override="ARCA",
            source="hotkey",
        )

        self.assertEqual(self.session.order_details[-1]["route"], "ARCA")

    def test_hidden_hotkey_is_editable_for_ib(self):
        self.window._hotkey_config = HotkeyRuntimeConfig(order_hotkeys=(
            OrderHotkeyRule(id="ib-hidden", key="Shift+F1", hidden=True),
        ))
        self.session.broker_detail.update({
            "broker_type": "interactive_brokers",
            "order_options": {
                "default_route": "SMART",
                "routes": ["SMART", "ARCA", "NYSE"],
                "route_editable": True,
                "hidden_order": True,
                "supported_tifs": ["Day", "GTC", "IOC", "EXT", "GTC_EXT"],
            },
        })
        self.window._apply_broker_status_ui()

        self.window._open_settings_overlay()
        overlay = self.window._settings_overlay
        hidden = overlay._order_rows[0]["hidden"]

        self.assertTrue(hidden.isEnabled())
        self.assertTrue(hidden.isChecked())
        self.assertTrue(overlay.order_capability_note.isHidden())
        self.assertFalse(overlay.default_route_combo.isEditable())
        self.assertTrue(overlay.default_route_combo.isEnabled())
        self.assertEqual(
            [
                overlay.default_route_combo.itemText(index)
                for index in range(overlay.default_route_combo.count())
            ],
            ["SMART", "ARCA", "NYSE"],
        )
        hidden.setChecked(False)
        self.assertFalse(overlay._collect_order_rules()[0].hidden)

    def test_route_and_hide_are_disabled_and_normalized_for_tt(self):
        self.window._hotkey_config = replace(
            DEFAULT_HOTKEY_CONFIG,
            order_hotkeys=(
                OrderHotkeyRule(
                    id="ib-hidden",
                    key="Shift+F1",
                    order_type="market",
                    route="ARCA",
                    hidden=True,
                ),
            ),
        )
        self.session.broker_detail.update({
            "broker_type": "tastytrade",
            "order_options": {
                "default_route": "SMART",
                "routes": ["SMART"],
                "route_editable": False,
                "hidden_order": False,
            },
        })
        self.window._apply_broker_status_ui()

        self.window._open_settings_overlay()
        overlay = self.window._settings_overlay
        row = overlay._order_rows[0]
        hidden = row["hidden"]
        route = row["route"]
        self.assertFalse(hidden.isEnabled())
        self.assertFalse(hidden.isChecked())
        self.assertFalse(route.isEnabled())
        self.assertFalse(route.isEditable())
        self.assertEqual(route.currentText(), "SMART")
        configured = overlay._collect_order_rules()[0]
        self.assertEqual(configured.route, "SMART")
        self.assertFalse(configured.hidden)
        self.assertIn("SMART", overlay.order_capability_note.text())
        self.assertIn("HIDE 已关闭", overlay.order_capability_note.text())
        overlay.save_btn.click()
        self.app.processEvents()
        self.assertIsNone(self.window._settings_overlay)
        self.assertEqual(self.window._hotkey_config.order_hotkeys[0].route, "SMART")
        self.assertFalse(self.window._hotkey_config.order_hotkeys[0].hidden)

        self.window._prepare_configured_order(
            {
                "side": "buy",
                "order_type": "market",
                "tif": "Day",
                "route": "ARCA",
                "price_offset": 0.0,
                "hidden": True,
            },
            1,
        )

        slot = self.window.slots[1]
        self.assertEqual(slot.pending_action, "Buy to Open")
        self.assertEqual(slot.pending_route, "SMART")
        self.assertFalse(slot.pending_hidden)
        self.window._confirm_pending_order(1)
        self.assertEqual(
            self.session.orders[-1],
            ("AAPL", 100, 0.0, "Buy to Open", "market", "Day"),
        )
        self.assertEqual(
            self.session.order_details[-1],
            {"route": "SMART", "hidden": False},
        )

    def test_only_active_trade_panel_uses_yellow_glow(self):
        first_effect = self.window.slots[1].container.graphicsEffect()
        second_effect = self.window.slots[2].container.graphicsEffect()
        self.assertTrue(first_effect.isEnabled())
        self.assertFalse(second_effect.isEnabled())

        self.window._activate_panel(2)

        self.assertFalse(first_effect.isEnabled())
        self.assertTrue(second_effect.isEnabled())

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

    def test_settings_overlay_defaults_to_hotkeys_and_pauses_shortcuts(self):
        self.window._setup_shortcuts()
        self.assertIsNotNone(self.window._shortcut_controller)

        self.window.settings_btn.click()
        self.app.processEvents()

        self.assertIsNotNone(self.window._settings_overlay)
        self.assertIsNone(self.window._shortcut_controller)
        self.assertEqual(self.window._settings_overlay.current_tab_index(), 0)
        self.assertEqual(self.window._settings_overlay.current_hotkey_tab_index(), 0)
        self.assertEqual(
            [self.window._settings_overlay.hotkey_tabs.tabText(i) for i in range(3)],
            ["股数快捷键", "下单快捷键", "固定快捷键"],
        )
        quantity_rows = self.window._settings_overlay._quantity_rows
        self.assertTrue(all(row["quantity"].alignment() & Qt.AlignHCenter for row in quantity_rows))
        self.assertEqual(len(quantity_rows), 9)
        self.assertTrue(all(row["enabled"].isChecked() for row in quantity_rows))
        self.assertTrue(all(row["key"] is None for row in quantity_rows))
        self.assertEqual(
            [row["fixed_key"] for row in quantity_rows],
            [f"Num+{index}" for index in range(1, 10)],
        )
        self.assertEqual(self.window._settings_overlay.quantity_count_label.text(), "9 / 20")
        first_rule = self.window._settings_overlay._order_rows[0]
        self.assertTrue(first_rule["key"].alignment() & Qt.AlignHCenter)
        self.assertTrue(first_rule["offset"].alignment() & Qt.AlignHCenter)
        self.assertEqual(first_rule["side"].itemData(0, Qt.TextAlignmentRole), Qt.AlignCenter)
        fixed_keys = self.window._settings_overlay.findChildren(
            QLabel,
            "settingsKeyCell",
        )
        self.assertTrue(fixed_keys)
        self.assertTrue(all(label.alignment() & Qt.AlignHCenter for label in fixed_keys))
        self.assertEqual(
            self.window._settings_overlay.geometry(),
            self.window.centralWidget().rect(),
        )
        self.assertTrue(
            self.window._settings_overlay.version_label.text().startswith("v_0_")
        )
        self.assertEqual(
            self.window._settings_overlay.about_tab_btn.text(),
            "关于 client",
        )
        self.assertNotEqual(
            self.window._settings_overlay.hotkey_tab_btn.focusPolicy(),
            Qt.NoFocus,
        )
        self.assertNotEqual(
            self.window._settings_overlay.about_tab_btn.focusPolicy(),
            Qt.NoFocus,
        )
        about_name = self.window._settings_overlay.findChild(
            QLabel,
            "settingsAboutName",
        )
        self.assertIsNotNone(about_name)
        self.assertEqual(about_name.text(), "client")
        self.window.show()
        self.app.processEvents()
        self.window._settings_overlay.hotkey_tabs.setCurrentIndex(1)
        self.app.processEvents()
        self.assertEqual(
            self.window._settings_overlay.order_scroll.horizontalScrollBar().maximum(),
            0,
        )

        self.window._settings_overlay.about_tab_btn.click()
        self.assertEqual(self.window._settings_overlay.current_tab_index(), 1)

        self.window._close_settings_overlay()
        self.app.processEvents()

        self.assertIsNone(self.window._settings_overlay)
        self.assertIsNotNone(self.window._shortcut_controller)

    def test_quantity_hotkey_enabled_state_is_saved_and_applied(self):
        self.window._open_settings_overlay()
        overlay = self.window._settings_overlay
        first_row = next(
            row for row in overlay._quantity_rows
            if row["id"] == "quantity_default_1"
        )
        enabled = first_row["enabled"]
        enabled.setChecked(False)

        overlay.save_btn.click()
        self.app.processEvents()

        self.assertIsNone(self.window._settings_overlay)
        quantity = next(
            item for item in self.window._hotkey_config.quantity_hotkeys
            if item.key == "Num+1"
        )
        binding = next(
            item for item in self.window._hotkey_bindings
            if item.action == HotkeyAction.QUANTITY_SET and item.params.get("value") == 100
        )
        self.assertFalse(quantity.enabled)
        self.assertFalse(binding.enabled)
        loaded = load_hotkey_config(scope_id=self.session.config_scope)
        self.assertTrue(loaded.used_local_config)
        self.assertFalse(next(item for item in loaded.config.quantity_hotkeys if item.key == "Num+1").enabled)

    def test_quantity_rules_can_be_added_deleted_and_apply_to_active_panel(self):
        self.window._open_settings_overlay()
        overlay = self.window._settings_overlay
        overlay.add_quantity_btn.click()
        self.assertEqual(len(overlay._quantity_rows), 10)
        self.assertEqual(overlay.quantity_count_label.text(), "10 / 20")

        custom_row = overlay._quantity_rows[-1]
        self.assertIsNone(custom_row["fixed_key"])
        custom_row["key"].setText("F8")
        custom_row["quantity"].setValue(350)
        custom_row["enabled"].setChecked(True)
        overlay.save_btn.click()
        self.app.processEvents()

        self.assertIsNone(self.window._settings_overlay)
        custom = self.window._hotkey_config.quantity_hotkeys[-1]
        self.assertEqual(custom.key, "F8")
        self.assertEqual(custom.quantity, 350)
        self.assertTrue(custom.enabled)

        self.window._activate_panel(2)
        self.window.show()
        self.window.activateWindow()
        self.window.setFocus()
        self.app.processEvents()
        QTest.keyClick(self.window, Qt.Key_F8)
        self.app.processEvents()
        self.assertEqual(self.window.slots[2].qty_value(), 350)
        self.assertEqual(self.window.slots[1].qty_value(), 100)

        self.window._open_settings_overlay()
        overlay = self.window._settings_overlay
        custom_row = overlay._quantity_rows[-1]
        custom_row["action"].click()
        self.assertEqual(len(overlay._quantity_rows), 9)
        self.assertEqual(overlay.quantity_count_label.text(), "9 / 20")

    def test_quantity_rule_limit_and_conflict_are_enforced_in_settings(self):
        self.window._open_settings_overlay()
        overlay = self.window._settings_overlay
        for _ in range(11):
            overlay.add_quantity_btn.click()
        self.assertEqual(len(overlay._quantity_rows), 20)
        self.assertFalse(overlay.add_quantity_btn.isEnabled())
        overlay._add_quantity_rule()
        self.assertIn("最多 20 条", overlay.error_label.text())

        custom_row = overlay._quantity_rows[-1]
        custom_row["key"].setText("Space")
        custom_row["enabled"].setChecked(True)
        emitted = []
        overlay.save_requested.connect(emitted.append)
        overlay.save_btn.click()
        self.assertEqual(emitted, [])
        self.assertIn("快捷键冲突", overlay.error_label.text())

    def test_settings_conflict_stays_open_and_is_not_saved(self):
        self.window._open_settings_overlay()
        overlay = self.window._settings_overlay
        emitted = []
        overlay.save_requested.connect(emitted.append)
        first_rule = overlay._order_rows[0]
        first_rule["enabled"].setChecked(True)
        first_rule["key"].setText("Space")

        overlay.save_btn.click()

        self.assertIsNotNone(self.window._settings_overlay)
        self.assertFalse(self.window._settings_overlay.error_label.isHidden())
        self.assertIn("快捷键冲突", self.window._settings_overlay.error_label.text())
        self.assertEqual(emitted, [])
        self.assertFalse(os.path.exists(os.path.join(self._config_dir.name, "hotkey.json")))

    def test_empty_enabled_hotkey_shows_safe_chinese_message(self):
        self.window._open_settings_overlay()
        overlay = self.window._settings_overlay
        row = overlay._order_rows[0]
        row["enabled"].setChecked(True)
        row["key"].clear()

        overlay.save_btn.click()

        message = overlay.error_label.text()
        self.assertIn("已启用的快捷键不能为空", message)
        self.assertNotIn("order_rule_1", message)
        self.assertNotIn("enabled binding", message)

    def test_corrupt_runtime_hotkey_config_logs_warning_and_uses_defaults(self):
        path = hotkey_config_path(self.session.config_scope)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            fh.write("{bad json")

        self.window._setup_shortcuts()

        self.assertTrue(any("快捷键配置无效" in row[1] for row in self.window._log_rows))
        self.assertEqual(self.window._hotkey_bindings, client_main_window.HOTKEY_BINDINGS)

    def test_market_action_does_not_change_panel_order_type(self):
        self.window._submit_market_order("sell", 1)
        self.assertEqual(self.session.orders[-1], ("AAPL", 100, 0.0, "Sell to Close", "market", "Day"))
        self.assertEqual(self.window.slots[1].order_type.currentText(), "Limit")

    def test_force_disconnect_clears_session_and_returns_to_login_root(self):
        self.window._return_to_login_after_force_disconnect()

        self.assertTrue(self.session.local_auth_cleared)
        self.assertIsNone(self.window.session)
        self.assertFalse(self.window._main_ui_built)
        self.assertFalse(self.window._init_ready)
        self.assertTrue(hasattr(self.window, "_login_user_entry"))

        login_root = self.window.centralWidget()
        self.window._return_to_login_after_force_disconnect()
        self.assertIs(self.window.centralWidget(), login_root)

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

        self.assertEqual(self.session.orders, [("AAPL", 100, 185.30, "Sell to Close", "limit", "Day")])
        self.assertEqual(self.window.slots[1].pending_action, "")

    def test_pending_order_has_no_price_or_side_button_selection_frame(self):
        self.window.current_quote["AAPL"] = {
            "symbol": "AAPL",
            "bid": 185.10,
            "ask": 185.30,
            "received_monotonic": time.monotonic(),
            "connection_generation": self.window._se_generation,
        }

        self.window._prepare_limit_order("buy", 1, "ask")
        slot = self.window.slots[1]

        self.assertEqual(slot.pending_action, "Buy to Open")
        self.assertIsNone(slot.price.property("pendingSide"))
        self.assertIsNone(slot.buy.property("pending"))
        self.assertIsNone(slot.sell.property("pending"))
        self.assertNotIn('QLineEdit[pendingSide="buy"]', client_main_window.theme.APP_QSS)
        self.assertNotIn('QPushButton#buyButton[pending="true"]', client_main_window.theme.APP_QSS)

    def test_limit_preparation_reads_latest_raw_quote_before_ui_flush(self):
        self.window.current_quote["AAPL"] = {
            "symbol": "AAPL",
            "bid": 180.00,
            "ask": 180.10,
            "last": 180.05,
            "received_monotonic": time.monotonic(),
        }
        self.window._ts_connection._cache_quote_message({
            "type": "QUOTE_DATA",
            "payload": {
                "symbol": "AAPL",
                "bid": 190.20,
                "ask": 190.30,
                "last": 190.25,
            },
        })

        self.window._prepare_limit_order("buy", 1, "ask")

        self.assertEqual(self.window.slots[1].price.text(), "190.20")

    def test_stale_quote_is_still_displayed_for_limit_preparation(self):
        self.window.current_quote["AAPL"] = {
            "symbol": "AAPL",
            "bid": 185.10,
            "ask": 185.30,
            "last": 185.20,
            "received_monotonic": time.monotonic() - 30,
        }
        self.window._prepare_limit_order("buy", 1, "ask")
        self.assertEqual(self.window.slots[1].price.text(), "185.10")
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

    def test_position_requires_double_click_and_loads_active_panel(self):
        self.window._update_positions([{"symbol": "MU", "qty": 100}])
        requested = []
        self.window._quote_subscriptions.request_symbol = (
            lambda panel_id, symbol: requested.append((panel_id, symbol)) or 1
        )
        self.window._activate_panel(2)
        index = self.window.positions_model.index(0, 0)

        self.window.positions_table.clicked.emit(index)
        self.assertEqual(self.window.slots[2].symbol_text(), "")
        self.assertEqual(requested, [])

        self.window.positions_table.doubleClicked.emit(index)
        self.assertEqual(self.window._active_panel_id, 2)
        self.assertEqual(self.window.slots[2].symbol_text(), "MU")
        self.assertEqual(self.window.slots[1].symbol_text(), "AAPL")
        self.assertEqual(requested, [(2, "MU")])

    def test_numpad_quantity_does_not_capture_main_keyboard_digits(self):
        self.window._setup_shortcuts()
        self.window.show()
        self.window.activateWindow()
        field = self.window.slots[1].qty_label
        field.setFocus()
        field.setText("")
        self.app.processEvents()

        QTest.keyClick(field, Qt.Key_3)
        self.assertEqual(field.text(), "3")

        QTest.keyClick(field, Qt.Key_4, Qt.KeypadModifier)
        self.app.processEvents()
        self.assertEqual(field.text(), "400")

    def test_fixed_space_and_arrow_shortcuts_follow_active_panel_context(self):
        self.window._setup_shortcuts()
        self.window.show()
        self.window.activateWindow()
        self.app.processEvents()

        QTest.keyClick(self.window, Qt.Key_Space)
        self.assertEqual(self.window._active_panel_id, 2)

        slot = self.window.slots[2]
        slot.price.setText("10.00")
        slot.price.setFocus()
        self.window.activateWindow()
        self.app.processEvents()
        self.assertIs(QApplication.activeWindow(), self.window)
        self.assertIs(QApplication.focusWidget(), slot.price)
        QTest.keyClick(slot.price, Qt.Key_Up)
        self.assertEqual(slot.price.text(), "10.05")
        QTest.keyClick(slot.price, Qt.Key_Left)
        self.assertEqual(slot.price.text(), "10.04")

    def test_configured_non_ioc_uses_bid_and_still_requires_enter(self):
        self.window.current_quote["AAPL"] = {
            "symbol": "AAPL",
            "bid": 185.10,
            "ask": 185.30,
            "last": 185.20,
            "received_monotonic": time.monotonic(),
        }
        self.window._prepare_configured_order(
            {
                "side": "buy",
                "order_type": "limit",
                "tif": "GTC",
                "route": "DEFAULT",
                "price_offset": 0.05,
                "hidden": False,
            },
            1,
        )
        self.assertEqual(self.window.slots[1].price.text(), "185.15")
        self.assertEqual(self.session.orders, [])
        self.window._confirm_pending_order(1)
        self.assertEqual(self.session.orders[-1], ("AAPL", 100, 185.15, "Buy to Open", "limit", "GTC"))

        self.window._action_limiter.reset()
        self.window._prepare_configured_order(
            {
                "side": "sell",
                "order_type": "market",
                "tif": "IOC",
                "route": "DEFAULT",
                "price_offset": 0,
                "hidden": False,
            },
            1,
        )
        slot = self.window.slots[1]
        self.assertEqual(self.session.orders[-1], ("AAPL", 100, 0.0, "Sell to Close", "market", "IOC"))
        self.assertEqual(slot.pending_action, "")
        self.assertFalse(slot.price.isEnabled())

    def test_configured_limit_ioc_submits_buy_ask_and_sell_bid_immediately(self):
        self.window.current_quote["AAPL"] = {
            "symbol": "AAPL",
            "bid": 185.10,
            "ask": 185.30,
            "received_monotonic": time.monotonic(),
            "connection_generation": self.window._se_generation,
        }

        self.window._prepare_configured_order({
            "side": "buy",
            "order_type": "limit",
            "tif": "IOC",
            "route": "DEFAULT",
            "price_offset": 0.05,
        }, 1)
        self.assertEqual(
            self.session.orders[-1],
            ("AAPL", 100, 185.35, "Buy to Open", "limit", "IOC"),
        )
        self.assertEqual(self.window.slots[1].pending_action, "")

        self.window._action_limiter.reset()
        self.window._prepare_configured_order({
            "side": "sell",
            "order_type": "limit",
            "tif": "IOC",
            "route": "DEFAULT",
            "price_offset": -0.05,
        }, 1)
        self.assertEqual(
            self.session.orders[-1],
            ("AAPL", 100, 185.05, "Sell to Close", "limit", "IOC"),
        )
        self.assertEqual(self.window.slots[1].pending_action, "")

    def test_configured_limit_sell_uses_ask_and_waits_for_confirmation(self):
        self.window.current_quote["AAPL"] = {
            "symbol": "AAPL",
            "bid": 185.10,
            "ask": 185.30,
            "received_monotonic": time.monotonic(),
        }

        self.window._prepare_configured_order({
            "side": "sell",
            "order_type": "limit",
            "tif": "Day",
            "route": "DEFAULT",
        }, 1)
        self.assertEqual(self.window.slots[1].price.text(), "185.30")
        self.assertEqual(self.session.orders, [])
        self.window._confirm_pending_order(1)
        self.assertEqual(
            self.session.orders[-1],
            ("AAPL", 100, 185.30, "Sell to Close", "limit", "Day"),
        )

    def test_limit_ioc_accepts_quote_within_five_seconds(self):
        self.window.current_quote["AAPL"] = {
            "symbol": "AAPL",
            "bid": 185.10,
            "ask": 185.30,
            "received_monotonic": time.monotonic() - 4.0,
            "connection_generation": self.window._se_generation,
        }

        self.window._prepare_configured_order({
            "side": "buy",
            "order_type": "limit",
            "tif": "IOC",
            "route": "DEFAULT",
        }, 1)
        self.assertEqual(
            self.session.orders[-1],
            ("AAPL", 100, 185.30, "Buy to Open", "limit", "IOC"),
        )

    def test_limit_ioc_refreshes_quote_older_than_five_seconds(self):
        self.window.current_quote["AAPL"] = {
            "symbol": "AAPL",
            "bid": 185.10,
            "ask": 185.30,
            "received_monotonic": time.monotonic() - 5.1,
            "connection_generation": self.window._se_generation,
        }

        self.session.refresh_quote_result = (
            True,
            {
                "symbol": "AAPL",
                "bid": 186.10,
                "ask": 186.30,
                "last": 186.20,
            },
            "行情刷新成功",
        )
        self.window._prepare_configured_order({
            "side": "buy",
            "order_type": "limit",
            "tif": "IOC",
            "route": "DEFAULT",
        }, 1)
        self.assertEqual(self.session.refresh_quote_calls[0][1], "ask")
        self.assertEqual(self.session.orders[-1], ("AAPL", 100, 186.30, "Buy to Open", "limit", "IOC"))

    def test_limit_ioc_does_not_submit_when_refresh_times_out(self):
        self.window.current_quote["AAPL"] = {
            "symbol": "AAPL",
            "bid": 185.10,
            "ask": 185.30,
            "received_monotonic": time.monotonic() - 5.1,
            "connection_generation": self.window._se_generation,
        }

        self.window._prepare_configured_order({
            "side": "buy",
            "order_type": "limit",
            "tif": "IOC",
            "route": "DEFAULT",
        }, 1)
        self.assertEqual(self.session.orders, [])
        self.assertEqual(self.window.slots[1].pending_action, "")
        self.assertEqual(self.window.slots[1].price.text(), "185.30")

    def test_limit_ioc_does_not_submit_quote_from_previous_connection(self):
        self.window.current_quote["AAPL"] = {
            "symbol": "AAPL",
            "bid": 185.10,
            "ask": 185.30,
            "received_monotonic": time.monotonic(),
            "connection_generation": self.window._se_generation - 1,
        }

        self.window._prepare_configured_order({
            "side": "buy",
            "order_type": "limit",
            "tif": "IOC",
            "route": "DEFAULT",
        }, 1)
        self.assertEqual(self.session.orders, [])

    def test_market_ioc_does_not_require_quote_cache(self):
        self.window.current_quote.clear()

        self.window._prepare_configured_order({
            "side": "buy",
            "order_type": "market",
            "tif": "IOC",
            "route": "DEFAULT",
        }, 1)
        self.assertEqual(
            self.session.orders[-1],
            ("AAPL", 100, 0.0, "Buy to Open", "market", "IOC"),
        )

    def test_ioc_does_not_submit_without_declared_tif_support(self):
        self.session.broker_detail["order_options"]["supported_tifs"] = ["Day", "GTC"]

        self.window._prepare_configured_order({
            "side": "buy",
            "order_type": "market",
            "tif": "IOC",
            "route": "DEFAULT",
        }, 1)
        self.assertEqual(self.session.orders, [])
        self.assertEqual(self.window.slots[1].pending_action, "")

        self.window._place_order(
            "Buy to Open",
            1,
            order_type_override="market",
            price_override=0.0,
            tif_override="IOC",
        )
        self.assertEqual(self.session.orders, [])

    def test_legacy_symbol_options_fall_back_to_global_tif_support(self):
        self.session.symbol_options["AAPL"] = {
            "default_route": "SMART",
            "routes": ["SMART"],
            "route_editable": False,
            "hidden_order": False,
        }

        self.assertTrue(self.window._broker_supports_tif("IOC", "AAPL"))

    def test_cancel_market_pending_restores_disabled_price_state(self):
        self.window._prepare_configured_order(
            {
                "side": "buy",
                "order_type": "market",
                "tif": "Day",
                "route": "DEFAULT",
            },
            1,
        )
        self.window._cancel_pending_order(1)
        slot = self.window.slots[1]
        self.assertEqual(slot.pending_action, "")
        self.assertEqual(slot.price.text(), "Market")
        self.assertFalse(slot.price.isEnabled())

    def test_symbol_validation_gates_trading_without_redundant_status_query(self):
        slot = self.window.slots[1]
        slot.set_trade_enabled(True)
        slot.symbol.setCurrentText("MSFT")
        self.window._mark_symbol_pending(1, "MSFT")
        self.assertFalse(slot.buy.isEnabled())
        self.assertFalse(slot.sell.isEnabled())

        self.window._handle_symbol_confirm_result(1, "MSFT", True, "ok", self.window._se_generation)

        self.assertEqual(slot.current_symbol, "MSFT")
        self.assertTrue(slot.buy.isEnabled())
        self.assertTrue(slot.sell.isEnabled())
        self.assertEqual(self.session.broker_status_queries, 0)

    def test_quote_sync_retries_symbol_that_was_pending_during_reconnect(self):
        slot = self.window.slots[1]
        slot.symbol.setCurrentText("MSFT")
        self.window._mark_symbol_pending(1, "MSFT")
        requested = []
        reconciled = []
        self.window._quote_subscriptions.request_symbol = (
            lambda panel_id, symbol: requested.append((panel_id, symbol))
        )
        self.window._quote_subscriptions.reconcile = (
            lambda force_resubscribe=False: reconciled.append(force_resubscribe)
        )

        self.window._sync_quote_subscriptions_async(force_resubscribe=True)

        self.assertEqual(requested, [(1, "MSFT")])
        self.assertEqual(reconciled, [True])

    def test_tt_route_stays_locked_in_main_and_settings(self):
        self.session.broker_detail.update({
            "broker_type": "tastytrade",
            "order_options": {
                "default_route": "SMART",
                "routes": ["SMART"],
                "route_editable": False,
                "hidden_order": False,
            },
        })
        self.window._apply_broker_status_ui()
        route = self.window.slots[1].route
        self.assertEqual(route.currentText(), "SMART")
        self.assertEqual(route.focusPolicy(), Qt.NoFocus)
        self.assertTrue(route.testAttribute(Qt.WA_TransparentForMouseEvents))
        self.assertEqual(self.window._resolve_route_value("ARCA"), "SMART")

        self.window._open_settings_overlay()
        overlay = self.window._settings_overlay
        self.assertFalse(overlay.default_route_combo.isEnabled())
        self.assertFalse(overlay.default_route_combo.isEditable())
        self.assertFalse(overlay._order_rows[0]["hidden"].isEnabled())
        self.assertFalse(overlay._order_rows[0]["route"].isEnabled())
        self.assertEqual(overlay._collect_config().default_route, "SMART")
        self.assertIn("SMART", overlay.order_capability_note.text())
        self.assertIn("HIDE 已关闭", overlay.order_capability_note.text())

    def test_four_order_tabs_switch_modes_and_keep_one_selected(self):
        for mode, button in self.window._order_mode_buttons.items():
            button.click()
            QTest.qWait(100)
            self.assertEqual(self.window._order_refresh.order_mode, mode)
            self.assertTrue(button.property("selected"))
            self.assertEqual(
                [bool(item.property("selected")) for item in self.window._order_mode_buttons.values()].count(True),
                1,
            )
        self.assertEqual(
            [mode for mode, _force in self.session.order_queries[-4:]],
            ["live", "filled", "inactive", "all"],
        )

    def test_batch_cancel_result_uses_reusable_weak_notification(self):
        tips = []
        self.window._show_weak_tip = lambda message, level="inf", duration_ms=3000: tips.append((message, level))
        self.window._batch_canceling_symbols.add("AAPL")

        self.window._handle_batch_cancel_result(
            "AAPL",
            total=2,
            success=2,
            failures=[],
            limiter_token="",
            generation=self.window._se_generation,
        )

        self.assertEqual(tips, [("AAPL 已撤销 2 笔活动订单", "ok")])

    def test_warn_console_log_uses_weak_notification(self):
        tips = []
        self.window._show_weak_tip = lambda message, level="inf", duration_ms=3000: tips.append((message, level))

        self.window._append_log("风险警告", "warn")
        self.window._append_log("普通信息", "inf")
        self.window._append_log("操作成功", "ok")
        self.window._append_log("操作失败", "err")

        self.assertEqual(tips, [("风险警告", "warn")])

    def test_auth_expiry_warning_is_emitted_once(self):
        tips = []
        self.session.auth_remaining = 30
        self.window._show_weak_tip = (
            lambda message, level="inf", duration_ms=3000: tips.append((message, level))
        )

        self.window._check_auth_lifetime()
        self.window._check_auth_lifetime()

        self.assertEqual(
            tips,
            [("认证将在1分钟后到期，到期后需要重新登录", "warn")],
        )

    def test_auth_expiry_returns_to_login_with_centered_yellow_notice(self):
        expired_session = self.session
        expired_session.auth_remaining = 0

        self.window._check_auth_lifetime()

        self.assertIsNone(self.window.session)
        self.assertTrue(expired_session.local_auth_cleared)
        self.assertFalse(self.window._main_ui_built)
        self.assertEqual(
            self.window._auth_notice_label.text(),
            "认证已过期，请重新认证登录",
        )
        self.assertTrue(self.window._auth_notice_label.alignment() & Qt.AlignHCenter)
        self.assertIn(
            client_main_window.theme.ACCENT_YELLOW,
            self.window._auth_notice_label.styleSheet(),
        )

    def test_temporary_sm_health_failure_keeps_authenticated_trading_session(self):
        self.window._on_server_disconnect()

        self.assertTrue(self.session.connected)
        self.assertTrue(self.window._se_connected)

    def test_weak_notification_deduplicates_same_active_message_and_level(self):
        initial_count = len(self.window._toast_widgets)

        self.window._show_weak_tip("重复警告", "warn")
        self.window._show_weak_tip("重复警告", "warn")
        self.assertEqual(len(self.window._toast_widgets), initial_count + 1)

        self.window._show_weak_tip("重复警告", "err")
        self.assertEqual(len(self.window._toast_widgets), initial_count + 2)

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

    def test_temporary_order_hotkey_uses_right_active_panel(self):
        binding = HotkeyBinding(
            "test_market_buy_right",
            "F8",
            HotkeyAction.ORDER_MARKET,
            HotkeyContext.TRADE_PANEL,
            True,
            {"side": "buy"},
        )
        right = self.window.slots[2]
        right.symbol.setCurrentText("MU")
        right.current_symbol = "MU"
        right.qty_label.setText("275")
        original_bindings = client_main_window.HOTKEY_BINDINGS
        client_main_window.HOTKEY_BINDINGS = (binding,)
        try:
            self.window._setup_shortcuts()
            self.window.show()
            self.window.activateWindow()
            right.symbol.lineEdit().setFocus()
            self.app.processEvents()

            QTest.keyClick(right.symbol.lineEdit(), Qt.Key_F8)
            self.app.processEvents()
        finally:
            self.window._teardown_shortcuts()
            client_main_window.HOTKEY_BINDINGS = original_bindings

        self.assertEqual(self.window._active_panel_id, 2)
        self.assertEqual(
            self.session.orders,
            [("MU", 275, 0.0, "Buy to Open", "market", "Day")],
        )


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
