"""Runtime DNSPod configuration used by the SM domain-pool workflow."""

from __future__ import annotations

import json
import re
import secrets
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

import database
from config import (
    SM_DNSPOD_LINE,
    SM_DNSPOD_MODE,
    SM_DNSPOD_SECRET_ID,
    SM_DNSPOD_SECRET_KEY,
    SM_DNS_TTL,
    SM_DOMAIN_COOLDOWN_SECONDS,
    SM_DOMAIN_ROOT,
    SM_TS_DOMAIN_SUFFIX,
)


class DNSConfigError(ValueError):
    pass


@dataclass(frozen=True)
class DNSRuntimeConfig:
    provider: str
    mode: str
    root_domain: str
    domain_suffix: str
    secret_id: str
    secret_key: str
    record_line: str
    ttl: int
    cooldown_seconds: int
    verified: bool = False
    last_test_at: str = ""
    last_test_error: str = ""
    config_version: int = 0
    updated_by: str = ""
    updated_at: str = ""


_MODES = {"mock", "manual", "real", "disabled"}
_PERMISSION_TEST_LOCK = threading.Lock()
_IMPORT_KEY_MAP = {
    "provider": "provider",
    "mode": "mode",
    "dnspodmode": "mode",
    "smdnspodmode": "mode",
    "rootdomain": "root_domain",
    "domainroot": "root_domain",
    "smdomainroot": "root_domain",
    "domainsuffix": "domain_suffix",
    "tsdomainsuffix": "domain_suffix",
    "smtsdomainsuffix": "domain_suffix",
    "secretid": "secret_id",
    "dnspodsecretid": "secret_id",
    "smdnspodsecretid": "secret_id",
    "secretkey": "secret_key",
    "dnspodsecretkey": "secret_key",
    "smdnspodsecretkey": "secret_key",
    "recordline": "record_line",
    "line": "record_line",
    "dnspodline": "record_line",
    "smdnspodline": "record_line",
    "ttl": "ttl",
    "dnsttl": "ttl",
    "smdnsttl": "ttl",
    "cooldownseconds": "cooldown_seconds",
    "domaincooldownseconds": "cooldown_seconds",
    "smdomaincooldownseconds": "cooldown_seconds",
}


def _canonical_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").strip().lower())


def _normalize_domain(value: Any, field: str) -> str:
    domain = str(value or "").strip().lower().strip(".")
    if not domain or len(domain) > 253:
        raise DNSConfigError(f"{field} is invalid")
    for label in domain.split("."):
        if (
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or not label.isascii()
            or not all(ch.isalnum() or ch == "-" for ch in label)
        ):
            raise DNSConfigError(f"{field} contains an invalid domain label")
    return domain


def _bounded_int(value: Any, field: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise DNSConfigError(f"{field} must be an integer") from exc
    if parsed < minimum or parsed > maximum:
        raise DNSConfigError(f"{field} must be between {minimum} and {maximum}")
    return parsed


def _environment_defaults() -> dict:
    return {
        "provider": "dnspod",
        "mode": SM_DNSPOD_MODE,
        "root_domain": SM_DOMAIN_ROOT,
        "domain_suffix": SM_TS_DOMAIN_SUFFIX,
        "secret_id": SM_DNSPOD_SECRET_ID,
        "secret_key": SM_DNSPOD_SECRET_KEY,
        "record_line": SM_DNSPOD_LINE,
        "ttl": SM_DNS_TTL,
        "cooldown_seconds": SM_DOMAIN_COOLDOWN_SECONDS,
        "verified": False,
        "last_test_at": "",
        "last_test_error": "",
        "config_version": 0,
        "updated_by": "bootstrap_env",
        "updated_at": "",
    }


def _to_runtime_config(raw: dict) -> DNSRuntimeConfig:
    provider = str(raw.get("provider") or "dnspod").strip().lower()
    if provider != "dnspod":
        raise DNSConfigError("only dnspod is supported")
    mode = str(raw.get("mode") or "manual").strip().lower()
    if mode not in _MODES:
        raise DNSConfigError("mode must be mock, manual, real, or disabled")
    root_domain = _normalize_domain(raw.get("root_domain"), "root_domain")
    domain_suffix = _normalize_domain(
        raw.get("domain_suffix") or f"ts.{root_domain}",
        "domain_suffix",
    )
    if domain_suffix != root_domain and not domain_suffix.endswith(f".{root_domain}"):
        raise DNSConfigError("domain_suffix must be inside root_domain")
    return DNSRuntimeConfig(
        provider=provider,
        mode=mode,
        root_domain=root_domain,
        domain_suffix=domain_suffix,
        secret_id=str(raw.get("secret_id") or "").strip(),
        secret_key=str(raw.get("secret_key") or "").strip(),
        record_line=str(raw.get("record_line") or "默认").strip() or "默认",
        ttl=_bounded_int(raw.get("ttl", 600), "ttl", 1, 604800),
        cooldown_seconds=_bounded_int(
            raw.get("cooldown_seconds", 300),
            "cooldown_seconds",
            0,
            604800,
        ),
        verified=bool(raw.get("verified")),
        last_test_at=str(raw.get("last_test_at") or ""),
        last_test_error=str(raw.get("last_test_error") or ""),
        config_version=int(raw.get("config_version") or 0),
        updated_by=str(raw.get("updated_by") or ""),
        updated_at=str(raw.get("updated_at") or ""),
    )


def get_runtime_config() -> DNSRuntimeConfig:
    stored = database.get_dns_provider_config()
    if stored:
        return _to_runtime_config(stored)
    bootstrap = _to_runtime_config(_environment_defaults())
    stored = database.upsert_dns_provider_config(
        asdict(bootstrap),
        updated_by="bootstrap_env",
    )
    return _to_runtime_config(stored)


def _merge_config(data: dict, current: DNSRuntimeConfig) -> DNSRuntimeConfig:
    values = asdict(current)
    clear_secret = bool(data.get("clear_secret"))
    editable_fields = {
        "provider",
        "mode",
        "root_domain",
        "domain_suffix",
        "record_line",
        "ttl",
        "cooldown_seconds",
    }
    for field in editable_fields:
        if field in data and data[field] is not None:
            values[field] = data[field]

    if clear_secret:
        values["secret_id"] = ""
        values["secret_key"] = ""
    else:
        secret_id = str(data.get("secret_id") or "").strip()
        secret_key = str(data.get("secret_key") or "").strip()
        if secret_id:
            values["secret_id"] = secret_id
        if secret_key:
            values["secret_key"] = secret_key

    values.update({
        "verified": False,
        "last_test_at": "",
        "last_test_error": "",
    })
    return _to_runtime_config(values)


def validate_ready(config: DNSRuntimeConfig) -> None:
    if config.mode == "disabled":
        raise DNSConfigError("DNSPod is disabled")
    if config.mode == "real" and (not config.secret_id or not config.secret_key):
        raise DNSConfigError("DNSPod SecretId/SecretKey is not configured")


def save_config(data: dict, updated_by: str = "") -> DNSRuntimeConfig:
    if not isinstance(data, dict):
        raise DNSConfigError("configuration must be a JSON object")
    current = get_runtime_config()
    candidate = _merge_config(data, current)
    if candidate.mode == "real":
        validate_ready(candidate)
    root_changed = (
        candidate.root_domain != current.root_domain
        or candidate.domain_suffix != current.domain_suffix
    )
    if root_changed and database.count_ts_domain_pool_entries() > 0:
        raise DNSConfigError(
            "root_domain/domain_suffix cannot change while the domain pool is not empty"
        )
    stored = database.upsert_dns_provider_config(
        asdict(candidate),
        updated_by=(updated_by or "").strip(),
    )
    return _to_runtime_config(stored)


def build_candidate_config(data: dict) -> DNSRuntimeConfig:
    """Build an in-memory candidate without changing persisted DNS settings."""
    if not isinstance(data, dict):
        raise DNSConfigError("configuration must be a JSON object")
    candidate = _merge_config(data, get_runtime_config())
    if candidate.mode != "real":
        raise DNSConfigError("DNSPod permission test requires real mode")
    validate_ready(candidate)
    return candidate


def parse_import_payload(raw: Any) -> dict:
    if isinstance(raw, dict):
        source = dict(raw)
        if isinstance(raw.get("credentials"), dict):
            source.update(raw["credentials"])
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise DNSConfigError("import content is empty")
        if text.startswith("{"):
            try:
                source = json.loads(text)
            except json.JSONDecodeError as exc:
                raise DNSConfigError(f"invalid JSON: {exc.msg}") from exc
            if not isinstance(source, dict):
                raise DNSConfigError("import JSON must be an object")
            if isinstance(source.get("credentials"), dict):
                credentials = source.pop("credentials")
                source.update(credentials)
        else:
            source = {}
            for line in text.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if "=" not in stripped:
                    raise DNSConfigError(f"invalid import line: {stripped}")
                key, value = stripped.split("=", 1)
                source[key.strip()] = value.strip()
    else:
        raise DNSConfigError("import content must be JSON or key=value text")

    normalized: dict[str, Any] = {}
    for key, value in source.items():
        target = _IMPORT_KEY_MAP.get(_canonical_key(str(key)))
        if target:
            normalized[target] = value
    if not normalized:
        raise DNSConfigError("no supported DNSPod configuration fields were found")
    return normalized


def import_config(raw: Any, updated_by: str = "") -> DNSRuntimeConfig:
    return save_config(parse_import_payload(raw), updated_by=updated_by)


def mask_secret_id(secret_id: str) -> str:
    value = (secret_id or "").strip()
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * min(12, len(value) - 8)}{value[-4:]}"


def public_config(config: DNSRuntimeConfig | None = None) -> dict:
    current = config or get_runtime_config()
    return {
        "provider": current.provider,
        "mode": current.mode,
        "root_domain": current.root_domain,
        "domain_suffix": current.domain_suffix,
        "secret_id_masked": mask_secret_id(current.secret_id),
        "secret_id_configured": bool(current.secret_id),
        "secret_key_configured": bool(current.secret_key),
        "record_line": current.record_line,
        "ttl": current.ttl,
        "cooldown_seconds": current.cooldown_seconds,
        "verified": current.verified,
        "last_test_at": current.last_test_at,
        "last_test_error": current.last_test_error,
        "config_version": current.config_version,
        "updated_by": current.updated_by,
        "updated_at": current.updated_at,
        "root_editable": database.count_ts_domain_pool_entries() == 0,
    }


def redact_error(message: Any, config: DNSRuntimeConfig | None = None) -> str:
    text = str(message or "")
    current = config
    if current is None:
        try:
            current = get_runtime_config()
        except Exception:
            current = None
    if current:
        for value in (current.secret_id, current.secret_key):
            if value:
                text = text.replace(value, "***")
    return text


def test_saved_config() -> dict:
    config = get_runtime_config()
    try:
        validate_ready(config)
        from dnspod_client import DNSPodClient

        result = DNSPodClient(config).test_connection()
        updated = database.update_dns_provider_test_result(True, "")
        return {
            "ok": True,
            "message": result,
            "config": public_config(_to_runtime_config(updated) if updated else config),
        }
    except Exception as exc:
        error = redact_error(exc, config)
        updated = database.update_dns_provider_test_result(False, error)
        return {
            "ok": False,
            "error": error,
            "config": public_config(_to_runtime_config(updated) if updated else config),
        }


def test_candidate_permissions(data: dict) -> dict:
    """Test current form values without saving credentials or verification state."""
    config = build_candidate_config(data)
    if not _PERMISSION_TEST_LOCK.acquire(blocking=False):
        return {
            "ok": False,
            "error": "another DNSPod permission test is already running",
            "steps": [],
            "cleanup_ok": True,
            "residual_record": False,
            "persisted": False,
        }

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    test_domain = (
        f"dnspod-check-{timestamp}-{secrets.token_hex(3)}.{config.root_domain}"
    )
    try:
        from dnspod_client import DNSPodClient

        result = DNSPodClient(config).test_permissions(test_domain)
        result["steps"] = [
            {
                **step,
                "detail": redact_error(step.get("detail", ""), config),
            }
            for step in result.get("steps", [])
        ]
        result["error"] = redact_error(result.get("error", ""), config)
        result["persisted"] = False
        result["message"] = (
            "DNSPod required permissions are available"
            if result.get("ok")
            else "DNSPod permission test did not fully pass"
        )
        return result
    except Exception as exc:
        return {
            "ok": False,
            "error": redact_error(exc, config),
            "test_domain": test_domain,
            "steps": [],
            "cleanup_ok": True,
            "residual_record": False,
            "persisted": False,
        }
    finally:
        _PERMISSION_TEST_LOCK.release()
