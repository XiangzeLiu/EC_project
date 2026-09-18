from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from Trader_Server import config
from Trader_Server import main
from Trader_Server.services import caddy_manager
from Trader_Server.services import config_sync
from Trader_Server.services import finance_reporter
from Trader_Server.services import ib_registration_validation
from Trader_Server.services import registration


class _SseResponse:
    def __init__(self, payload: dict):
        encoded = json.dumps(payload).encode("utf-8")
        self._lines = iter((b"data: " + encoded + b"\n", b"\n"))

    def readline(self):
        return next(self._lines, b"")

    def close(self):
        return None


class TraderRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._state = {
            "server_id": main.state.server_id,
            "token": main.state.token,
            "manager_url": main.state.manager_url,
            "node_name": main.state.node_name,
            "region": main.state.region,
            "public_ip": main.state.public_ip,
            "assigned_domain": main.state.assigned_domain,
            "public_endpoint": main.state.public_endpoint,
            "status": main.state.status,
        }
        self._approval_task = main._approval_broker_task
        main._approval_broker_task = asyncio.create_task(asyncio.sleep(3600))
        main.state.manager_url = "https://manager.invalid"
        main.state.server_id = ""
        main.state.token = ""
        main.state.status = "registering"

    async def asyncTearDown(self):
        if main._approval_broker_task is not None and main._approval_broker_task is not self._approval_task:
            main._approval_broker_task.cancel()
            await asyncio.gather(main._approval_broker_task, return_exceptions=True)
        main._approval_broker_task = self._approval_task
        for name, value in self._state.items():
            setattr(main.state, name, value)

    async def test_approval_is_persisted_before_gui_receives_success(self):
        payload = {
            "approved": True,
            "server_id": "node_test_1234",
            "token": "token_test",
            "public_ip": "203.0.113.10",
        }
        saved = []

        def save_config(data):
            saved.append(dict(data))
            return True

        with patch.object(main, "urlopen", return_value=_SseResponse(payload)), patch.object(
            config, "save_config", side_effect=save_config
        ), patch.object(config, "clear_register_state"):
            response = await main.api_await_approval("req_test")
            first_chunk = await anext(response.body_iterator.__aiter__())

        self.assertIn(b'"approved": true', first_chunk)
        self.assertEqual(len(saved), 1)
        self.assertEqual(main.state.server_id, "node_test_1234")
        self.assertEqual(main.state.token, "token_test")
        self.assertEqual(main.state.status, "approved")

    async def test_approval_is_not_forwarded_when_credentials_cannot_be_saved(self):
        payload = {
            "approved": True,
            "server_id": "node_test_1234",
            "token": "token_test",
        }

        with patch.object(main, "urlopen", return_value=_SseResponse(payload)), patch.object(
            config, "save_config", return_value=False
        ), patch.object(config, "clear_register_state"):
            response = await main.api_await_approval("req_test")
            chunks = [chunk async for chunk in response.body_iterator]

        body = b"".join(chunks)
        self.assertNotIn(b'"approved": true', body)
        self.assertIn(b"could not be persisted", body)
        self.assertEqual(main.state.server_id, "")
        self.assertEqual(main.state.token, "")
        self.assertEqual(main.state.status, "registering")

class LocalServerStartupTests(unittest.TestCase):
    def test_ready_server_allows_gui_startup(self):
        server = type("Server", (), {"started": True, "should_exit": False})()
        thread = type("Thread", (), {"is_alive": lambda self: True})()

        self.assertTrue(main._wait_for_local_server(server, thread, timeout=0.01))
        self.assertFalse(server.should_exit)

    def test_dead_server_blocks_gui_startup(self):
        server = type("Server", (), {"started": False, "should_exit": False})()
        thread = type("Thread", (), {"is_alive": lambda self: False})()

        self.assertFalse(main._wait_for_local_server(server, thread, timeout=0.01))
        self.assertFalse(server.should_exit)


class StartupRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._assigned_domain = main.state.assigned_domain
        self._startup_error = main._startup_error
        self._heartbeat = main._heartbeat
        self._startup_recovery_task = main._startup_recovery_task
        main.state.assigned_domain = "ts01.example.com"

    async def asyncTearDown(self):
        if main._startup_recovery_task is not None and main._startup_recovery_task is not self._startup_recovery_task:
            main._startup_recovery_task.cancel()
            await asyncio.gather(main._startup_recovery_task, return_exceptions=True)
        main._startup_recovery_task = self._startup_recovery_task
        main._heartbeat = self._heartbeat
        main.state.assigned_domain = self._assigned_domain
        main._startup_error = self._startup_error

    async def test_caddy_recovery_failure_does_not_fail_local_startup(self):
        logger = main.logging.getLogger("test.startup-recovery")

        with patch.object(main, "test_connection", return_value=(True, "ok")), patch.object(
            caddy_manager,
            "configure_and_start_caddy",
            return_value={"ok": False, "reason": "reload failed"},
        ):
            await main._restore_registered_node_services(logger)

    async def test_startup_error_preserves_real_exception(self):
        with patch.object(main, "_initialize_runtime", side_effect=RuntimeError("invalid runtime")):
            with self.assertRaisesRegex(RuntimeError, "invalid runtime"):
                await main.on_startup()

        self.assertEqual(main._startup_error, "RuntimeError: invalid runtime")

    async def test_successful_startup_uses_initialized_listener(self):
        with patch.object(
            main,
            "_initialize_runtime",
            new=AsyncMock(return_value=("127.0.0.1", 8900)),
        ), patch("builtins.print") as print_mock:
            await main.on_startup()

        self.assertEqual(main._startup_error, "")
        print_mock.assert_any_call("  监听地址   : %s:%d" % ("127.0.0.1", 8900))

    async def test_external_recovery_does_not_block_local_initialization(self):
        recovery_started = asyncio.Event()
        recovery_release = asyncio.Event()

        async def blocked_recovery(_logger):
            recovery_started.set()
            await recovery_release.wait()

        class Heartbeat:
            async def start(self):
                return None

        with patch.object(main, "init_logging"), patch.object(
            main, "production_config_errors", return_value=[]
        ), patch.object(main, "tls_diagnostics", return_value={
            "certifi_cafile": "ca.pem",
            "custom_cafile": "",
            "hostname_check": True,
            "certificate_required": True,
        }), patch.object(main, "check_and_restore_session", return_value=True), patch.object(
            main, "HeartbeatSender", return_value=Heartbeat()
        ), patch.object(main, "_restore_registered_node_services", side_effect=blocked_recovery), patch.object(
            ib_registration_validation, "start_pending_ib_validation_worker"
        ), patch.object(config_sync, "start_config_event_listener"), patch.object(
            finance_reporter, "start_finance_reporter"
        ):
            await asyncio.wait_for(main._initialize_runtime(), timeout=0.2)
            await asyncio.wait_for(recovery_started.wait(), timeout=0.2)

        self.assertIsNotNone(main._startup_recovery_task)
        self.assertFalse(main._startup_recovery_task.done())
        recovery_release.set()
        await main._startup_recovery_task


class RegistrationRestoreTests(unittest.TestCase):
    def test_valid_credentials_clear_stale_registration_request(self):
        saved = {
            "server_id": registration.state.server_id,
            "token": registration.state.token,
            "manager_url": registration.state.manager_url,
            "node_name": registration.state.node_name,
            "region": registration.state.region,
            "public_ip": registration.state.public_ip,
            "assigned_domain": registration.state.assigned_domain,
            "public_endpoint": registration.state.public_endpoint,
            "status": registration.state.status,
        }
        config_data = {
            "server_id": "node_test",
            "token": "token_test",
            "manager_url": "https://manager.invalid",
        }
        try:
            with patch.object(config, "load_config", return_value=config_data), patch.object(
                config, "is_registered", return_value=True
            ), patch.object(registration, "clear_register_state") as clear_state:
                self.assertTrue(registration.check_and_restore_session())

            clear_state.assert_called_once_with()
        finally:
            for name, value in saved.items():
                setattr(registration.state, name, value)

if __name__ == "__main__":
    unittest.main()
