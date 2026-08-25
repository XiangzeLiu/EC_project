"""Independent web sessions for the trader software center."""

from __future__ import annotations

import secrets
import time

import database
from config import SM_COOKIE_SAMESITE, SM_COOKIE_SECURE, SM_SOFTWARE_SESSION_MAX_AGE

SOFTWARE_SESSION_COOKIE = "software_sid"
_sessions: dict[str, dict] = {}


def create_trader_session(account: dict) -> str:
    sid = secrets.token_urlsafe(32)
    _sessions[sid] = {
        "id": account.get("id"),
        "username": account.get("username") or "",
        "role": "trader",
        "created_at": time.time(),
    }
    return sid


def _expired(session: dict) -> bool:
    return time.time() - float(session.get("created_at") or 0) > SM_SOFTWARE_SESSION_MAX_AGE


def get_trader_session(request) -> dict | None:
    sid = str(request.cookies.get(SOFTWARE_SESSION_COOKIE) or "").strip()
    session = _sessions.get(sid)
    if not sid or not session:
        return None
    if _expired(session):
        _sessions.pop(sid, None)
        return None
    account = database.get_account_by_username(str(session.get("username") or ""))
    if not account or account.get("role") != "trader" or account.get("status") != "active":
        _sessions.pop(sid, None)
        return None
    return session


def invalidate_session(request) -> None:
    sid = str(request.cookies.get(SOFTWARE_SESSION_COOKIE) or "").strip()
    if sid:
        _sessions.pop(sid, None)


def set_session_cookie(response, sid: str) -> None:
    response.set_cookie(
        SOFTWARE_SESSION_COOKIE,
        sid,
        max_age=SM_SOFTWARE_SESSION_MAX_AGE,
        httponly=True,
        secure=SM_COOKIE_SECURE,
        samesite=SM_COOKIE_SAMESITE,
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(
        SOFTWARE_SESSION_COOKIE,
        secure=SM_COOKIE_SECURE,
        samesite=SM_COOKIE_SAMESITE,
    )
