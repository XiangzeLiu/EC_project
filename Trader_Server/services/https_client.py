"""Shared HTTPS transport for outbound Trader Server requests."""

from __future__ import annotations

import os
import socket
import ssl
import urllib.request
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urlsplit

import certifi


class TLSConfigurationError(RuntimeError):
    """Raised when the TS trust store cannot be initialized safely."""


def _required_ca_file() -> Path:
    path = Path(certifi.where())
    if not path.is_file():
        raise TLSConfigurationError(f"Bundled CA certificate file is unavailable: {path}")
    return path


def _optional_ca_file() -> Path | None:
    configured = os.getenv("TS_CA_BUNDLE", "").strip()
    if not configured:
        return None
    path = Path(os.path.expandvars(configured)).expanduser()
    if not path.is_file():
        raise TLSConfigurationError(f"Configured TS_CA_BUNDLE file does not exist: {path}")
    return path


@lru_cache(maxsize=1)
def get_ssl_context() -> ssl.SSLContext:
    """Build a reusable context with system roots plus certifi and optional private CA roots."""
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=str(_required_ca_file()))
    custom_ca = _optional_ca_file()
    if custom_ca is not None:
        context.load_verify_locations(cafile=str(custom_ca))
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def reset_ssl_context() -> None:
    """Clear the cached context after changing TS_CA_BUNDLE in tests or at startup."""
    get_ssl_context.cache_clear()


def urlopen(target: Any, *args: Any, **kwargs: Any):
    """Open a URL while applying the shared verified context to HTTPS requests."""
    url = target.full_url if isinstance(target, urllib.request.Request) else str(target)
    if urlsplit(url).scheme.lower() == "https":
        kwargs["context"] = get_ssl_context()
    return urllib.request.urlopen(target, *args, **kwargs)


def tls_diagnostics() -> dict[str, Any]:
    """Return non-secret trust-store diagnostics for the local TS control surface."""
    context = get_ssl_context()
    custom_ca = _optional_ca_file()
    return {
        "certifi_cafile": str(_required_ca_file()),
        "custom_cafile": str(custom_ca) if custom_ca else "",
        "hostname_check": bool(context.check_hostname),
        "certificate_required": context.verify_mode == ssl.CERT_REQUIRED,
    }


def describe_connection_error(error: BaseException) -> str:
    """Convert TLS and network failures into actionable TS operator messages."""
    reason: BaseException | object = error
    if isinstance(error, URLError):
        reason = error.reason
    if isinstance(reason, TLSConfigurationError):
        return f"TLS 证书库不可用：{reason}"
    if isinstance(reason, ssl.SSLCertVerificationError):
        message = str(reason).lower()
        if "hostname" in message or "doesn't match" in message or "not valid for" in message:
            return "管理端证书域名不匹配"
        if "expired" in message:
            return "管理端 HTTPS 证书已过期"
        return "管理端证书链验证失败"
    if isinstance(reason, ssl.SSLError):
        return "管理端 TLS 握手失败"
    if isinstance(reason, socket.gaierror):
        return "无法解析管理端域名"
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "连接管理端超时"
    if isinstance(reason, ConnectionRefusedError):
        return "管理端拒绝连接"
    return str(error)
