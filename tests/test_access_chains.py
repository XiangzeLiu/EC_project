import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace


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
from Client.ui_qt import main_window as client_main_window
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
            json={"username": "trader-a", "connection_id": "conn-other"},
        ).json()
        self.assertFalse(other_occupy["ok"])
        self.assertEqual(other_occupy["error"], "node_not_bound_to_account")

        other_verify = self.client.post(
            "/auth/verify-token",
            headers={"Authorization": f"Bearer {second_row['token']}"},
            json={
                "token": login["token"],
                "server_id": second["server_id"],
                "connection_id": "conn-other",
            },
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
            json={"username": "trader-a", "connection_id": "conn-own"},
        ).json()
        self.assertTrue(own_occupy["ok"])
        own_verify = self.client.post(
            "/auth/verify-token",
            headers={"Authorization": f"Bearer {first_row['token']}"},
            json={
                "token": login["token"],
                "server_id": first["server_id"],
                "connection_id": "conn-own",
            },
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
            json={"username": "expiring-user", "connection_id": "conn-expiring"},
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
                "connection_id": "conn-expiring",
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
        state._connection_id = "conn-invalid"
        response = self.client.post(
            "/auth/verify-token",
            headers={"Authorization": f"Bearer {node_row['token']}"},
            json={
                "token": "invalid-token",
                "server_id": approved["server_id"],
                "username": "current-user",
                "connection_id": "conn-invalid",
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
            json={"username": "suspended-node-user", "connection_id": "conn-suspended"},
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
                "connection_id": "conn-suspended",
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
            json={"username": "takeover-user", "connection_id": "conn-takeover-old"},
        ).json()["ok"])

        second = self.client.post("/auth/login", json={
            "username": "takeover-user",
            "password": "pw",
            "force": True,
        }).json()
        self.assertTrue(self.client.post(
            f"/api/nodes/{approved['server_id']}/occupy",
            headers={"Authorization": f"Bearer {second['token']}"},
            json={"username": "takeover-user", "connection_id": "conn-takeover-new"},
        ).json()["ok"])

        stale = self.client.post(
            "/auth/verify-token",
            headers={"Authorization": f"Bearer {node_row['token']}"},
            json={
                "token": first["token"],
                "server_id": approved["server_id"],
                "recheck": True,
                "username": "takeover-user",
                "connection_id": "conn-takeover-old",
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
                "connection_id": "conn-takeover-new",
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
            json={"username": "disconnect-user", "connection_id": "conn-disconnect"},
        ).json()["ok"])

        wrong = self.client.post(
            "/nodes/release-occupation",
            headers={"Authorization": f"Bearer {node_row['token']}"},
            json={
                "server_id": approved["server_id"],
                "username": "disconnect-user",
                "client_token": "another-session-token",
                "connection_id": "conn-disconnect",
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
                "connection_id": "conn-disconnect",
            },
        ).json()
        self.assertTrue(released["ok"])
        self.assertTrue(released["released"])
        self.assertIsNone(
            node_state.manager.get_occupation_info(approved["server_id"])
        )

    def test_old_connection_cannot_release_new_reconnect(self):
        self.client.post("/api/domain-pool/import", json={
            "domains": ["ts-01.ts.scjrdomain.com"],
        })
        approved, node_row = self._create_online_node(1, "8.8.8.8")
        database.create_account(
            "reconnect-user",
            "pw",
            "wss://ts-01.ts.scjrdomain.com/ws",
            "TT",
            role="trader",
        )
        login = self.client.post("/auth/login", json={
            "username": "reconnect-user",
            "password": "pw",
            "force": False,
        }).json()
        client_headers = {"Authorization": f"Bearer {login['token']}"}
        node_headers = {"Authorization": f"Bearer {node_row['token']}"}

        first_connection = "conn-reconnect-old"
        second_connection = "conn-reconnect-new"
        self.assertTrue(self.client.post(
            f"/api/nodes/{approved['server_id']}/occupy",
            headers=client_headers,
            json={"username": "reconnect-user", "connection_id": first_connection},
        ).json()["ok"])
        self.assertTrue(self.client.post(
            "/auth/verify-token",
            headers=node_headers,
            json={
                "token": login["token"],
                "server_id": approved["server_id"],
                "connection_id": first_connection,
            },
        ).json()["valid"])

        self.assertTrue(self.client.post(
            f"/api/nodes/{approved['server_id']}/occupy",
            headers=client_headers,
            json={"username": "reconnect-user", "connection_id": second_connection},
        ).json()["ok"])
        self.assertTrue(self.client.post(
            "/auth/verify-token",
            headers=node_headers,
            json={
                "token": login["token"],
                "server_id": approved["server_id"],
                "connection_id": second_connection,
            },
        ).json()["valid"])

        stale_release = self.client.post(
            "/nodes/release-occupation",
            headers=node_headers,
            json={
                "server_id": approved["server_id"],
                "username": "reconnect-user",
                "client_token": login["token"],
                "connection_id": first_connection,
            },
        ).json()
        self.assertFalse(stale_release["released"])
        occupation = node_state.manager.get_occupation_info(approved["server_id"])
        self.assertEqual(occupation["occupied_by"], "reconnect-user")
        self.assertTrue(occupation["connection_confirmed"])

        current_release = self.client.post(
            "/nodes/release-occupation",
            headers=node_headers,
            json={
                "server_id": approved["server_id"],
                "username": "reconnect-user",
                "client_token": login["token"],
                "connection_id": second_connection,
            },
        ).json()
        self.assertTrue(current_release["released"])

    def test_unconfirmed_connection_reservation_expires_without_offlining_ts(self):
        self.client.post("/api/domain-pool/import", json={
            "domains": ["ts-01.ts.scjrdomain.com"],
        })
        approved, _node_row = self._create_online_node(1, "8.8.8.8")
        state = node_state.manager.get(approved["server_id"])
        ok, error = node_state.manager.occupy(
            approved["server_id"],
            "reserved-user",
            "reserved-token",
            "conn-never-arrived",
        )
        self.assertTrue(ok, error)
        state._reservation_deadline = time.time() - 1

        expired = node_state.manager.expire_unconfirmed_occupations()
        self.assertEqual(expired, [approved["server_id"]])
        self.assertEqual(state.status, "online")
        self.assertIsNone(node_state.manager.get_occupation_info(approved["server_id"]))

    def test_occupied_heartbeat_uses_transition_window_until_real_heartbeat(self):
        self.client.post("/api/domain-pool/import", json={
            "domains": ["ts-01.ts.scjrdomain.com"],
        })
        approved, _node_row = self._create_online_node(1, "8.8.8.8")
        state = node_state.manager.get(approved["server_id"])
        state.last_heartbeat = time.time() - 50
        ok, error = node_state.manager.occupy(
            approved["server_id"],
            "heartbeat-user",
            "heartbeat-token",
            "conn-heartbeat",
        )
        self.assertTrue(ok, error)
        self.assertGreater(state.heartbeat_timeout, 75)
        self.assertTrue(state.is_alive)

        node_state.manager.update_heartbeat(approved["server_id"], "8.8.8.8")
        self.assertEqual(state.heartbeat_timeout, node_state.OCCUPIED_HEARTBEAT_TIMEOUT)
        self.assertTrue(state._occupied_hb_confirmed)

    def test_db_load_never_restores_client_occupation(self):
        manager = node_state.NodeStateManager()
        manager.register({
            "server_id": "node-restarted",
            "node_name": "Restarted node",
            "req_status": "online",
            "last_heartbeat": datetime.now(timezone.utc).isoformat(),
            "occupied_by": "stale-client",
        })
        self.assertIsNone(manager.get_occupation_info("node-restarted"))
        snapshot = manager.prepare_db_sync_data()[0]
        self.assertEqual(snapshot["occupied_by"], "")
        self.assertEqual(snapshot["occupied_at"], "")

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


class ClientConnectionLifecycleTests(unittest.TestCase):
    def test_main_connection_flow_creates_one_durable_websocket(self):
        instances = []
        occupied = []

        class FakeWebSocketClient:
            @staticmethod
            def normalize_endpoint(endpoint, default_port=8900):
                return f"wss://{endpoint}/ws"

            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.connection_id = "conn-single-durable"
                self.started = False
                instances.append(self)

            def start(self):
                self.started = True

        fake_window = SimpleNamespace(
            _last_connected_se="",
            _se_generation=0,
            _se_server_id="node-1",
            _se_client=None,
            _se_connection_id="",
            http=SimpleNamespace(token="client-token"),
        )
        fake_window._wrap_se_message_handler = lambda generation: None
        fake_window._wrap_se_status_handler = lambda generation: None
        fake_window._wrap_ts_latency_handler = lambda generation: None
        fake_window._wrap_se_state_handler = lambda generation: None
        fake_window._prepare_ts_reconnect = lambda generation, attempt, connection_id: True

        def occupy(connection_id="", **_kwargs):
            occupied.append(connection_id)
            return True

        fake_window._occupy_se_node = occupy

        original_client = client_main_window.TSWebSocketClient
        try:
            client_main_window.TSWebSocketClient = FakeWebSocketClient
            client_main_window.TradingTerminalQt._connect_ts_with_retry(
                fake_window,
                "ts-01.ts.scjrdomain.com",
            )
        finally:
            client_main_window.TSWebSocketClient = original_client

        self.assertEqual(len(instances), 1)
        self.assertTrue(instances[0].started)
        self.assertIs(fake_window._se_client, instances[0])
        self.assertEqual(occupied, ["conn-single-durable"])

    def test_stop_interrupts_retry_wait_without_extra_connection(self):
        reconnecting = threading.Event()
        attempts = []

        def on_state(state, _detail):
            if state == "reconnecting":
                reconnecting.set()

        client = TSWebSocketClient(
            ws_url="ws://127.0.0.1:1/ws",
            reconnect_enabled=True,
            on_reconnect_prepare_callback=lambda _attempt, _connection_id: True,
            on_state_callback=on_state,
        )

        async def fake_connect(connection_id):
            attempts.append(connection_id)
            return True

        client._connect_and_run = fake_connect
        client.start()
        self.assertTrue(reconnecting.wait(1.0))
        client.stop(wait=True, timeout=1.0)
        self.assertTrue(client.wait_until_stopped(1.0))
        self.assertEqual(len(attempts), 1)


class _FakeWebSocket:
    def __init__(self):
        self.sent = []
        self.closed = []

    async def send_json(self, payload):
        self.sent.append(payload)

    async def close(self, code=1000, reason=""):
        self.closed.append((code, reason))


class _ScriptedWebSocket(_FakeWebSocket):
    def __init__(self, messages):
        super().__init__()
        self.messages = list(messages)
        self.client = SimpleNamespace(host="127.0.0.1", port=12345)
        self.accepted = False

    async def accept(self):
        self.accepted = True

    async def receive_text(self):
        if self.messages:
            return self.messages.pop(0)
        raise ts_ws_server.WebSocketDisconnect()


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

    async def test_connect_propagates_physical_connection_id_to_sm_verification(self):
        captured = []
        connection_id = "conn-propagated-to-sm"
        ws = _ScriptedWebSocket([json.dumps({
            "type": "CONNECT",
            "payload": {
                "token": "client-token",
                "server_id": "node-1",
                "connection_id": connection_id,
                "trace_id": "trace-1",
            },
        })])
        original_validate = ts_ws_server._validate_client_token
        try:
            async def fake_validate(token, server_id="", recheck_username="", connection_id=""):
                captured.append((token, server_id, connection_id))
                return {
                    "valid": True,
                    "allowed": True,
                    "username": "user",
                    "server_id": "node-1",
                    "token_type": "client",
                }

            ts_ws_server._validate_client_token = fake_validate
            await ts_ws_server.handle_client_connection(ws)
        finally:
            ts_ws_server._validate_client_token = original_validate
            ts_ws_server._cancel_pending_releases("user", "node-1")

        self.assertTrue(ws.accepted)
        self.assertEqual(captured, [("client-token", "node-1", connection_id)])
        self.assertEqual(ws.sent[0]["type"], "CONNECT_ACK")

    async def test_revoked_token_closes_existing_connection(self):
        ws = _FakeWebSocket()
        ts_ws_server._connections[ws] = {
            "server_id": "node-1",
            "username": "user",
            "client_token": "expired-token",
            "connection_id": "conn-expired",
            "last_auth_check": 0,
        }
        original_validate = ts_ws_server._validate_client_token
        try:
            async def fake_validate(token, server_id="", recheck_username="", connection_id=""):
                return {"valid": False, "allowed": False, "reason": "invalid_or_expired"}

            ts_ws_server._validate_client_token = fake_validate
            self.assertFalse(await ts_ws_server._revalidate_connection(ws))
            self.assertEqual(ws.closed[0][0], 4004)
        finally:
            ts_ws_server._validate_client_token = original_validate

    async def test_pending_disconnect_release_is_cancelled_by_reconnect(self):
        called = []
        original_notify = ts_ws_server._notify_sm_connection_closed
        original_grace = ts_ws_server._RELEASE_GRACE_SECONDS
        try:
            async def fake_notify(conn):
                called.append(conn["connection_id"])
                return True

            ts_ws_server._notify_sm_connection_closed = fake_notify
            ts_ws_server._RELEASE_GRACE_SECONDS = 0.05
            conn = {
                "session_id": "session-old",
                "username": "user",
                "server_id": "node-1",
                "client_token": "token",
                "connection_id": "conn-old",
                "token_type": "client",
            }
            ts_ws_server._cleanup_connection_artifacts(conn)
            ts_ws_server._cancel_pending_releases("user", "node-1")
            await asyncio.sleep(0.1)
            self.assertEqual(called, [])
            self.assertNotIn("conn-old", ts_ws_server._pending_release_tasks)
        finally:
            ts_ws_server._notify_sm_connection_closed = original_notify
            ts_ws_server._RELEASE_GRACE_SECONDS = original_grace

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
