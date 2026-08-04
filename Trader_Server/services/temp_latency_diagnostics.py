"""Temporary TS-side latency diagnostics.

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
_LOCK = threading.Lock()
_SESSION_ID = f"tsdiag_{uuid.uuid4().hex[:12]}"


def _enabled() -> bool:
    return os.environ.get("TS_TEMP_LATENCY_DIAGNOSTICS", "1").strip().lower() not in _DISABLED_VALUES


def _path() -> Path:
    root = Path(__file__).resolve().parents[1] / "data" / "logs" / "latency_diagnostics"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"ts_latency_{datetime.now(_BEIJING).strftime('%Y%m%d')}.jsonl"


def record(event: str, **fields: Any) -> None:
    if not _enabled():
        return
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "utc": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "beijing": now.astimezone(_BEIJING).isoformat(timespec="milliseconds"),
        "event": str(event),
        "session_id": _SESSION_ID,
        **fields,
    }
    try:
        with _LOCK:
            with _path().open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    except OSError:
        pass
