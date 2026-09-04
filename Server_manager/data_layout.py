"""Portable Server Manager data-directory contract."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DATA_FORMAT_VERSION = 1
DEPLOYMENT_FILE_NAME = "deployment.json"
MANIFEST_FILE_NAME = "data_manifest.json"

_DEPLOYMENT_KEYS = frozenset(
    {
        "server_host",
        "server_port",
        "public_http_port",
        "public_https_port",
        "public_base_url",
        "caddy_admin",
        "caddy_auto_manage",
        "caddy_required",
        "caddy_start_timeout",
        "cookie_secure",
        "cookie_samesite",
        "client_token_ttl_seconds",
        "software_directory",
        "software_max_upload_bytes",
        "software_retention_count",
        "software_session_max_age",
        "finance_enabled",
        "finance_retention_months",
        "finance_cleanup_interval_seconds",
        "domain_cooldown_seconds",
        "domain_pool_required",
        "dnspod_mode",
        "certificate_file",
        "key_file",
    }
)


class DataLayoutError(ValueError):
    """Raised when the portable data-directory metadata is invalid."""


def _root(data_dir: str | Path) -> Path:
    return Path(data_dir).expanduser().resolve()


def deployment_path(data_dir: str | Path) -> Path:
    return _root(data_dir) / DEPLOYMENT_FILE_NAME


def manifest_path(data_dir: str | Path) -> Path:
    return _root(data_dir) / MANIFEST_FILE_NAME


def is_within(data_dir: str | Path, path: str | Path) -> bool:
    root = _root(data_dir)
    candidate = Path(path).expanduser().resolve()
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def relative_path(data_dir: str | Path, path: str | Path) -> str:
    root = _root(data_dir)
    candidate = Path(path).expanduser().resolve()
    try:
        return candidate.relative_to(root).as_posix()
    except ValueError as exc:
        raise DataLayoutError(f"path is outside data directory: {candidate}") from exc


def resolve_relative_path(data_dir: str | Path, value: str | Path) -> Path:
    root = _root(data_dir)
    raw = Path(str(value or "").strip())
    if raw.is_absolute():
        candidate = raw.resolve()
    else:
        candidate = (root / raw).resolve()
    if not is_within(root, candidate):
        raise DataLayoutError(f"path is outside data directory: {candidate}")
    return candidate


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DataLayoutError(f"invalid JSON file: {path}") from exc
    if not isinstance(value, dict):
        raise DataLayoutError(f"JSON root must be an object: {path}")
    return value


def load_deployment_config(data_dir: str | Path) -> dict[str, Any]:
    path = deployment_path(data_dir)
    if path.exists() and not path.is_file():
        raise DataLayoutError("deployment.json is not a regular file")
    if not path.exists():
        return {}
    value = _read_object(path)
    try:
        version = int(value.get("format_version", 0))
    except (TypeError, ValueError) as exc:
        raise DataLayoutError("deployment.json format_version is invalid") from exc
    if version != DATA_FORMAT_VERSION:
        raise DataLayoutError(
            f"unsupported deployment.json format_version: {version}"
        )
    if value.get("product") not in {None, "server_manager"}:
        raise DataLayoutError("deployment.json belongs to another product")
    return {key: value[key] for key in _DEPLOYMENT_KEYS if key in value}


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    try:
        temp_path.write_text(payload, encoding="utf-8")
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def write_deployment_config(data_dir: str | Path, values: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "product": "server_manager",
        "format_version": DATA_FORMAT_VERSION,
    }
    payload.update({key: values[key] for key in _DEPLOYMENT_KEYS if key in values})
    _atomic_write_json(deployment_path(data_dir), payload)
    return payload


def ensure_deployment_config(data_dir: str | Path, values: dict[str, Any]) -> dict[str, Any]:
    path = deployment_path(data_dir)
    if path.exists():
        return load_deployment_config(data_dir)
    return write_deployment_config(data_dir, values)


def write_data_manifest(
    data_dir: str | Path,
    *,
    database_schema_version: int,
    application_version: str = "",
) -> dict[str, Any]:
    payload = {
        "product": "server_manager",
        "format_version": DATA_FORMAT_VERSION,
        "database_schema_version": int(database_schema_version),
        "application_version": str(application_version or ""),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "paths": {
            "database": "server_manager.db",
            "software": "software",
            "certificates": "certificates",
            "logs": "logs",
        },
    }
    _atomic_write_json(manifest_path(data_dir), payload)
    return payload


def load_data_manifest(data_dir: str | Path) -> dict[str, Any] | None:
    path = manifest_path(data_dir)
    if path.exists() and not path.is_file():
        raise DataLayoutError("data_manifest.json is not a regular file")
    if not path.exists():
        return None
    value = _read_object(path)
    try:
        version = int(value.get("format_version", 0))
    except (TypeError, ValueError) as exc:
        raise DataLayoutError("data_manifest.json format_version is invalid") from exc
    if version != DATA_FORMAT_VERSION:
        raise DataLayoutError(
            f"unsupported data_manifest.json format_version: {version}"
        )
    if value.get("product") not in {None, "server_manager"}:
        raise DataLayoutError("data_manifest.json belongs to another product")
    paths = value.get("paths")
    if paths is not None:
        if not isinstance(paths, dict):
            raise DataLayoutError("data_manifest.json paths must be an object")
        expected = {
            "database": "server_manager.db",
            "software": "software",
            "certificates": "certificates",
            "logs": "logs",
        }
        for key, expected_path in expected.items():
            if key in paths and paths[key] != expected_path:
                raise DataLayoutError(f"data_manifest.json has an invalid {key} path")
    return value


def validate_data_manifest(
    data_dir: str | Path,
    *,
    database_schema_version: int | None = None,
) -> dict[str, Any] | None:
    """Validate portable metadata before opening a database for writes."""
    value = load_data_manifest(data_dir)
    if value is None:
        return None
    declared = value.get("database_schema_version")
    if declared is not None:
        try:
            declared_version = int(declared)
        except (TypeError, ValueError) as exc:
            raise DataLayoutError(
                "data_manifest.json database_schema_version is invalid"
            ) from exc
        if database_schema_version is not None and declared_version > int(database_schema_version):
            raise DataLayoutError(
                "data_manifest.json requires a newer database schema"
            )
    return value


def validate_data_paths(
    data_dir: str | Path,
    database_path: str | Path,
    software_dir: str | Path,
    certificate_file: str = "",
    key_file: str = "",
) -> list[str]:
    errors: list[str] = []
    root = _root(data_dir)
    for label, value in (
        ("database", database_path),
        ("software directory", software_dir),
    ):
        if not is_within(root, value):
            errors.append(f"{label} must be inside SM_DATA_DIR")
    for label, value in (("certificate", certificate_file), ("private key", key_file)):
        if value and not is_within(root, value):
            errors.append(f"{label} must be inside SM_DATA_DIR")
    return errors
