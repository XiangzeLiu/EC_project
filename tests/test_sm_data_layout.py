import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Server_manager"))
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SM_DNSPOD_MODE", "mock")
os.environ.setdefault("SM_LOG_LEVEL", "CRITICAL")

import database
from config import persist_data_metadata
from data_layout import (
    DataLayoutError,
    deployment_path,
    load_deployment_config,
    load_data_manifest,
    relative_path,
    resolve_relative_path,
    validate_data_paths,
    write_deployment_config,
)


class SMDataLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.data_dir = self.root / "data"
        self.original_db_path = database._DB_PATH

    def tearDown(self):
        database._DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_deployment_config_is_atomic_and_whitelists_runtime_values(self):
        payload = write_deployment_config(
            self.data_dir,
            {
                "server_port": 18800,
                "public_https_port": 4430,
                "public_base_url": "https://scjrdomain.com:4430",
                "secret_key": "must-not-be-persisted",
            },
        )

        self.assertEqual(payload["server_port"], 18800)
        self.assertNotIn("secret_key", payload)
        self.assertEqual(
            load_deployment_config(self.data_dir)["public_https_port"],
            4430,
        )
        self.assertTrue(deployment_path(self.data_dir).is_file())
        self.assertFalse(list(self.data_dir.glob("*.tmp")))

    def test_relative_paths_reject_escape(self):
        self.assertEqual(relative_path(self.data_dir, self.data_dir / "software"), "software")
        self.assertEqual(
            resolve_relative_path(self.data_dir, "certificates/site.crt"),
            (self.data_dir / "certificates/site.crt").resolve(),
        )
        with self.assertRaises(DataLayoutError):
            resolve_relative_path(self.data_dir, "../outside.key")
        self.assertEqual(
            validate_data_paths(
                self.data_dir,
                self.data_dir / "server_manager.db",
                self.data_dir / "software",
                str(self.root / "outside.crt"),
                str(self.data_dir / "certificates/site.key"),
            ),
            ["certificate must be inside SM_DATA_DIR"],
        )

    def test_database_init_writes_portable_metadata(self):
        database._DB_PATH = str(self.data_dir / "server_manager.db")
        database.init_db()
        persist_data_metadata(
            database.DB_SCHEMA_VERSION,
            data_dir=self.data_dir,
        )

        manifest = load_data_manifest(self.data_dir)
        self.assertIsNotNone(manifest)
        self.assertEqual(manifest["database_schema_version"], database.DB_SCHEMA_VERSION)
        self.assertEqual(manifest["paths"]["database"], "server_manager.db")
        self.assertTrue((self.data_dir / "software").is_dir())
        self.assertTrue((self.data_dir / "certificates").is_dir())
        self.assertTrue((self.data_dir / "logs").is_dir())

        info = database.validate_database(database._DB_PATH, allow_missing=False)
        self.assertEqual(info["integrity"], "ok")
        self.assertEqual(info["schema_version"], database.DB_SCHEMA_VERSION)

    def test_backup_uses_consistent_copy_and_does_not_change_source(self):
        database._DB_PATH = str(self.data_dir / "server_manager.db")
        database.init_db()
        persist_data_metadata(
            database.DB_SCHEMA_VERSION,
            data_dir=self.data_dir,
        )
        conn = sqlite3.connect(database._DB_PATH)
        try:
            conn.execute(
                "INSERT INTO accounts (username, password_hash, role, status) VALUES (?, ?, ?, ?)",
                ("backup-test", "hash", "trader", "active"),
            )
            conn.commit()
        finally:
            conn.close()

        source_before = database.inspect_database(database._DB_PATH)
        target = self.root / "import-staging" / "server_manager.db"
        copied = database.backup_database(database._DB_PATH, target)
        self.assertEqual(copied["integrity"], "ok")
        self.assertEqual(copied["schema_version"], source_before["schema_version"])
        check = sqlite3.connect(target)
        try:
            self.assertEqual(
                check.execute(
                    "SELECT COUNT(*) FROM accounts WHERE username='backup-test'"
                ).fetchone()[0],
                1,
            )
        finally:
            check.close()
        self.assertEqual(database.inspect_database(database._DB_PATH), source_before)

    def test_newer_database_is_rejected_before_migration(self):
        path = self.data_dir / "future.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA user_version = 11")
            conn.commit()
        finally:
            conn.close()

        with self.assertRaises(database.DatabaseValidationError):
            database.validate_database(path, allow_missing=False)

    def test_manifest_with_future_schema_is_rejected(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "data_manifest.json").write_text(
            json.dumps(
                {
                    "product": "server_manager",
                    "format_version": 1,
                    "database_schema_version": database.DB_SCHEMA_VERSION + 1,
                    "paths": {"database": "server_manager.db"},
                }
            ),
            encoding="utf-8",
        )
        database._DB_PATH = str(self.data_dir / "server_manager.db")
        with self.assertRaises(database.DatabaseValidationError):
            database.init_db()

    def test_runtime_config_reads_deployment_file_from_data_directory(self):
        write_deployment_config(
            self.data_dir,
            {
                "server_port": 19999,
                "public_http_port": 7999,
                "public_https_port": 4430,
                "public_base_url": "https://scjrdomain.com:4430",
                "caddy_admin": "127.0.0.1:2119",
            },
        )
        env = os.environ.copy()
        env.update(
            {
                "SM_ENVIRONMENT": "development",
                "SM_DATA_DIR": str(self.data_dir),
                "SERVER_PORT": "17777",
                "SM_PUBLIC_HTTP_PORT": "7888",
                "SM_CADDY_ADMIN": "127.0.0.1:2219",
                "SM_LOG_LEVEL": "CRITICAL",
            }
        )
        code = (
            "import sys; "
            f"sys.path.insert(0, {str(ROOT / 'Server_manager')!r}); "
            "import config; "
            "print(config.SERVER_PORT, config.SM_PUBLIC_HTTP_PORT, config.SM_CADDY_ADMIN)"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("19999 7999 127.0.0.1:2119", result.stdout)

    def test_production_config_accepts_migrated_dnspod_credentials(self):
        database._DB_PATH = str(self.data_dir / "server_manager.db")
        database.init_db()
        conn = sqlite3.connect(database._DB_PATH)
        try:
            conn.execute(
                """
                INSERT INTO dns_provider_config (
                    id, provider, mode, root_domain, secret_id, secret_key,
                    record_line, ttl, cooldown_seconds, verified
                ) VALUES (1, 'dnspod', 'real', 'scjrdomain.com', 'AKID-migrated',
                          'migrated-key', '默认', 600, 1800, 1)
                """
            )
            conn.commit()
        finally:
            conn.close()
        write_deployment_config(
            self.data_dir,
            {
                "server_host": "127.0.0.1",
                "server_port": 18800,
                "public_http_port": 8800,
                "public_https_port": 4430,
                "public_base_url": "https://scjrdomain.com:4430",
                "caddy_admin": "127.0.0.1:2019",
                "caddy_auto_manage": True,
                "caddy_required": True,
                "cookie_secure": True,
                "domain_pool_required": True,
                "domain_cooldown_seconds": 1800,
                "dnspod_mode": "real",
            },
        )
        env = os.environ.copy()
        for key in ("SM_DNSPOD_SECRET_ID", "SM_DNSPOD_SECRET_KEY"):
            env.pop(key, None)
        env.update(
            {
                "SM_ENVIRONMENT": "production",
                "SM_DATA_DIR": str(self.data_dir),
                "SERVER_MANAGER_DB_PATH": str(self.data_dir / "server_manager.db"),
                "SM_SOFTWARE_STORAGE_DIR": str(self.data_dir / "software"),
                "SM_LOG_LEVEL": "CRITICAL",
            }
        )
        code = (
            "import sys; "
            f"sys.path.insert(0, {str(ROOT / 'Server_manager')!r}); "
            "import config; "
            "print(config.production_config_errors("
            f"{str(self.data_dir / 'server_manager.db')!r}))"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("[]", result.stdout)


if __name__ == "__main__":
    unittest.main()
