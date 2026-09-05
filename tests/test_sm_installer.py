import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
HELPER_DIR = ROOT / "packaging" / "installer"
sys.path.insert(0, str(HELPER_DIR))

import sm_deploy_helper as helper


class SMInstallerHelperTests(unittest.TestCase):
    def test_command_output_decoding_tolerates_mixed_windows_bytes(self):
        result = helper._run(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(bytes([0x70, 0xA8, 0x20, 0x71]))",
            ]
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("p", result.stdout)
        self.assertIn("q", result.stdout)

    def test_failed_preflight_writes_a_diagnostic_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request = root / "request.ini"
            report = root / "report.json"
            request.write_text("[install]\nmode=invalid\n", encoding="utf-8")

            result = helper.main(
                [
                    "--preflight",
                    "--request-file",
                    str(request),
                    "--report-file",
                    str(report),
                ]
            )

            self.assertEqual(result, 1)
            payload = report.read_text(encoding="utf-8")
            self.assertIn('"status": "failed"', payload)
            self.assertIn("install mode must be fresh or upgrade", payload)

    def test_fixed_configuration_cannot_be_overridden(self):
        request = {"public_https_port": "443"}
        with self.assertRaises(helper.InstallerError):
            helper._validate_fixed_configuration(request)

    def test_request_paths_require_upgrade_source(self):
        with self.assertRaises(helper.InstallerError):
            helper._request_paths(
                {
                    "mode": "upgrade",
                    "app_dir": str(ROOT / "app"),
                    "data_dir": str(ROOT / "data"),
                }
            )

    def test_upgrade_source_cannot_be_the_target_data_directory(self):
        data = ROOT / "data"
        with self.assertRaises(helper.InstallerError):
            helper._request_paths(
                {
                    "mode": "upgrade",
                    "app_dir": str(ROOT / "app"),
                    "data_dir": str(data),
                    "source_data": str(data),
                }
            )

    def test_stale_transaction_cleanup_does_not_touch_external_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "runtime"
            app = root / "app"
            data = runtime / "data"
            source = root / "old-data"
            transaction = runtime / ".installer" / "transactions" / "tx"
            app.mkdir(parents=True)
            data.mkdir(parents=True)
            source.mkdir(parents=True)
            transaction.mkdir(parents=True)
            (app / "partial.exe").write_text("partial", encoding="utf-8")
            (data / "partial.db").write_text("partial", encoding="utf-8")
            (source / "server_manager.db").write_text("source", encoding="utf-8")
            state_path = runtime / ".installer" / "transaction.json"
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(
                json.dumps(
                    {
                        "product": "server_manager",
                        "phase": "prepared",
                        "app_dir": str(app),
                        "data_dir": str(data),
                        "source_dir": str(source),
                        "transaction_root": str(transaction),
                        "lock_path": str(state_path.parent / "install.lock"),
                    }
                ),
                encoding="utf-8",
            )
            (state_path.parent / "install.lock").write_text("tx", encoding="ascii")

            with mock.patch.object(helper, "_set_runtime_acl"):
                helper._discard_stale(
                    state_path,
                    runtime,
                    app,
                    data,
                )

            self.assertFalse(app.exists())
            self.assertFalse(data.exists())
            self.assertTrue(source.exists())
            self.assertTrue((source / "server_manager.db").exists())
            self.assertFalse(state_path.exists())
            self.assertFalse((state_path.parent / "install.lock").exists())

    def test_committed_stale_cleanup_keeps_deployed_app_and_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "runtime"
            app = root / "app"
            data = runtime / "data"
            transactions = runtime / ".installer" / "transactions" / "committed-tx"
            app.mkdir(parents=True)
            data.mkdir(parents=True)
            transactions.mkdir(parents=True)
            (app / "ServerManager.exe").write_text("live", encoding="utf-8")
            (data / "server_manager.db").write_text("live", encoding="utf-8")
            state_path = runtime / ".installer" / "transaction.json"
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(
                json.dumps(
                    {
                        "product": "server_manager",
                        "phase": "committed",
                        "app_dir": str(app),
                        "data_dir": str(data),
                        "transaction_root": str(transactions),
                    }
                ),
                encoding="utf-8",
            )
            (state_path.parent / "install.lock").write_text("tx", encoding="ascii")

            with mock.patch.object(helper, "_set_runtime_acl"):
                helper._discard_stale(state_path, runtime, app, data)

            self.assertTrue((app / "ServerManager.exe").exists())
            self.assertTrue((data / "server_manager.db").exists())
            self.assertFalse((state_path.parent / "transactions").exists())
            self.assertFalse(state_path.exists())
            self.assertFalse((state_path.parent / "install.lock").exists())

    def test_stale_cleanup_does_not_follow_state_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "runtime"
            app = root / "app"
            data = runtime / "data"
            unrelated = root / "unrelated"
            app.mkdir(parents=True)
            data.mkdir(parents=True)
            unrelated.mkdir()
            (unrelated / "keep.txt").write_text("keep", encoding="utf-8")
            state_path = runtime / ".installer" / "transaction.json"
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(
                json.dumps(
                    {
                        "product": "server_manager",
                        "phase": "prepared",
                        "app_dir": str(unrelated),
                        "data_dir": str(unrelated),
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch.object(helper, "_set_runtime_acl"):
                helper._discard_stale(state_path, runtime, app, data)

            self.assertTrue((unrelated / "keep.txt").exists())
            self.assertFalse(app.exists())
            self.assertFalse(data.exists())

    def test_stale_cleanup_rejects_state_file_outside_runtime_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "runtime"
            state_path = root / "elsewhere" / "transaction.json"
            app = root / "app"
            data = runtime / "data"
            state_path.parent.mkdir(parents=True)

            with self.assertRaises(helper.InstallerError):
                helper._discard_stale(state_path, runtime, app, data)

    def test_disk_usage_check_accepts_a_new_data_parent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing_parent = root / "new" / "runtime"
            self.assertFalse(missing_parent.exists())
            self.assertTrue(helper._existing_disk_path(missing_parent).samefile(root))

    def test_source_database_is_backed_up_and_source_remains_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source-data"
            stage = root / "stage-data"
            source.mkdir()
            certificates = source / "certificates"
            certificates.mkdir()
            (certificates / "server.crt").write_text("test certificate", encoding="utf-8")
            (certificates / "server.key").write_text("test private key", encoding="utf-8")
            source_db = source / "server_manager.db"

            init_code = (
                "import sys, sqlite3; "
                f"sys.path.insert(0, {str(ROOT / 'Server_manager')!r}); "
                "import database; "
                f"database._DB_PATH = {str(source_db)!r}; "
                "database.init_db(); "
                "conn=sqlite3.connect(database._DB_PATH); "
                "conn.execute(\"INSERT INTO accounts (username, password_hash, role, status) VALUES (?, ?, ?, ?)\", (\"migration-user\", \"hash\", \"trader\", \"active\")); "
                "conn.commit(); conn.close()"
            )
            env = os.environ.copy()
            env.update(
                {
                    "SM_ENVIRONMENT": "selftest",
                    "SM_DATA_DIR": str(source),
                    "SERVER_MANAGER_DB_PATH": str(source_db),
                    "SM_SOFTWARE_STORAGE_DIR": str(source / "software"),
                    "SM_CADDY_AUTO_MANAGE": "0",
                    "SM_CADDY_REQUIRED": "0",
                    "SM_DNSPOD_MODE": "disabled",
                    "SM_BOOTSTRAP_ADMIN_PASSWORD": "Test-Only-Password-123",
                }
            )
            result = subprocess.run(
                [sys.executable, "-c", init_code],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            before = hashlib.sha256(source_db.read_bytes()).hexdigest()

            request = {
                "mode": "upgrade",
                "dnspod_secret_id": "installer-test-id",
                "dnspod_secret_key": "installer-test-key",
                    "bootstrap_admin_username": "admin",
                    "bootstrap_admin_password": "Test-Only-Password-123",
                    "certificate_source": str(certificates / "server.crt"),
                    "key_source": str(certificates / "server.key"),
                }
            stage_code = (
                "import sys, sqlite3; "
                f"sys.path.insert(0, {str(HELPER_DIR)!r}); "
                "import sm_deploy_helper as h; "
                f"h._prepare_stage_data(h.Path({str(source)!r}), h.Path({str(stage)!r}), {request!r}); "
                f"conn=sqlite3.connect({str(stage / 'server_manager.db')!r}); "
                "print(conn.execute(\"SELECT COUNT(*) FROM accounts WHERE username='migration-user'\").fetchone()[0]); conn.close()"
            )
            result = subprocess.run(
                [sys.executable, "-c", stage_code],
                cwd=ROOT,
                env=os.environ.copy(),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout.strip().splitlines()[-1], "1")
            self.assertEqual(hashlib.sha256(source_db.read_bytes()).hexdigest(), before)

    def test_inno_and_build_pipeline_reference_the_new_helper(self):
        inno = (ROOT / "packaging" / "inno" / "server_manager.iss").read_text(
            encoding="utf-8"
        )
        packager = (ROOT / "packaging" / "build_server.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("InstallerHelper", inno)
        self.assertIn("ExtractTemporaryFile('SC_SM_InstallerHelper.exe')", inno)
        self.assertIn("--prepare", inno)
        self.assertIn("--commit", inno)
        self.assertIn("sm_deploy_helper.py", packager)
        self.assertIn("SC_SM_InstallerHelper", packager)


if __name__ == "__main__":
    unittest.main()
