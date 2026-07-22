"""Public IPv4 detection for an unregistered Trader Server."""

from __future__ import annotations

import ipaddress
import json
import urllib.request


PUBLIC_IP_ENDPOINTS = (
    ("https://api.ipify.org?format=json", "json"),
    ("https://ifconfig.me/ip", "text"),
    ("https://icanhazip.com", "text"),
)


def validate_public_ipv4(value: str) -> str:
    ip = ipaddress.ip_address((value or "").strip())
    if not isinstance(ip, ipaddress.IPv4Address):
        raise ValueError("not an IPv4 address")
    if not ip.is_global:
        raise ValueError("not a public IPv4 address")
    return str(ip)


def detect_public_ipv4(timeout: float = 4.0) -> str:
    errors = []
    headers = {"Accept": "application/json,text/plain", "User-Agent": "TraderServer/1.0"}
    for url, response_type in PUBLIC_IP_ENDPOINTS:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as response:
                text = response.read(256).decode("utf-8", errors="replace").strip()
            if response_type == "json":
                text = str(json.loads(text).get("ip") or "").strip()
            return validate_public_ipv4(text)
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    raise RuntimeError("public IPv4 detection failed: " + " | ".join(errors))
