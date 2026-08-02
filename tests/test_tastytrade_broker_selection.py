from __future__ import annotations

import unittest
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from Trader_Server.api import tastytrade as tt_module
from Trader_Server.api.tastytrade import TastytradeBroker


class _DummyClient:
    async def aclose(self) -> None:
        return None


class _DummySession:
    def __init__(self, secret: str, token: str):
        self.secret = secret
        self.token = token
        self.session_token = "active"
        self._client = _DummyClient()


class _DummyQuoteStreamer:
    async def get_event(self, *_args):
        await asyncio.sleep(60)

    async def __aexit__(self, *_args) -> None:
        return None


def _record(account_number: str, *, authority: str = "owner", closed: bool = False) -> dict:
    return {
        "account": SimpleNamespace(
            account_number=account_number,
            nickname=f"Account {account_number}",
            account_type_name="Individual",
            is_closed=closed,
        ),
        "authority_level": authority,
    }


class TastytradeBrokerSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_tt_order_options_are_smart_only_and_reject_ib_only_fields(self):
        broker = TastytradeBroker()
        broker._get_fresh = AsyncMock(return_value=(object(), object()))
        options = broker.status_detail()["order_options"]
        self.assertEqual(options["routes"], ["SMART"])
        self.assertFalse(options["route_editable"])
        self.assertFalse(options["hidden_order"])
        symbol_options = await broker.get_symbol_order_options("aapl")
        self.assertEqual(symbol_options["symbol"], "AAPL")
        self.assertEqual(symbol_options["routes"], ["SMART"])
        self.assertTrue(symbol_options["routes_validated"])

        base_order = {
            "symbol": "AAPL",
            "qty": 1,
            "price": 190,
            "action": "Buy to Open",
            "order_type": "limit",
            "tif": "Day",
        }
        with self.assertRaisesRegex(ValueError, "SMART"):
            await broker.place_order(dict(base_order, route="ARCA"))
        with self.assertRaisesRegex(ValueError, "hidden"):
            await broker.place_order(dict(base_order, route="SMART", hidden=True))

    async def test_explicit_missing_account_fails_without_fallback(self):
        broker = TastytradeBroker()
        broker._get_account_records = AsyncMock(return_value=[_record("A-1")])
        with patch("Trader_Server.api.tastytrade.Session", _DummySession):
            connected = await broker.connect({
                "secret": "secret",
                "token": "token",
                "account_number": "MISSING",
            })

        self.assertFalse(connected)
        self.assertEqual(broker.get_connection_error()["code"], "BROKER_ACCOUNT_NOT_FOUND")

    async def test_blank_account_selects_first_open(self):
        broker = TastytradeBroker()
        broker._get_account_records = AsyncMock(return_value=[
            _record("CLOSED", closed=True),
            _record("OPEN"),
        ])
        with patch("Trader_Server.api.tastytrade.Session", _DummySession):
            connected = await broker.connect({"secret": "secret", "token": "token"})

        self.assertTrue(connected)
        self.assertEqual(broker.status_detail()["account"]["account_number"], "OPEN")
        await broker.disconnect()

    async def test_read_only_account_disables_trade_capabilities(self):
        broker = TastytradeBroker()
        broker._get_account_records = AsyncMock(return_value=[
            _record("READ", authority="read-only"),
        ])
        with patch("Trader_Server.api.tastytrade.Session", _DummySession):
            connected = await broker.connect({"secret": "secret", "token": "token"})

        self.assertTrue(connected)
        capabilities = broker.effective_capabilities()
        self.assertFalse(capabilities["orders"])
        self.assertFalse(capabilities["cancel_order"])
        self.assertTrue(capabilities["quotes"])
        self.assertTrue(capabilities["positions"])
        self.assertTrue(capabilities["order_query"])
        await broker.disconnect()

    async def test_stale_quote_stream_rebuilds_and_resubscribes_existing_symbols(self):
        broker = TastytradeBroker()
        broker._connected = True
        broker._session = _DummySession("secret", "token")
        broker._account = SimpleNamespace(account_number="OPEN")
        broker._quote_streamer = _DummyQuoteStreamer()
        broker._quote_stream_started_at = time.monotonic() - tt_module.QUOTE_STREAM_MAX_AGE_SECONDS - 1
        broker._subscribed_symbols = {"AAPL"}
        broker._get_fresh = AsyncMock(return_value=(broker._session, broker._account))
        broker._create_quote_streamer = AsyncMock(return_value=_DummyQuoteStreamer())
        broker._streamer_subscribe = AsyncMock()

        with patch.object(tt_module, "_SDK_AVAILABLE", True), patch.object(tt_module, "_DX_AVAILABLE", True), patch.object(tt_module, "DXQuote", object):
            await broker.subscribe_quotes(["AAPL"])

        broker._streamer_subscribe.assert_awaited_once_with(["AAPL"])
        self.assertEqual(broker._subscribed_symbols, {"AAPL"})
        await broker._stop_quote_stream()

    async def test_stale_quote_stream_rebuild_includes_new_symbol(self):
        broker = TastytradeBroker()
        broker._connected = True
        broker._session = _DummySession("secret", "token")
        broker._account = SimpleNamespace(account_number="OPEN")
        broker._quote_streamer = _DummyQuoteStreamer()
        broker._quote_stream_started_at = time.monotonic() - tt_module.QUOTE_STREAM_MAX_AGE_SECONDS - 1
        broker._subscribed_symbols = {"MSFT"}
        broker._get_fresh = AsyncMock(return_value=(broker._session, broker._account))
        broker._create_quote_streamer = AsyncMock(return_value=_DummyQuoteStreamer())
        broker._streamer_subscribe = AsyncMock()

        with patch.object(tt_module, "_SDK_AVAILABLE", True), patch.object(tt_module, "_DX_AVAILABLE", True), patch.object(tt_module, "DXQuote", object):
            await broker.subscribe_quotes(["AAPL"])

        broker._streamer_subscribe.assert_awaited_once_with(["AAPL", "MSFT"])
        self.assertEqual(broker._subscribed_symbols, {"AAPL", "MSFT"})
        await broker._stop_quote_stream()


if __name__ == "__main__":
    unittest.main()
