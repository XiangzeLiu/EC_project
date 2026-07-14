from __future__ import annotations

"""In-memory broker access gate keyed by (client username, server_id)."""

import time
from typing import Any

_GRACE_SECONDS = 10.0
_gates: dict[tuple[str, str], dict[str, Any]] = {}


def _now() -> float:
    return time.time()


def _key(username: str, server_id: str) -> tuple[str, str]:
    return ((username or '').strip(), (server_id or '').strip())


def _empty_status(username: str, server_id: str, status: str = 'not_logged_in') -> dict[str, Any]:
    return {
        'active': False,
        'status': status,
        'username': (username or '').strip(),
        'server_id': (server_id or '').strip(),
        'account_username': '',
        'grace_remaining': 0,
        'updated_at': 0.0,
    }


def _status_record(username: str, server_id: str) -> dict[str, Any]:
    key = _key(username, server_id)
    rec = _gates.get(key)
    if not rec:
        return _empty_status(username, server_id)

    remaining = max(0, int(rec.get('grace_until', 0.0) - _now()))
    status = rec.get('status', 'active')
    if status == 'grace_pending' and remaining <= 0:
        _gates.pop(key, None)
        return _empty_status(username, server_id, status='expired')

    active = status == 'active' or (status == 'grace_pending' and remaining > 0)
    return {
        'active': active,
        'status': status,
        'username': rec.get('username', ''),
        'server_id': rec.get('server_id', ''),
        'account_username': rec.get('account_username', ''),
        'grace_remaining': remaining,
        'updated_at': rec.get('updated_at', 0.0),
    }


def login_gate(username: str, server_id: str, account_username: str, account_password: str) -> dict[str, Any]:
    rec = {
        'username': (username or '').strip(),
        'server_id': (server_id or '').strip(),
        'account_username': (account_username or '').strip(),
        'status': 'active',
        'grace_until': 0.0,
        'updated_at': _now(),
    }
    _gates[_key(username, server_id)] = rec
    return _status_record(username, server_id)


def start_grace(username: str, server_id: str, grace_seconds: float = _GRACE_SECONDS) -> dict[str, Any]:
    key = _key(username, server_id)
    rec = _gates.get(key)
    if not rec:
        return _status_record(username, server_id)

    rec['status'] = 'grace_pending'
    rec['grace_until'] = _now() + max(0.0, grace_seconds)
    rec['updated_at'] = _now()
    return _status_record(username, server_id)


def restore_gate(username: str, server_id: str) -> dict[str, Any]:
    key = _key(username, server_id)
    rec = _gates.get(key)
    if not rec:
        return _status_record(username, server_id)

    status = _status_record(username, server_id)
    if not status.get('active'):
        return status

    rec['status'] = 'active'
    rec['grace_until'] = 0.0
    rec['updated_at'] = _now()
    return _status_record(username, server_id)


def logout_gate(username: str, server_id: str) -> dict[str, Any]:
    _gates.pop(_key(username, server_id), None)
    return _status_record(username, server_id)


def get_gate_status(username: str, server_id: str) -> dict[str, Any]:
    return _status_record(username, server_id)


def is_gate_active(username: str, server_id: str) -> bool:
    return bool(get_gate_status(username, server_id).get('active'))


def clear_expired() -> None:
    now = _now()
    expired = [
        key
        for key, rec in _gates.items()
        if rec.get('status') == 'grace_pending' and rec.get('grace_until', 0.0) <= now
    ]
    for key in expired:
        _gates.pop(key, None)
