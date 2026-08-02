"""Client version helpers."""

from __future__ import annotations

import datetime as dt
import os


PLATFORM_PC = 0
_BUILD_TIMESTAMP = os.environ.get("SC_CLIENT_BUILD_TIMESTAMP", "").strip()
if not _BUILD_TIMESTAMP:
    _BUILD_TIMESTAMP = dt.datetime.now().strftime("%Y%m%d%H%M%S")


def client_version(platform: int = PLATFORM_PC) -> str:
    return f"v_{platform}_{_BUILD_TIMESTAMP}"
