from __future__ import annotations

import ssl
import socket
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import URLError

from Trader_Server.services import https_client


class TraderHttpsTests(unittest.TestCase):
    def tearDown(self):
        https_client.reset_ssl_context()

    def test_context_requires_valid_certificate_and_hostname(self):
        context = https_client.get_ssl_context()

        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertTrue(Path(https_client.tls_diagnostics()["certifi_cafile"]).is_file())

    def test_https_request_uses_shared_context(self):
        response = MagicMock()
        with patch.object(https_client.urllib.request, "urlopen", return_value=response) as mocked:
            request = urllib.request.Request("https://scjrdomain.com/ping")
            actual = https_client.urlopen(request, timeout=10)

        self.assertIs(actual, response)
        self.assertIs(mocked.call_args.kwargs["context"], https_client.get_ssl_context())
        self.assertEqual(mocked.call_args.kwargs["timeout"], 10)

    def test_http_request_does_not_receive_ssl_context(self):
        response = MagicMock()
        with patch.object(https_client.urllib.request, "urlopen", return_value=response) as mocked:
            https_client.urlopen("http://127.0.0.1:8900/health", timeout=2)

        self.assertNotIn("context", mocked.call_args.kwargs)

    def test_missing_custom_ca_is_rejected(self):
        with patch.dict("os.environ", {"TS_CA_BUNDLE": "missing-private-ca.pem"}, clear=False):
            https_client.reset_ssl_context()
            with self.assertRaises(https_client.TLSConfigurationError):
                https_client.get_ssl_context()

    def test_certificate_and_dns_errors_are_operator_readable(self):
        cert_error = ssl.SSLCertVerificationError(
            1,
            "certificate verify failed: unable to get local issuer certificate",
        )

        self.assertEqual(
            https_client.describe_connection_error(URLError(cert_error)),
            "管理端证书链验证失败",
        )
        self.assertEqual(
            https_client.describe_connection_error(URLError(socket.gaierror(11001, "host not found"))),
            "无法解析管理端域名",
        )

    def test_external_ts_requests_do_not_call_raw_urlopen(self):
        root = Path(__file__).resolve().parents[1]
        allowed = {
            Path("Trader_Server/services/https_client.py"),
            Path("Trader_Server/ui_qt/api_client.py"),
        }

        for path in (root / "Trader_Server").rglob("*.py"):
            relative = path.relative_to(root)
            if relative in allowed:
                continue
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("urllib.request.urlopen", source, str(relative))


if __name__ == "__main__":
    unittest.main()
