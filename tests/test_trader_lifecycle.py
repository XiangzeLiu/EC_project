from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import anyio

import Trader_Server.api.tastytrade as tastytrade_module
import Trader_Server.services.config_sync as config_sync
from Trader_Server.api.tastytrade import TastytradeBroker


class _ScopeStreamer:
    instances: list["_ScopeStreamer"] = []

    def __init__(self, *_args, **_kwargs):
        self.enter_task = None
        self.exit_task = None
        self._task_group = None
        self.subscriptions = []
        self.__class__.instances.append(self)

    async def __aenter__(self):
        self.enter_task = asyncio.current_task()
        self._task_group = anyio.create_task_group()
        await self._task_group.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.exit_task = asyncio.current_task()
        return await self._task_group.__aexit__(exc_type, exc_val, exc_tb)

    async def subscribe_accounts(self, accounts):
        self.subscriptions.append(accounts)

    def listen(self, *_args):
        async def _events():
            while True:
                await asyncio.sleep(3600)
                yield None

        return _events()

    async def subscribe(self, *_args):
        return None

    async def get_event(self, *_args):
        await asyncio.sleep(3600)


class TraderLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.broker = TastytradeBroker()
        self.broker._connected = True
        self.broker._session = type("Session", (), {"session_token": "active"})()
        self.broker._account = type("Account", (), {"account_number": "TEST"})()

    async def test_account_stream_enters_and_exits_on_owner_task(self):
        class DummyPlacedOrder:
            pass

        class DummyPosition:
            pass

        _ScopeStreamer.instances.clear()
        with patch.object(tastytrade_module, "AlertStreamer", _ScopeStreamer), patch.object(
            tastytrade_module, "PlacedOrder", DummyPlacedOrder
        ), patch.object(tastytrade_module, "CurrentPosition", DummyPosition):
            await self.broker.start_account_events()
            await self.broker.stop_account_events()

        streamer = _ScopeStreamer.instances[0]
        self.assertIs(streamer.enter_task, streamer.exit_task)
        self.assertEqual(self.broker._account_event_tasks, [])
        self.assertIsNone(self.broker._account_streamer)

    async def test_quote_stream_enters_and_exits_on_owner_task(self):
        _ScopeStreamer.instances.clear()
        with patch.object(tastytrade_module, "DXLinkStreamer", _ScopeStreamer), patch.object(
            tastytrade_module, "DXQuote", object
        ), patch.object(tastytrade_module, "_DX_AVAILABLE", True):
            await self.broker.subscribe_quotes(["AAPL"])
            await self.broker._stop_quote_stream()

        streamer = _ScopeStreamer.instances[0]
        self.assertIs(streamer.enter_task, streamer.exit_task)
        self.assertIsNone(self.broker._quote_streamer)

    async def test_reconnect_stops_existing_stream_owners_before_session_reset(self):
        account_stop = AsyncMock()
        quote_stop = AsyncMock()
        self.broker._account_stream_owner_task = object()
        self.broker._quote_owner_task = object()
        with patch.object(self.broker, "stop_account_events", account_stop), patch.object(
            self.broker, "_stop_quote_stream", quote_stop
        ), patch.object(tastytrade_module, "_SDK_AVAILABLE", False):
            result = await self.broker.connect({})

        self.assertFalse(result)
        account_stop.assert_awaited_once()
        quote_stop.assert_awaited_once()

    async def test_approval_and_heartbeat_reload_create_one_broker(self):
        class FakeBroker:
            broker_type = "tt"
            _connected = False

            def normalize_credentials(self, credentials):
                return credentials

            async def connect(self, credentials):
                await asyncio.sleep(0.05)
                self._connected = True
                return True

            async def is_connected(self):
                return self._connected

            async def disconnect(self):
                self._connected = False

            def set_quote_callback(self, callback):
                self.quote_callback = callback

            def set_order_event_callback(self, callback):
                self.order_callback = callback

            def set_position_event_callback(self, callback):
                self.position_callback = callback

            def effective_capabilities(self):
                return {}

        created = []

        def create(_broker_type):
            created.append(FakeBroker())
            return created[-1]

        original = {
            "broker": config_sync._current_broker,
            "broker_type": config_sync._current_broker_type,
            "version": config_sync._local_config_version,
            "lock": config_sync._broker_lifecycle_lock,
            "retry_task": config_sync._auto_reconnect_task,
            "last_reload": config_sync._last_reload_trigger_ts,
        }
        try:
            config_sync._current_broker = None
            config_sync._current_broker_type = ""
            config_sync._local_config_version = 0
            config_sync._broker_lifecycle_lock = asyncio.Lock()
            config_sync._auto_reconnect_task = None
            config_sync._last_reload_trigger_ts = 0
            config_sync.state.token = "token"
            config_sync.state.server_id = "server"

            with patch.object(config_sync.BrokerFactory, "create", side_effect=create), patch.object(
                config_sync, "_pull_config_from_sm",
                AsyncMock(return_value={"broker_type": "tt", "credentials": {}, "config_version": 1}),
            ), patch.object(config_sync, "_bind_broker_events", AsyncMock()), patch.object(
                config_sync, "_restore_quote_subscriptions", AsyncMock()
            ), patch.object(config_sync, "_start_auto_reconnect"), patch.object(
                config_sync, "_broadcast_status"
            ):
                await asyncio.gather(
                    config_sync.init_broker(),
                    config_sync.check_and_reload(1),
                )

            self.assertEqual(len(created), 1)
            self.assertEqual(config_sync._local_config_version, 1)
        finally:
            config_sync._current_broker = original["broker"]
            config_sync._current_broker_type = original["broker_type"]
            config_sync._local_config_version = original["version"]
            config_sync._broker_lifecycle_lock = original["lock"]
            config_sync._auto_reconnect_task = original["retry_task"]
            config_sync._last_reload_trigger_ts = original["last_reload"]


if __name__ == "__main__":
    unittest.main()
