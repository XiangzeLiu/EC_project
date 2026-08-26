"""Low-priority TS finance reporter isolated from trading lifecycle operations."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import urllib.error
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from ..api.base import FinanceCollectionSkipped
from ..config import (
    TS_FINANCE_ENABLED,
    TS_FINANCE_INTERVAL_SECONDS,
    TS_FINANCE_REQUEST_TIMEOUT_SECONDS,
    state,
)
from .https_client import urlopen


log = logging.getLogger("trader_server.finance_reporter")
NY_TZ = ZoneInfo("America/New_York")
EXTENDED_SESSION_START = time(4, 0)
EXTENDED_SESSION_END = time(20, 30)
FINAL_COLLECTION_START = time(23, 45)

_reporter_task: asyncio.Task | None = None
_wake_event: asyncio.Event | None = None
_cycle_lock: asyncio.Lock | None = None
_pending_account_key = ""
_last_account_key = ""


def _account_key(broker_type: str, account_id: str, currency: str) -> str:
    raw_type = str(broker_type or "").strip().lower().replace("-", "_")
    canonical_type = (
        "tastytrade" if raw_type in {"tt", "tastytrade", "tasty_trade"}
        else "interactive_brokers" if raw_type in {"ib", "ibkr", "interactive_brokers", "interactivebrokers"}
        else raw_type
    )
    raw = f"{canonical_type}:{str(account_id or '').strip().upper()}:{str(currency or 'USD').strip().upper()}"
    return f"fin_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]}"


def _observed_fixed_holiday(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _nth_weekday(year: int, month: int, weekday: int, ordinal: int) -> date:
    cursor = date(year, month, 1)
    cursor += timedelta(days=(weekday - cursor.weekday()) % 7)
    return cursor + timedelta(days=7 * (ordinal - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    cursor = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return cursor - timedelta(days=(cursor.weekday() - weekday) % 7)


def _easter_sunday(year: int) -> date:
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _is_us_equity_trading_day(day: date) -> bool:
    if day.weekday() >= 5:
        return False
    holidays: set[date] = set()
    for fixed_year in (day.year - 1, day.year, day.year + 1):
        for month, day_of_month in ((1, 1), (6, 19), (7, 4), (12, 25)):
            observed = _observed_fixed_holiday(date(fixed_year, month, day_of_month))
            if observed.year == day.year:
                holidays.add(observed)
    holidays.update({
        _nth_weekday(day.year, 1, 0, 3),
        _nth_weekday(day.year, 2, 0, 3),
        _easter_sunday(day.year) - timedelta(days=2),
        _last_weekday(day.year, 5, 0),
        _nth_weekday(day.year, 9, 0, 1),
        _nth_weekday(day.year, 11, 3, 4),
    })
    return day not in holidays


def _automatic_report_kind(now: datetime | None = None) -> str | None:
    """Return the low-priority collection work appropriate for the current ET time."""
    local_now = (now or datetime.now(timezone.utc)).astimezone(NY_TZ)
    if not _is_us_equity_trading_day(local_now.date()):
        return None
    local_time = local_now.time().replace(tzinfo=None)
    if EXTENDED_SESSION_START <= local_time <= EXTENDED_SESSION_END:
        return "current"
    if local_time >= FINAL_COLLECTION_START:
        return "finalization"
    return None


def start_finance_reporter() -> None:
    global _reporter_task, _wake_event, _cycle_lock
    if not TS_FINANCE_ENABLED or state.is_shutting_down:
        return
    if _reporter_task and not _reporter_task.done():
        return
    _wake_event = asyncio.Event()
    _cycle_lock = asyncio.Lock()
    _reporter_task = asyncio.create_task(_reporter_loop(), name="ts-finance-reporter")
    log.info("Finance reporter started (interval=%ss)", TS_FINANCE_INTERVAL_SECONDS)


def request_manual_collection(account_key: str) -> bool:
    """Wake the reporter for an SM-authorized account-level collection request."""
    global _pending_account_key
    key = str(account_key or "").strip()
    if not key.startswith("fin_"):
        return False
    _pending_account_key = key
    if _wake_event is not None:
        _wake_event.set()
        return True
    return False


async def stop_finance_reporter() -> None:
    global _reporter_task, _wake_event, _cycle_lock, _pending_account_key
    task = _reporter_task
    _reporter_task = None
    if task and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    _wake_event = None
    _cycle_lock = None
    _pending_account_key = ""


def _post_report(payload: dict[str, Any]) -> dict[str, Any]:
    url = f"{state.manager_url.rstrip('/')}/nodes/finance/report"
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {state.token}",
            "Content-Type": "application/json",
        },
    )
    with urlopen(request, timeout=TS_FINANCE_REQUEST_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


async def _collect_one(
    target_date: date,
    report_kind: str,
    expected_account_key: str = "",
) -> dict[str, Any] | None:
    global _last_account_key
    if not state.server_id or not state.token or not state.manager_url:
        return None

    from .config_sync import get_broker_snapshot

    broker, generation = get_broker_snapshot()
    if broker is None:
        raise FinanceCollectionSkipped("BROKER_NOT_READY", "Broker is not initialized")
    connected = await broker.is_connected()
    if not connected:
        raise FinanceCollectionSkipped("BROKER_NOT_READY", "Broker is not connected")

    broker_payload = await broker.collect_finance_report(
        target_date.isoformat(),
        timeout=TS_FINANCE_REQUEST_TIMEOUT_SECONDS,
    )
    current_broker, current_generation = get_broker_snapshot()
    if current_broker is not broker or current_generation != generation:
        raise FinanceCollectionSkipped("BROKER_CHANGED", "Broker config changed during collection")

    local_account_key = _account_key(
        broker.broker_type,
        str(broker_payload.get("broker_account_id") or ""),
        str(broker_payload.get("currency") or "USD"),
    )
    if expected_account_key and local_account_key != expected_account_key:
        raise FinanceCollectionSkipped(
            "ACCOUNT_CHANGED",
            "Broker account changed before manual finance collection completed",
        )

    report = {
        "schema_version": 2,
        "server_id": state.server_id,
        "broker_type": broker.broker_type,
        "broker_account_id": broker_payload.get("broker_account_id", ""),
        "currency": broker_payload.get("currency", "USD"),
        "trade_date": target_date.isoformat(),
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "report_kind": report_kind,
        "data_status": "completed" if report_kind == "finalization" else "in_progress",
        "balances": broker_payload.get("balances") or {},
        "trades": broker_payload.get("trades") or {},
        "cash_flows": broker_payload.get("cash_flows") or {},
        "pnl": broker_payload.get("pnl") or {},
        "symbols": broker_payload.get("symbols") or [],
        "trade_events": broker_payload.get("trade_events") or [],
        "coverage": broker_payload.get("coverage") or {},
        "warnings": broker_payload.get("warnings") or [],
    }
    response = await asyncio.to_thread(_post_report, report)
    if not response.get("ok"):
        raise RuntimeError(str(response.get("error") or "SM rejected finance report"))
    result = response.get("data") if isinstance(response.get("data"), dict) else {}
    account_key = str(result.get("account_key") or "")
    if account_key:
        _last_account_key = account_key
    return result


async def _run_cycle(expected_account_key: str = "", report_kind: str = "current") -> bool:
    if _cycle_lock is None or _cycle_lock.locked():
        return False
    async with _cycle_lock:
        today = datetime.now(NY_TZ).date()
        result = await _collect_one(today, report_kind, expected_account_key)
        if not result:
            return False
        if report_kind != "current":
            return True
        for raw_date in list(result.get("reconcile_dates") or [])[:1]:
            try:
                reconcile_date = date.fromisoformat(str(raw_date))
            except ValueError:
                continue
            try:
                await _collect_one(reconcile_date, "finalization", expected_account_key)
            except FinanceCollectionSkipped as exc:
                log.info(
                    "Prior-day finance reconciliation skipped [%s]: %s",
                    exc.code,
                    exc.message,
                )
            except Exception as exc:
                # The current-day report has already been persisted. A
                # best-effort TT reconciliation must never turn it into a
                # failed cycle or affect subsequent trading work.
                log.warning("Prior-day finance reconciliation failed: %s", exc)
        return True


async def _reporter_loop() -> None:
    global _pending_account_key
    while TS_FINANCE_ENABLED and not state.is_shutting_down:
        event = _wake_event
        if event is None:
            return
        event.clear()
        expected_key = _pending_account_key
        _pending_account_key = ""
        report_kind = "current" if expected_key else _automatic_report_kind()
        succeeded = report_kind is None
        if report_kind:
            try:
                succeeded = await _run_cycle(expected_key, report_kind)
            except FinanceCollectionSkipped as exc:
                log.info("Finance collection skipped [%s]: %s", exc.code, exc.message)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                log.warning("Finance report transport failed: %s", exc)
            except asyncio.CancelledError:
                raise
            except NotImplementedError:
                log.warning("Current broker does not support finance reporting")
            except Exception as exc:
                log.warning("Finance collection failed: %s", exc)

        delay = TS_FINANCE_INTERVAL_SECONDS
        if not succeeded:
            delay = min(30, TS_FINANCE_INTERVAL_SECONDS)
        try:
            await asyncio.wait_for(event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass


def reporter_status() -> dict[str, Any]:
    return {
        "enabled": bool(TS_FINANCE_ENABLED),
        "running": bool(_reporter_task and not _reporter_task.done()),
        "last_account_key": _last_account_key,
        "pending_manual": bool(_pending_account_key),
    }
