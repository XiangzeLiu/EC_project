from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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


if __name__ == "__main__":
    unittest.main()
