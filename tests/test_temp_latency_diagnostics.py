from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from Client.network.temp_latency_diagnostics import (
    ClientLatencyDiagnostics,
    record_active_ui_latency,
)
from Client.network.ts_websocket import TSWebSocketClient
from Trader_Server.network import ws_server


class ClientTemporaryLatencyDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_ping_log_contains_timing_but_no_connection_secrets(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(
            os.environ,
            {"APPDATA": folder, "SC_CLIENT_TEMP_LATENCY_DIAGNOSTICS": "1"},
            clear=False,
        ):
            diagnostic = ClientLatencyDiagnostics()
            diagnostic.start()
            client = TSWebSocketClient(token="do-not-log", ws_url="wss://private.example/ws")
            client._latency_diagnostic = diagnostic
            client._send_lock = asyncio.Lock()

            class FakeSocket:
                sent: list[str] = []

                async def send(self, text: str) -> None:
                    self.sent.append(text)

            socket = FakeSocket()
            self.assertTrue(await client._send_latency_ping(socket))
            record_active_ui_latency(321)
            diagnostic.close()

            files = list((Path(folder) / "SC Client" / "diagnostics").glob("client_latency_*.jsonl"))
            self.assertEqual(len(files), 1)
            text = files[0].read_text(encoding="utf-8")
            rows = [json.loads(line) for line in text.splitlines()]
            self.assertIn("client_ping_sent", {row["event"] for row in rows})
            self.assertIn("ui_latency_displayed", {row["event"] for row in rows})
            self.assertNotIn("do-not-log", text)
            self.assertNotIn("private.example", text)
            self.assertNotIn("broker", text.lower())


class ServerTemporaryLatencyDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_pong_exposes_only_timing_diagnostics(self):
        class FakeSocket:
            def __init__(self) -> None:
                self.sent: list[dict] = []

            async def send_json(self, payload: dict) -> None:
                self.sent.append(payload)

        socket = FakeSocket()
        ws_server._send_locks[socket] = asyncio.Lock()
        try:
            with patch.object(ws_server, "record_latency_diagnostic") as record:
                self.assertTrue(await ws_server._send_pong_with_latency_diagnostics(socket, "ping-1", "session-1"))
            self.assertEqual(socket.sent[0]["type"], "PONG")
            self.assertEqual(socket.sent[0]["id"], "ping-1")
            self.assertEqual(
                set(socket.sent[0]["payload"]),
                {
                    "server_received_utc_ms",
                    "server_send_started_utc_ms",
                    "server_send_lock_wait_ms",
                },
            )
            record.assert_called_once()
        finally:
            ws_server._send_locks.pop(socket, None)

    async def test_cancelled_lock_wait_does_not_release_another_sender_lock(self):
        class FakeSocket:
            async def send_json(self, _payload: dict) -> None:
                raise AssertionError("send_json should not be reached")

        socket = FakeSocket()
        lock = asyncio.Lock()
        await lock.acquire()
        ws_server._send_locks[socket] = lock
        task = asyncio.create_task(ws_server._send_pong_with_latency_diagnostics(socket, "ping-2", "session-2"))
        await asyncio.sleep(0)
        task.cancel()
        try:
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(lock.locked())
        finally:
            if lock.locked():
                lock.release()
            ws_server._send_locks.pop(socket, None)


if __name__ == "__main__":
    unittest.main()
