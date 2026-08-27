import sys
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Server_manager"))
sys.path.insert(0, str(ROOT))

import database
import main as sm_main
from services import software_release_service


class SoftwareCenterTests(unittest.TestCase):
    def setUp(self):
        self.temp_db = tempfile.TemporaryDirectory()
        self.temp_storage = tempfile.TemporaryDirectory()
        self.original_db_path = database._DB_PATH
        self.original_storage = software_release_service.SM_SOFTWARE_STORAGE_DIR
        self.original_max_size = software_release_service.SM_SOFTWARE_MAX_UPLOAD_BYTES
        database._DB_PATH = str(Path(self.temp_db.name) / "sm.db")
        software_release_service.SM_SOFTWARE_STORAGE_DIR = Path(self.temp_storage.name)
        software_release_service.SM_SOFTWARE_MAX_UPLOAD_BYTES = 1024 * 1024
        database.init_db()
        database.create_account("software-admin", "admin-pw", role="admin")
        database.create_account("software-trader", "trader-pw", role="trader")
        sm_main._admin_sessions.clear()
        self.client = TestClient(sm_main.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        sm_main._admin_sessions.clear()
        database._DB_PATH = self.original_db_path
        software_release_service.SM_SOFTWARE_STORAGE_DIR = self.original_storage
        software_release_service.SM_SOFTWARE_MAX_UPLOAD_BYTES = self.original_max_size
        self.temp_db.cleanup()
        self.temp_storage.cleanup()

    def _admin_login(self):
        response = self.client.post(
            "/admin/login",
            data={"username": "software-admin", "password": "admin-pw"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        sid = self.client.cookies.get("admin_sid")
        return sm_main._admin_sessions[sid]["csrf_token"]

    def _upload_response(
        self,
        csrf,
        version,
        product="client",
        artifact_type="installer",
        name="SCClient.exe",
        content=b"software-bytes",
        replace=False,
        expected_artifact_id="",
    ):
        return self.client.post(
            "/api/admin/software/upload",
            data={
                "product_type": product,
                "version": version,
                "artifact_type": artifact_type,
                "platform": "windows-x64",
                "replace": "true" if replace else "false",
                "expected_artifact_id": expected_artifact_id,
            },
            files={"file": (name, content, "application/octet-stream")},
            headers={"X-SM-CSRF": csrf},
        )

    def _upload(self, csrf, version, **kwargs):
        response = self._upload_response(csrf, version, **kwargs)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["data"]

    def _insert_legacy_portable(self, release: dict, content=b"legacy-portable"):
        release_id = release["release_id"]
        artifact_id = "art_legacy_portable"
        storage_key = f"client/{release_id}/legacy.zip"
        path = Path(self.temp_storage.name) / storage_key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        now = datetime.now(timezone.utc).isoformat()
        record = database.create_software_release_record(
            {
                "release_id": release_id,
                "product_type": "client",
                "version": release["version"],
                "platform": release["platform"],
                "created_by": "legacy",
                "created_at": now,
                "updated_at": now,
            },
            {
                "artifact_id": artifact_id,
                "artifact_type": "portable",
                "file_name": "legacy.zip",
                "storage_key": storage_key,
                "file_size": len(content),
                "sha256": "legacy-sha",
                "created_at": now,
            },
        )
        self.assertIsNotNone(record)
        return artifact_id

    def test_download_shortcut_routes_by_trader_session(self):
        for path in ("/download", "/download/"):
            response = self.client.get(path, follow_redirects=False)
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers["location"], "/software/login")

        self._admin_login()
        admin_response = self.client.get("/download", follow_redirects=False)
        self.assertEqual(admin_response.status_code, 302)
        self.assertEqual(admin_response.headers["location"], "/software/login")
        self.client.get("/admin/logout", follow_redirects=False)

        login = self.client.post(
            "/software/login",
            data={"username": "software-trader", "password": "trader-pw"},
            follow_redirects=False,
        )
        self.assertEqual(login.status_code, 302)

        trader_response = self.client.get("/download", follow_redirects=False)
        self.assertEqual(trader_response.status_code, 302)
        self.assertEqual(trader_response.headers["location"], "/software/trader")
        self.assertEqual(self.client.get("/download").status_code, 200)

    def test_trader_can_download_visible_client_but_never_ts(self):
        csrf = self._admin_login()
        client_release = self._upload(csrf, "v_2026082501")
        ts_release = self._upload(
            csrf,
            "v_2026082501",
            product="ts",
            artifact_type="archive",
            name="TraderServer.zip",
        )

        self.assertNotIn("storage_key", client_release["artifacts"][0])
        self.assertEqual(
            self.client.post(
                f"/api/admin/software/{client_release['release_id']}/status",
                json={"status": "published"},
                headers={"X-SM-CSRF": csrf},
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.post(
                f"/api/admin/software/{client_release['release_id']}/visibility",
                json={"visible": True},
                headers={"X-SM-CSRF": csrf},
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.post(
                f"/api/admin/software/{client_release['release_id']}/default",
                headers={"X-SM-CSRF": csrf},
            ).status_code,
            200,
        )

        self.client.get("/admin/logout", follow_redirects=False)
        login = self.client.post(
            "/software/login",
            data={"username": "software-trader", "password": "trader-pw"},
            follow_redirects=False,
        )
        self.assertEqual(login.status_code, 302)

        page = self.client.get("/software/trader")
        self.assertEqual(page.status_code, 200)
        self.assertIn("下载安装器", page.text)
        self.assertNotIn("TraderServer.zip", page.text)

        downloaded = self.client.get(f"/software/releases/{client_release['release_id']}/download")
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.content, b"software-bytes")

        blocked = self.client.get(f"/software/releases/{ts_release['release_id']}/download", follow_redirects=False)
        self.assertEqual(blocked.status_code, 303)
        self.assertEqual(
            self.client.get("/api/admin/software/releases?product=ts").status_code,
            401,
        )

    def test_release_variants_share_version_and_retention_archives_oldest(self):
        csrf = self._admin_login()
        first = self._upload(csrf, "v_2026082501")
        archive = self._upload(csrf, "v_2026082501", artifact_type="archive", name="SCClient.zip")
        self.assertEqual(first["release_id"], archive["release_id"])
        self.assertEqual(len(archive["artifacts"]), 2)
        self.assertEqual(
            self.client.post(
                f"/api/admin/software/{first['release_id']}/status",
                json={"status": "published"},
                headers={"X-SM-CSRF": csrf},
            ).status_code,
            200,
        )

        published = []
        for index in range(2, 6):
            release = self._upload(csrf, f"v_202608250{index}")
            published.append(release["release_id"])
            response = self.client.post(
                f"/api/admin/software/{release['release_id']}/status",
                json={"status": "published"},
                headers={"X-SM-CSRF": csrf},
            )
            self.assertEqual(response.status_code, 200)

        records = database.list_software_releases("client")
        by_id = {item["release_id"]: item for item in records}
        self.assertEqual(by_id[first["release_id"]]["status"], "archived")
        self.assertEqual(sum(item["status"] == "published" for item in records), 3)

        self.client.post(
            f"/api/admin/software/{published[-1]}/status",
            json={"status": "offline"},
            headers={"X-SM-CSRF": csrf},
        )
        deleted = self.client.delete(
            f"/api/admin/software/{published[-1]}",
            headers={"X-SM-CSRF": csrf},
        )
        self.assertEqual(deleted.status_code, 200)

    def test_same_type_requires_confirmation_and_published_replace_is_atomic(self):
        csrf = self._admin_login()
        release = self._upload(csrf, "v_2026082701", content=b"old-installer")
        first_artifact = release["artifacts"][0]

        conflict = self._upload_response(
            csrf,
            "v_2026082701",
            content=b"new-installer",
        )
        self.assertEqual(conflict.status_code, 409)
        conflict_body = conflict.json()
        self.assertEqual(conflict_body["code"], "SOFTWARE_ARTIFACT_CONFLICT")
        self.assertEqual(conflict_body["conflict"]["artifact"]["artifact_id"], first_artifact["artifact_id"])

        self.client.post(
            f"/api/admin/software/{release['release_id']}/status",
            json={"status": "published"},
            headers={"X-SM-CSRF": csrf},
        )
        self.client.post(
            f"/api/admin/software/{release['release_id']}/visibility",
            json={"visible": True},
            headers={"X-SM-CSRF": csrf},
        )
        self.client.post(
            f"/api/admin/software/{release['release_id']}/default",
            headers={"X-SM-CSRF": csrf},
        )

        replaced_response = self._upload_response(
            csrf,
            "v_2026082701",
            name="SCClient-new.exe",
            content=b"new-installer",
            replace=True,
            expected_artifact_id=first_artifact["artifact_id"],
        )
        self.assertEqual(replaced_response.status_code, 200, replaced_response.text)
        self.assertEqual(replaced_response.json()["action"], "replaced")
        replaced = replaced_response.json()["data"]
        self.assertEqual(replaced["status"], "published")
        self.assertTrue(replaced["trader_visible"])
        self.assertTrue(replaced["is_default"])
        current = replaced["artifacts"][0]
        self.assertEqual(current["revision"], 2)
        self.assertNotEqual(current["artifact_id"], first_artifact["artifact_id"])

        stale_confirmation = self._upload_response(
            csrf,
            "v_2026082701",
            content=b"stale-overwrite",
            replace=True,
            expected_artifact_id=first_artifact["artifact_id"],
        )
        self.assertEqual(stale_confirmation.status_code, 409)
        self.assertEqual(
            stale_confirmation.json()["conflict"]["artifact"]["artifact_id"],
            current["artifact_id"],
        )
        history = database.list_software_artifact_history(release["release_id"])
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["artifact_id"], first_artifact["artifact_id"])
        oldest_history_path = Path(self.temp_storage.name) / history[0]["storage_key"]
        self.assertTrue(oldest_history_path.is_file())

        downloaded = self.client.get(
            f"/admin/software/releases/{release['release_id']}/download?artifact_id={current['artifact_id']}"
        )
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.content, b"new-installer")
        wrong_artifact = self.client.get(
            f"/admin/software/releases/{release['release_id']}/download?artifact_id=art_missing"
        )
        self.assertEqual(wrong_artifact.status_code, 404)

        second_replace = self._upload_response(
            csrf,
            "v_2026082701",
            name="SCClient-newer.exe",
            content=b"newest-installer",
            replace=True,
            expected_artifact_id=current["artifact_id"],
        )
        self.assertEqual(second_replace.status_code, 200, second_replace.text)
        newest = second_replace.json()["data"]["artifacts"][0]
        self.assertEqual(newest["revision"], 3)
        latest_history = database.list_software_artifact_history(release["release_id"])
        self.assertEqual(len(latest_history), 1)
        self.assertEqual(latest_history[0]["artifact_id"], current["artifact_id"])
        self.assertFalse(oldest_history_path.exists())

    def test_deleted_version_is_reused_as_a_clean_draft(self):
        csrf = self._admin_login()
        release = self._upload(csrf, "v_2026082702", content=b"deleted-installer")
        release_id = release["release_id"]
        self.client.post(
            f"/api/admin/software/{release_id}/status",
            json={"status": "published"},
            headers={"X-SM-CSRF": csrf},
        )
        self.client.post(
            f"/api/admin/software/{release_id}/status",
            json={"status": "offline"},
            headers={"X-SM-CSRF": csrf},
        )
        stored = database.get_software_release(release_id)
        old_path = Path(self.temp_storage.name) / stored["artifacts"][0]["storage_key"]
        self.assertTrue(old_path.is_file())

        deleted = self.client.delete(
            f"/api/admin/software/{release_id}",
            headers={"X-SM-CSRF": csrf},
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertFalse(old_path.exists())

        restored_response = self._upload_response(
            csrf,
            "v_2026082702",
            artifact_type="archive",
            name="SCClient.zip",
            content=b"restored-archive",
        )
        self.assertEqual(restored_response.status_code, 200, restored_response.text)
        self.assertEqual(restored_response.json()["action"], "restored")
        restored = restored_response.json()["data"]
        self.assertEqual(restored["release_id"], release_id)
        self.assertEqual(restored["status"], "draft")
        self.assertFalse(restored["trader_visible"])
        self.assertFalse(restored["is_default"])
        self.assertEqual(restored["published_at"], "")
        self.assertEqual([item["artifact_type"] for item in restored["artifacts"]], ["archive"])

    def test_artifact_type_validation_and_single_artifact_delete(self):
        csrf = self._admin_login()
        invalid_installer = self._upload_response(
            csrf,
            "v_2026082703",
            artifact_type="installer",
            name="SCClient.zip",
        )
        self.assertEqual(invalid_installer.status_code, 400)
        invalid_archive = self._upload_response(
            csrf,
            "v_2026082703",
            artifact_type="archive",
            name="SCClient.exe",
        )
        self.assertEqual(invalid_archive.status_code, 400)
        legacy_upload = self._upload_response(
            csrf,
            "v_2026082703",
            artifact_type="portable",
            name="SCClient.zip",
        )
        self.assertEqual(legacy_upload.status_code, 400)

        release = self._upload(csrf, "v_2026082703", content=b"installer")
        release = self._upload(
            csrf,
            "v_2026082703",
            artifact_type="archive",
            name="SCClient.zip",
            content=b"archive",
        )
        artifacts = {item["artifact_type"]: item for item in release["artifacts"]}
        self.client.post(
            f"/api/admin/software/{release['release_id']}/status",
            json={"status": "published"},
            headers={"X-SM-CSRF": csrf},
        )
        deleted = self.client.delete(
            f"/api/admin/software/{release['release_id']}/artifacts/{artifacts['archive']['artifact_id']}",
            headers={"X-SM-CSRF": csrf},
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(
            [item["artifact_type"] for item in deleted.json()["data"]["artifacts"]],
            ["installer"],
        )
        blocked = self.client.delete(
            f"/api/admin/software/{release['release_id']}/artifacts/{artifacts['installer']['artifact_id']}",
            headers={"X-SM-CSRF": csrf},
        )
        self.assertEqual(blocked.status_code, 400)
        self.assertIn("至少需要保留一个", blocked.json()["error"])
        missing_csrf = self.client.delete(
            f"/api/admin/software/{release['release_id']}/artifacts/{artifacts['installer']['artifact_id']}"
        )
        self.assertEqual(missing_csrf.status_code, 403)
        other = self._upload(csrf, "v_2026082705")
        wrong_release = self.client.delete(
            f"/api/admin/software/{other['release_id']}/artifacts/{artifacts['installer']['artifact_id']}",
            headers={"X-SM-CSRF": csrf},
        )
        self.assertEqual(wrong_release.status_code, 400)
        self.assertIsNotNone(
            next(
                item
                for item in database.get_software_release(release["release_id"])["artifacts"]
                if item["artifact_id"] == artifacts["installer"]["artifact_id"]
            )
        )

    def test_trader_downloads_each_format_and_legacy_portable_remains_read_only(self):
        csrf = self._admin_login()
        release = self._upload(csrf, "v_2026082704", content=b"installer-content")
        release = self._upload(
            csrf,
            "v_2026082704",
            artifact_type="archive",
            name="SCClient.zip",
            content=b"archive-content",
        )
        legacy_id = self._insert_legacy_portable(release)
        release = database.get_software_release(release["release_id"])
        artifacts = {item["artifact_type"]: item for item in release["artifacts"]}
        self.client.post(
            f"/api/admin/software/{release['release_id']}/status",
            json={"status": "published"},
            headers={"X-SM-CSRF": csrf},
        )
        self.client.post(
            f"/api/admin/software/{release['release_id']}/visibility",
            json={"visible": True},
            headers={"X-SM-CSRF": csrf},
        )
        legacy_delete = self.client.delete(
            f"/api/admin/software/{release['release_id']}/artifacts/{legacy_id}",
            headers={"X-SM-CSRF": csrf},
        )
        self.assertEqual(legacy_delete.status_code, 400)

        self.client.get("/admin/logout", follow_redirects=False)
        self.client.post(
            "/software/login",
            data={"username": "software-trader", "password": "trader-pw"},
            follow_redirects=False,
        )
        page = self.client.get("/software/trader")
        self.assertIn("下载安装器", page.text)
        self.assertIn("下载压缩包", page.text)
        self.assertIn("下载便携包", page.text)
        expected = {
            "installer": b"installer-content",
            "archive": b"archive-content",
            "portable": b"legacy-portable",
        }
        for artifact_type, content in expected.items():
            downloaded = self.client.get(
                f"/software/releases/{release['release_id']}/download?artifact_id={artifacts[artifact_type]['artifact_id']}"
            )
            self.assertEqual(downloaded.status_code, 200)
            self.assertEqual(downloaded.content, content)
        missing = self.client.get(
            f"/software/releases/{release['release_id']}/download?artifact_id=art_missing",
            follow_redirects=False,
        )
        self.assertEqual(missing.status_code, 303)

    def test_admin_mutations_require_csrf_and_default_requires_visible_published_client(self):
        csrf = self._admin_login()
        release = self._upload(csrf, "v_2026082601")
        missing_csrf = self.client.post(
            f"/api/admin/software/{release['release_id']}/status",
            json={"status": "published"},
        )
        self.assertEqual(missing_csrf.status_code, 403)
        not_visible = self.client.post(
            f"/api/admin/software/{release['release_id']}/default",
            headers={"X-SM-CSRF": csrf},
        )
        self.assertEqual(not_visible.status_code, 400)


class SoftwareSchemaMigrationTests(unittest.TestCase):
    def test_v9_database_receives_artifact_revision_and_history_schema(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = sqlite3.connect(str(Path(temp_dir) / "legacy.db"))
            conn.row_factory = sqlite3.Row
            try:
                conn.executescript(database.V7_SCHEMA_SQL)
                conn.execute(
                    """
                    INSERT INTO software_releases (
                        release_id, product_type, version, platform, status,
                        created_by, created_at, updated_at
                    ) VALUES ('rel_legacy', 'client', 'v_legacy', 'windows-x64',
                              'draft', 'admin', '2026-08-01T00:00:00+00:00',
                              '2026-08-01T00:00:00+00:00')
                    """
                )
                conn.execute(
                    """
                    INSERT INTO software_artifacts (
                        artifact_id, release_id, artifact_type, file_name,
                        storage_key, file_size, sha256, created_at
                    ) VALUES ('art_legacy', 'rel_legacy', 'installer', 'legacy.exe',
                              'client/rel_legacy/legacy.exe', 10, 'old-sha',
                              '2026-08-01T00:00:00+00:00')
                    """
                )
                conn.execute("PRAGMA user_version=9")
                reports = database.run_migrations(conn)
                conn.commit()

                self.assertEqual(database._get_user_version(conn), database.DB_SCHEMA_VERSION_V10)
                columns = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(software_artifacts)").fetchall()
                }
                self.assertIn("revision", columns)
                self.assertIn("updated_at", columns)
                migrated = conn.execute(
                    "SELECT revision, updated_at FROM software_artifacts WHERE artifact_id='art_legacy'"
                ).fetchone()
                self.assertEqual(migrated["revision"], 1)
                self.assertEqual(migrated["updated_at"], "2026-08-01T00:00:00+00:00")
                self.assertTrue(database._table_exists(conn, "software_artifact_history"))
                self.assertEqual(reports[-1]["to_version"], database.DB_SCHEMA_VERSION_V10)
            finally:
                conn.close()
