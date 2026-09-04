"""Transactional deployment helper for the SM Windows installer.

The helper is intentionally separate from the running SM application.  It
only prepares data, controls the SM-owned processes, and starts the already
packaged launcher after a successful commit.
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
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
        timeout=timeout,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _listening_pids(port: int) -> list[int]:
    if os.name != "nt":
        return []
    result = _run(["netstat", "-ano", "-p", "TCP"])
    pids: list[int] = []
    for line in result.stdout.splitlines():
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


def _stop_owned_processes(app_dir: Path) -> None:
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
            result = _run(["taskkill", "/PID", str(pid), "/T"], timeout=15)
            if result.returncode != 0:
                raise InstallerError(f"unable to stop SM process pid={pid}")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if not any(
            _port_is_open(port)
            for port in (FIXED_SERVER_PORT, FIXED_PUBLIC_HTTP_PORT, FIXED_PUBLIC_HTTPS_PORT, 2019)
        ):
            return
        time.sleep(0.2)
    raise InstallerError("SM application or Caddy ports did not become free")


def _firewall_rule_exists(name: str) -> bool:
    if os.name != "nt":
        return False
    result = _run(["netsh", "advfirewall", "firewall", "show", "rule", f"name={name}"])
    return result.returncode == 0 and name.lower() in result.stdout.lower()


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
    transaction_id = uuid.uuid4().hex
    transaction_root = runtime_root / ".installer" / "transactions" / transaction_id
    transaction_root.mkdir(parents=True, exist_ok=False)
    lock_path = runtime_root / ".installer" / "install.lock"
    try:
        _acquire_lock(lock_path, transaction_id)
    except Exception:
        _remove_tree(transaction_root)
        raise
    app_backup = transaction_root / "app-backup"
    data_backup = transaction_root / "data-backup"
    source_snapshot = transaction_root / "source-data"
    stop_attempted = False
    try:
        if source_dir is not None and not source_dir.is_dir():
            raise InstallerError(f"source data directory does not exist: {source_dir}")
        if mode == "fresh" and data_dir.exists() and any(data_dir.iterdir()):
            raise InstallerError("target data directory is not empty; select upgrade deployment")
        stop_attempted = True
        _stop_owned_processes(app_dir)
        if source_dir is not None and source_dir == data_dir and data_dir.exists():
            _copy_tree(data_dir, source_snapshot)
            source_dir = source_snapshot
        if app_dir.is_dir():
            _copy_tree(app_dir, app_backup)
        if data_dir.is_dir():
            _copy_tree(data_dir, data_backup)
        state = {
            "product": "server_manager",
            "transaction_id": transaction_id,
            "phase": "prepared",
            "request_path": str(request_path),
            "app_dir": str(app_dir),
            "data_dir": str(data_dir),
            "source_dir": str(source_dir) if source_dir else "",
            "transaction_root": str(transaction_root),
            "lock_path": str(lock_path),
            "app_backup": str(app_backup) if app_backup.exists() else "",
            "data_backup": str(data_backup) if data_backup.exists() else "",
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


def _rollback_state(state: dict[str, Any]) -> None:
    app_dir = _full(state["app_dir"])
    data_dir = _full(state["data_dir"])
    app_backup = _full(state["app_backup"]) if state.get("app_backup") else None
    data_backup = _full(state["data_backup"]) if state.get("data_backup") else None
    try:
        _stop_owned_processes(app_dir)
    except InstallerError:
        pass
    try:
        _restore_firewall(state)
    except Exception:
        pass
    if app_backup and app_backup.exists():
        _remove_tree(app_dir)
        app_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(app_backup), str(app_dir))
    elif not state.get("had_app"):
        _remove_tree(app_dir)
    if data_backup and data_backup.exists():
        _remove_tree(data_dir)
        data_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(data_backup), str(data_dir))
    elif not state.get("had_data"):
        _remove_tree(data_dir)
    restart_error = ""
    if state.get("had_app"):
        try:
            _start_and_check(app_dir)
        except Exception as exc:
            restart_error = str(exc)
    state["phase"] = "rolled_back"
    if restart_error:
        state["restart_error"] = restart_error
    _write_json(_full(state["transaction_root"]) / "state.json", state)
    _release_lock(state)
    if restart_error:
        raise InstallerError(f"previous SM was restored but could not be restarted: {restart_error}")


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
        return state
    except Exception as exc:
        try:
            _rollback_state(state)
        except Exception as rollback_error:
            raise InstallerError(f"installation failed and rollback failed: {rollback_error}") from exc
        if isinstance(exc, InstallerError):
            raise
        raise InstallerError(f"installation failed: {exc}") from exc


def _recover(state_path: Path) -> None:
    state = _load_state(state_path)
    phase = str(state.get("phase") or "")
    if phase == "committed":
        _release_lock(state)
        return
    if phase in {"prepared", "committing"}:
        _rollback_state(state)
        return
    if phase == "rolled_back":
        _release_lock(state)
        return
    raise InstallerError(f"unsupported installer transaction phase: {phase}")


def _preflight(request_path: Path, report_path: Path) -> dict[str, Any]:
    request = _read_ini(request_path)
    mode, app_dir, data_dir, source_dir = _request_paths(request)
    _validate_fixed_configuration(request)
    _validate_credentials(request, mode)
    report: dict[str, Any] = {
        "product": "server_manager",
        "mode": mode,
        "app_dir": str(app_dir),
        "data_dir": str(data_dir),
        "source_data": str(source_dir) if source_dir else "",
        "fixed_public_url": FIXED_PUBLIC_BASE_URL,
        "ports": {
            str(port): {"occupied_before_stop": _port_is_open(port)}
            for port in (FIXED_SERVER_PORT, FIXED_PUBLIC_HTTP_PORT, FIXED_PUBLIC_HTTPS_PORT, 2019)
        },
        "source_exists": source_dir.is_dir() if source_dir else True,
        "external_network_check": "manual",
    }
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
    report["disk_free_bytes"] = disk_free
    report["firewall_rules"] = {
        name: _firewall_rule_exists(name) for name in MANAGED_FIREWALL_RULES
    }
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
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--recover", action="store_true")
    parser.add_argument("--request-file", type=Path)
    parser.add_argument("--report-file", type=Path)
    parser.add_argument("--state-file", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.preflight:
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
        elif args.rollback:
            if not args.state_file:
                raise InstallerError("rollback requires a state file")
            _rollback_state(_load_state(args.state_file))
        elif args.recover:
            if not args.state_file:
                raise InstallerError("recover requires a state file")
            _recover(args.state_file)
        else:
            raise InstallerError("one installer operation is required")
        return 0
    except Exception as exc:
        print(f"SM installer helper failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
