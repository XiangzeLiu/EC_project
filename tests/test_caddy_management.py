import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Server_manager"))
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SM_CADDY_AUTO_MANAGE", "0")

from services import caddy_manager as sm_caddy
from Trader_Server.services import caddy_manager as ts_caddy


class CaddyRenderTests(unittest.TestCase):
    def test_sm_render_uses_loopback_admin_and_upstream(self):
        rendered = sm_caddy.render_sm_caddyfile()
        self.assertIn("admin 127.0.0.1:2019", rendered)
        self.assertIn("scjrdomain.com", rendered)
        self.assertIn("reverse_proxy 127.0.0.1:8800", rendered)
        self.assertLess(rendered.index("handle @internal_only"), rendered.index("handle {"))

    def test_ts_render_routes_before_fallback_404(self):
        rendered = ts_caddy.render_ts_caddyfile("ts-01.ts.scjrdomain.com")
        self.assertIn("admin 127.0.0.1:2020", rendered)
        self.assertIn("reverse_proxy 127.0.0.1:8900", rendered)
        self.assertLess(rendered.index("handle @client_ws"), rendered.rindex("handle {"))
        self.assertLess(rendered.index("handle @sm_admin"), rendered.rindex("handle {"))
        self.assertLess(rendered.index("reverse_proxy"), rendered.index("respond 404"))

    def test_admin_endpoint_must_be_loopback(self):
        with self.assertRaises(ValueError):
            sm_caddy.render_sm_caddyfile(admin_address="0.0.0.0:2019")
        with self.assertRaises(ValueError):
            ts_caddy.render_ts_caddyfile(
                "ts-01.ts.scjrdomain.com",
                admin_address="8.8.8.8:2020",
            )

    def test_real_caddy_adapt_keeps_ts_404_after_proxies(self):
        candidates = (
            ROOT / "Trader_Server" / "caddy" / "caddy.exe",
            ROOT / "Server_manager" / "caddy" / "caddy.exe",
        )
        caddy = next((path for path in candidates if path.is_file()), None)
        if caddy is None:
            self.skipTest("caddy.exe is not available")
        result = subprocess.run(
            [str(caddy), "adapt", "--config", "-", "--adapter", "caddyfile"],
            input=ts_caddy.render_ts_caddyfile("ts-01.ts.scjrdomain.com"),
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        adapted = json.loads(result.stdout)
        serialized = json.dumps(adapted)
        self.assertLess(serialized.index('"reverse_proxy"'), serialized.index('"static_response"'))


class CaddyManagerTests(unittest.TestCase):
    def test_sm_reload_success_uses_persistent_runtime_dirs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_dir = Path(temp_dir)
            caddy = runtime_dir / "caddy.exe"
            caddy.touch()
            completed = subprocess.CompletedProcess([], 0, "", "")
            with (
                patch.object(sm_caddy, "SM_CADDY_AUTO_MANAGE", True),
                patch.object(sm_caddy, "_runtime_dir", return_value=runtime_dir),
                patch.object(sm_caddy, "_caddy_executable", return_value=caddy),
                patch.object(sm_caddy, "_run_command", side_effect=(completed, completed)),
            ):
                result = sm_caddy.configure_and_start_caddy()
            self.assertTrue(result["ok"])
            self.assertEqual(result["action"], "reloaded")
            self.assertTrue((runtime_dir / "Caddyfile").is_file())
            self.assertTrue((runtime_dir / "data").is_dir())
            self.assertTrue((runtime_dir / "config").is_dir())
            self.assertTrue((runtime_dir / "logs").is_dir())

    def test_ts_starts_after_reload_miss(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_dir = Path(temp_dir)
            caddy = runtime_dir / "caddy.exe"
            caddy.touch()
            validate = subprocess.CompletedProcess([], 0, "", "")
            reload_miss = subprocess.CompletedProcess([], 1, "", "connection refused")
            with (
                patch.object(ts_caddy, "TS_CADDY_AUTO_MANAGE", True),
                patch.object(ts_caddy, "_runtime_dir", return_value=runtime_dir),
                patch.object(ts_caddy, "_caddy_executable", return_value=caddy),
                patch.object(ts_caddy, "_run_command", side_effect=(validate, reload_miss)),
                patch.object(ts_caddy, "_start_detached", return_value={"ok": True, "action": "started"}),
            ):
                result = ts_caddy.configure_and_start_caddy("ts-01.ts.scjrdomain.com")
            self.assertTrue(result["ok"])
            self.assertEqual(result["action"], "started")

    def test_missing_caddy_is_reported_without_start_attempt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_dir = Path(temp_dir)
            with (
                patch.object(ts_caddy, "TS_CADDY_AUTO_MANAGE", True),
                patch.object(ts_caddy, "_runtime_dir", return_value=runtime_dir),
                patch.object(ts_caddy, "_caddy_executable", return_value=None),
            ):
                result = ts_caddy.configure_and_start_caddy("ts-01.ts.scjrdomain.com")
            self.assertFalse(result["ok"])
            self.assertTrue(result["skipped"])
            self.assertIn("not found", result["reason"])


if __name__ == "__main__":
    unittest.main()
