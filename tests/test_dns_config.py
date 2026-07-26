import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Server_manager"))
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SM_DNSPOD_MODE", "mock")
os.environ.setdefault("SM_LOG_LEVEL", "CRITICAL")

import database
import domain_pool
from dnspod_client import DNSPodClient
from services import dns_config_service
from services.dns_config_service import DNSRuntimeConfig


class _FakeDNSRequest:
    def __init__(self):
        self.payload = {}

    def from_json_string(self, value):
        self.payload = json.loads(value)


class _FakeDNSModels:
    DescribeRecordListRequest = _FakeDNSRequest
    CreateRecordRequest = _FakeDNSRequest
    ModifyRecordRequest = _FakeDNSRequest
    DeleteRecordRequest = _FakeDNSRequest


class _FakeTencentCloudError(RuntimeError):
    def __init__(self, code, message="SDK request failed"):
        super().__init__(message)
        self._code = code

    def get_code(self):
        return self._code


class _FakeDNSClient:
    def __init__(self, fail_on="", describe_no_data=False):
        self.fail_on = fail_on
        self.describe_no_data = describe_no_data
        self.calls = []

    def _record(self, action):
        self.calls.append(action)
        if self.fail_on == action:
            raise RuntimeError(f"{action} denied")

    def DescribeRecordList(self, request):
        self._record("describe")
        if self.describe_no_data:
            raise _FakeTencentCloudError("ResourceNotFound.NoDataOfRecord")
        return SimpleNamespace(RecordList=[])

    def CreateRecord(self, request):
        self._record("create")
        return SimpleNamespace(RecordId=101)

    def ModifyRecord(self, request):
        self._record("modify")
        return SimpleNamespace()

    def DeleteRecord(self, request):
        self._record("delete")
        return SimpleNamespace()


class DNSConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database._DB_PATH = str(Path(self.temp_dir.name) / "sm.db")
        database.init_db()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _permission_client(self, fail_on="", describe_no_data=False):
        config = DNSRuntimeConfig(
            mode="real",
            root_domain="scjrdomain.com",
            secret_id="AKID-permission",
            secret_key="permission-key",
            record_line="默认",
            ttl=600,
            cooldown_seconds=1800,
        )
        client = DNSPodClient(config)
        fake = _FakeDNSClient(
            fail_on=fail_on,
            describe_no_data=describe_no_data,
        )
        client._client = fake
        client._models = _FakeDNSModels
        return client, fake

    def test_schema_v5_and_environment_bootstrap(self):
        config = dns_config_service.get_runtime_config()
        self.assertEqual(config.mode, "mock")
        self.assertEqual(config.root_domain, "scjrdomain.com")
        conn = database._get_conn()
        try:
            self.assertEqual(database._get_user_version(conn), 5)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM dns_provider_config"
                ).fetchone()[0],
                1,
            )
        finally:
            conn.close()

    def test_v4_to_v5_migration_creates_dns_config_table(self):
        conn = database._get_conn()
        try:
            conn.execute("DROP TABLE dns_provider_config")
            conn.execute("PRAGMA user_version = 4")
            conn.commit()
        finally:
            conn.close()

        reports = database.init_db()

        self.assertTrue(any(
            report.get("from_version") == 4
            and report.get("to_version") == 5
            for report in reports
        ))
        conn = database._get_conn()
        try:
            self.assertEqual(database._get_user_version(conn), 5)
            self.assertTrue(database._table_exists(conn, "dns_provider_config"))
        finally:
            conn.close()

    def test_save_masks_and_preserves_secret_key(self):
        saved = dns_config_service.save_config({
            "secret_id": "AKID1234567890",
            "secret_key": "top-secret-key",
        }, updated_by="root")
        self.assertEqual(saved.secret_key, "top-secret-key")

        updated = dns_config_service.save_config({
            "secret_id": "",
            "secret_key": "",
        }, updated_by="root")
        self.assertEqual(updated.secret_id, "AKID1234567890")
        self.assertEqual(updated.secret_key, "top-secret-key")
        self.assertEqual(updated.ttl, 600)
        self.assertEqual(updated.cooldown_seconds, 1800)

        public = dns_config_service.public_config(updated)
        self.assertNotIn("secret_key", public)
        self.assertTrue(public["secret_key_configured"])
        self.assertNotEqual(public["secret_id_masked"], updated.secret_id)

    def test_clear_is_disabled_and_credentials_are_replaced_as_a_pair(self):
        dns_config_service.save_config({
            "secret_id": "AKID1234567890",
            "secret_key": "top-secret-key",
        })
        with self.assertRaises(dns_config_service.DNSConfigError):
            dns_config_service.save_config({"clear_secret": True})
        with self.assertRaises(dns_config_service.DNSConfigError):
            dns_config_service.save_config({"secret_id": "only-id"})

    def test_import_parser_supports_json_and_key_value_text(self):
        imported = dns_config_service.parse_import_payload(
            '{"SecretId":"AKID-json","SecretKey":"json-key","Mode":"real","TTL":1200}'
        )
        self.assertEqual(imported["secret_id"], "AKID-json")
        self.assertEqual(imported["secret_key"], "json-key")

        updated = dns_config_service.parse_import_payload(
            "SM_DNSPOD_MODE=real\nSM_DNSPOD_SECRET_ID=AKID-kv\n"
            "SM_DNSPOD_SECRET_KEY=kv-key\nSM_DNS_TTL=1800"
        )
        self.assertEqual(updated["secret_id"], "AKID-kv")
        self.assertEqual(updated["secret_key"], "kv-key")

        nested = dns_config_service.parse_import_payload({
            "mode": "real",
            "ttl": 2400,
            "credentials": {
                "SecretId": "AKID-nested",
                "SecretKey": "nested-key",
            },
        })
        self.assertEqual(nested["secret_id"], "AKID-nested")
        self.assertEqual(nested["secret_key"], "nested-key")

    def test_root_and_dns_parameters_are_fixed(self):
        database.import_ts_domain_pool([{
            "fqdn": "www.ts01.scjrdomain.com",
            "root_domain": "scjrdomain.com",
            "record_name": "www.ts01",
            "public_endpoint": "wss://www.ts01.scjrdomain.com/ws",
        }])
        config = dns_config_service.save_config({
            "root_domain": "example.com",
            "domain_suffix": "ts.example.com",
            "record_line": "境外",
            "ttl": 1200,
            "cooldown_seconds": 60,
        })
        self.assertEqual(config.root_domain, "scjrdomain.com")
        self.assertEqual(config.record_line, "默认")
        self.assertEqual(config.ttl, 600)
        self.assertEqual(config.cooldown_seconds, 1800)

    def test_runtime_changes_apply_without_restart(self):
        dns_config_service.save_config({
            "record_line": "境外",
            "ttl": 1800,
        })
        client = DNSPodClient()
        self.assertEqual(client.mode, "mock")
        self.assertEqual(client.line, "默认")
        self.assertEqual(client.ttl, 600)

    def test_allocation_cleanup_uses_original_config_snapshot(self):
        database.import_ts_domain_pool([{
            "fqdn": "www.ts01.scjrdomain.com",
            "root_domain": "scjrdomain.com",
            "record_name": "www.ts01",
            "public_endpoint": "wss://www.ts01.scjrdomain.com/ws",
        }])
        assignment = domain_pool.allocate_domain("snapshot-node", "8.8.8.8")
        self.assertEqual(assignment.dns_config.mode, "mock")
        dns_config_service.save_config({"mode": "disabled"})

        domain_pool.abort_allocation(assignment, "approval lost")

        entry = database.get_ts_domain_pool_entry(assignment["id"])
        self.assertEqual(entry["status"], "available")

    def test_mock_connection_test_updates_verification_status(self):
        result = dns_config_service.test_saved_config()
        self.assertTrue(result["ok"])
        self.assertTrue(result["config"]["verified"])
        self.assertTrue(result["config"]["last_test_at"])

    def test_error_redaction_removes_saved_credentials(self):
        config = dns_config_service.save_config({
            "mode": "real",
            "secret_id": "AKID-redact",
            "secret_key": "redact-key",
        })
        error = dns_config_service.redact_error(
            "request used AKID-redact and redact-key",
            config,
        )
        self.assertNotIn("AKID-redact", error)
        self.assertNotIn("redact-key", error)

    def test_permission_probe_runs_all_required_actions_and_cleans_up(self):
        client, fake = self._permission_client()

        result = client.test_permissions(
            "dnspod-check-success.scjrdomain.com"
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(fake.calls, ["describe", "create", "modify", "delete"])
        self.assertTrue(result["cleanup_ok"])
        self.assertFalse(result["residual_record"])
        self.assertEqual(
            [step["status"] for step in result["steps"]],
            ["passed", "passed", "passed", "passed"],
        )

    def test_no_data_response_is_treated_as_an_empty_query(self):
        client, fake = self._permission_client(describe_no_data=True)

        result = client.test_permissions(
            "dnspod-check-empty.scjrdomain.com"
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(fake.calls, ["describe", "create", "modify", "delete"])
        self.assertEqual(result["steps"][0]["status"], "passed")
        self.assertTrue(result["cleanup_ok"])

    def test_no_data_response_allows_upsert_to_create_a_record(self):
        client, fake = self._permission_client(describe_no_data=True)

        result = client.upsert_a_record(
            "www.ts99.scjrdomain.com",
            "8.8.8.8",
        )

        self.assertEqual(result.action, "created")
        self.assertEqual(result.record_id, "101")
        self.assertEqual(fake.calls, ["describe", "create"])

    def test_no_data_response_makes_delete_idempotent(self):
        client, fake = self._permission_client(describe_no_data=True)

        result = client.delete_a_record("www.ts99.scjrdomain.com")

        self.assertEqual(result.action, "not-found")
        self.assertEqual(result.record_id, "")
        self.assertEqual(fake.calls, ["describe"])

    def test_no_data_response_still_proves_api_connectivity(self):
        client, fake = self._permission_client(describe_no_data=True)

        message = client.test_connection()

        self.assertIn("API is reachable", message)
        self.assertIn("no records exist", message)
        self.assertEqual(fake.calls, ["describe"])

    def test_unknown_query_error_is_not_treated_as_no_data(self):
        client, fake = self._permission_client(fail_on="describe")

        with self.assertRaisesRegex(RuntimeError, "describe denied"):
            client.upsert_a_record("www.ts99.scjrdomain.com", "8.8.8.8")

        self.assertEqual(fake.calls, ["describe"])

    def test_permission_probe_cleans_up_after_modify_failure(self):
        client, fake = self._permission_client(fail_on="modify")

        result = client.test_permissions(
            "dnspod-check-modify.scjrdomain.com"
        )

        self.assertFalse(result["ok"])
        self.assertEqual(fake.calls, ["describe", "create", "modify", "delete"])
        self.assertTrue(result["cleanup_ok"])
        self.assertFalse(result["residual_record"])
        modify = next(step for step in result["steps"] if step["key"] == "modify")
        delete = next(step for step in result["steps"] if step["key"] == "delete")
        self.assertEqual(modify["status"], "failed")
        self.assertEqual(delete["status"], "passed")

    def test_permission_probe_skips_modify_when_create_fails(self):
        client, fake = self._permission_client(fail_on="create")

        result = client.test_permissions(
            "dnspod-check-create.scjrdomain.com"
        )

        self.assertFalse(result["ok"])
        self.assertNotIn("modify", fake.calls)
        self.assertNotIn("delete", fake.calls)
        create = next(step for step in result["steps"] if step["key"] == "create")
        modify = next(step for step in result["steps"] if step["key"] == "modify")
        self.assertEqual(create["status"], "failed")
        self.assertEqual(modify["status"], "skipped")

    def test_permission_probe_reports_residual_record_when_delete_fails(self):
        client, fake = self._permission_client(fail_on="delete")

        result = client.test_permissions(
            "dnspod-check-delete.scjrdomain.com"
        )

        self.assertFalse(result["ok"])
        self.assertEqual(fake.calls, ["describe", "create", "modify", "delete"])
        self.assertFalse(result["cleanup_ok"])
        self.assertTrue(result["residual_record"])
        self.assertEqual(result["record_id"], "101")

    def test_candidate_permission_test_does_not_persist_form_values(self):
        dns_config_service.get_runtime_config()
        before = database.get_dns_provider_config()
        seen = {}

        def fake_test(_client, test_domain):
            seen["secret_id"] = _client.secret_id
            seen["secret_key"] = _client.secret_key
            return {
                "ok": True,
                "test_domain": test_domain,
                "steps": [{
                    "key": "describe",
                    "label": "查询权限",
                    "status": "passed",
                    "detail": "ok",
                }],
                "cleanup_ok": True,
                "residual_record": False,
                "record_id": "",
                "error": "",
            }

        with patch.object(dns_config_service, "SM_DNSPOD_MODE", "real"):
            with patch.object(DNSPodClient, "test_permissions", new=fake_test):
                result = dns_config_service.test_candidate_permissions({
                    "secret_id": "AKID-unsaved",
                    "secret_key": "unsaved-key",
                })

        after = database.get_dns_provider_config()
        self.assertTrue(result["ok"])
        self.assertFalse(result["persisted"])
        self.assertEqual(before, after)
        self.assertEqual(seen["secret_id"], "AKID-unsaved")
        self.assertEqual(seen["secret_key"], "unsaved-key")
        self.assertNotIn("AKID-unsaved", json.dumps(result, ensure_ascii=False))
        self.assertNotIn("unsaved-key", json.dumps(result, ensure_ascii=False))

    def test_candidate_permission_errors_are_redacted(self):
        def fake_test(_client, test_domain):
            return {
                "ok": False,
                "test_domain": test_domain,
                "steps": [{
                    "key": "describe",
                    "label": "查询权限",
                    "status": "failed",
                    "detail": "AKID-candidate and candidate-key were rejected",
                }],
                "cleanup_ok": True,
                "residual_record": False,
                "record_id": "",
                "error": "candidate-key denied",
            }

        with patch.object(dns_config_service, "SM_DNSPOD_MODE", "real"):
            with patch.object(DNSPodClient, "test_permissions", new=fake_test):
                result = dns_config_service.test_candidate_permissions({
                    "secret_id": "AKID-candidate",
                    "secret_key": "candidate-key",
                })

        payload = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("AKID-candidate", payload)
        self.assertNotIn("candidate-key", payload)
        self.assertIn("***", payload)


if __name__ == "__main__":
    unittest.main()
