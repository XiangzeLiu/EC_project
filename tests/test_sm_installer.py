import hashlib
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


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
