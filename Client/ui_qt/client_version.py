"""Client version helpers."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path


PLATFORM_PC = 0
_TIMESTAMP_RE = re.compile(r"^\d{14}$")
_BUILD_INFO_PATH = Path(__file__).resolve().parents[1] / "client_build_info.json"


def _load_build_timestamp() -> str:
    # Packaged builds carry this file so the version stays fixed across launches.
    try:
        payload = json.loads(_BUILD_INFO_PATH.read_text(encoding="utf-8-sig"))
        timestamp = str(payload.get("build_timestamp") or "").strip()
        if _TIMESTAMP_RE.fullmatch(timestamp):
            return timestamp
    except (OSError, ValueError, TypeError):
        pass

    override = os.environ.get("SC_CLIENT_BUILD_TIMESTAMP", "").strip()
    if _TIMESTAMP_RE.fullmatch(override):
        return override
    return dt.datetime.now().strftime("%Y%m%d%H%M%S")


_BUILD_TIMESTAMP = _load_build_timestamp()


def packaged_build_info_available() -> bool:
    return _BUILD_INFO_PATH.is_file()


def client_version(platform: int = PLATFORM_PC) -> str:
    return f"v_{platform}_{_BUILD_TIMESTAMP}"
