import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from Client.ui_qt import client_version as version_module


ROOT_DIR = Path(__file__).resolve().parents[1]


class ClientPackagingTests(unittest.TestCase):
    def test_packaged_build_timestamp_takes_precedence_and_stays_fixed(self):
        with tempfile.TemporaryDirectory() as tmp:
            build_info = Path(tmp) / "client_build_info.json"
            build_info.write_text(
                json.dumps({"platform": 0, "build_timestamp": "20260803153020"}),
                encoding="utf-8",
            )
            with patch.object(version_module, "_BUILD_INFO_PATH", build_info), patch.dict(
                os.environ,
                {"SC_CLIENT_BUILD_TIMESTAMP": "20270101010101"},
            ):
                first = version_module._load_build_timestamp()
                second = version_module._load_build_timestamp()

        self.assertEqual(first, "20260803153020")
        self.assertEqual(second, first)

    def test_client_package_self_test_cli(self):
        result = subprocess.run(
            [sys.executable, str(ROOT_DIR / "Client" / "main.py"), "--package-self-test"],
            cwd=ROOT_DIR,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Client package self-test passed", result.stdout)

    def test_build_script_uses_sc_brand_and_requires_installer_by_default(self):
        script = (ROOT_DIR / "build_client_installer.bat").read_text(encoding="utf-8")

        self.assertIn('set "APP_DISPLAY_NAME=SC Client"', script)
        self.assertIn('set "APP_EXE_NAME=SCClient"', script)
        self.assertIn("requirements-build.txt", script)
        self.assertIn('set "ICON_DIR=%ROOT_DIR%\\Client\\assets\\icons"', script)
        self.assertIn('set "APP_ICON=%ICON_DIR%\\sc-client.ico"', script)
        self.assertIn('--icon "%APP_ICON%"', script)
        self.assertIn('--add-data "%ICON_DIR%;Client\\assets\\icons"', script)
        self.assertIn("SetupIconFile=%APP_ICON%", script)
        self.assertIn("UninstallDisplayIcon={app}\\{#MyAppExeName}", script)
        self.assertIn('IconFilename: "{app}\\{#MyAppExeName}"', script)
        self.assertIn("--package-self-test", script)
        self.assertIn("Checking Client source for provider identifiers", script)
        self.assertIn("Checking packaged application for provider identifiers", script)
        self.assertIn("provider identifiers found in Client source", script)
        self.assertIn("provider identifiers found in packaged application", script)
        self.assertIn("ALLOW_PORTABLE_ONLY", script)
        self.assertRegex(script, re.compile(r"Inno Setup 6 was not found.*goto :fail", re.S))


if __name__ == "__main__":
    unittest.main()
