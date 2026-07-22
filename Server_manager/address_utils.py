"""
Address helpers for TS endpoints.

SM may see the same Trader Server as a bare host, host:port, ws:// URL,
wss:// URL, or http(s) URL. These helpers keep matching and admin-call URL
construction consistent across the manager.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


DEFAULT_TS_PORT = 8900


def _split_endpoint(endpoint: str) -> tuple[str, str, int | None, str]:
    raw = (endpoint or "").strip()
    if not raw:
        return "", "", None, ""

    if "://" in raw:
        parsed = urlsplit(raw)
        port = None
        try:
            port = parsed.port
        except ValueError:
            port = None
        return parsed.scheme.lower(), (parsed.hostname or "").lower(), port, parsed.netloc.lower()

    authority = raw.split("?", 1)[0].split("#", 1)[0].split("/", 1)[0].strip()
    if authority.startswith("//"):
        authority = authority[2:]
    parsed = urlsplit(f"//{authority}")
    port = None
    try:
        port = parsed.port
    except ValueError:
        port = None
    host = (parsed.hostname or authority).lower()
    return "", host, port, authority.lower()


def address_candidates(endpoint: str) -> set[str]:
    """Return comparable address keys for host/domain/URL matching."""
    scheme, host, port, authority = _split_endpoint(endpoint)
    candidates = set()
    raw = (endpoint or "").strip().lower()
    if raw:
        candidates.add(raw.rstrip("/"))
    if authority:
        candidates.add(authority.rstrip("/"))
    if host:
        candidates.add(host)
    if host and port:
        candidates.add(f"{host}:{port}")
    if host and not port and scheme in {"https", "wss"}:
        candidates.add(f"{host}:443")
    return {c for c in candidates if c}


def node_address_candidates(node) -> set[str]:
    """Return every address form associated with a node row or NodeState."""
    if node is None:
        return set()

    def value(name: str) -> str:
        if isinstance(node, dict):
            return str(node.get(name) or "")
        return str(getattr(node, name, "") or getattr(node, f"_{name}", "") or "")

    candidates: set[str] = set()
    for field in ("host", "public_ip", "assigned_domain", "public_endpoint", "current_ip"):
        candidates.update(address_candidates(value(field)))
    return candidates


def endpoint_matches_node(endpoint: str, node) -> bool:
    requested = address_candidates(endpoint)
    return bool(requested and (requested & node_address_candidates(node)))


def ts_http_base_url(endpoint: str) -> str:
    """Convert a TS endpoint to an HTTP(S) base URL for REST admin calls."""
    raw = (endpoint or "").strip()
    if not raw:
        return ""

    if "://" in raw:
        parsed = urlsplit(raw)
        scheme = parsed.scheme.lower()
        if scheme == "ws":
            scheme = "http"
        elif scheme == "wss":
            scheme = "https"
        elif scheme not in {"http", "https"}:
            scheme = "http"
        return urlunsplit((scheme, parsed.netloc, "", "", "")).rstrip("/")

    authority = raw.split("?", 1)[0].split("#", 1)[0].split("/", 1)[0].strip()
    return f"http://{authority}".rstrip("/")


def ts_api_url(endpoint: str, path: str) -> str:
    base = ts_http_base_url(endpoint)
    if not base:
        return ""
    api_path = "/" + (path or "").lstrip("/")
    return f"{base}{api_path}"


def tcp_probe_target(endpoint: str, default_port: int = DEFAULT_TS_PORT) -> tuple[str, int] | None:
    """Return host and port for a TCP liveliness probe."""
    scheme, host, port, _authority = _split_endpoint(endpoint)
    if not host or host == "0.0.0.0":
        return None
    if port is None:
        port = 443 if scheme in {"https", "wss"} else default_port
    return host, port
