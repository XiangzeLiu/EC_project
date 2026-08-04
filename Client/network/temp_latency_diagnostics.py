"""Temporary latency diagnostics for the Client/TS incident investigation.

TEMP_LATENCY_DIAGNOSTIC: remove this module and its call sites after the
current Client-to-TS latency incident is explained.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


_DISABLED_VALUES = {"0", "false", "no", "off"}
_BEIJING = timezone(timedelta(hours=8))
_ACTIVE_LOCK = threading.RLock()
_ACTIVE: "ClientLatencyDiagnostics | None" = None


def diagnostics_enabled() -> bool:
    """Return whether temporary diagnostics are enabled for this run."""

    return os.environ.get("SC_CLIENT_TEMP_LATENCY_DIAGNOSTICS", "1").strip().lower() not in _DISABLED_VALUES


def diagnostics_root() -> Path:
    appdata = os.environ.get("APPDATA", "").strip()
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / "SC Client" / "diagnostics"


def _timestamp() -> dict[str, str]:
    now = datetime.now(timezone.utc)
    return {
        "utc": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "beijing": now.astimezone(_BEIJING).isoformat(timespec="milliseconds"),
    }


class ClientLatencyDiagnostics:
    """Small, thread-safe JSONL writer for one Client connection session."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._path: Path | None = None
        self._session_id = f"diag_{uuid.uuid4().hex[:12]}"
        self._closed = False

    @property
    def session_id(self) -> str:
        return self._session_id

    def start(self) -> None:
        if not diagnostics_enabled():
            return
        with self._lock:
            if self._path is not None or self._closed:
                return
            try:
                folder = diagnostics_root()
                folder.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now(_BEIJING).strftime("%Y%m%d_%H%M%S")
                self._path = folder / f"client_latency_{stamp}_{self._session_id[-12:]}.jsonl"
                self.record(
                    "diagnostic_started",
                    temporary=True,
                    remove_after_incident=True,
                    note="TEMP_LATENCY_DIAGNOSTIC",
                )
            except OSError:
                self._path = None

        with _ACTIVE_LOCK:
            global _ACTIVE
            _ACTIVE = self

    def record(self, event: str, **fields: Any) -> None:
        with self._lock:
            if self._path is None or self._closed:
                return
            payload: dict[str, Any] = {
                **_timestamp(),
                "event": str(event),
                "session_id": self._session_id,
                **fields,
            }
            try:
                with self._path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
            except OSError:
                pass

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self.record("diagnostic_stopped", temporary=True, remove_after_incident=True)
            self._closed = True
        with _ACTIVE_LOCK:
            global _ACTIVE
            if _ACTIVE is self:
                _ACTIVE = None


def record_active_ui_latency(latency_ms: int) -> None:
    """Record the exact moment the main window applies a latency value."""

    with _ACTIVE_LOCK:
        active = _ACTIVE
    if active is not None:
        active.record("ui_latency_displayed", latency_ms=int(latency_ms))
