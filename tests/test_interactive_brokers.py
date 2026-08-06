import datetime
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from Client.services import trading_session as trading_session_module
from Client.services.trading_session import TradingSession
from Trader_Server.api.factory import BrokerFactory
from Trader_Server.api.interactive_brokers import (
    IBBroker,
    IBRequestError,
    _action_from_order_ref,
    _normalize_status,
    _tif_from_ib,
    _tif_to_ib,
)
from Trader_Server.services import config_sync, ib_registration_validation


class InteractiveBrokersAdapterTests(unittest.TestCase):
    @staticmethod
    def _ready_broker(app, account_id="U123"):
        broker = IBBroker()
        broker._ib_app = app
        broker._connected = True
        broker._account_id = account_id
        broker._managed_accounts = [account_id]
        broker._account_verified = True
        return broker

    def test_factory_creates_interactive_brokers(self):
        broker = BrokerFactory.create("interactive_brokers")
        self.assertIsInstance(broker, IBBroker)
        self.assertEqual(broker.broker_type, "interactive_brokers")

    def test_gateway_configuration_is_fixed(self):
        broker = IBBroker()
        valid = broker.normalize_credentials(
            {"host": "127.0.0.1", "port": 4001, "client_id": 1, "account_id": "U1"}
        )
        self.assertEqual(broker._validate_gateway_config(valid), (True, ""))
        invalid = dict(valid, port=4002)
        self.assertFalse(broker._validate_gateway_config(invalid)[0])

    def test_missing_account_disables_account_capabilities(self):
        broker = IBBroker()
        capabilities = broker.effective_capabilities()
        self.assertTrue(capabilities["quotes"])
        self.assertFalse(capabilities["orders"])
        self.assertFalse(capabilities["cancel_order"])
        self.assertFalse(capabilities["positions"])
        self.assertFalse(capabilities["order_query"])

    def test_order_mapping_matches_existing_client_contract(self):
        self.assertEqual(_tif_to_ib("Day"), ("DAY", False))
        self.assertEqual(_tif_to_ib("GTC_EXT"), ("GTC", True))
        self.assertEqual(_tif_from_ib("DAY", True), "EXT")
        self.assertEqual(_action_from_order_ref("EC:BTO"), "Buy to Open")
        self.assertEqual(_action_from_order_ref("EC:STC"), "Sell to Close")
        self.assertEqual(_normalize_status("Submitted", 0, 10), "Live")
        self.assertEqual(_normalize_status("Submitted", 3, 7), "Partial")
        self.assertEqual(_normalize_status("Inactive", 0, 10), "Rejected")

    def test_serialized_order_is_consumable_by_current_client(self):
        broker = IBBroker()
        order = SimpleNamespace(
            orderId=41,
            permId=99,
            account="U123",
            orderRef="EC:BTO",
            totalQuantity=10,
            orderType="LMT",
            lmtPrice=189.25,
            tif="DAY",
            outsideRth=True,
        )
        contract = SimpleNamespace(symbol="AAPL", secType="STK", currency="USD")
        item = {
            "order_id": 41,
            "perm_id": 99,
            "order": order,
            "contract": contract,
            "status": "Filled",
            "filled": 10,
            "remaining": 0,
            "updated_at": "2026-07-29T12:00:00+00:00",
        }
        fills = {
            ("order", 41): [
                {"fill_price": "189.25", "quantity": "10", "filled_at": "2026-07-29T12:00:00+00:00"}
            ]
        }
        serialized = broker._serialize_order(item, fills)
        self.assertEqual(serialized["id"], "41")
        self.assertEqual(serialized["action"], "Buy to Open")
        self.assertEqual(serialized["type"], "LIMIT")
        self.assertEqual(serialized["tif"], "EXT")
        self.assertEqual(serialized["status"], "Filled")
        self.assertEqual(serialized["legs"][0]["fills"][0]["quantity"], "10")

    def test_client_status_hides_detailed_ib_connection_reason(self):
        original = (
            config_sync._current_broker,
            config_sync._current_broker_type,
            dict(config_sync._last_connect_error),
        )
        try:
            config_sync._current_broker = None
            config_sync._current_broker_type = "interactive_brokers"
            config_sync._last_connect_error = {
                "code": "IB_API_HANDSHAKE_TIMEOUT",
                "message": "Gateway not running or API disabled",
                "retryable": True,
            }
            private = config_sync.get_broker_status()
            public = config_sync.get_broker_status(public=True)
            self.assertIn("Gateway", private["error"]["message"])
            self.assertEqual(public["error"]["message"], "交易服务暂不可用")
            self.assertNotIn("Gateway", public["error"]["message"])
        finally:
            config_sync._current_broker = original[0]
            config_sync._current_broker_type = original[1]
            config_sync._last_connect_error = original[2]


class InteractiveBrokersRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_routes_come_from_contract_details_and_hidden_reaches_ib_order(self):
        confirmed_contract = SimpleNamespace(
            conId=265598,
            symbol="AAPL",
            secType="STK",
            exchange="SMART",
            primaryExchange="NASDAQ",
            currency="USD",
        )

        class FakeApp:
            def __init__(self):
                self.managed_accounts = ["U123"]
                self.submissions = []

            @staticmethod
            def isConnected():
                return True

            async def request_contract_details(self, _contract):
                return [
                    SimpleNamespace(
                        contract=confirmed_contract,
                        validExchanges="SMART,ARCA,NYSE",
                    )
                ]

            async def subscribe_market_data(self, _symbol, _contract):
                return None

            @staticmethod
            def allocate_order_id():
                return 901

            async def place_order_and_wait(self, order_id, contract, order):
                self.submissions.append((order_id, contract, order))
                return {"status": "Live"}

        app = FakeApp()
        broker = InteractiveBrokersAdapterTests._ready_broker(app)
        self.assertEqual(broker.status_detail()["order_options"]["routes"], ["SMART"])
        self.assertIn("IOC", broker.status_detail()["order_options"]["supported_tifs"])

        await broker.subscribe_quotes(["AAPL"])
        routes = broker.status_detail()["order_options"]["routes"]
        result = await broker.place_order(
            {
                "symbol": "AAPL",
                "qty": 1,
                "price": 190,
                "action": "Buy to Open",
                "order_type": "limit",
                "tif": "Day",
                "route": "ARCA",
                "hidden": True,
            }
        )

        self.assertEqual(routes, ["SMART", "ARCA", "NYSE", "NASDAQ"])
        self.assertEqual(result["order_id"], "901")
        submitted_contract = app.submissions[0][1]
        submitted_order = app.submissions[0][2]
        self.assertEqual(submitted_contract.exchange, "ARCA")
        self.assertTrue(submitted_order.hidden)

    async def test_symbol_order_options_keep_routes_isolated_by_symbol(self):
        contracts = {
            "AAPL": SimpleNamespace(
                conId=265598,
                symbol="AAPL",
                secType="STK",
                exchange="SMART",
                primaryExchange="NASDAQ",
                currency="USD",
            ),
            "MU": SimpleNamespace(
                conId=116438,
                symbol="MU",
                secType="STK",
                exchange="SMART",
                primaryExchange="NYSE",
                currency="USD",
            ),
        }

        class FakeApp:
            @staticmethod
            def isConnected():
                return True

            async def request_contract_details(self, contract):
                symbol = str(contract.symbol).upper()
                routes = "SMART,ARCA" if symbol == "AAPL" else "SMART,NYSE"
                return [SimpleNamespace(contract=contracts[symbol], validExchanges=routes)]

        broker = InteractiveBrokersAdapterTests._ready_broker(FakeApp())
        aapl = await broker.get_symbol_order_options("AAPL")
        mu = await broker.get_symbol_order_options("MU")

        self.assertEqual(aapl["routes"], ["SMART", "ARCA", "NASDAQ"])
        self.assertEqual(mu["routes"], ["SMART", "NYSE"])
        self.assertNotIn("ARCA", mu["routes"])
        self.assertTrue(aapl["routes_validated"])
        self.assertIn("IOC", aapl["supported_tifs"])

    async def test_accounts_summary_and_quotes_use_confirmed_us_stock_contract(self):
        confirmed_contract = SimpleNamespace(
            conId=265598,
            symbol="AAPL",
            secType="STK",
            exchange="SMART",
            currency="USD",
        )

        class FakeApp:
            def __init__(self):
                self.managed_accounts = ["U123", "U456"]
                self.contract_requests = []
                self.subscriptions = []
                self.unsubscriptions = []

            @staticmethod
            def isConnected():
                return True

            async def request_account_summary(self):
                return {
                    "U123": {"NetLiquidation": {"value": "100000", "currency": "USD"}},
                    "U456": {"NetLiquidation": {"value": "200000", "currency": "USD"}},
                }

            async def request_contract_details(self, contract):
                self.contract_requests.append(contract)
                return [SimpleNamespace(contract=confirmed_contract)]

            async def subscribe_market_data(self, symbol, contract):
                self.subscriptions.append((symbol, contract))

            def unsubscribe_market_data(self, symbol):
                self.unsubscriptions.append(symbol)

        app = FakeApp()
        broker = InteractiveBrokersAdapterTests._ready_broker(app)
        broker._managed_accounts = ["U123", "U456"]

        accounts = await broker.get_accounts()
        summary = await broker.get_account_summary("U456")
        await broker.subscribe_quotes(["aapl"])
        await broker.unsubscribe_quotes(["AAPL"])

        self.assertEqual(accounts, [{"account_id": "U123"}, {"account_id": "U456"}])
        self.assertEqual(summary["account_id"], "U456")
        self.assertEqual(summary["values"]["NetLiquidation"]["value"], "200000")
        requested_contract = app.contract_requests[0]
        self.assertEqual(requested_contract.symbol, "AAPL")
        self.assertEqual(requested_contract.secType, "STK")
        self.assertEqual(requested_contract.exchange, "SMART")
        self.assertEqual(requested_contract.currency, "USD")
        self.assertEqual(app.subscriptions, [("AAPL", confirmed_contract)])
        self.assertEqual(app.unsubscriptions, ["AAPL"])

    async def test_ambiguous_us_stock_symbol_is_rejected_before_subscription(self):
        contracts = [
            SimpleNamespace(conId=1, symbol="ABC", secType="STK", currency="USD"),
            SimpleNamespace(conId=2, symbol="ABC", secType="STK", currency="USD"),
        ]

        class FakeApp:
            @staticmethod
            def isConnected():
                return True

            async def request_contract_details(self, contract):
                return [SimpleNamespace(contract=item) for item in contracts]

            async def subscribe_market_data(self, symbol, contract):
                raise AssertionError("ambiguous contract must not be subscribed")

        broker = InteractiveBrokersAdapterTests._ready_broker(FakeApp())
        with self.assertRaises(IBRequestError) as raised:
            await broker.subscribe_quotes(["ABC"])
        self.assertEqual(raised.exception.code, "IB_CONTRACT_AMBIGUOUS")

    async def test_positions_are_filtered_to_selected_account_and_enriched(self):
        aapl = SimpleNamespace(symbol="AAPL", secType="STK", currency="USD")
        msft = SimpleNamespace(symbol="MSFT", secType="STK", currency="USD")
        option = SimpleNamespace(symbol="AAPL", secType="OPT", currency="USD")

        class FakeApp:
            @staticmethod
            def isConnected():
                return True

            async def request_positions(self):
                return [
                    {"account": "U123", "contract": aapl, "position": 5, "average_cost": 100},
                    {"account": "U999", "contract": msft, "position": 8, "average_cost": 200},
                    {"account": "U123", "contract": option, "position": 1, "average_cost": 10},
                ]

            async def request_portfolio(self, account_id):
                self.requested_account = account_id
                return {
                    "101": {
                        "contract": aapl,
                        "average_cost": 101.25,
                        "market_price": 110.5,
                        "unrealized_pnl": 46.25,
                        "realized_pnl": 12.5,
                    }
                }

        app = FakeApp()
        broker = InteractiveBrokersAdapterTests._ready_broker(app)
        positions = await broker.get_positions({"symbols": ["aapl", "msft"]})

        self.assertEqual(app.requested_account, "U123")
        self.assertEqual(len(positions), 1)
        self.assertEqual(
            positions[0],
            {
                "symbol": "AAPL",
                "quantity": 5.0,
                "direction": "Long",
                "average_open_price": 101.25,
                "close_price": 110.5,
                "unrealized": 46.25,
                "realized_today": 12.5,
            },
        )

    async def test_place_order_builds_official_ib_order_for_limit_and_market(self):
        contract = SimpleNamespace(symbol="AAPL", secType="STK", currency="USD")

        class FakeApp:
            def __init__(self):
                self.next_order_id = 700
                self.submissions = []

            @staticmethod
            def isConnected():
                return True

            def allocate_order_id(self):
                order_id = self.next_order_id
                self.next_order_id += 1
                return order_id

            async def place_order_and_wait(self, order_id, submitted_contract, order):
                self.submissions.append((order_id, submitted_contract, order))
                return {"status": "Live"}

        app = FakeApp()
        broker = InteractiveBrokersAdapterTests._ready_broker(app)
        broker._contract_cache["AAPL"] = contract

        limit_result = await broker.place_order(
            {
                "symbol": "aapl",
                "qty": 3,
                "price": 189.25,
                "action": "Sell to Close",
                "order_type": "limit",
                "tif": "GTC_EXT",
            }
        )
        market_result = await broker.place_order(
            {
                "symbol": "AAPL",
                "qty": 2,
                "price": 0,
                "action": "Buy to Open",
                "order_type": "market",
                "tif": "IOC",
            }
        )
        limit_ioc_result = await broker.place_order(
            {
                "symbol": "AAPL",
                "qty": 4,
                "price": 190.75,
                "action": "Buy to Open",
                "order_type": "limit",
                "tif": "IOC",
            }
        )

        self.assertEqual(limit_result["order_id"], "700")
        self.assertEqual(market_result["order_id"], "701")
        self.assertEqual(limit_ioc_result["order_id"], "702")
        limit_order = app.submissions[0][2]
        self.assertEqual(limit_order.__class__.__module__, "ibapi.order")
        self.assertEqual(limit_order.action, "SELL")
        self.assertEqual(limit_order.totalQuantity, 3)
        self.assertEqual(limit_order.orderType, "LMT")
        self.assertEqual(limit_order.lmtPrice, 189.25)
        self.assertEqual(limit_order.tif, "GTC")
        self.assertTrue(limit_order.outsideRth)
        self.assertEqual(limit_order.account, "U123")
        self.assertEqual(limit_order.orderRef, "EC:STC")
        self.assertTrue(limit_order.transmit)

        market_order = app.submissions[1][2]
        self.assertEqual(market_order.action, "BUY")
        self.assertEqual(market_order.orderType, "MKT")
        self.assertEqual(market_order.tif, "IOC")
        self.assertFalse(market_order.outsideRth)
        self.assertEqual(market_order.orderRef, "EC:BTO")

        limit_ioc_order = app.submissions[2][2]
        self.assertEqual(limit_ioc_order.action, "BUY")
        self.assertEqual(limit_ioc_order.totalQuantity, 4)
        self.assertEqual(limit_ioc_order.orderType, "LMT")
        self.assertEqual(limit_ioc_order.lmtPrice, 190.75)
        self.assertEqual(limit_ioc_order.tif, "IOC")
        self.assertFalse(limit_ioc_order.outsideRth)
        self.assertEqual(limit_ioc_order.orderRef, "EC:BTO")

    async def test_order_query_merges_sources_and_filters_unowned_orders(self):
        stock = SimpleNamespace(symbol="AAPL", secType="STK", currency="USD")
        option = SimpleNamespace(symbol="AAPL", secType="OPT", currency="USD")

        def order_item(order_id, status, account="U123", order_ref="EC:BTO", contract=stock, perm_id=0):
            return {
                "order_id": order_id,
                "perm_id": perm_id,
                "contract": contract,
                "order": SimpleNamespace(
                    orderId=order_id,
                    permId=perm_id,
                    account=account,
                    orderRef=order_ref,
                    totalQuantity=2,
                    orderType="LMT",
                    lmtPrice=190.0,
                    tif="DAY",
                    outsideRth=False,
                ),
                "status": status,
                "filled": 2 if status == "Filled" else 0,
                "remaining": 0 if status == "Filled" else 2,
                "updated_at": f"2026-07-29T12:0{order_id}:00+00:00",
            }

        live_order = order_item(1, "Submitted", perm_id=101)
        filled_order = order_item(2, "Filled", order_ref="EC:STC", perm_id=102)
        foreign_ref = order_item(3, "Submitted", order_ref="MANUAL")
        foreign_account = order_item(4, "Submitted", account="U999")
        non_stock = order_item(5, "Submitted", contract=option)

        class FakeApp:
            def __init__(self):
                self.known_orders = {item["order_id"]: item for item in (live_order, foreign_ref)}
                self.completed_requests = 0
                self.execution_requests = []

            @staticmethod
            def isConnected():
                return True

            async def request_open_orders(self):
                return [live_order, foreign_ref, foreign_account, non_stock]

            async def request_completed_orders(self):
                self.completed_requests += 1
                return [filled_order]

            async def request_executions(self, account_id):
                self.execution_requests.append(account_id)
                return [
                    {
                        "contract": stock,
                        "execution": SimpleNamespace(
                            acctNumber="U123",
                            orderId=2,
                            permId=102,
                            price=191.5,
                            shares=2,
                            time="20260729 12:00:00 UTC",
                        ),
                    },
                    {
                        "contract": stock,
                        "execution": SimpleNamespace(
                            acctNumber="U999",
                            orderId=2,
                            permId=102,
                            price=1,
                            shares=99,
                            time="20260729 12:00:00 UTC",
                        ),
                    },
                ]

        app = FakeApp()
        broker = InteractiveBrokersAdapterTests._ready_broker(app)

        all_orders = await broker.get_orders("all")
        live_orders = await broker.get_orders("live")

        self.assertEqual({item["id"] for item in all_orders}, {"1", "2"})
        filled = next(item for item in all_orders if item["id"] == "2")
        self.assertEqual(filled["action"], "Sell to Close")
        self.assertEqual(filled["legs"][0]["fills"][0]["fill_price"], "191.5")
        self.assertEqual(filled["legs"][0]["fills"][0]["quantity"], "2")
        self.assertEqual([item["id"] for item in live_orders], ["1"])
        self.assertEqual(app.completed_requests, 1)
        self.assertEqual(app.execution_requests, ["U123"])

    async def test_cancel_order_only_allows_orders_owned_by_this_ts(self):
        def order_item(order_id, account="U123", order_ref="EC:BTO"):
            return {
                "order_id": order_id,
                "order": SimpleNamespace(account=account, orderRef=order_ref),
            }

        class FakeApp:
            def __init__(self):
                self.known_orders = {
                    11: order_item(11),
                    12: order_item(12, order_ref="MANUAL"),
                }
                self.cancelled = []
                self.open_order_requests = 0

            @staticmethod
            def isConnected():
                return True

            async def request_open_orders(self):
                self.open_order_requests += 1
                return list(self.known_orders.values())

            async def cancel_order_and_wait(self, order_id):
                self.cancelled.append(order_id)
                return {"status": "Cancelled"}

        app = FakeApp()
        broker = InteractiveBrokersAdapterTests._ready_broker(app)

        result = await broker.cancel_order("11")
        self.assertEqual(result, {"success": True, "order_id": "11", "status": "Cancelled"})
        with self.assertRaises(PermissionError):
            await broker.cancel_order("12")

        self.assertEqual(app.cancelled, [11])
        self.assertEqual(app.open_order_requests, 1)


class InteractiveBrokersClientCompatibilityTests(unittest.TestCase):
    @staticmethod
    def _ib_session():
        session = TradingSession(SimpleNamespace())
        session.connected = True
        session.bind_se_client(SimpleNamespace(is_connected=True))
        session.set_broker_detail(
            {
                "broker_type": "interactive_brokers",
                "connected": True,
                "capabilities": {
                    "quotes": True,
                    "orders": True,
                    "cancel_order": True,
                    "positions": True,
                    "order_query": True,
                },
            }
        )
        return session

    def test_client_parses_ib_orders_through_existing_order_query_contract(self):
        session = self._ib_session()
        requests = []

        def fake_request(msg_type, payload, timeout=10.0):
            requests.append((msg_type, payload, timeout))
            return {
                "payload": {
                    "success": True,
                    "orders": [
                        {
                            "id": "701",
                            "symbol": "AAPL",
                            "action": "Sell to Close",
                            "qty": "2",
                            "price": "191.50",
                            "status": "Live",
                            "type": "LIMIT",
                            "tif": "GTC_EXT",
                        }
                    ],
                }
            }

        session._request_se = fake_request
        orders = session.get_orders("live")

        self.assertEqual(requests[0][0:2], ("ORDER_QUERY", {"mode": "live"}))
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["id"], "701")
        self.assertEqual(orders[0]["action"], "SELL")
        self.assertEqual(orders[0]["raw_status"], "Live")
        self.assertEqual(orders[0]["tif"], "GTC_EXT")

    def test_quote_ack_caches_symbol_order_options_and_reconnect_clears_them(self):
        session = self._ib_session()
        session._request_se = lambda *_args, **_kwargs: {
            "payload": {
                "success": True,
                "message": "ok",
                "symbol_order_options": {
                    "aapl": {
                        "default_route": "smart",
                        "routes": ["smart", "arca", "ARCA"],
                        "route_editable": True,
                        "hidden_order": True,
                        "routes_validated": True,
                        "supported_tifs": ["Day", "IOC"],
                    }
                },
            }
        }

        ok, _message = session.subscribe_quotes(["AAPL"])

        self.assertTrue(ok)
        self.assertEqual(session.symbol_order_options("AAPL")["routes"], ["SMART", "ARCA"])
        self.assertTrue(session.symbol_order_options("AAPL")["routes_validated"])
        self.assertEqual(session.symbol_order_options("AAPL")["supported_tifs"], ["Day", "IOC"])
        session.bind_se_client(SimpleNamespace(is_connected=True))
        self.assertEqual(session.symbol_order_options("AAPL"), {})

    def test_legacy_quote_ack_without_order_options_remains_compatible(self):
        session = self._ib_session()
        session._request_se = lambda *_args, **_kwargs: {
            "payload": {"success": True, "message": "ok"}
        }

        ok, message = session.subscribe_quotes(["AAPL"])

        self.assertTrue(ok)
        self.assertEqual(message, "ok")
        self.assertEqual(session.symbol_order_options("AAPL"), {})

    def test_client_four_order_modes_share_protocol_and_filter_current_day(self):
        session = self._ib_session()
        today = datetime.datetime.combine(
            datetime.datetime.now(session._ET).date(),
            datetime.time(12, 0),
            tzinfo=session._ET,
        ).isoformat()
        yesterday = (
            datetime.datetime.fromisoformat(today) - datetime.timedelta(days=1)
        ).isoformat()
        requested_modes = []

        def order(status, order_id, updated_at=today):
            return {
                "id": order_id,
                "symbol": "AAPL",
                "action": "Buy to Open",
                "qty": "1",
                "price": "190.00",
                "status": status,
                "type": "LIMIT",
                "tif": "Day",
                "updated_at": updated_at,
            }

        current_orders = [
            order("Live", "live"),
            order("Filled", "filled"),
            order("Cancelled", "cancelled"),
            order("Rejected", "rejected"),
            order("Expired", "expired"),
        ]

        def fake_request(_msg_type, payload, timeout=10.0):
            requested_modes.append(payload["mode"])
            orders = [current_orders[0]] if payload["mode"] == "live" else current_orders + [order("Filled", "old", yesterday)]
            return {"payload": {"success": True, "orders": orders}}

        session._request_se = fake_request
        live = session.query_orders("live", force=True).data
        filled = session.query_orders("filled", force=True).data
        inactive = session.query_orders("inactive", force=True).data
        all_orders = session.query_orders("all", force=True).data

        self.assertEqual([item["id"] for item in live], ["live"])
        self.assertEqual([item["id"] for item in filled], ["filled"])
        self.assertEqual(
            [item["id"] for item in inactive],
            ["cancelled", "rejected", "expired"],
        )
        self.assertEqual([item["id"] for item in all_orders], ["live", "filled", "cancelled", "rejected", "expired"])
        self.assertEqual(requested_modes, ["live", "all", "all", "all"])

    def test_client_order_day_filter_survives_china_midnight(self):
        session = self._ib_session()
        china_tz = datetime.timezone(datetime.timedelta(hours=8))
        fixed_china_time = datetime.datetime(2026, 7, 7, 2, 0, tzinfo=china_tz)

        class FixedDateTime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_china_time.astimezone(tz) if tz else fixed_china_time.replace(tzinfo=None)

        orders = [
            {
                "id": "same-us-session",
                "symbol": "AAPL",
                "action": "Buy to Open",
                "qty": "1",
                "price": "190.00",
                "status": "Filled",
                "type": "LIMIT",
                "tif": "Day",
                "updated_at": "2026-07-06T15:00:00-04:00",
            },
            {
                "id": "previous-us-day",
                "symbol": "AAPL",
                "action": "Buy to Open",
                "qty": "1",
                "price": "189.00",
                "status": "Filled",
                "type": "LIMIT",
                "tif": "Day",
                "updated_at": "2026-07-05T15:00:00-04:00",
            },
        ]
        session._request_se = lambda *_args, **_kwargs: {
            "payload": {"success": True, "orders": orders}
        }

        with patch.object(trading_session_module.datetime, "datetime", FixedDateTime):
            result = session.query_orders("all", force=True)

        self.assertTrue(result.success)
        self.assertEqual([item["id"] for item in result.data], ["same-us-session"])

    def test_client_today_activity_consumes_ib_positions_and_fills_without_broker_branch(self):
        session = self._ib_session()
        session_time = datetime.datetime.combine(
            datetime.datetime.now(session._ET).date(),
            datetime.time(12, 0),
            tzinfo=session._ET,
        ).isoformat()

        def fake_request(msg_type, payload, timeout=10.0):
            if msg_type == "POSITION_QUERY":
                return {
                    "payload": {
                        "success": True,
                        "positions": [
                            {
                                "symbol": "AAPL",
                                "quantity": 4,
                                "direction": "Long",
                                "average_open_price": 100,
                                "close_price": 110,
                                "realized_today": 0,
                            }
                        ],
                    }
                }
            if msg_type == "ORDER_QUERY":
                return {
                    "payload": {
                        "success": True,
                        "orders": [
                            {
                                "status": "Filled",
                                "updated_at": session_time,
                                "legs": [
                                    {
                                        "symbol": "AAPL",
                                        "action": "Buy to Open",
                                        "quantity": "2",
                                        "fills": [{"fill_price": "105", "quantity": "2"}],
                                    }
                                ],
                            }
                        ],
                    }
                }
            self.fail(f"Unexpected message type: {msg_type}")

        session._request_se = fake_request
        activity = session.get_today_activity()

        self.assertEqual(len(activity), 1)
        self.assertEqual(activity[0]["symbol"], "AAPL")
        self.assertEqual(activity[0]["qty"], 4.0)
        self.assertEqual(activity[0]["unrealized"], 40.0)
        self.assertEqual(activity[0]["qty_bot"], 2.0)
        self.assertEqual(activity[0]["exes"], 1)


class InteractiveBrokersConfigSyncTests(unittest.IsolatedAsyncioTestCase):
    _GLOBAL_NAMES = (
        "_current_broker",
        "_current_broker_type",
        "_local_config_version",
        "_auto_reconnect_task",
        "_connect_failure_count",
        "_next_connect_retry_at",
        "_auto_retry_paused",
        "_auto_retry_pause_reason",
        "_last_connect_error",
    )

    def setUp(self):
        self._globals = {name: getattr(config_sync, name) for name in self._GLOBAL_NAMES}
        self._state = (config_sync.state.token, config_sync.state.server_id)
        config_sync.state.token = "node-token"
        config_sync.state.server_id = "ib-node"
        config_sync._current_broker = None
        config_sync._current_broker_type = ""
        config_sync._local_config_version = 0
        config_sync._auto_reconnect_task = None
        config_sync._reset_connect_retry_state()

    def tearDown(self):
        config_sync.state.token, config_sync.state.server_id = self._state
        for name, value in self._globals.items():
            setattr(config_sync, name, value)

    async def test_config_sync_creates_normalizes_and_connects_ib_adapter(self):
        class FakeBroker:
            broker_type = "interactive_brokers"

            def __init__(self):
                self._connected = False
                self.normalized = None
                self.connected_with = None
                self.quote_callback = None

            def normalize_credentials(self, credentials):
                self.normalized = dict(credentials)
                return dict(credentials)

            async def connect(self, credentials):
                self.connected_with = dict(credentials)
                self._connected = True
                return True

            def set_quote_callback(self, callback):
                self.quote_callback = callback

            @staticmethod
            def effective_capabilities():
                return {
                    "quotes": True,
                    "orders": True,
                    "cancel_order": True,
                    "positions": True,
                    "order_query": True,
                }

            @staticmethod
            def status_detail():
                return {"account": {"account_id": "U123", "managed": True}}

        credentials = {"host": "127.0.0.1", "port": 4001, "client_id": 1, "account_id": "U123"}
        broker = FakeBroker()
        with (
            patch.object(
                config_sync,
                "_pull_config_from_sm",
                AsyncMock(
                    return_value={
                        "broker_type": "interactive_brokers",
                        "credentials": credentials,
                        "config_version": 7,
                    }
                ),
            ),
            patch.object(config_sync.BrokerFactory, "create", return_value=broker) as create,
            patch.object(config_sync, "_restore_quote_subscriptions", AsyncMock()) as restore,
            patch.object(config_sync, "_start_auto_reconnect") as reconnect,
            patch.object(config_sync, "_broadcast_status") as broadcast,
        ):
            initialized = await config_sync.init_broker()

        self.assertTrue(initialized)
        create.assert_called_once_with("interactive_brokers")
        self.assertEqual(broker.normalized, credentials)
        self.assertEqual(broker.connected_with, credentials)
        self.assertIsNotNone(broker.quote_callback)
        restore.assert_awaited_once_with(broker)
        reconnect.assert_called_once_with()
        broadcast.assert_called_once_with("interactive_brokers", "connected")
        status = config_sync.get_broker_status(public=True)
        self.assertTrue(status["connected"])
        self.assertNotIn("config_version", status)
        self.assertNotIn("account_id", status["account"])
        self.assertNotIn("broker_type", status)
        self.assertTrue(status["capabilities"]["orders"])

    async def test_config_sync_preserves_private_error_and_shortens_client_error(self):
        class FailingBroker:
            broker_type = "interactive_brokers"

            def normalize_credentials(self, credentials):
                return dict(credentials)

            async def connect(self, credentials):
                return False

            @staticmethod
            def get_connection_error():
                return {
                    "code": "IB_API_HANDSHAKE_TIMEOUT",
                    "message": "IB Gateway API is disabled",
                    "retryable": True,
                }

        with (
            patch.object(
                config_sync,
                "_pull_config_from_sm",
                AsyncMock(
                    return_value={
                        "broker_type": "interactive_brokers",
                        "credentials": {
                            "host": "127.0.0.1",
                            "port": 4001,
                            "client_id": 1,
                            "account_id": "U123",
                        },
                        "config_version": 8,
                    }
                ),
            ),
            patch.object(config_sync.BrokerFactory, "create", return_value=FailingBroker()),
            patch.object(config_sync, "_broadcast_status"),
        ):
            initialized = await config_sync.init_broker()

        self.assertFalse(initialized)
        private = config_sync.get_broker_status()
        public = config_sync.get_broker_status(public=True)
        self.assertEqual(private["error"]["code"], "IB_API_HANDSHAKE_TIMEOUT")
        self.assertIn("Gateway API", private["error"]["message"])
        self.assertEqual(public["error"]["code"], "TRADING_SERVICE_UNAVAILABLE")
        self.assertEqual(public["error"]["message"], "交易服务暂不可用")


class InteractiveBrokersRegistrationWorkerTests(unittest.TestCase):
    def test_pending_worker_polls_sm_validates_locally_and_returns_accounts(self):
        pending = {
            "request_id": "req_ib_worker",
            "validation_secret": "w" * 40,
            "broker_type": "interactive_brokers",
            "manager_url": "https://sm.scjrdomain.com",
        }
        calls = []

        def fake_json_request(url, secret, payload, timeout):
            calls.append((url, secret, payload, timeout))
            if url.endswith("/nodes/registration-validation/poll"):
                return {
                    "ok": True,
                    "job": {
                        "job_id": "ibv_job_1",
                        "credentials": {"host": "127.0.0.1", "port": 4001, "client_id": 1},
                    },
                }
            return {"ok": True}

        validation_result = {
            "ok": True,
            "accounts": [{"account_id": "U100"}, {"account_id": "U200"}],
        }
        with (
            patch.object(ib_registration_validation, "load_register_state", side_effect=[pending, None]),
            patch.object(ib_registration_validation, "_json_request", side_effect=fake_json_request),
            patch.object(
                ib_registration_validation,
                "_validate_local_gateway",
                AsyncMock(return_value=validation_result),
            ) as validate,
        ):
            ib_registration_validation._worker_loop()

        validate.assert_awaited_once_with()
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0][0].endswith("/nodes/registration-validation/poll"))
        self.assertTrue(calls[1][0].endswith("/nodes/registration-validation/result"))
        self.assertEqual(calls[0][1], pending["validation_secret"])
        self.assertEqual(calls[1][1], pending["validation_secret"])
        self.assertEqual(calls[1][2]["request_id"], pending["request_id"])
        self.assertEqual(calls[1][2]["job_id"], "ibv_job_1")
        self.assertEqual(calls[1][2]["result"], validation_result)


if __name__ == "__main__":
    unittest.main()
