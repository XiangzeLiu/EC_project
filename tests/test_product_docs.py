import os
import sys
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Server_manager"))
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SM_DNSPOD_MODE", "mock")
os.environ.setdefault("SM_ALLOWED_HOSTS", "testserver,localhost,127.0.0.1")
os.environ.setdefault("SM_LOG_LEVEL", "CRITICAL")
os.environ.setdefault("SM_CADDY_AUTO_MANAGE", "0")

import main as sm_main
from fastapi.testclient import TestClient


class ProductDocsTests(unittest.TestCase):
    def setUp(self):
        sm_main._admin_sessions.clear()
        self.client = TestClient(sm_main.app)

    def tearDown(self):
        self.client.close()
        sm_main._admin_sessions.clear()

    def test_product_docs_require_admin_session(self):
        response = self.client.get("/admin/product-docs", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/admin/login")

        content_response = self.client.get(
            "/admin/product-docs/content",
            follow_redirects=False,
        )
        self.assertEqual(content_response.status_code, 401)
        self.assertEqual(content_response.json()["redirect"], "/admin/login")

        download_response = self.client.get(
            "/admin/product-docs/download",
            follow_redirects=False,
        )
        self.assertEqual(download_response.status_code, 302)
        self.assertEqual(download_response.headers["location"], "/admin/login")

    def test_product_docs_are_loaded_inside_dashboard(self):
        session_id = "product-docs-test-session"
        sm_main._admin_sessions[session_id] = {
            "id": 1,
            "username": "maintainer",
            "role": "super_admin",
            "created_at": time.time(),
        }
        self.client.cookies.set("admin_sid", session_id)

        old_url = self.client.get("/admin/product-docs", follow_redirects=False)
        self.assertEqual(old_url.status_code, 302)
        self.assertEqual(old_url.headers["location"], "/admin/dashboard#docs")

        dashboard = self.client.get("/admin/dashboard")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn('data-module="docs"', dashboard.text)
        self.assertIn('id="mod-docs"', dashboard.text)
        self.assertIn("/admin/product-docs/content", dashboard.text)
        self.assertNotIn("location.href='/admin/product-docs'", dashboard.text)

        response = self.client.get("/admin/product-docs/content")

        self.assertEqual(response.status_code, 200)
        self.assertIn("生产维护版", response.text)
        self.assertIn("系统总览与数据流", response.text)
        self.assertIn('id="product-docs-root"', response.text)
        self.assertIn('data-product-docs-version="2"', response.text)
        self.assertIn('href="/admin/product-docs/download"', response.text)
        self.assertIn("下载 PDF", response.text)
        self.assertNotIn("data-product-docs-print", response.text)
        self.assertNotIn("data-product-docs-toc", response.text)
        self.assertNotIn("product-docs-toc", response.text)
        self.assertEqual(response.text.count('class="product-docs-nav-group'), 5)
        self.assertIn("product-docs-nav-sections", response.text)
        self.assertIn("data-doc-section-target", response.text)
        self.assertIn("product-docs-article active", response.text)
        self.assertIn("product-docs-article\" data-doc-id=", response.text)
        self.assertIn("width:70.7107%", response.text)
        self.assertIn("<svg", response.text)
        self.assertNotIn("<html", response.text.lower())
        self.assertNotIn("<body", response.text.lower())
        self.assertNotIn("mermaid", response.text.lower())
        self.assertNotIn("cdn.jsdelivr.net", response.text)
        self.assertNotRegex(response.text, r'href="[^"]+\.md"')
        self.assertNotIn("window.print()", dashboard.text)

        download = self.client.get("/admin/product-docs/download")
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.headers["content-type"], "application/pdf")
        self.assertIn("attachment", download.headers["content-disposition"])
        self.assertEqual(download.headers["cache-control"], "private, no-store")
        self.assertTrue(download.content.startswith(b"%PDF"))


if __name__ == "__main__":
    unittest.main()
