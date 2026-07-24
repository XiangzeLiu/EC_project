import os
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Server_manager"))
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SM_DNSPOD_MODE", "mock")
os.environ.setdefault("SM_DOMAIN_POOL_REQUIRED", "1")
os.environ.setdefault("SM_ALLOWED_HOSTS", "testserver,localhost,127.0.0.1")
os.environ.setdefault("SM_LOG_LEVEL", "CRITICAL")
os.environ.setdefault("SM_CADDY_AUTO_MANAGE", "0")

import auth
import config as sm_config
import database
import main as sm_main
import node_state
from fastapi.testclient import TestClient

from Client.network.ts_websocket import TSWebSocketClient
from Trader_Server import config as ts_config
from Trader_Server.services import registration
from Trader_Server.services.trading_svc import _validate_order_params
from Trader_Server.network import ws_server as ts_ws_server
from Trader_Server.services import broker_gate as ts_broker_gate
from Trader_Server.services import config_sync as ts_config_sync
from Trader_Server.services import trading_svc as ts_trading_svc


class AccessChainTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database._DB_PATH = str(Path(self.temp_dir.name) / "sm.db")
        database.init_db()
        node_state.manager._states.clear()
        sm_config.active_client_tokens.clear()
        sm_main._admin_sessions.clear()
        self.original_admin_check = sm_main._is_admin_logged_in
        self.original_probe = sm_main._probe_ts_request_alive
        sm_main._is_admin_logged_in = lambda request: True
        sm_main._probe_ts_request_alive = lambda req, request_id, timeout_s=10: ("ok", "")
        self.client = TestClient(sm_main.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        sm_main._is_admin_logged_in = self.original_admin_check
        sm_main._probe_ts_request_alive = self.original_probe
        node_state.manager._states.clear()
        sm_config.active_client_tokens.clear()
        self.temp_dir.cleanup()

    def _create_online_node(self, index: int, public_ip: str) -> tuple[dict, dict]:
        registered = self.client.post("/nodes/register-request", json={
            "node_name": f"ts-{index}",
            "region": "TT",
            "host": public_ip,
            "public_ip": public_ip,
        }).json()
        self.assertTrue(registered["ok"], registered)
        approved = self.client.post(
            f"/api/nodes/{registered['request_id']}/approve",
            json={"broker_type": "TT", "credentials": {}},
        ).json()
        self.assertTrue(approved["ok"], approved)
        request_row = database.get_node_request_by_id(registered["request_id"])
        heartbeat = self.client.post(
            "/nodes/heartbeat",
            headers={"Authorization": f"Bearer {request_row['token']}"},
            json={"ip": public_ip},
        ).json()
        self.assertEqual(heartbeat["status"], "ok")
        return approved, request_row

    def test_account_cannot_cross_query_occupy_or_verify_another_node(self):
        imported = self.client.post(
            "/api/domain-pool/import",
            json={"domains": [
                "ts-01.ts.scjrdomain.com",
                "ts-02.ts.scjrdomain.com",
            ]},
        ).json()
        self.assertTrue(imported["ok"])
        first, first_row = self._create_online_node(1, "8.8.8.8")
        second, second_row = self._create_online_node(2, "1.1.1.1")

        database.create_account(
            "trader-a",
            "pw",
            "wss://ts-01.ts.scjrdomain.com/ws",
            "TT",
            role="trader",
        )
        login = self.client.post("/auth/login", json={
            "username": "trader-a",
            "password": "pw",
            "force": False,
        }).json()
        headers = {"Authorization": f"Bearer {login['token']}"}

        other_status = self.client.get(
            "/api/accounts/se-status?address=wss%3A%2F%2Fts-02.ts.scjrdomain.com%2Fws",
            headers=headers,
        ).json()
        self.assertFalse(other_status["ok"])
        self.assertEqual(other_status["error"], "address_not_bound_to_account")

        other_occupy = self.client.post(
            f"/api/nodes/{second['server_id']}/occupy",
            headers=headers,
            json={"username": "trader-a"},
        ).json()
        self.assertFalse(other_occupy["ok"])
        self.assertEqual(other_occupy["error"], "node_not_bound_to_account")

        other_verify = self.client.post(
            "/auth/verify-token",
            headers={"Authorization": f"Bearer {second_row['token']}"},
            json={"token": login["token"], "server_id": second["server_id"]},
        ).json()
        self.assertFalse(other_verify["valid"])
        self.assertEqual(other_verify["reason"], "node_not_bound_to_account")

        own_status = self.client.get(
            "/api/accounts/se-status?address=wss%3A%2F%2Fts-01.ts.scjrdomain.com%2Fws",
            headers=headers,
        ).json()
        self.assertTrue(own_status["online"])
        own_occupy = self.client.post(
            f"/api/nodes/{first['server_id']}/occupy",
            headers=headers,
            json={"username": "trader-a"},
        ).json()
        self.assertTrue(own_occupy["ok"])
        own_verify = self.client.post(
            "/auth/verify-token",
            headers={"Authorization": f"Bearer {first_row['token']}"},
            json={"token": login["token"], "server_id": first["server_id"]},
        ).json()
        self.assertTrue(own_verify["valid"])

    def test_domain_delete_requires_bound_node_to_be_deleted_first(self):
        imported = self.client.post("/api/domain-pool/import", json={
            "domains": ["www.ts01.scjrdomain.com"],
        }).json()
        self.assertTrue(imported["ok"], imported)

        approved, _row = self._create_online_node(1, "8.8.8.8")
        entry = database.list_ts_domain_pool()["items"][0]
        self.assertEqual(entry["status"], "occupied")

        blocked = self.client.post(
            f"/api/domain-pool/{entry['id']}/delete"
        ).json()
        self.assertFalse(blocked["ok"])
        self.assertIn("delete the TS node first", blocked["error"])

        deleted_node = self.client.post(
            f"/api/nodes/{approved['server_id']}/delete"
        ).json()
        self.assertTrue(deleted_node["ok"], deleted_node)
        self.assertEqual(
            database.get_ts_domain_pool_entry(entry["id"])["status"],
            "cooling",
        )
        released_entry = database.get_ts_domain_pool_entry(entry["id"])
        self.assertEqual(released_entry["assigned_server_id"], "")
        self.assertEqual(released_entry["assigned_node_name"], "")
        self.assertEqual(released_entry["assigned_ip"], "")

        deleted_domain = self.client.post(
            f"/api/domain-pool/{entry['id']}/delete"
        ).json()
        self.assertTrue(deleted_domain["ok"], deleted_domain)
        self.assertIsNone(database.get_ts_domain_pool_entry(entry["id"]))

    def test_expired_client_token_is_pruned(self):
        token = auth.generate_client_token("expired-user")
        sm_config.active_client_tokens[token]["created_at"] = (
            time.time() - sm_config.CLIENT_TOKEN_TTL_SECONDS - 1
        )
        self.assertEqual(auth.get_client_username(token), "")
        self.assertNotIn(token, sm_config.active_client_tokens)

    def test_expired_connection_recheck_releases_stale_occupation(self):
        self.client.post("/api/domain-pool/import", json={
            "domains": ["ts-01.ts.scjrdomain.com"],
        })
        approved, node_row = self._create_online_node(1, "8.8.8.8")
        database.create_account(
            "expiring-user",
            "pw",
            "wss://ts-01.ts.scjrdomain.com/ws",
            "TT",
            role="trader",
        )
        login = self.client.post("/auth/login", json={
            "username": "expiring-user",
            "password": "pw",
            "force": False,
        }).json()
        headers = {"Authorization": f"Bearer {login['token']}"}
        occupied = self.client.post(
            f"/api/nodes/{approved['server_id']}/occupy",
            headers=headers,
            json={"username": "expiring-user"},
        ).json()
        self.assertTrue(occupied["ok"])

        sm_config.active_client_tokens[login["token"]]["created_at"] = (
            time.time() - sm_config.CLIENT_TOKEN_TTL_SECONDS - 1
        )
        rechecked = self.client.post(
            "/auth/verify-token",
            headers={"Authorization": f"Bearer {node_row['token']}"},
            json={
                "token": login["token"],
                "server_id": approved["server_id"],
                "recheck": True,
                "username": "expiring-user",
            },
        ).json()
        self.assertFalse(rechecked["valid"])
        self.assertTrue(rechecked["occupation_released"])
        self.assertIsNone(
            node_state.manager.get_occupation_info(approved["server_id"])
        )

    def test_invalid_initial_token_cannot_release_occupation(self):
        self.client.post("/api/domain-pool/import", json={
            "domains": ["ts-01.ts.scjrdomain.com"],
        })
        approved, node_row = self._create_online_node(1, "8.8.8.8")
        state = node_state.manager.get(approved["server_id"])
        state.occupied_by = "current-user"
        response = self.client.post(
            "/auth/verify-token",
            headers={"Authorization": f"Bearer {node_row['token']}"},
            json={
                "token": "invalid-token",
                "server_id": approved["server_id"],
                "username": "current-user",
            },
        ).json()
        self.assertFalse(response["valid"])
        self.assertFalse(response["occupation_released"])
        self.assertEqual(
            node_state.manager.get_occupation_info(approved["server_id"])["occupied_by"],
            "current-user",
        )

    def test_suspended_node_revokes_existing_client_connection(self):
        self.client.post("/api/domain-pool/import", json={
            "domains": ["ts-01.ts.scjrdomain.com"],
        })
        approved, node_row = self._create_online_node(1, "8.8.8.8")
        database.create_account(
            "suspended-node-user",
            "pw",
            "wss://ts-01.ts.scjrdomain.com/ws",
            "TT",
            role="trader",
        )
        login = self.client.post("/auth/login", json={
            "username": "suspended-node-user",
            "password": "pw",
            "force": False,
        }).json()
        occupied = self.client.post(
            f"/api/nodes/{approved['server_id']}/occupy",
            headers={"Authorization": f"Bearer {login['token']}"},
            json={"username": "suspended-node-user"},
        ).json()
        self.assertTrue(occupied["ok"])
        suspended = self.client.post(
            f"/api/nodes/{approved['server_id']}/suspend"
        ).json()
        self.assertTrue(suspended["ok"])

        rechecked = self.client.post(
            "/auth/verify-token",
            headers={"Authorization": f"Bearer {node_row['token']}"},
            json={
                "token": login["token"],
                "server_id": approved["server_id"],
                "recheck": True,
                "username": "suspended-node-user",
            },
        ).json()
        self.assertFalse(rechecked["valid"])
        self.assertEqual(rechecked["reason"], "node_suspended")
        self.assertTrue(rechecked["occupation_released"])
        self.assertIsNone(
            node_state.manager.get_occupation_info(approved["server_id"])
        )

    def test_force_login_invalidates_previous_token(self):
        database.create_account(
            "single-login",
            "pw",
            "wss://ts-01.ts.scjrdomain.com/ws",
            "TT",
            role="trader",
        )
        first = self.client.post("/auth/login", json={
            "username": "single-login",
            "password": "pw",
            "force": False,
        })
        self.assertEqual(first.status_code, 200)
        first_token = first.json()["token"]
        duplicate = self.client.post("/auth/login", json={
            "username": "single-login",
            "password": "pw",
            "force": False,
        })
        self.assertEqual(duplicate.status_code, 409)
        takeover = self.client.post("/auth/login", json={
            "username": "single-login",
            "password": "pw",
            "force": True,
        })
        self.assertEqual(takeover.status_code, 200)
        self.assertEqual(auth.get_client_username(first_token), "")

    def test_stale_recheck_cannot_release_takeover_session(self):
        self.client.post("/api/domain-pool/import", json={
            "domains": ["ts-01.ts.scjrdomain.com"],
        })
        approved, node_row = self._create_online_node(1, "8.8.8.8")
        database.create_account(
            "takeover-user",
            "pw",
            "wss://ts-01.ts.scjrdomain.com/ws",
            "TT",
            role="trader",
        )
        first = self.client.post("/auth/login", json={
            "username": "takeover-user",
            "password": "pw",
            "force": False,
        }).json()
        self.assertTrue(self.client.post(
            f"/api/nodes/{approved['server_id']}/occupy",
            headers={"Authorization": f"Bearer {first['token']}"},
            json={"username": "takeover-user"},
        ).json()["ok"])

        second = self.client.post("/auth/login", json={
            "username": "takeover-user",
            "password": "pw",
            "force": True,
        }).json()
        self.assertTrue(self.client.post(
            f"/api/nodes/{approved['server_id']}/occupy",
            headers={"Authorization": f"Bearer {second['token']}"},
            json={"username": "takeover-user"},
        ).json()["ok"])

        stale = self.client.post(
            "/auth/verify-token",
            headers={"Authorization": f"Bearer {node_row['token']}"},
            json={
                "token": first["token"],
                "server_id": approved["server_id"],
                "recheck": True,
                "username": "takeover-user",
            },
        ).json()
        self.assertFalse(stale["valid"])
        self.assertFalse(stale["occupation_released"])

        current = self.client.post(
            "/auth/verify-token",
            headers={"Authorization": f"Bearer {node_row['token']}"},
            json={
                "token": second["token"],
                "server_id": approved["server_id"],
            },
        ).json()
        self.assertTrue(current["valid"])

    def test_ts_disconnect_releases_only_matching_session(self):
        self.client.post("/api/domain-pool/import", json={
            "domains": ["ts-01.ts.scjrdomain.com"],
        })
        approved, node_row = self._create_online_node(1, "8.8.8.8")
        database.create_account(
            "disconnect-user",
            "pw",
            "wss://ts-01.ts.scjrdomain.com/ws",
            "TT",
            role="trader",
        )
        first = self.client.post("/auth/login", json={
            "username": "disconnect-user",
            "password": "pw",
            "force": False,
        }).json()
        self.assertTrue(self.client.post(
            f"/api/nodes/{approved['server_id']}/occupy",
            headers={"Authorization": f"Bearer {first['token']}"},
            json={"username": "disconnect-user"},
        ).json()["ok"])

        wrong = self.client.post(
            "/nodes/release-occupation",
            headers={"Authorization": f"Bearer {node_row['token']}"},
            json={
                "server_id": approved["server_id"],
                "username": "disconnect-user",
                "client_token": "another-session-token",
            },
        ).json()
        self.assertTrue(wrong["ok"])
        self.assertFalse(wrong["released"])

        released = self.client.post(
            "/nodes/release-occupation",
            headers={"Authorization": f"Bearer {node_row['token']}"},
            json={
                "server_id": approved["server_id"],
                "username": "disconnect-user",
                "client_token": first["token"],
            },
        ).json()
        self.assertTrue(released["ok"])
        self.assertTrue(released["released"])
        self.assertIsNone(
            node_state.manager.get_occupation_info(approved["server_id"])
        )

    def test_admin_force_release_prefers_public_wss_endpoint(self):
        self.client.post("/api/domain-pool/import", json={
            "domains": ["ts-01.ts.scjrdomain.com"],
        })
        approved, _row = self._create_online_node(1, "8.8.8.8")
        state = node_state.manager.get(approved["server_id"])
        state.occupied_by = "trader-a"
        captured = []
        original_force = sm_main._force_disconnect_ts_clients
        try:
            def fake_force(endpoint, token, reason, timeout_s=8):
                captured.append(endpoint)
                return True, {"ok": True, "kicked": 1}

            sm_main._force_disconnect_ts_clients = fake_force
            result = self.client.post(
                f"/api/nodes/{approved['server_id']}/force-release"
            ).json()
            self.assertTrue(result["ok"])
            self.assertEqual(captured, ["wss://ts-01.ts.scjrdomain.com/ws"])
        finally:
            sm_main._force_disconnect_ts_clients = original_force

    def test_endpoint_normalization_uses_wss_for_bare_domain(self):
        self.assertEqual(
            TSWebSocketClient.normalize_endpoint("ts-01.ts.scjrdomain.com"),
            "wss://ts-01.ts.scjrdomain.com/ws",
        )
        self.assertEqual(
            TSWebSocketClient.normalize_endpoint("8.8.8.8"),
            "ws://8.8.8.8:8900/ws",
        )
        self.assertEqual(
            TSWebSocketClient.normalize_endpoint("ts-01.ts.scjrdomain.com:443"),
            "wss://ts-01.ts.scjrdomain.com:443/ws",
        )

    def test_pending_registration_is_resumed_without_new_submission(self):
        original_file = ts_config.REGISTER_STATE_FILE
        original_state = {
            "server_id": ts_config.state.server_id,
            "token": ts_config.state.token,
            "status": ts_config.state.status,
            "manager_url": ts_config.state.manager_url,
            "node_name": ts_config.state.node_name,
        }
        try:
            ts_config.REGISTER_STATE_FILE = Path(self.temp_dir.name) / ".register_state.json"
            ts_config.state.server_id = ""
            ts_config.state.token = ""
            ts_config.state.status = "registering"
            ts_config.save_register_state(
                "req_resume",
                "https://scjrdomain.com",
                "resume-node",
                "2099-01-01T00:00:00+00:00",
            )
            resumed = registration.submit_registration()
            self.assertTrue(resumed["resumed"])
            self.assertEqual(resumed["request_id"], "req_resume")
        finally:
            ts_config.REGISTER_STATE_FILE = original_file
            for key, value in original_state.items():
                setattr(ts_config.state, key, value)

    def test_extended_tif_values_are_accepted(self):
        base = {
            "symbol": "AAPL",
            "action": "Buy to Open",
            "qty": 1,
            "price": 100,
            "order_type": "limit",
        }
        for tif in ("EXT", "GTC_EXT"):
            normalized, error = _validate_order_params({**base, "tif": tif})
            self.assertIsNone(error)
            self.assertEqual(normalized["tif"], tif)


class _FakeWebSocket:
    def __init__(self):
        self.sent = []
        self.closed = []

    async def send_json(self, payload):
        self.sent.append(payload)

    async def close(self, code=1000, reason=""):
        self.closed.append((code, reason))


class WebSocketAccessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ts_ws_server._connections.clear()
        ts_ws_server._send_locks.clear()
        ts_broker_gate._gates.clear()

    def tearDown(self):
        ts_ws_server._connections.clear()
        ts_ws_server._send_locks.clear()
        ts_broker_gate._gates.clear()

    async def test_new_connection_replaces_existing_node_connection(self):
        old = _FakeWebSocket()
        new = _FakeWebSocket()
        ts_ws_server._connections[old] = {"server_id": "node-1", "username": "user"}
        ts_ws_server._connections[new] = {"server_id": "node-1", "username": "user"}
        await ts_ws_server._replace_existing_node_connections(new, "user", "node-1")
        self.assertEqual(old.closed[0][0], ts_ws_server._FORCE_DISCONNECT_CODE)
        self.assertFalse(new.closed)

    async def test_revoked_token_closes_existing_connection(self):
        ws = _FakeWebSocket()
        ts_ws_server._connections[ws] = {
            "server_id": "node-1",
            "username": "user",
            "client_token": "expired-token",
            "last_auth_check": 0,
        }
        original_validate = ts_ws_server._validate_client_token
        try:
            async def fake_validate(token, server_id="", recheck_username=""):
                return {"valid": False, "allowed": False, "reason": "invalid_or_expired"}

            ts_ws_server._validate_client_token = fake_validate
            self.assertFalse(await ts_ws_server._revalidate_connection(ws))
            self.assertEqual(ws.closed[0][0], 4004)
        finally:
            ts_ws_server._validate_client_token = original_validate

    async def test_quotes_require_runtime_broker_login_gate(self):
        response = await ts_ws_server._handle_quote_subscribe(
            {"id": "q1", "payload": {"action": "subscribe", "symbols": ["AAPL"]}},
            "session-1",
            "trace-1",
            {"username": "user", "server_id": "node-1"},
        )
        self.assertFalse(response["payload"]["success"])
        self.assertEqual(response["payload"]["code"], "BROKER_LOGIN_REQUIRED")

    async def test_broker_login_activates_gate_and_allows_position_query(self):
        original_login = ts_config_sync.login_broker_with_credentials
        original_connected = ts_trading_svc.ensure_broker_connected
        original_current = ts_trading_svc.get_current_broker

        class FakeBroker:
            @staticmethod
            def capabilities():
                return {"positions": True}

            async def get_positions(self, filters=None):
                return [{"symbol": "AAPL", "quantity": 1}]

        try:
            async def fake_login(broker_type="", credentials=None):
                return {"success": True, "code": "BROKER_LOGIN_OK", "message": "ok"}

            async def fake_connected():
                return True

            ts_config_sync.login_broker_with_credentials = fake_login
            login_response = await ts_ws_server._handle_broker_login(
                {
                    "id": "login-1",
                    "payload": {
                        "account_username": "broker-user",
                        "account_password": "broker-password",
                    },
                },
                "session-1",
                "trace-1",
                {"username": "client-user", "server_id": "node-1"},
            )
            self.assertTrue(login_response["payload"]["success"])
            self.assertTrue(ts_broker_gate.is_gate_active("client-user", "node-1"))

            ts_trading_svc.ensure_broker_connected = fake_connected
            ts_trading_svc.get_current_broker = lambda: FakeBroker()
            positions = await ts_trading_svc.get_positions(
                username="client-user",
                server_id="node-1",
                session_id="session-1",
            )
            self.assertTrue(positions["success"])
            self.assertEqual(positions["positions"][0]["symbol"], "AAPL")
        finally:
            ts_config_sync.login_broker_with_credentials = original_login
            ts_trading_svc.ensure_broker_connected = original_connected
            ts_trading_svc.get_current_broker = original_current

    async def test_broker_challenge_does_not_activate_gate(self):
        original_login = ts_config_sync.login_broker_with_credentials
        try:
            async def fake_challenge(broker_type="", credentials=None):
                return {
                    "success": False,
                    "code": "BROKER_DEVICE_CHALLENGE_REQUIRED",
                    "message": "challenge",
                    "challenge_token": "challenge-token",
                    "challenge": {},
                }

            ts_config_sync.login_broker_with_credentials = fake_challenge
            response = await ts_ws_server._handle_broker_login(
                {
                    "id": "login-2",
                    "payload": {
                        "account_username": "broker-user",
                        "account_password": "broker-password",
                    },
                },
                "session-2",
                "trace-2",
                {"username": "client-user", "server_id": "node-1"},
            )
            self.assertFalse(response["payload"]["success"])
            self.assertEqual(
                response["payload"]["code"],
                "BROKER_DEVICE_CHALLENGE_REQUIRED",
            )
            self.assertEqual(response["payload"]["challenge_token"], "challenge-token")
            self.assertFalse(ts_broker_gate.is_gate_active("client-user", "node-1"))
        finally:
            ts_config_sync.login_broker_with_credentials = original_login


class AdminManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database._DB_PATH = str(Path(self.temp_dir.name) / "admin.db")
        database.init_db()
        node_state.manager._states.clear()
        sm_config.active_client_tokens.clear()
        sm_main._admin_sessions.clear()
        self.client = TestClient(sm_main.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        node_state.manager._states.clear()
        sm_config.active_client_tokens.clear()
        sm_main._admin_sessions.clear()
        self.temp_dir.cleanup()

    def test_admin_login_domain_import_and_account_lifecycle(self):
        login = self.client.post(
            "/admin/login",
            data={"username": "admin", "password": "admin_sc"},
            follow_redirects=False,
        )
        self.assertEqual(login.status_code, 302)
        self.assertEqual(login.headers["location"], "/admin/dashboard")
        dashboard = self.client.get("/admin/dashboard")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn("domain-pool-body", dashboard.text)
        self.assertIn("deleteDomainEntry", dashboard.text)
        self.assertNotIn("初始化20个", dashboard.text)

        generated = self.client.post(
            "/api/domain-pool/import",
            json={"count": 20, "start": 1},
        ).json()
        self.assertFalse(generated["ok"])

        imported = self.client.post(
            "/api/domain-pool/import",
            json={
                "domains": "，".join([
                    "ts-01.ts.scjrdomain.com",
                    "ts-02.ts.scjrdomain.com",
                ]) + "\n" + "\n".join(
                    f"ts-{index:02d}.ts.scjrdomain.com"
                    for index in range(3, 21)
                ),
            },
        ).json()
        self.assertTrue(imported["ok"])
        self.assertEqual(imported["inserted"], 20)

        last_domain = database.list_ts_domain_pool(
            page=1,
            page_size=20,
        )["items"][-1]
        deleted = self.client.post(
            f"/api/domain-pool/{last_domain['id']}/delete"
        ).json()
        self.assertTrue(deleted["ok"], deleted)
        self.assertEqual(database.list_ts_domain_pool()["total"], 19)

        created = self.client.post("/api/accounts/create", json={
            "username": "managed-trader",
            "password": "pw",
            "role": "trader",
            "se_address": "wss://ts-01.ts.scjrdomain.com/ws",
            "broker_tag": "TT",
            "description": "integration",
        }).json()
        self.assertTrue(created["ok"], created)

        client_login = self.client.post("/auth/login", json={
            "username": "managed-trader",
            "password": "pw",
            "force": False,
        }).json()
        self.assertEqual(
            client_login["se_address"],
            "wss://ts-01.ts.scjrdomain.com/ws",
        )
        token = client_login["token"]

        suspended = self.client.post(
            f"/api/accounts/{created['data']['id']}/suspend"
        ).json()
        self.assertTrue(suspended["ok"])
        self.assertEqual(auth.get_client_username(token), "")
        denied = self.client.post("/auth/login", json={
            "username": "managed-trader",
            "password": "pw",
            "force": False,
        })
        self.assertEqual(denied.status_code, 401)


if __name__ == "__main__":
    unittest.main()
