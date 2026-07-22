"""Render and manage the local TS Caddy process after registration."""

from __future__ import annotations

import os
import http.client
import subprocess
import sys
import threading
import time
from pathlib import Path

from ..config import (
    DEFAULT_WS_PORT,
    TS_CADDY_ADMIN,
    TS_CADDY_AUTO_MANAGE,
    TS_CADDY_DIR,
    TS_CADDY_EXE,
    TS_CADDY_START_TIMEOUT,
)


_MANAGE_LOCK = threading.Lock()


def _app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def _resolve_from_app(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = _app_dir() / path
    return path.resolve()


def _runtime_dir() -> Path:
    if TS_CADDY_DIR:
        return _resolve_from_app(TS_CADDY_DIR)
    return (_app_dir() / "caddy").resolve()


def _caddy_executable(runtime_dir: Path) -> Path | None:
    candidates: list[Path] = []
    if TS_CADDY_EXE:
        candidates.append(_resolve_from_app(TS_CADDY_EXE))
    candidates.extend((runtime_dir / "caddy.exe", _app_dir() / "caddy.exe"))
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def _parse_admin_address(value: str) -> tuple[str, int]:
    raw = (value or "").strip()
    if raw.startswith("http://"):
        raw = raw[len("http://"):]
    if raw.startswith("https://"):
        raw = raw[len("https://"):]
    host, separator, port_text = raw.rpartition(":")
    if not separator or not host or not port_text.isdigit():
        raise ValueError("TS_CADDY_ADMIN must use host:port format")
    port = int(port_text)
    if port < 1 or port > 65535:
        raise ValueError("TS_CADDY_ADMIN port is out of range")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("TS_CADDY_ADMIN must bind to a loopback address")
    return host, port


def render_ts_caddyfile(
    domain: str,
    ws_port: int = DEFAULT_WS_PORT,
    admin_address: str = TS_CADDY_ADMIN,
) -> str:
    fqdn = (domain or "").strip().lower().strip(".")
    if not fqdn or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for char in fqdn):
        raise ValueError("invalid assigned TS domain")
    _parse_admin_address(admin_address)
    return f"""{{
\tadmin {admin_address}
}}

{fqdn} {{
\tencode zstd gzip

\t@client_ws path /ws
\thandle @client_ws {{
\t\treverse_proxy 127.0.0.1:{int(ws_port)}
\t}}

\t@sm_admin path /api/admin/force-disconnect /api/register/pre-approve-check
\thandle @sm_admin {{
\t\treverse_proxy 127.0.0.1:{int(ws_port)}
\t}}

\thandle {{
\t\trespond 404
\t}}
}}
"""


def _runtime_environment(runtime_dir: Path) -> dict[str, str]:
    data_dir = runtime_dir / "data"
    config_dir = runtime_dir / "config"
    logs_dir = runtime_dir / "logs"
    for directory in (runtime_dir, data_dir, config_dir, logs_dir):
        directory.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["XDG_DATA_HOME"] = str(data_dir)
    env["XDG_CONFIG_HOME"] = str(config_dir)
    return env


def _write_config(config_path: Path, content: str) -> None:
    if config_path.exists() and config_path.read_text(encoding="utf-8") == content:
        return
    temp_path = config_path.with_suffix(".tmp")
    temp_path.write_text(content, encoding="utf-8")
    os.replace(temp_path, config_path)


def _run_command(
    args: list[str],
    runtime_dir: Path,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(runtime_dir),
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _admin_ready(admin_address: str) -> bool:
    host, port = _parse_admin_address(admin_address)
    connection = http.client.HTTPConnection(host, port, timeout=0.5)
    try:
        connection.request("GET", "/config/")
        response = connection.getresponse()
        response.read()
        return response.status == 200
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def _log_tail(log_path: Path, limit: int = 20) -> str:
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-limit:])
    except OSError:
        return ""


def _start_detached(
    caddy: Path,
    config_path: Path,
    runtime_dir: Path,
    env: dict[str, str],
    admin_address: str,
) -> dict:
    log_path = runtime_dir / "logs" / "caddy-runtime.log"
    pid_path = runtime_dir / "caddy.pid"
    creation_flags = (
        getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    )
    command = [
        str(caddy),
        "run",
        "--config",
        str(config_path),
        "--adapter",
        "caddyfile",
        "--pidfile",
        str(pid_path),
    ]
    with open(log_path, "a", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            command,
            cwd=str(runtime_dir),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            creationflags=creation_flags,
            close_fds=True,
            start_new_session=(os.name != "nt"),
        )

    deadline = time.monotonic() + TS_CADDY_START_TIMEOUT
    while time.monotonic() < deadline:
        return_code = process.poll()
        if return_code is not None:
            return {
                "ok": False,
                "reason": f"Caddy exited during startup with code {return_code}",
                "log_tail": _log_tail(log_path),
            }
        if _admin_ready(admin_address):
            return {
                "ok": True,
                "action": "started",
                "pid": process.pid,
                "log_path": str(log_path),
            }
        time.sleep(0.2)

    try:
        process.terminate()
    except OSError:
        pass
    return {
        "ok": False,
        "reason": "Caddy admin endpoint did not become ready before timeout",
        "log_tail": _log_tail(log_path),
    }


def configure_and_start_caddy(domain: str, ws_port: int = DEFAULT_WS_PORT) -> dict:
    if not TS_CADDY_AUTO_MANAGE:
        return {"ok": False, "skipped": True, "reason": "automatic Caddy management disabled"}

    with _MANAGE_LOCK:
        runtime_dir = _runtime_dir()
        env = _runtime_environment(runtime_dir)
        caddy = _caddy_executable(runtime_dir)
        if caddy is None:
            return {
                "ok": False,
                "skipped": True,
                "reason": "caddy.exe not found",
                "runtime_dir": str(runtime_dir),
            }

        try:
            config_path = runtime_dir / "Caddyfile"
            _write_config(config_path, render_ts_caddyfile(domain, ws_port))

            validate = _run_command(
                [str(caddy), "validate", "--config", str(config_path), "--adapter", "caddyfile"],
                runtime_dir,
                env,
            )
            if validate.returncode != 0:
                return {
                    "ok": False,
                    "reason": (validate.stderr or validate.stdout or "Caddy validation failed").strip(),
                    "config_path": str(config_path),
                }

            reload_result = _run_command(
                [
                    str(caddy),
                    "reload",
                    "--config",
                    str(config_path),
                    "--adapter",
                    "caddyfile",
                    "--address",
                    TS_CADDY_ADMIN,
                ],
                runtime_dir,
                env,
            )
            if reload_result.returncode == 0:
                return {
                    "ok": True,
                    "action": "reloaded",
                    "config_path": str(config_path),
                    "caddy_exe": str(caddy),
                }

            started = _start_detached(
                caddy,
                config_path,
                runtime_dir,
                env,
                TS_CADDY_ADMIN,
            )
            started.update({"config_path": str(config_path), "caddy_exe": str(caddy)})
            if not started.get("ok"):
                started["reload_error"] = (
                    reload_result.stderr or reload_result.stdout or "Caddy reload failed"
                ).strip()
            return started
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            return {"ok": False, "reason": str(exc), "runtime_dir": str(runtime_dir)}
