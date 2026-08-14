"""Execute pending-registration IB validation jobs on the TS host."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import urllib.error
import urllib.request

from ..api.interactive_brokers import IBBroker
from ..config import load_register_state
from .https_client import urlopen

log = logging.getLogger("trader_server.ib_registration_validation")

_worker_lock = threading.Lock()
_worker_thread: threading.Thread | None = None


def _is_ib(value: str) -> bool:
    return str(value or "").strip().lower() in {"ib", "interactive_brokers"}


async def _validate_local_gateway() -> dict:
    broker = IBBroker()
    try:
        connected = await broker.connect(
            {"host": "127.0.0.1", "port": 4001, "client_id": 1, "account_id": ""}
        )
        if not connected:
            error = broker.get_connection_error()
            return {
                "ok": False,
                "code": str(error.get("code") or "IB_GATEWAY_UNREACHABLE"),
                "error": str(error.get("message") or "Unable to connect to IB Gateway"),
            }
        accounts = await broker.get_accounts()
        if not accounts:
            return {"ok": False, "code": "IB_NO_MANAGED_ACCOUNTS", "error": "No managed IB accounts returned"}
        return {"ok": True, "accounts": accounts}
    except Exception as exc:
        return {"ok": False, "code": "IB_VALIDATION_FAILED", "error": str(exc)[:240]}
    finally:
        await broker.disconnect()


def _json_request(url: str, secret: str, payload: dict, timeout: int) -> dict:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json")
    request.add_header("Authorization", f"Bearer {secret}")
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def _worker_loop() -> None:
    while True:
        pending = load_register_state()
        if not pending:
            return
        request_id = str(pending.get("request_id") or "").strip()
        secret = str(pending.get("validation_secret") or "").strip()
        broker_type = str(pending.get("broker_type") or "").strip()
        manager_url = str(pending.get("manager_url") or "").rstrip("/")
        if not _is_ib(broker_type):
            return
        if not request_id or not secret or not manager_url:
            log.error("Pending IB registration cannot validate: missing request identity")
            return

        try:
            polled = _json_request(
                f"{manager_url}/nodes/registration-validation/poll",
                secret,
                {"request_id": request_id},
                timeout=30,
            )
            job = polled.get("job") if isinstance(polled, dict) else None
            if not job:
                continue
            job_id = str(job.get("job_id") or "").strip()
            if not job_id:
                time.sleep(1)
                continue
            result = asyncio.run(_validate_local_gateway())
            _json_request(
                f"{manager_url}/nodes/registration-validation/result",
                secret,
                {"request_id": request_id, "job_id": job_id, "result": result},
                timeout=15,
            )
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403, 404, 409, 410}:
                log.error("Pending IB validation worker stopped after HTTP %s", exc.code)
                return
            log.warning("Pending IB validation poll HTTP %s", exc.code)
            time.sleep(2)
        except Exception as exc:
            log.warning("Pending IB validation poll failed: %s", exc)
            time.sleep(2)


def start_pending_ib_validation_worker() -> bool:
    global _worker_thread
    pending = load_register_state()
    if not pending or not _is_ib(str(pending.get("broker_type") or "")):
        return False
    with _worker_lock:
        if _worker_thread and _worker_thread.is_alive():
            return True
        _worker_thread = threading.Thread(
            target=_worker_loop,
            name="pending-ib-validation",
            daemon=True,
        )
        _worker_thread.start()
        return True


__all__ = ["start_pending_ib_validation_worker"]
