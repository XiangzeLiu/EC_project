import sys
import tempfile
import unittest
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

    def _upload(self, csrf, version, product="client", artifact_type="installer", name="SCClient.exe"):
        response = self.client.post(
            "/api/admin/software/upload",
            data={
                "product_type": product,
                "version": version,
                "artifact_type": artifact_type,
                "platform": "windows-x64",
            },
            files={"file": (name, b"software-bytes", "application/octet-stream")},
            headers={"X-SM-CSRF": csrf},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["data"]

    def test_trader_can_download_visible_client_but_never_ts(self):
        csrf = self._admin_login()
        client_release = self._upload(csrf, "v_2026082501")
        ts_release = self._upload(csrf, "v_2026082501", product="ts", name="TraderServer.zip")

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
        self.assertIn("下载 Client", page.text)
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
        portable = self._upload(csrf, "v_2026082501", artifact_type="portable", name="SCClient.zip")
        self.assertEqual(first["release_id"], portable["release_id"])
        self.assertEqual(len(portable["artifacts"]), 2)
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
