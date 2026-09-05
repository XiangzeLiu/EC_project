import os
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]


class ServerPackagingTests(unittest.TestCase):
    def test_build_entry_scripts_use_shared_packager(self):
        for script_name, target in (("build_sm.bat", "sm"), ("build_ts.bat", "ts")):
            with self.subTest(script=script_name):
                script = (ROOT_DIR / script_name).read_text(encoding="utf-8")
                self.assertIn("packaging\\build_server.ps1", script)
                self.assertIn(f"-Target {target}", script)

        combined = (ROOT_DIR / "build_servers.bat").read_text(encoding="utf-8")
        self.assertIn("build_sm.bat", combined)
        self.assertIn("build_ts.bat", combined)
        self.assertIn("SERVER_BUILD_TIMESTAMP", combined)
        self.assertLess(combined.index("SERVER_BUILD_TIMESTAMP"), combined.index("build_sm.bat"))

    def test_shared_packager_uses_explicit_resource_allowlist(self):
        script = (ROOT_DIR / "packaging" / "build_server.ps1").read_text(encoding="utf-8")

        self.assertIn('"--add-data", ((Join-Path $sourceDir "templates")', script)
        self.assertIn('"--add-data", ((Join-Path $sourceDir "resources")', script)
        self.assertIn('"--add-data", ((Join-Path $sourceDir "assets")', script)
        self.assertNotIn('"--collect-all", "Server_manager"', script)
        self.assertNotIn('"--collect-all", "Trader_Server"', script)
        self.assertNotIn("Trader_Server\\data;Trader_Server\\data", script)
        self.assertNotIn("Server_manager\\data;Server_manager\\data", script)
        self.assertIn("Forbidden runtime or secret-bearing files found in package", script)
        self.assertIn("ALLOW_DIRTY_BUILD", script)
        self.assertIn("--package-self-test", script)
        self.assertIn("function Compress-ArchiveWithRetry", script)
        self.assertIn("Compress-ArchiveWithRetry -SourcePath $appOut", script)
        self.assertIn("$artifactEntries = foreach ($artifactPath in $artifactPaths)", script)
        self.assertIn("$checksumLines = foreach ($artifact in $artifactEntries)", script)
        self.assertIn("WaitForExit($TimeoutSeconds * 1000)", script)
        self.assertIn("ALLOW_ARCHIVE_ONLY", script)
        self.assertIn("SERVER_RELEASE_BUILD", script)
        self.assertIn("INNO_COMMERCIAL_LICENSE_CONFIRMED", script)
        self.assertIn('"_VALIDATION"', script)
        self.assertIn("installed configuration contains a non-empty secret", script)

    def test_packaging_runbook_marks_release_boundaries(self):
        runbook = (ROOT_DIR / "packaging" / "README.md").read_text(encoding="utf-8")

        self.assertIn("build_servers.bat", runbook)
        self.assertIn("ALLOW_DIRTY_BUILD", runbook)
        self.assertIn("git_dirty=true", runbook)
        self.assertIn("SHA256SUMS.txt", runbook)
        self.assertIn("%ProgramData%\\SC\\ServerManager\\data", runbook)
        self.assertIn("bootstrap_packaging_tools.bat", runbook)

    def test_packaging_tools_are_version_and_hash_locked(self):
        lock = json.loads(
            (ROOT_DIR / "packaging" / "windows_tools.lock.json").read_text(encoding="utf-8")
        )
        bootstrap = (ROOT_DIR / "packaging" / "bootstrap_windows_tools.ps1").read_text(
            encoding="utf-8"
        )

        self.assertEqual(lock["inno_setup"]["version"], "6.7.3")
        self.assertEqual(len(lock["inno_setup"]["installer_sha256"]), 64)
        self.assertEqual(lock["windows_sdk_build_tools"]["version"], "10.0.28000.2705")
        self.assertEqual(len(lock["windows_sdk_build_tools"]["signtool_sha256"]), 64)
        self.assertTrue(lock["inno_setup"]["url"].startswith("https://"))
        self.assertTrue(lock["windows_sdk_build_tools"]["url"].startswith("https://"))
        self.assertIn("Assert-ValidPublisherSignature", bootstrap)
        self.assertIn("package_sha512_base64", bootstrap)
        self.assertNotIn("Remove-Item -Recurse", bootstrap)

    def test_installed_launchers_externalize_and_protect_runtime_data(self):
        sm_launcher = (ROOT_DIR / "deploy" / "windows" / "start_sm_installed.bat").read_text(
            encoding="utf-8"
        )
        ts_launcher = (ROOT_DIR / "deploy" / "windows" / "start_ts_installed.bat").read_text(
            encoding="utf-8"
        )

        self.assertIn(r"%ProgramData%\SC\ServerManager", sm_launcher)
        self.assertIn(r"%SM_RUNTIME_ROOT%\sm.local.bat", sm_launcher)
        self.assertIn(r"%~dp0caddy\caddy.exe", sm_launcher)
        self.assertIn("-Verb RunAs", sm_launcher)
        self.assertNotIn("admin123", sm_launcher)

        self.assertIn(r"%ProgramData%\SC\TraderServer", ts_launcher)
        self.assertIn(r"%TS_RUNTIME_ROOT%\ts.local.bat", ts_launcher)
        self.assertIn(r"%~dp0caddy\caddy.exe", ts_launcher)
        self.assertIn("-Verb RunAs", ts_launcher)

    def test_inno_installers_preserve_program_data(self):
        templates = {
            "sm": ROOT_DIR / "packaging" / "inno" / "server_manager.iss",
            "ts": ROOT_DIR / "packaging" / "inno" / "trader_server.iss",
        }
        app_ids = set()
        for name, path in templates.items():
            with self.subTest(name=name):
                content = path.read_text(encoding="utf-8")
                app_id_line = next(line for line in content.splitlines() if line.startswith("AppId="))
                self.assertNotIn(app_id_line, app_ids)
                app_ids.add(app_id_line)
                self.assertIn("{commonappdata}\\SC\\", content)
                self.assertIn("Flags: uninsneveruninstall", content)
                self.assertIn("Flags: onlyifdoesntexist uninsneveruninstall", content)
                self.assertIn("AfterInstall: HardenRuntimeAcl", content)
                self.assertTrue(
                    "RaiseException('Unable to secure" in content
                    or "RaiseException('无法保护" in content
                )
                self.assertIn("SignedUninstaller=yes", content)
                self.assertIn("SignTool=scsign", content)
                self.assertNotIn("[UninstallDelete]", content)
                for line in content.splitlines():
                    if line.startswith("Type:"):
                        self.assertIn('Name: "{app}\\', line)
                if name == "sm":
                    self.assertIn("InstallerHelper", content)
                    self.assertIn("--prepare", content)
                    self.assertIn("--commit", content)
                    self.assertIn("ExtractTemporaryFile('SC_SM_InstallerHelper.exe')", content)
                    self.assertIn("固定生产访问配置", content)
                    self.assertIn("CertificatePage: TInputFileWizardPage", content)
                    self.assertIn("CreateInputFilePage", content)
                    self.assertIn("certificate_source', CertificatePage.Values[0]", content)
                    self.assertIn("key_source', CertificatePage.Values[1]", content)
                    self.assertIn("条件必填", content)
                    self.assertIn("系统固定不可修改", content)

    def test_signing_helper_is_fail_closed_for_release_builds(self):
        helper = (ROOT_DIR / "packaging" / "windows_signing.ps1").read_text(encoding="utf-8")
        packager = (ROOT_DIR / "packaging" / "build_server.ps1").read_text(encoding="utf-8")

        self.assertIn('"/fd", "SHA256"', helper)
        self.assertIn('"/tr", $Configuration.TimestampUrl', helper)
        self.assertIn('"/td", "SHA256"', helper)
        self.assertIn("TimeStamperCertificate", helper)
        self.assertIn("Get-InnoSignToolCommand", helper)
        self.assertIn("Microsoft Corporation", helper)
        self.assertIn("SERVER_SIGN_CERT_THUMBPRINT", helper)
        self.assertIn("REQUIRE_CODE_SIGNING", helper)
        self.assertIn("/Sscsign=$innoSignCommand", packager)
        self.assertIn("Assert-AuthenticodeSignature -Path $installerPath", packager)

        sign_app = packager.index("Signing packaged executable")
        self_test = packager.index("Running frozen package self-test")
        archive = packager.index("Compress-ArchiveWithRetry -SourcePath $appOut")
        installer = packager.index("Building Inno Setup installer")
        manifest = packager.index("$manifest = [ordered]@{")
        self.assertLess(sign_app, self_test)
        self.assertLess(self_test, archive)
        self.assertLess(archive, installer)
        self.assertLess(installer, manifest)

    def test_build_requirements_are_pinned_and_include_timezone_data(self):
        for path in (
            ROOT_DIR / "Server_manager" / "requirements-build.txt",
            ROOT_DIR / "Trader_Server" / "requirements-build.txt",
        ):
            with self.subTest(path=path):
                content = path.read_text(encoding="utf-8")
                self.assertIn("PyInstaller==6.21.0", content)
                self.assertIn("tzdata==2026.3", content)
                for line in content.splitlines():
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#") or stripped.startswith("ibapi @"):
                        continue
                    self.assertIn("==", stripped)

    def test_production_start_scripts_externalize_runtime_data(self):
        sm_script = (ROOT_DIR / "deploy" / "windows" / "start_sm.bat").read_text(encoding="utf-8")
        ts_script = (ROOT_DIR / "deploy" / "windows" / "start_ts.bat").read_text(encoding="utf-8")
        sm_example = (ROOT_DIR / "deploy" / "windows" / "sm.local.bat.example").read_text(
            encoding="utf-8"
        )

        self.assertIn('set "SM_ENVIRONMENT=production"', sm_script)
        self.assertIn('set "SM_DATA_DIR=%~dp0data"', sm_script)
        self.assertIn('set "SERVER_MANAGER_DB_PATH=%SM_DATA_DIR%\\server_manager.db"', sm_script)
        self.assertIn('deployment.json', sm_script)
        self.assertIn('deployment.json', (ROOT_DIR / "deploy" / "windows" / "start_sm_installed.bat").read_text(encoding="utf-8"))
        self.assertIn('set "SM_DOMAIN_COOLDOWN_SECONDS=1800"', sm_script)
        self.assertNotIn("admin123", sm_script)
        self.assertNotIn("admin123", sm_example)

        self.assertIn('set "TS_ENVIRONMENT=production"', ts_script)
        self.assertIn('set "TS_DATA_DIR=%~dp0data"', ts_script)

        docs_source = (ROOT_DIR / "docs" / "product" / "01_系统总览与数据流.md").read_text(
            encoding="utf-8"
        )
        docs_fragment = (
            ROOT_DIR / "Server_manager" / "templates" / "product_docs_fragment.html"
        ).read_text(encoding="utf-8")
        self.assertIn("`600` / `1800` 秒", docs_source)
        self.assertIn("<code>600</code> / <code>1800</code> 秒", docs_fragment)

    def test_caddy_binaries_match_the_pinned_hash(self):
        expected = "0A37A942F6672AA056458A46C2E4A7D9F4621CFC8D230378BF285C0B38C38AEC"
        import hashlib

        for path in (
            ROOT_DIR / "Server_manager" / "caddy" / "caddy.exe",
            ROOT_DIR / "Trader_Server" / "caddy" / "caddy.exe",
        ):
            with self.subTest(path=path):
                digest = hashlib.sha256(path.read_bytes()).hexdigest().upper()
                self.assertEqual(digest, expected)

    def test_server_manager_source_package_self_test(self):
        if os.environ.get("SERVER_PACKAGING_TARGET", "").lower() == "ts":
            self.skipTest("SM source self-test is covered by the SM build")
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "sm-data"
            env = os.environ.copy()
            env.update(
                {
                    "SM_ENVIRONMENT": "selftest",
                    "SM_DATA_DIR": str(data_dir),
                    "SERVER_MANAGER_DB_PATH": str(data_dir / "server_manager.db"),
                    "SM_SOFTWARE_STORAGE_DIR": str(data_dir / "software"),
                    "SM_CADDY_AUTO_MANAGE": "0",
                    "SM_CADDY_REQUIRED": "0",
                    "SM_DNSPOD_MODE": "disabled",
                }
            )
            result = subprocess.run(
                [sys.executable, str(ROOT_DIR / "Server_manager" / "main.py"), "--package-self-test"],
                cwd=ROOT_DIR,
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Server Manager package self-test passed", result.stdout)

    def test_trader_server_source_package_self_test(self):
        if os.environ.get("SERVER_PACKAGING_TARGET", "").lower() == "sm":
            self.skipTest("TS source self-test is covered by the TS build")
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env.update(
                {
                    "QT_QPA_PLATFORM": "offscreen",
                    "TS_ENVIRONMENT": "selftest",
                    "TS_DATA_DIR": str(Path(tmp) / "ts-data"),
                    "TS_CADDY_AUTO_MANAGE": "0",
                    "TS_CADDY_REQUIRED": "0",
                }
            )
            result = subprocess.run(
                [sys.executable, str(ROOT_DIR / "Trader_Server" / "main.py"), "--package-self-test"],
                cwd=ROOT_DIR,
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Trader Server package self-test passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
