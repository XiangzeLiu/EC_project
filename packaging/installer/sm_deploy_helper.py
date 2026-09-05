"""Transactional deployment helper for the SM Windows installer.

The helper is intentionally separate from the running SM application.  It
only prepares data, controls the SM-owned processes, and starts the already
packaged launcher after a successful commit.
"""

from __future__ import annotations

import argparse
import configparser
import json
import locale
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any


FIXED_SERVER_HOST = "127.0.0.1"
FIXED_SERVER_PORT = 18800
FIXED_PUBLIC_HTTP_PORT = 8800
FIXED_PUBLIC_HTTPS_PORT = 4430
FIXED_PUBLIC_BASE_URL = "https://scjrdomain.com:4430"
FIXED_CADDY_ADMIN = "127.0.0.1:2019"
MINIMUM_FREE_BYTES = 256 * 1024 * 1024
RUNTIME_RELATIVE = Path("SC") / "ServerManager"
MANAGED_FIREWALL_RULES = {
    "SC SM HTTP": FIXED_PUBLIC_HTTP_PORT,
    "SC SM HTTPS": FIXED_PUBLIC_HTTPS_PORT,
}


class InstallerError(RuntimeError):
    """Raised when installation cannot continue safely."""


def _full(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _is_within(parent: str | Path, child: str | Path) -> bool:
    try:
        _full(child).relative_to(_full(parent))
        return True
    except ValueError:
        return False


def _is_reparse_or_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _assert_safe_tree(path: Path) -> None:
    if not path.exists():
        return
    if _is_reparse_or_link(path):
        raise InstallerError(f"reparse point or symbolic link is not allowed: {path}")
    for child in path.rglob("*"):
        if _is_reparse_or_link(child):
            raise InstallerError(f"reparse point or symbolic link is not allowed: {child}")


def _set_runtime_acl(path: Path, *, directory: bool = False) -> None:
    """Apply the installer metadata ACL without relying on parent inheritance."""
    if os.name != "nt" or not path.exists():
        return
    arguments = [
        "icacls",
        str(path),
        "/inheritance:r",
        "/grant:r",
        "*S-1-5-18:(OI)(CI)F" if directory else "*S-1-5-18:F",
        "*S-1-5-32-544:(OI)(CI)F" if directory else "*S-1-5-32-544:F",
    ]
    result = _run(arguments)
    if result.returncode != 0:
        raise InstallerError(f"unable to protect installer metadata: {path}")


def _ensure_installer_directory(runtime_root: Path) -> Path:
    installer_dir = runtime_root / ".installer"
    installer_dir.mkdir(parents=True, exist_ok=True)
    _set_runtime_acl(installer_dir, directory=True)
    return installer_dir


def _remove_file(path: Path) -> None:
    if not path.exists():
        return
    _set_runtime_acl(path)
    path.unlink()


def _required_path(value: str | Path | None, label: str) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise InstallerError(f"installer state is missing {label}")
    return _full(raw)


def _copy_tree(source: Path, target: Path) -> None:
    _assert_safe_tree(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, dirs_exist_ok=True, symlinks=False)


def _remove_tree(path: Path) -> None:
    if path.exists():
        _assert_safe_tree(path)
        shutil.rmtree(path)


def _read_ini(path: Path) -> dict[str, str]:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    try:
        with path.open("r", encoding="utf-8") as stream:
            parser.read_file(stream)
    except (OSError, UnicodeError, configparser.Error) as exc:
        raise InstallerError(f"invalid installer request: {path}") from exc
    if not parser.has_section("install"):
        raise InstallerError("installer request is missing the [install] section")
    return {key: value.strip() for key, value in parser.items("install")}


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    if path.name in {"transaction.json", "install.lock"}:
        _set_runtime_acl(path)


def _load_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallerError(f"invalid installer transaction state: {path}") from exc
    if not isinstance(value, dict) or value.get("product") != "server_manager":
        raise InstallerError("installer transaction belongs to another product")
    return value


def _request_paths(request: dict[str, str]) -> tuple[str, Path, Path, Path | None]:
    mode = request.get("mode", "").lower()
    if mode not in {"fresh", "upgrade"}:
        raise InstallerError("install mode must be fresh or upgrade")
    app_text = request.get("app_dir", "").strip()
    data_text = request.get("data_dir", "").strip()
    if not app_text or not data_text:
        raise InstallerError("application and data directories are required")
    app_dir = _full(app_text)
    data_dir = _full(data_text)
    source_text = request.get("source_data", "").strip()
    source_dir = _full(source_text) if source_text else None
    if source_dir and source_dir == app_dir:
        raise InstallerError("source data directory cannot be the application directory")
    if source_dir and source_dir == data_dir:
        raise InstallerError(
            "upgrade source data directory must be different from the target data directory"
        )
    if mode == "upgrade" and source_dir is None:
        raise InstallerError("upgrade deployment requires a source data directory")
    if mode == "fresh" and source_dir is not None:
        raise InstallerError("fresh deployment cannot specify a source data directory")
    return mode, app_dir, data_dir, source_dir


def _validate_fixed_configuration(request: dict[str, str]) -> None:
    expected = {
        "server_host": FIXED_SERVER_HOST,
        "server_port": str(FIXED_SERVER_PORT),
        "public_http_port": str(FIXED_PUBLIC_HTTP_PORT),
        "public_https_port": str(FIXED_PUBLIC_HTTPS_PORT),
        "public_base_url": FIXED_PUBLIC_BASE_URL,
        "caddy_admin": FIXED_CADDY_ADMIN,
    }
    for key, value in expected.items():
        supplied = request.get(key, value).strip()
        if supplied != value:
            raise InstallerError(f"fixed configuration cannot be changed: {key}")


def _validate_credentials(request: dict[str, str], mode: str) -> None:
    username = request.get("bootstrap_admin_username", "admin").strip()
    password = request.get("bootstrap_admin_password", "")
    if not username or any(char in username for char in '\r\n"'):
        raise InstallerError("bootstrap administrator username is invalid")
    if mode == "fresh" and (len(password) < 12 or password == "admin123"):
        raise InstallerError("a strong non-default bootstrap administrator password is required")
    for key in ("dnspod_secret_id", "dnspod_secret_key"):
        value = request.get(key, "")
        if any(char in value for char in '\r\n"'):
            raise InstallerError(f"{key} contains unsupported characters")


def _port_is_open(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0
    finally:
        sock.close()


def _existing_disk_path(path: Path) -> Path:
    """Return the nearest existing parent for disk-usage checks."""
    candidate = _full(path)
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _run(command: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding=locale.getpreferredencoding(False),
        errors="replace",
        timeout=timeout,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _listening_pids(port: int) -> list[int]:
    if os.name != "nt":
        return []
    result = _run(["netstat", "-ano", "-p", "TCP"])
    pids: list[int] = []
    for line in (result.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
            local = parts[1].rsplit(":", 1)
            if len(local) == 2 and local[1] == str(port):
                try:
                    pids.append(int(parts[4]))
                except ValueError:
                    continue
    return sorted(set(pids))


def _process_path(pid: int) -> Path | None:
    if os.name != "nt":
        return None
    command = (
        "(Get-CimInstance Win32_Process -Filter \"ProcessId = %d\").ExecutablePath" % pid
    )
    result = _run(["powershell", "-NoProfile", "-Command", command])
    value = result.stdout.strip()
    return _full(value) if result.returncode == 0 and value else None


def _named_processes() -> list[tuple[int, Path | None]]:
    if os.name != "nt":
        return []
    command = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -in @('ServerManager.exe','caddy.exe') } | "
        "ForEach-Object { '{0}`t{1}' -f $_.ProcessId,$_.ExecutablePath }"
    )
    result = _run(["powershell", "-NoProfile", "-Command", command])
    processes: list[tuple[int, Path | None]] = []
    for line in (result.stdout or "").splitlines():
        parts = line.split("\t", 1)
        if not parts or not parts[0].isdigit():
            continue
        processes.append((int(parts[0]), _full(parts[1]) if len(parts) > 1 and parts[1] else None))
    return processes


def _stop_owned_processes(app_dir: Path) -> None:
    app_dir = _full(app_dir)
    pids_to_stop: set[int] = set()
    for port in (FIXED_SERVER_PORT, FIXED_PUBLIC_HTTP_PORT, FIXED_PUBLIC_HTTPS_PORT, 2019):
        try:
            port_number = int(port)
        except ValueError:
            continue
        for pid in _listening_pids(port_number):
            executable = _process_path(pid)
            if executable is None or not _is_within(app_dir, executable):
                raise InstallerError(
                    f"port {port_number} is occupied by an unknown process (pid={pid})"
                )
            pids_to_stop.add(pid)
    for pid, executable in _named_processes():
        if executable is not None and _is_within(app_dir, executable):
            pids_to_stop.add(pid)
    for pid in sorted(pids_to_stop):
        result = _run(["taskkill", "/PID", str(pid), "/T"], timeout=15)
        if result.returncode != 0:
            raise InstallerError(f"unable to stop SM process pid={pid}")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if not any(
            _port_is_open(port)
            for port in (FIXED_SERVER_PORT, FIXED_PUBLIC_HTTP_PORT, FIXED_PUBLIC_HTTPS_PORT, 2019)
        ) and not any(
            executable is not None and _is_within(app_dir, executable)
            for _, executable in _named_processes()
        ):
            return
        time.sleep(0.2)
    raise InstallerError("SM application or Caddy ports did not become free")


def _port_details(app_dir: Path) -> dict[str, list[dict[str, Any]]]:
    details: dict[str, list[dict[str, Any]]] = {}
    for port in (FIXED_SERVER_PORT, FIXED_PUBLIC_HTTP_PORT, FIXED_PUBLIC_HTTPS_PORT, 2019):
        rows: list[dict[str, Any]] = []
        for pid in _listening_pids(port):
            executable = _process_path(pid)
            rows.append(
                {
                    "pid": pid,
                    "executable": str(executable) if executable else "",
                    "owned_by_selected_app": bool(
                        executable and _is_within(app_dir, executable)
                    ),
                }
            )
        details[str(port)] = rows
    return details


def _probe_writable(path: Path) -> None:
    parent = _existing_disk_path(path)
    if not os.access(parent, os.W_OK):
        raise InstallerError(f"installation path is not writable: {parent}")
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".sc-sm-preflight-", dir=parent, delete=False
        ) as stream:
            probe = Path(stream.name)
        probe.unlink()
    except OSError as exc:
        raise InstallerError(f"installation path is not writable: {parent}") from exc


def _environment_report(app_dir: Path, data_dir: Path) -> dict[str, Any]:
    app_dir = _full(app_dir)
    data_dir = _full(data_dir)
    for target in (app_dir, data_dir):
        if target.exists():
            _assert_safe_tree(target)
    disk_paths = {
        str(_existing_disk_path(app_dir.parent)),
        str(_existing_disk_path(data_dir.parent)),
    }
    disk_free = {}
    for path_text in sorted(disk_paths):
        try:
            disk_free[path_text] = shutil.disk_usage(path_text).free
        except OSError as exc:
            raise InstallerError(f"unable to inspect free disk space: {path_text}") from exc
    if any(free < MINIMUM_FREE_BYTES for free in disk_free.values()):
        raise InstallerError("insufficient free disk space for SM installation")
    _probe_writable(app_dir.parent)
    _probe_writable(data_dir.parent)
    port_details = _port_details(app_dir)
    unknown_ports = {
        port: rows
        for port, rows in port_details.items()
        if any(not row["owned_by_selected_app"] for row in rows)
    }
    if unknown_ports:
        raise InstallerError(f"required port is occupied by another process: {unknown_ports}")
    return {
        "product": "server_manager",
        "status": "passed",
        "app_dir": str(app_dir),
        "data_dir": str(data_dir),
        "ports": port_details,
        "disk_free_bytes": disk_free,
        "firewall_rules": {
            name: _firewall_rule_exists(name) for name in MANAGED_FIREWALL_RULES
        },
    }


def _firewall_rule_exists(name: str) -> bool:
    if os.name != "nt":
        return False
    result = _run(["netsh", "advfirewall", "firewall", "show", "rule", f"name={name}"])
    return result.returncode == 0 and name.lower() in (result.stdout or "").lower()


def _configure_firewall() -> list[str]:
    actions: list[str] = []
    if os.name != "nt":
        return actions
    for name, port in MANAGED_FIREWALL_RULES.items():
        result = _run(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={name}"])
        if result.returncode not in (0, 1):
            raise InstallerError(f"unable to prepare Windows firewall rule: {name}")
        result = _run(
            [
                "netsh",
                "advfirewall",
                "firewall",
                "add",
                "rule",
                f"name={name}",
                "dir=in",
                "action=allow",
                "protocol=TCP",
                f"localport={port}",
                "profile=any",
                "enable=yes",
            ]
        )
        if result.returncode != 0:
            raise InstallerError(f"unable to create Windows firewall rule: {name}")
        actions.append(f"{name}:TCP:{port}")
    return actions


def _restore_firewall(state: dict[str, Any]) -> None:
    if os.name != "nt":
        return
    before = state.get("firewall_rules_before") or {}
    for name, port in MANAGED_FIREWALL_RULES.items():
        _run(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={name}"])
        if before.get(name):
            _run(
                [
                    "netsh",
                    "advfirewall",
                    "firewall",
                    "add",
                    "rule",
                    f"name={name}",
                    "dir=in",
                    "action=allow",
                    "protocol=TCP",
                    f"localport={port}",
                    "profile=any",
                    "enable=yes",
                ]
            )


def _acquire_lock(lock_path: Path, transaction_id: str) -> None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise InstallerError("another SM installer transaction is already running") from exc
    try:
        os.write(descriptor, transaction_id.encode("ascii"))
    finally:
        os.close(descriptor)
    _set_runtime_acl(lock_path)


def _release_lock(state: dict[str, Any]) -> None:
    lock_text = state.get("lock_path")
    if not lock_text:
        return
    lock_path = _full(lock_text)
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass


def _batch_value(value: str) -> str:
    if any(char in value for char in '\r\n"'):
        raise InstallerError("local configuration contains unsupported characters")
    return value


def _write_local_config(runtime_root: Path, request: dict[str, str]) -> None:
    runtime_root.mkdir(parents=True, exist_ok=True)
    lines = [
        "@echo off",
        "rem Generated by the SC Server Manager installer. Do not commit this file.",
        f'set "SM_DNSPOD_SECRET_ID={_batch_value(request.get("dnspod_secret_id", ""))}"',
        f'set "SM_DNSPOD_SECRET_KEY={_batch_value(request.get("dnspod_secret_key", ""))}"',
    ]
    path = runtime_root / "sm.local.bat"
    path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    if os.name == "nt":
        result = _run(
            [
                "icacls",
                str(path),
                "/inheritance:r",
                "/grant:r",
                "*S-1-5-18:F",
                "*S-1-5-32-544:F",
            ]
        )
        if result.returncode != 0:
            raise InstallerError("unable to protect SM local configuration")


def _load_shared_sm_modules(data_dir: Path, database_path: Path, request: dict[str, str]):
    os.environ.update(
        {
            "SM_ENVIRONMENT": "production",
            "SM_DATA_DIR": str(data_dir),
            "SERVER_MANAGER_DB_PATH": str(database_path),
            "SM_SOFTWARE_STORAGE_DIR": str(data_dir / "software"),
            "SM_DNSPOD_MODE": "real",
            "SM_DNSPOD_SECRET_ID": request.get("dnspod_secret_id", ""),
            "SM_DNSPOD_SECRET_KEY": request.get("dnspod_secret_key", ""),
            "SM_BOOTSTRAP_ADMIN_USERNAME": request.get("bootstrap_admin_username", "admin"),
            "SM_BOOTSTRAP_ADMIN_PASSWORD": request.get("bootstrap_admin_password", ""),
            "SM_CADDY_AUTO_MANAGE": "1",
            "SM_CADDY_REQUIRED": "1",
        }
    )
    source_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    if str(source_root / "Server_manager") not in sys.path:
        sys.path.insert(0, str(source_root / "Server_manager"))
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))
    import config  # type: ignore
    import data_layout  # type: ignore
    import database  # type: ignore

    database._DB_PATH = str(database_path)
    return config, data_layout, database


def _default_deployment() -> dict[str, Any]:
    return {
        "server_host": FIXED_SERVER_HOST,
        "server_port": FIXED_SERVER_PORT,
        "public_http_port": FIXED_PUBLIC_HTTP_PORT,
        "public_https_port": FIXED_PUBLIC_HTTPS_PORT,
        "public_base_url": FIXED_PUBLIC_BASE_URL,
        "caddy_admin": FIXED_CADDY_ADMIN,
        "caddy_auto_manage": True,
        "caddy_required": True,
        "caddy_start_timeout": 10,
        "cookie_secure": True,
        "cookie_samesite": "lax",
        "client_token_ttl_seconds": 86400,
        "software_directory": "software",
        "software_max_upload_bytes": 1024 * 1024 * 1024,
        "software_retention_count": 3,
        "software_session_max_age": 7200,
        "finance_enabled": True,
        "finance_retention_months": 3,
        "finance_cleanup_interval_seconds": 21600,
        "domain_cooldown_seconds": 1800,
        "domain_pool_required": True,
        "dnspod_mode": "real",
    }


def _copy_selected_data(source: Path, stage: Path) -> dict[str, Any]:
    if not source.is_dir():
        raise InstallerError(f"source data directory does not exist: {source}")
    _assert_safe_tree(source)
    database_source = source / "server_manager.db"
    if not database_source.is_file():
        raise InstallerError("source data directory does not contain server_manager.db")
    stage.mkdir(parents=True, exist_ok=True)
    for name in ("deployment.json", "data_manifest.json"):
        candidate = source / name
        if candidate.is_file():
            shutil.copy2(candidate, stage / name)
    for directory_name in ("software", "certificates"):
        candidate = source / directory_name
        if candidate.is_dir():
            _copy_tree(candidate, stage / directory_name)
    return {"source_database": str(database_source)}


def _source_has_certificate_pair(source: Path | None) -> bool:
    if source is None:
        return False
    deployment_path = source / "deployment.json"
    if not deployment_path.is_file():
        return False
    try:
        payload = json.loads(deployment_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    certificate_file = str(payload.get("certificate_file") or "").strip()
    key_file = str(payload.get("key_file") or "").strip()
    if not certificate_file or not key_file:
        return False
    try:
        cert_path = _full(source / certificate_file)
        key_path = _full(source / key_file)
    except (OSError, ValueError):
        return False
    return (
        _is_within(source, cert_path)
        and _is_within(source, key_path)
        and cert_path.is_file()
        and key_path.is_file()
        and cert_path.stat().st_size > 0
        and key_path.stat().st_size > 0
    )


def _source_has_dns_credentials(source: Path | None) -> bool:
    if source is None:
        return False
    database_path = source / "server_manager.db"
    if not database_path.is_file():
        return False
    try:
        connection = sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
        try:
            row = connection.execute(
                "SELECT secret_id, secret_key FROM dns_provider_config WHERE id=1"
            ).fetchone()
            return bool(row and str(row[0] or "").strip() and str(row[1] or "").strip())
        finally:
            connection.close()
    except sqlite3.Error:
        return False


def _sqlite_backup(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        source_uri = f"file:{source.as_posix()}?mode=ro"
        source_conn = sqlite3.connect(source_uri, uri=True)
        target_conn = sqlite3.connect(str(target))
        try:
            source_conn.backup(target_conn)
        finally:
            target_conn.close()
            source_conn.close()
    except sqlite3.Error as exc:
        raise InstallerError(f"unable to create a consistent database copy: {exc}") from exc


def _database_has_super_admin(path: Path) -> bool:
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            row = connection.execute(
                "SELECT 1 FROM accounts WHERE role='super_admin' LIMIT 1"
            ).fetchone()
            return row is not None
        finally:
            connection.close()
    except sqlite3.Error:
        return False


def _prepare_stage_data(source: Path | None, stage: Path, request: dict[str, str]) -> Path:
    stage.mkdir(parents=True, exist_ok=True)
    source_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    if str(source_root / "Server_manager") not in sys.path:
        sys.path.insert(0, str(source_root / "Server_manager"))
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))
    import data_layout as data_layout_metadata  # type: ignore

    if source is None:
        deployment = _default_deployment()
    else:
        copy_info = _copy_selected_data(source, stage)
        _sqlite_backup(Path(copy_info["source_database"]), stage / "server_manager.db")
        deployment = _default_deployment()
        deployment_path = stage / "deployment.json"
        if deployment_path.is_file():
            try:
                old = data_layout_metadata.load_deployment_config(stage)
            except Exception as exc:
                raise InstallerError("source deployment.json is invalid") from exc
            deployment.update({key: old[key] for key in old if key in deployment})

    deployment.update(
        {
            "server_host": FIXED_SERVER_HOST,
            "server_port": FIXED_SERVER_PORT,
            "public_http_port": FIXED_PUBLIC_HTTP_PORT,
            "public_https_port": FIXED_PUBLIC_HTTPS_PORT,
            "public_base_url": FIXED_PUBLIC_BASE_URL,
            "caddy_admin": FIXED_CADDY_ADMIN,
            "caddy_auto_manage": True,
            "caddy_required": True,
            "cookie_secure": True,
            "domain_pool_required": True,
            "dnspod_mode": "real",
            "software_directory": "software",
        }
    )

    certificate_source = request.get("certificate_source", "").strip()
    key_source = request.get("key_source", "").strip()
    if bool(certificate_source) != bool(key_source):
        raise InstallerError("certificate and private key must be supplied together")
    if certificate_source:
        cert_target = stage / "certificates" / "server.crt"
        key_target = stage / "certificates" / "server.key"
        cert_path = _full(certificate_source)
        key_path = _full(key_source)
        if not cert_path.is_file() or not key_path.is_file():
            raise InstallerError("certificate or private key file does not exist")
        cert_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cert_path, cert_target)
        shutil.copy2(key_path, key_target)
        deployment["certificate_file"] = "certificates/server.crt"
        deployment["key_file"] = "certificates/server.key"

    certificate_file = str(deployment.get("certificate_file") or "").strip()
    key_file = str(deployment.get("key_file") or "").strip()
    if bool(certificate_file) != bool(key_file):
        raise InstallerError("deployment certificate and private key must be configured together")
    if not certificate_file or not key_file:
        raise InstallerError(
            "a certificate and private key are required because SM uses non-standard public ports"
        )
    try:
        cert_path = data_layout_metadata.resolve_relative_path(stage, certificate_file)
        key_path = data_layout_metadata.resolve_relative_path(stage, key_file)
    except Exception as exc:
        raise InstallerError("certificate paths must be inside the data directory") from exc
    if not cert_path.is_file() or cert_path.stat().st_size == 0:
        raise InstallerError(f"certificate file is missing or empty: {cert_path}")
    if not key_path.is_file() or key_path.stat().st_size == 0:
        raise InstallerError(f"private key file is missing or empty: {key_path}")

    data_layout_metadata.write_deployment_config(stage, deployment)
    config, data_layout, database = _load_shared_sm_modules(
        stage, stage / "server_manager.db", request
    )
    if source is not None and not _database_has_super_admin(stage / "server_manager.db"):
        _validate_credentials({**request, "mode": "fresh"}, "fresh")
    try:
        database.init_db()
    except Exception as exc:
        raise InstallerError(f"database initialization or migration failed: {exc}") from exc
    manifest_version = int(getattr(database, "DB_SCHEMA_VERSION", 0))
    data_layout.write_data_manifest(
        stage,
        database_schema_version=manifest_version,
        application_version=request.get("application_version", ""),
    )
    errors = config.production_config_errors(stage / "server_manager.db")
    if errors:
        raise InstallerError("production configuration is invalid: " + "; ".join(errors))
    return stage


def _backup_path(runtime_root: Path, name: str) -> Path:
    return runtime_root / ".installer" / "backups" / name


def _prepare(request_path: Path, state_path: Path) -> dict[str, Any]:
    request = _read_ini(request_path)
    mode, app_dir, data_dir, source_dir = _request_paths(request)
    _validate_fixed_configuration(request)
    _validate_credentials(request, mode)
    runtime_root = data_dir.parent
    runtime_root.mkdir(parents=True, exist_ok=True)
    _ensure_installer_directory(runtime_root)
    transaction_id = uuid.uuid4().hex
    transaction_root = runtime_root / ".installer" / "transactions" / transaction_id
    transaction_root.mkdir(parents=True, exist_ok=False)
    lock_path = runtime_root / ".installer" / "install.lock"
    try:
        _acquire_lock(lock_path, transaction_id)
    except Exception:
        _remove_tree(transaction_root)
        raise
    stop_attempted = False
    try:
        if source_dir is not None and not source_dir.is_dir():
            raise InstallerError(f"source data directory does not exist: {source_dir}")
        if mode == "fresh" and data_dir.exists() and any(data_dir.iterdir()):
            raise InstallerError("target data directory is not empty; select upgrade deployment")
        stop_attempted = True
        _stop_owned_processes(app_dir)
        state = {
            "product": "server_manager",
            "transaction_id": transaction_id,
            "phase": "prepared",
            "created_at": time.time(),
            "request_path": str(request_path),
            "app_dir": str(app_dir),
            "data_dir": str(data_dir),
            "source_dir": str(source_dir) if source_dir else "",
            "transaction_root": str(transaction_root),
            "lock_path": str(lock_path),
            "had_app": app_dir.exists(),
            "had_data": data_dir.exists(),
            "firewall_rules_before": {
                name: _firewall_rule_exists(name) for name in MANAGED_FIREWALL_RULES
            },
        }
        _write_json(state_path, state)
        return state
    except Exception:
        _release_lock({"lock_path": str(lock_path)})
        _remove_tree(transaction_root)
        try:
            _remove_file(state_path)
        except Exception:
            pass
        if stop_attempted and (app_dir / "start_sm.bat").is_file():
            try:
                _start_and_check(app_dir)
            except Exception:
                pass
        raise


def _start_and_check(app_dir: Path) -> None:
    launcher = app_dir / "start_sm.bat"
    executable = app_dir / "ServerManager.exe"
    caddy = app_dir / "caddy" / "caddy.exe"
    if not launcher.is_file() or not executable.is_file() or not caddy.is_file():
        raise InstallerError("installed SM files are incomplete")
    creation_flags = (
        getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )
    subprocess.Popen(
        ["cmd.exe", "/d", "/c", str(launcher)],
        cwd=str(app_dir),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creation_flags,
        close_fds=True,
    )
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{FIXED_SERVER_PORT}/ping", timeout=2
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
                if payload.get("status") == "pong":
                    return
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            time.sleep(0.5)
    raise InstallerError("SM local health check did not succeed")


def _discard_transaction(
    state: dict[str, Any],
    state_path: Path,
    *,
    app_dir: Path | None = None,
    data_dir: Path | None = None,
) -> None:
    """Discard failed installer output without touching an external upgrade source."""
    runtime_root = _full(state_path).parent.parent
    app_dir = _required_path(app_dir or state.get("app_dir"), "application directory")
    data_dir = _required_path(data_dir or state.get("data_dir"), "data directory")
    expected_data_dir = runtime_root / "data"
    if data_dir != expected_data_dir:
        raise InstallerError("refusing to discard data outside the SM runtime directory")
    if (
        app_dir == app_dir.parent
        or data_dir == data_dir.parent
        or app_dir == runtime_root
        or app_dir == runtime_root.parent
    ):
        raise InstallerError("refusing to discard an unsafe installation path")

    _stop_owned_processes(app_dir)
    _remove_tree(app_dir)
    _remove_tree(data_dir)
    try:
        _restore_firewall(state)
    except Exception:
        pass

    # The state file is diagnostic metadata.  Cleanup is anchored to the
    # installer directory so a corrupt or tampered transaction path cannot
    # redirect deletion outside the SM runtime.
    _remove_tree(runtime_root / ".installer" / "transactions")
    _remove_file(runtime_root / "sm.local.bat")
    _remove_file(runtime_root / ".installer" / "install.lock")
    _remove_file(state_path)


def _discard_stale(
    state_path: Path,
    runtime_root: Path,
    app_dir: Path,
    data_dir: Path,
) -> None:
    """Make a failed or unreadable previous transaction non-blocking."""
    state_path = _full(state_path)
    runtime_root = _full(runtime_root)
    installer_dir = runtime_root / ".installer"
    expected_state_path = installer_dir / "transaction.json"
    if state_path != expected_state_path:
        raise InstallerError("installer state path is outside the SM runtime directory")
    installer_dir = _ensure_installer_directory(runtime_root)
    lock_path = installer_dir / "install.lock"
    state: dict[str, Any] = {
        "product": "server_manager",
        "app_dir": str(_full(app_dir)),
        "data_dir": str(_full(data_dir)),
        "lock_path": str(lock_path),
        "transaction_root": str(installer_dir / "transactions"),
        "phase": "stale",
    }
    if state_path.is_file():
        try:
            loaded = _load_state(state_path)
            state.update(loaded)
        except InstallerError:
            # The metadata itself may have a broken ACL or be truncated.
            _set_runtime_acl(state_path)
    if state.get("phase") == "committed":
        # A committed deployment is already live.  Only remove installer
        # metadata; never treat a stale cleanup as permission to delete app/data.
        _remove_tree(installer_dir / "transactions")
        _remove_file(lock_path)
        _remove_file(state_path)
        return
    _discard_transaction(
        state,
        state_path,
        app_dir=app_dir,
        data_dir=data_dir,
    )


def _environment_preflight(
    app_dir: Path,
    data_dir: Path,
    report_path: Path,
) -> dict[str, Any]:
    report = _environment_report(app_dir, data_dir)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(report_path, report)
    return report


def _commit(request_path: Path, state_path: Path) -> dict[str, Any]:
    request = _read_ini(request_path)
    state = _load_state(state_path)
    if state.get("phase") != "prepared":
        raise InstallerError("installer transaction is not ready to commit")
    mode, app_dir, data_dir, requested_source_dir = _request_paths(request)
    if str(app_dir) != state.get("app_dir") or str(data_dir) != state.get("data_dir"):
        raise InstallerError("installer request does not match transaction state")
    source_dir = (
        _full(state["source_dir"])
        if mode == "upgrade" and state.get("source_dir")
        else requested_source_dir
    )
    transaction_root = _full(state["transaction_root"])
    stage_data = transaction_root / "data-stage"
    try:
        state["phase"] = "committing"
        _write_json(state_path, state)
        _prepare_stage_data(source_dir, stage_data, request)
        _remove_tree(data_dir)
        shutil.move(str(stage_data), str(data_dir))
        _write_local_config(data_dir.parent, request)
        firewall_actions = _configure_firewall()
        _start_and_check(app_dir)
        state.update(
            {
                "phase": "committed",
                "mode": mode,
                "database": str(data_dir / "server_manager.db"),
                "firewall_actions": firewall_actions,
                "completed_at": time.time(),
            }
        )
        _write_json(state_path, state)
        _write_json(transaction_root / "result.json", {key: state[key] for key in (
            "product", "transaction_id", "phase", "mode", "database", "completed_at"
        )})
        _release_lock(state)
        try:
            _remove_tree(transaction_root)
            _remove_file(state_path)
        except Exception:
            # A committed deployment must remain usable even if metadata cleanup
            # is temporarily blocked; the next installer launch can remove it.
            pass
        return state
    except Exception as exc:
        try:
            _discard_transaction(state, state_path)
        except Exception as rollback_error:
            raise InstallerError(f"installation failed and cleanup failed: {rollback_error}") from exc
        if isinstance(exc, InstallerError):
            raise
        raise InstallerError(f"installation failed: {exc}") from exc


def _recover(state_path: Path) -> None:
    state = _load_state(state_path)
    _discard_transaction(state, state_path)


def _preflight(request_path: Path, report_path: Path) -> dict[str, Any]:
    request = _read_ini(request_path)
    mode, app_dir, data_dir, source_dir = _request_paths(request)
    _validate_fixed_configuration(request)
    _validate_credentials(request, mode)
    report = _environment_report(app_dir, data_dir)
    report.update(
        {
            "mode": mode,
            "source_data": str(source_dir) if source_dir else "",
            "fixed_public_url": FIXED_PUBLIC_BASE_URL,
            "source_exists": source_dir.is_dir() if source_dir else True,
            "external_network_check": "manual",
        }
    )
    if source_dir and not source_dir.is_dir():
        raise InstallerError(f"source data directory does not exist: {source_dir}")
    has_requested_certificate = bool(
        request.get("certificate_source", "").strip()
        and request.get("key_source", "").strip()
    )
    if has_requested_certificate:
        requested_cert = _full(request["certificate_source"])
        requested_key = _full(request["key_source"])
        if (
            not requested_cert.is_file()
            or requested_cert.stat().st_size == 0
            or not requested_key.is_file()
            or requested_key.stat().st_size == 0
        ):
            raise InstallerError("certificate or private key file is missing or empty")
    if not has_requested_certificate and not _source_has_certificate_pair(source_dir):
        raise InstallerError(
            "a certificate/private-key pair is required for a non-standard public endpoint"
        )
    has_requested_dns = bool(
        request.get("dnspod_secret_id", "").strip()
        and request.get("dnspod_secret_key", "").strip()
    )
    if not has_requested_dns and not _source_has_dns_credentials(source_dir):
        raise InstallerError("DNSPod Secret ID and Secret Key are required for the production domain pool")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(report_path, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SC Server Manager deployment helper")
    parser.add_argument("--environment-preflight", action="store_true")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--discard-stale", action="store_true")
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--recover", action="store_true")
    parser.add_argument("--app-dir", type=Path)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--runtime-root", type=Path)
    parser.add_argument("--request-file", type=Path)
    parser.add_argument("--report-file", type=Path)
    parser.add_argument("--state-file", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.environment_preflight:
            if (
                not args.app_dir
                or not args.data_dir
                or not args.report_file
            ):
                raise InstallerError(
                    "environment preflight requires app, data, and report paths"
                )
            _environment_preflight(args.app_dir, args.data_dir, args.report_file)
        elif args.preflight:
            if not args.request_file or not args.report_file:
                raise InstallerError("preflight requires request and report files")
            _preflight(args.request_file, args.report_file)
        elif args.prepare:
            if not args.request_file or not args.state_file:
                raise InstallerError("prepare requires request and state files")
            _prepare(args.request_file, args.state_file)
        elif args.commit:
            if not args.request_file or not args.state_file:
                raise InstallerError("commit requires request and state files")
            _commit(args.request_file, args.state_file)
        elif args.discard_stale:
            if not args.state_file or not args.runtime_root or not args.app_dir or not args.data_dir:
                raise InstallerError(
                    "discard-stale requires state, runtime, app, and data paths"
                )
            _discard_stale(args.state_file, args.runtime_root, args.app_dir, args.data_dir)
        elif args.rollback or args.recover:
            if not args.state_file:
                raise InstallerError("discard operation requires a state file")
            _recover(args.state_file)
        else:
            raise InstallerError("one installer operation is required")
        return 0
    except Exception as exc:
        if (args.preflight or args.environment_preflight) and args.report_file:
            try:
                _write_json(
                    args.report_file,
                    {
                        "product": "server_manager",
                        "status": "failed",
                        "error": str(exc),
                    },
                )
            except Exception:
                pass
        print(f"SM installer helper failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
