"""Finance overview persistence, validation, aggregation, and retention.

The finance subsystem is intentionally independent from the order path.  TS
reports are treated as eventually consistent observations: executions are
merged by broker execution id while account snapshots are accepted only when
their collection timestamp is newer than the stored observation.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import database


NY_TZ = ZoneInfo("America/New_York")
REPORT_SCHEMA_VERSION = 2
SUPPORTED_REPORT_SCHEMA_VERSIONS = {1, 2}
MAX_REPORT_SYMBOLS = 2000
MAX_REPORT_EVENTS = 10000
MAX_QUERY_DAYS = 93
MANUAL_COLLECTION_COOLDOWN_SECONDS = 60
COLLECTION_INTERVAL_MINUTES = 15
EXTENDED_SESSION_START = time(4, 0)
EXTENDED_SESSION_END = time(20, 30)
FINAL_COLLECTION_BUCKET = time(23, 45)


class FinanceValidationError(ValueError):
    """Raised when a finance report or query is outside the accepted contract."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: Any, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise FinanceValidationError(f"{field} format is invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_ny_datetime(value: Any, field: str, *, end_of_day: bool = False) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise FinanceValidationError(f"{field} is required")
    try:
        if len(raw) == 10:
            parsed_day = date.fromisoformat(raw)
            parsed = datetime.combine(
                parsed_day,
                time.max if end_of_day else time.min,
                NY_TZ,
            )
        else:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=NY_TZ)
    except (TypeError, ValueError) as exc:
        raise FinanceValidationError(f"{field} format is invalid") from exc
    return parsed.astimezone(timezone.utc)


def _parse_date(value: Any, field: str) -> date:
    try:
        return date.fromisoformat(str(value or ""))
    except (TypeError, ValueError) as exc:
        raise FinanceValidationError(f"{field} format is invalid") from exc


def _canonical_broker_type(value: Any) -> str:
    raw = str(value or "").strip().lower().replace("-", "_")
    if raw in {"tt", "tastytrade", "tasty_trade"}:
        return "tastytrade"
    if raw in {"ib", "ibkr", "interactive_brokers", "interactivebrokers"}:
        return "interactive_brokers"
    raise FinanceValidationError("Unsupported broker type")


def canonical_broker_type(value: Any) -> str:
    return _canonical_broker_type(value)


def account_key_for(broker_type: Any, broker_account_id: Any, currency: Any = "USD") -> str:
    broker = _canonical_broker_type(broker_type)
    account_id = str(broker_account_id or "").strip().upper()
    normalized_currency = str(currency or "USD").strip().upper()
    if not account_id or len(account_id) > 120:
        raise FinanceValidationError("Broker account is required")
    if normalized_currency != "USD":
        raise FinanceValidationError("Finance overview currently supports USD only")
    digest = hashlib.sha256(f"{broker}:{account_id}:{normalized_currency}".encode("utf-8")).hexdigest()
    return f"fin_{digest[:32]}"


def _number(value: Any, field: str, *, nullable: bool = False) -> float | None:
    if value is None or value == "":
        return None if nullable else 0.0
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise FinanceValidationError(f"{field} must be numeric") from exc
    if not math.isfinite(result) or abs(result) > 1e16:
        raise FinanceValidationError(f"{field} is outside the valid range")
    return result


def _non_negative(value: Any, field: str, *, nullable: bool = False) -> float | None:
    result = _number(value, field, nullable=nullable)
    if result is not None and result < 0:
        raise FinanceValidationError(f"{field} must not be negative")
    return result


def _safe_text(value: Any, *, max_length: int) -> str:
    return str(value or "").strip()[:max_length]


def _event_side(value: Any, field: str) -> str:
    raw = str(value or "").strip().upper()
    if raw in {"BUY", "BOT", "B"}:
        return "buy"
    if raw in {"SELL", "SLD", "S"}:
        return "sell"
    raise FinanceValidationError(f"{field} must be buy or sell")


def _boolean(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _normalize_symbols(raw_symbols: Any) -> list[dict[str, Any]]:
    if raw_symbols is None:
        return []
    if not isinstance(raw_symbols, list) or len(raw_symbols) > MAX_REPORT_SYMBOLS:
        raise FinanceValidationError("Symbol summary count exceeds the limit")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_symbols):
        if not isinstance(item, dict):
            raise FinanceValidationError("Symbol summary format is invalid")
        symbol = _safe_text(item.get("symbol"), max_length=64).upper()
        if not symbol or symbol in seen:
            raise FinanceValidationError("Symbol is invalid or duplicated")
        seen.add(symbol)
        fees = _non_negative(item.get("fees"), f"symbols[{index}].fees", nullable=True)
        buy_amount = _non_negative(item.get("buy_amount"), f"symbols[{index}].buy_amount") or 0.0
        sell_amount = _non_negative(item.get("sell_amount"), f"symbols[{index}].sell_amount") or 0.0
        net = _number(item.get("trade_net_flow"), f"symbols[{index}].trade_net_flow", nullable=True)
        if net is None and fees is not None:
            net = sell_amount - buy_amount - fees
        result.append({
            "symbol": symbol,
            "buy_quantity": _non_negative(item.get("buy_quantity"), f"symbols[{index}].buy_quantity") or 0.0,
            "sell_quantity": _non_negative(item.get("sell_quantity"), f"symbols[{index}].sell_quantity") or 0.0,
            "buy_amount": buy_amount,
            "sell_amount": sell_amount,
            "fees": fees,
            "trade_net_flow": net,
            "trade_count": max(0, int(item.get("trade_count", 0) or 0)),
        })
    return result


def _normalize_events(raw_events: Any, trade_date: date) -> list[dict[str, Any]]:
    if raw_events is None:
        return []
    if not isinstance(raw_events, list) or len(raw_events) > MAX_REPORT_EVENTS:
        raise FinanceValidationError("Trade event count exceeds the limit")
    events: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_events):
        if not isinstance(item, dict):
            raise FinanceValidationError("Trade event format is invalid")
        execution_key = _safe_text(item.get("execution_key") or item.get("id"), max_length=180)
        if not execution_key or execution_key in seen:
            raise FinanceValidationError("Trade execution key is invalid or duplicated")
        seen.add(execution_key)
        executed_at = _parse_datetime(item.get("executed_at"), f"trade_events[{index}].executed_at")
        if executed_at.astimezone(NY_TZ).date() != trade_date:
            raise FinanceValidationError("Trade event date does not match report trade_date")
        symbol = _safe_text(item.get("symbol") or "UNKNOWN", max_length=64).upper() or "UNKNOWN"
        events.append({
            "execution_key": execution_key,
            "trade_date": trade_date.isoformat(),
            "executed_at": executed_at,
            "symbol": symbol,
            "side": _event_side(item.get("side"), f"trade_events[{index}].side"),
            "quantity": _non_negative(item.get("quantity"), f"trade_events[{index}].quantity") or 0.0,
            "gross_amount": _non_negative(item.get("gross_amount"), f"trade_events[{index}].gross_amount") or 0.0,
            "fee": _non_negative(item.get("fee"), f"trade_events[{index}].fee", nullable=True),
            "realized_pnl": _number(item.get("realized_pnl"), f"trade_events[{index}].realized_pnl", nullable=True),
            "is_voided": _boolean(item.get("voided", item.get("is_voided", False))),
        })
    return events


def _normalize_report(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise FinanceValidationError("Finance report format is invalid")
    schema_version = int(payload.get("schema_version", 1) or 1)
    if schema_version not in SUPPORTED_REPORT_SCHEMA_VERSIONS:
        raise FinanceValidationError("Finance report schema version is unsupported")

    broker_type = _canonical_broker_type(payload.get("broker_type"))
    broker_account_id = _safe_text(payload.get("broker_account_id"), max_length=120).upper()
    currency = _safe_text(payload.get("currency") or "USD", max_length=12).upper()
    account_key = account_key_for(broker_type, broker_account_id, currency)
    trade_day = _parse_date(payload.get("trade_date"), "trade_date")
    collected_at = _parse_datetime(payload.get("collected_at"), "collected_at")
    raw_kind = _safe_text(payload.get("report_kind") or "current", max_length=32).lower()
    report_kind = "finalization" if raw_kind == "reconciliation" else raw_kind
    if report_kind not in {"current", "finalization", "late_fee"}:
        raise FinanceValidationError("report_kind is invalid")
    raw_status = _safe_text(payload.get("data_status") or "in_progress", max_length=32).lower()
    data_status = "completed" if raw_status == "completed" else "in_progress"

    balances = payload.get("balances") if isinstance(payload.get("balances"), dict) else {}
    trades = payload.get("trades") if isinstance(payload.get("trades"), dict) else {}
    cash_flows = payload.get("cash_flows") if isinstance(payload.get("cash_flows"), dict) else {}
    pnl = payload.get("pnl") if isinstance(payload.get("pnl"), dict) else {}
    coverage = payload.get("coverage") if isinstance(payload.get("coverage"), dict) else {}
    warnings_raw = payload.get("warnings") or []
    if not isinstance(warnings_raw, list):
        warnings_raw = [warnings_raw]
    warnings = [_safe_text(item, max_length=300) for item in warnings_raw if _safe_text(item, max_length=300)][:20]

    fees = _non_negative(trades.get("fees"), "trades.fees", nullable=True)
    buy_amount = _non_negative(trades.get("buy_amount"), "trades.buy_amount") or 0.0
    sell_amount = _non_negative(trades.get("sell_amount"), "trades.sell_amount") or 0.0
    trade_net_flow = _number(trades.get("trade_net_flow"), "trades.trade_net_flow", nullable=True)
    if trade_net_flow is None and fees is not None:
        trade_net_flow = sell_amount - buy_amount - fees
    activation_at = payload.get("account_activated_at") or payload.get("activated_at")
    normalized_activation = _parse_datetime(activation_at, "account_activated_at") if activation_at else None

    safe_coverage = {
        str(key)[:80]: value
        for key, value in coverage.items()
        if isinstance(value, (str, int, float, bool, type(None)))
    }
    return {
        "schema_version": schema_version,
        "account_key": account_key,
        "broker_type": broker_type,
        "broker_account_id": broker_account_id,
        "currency": currency,
        "trade_date": trade_day.isoformat(),
        "collected_at": collected_at,
        "report_kind": report_kind,
        "data_status": data_status,
        "account_activated_at": normalized_activation,
        "balances": {
            "net_liquidating_value": _number(balances.get("net_liquidating_value"), "balances.net_liquidating_value", nullable=True),
            "cash_balance": _number(balances.get("cash_balance"), "balances.cash_balance", nullable=True),
            "buying_power": _number(balances.get("buying_power"), "balances.buying_power", nullable=True),
        },
        "trades": {
            "buy_amount": buy_amount,
            "sell_amount": sell_amount,
            "fees": fees,
            "trade_net_flow": trade_net_flow,
            "turnover": buy_amount + sell_amount,
            "trade_count": max(0, int(trades.get("trade_count", 0) or 0)),
        },
        # These are retained for protocol compatibility only.  Phase one
        # deliberately excludes non-trading cash movements from aggregation.
        "cash_flows": {
            "deposits": _non_negative(cash_flows.get("deposits"), "cash_flows.deposits", nullable=True),
            "withdrawals": _non_negative(cash_flows.get("withdrawals"), "cash_flows.withdrawals", nullable=True),
            "dividends": _number(cash_flows.get("dividends"), "cash_flows.dividends", nullable=True),
            "interest": _number(cash_flows.get("interest"), "cash_flows.interest", nullable=True),
            "other_cash_flow": _number(cash_flows.get("other_cash_flow"), "cash_flows.other_cash_flow", nullable=True),
        },
        "pnl": {
            "realized_pnl": _number(pnl.get("realized_pnl"), "pnl.realized_pnl", nullable=True),
            "unrealized_pnl": _number(pnl.get("unrealized_pnl"), "pnl.unrealized_pnl", nullable=True),
            "equity_open": _number(pnl.get("equity_open"), "pnl.equity_open", nullable=True),
            "equity_close": _number(pnl.get("equity_close"), "pnl.equity_close", nullable=True),
        },
        "symbols": _normalize_symbols(payload.get("symbols")),
        "trade_events": _normalize_events(payload.get("trade_events"), trade_day),
        "coverage": safe_coverage,
        "warnings": warnings,
        "legacy_aggregate": "trade_events" not in payload,
    }


def _bucket_at(collected_at: datetime) -> str:
    utc = collected_at.astimezone(timezone.utc)
    minute = utc.minute - (utc.minute % COLLECTION_INTERVAL_MINUTES)
    return utc.replace(minute=minute, second=0, microsecond=0).isoformat()


def _as_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _upsert_account_and_source(
    conn,
    report: dict[str, Any],
    server_id: str,
    now_iso: str,
) -> tuple[bool, datetime]:
    existing = conn.execute(
        "SELECT first_trade_date, activated_at FROM finance_accounts WHERE account_key=?",
        (report["account_key"],),
    ).fetchone()
    activation = report["account_activated_at"] or report["collected_at"]
    activation = activation.astimezone(timezone.utc)
    created = existing is None
    if created:
        conn.execute(
            """
            INSERT INTO finance_accounts (
                account_key, broker_type, broker_account_id, currency,
                first_trade_date, first_seen_at, last_seen_at, activated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                report["account_key"], report["broker_type"], report["broker_account_id"],
                report["currency"], activation.astimezone(NY_TZ).date().isoformat(),
                now_iso, now_iso, _as_iso(activation),
            ),
        )
    else:
        raw_activation = str(existing["activated_at"] or "")
        try:
            activation = _parse_datetime(raw_activation, "activated_at") if raw_activation else activation
        except FinanceValidationError:
            pass
        conn.execute(
            "UPDATE finance_accounts SET last_seen_at=? WHERE account_key=?",
            (now_iso, report["account_key"]),
        )

    conn.execute(
        """
        INSERT INTO finance_account_sources (
            account_key, server_id, first_seen_at, last_seen_at,
            last_success_at, last_status, last_error
        ) VALUES (?, ?, ?, ?, '', 'received', '')
        ON CONFLICT(account_key, server_id) DO UPDATE SET
            last_seen_at=excluded.last_seen_at,
            last_status='received',
            last_error=''
        """,
        (report["account_key"], server_id, now_iso, now_iso),
    )
    return created, activation


def _deletion_windows_for_range(conn, account_key: str, start_at: datetime, end_at: datetime) -> list[tuple[datetime, datetime]]:
    rows = conn.execute(
        """
        SELECT start_at, end_at FROM finance_deletion_windows
        WHERE account_key=? AND end_at>=? AND start_at<=?
        """,
        (account_key, _as_iso(start_at), _as_iso(end_at)),
    ).fetchall()
    windows: list[tuple[datetime, datetime]] = []
    for row in rows:
        try:
            windows.append((_parse_datetime(row["start_at"], "start_at"), _parse_datetime(row["end_at"], "end_at")))
        except FinanceValidationError:
            continue
    return windows


def _is_blocked(value: datetime, windows: Iterable[tuple[datetime, datetime]]) -> bool:
    return any(start <= value <= end for start, end in windows)


def _upsert_trade_event(conn, event: dict[str, Any], *, account_key: str, server_id: str, collected_at: datetime, now_iso: str) -> bool:
    existing = conn.execute(
        "SELECT * FROM finance_trade_events WHERE account_key=? AND execution_key=?",
        (account_key, event["execution_key"]),
    ).fetchone()
    collected_iso = _as_iso(collected_at)
    if existing is None:
        conn.execute(
            """
            INSERT INTO finance_trade_events (
                account_key, execution_key, trade_date, executed_at, symbol, side,
                quantity, gross_amount, fee, realized_pnl, is_voided, source_server_id,
                collected_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_key, event["execution_key"], event["trade_date"], _as_iso(event["executed_at"]),
                event["symbol"], event["side"], event["quantity"], event["gross_amount"],
                event["fee"], event["realized_pnl"], int(event["is_voided"]), server_id, collected_iso, now_iso,
            ),
        )
        return True

    old_collected = _parse_datetime(existing["collected_at"], "collected_at")
    incoming_is_newer = collected_at >= old_collected
    fee = existing["fee"]
    realized = existing["realized_pnl"]
    if event["fee"] is not None and (fee is None or incoming_is_newer):
        fee = event["fee"]
    if event["realized_pnl"] is not None and (realized is None or incoming_is_newer):
        realized = event["realized_pnl"]
    if incoming_is_newer:
        trade_date = event["trade_date"]
        executed_at = _as_iso(event["executed_at"])
        symbol = event["symbol"]
        side = event["side"]
        quantity = event["quantity"]
        gross_amount = event["gross_amount"]
        is_voided = int(event["is_voided"])
        source = server_id
        next_collected = collected_iso
    else:
        trade_date = existing["trade_date"]
        executed_at = existing["executed_at"]
        symbol = existing["symbol"]
        side = existing["side"]
        quantity = existing["quantity"]
        gross_amount = existing["gross_amount"]
        is_voided = int(existing["is_voided"] or 0)
        source = existing["source_server_id"]
        next_collected = existing["collected_at"]
    conn.execute(
        """
        UPDATE finance_trade_events
        SET trade_date=?, executed_at=?, symbol=?, side=?, quantity=?, gross_amount=?,
            fee=?, realized_pnl=?, is_voided=?, source_server_id=?, collected_at=?, updated_at=?
        WHERE account_key=? AND execution_key=?
        """,
        (
            trade_date, executed_at, symbol, side, quantity, gross_amount, fee, realized,
            is_voided, source, next_collected, now_iso, account_key, event["execution_key"],
        ),
    )
    return incoming_is_newer or event["fee"] is not None or event["realized_pnl"] is not None


def _latest_snapshot(conn, account_key: str, trade_date: str):
    return conn.execute(
        """
        SELECT * FROM finance_snapshots
        WHERE account_key=? AND trade_date=?
        ORDER BY collected_at DESC, bucket_at DESC
        LIMIT 1
        """,
        (account_key, trade_date),
    ).fetchone()


def _equity_open_from_snapshots(conn, account_key: str, trade_date: str) -> float | None:
    row = conn.execute(
        """
        SELECT equity_open FROM finance_snapshots
        WHERE account_key=? AND trade_date=? AND equity_open IS NOT NULL
        ORDER BY collected_at ASC, bucket_at ASC
        LIMIT 1
        """,
        (account_key, trade_date),
    ).fetchone()
    return float(row[0]) if row and row[0] is not None else None


def _rebuild_symbols(conn, account_key: str, trade_date: str, now_iso: str) -> None:
    conn.execute(
        "DELETE FROM finance_daily_symbols WHERE account_key=? AND trade_date=?",
        (account_key, trade_date),
    )
    rows = conn.execute(
        """
        SELECT symbol,
               SUM(CASE WHEN side='buy' THEN quantity ELSE 0 END) AS buy_quantity,
               SUM(CASE WHEN side='sell' THEN quantity ELSE 0 END) AS sell_quantity,
               SUM(CASE WHEN side='buy' THEN gross_amount ELSE 0 END) AS buy_amount,
               SUM(CASE WHEN side='sell' THEN gross_amount ELSE 0 END) AS sell_amount,
               SUM(CASE WHEN fee IS NULL THEN 1 ELSE 0 END) AS missing_fee_count,
               SUM(COALESCE(fee, 0)) AS fees,
               COUNT(*) AS trade_count
        FROM finance_trade_events
        WHERE account_key=? AND trade_date=? AND is_voided=0
        GROUP BY symbol
        ORDER BY symbol
        """,
        (account_key, trade_date),
    ).fetchall()
    for row in rows:
        fees = None if int(row["missing_fee_count"] or 0) else float(row["fees"] or 0)
        buy_amount = float(row["buy_amount"] or 0)
        sell_amount = float(row["sell_amount"] or 0)
        net = sell_amount - buy_amount - fees if fees is not None else None
        conn.execute(
            """
            INSERT INTO finance_daily_symbols (
                account_key, trade_date, symbol, buy_quantity, sell_quantity,
                buy_amount, sell_amount, fees, trade_net_flow, trade_count, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_key, trade_date, row["symbol"], float(row["buy_quantity"] or 0),
                float(row["sell_quantity"] or 0), buy_amount, sell_amount, fees, net,
                int(row["trade_count"] or 0), now_iso,
            ),
        )


def _rebuild_daily_from_ledger(
    conn,
    account_key: str,
    trade_date: str,
    *,
    report: dict[str, Any] | None,
    server_id: str,
    now_iso: str,
) -> None:
    aggregate = conn.execute(
        """
        SELECT
            SUM(CASE WHEN side='buy' THEN gross_amount ELSE 0 END) AS buy_amount,
            SUM(CASE WHEN side='sell' THEN gross_amount ELSE 0 END) AS sell_amount,
            SUM(CASE WHEN fee IS NULL THEN 1 ELSE 0 END) AS missing_fee_count,
            SUM(COALESCE(fee, 0)) AS fees,
            SUM(CASE WHEN realized_pnl IS NULL THEN 1 ELSE 0 END) AS missing_realized_count,
            SUM(COALESCE(realized_pnl, 0)) AS realized_pnl,
            COUNT(*) AS trade_count
        FROM finance_trade_events
        WHERE account_key=? AND trade_date=? AND is_voided=0
        """,
        (account_key, trade_date),
    ).fetchone()
    event_count = int(aggregate["trade_count"] or 0)
    snapshot = _latest_snapshot(conn, account_key, trade_date)
    existing = conn.execute(
        "SELECT * FROM finance_daily_accounts WHERE account_key=? AND trade_date=?",
        (account_key, trade_date),
    ).fetchone()
    if event_count == 0 and snapshot is None and existing is None:
        return

    buy_amount = float(aggregate["buy_amount"] or 0)
    sell_amount = float(aggregate["sell_amount"] or 0)
    fees = None if event_count and int(aggregate["missing_fee_count"] or 0) else float(aggregate["fees"] or 0)
    trade_net_flow = sell_amount - buy_amount - fees if fees is not None else None
    event_realized = None
    if event_count and not int(aggregate["missing_realized_count"] or 0):
        event_realized = float(aggregate["realized_pnl"] or 0)

    previous_collected = None
    if existing and existing["collected_at"]:
        try:
            previous_collected = _parse_datetime(existing["collected_at"], "collected_at")
        except FinanceValidationError:
            previous_collected = None
    metadata_from_report = bool(report and (previous_collected is None or report["collected_at"] >= previous_collected))

    if snapshot is not None:
        equity_close = snapshot["net_liquidating_value"]
        unrealized_pnl = snapshot["unrealized_pnl"]
        snapshot_realized = snapshot["realized_pnl"]
    else:
        equity_close = existing["equity_close"] if existing else None
        unrealized_pnl = existing["unrealized_pnl"] if existing else None
        snapshot_realized = None
    equity_open = _equity_open_from_snapshots(conn, account_key, trade_date)

    if metadata_from_report and report is not None:
        pnl = report["pnl"]
        if pnl["equity_open"] is not None:
            equity_open = pnl["equity_open"]
        if pnl["equity_close"] is not None:
            equity_close = pnl["equity_close"]
        if pnl["unrealized_pnl"] is not None:
            unrealized_pnl = pnl["unrealized_pnl"]
        report_realized = pnl["realized_pnl"]
    else:
        report_realized = existing["realized_pnl"] if existing else None
    realized_pnl = report_realized if report_realized is not None else snapshot_realized
    if realized_pnl is None:
        realized_pnl = event_realized
    equity_change = (
        float(equity_close) - float(equity_open)
        if equity_close is not None and equity_open is not None
        else None
    )

    coverage: dict[str, Any] = {}
    if existing:
        try:
            coverage.update(json.loads(existing["completeness_json"] or "{}"))
        except (TypeError, json.JSONDecodeError):
            pass
    if report is not None and metadata_from_report:
        if report["report_kind"] == "late_fee":
            # Passive late-fee updates carry only changed execution fees. They
            # must not make an already-complete finalization look incomplete.
            coverage.update({
                key: value for key, value in report["coverage"].items()
                if key not in {
                    "transactions_complete", "balances_available",
                    "fees_available", "pnl_available", "equity_open_scope",
                }
            })
        else:
            coverage.update(report["coverage"])
    if report is not None and report["report_kind"] == "finalization":
        coverage["finalization_attempted"] = True
        report_day = date.fromisoformat(report["trade_date"])
        collected_day = report["collected_at"].astimezone(NY_TZ).date()
        if report["broker_type"] == "tastytrade" and report_day < collected_day:
            # Cross-day verification is TT-only. Preserve a successful
            # verification when a later partial retry arrives.
            coverage["cross_day_reconciled"] = bool(
                coverage.get("cross_day_reconciled")
                or report["coverage"].get("transactions_complete", True)
            )
    coverage.update({
        "event_count": event_count,
        "transactions_complete": bool(coverage.get("transactions_complete", True)),
        "fees_available": fees is not None,
        "pnl_available": equity_open is not None and equity_close is not None,
    })
    prior_status = str(existing["data_status"] or "") if existing else ""
    final_now = bool(
        coverage.get("finalization_attempted")
        and coverage["transactions_complete"]
        and coverage["fees_available"]
    )
    data_status = "completed" if prior_status == "completed" or final_now else "in_progress"
    completed_at = (
        existing["completed_at"] if existing and existing["completed_at"] else now_iso
    ) if data_status == "completed" else ""
    collected_at = (
        _as_iso(report["collected_at"])
        if report and metadata_from_report
        else (existing["collected_at"] if existing else (snapshot["collected_at"] if snapshot else now_iso))
    )

    conn.execute(
        """
        INSERT INTO finance_daily_accounts (
            account_key, trade_date, buy_amount, sell_amount, fees, trade_net_flow,
            turnover, deposits, withdrawals, dividends, interest, other_cash_flow,
            realized_pnl, unrealized_pnl, equity_open, equity_close, equity_change,
            trade_count, source_server_id, data_status, completeness_json,
            collected_at, completed_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(account_key, trade_date) DO UPDATE SET
            buy_amount=excluded.buy_amount,
            sell_amount=excluded.sell_amount,
            fees=excluded.fees,
            trade_net_flow=excluded.trade_net_flow,
            turnover=excluded.turnover,
            deposits=NULL, withdrawals=NULL, dividends=NULL, interest=NULL, other_cash_flow=NULL,
            realized_pnl=excluded.realized_pnl,
            unrealized_pnl=excluded.unrealized_pnl,
            equity_open=excluded.equity_open,
            equity_close=excluded.equity_close,
            equity_change=excluded.equity_change,
            trade_count=excluded.trade_count,
            source_server_id=excluded.source_server_id,
            data_status=excluded.data_status,
            completeness_json=excluded.completeness_json,
            collected_at=excluded.collected_at,
            completed_at=excluded.completed_at,
            updated_at=excluded.updated_at
        """,
        (
            account_key, trade_date, buy_amount, sell_amount, fees, trade_net_flow,
            buy_amount + sell_amount, realized_pnl, unrealized_pnl, equity_open,
            equity_close, equity_change, event_count, server_id, data_status,
            json.dumps(coverage, ensure_ascii=False, separators=(",", ":")),
            collected_at, completed_at, now_iso,
        ),
    )
    _rebuild_symbols(conn, account_key, trade_date, now_iso)


def _apply_legacy_aggregate(conn, report: dict[str, Any], server_id: str, now_iso: str) -> bool:
    """Accept a pre-v2 TS during a rolling deployment without weakening v2 data."""
    existing_events = conn.execute(
        "SELECT 1 FROM finance_trade_events WHERE account_key=? AND trade_date=? LIMIT 1",
        (report["account_key"], report["trade_date"]),
    ).fetchone()
    if existing_events:
        return False
    existing = conn.execute(
        "SELECT collected_at, data_status, completed_at FROM finance_daily_accounts WHERE account_key=? AND trade_date=?",
        (report["account_key"], report["trade_date"]),
    ).fetchone()
    # An incomplete aggregate cannot prove that omitted executions are zero.
    # Keep the last observation until a complete aggregate arrives instead of
    # turning an already-collected day into a lower total.
    if existing and not bool(report["coverage"].get("transactions_complete", True)):
        return False
    if existing:
        try:
            if report["collected_at"] < _parse_datetime(existing["collected_at"], "collected_at"):
                return False
        except FinanceValidationError:
            pass
    trades = report["trades"]
    pnl = report["pnl"]
    final = report["report_kind"] == "finalization" and bool(report["coverage"].get("transactions_complete", True))
    status = "completed" if final or (existing and existing["data_status"] == "completed") else "in_progress"
    completed_at = (existing["completed_at"] if existing and existing["completed_at"] else now_iso) if status == "completed" else ""
    equity_change = (
        pnl["equity_close"] - pnl["equity_open"]
        if pnl["equity_close"] is not None and pnl["equity_open"] is not None
        else None
    )
    coverage = dict(report["coverage"])
    coverage["legacy_aggregate"] = True
    conn.execute(
        """
        INSERT INTO finance_daily_accounts (
            account_key, trade_date, buy_amount, sell_amount, fees, trade_net_flow,
            turnover, deposits, withdrawals, dividends, interest, other_cash_flow,
            realized_pnl, unrealized_pnl, equity_open, equity_close, equity_change,
            trade_count, source_server_id, data_status, completeness_json,
            collected_at, completed_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(account_key, trade_date) DO UPDATE SET
            buy_amount=excluded.buy_amount, sell_amount=excluded.sell_amount,
            fees=excluded.fees, trade_net_flow=excluded.trade_net_flow,
            turnover=excluded.turnover, realized_pnl=excluded.realized_pnl,
            unrealized_pnl=excluded.unrealized_pnl, equity_open=excluded.equity_open,
            equity_close=excluded.equity_close, equity_change=excluded.equity_change,
            trade_count=excluded.trade_count, source_server_id=excluded.source_server_id,
            data_status=excluded.data_status, completeness_json=excluded.completeness_json,
            collected_at=excluded.collected_at, completed_at=excluded.completed_at,
            updated_at=excluded.updated_at
        """,
        (
            report["account_key"], report["trade_date"], trades["buy_amount"],
            trades["sell_amount"], trades["fees"], trades["trade_net_flow"],
            trades["turnover"], pnl["realized_pnl"], pnl["unrealized_pnl"],
            pnl["equity_open"], pnl["equity_close"], equity_change,
            trades["trade_count"], server_id, status,
            json.dumps(coverage, ensure_ascii=False, separators=(",", ":")),
            _as_iso(report["collected_at"]), completed_at, now_iso,
        ),
    )
    conn.execute(
        "DELETE FROM finance_daily_symbols WHERE account_key=? AND trade_date=?",
        (report["account_key"], report["trade_date"]),
    )
    for item in report["symbols"]:
        conn.execute(
            """
            INSERT INTO finance_daily_symbols (
                account_key, trade_date, symbol, buy_quantity, sell_quantity,
                buy_amount, sell_amount, fees, trade_net_flow, trade_count, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                report["account_key"], report["trade_date"], item["symbol"],
                item["buy_quantity"], item["sell_quantity"], item["buy_amount"],
                item["sell_amount"], item["fees"], item["trade_net_flow"],
                item["trade_count"], now_iso,
            ),
        )
    return True


def _snapshot_observed_at(report: dict[str, Any]) -> datetime:
    """Use the ET day-end bucket for final reports, including TT rechecks."""
    if report["report_kind"] != "finalization":
        return report["collected_at"]
    report_day = date.fromisoformat(report["trade_date"])
    return datetime.combine(report_day, FINAL_COLLECTION_BUCKET, NY_TZ).astimezone(timezone.utc)


def _upsert_snapshot(conn, report: dict[str, Any], server_id: str, now_iso: str, windows: list[tuple[datetime, datetime]]) -> bool:
    balances = report["balances"]
    if report["report_kind"] == "late_fee" or not any(value is not None for value in balances.values()):
        return False
    collected_at = report["collected_at"]
    observed_at = _snapshot_observed_at(report)
    if _is_blocked(observed_at, windows):
        return False
    bucket_at = _bucket_at(observed_at)
    existing = conn.execute(
        "SELECT collected_at FROM finance_snapshots WHERE account_key=? AND trade_date=? AND bucket_at=?",
        (report["account_key"], report["trade_date"], bucket_at),
    ).fetchone()
    if existing:
        try:
            if collected_at < _parse_datetime(existing["collected_at"], "collected_at"):
                return False
        except FinanceValidationError:
            pass
    pnl = report["pnl"]
    conn.execute(
        """
        INSERT INTO finance_snapshots (
            account_key, trade_date, bucket_at, collected_at, source_server_id,
            net_liquidating_value, cash_balance, buying_power, realized_pnl,
            unrealized_pnl, equity_open, coverage_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(account_key, trade_date, bucket_at) DO UPDATE SET
            collected_at=excluded.collected_at,
            source_server_id=excluded.source_server_id,
            net_liquidating_value=excluded.net_liquidating_value,
            cash_balance=excluded.cash_balance,
            buying_power=excluded.buying_power,
            realized_pnl=excluded.realized_pnl,
            unrealized_pnl=excluded.unrealized_pnl,
            equity_open=excluded.equity_open,
            coverage_json=excluded.coverage_json
        """,
        (
            report["account_key"], report["trade_date"], bucket_at, _as_iso(collected_at),
            server_id, balances["net_liquidating_value"], balances["cash_balance"],
            balances["buying_power"], pnl["realized_pnl"], pnl["unrealized_pnl"],
            pnl["equity_open"], json.dumps(report["coverage"], ensure_ascii=False, separators=(",", ":")),
        ),
    )
    return True


def ingest_report(server_id: str, payload: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    server_id = _safe_text(server_id, max_length=120)
    if not server_id:
        raise FinanceValidationError("server_id is invalid")
    report = _normalize_report(payload)
    now_utc = (now or _utc_now()).astimezone(timezone.utc)
    now_iso = _as_iso(now_utc)
    now_ny = now_utc.astimezone(NY_TZ)
    report_day = date.fromisoformat(report["trade_date"])
    ny_today = now_ny.date()
    if report["report_kind"] == "current" and report_day != ny_today:
        raise FinanceValidationError("Current reports must use the current US Eastern day")
    if report["report_kind"] == "finalization":
        allowed_prior_day = (
            report["broker_type"] == "tastytrade"
            and report_day == _previous_us_equity_trading_day(ny_today)
        )
        if report_day != ny_today and not allowed_prior_day:
            raise FinanceValidationError(
                "Finalization reports must use the current US Eastern day, "
                "except for Tastytrade's prior trading-day reconciliation"
            )
    if report["report_kind"] == "late_fee":
        if report_day != ny_today - timedelta(days=1) or now_ny.time() > time(0, 30):
            raise FinanceValidationError("Late fee reports are only allowed until 00:30 ET for the prior day")

    conn = database._get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        created, activated_at = _upsert_account_and_source(conn, report, server_id, now_iso)
        report_start = datetime.combine(report_day, time.min, NY_TZ).astimezone(timezone.utc)
        report_end = datetime.combine(report_day, time.max, NY_TZ).astimezone(timezone.utc)
        windows = _deletion_windows_for_range(conn, report["account_key"], report_start, report_end)
        accepted_events = 0
        blocked_events = 0
        if report["trade_events"]:
            for event in report["trade_events"]:
                if event["executed_at"] < activated_at or _is_blocked(event["executed_at"], windows):
                    blocked_events += 1
                    continue
                if _upsert_trade_event(
                    conn,
                    event,
                    account_key=report["account_key"],
                    server_id=server_id,
                    collected_at=report["collected_at"],
                    now_iso=now_iso,
                ):
                    accepted_events += 1
        snapshot_written = _upsert_snapshot(conn, report, server_id, now_iso, windows)
        legacy_applied = False
        if report["legacy_aggregate"] and not report["trade_events"]:
            # A legacy aggregate has no event timestamp.  It is only accepted
            # when no part of its day was manually deleted. Otherwise it
            # could recreate totals that cannot be separated by timestamp.
            if not windows:
                legacy_applied = _apply_legacy_aggregate(conn, report, server_id, now_iso)
            else:
                blocked_events += 1
        else:
            _rebuild_daily_from_ledger(
                conn,
                report["account_key"],
                report["trade_date"],
                report=report,
                server_id=server_id,
                now_iso=now_iso,
            )

        warning_text = "; ".join(report["warnings"])
        source_status = "ok" if accepted_events or snapshot_written or not report["trade_events"] else "partial"
        conn.execute(
            """
            UPDATE finance_account_sources
            SET last_success_at=?, last_status=?, last_error=?
            WHERE account_key=? AND server_id=?
            """,
            (now_iso, source_status, warning_text[:500], report["account_key"], server_id),
        )
        conn.execute(
            """
            INSERT INTO finance_collection_status (
                account_key, last_attempt_at, last_success_at, last_status,
                last_error, last_source_server_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_key) DO UPDATE SET
                last_attempt_at=excluded.last_attempt_at,
                last_success_at=excluded.last_success_at,
                last_status=excluded.last_status,
                last_error=excluded.last_error,
                last_source_server_id=excluded.last_source_server_id,
                updated_at=excluded.updated_at
            """,
            (
                report["account_key"], now_iso, now_iso, source_status,
                warning_text[:500], server_id, now_iso,
            ),
        )
        reconcile_dates = _pending_cross_day_reconciliation(conn, report, ny_today)
        conn.commit()
        return {
            "accepted": bool(accepted_events or snapshot_written or legacy_applied),
            "blocked": bool(blocked_events and not accepted_events and not snapshot_written),
            "account_key": report["account_key"],
            "account_created": created,
            "accepted_events": accepted_events,
            "blocked_events": blocked_events,
            "reconcile_dates": reconcile_dates,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _normalize_account_keys(values: Iterable[Any] | None) -> list[str]:
    result: list[str] = []
    for value in values or []:
        key = _safe_text(value, max_length=80)
        if key and key not in result:
            if not key.startswith("fin_"):
                raise FinanceValidationError("Account filter is invalid")
            result.append(key)
    return result


def _validate_date_range(start_value: Any, end_value: Any) -> tuple[date, date]:
    start = _parse_date(start_value, "start_date")
    end = _parse_date(end_value, "end_date")
    if start > end:
        raise FinanceValidationError("Start date cannot be after end date")
    if (end - start).days + 1 > MAX_QUERY_DAYS:
        raise FinanceValidationError("Query range is limited to three calendar months")
    month_index = end.year * 12 + end.month - 3
    earliest = date(month_index // 12, month_index % 12 + 1, 1)
    if start < earliest:
        raise FinanceValidationError("Query range is limited to three calendar months")
    return start, end


def _validate_delete_range(start_value: Any, end_value: Any, *, now: datetime | None = None) -> tuple[datetime, datetime]:
    start = _parse_ny_datetime(start_value, "start_at", end_of_day=False)
    end = _parse_ny_datetime(end_value, "end_at", end_of_day=True)
    if start > end:
        raise FinanceValidationError("Start time cannot be after end time")
    current = (now or _utc_now()).astimezone(NY_TZ).replace(second=0, microsecond=0).astimezone(timezone.utc)
    if end > current:
        raise FinanceValidationError("Delete range must not include future US Eastern time")
    _validate_date_range(start.astimezone(NY_TZ).date().isoformat(), end.astimezone(NY_TZ).date().isoformat())
    return start, end


def _placeholders(values: list[Any]) -> str:
    return ",".join("?" for _ in values)


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


def _xnys_holidays(year: int) -> set[date]:
    result: set[date] = set()
    for fixed_year in (year - 1, year, year + 1):
        for month, day in ((1, 1), (6, 19), (7, 4), (12, 25)):
            observed = _observed_fixed_holiday(date(fixed_year, month, day))
            if observed.year == year:
                result.add(observed)
    result.update({
        _nth_weekday(year, 1, 0, 3),
        _nth_weekday(year, 2, 0, 3),
        _easter_sunday(year) - timedelta(days=2),
        _last_weekday(year, 5, 0),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 11, 3, 4),
    })
    return result


def _is_us_equity_trading_day(day: date) -> bool:
    return day.weekday() < 5 and day not in _xnys_holidays(day.year)


def _previous_us_equity_trading_day(day: date) -> date:
    cursor = day - timedelta(days=1)
    while not _is_us_equity_trading_day(cursor):
        cursor -= timedelta(days=1)
    return cursor


def _pending_cross_day_reconciliation(conn, report: dict[str, Any], today: date) -> list[str]:
    """Return at most one TT day that was observed but not rechecked yet."""
    if (
        report["schema_version"] < 2
        or report["broker_type"] != "tastytrade"
        or report["report_kind"] != "current"
    ):
        return []
    prior_day = _previous_us_equity_trading_day(today)
    row = conn.execute(
        """
        SELECT completeness_json
        FROM finance_daily_accounts
        WHERE account_key=? AND trade_date=?
        """,
        (report["account_key"], prior_day.isoformat()),
    ).fetchone()
    if not row:
        return []
    try:
        coverage = json.loads(row["completeness_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        coverage = {}
    return [] if coverage.get("cross_day_reconciled") else [prior_day.isoformat()]


def _account_activation(account: dict[str, Any]) -> datetime:
    raw = account.get("activated_at") or account.get("first_seen_at")
    try:
        return _parse_datetime(raw, "activated_at")
    except FinanceValidationError:
        return datetime.combine(date.fromisoformat(account["first_trade_date"]), time.min, NY_TZ).astimezone(timezone.utc)


def _expected_buckets(account: dict[str, Any], trade_day: date, *, now: datetime | None = None) -> list[str]:
    if not _is_us_equity_trading_day(trade_day):
        return []
    activation = _account_activation(account).astimezone(NY_TZ)
    start = datetime.combine(trade_day, EXTENDED_SESSION_START, NY_TZ)
    end = datetime.combine(trade_day, EXTENDED_SESSION_END, NY_TZ)
    if activation.date() == trade_day:
        start = max(start, activation)
    elif activation.date() > trade_day:
        return []
    current_ny = (now or _utc_now()).astimezone(NY_TZ)
    if trade_day == current_ny.date():
        end = min(end, current_ny)
    if end < start:
        return []
    minute = start.minute - (start.minute % COLLECTION_INTERVAL_MINUTES)
    cursor = start.replace(minute=minute, second=0, microsecond=0)
    if cursor < start:
        cursor += timedelta(minutes=COLLECTION_INTERVAL_MINUTES)
    result: list[str] = []
    while cursor <= end:
        result.append(_bucket_at(cursor.astimezone(timezone.utc)))
        cursor += timedelta(minutes=COLLECTION_INTERVAL_MINUTES)
    final_bucket = datetime.combine(trade_day, FINAL_COLLECTION_BUCKET, NY_TZ)
    if trade_day < current_ny.date() or current_ny >= final_bucket:
        result.append(_bucket_at(final_bucket.astimezone(timezone.utc)))
    return list(dict.fromkeys(result))


def _row_dicts(rows) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def list_accounts(start_date: Any, end_date: Any) -> list[dict[str, Any]]:
    start, end = _validate_date_range(start_date, end_date)
    conn = database._get_conn()
    try:
        rows = conn.execute(
            """
            SELECT a.*, c.last_success_at, c.last_status, c.last_error,
                   c.last_source_server_id, c.last_manual_requested_at
            FROM finance_accounts a
            LEFT JOIN finance_collection_status c ON c.account_key=a.account_key
            WHERE a.first_trade_date <= ?
              AND (
                  substr(a.last_seen_at, 1, 10) >= ?
                  OR EXISTS (SELECT 1 FROM finance_daily_accounts d WHERE d.account_key=a.account_key AND d.trade_date BETWEEN ? AND ?)
                  OR EXISTS (SELECT 1 FROM finance_deletion_windows w WHERE w.account_key=a.account_key AND w.start_trade_date<=? AND w.end_trade_date>=?)
              )
            ORDER BY a.broker_type, a.broker_account_id
            """,
            (end.isoformat(), start.isoformat(), start.isoformat(), end.isoformat(), end.isoformat(), start.isoformat()),
        ).fetchall()
        return _row_dicts(rows)
    finally:
        conn.close()


def _account_query(conn, start: date, end: date, selected: list[str]) -> list[dict[str, Any]]:
    params: list[Any] = [end.isoformat(), start.isoformat(), start.isoformat(), end.isoformat(), end.isoformat(), start.isoformat()]
    sql = """
        SELECT a.*, c.last_success_at, c.last_status, c.last_error,
               c.last_source_server_id, c.last_manual_requested_at
        FROM finance_accounts a
        LEFT JOIN finance_collection_status c ON c.account_key=a.account_key
        WHERE a.first_trade_date <= ?
          AND (
              substr(a.last_seen_at, 1, 10) >= ?
              OR EXISTS (SELECT 1 FROM finance_daily_accounts d WHERE d.account_key=a.account_key AND d.trade_date BETWEEN ? AND ?)
              OR EXISTS (SELECT 1 FROM finance_deletion_windows w WHERE w.account_key=a.account_key AND w.start_trade_date<=? AND w.end_trade_date>=?)
          )
    """
    if selected:
        sql += f" AND a.account_key IN ({_placeholders(selected)})"
        params.extend(selected)
    sql += " ORDER BY a.broker_type, a.broker_account_id"
    return _row_dicts(conn.execute(sql, params).fetchall())


def _coverage_for_daily(account: dict[str, Any], trade_day: date, snapshot_buckets: set[str], now: datetime) -> dict[str, Any]:
    expected = _expected_buckets(account, trade_day, now=now)
    actual = len(set(expected).intersection(snapshot_buckets))
    missing = [bucket for bucket in expected if bucket not in snapshot_buckets]
    percent = round(actual * 100.0 / len(expected), 1) if expected else 100.0
    return {
        "expected_points": len(expected),
        "captured_points": actual,
        "missing_points": len(missing),
        "missing_buckets": missing,
        "coverage_pct": percent,
    }


def _range_days(start: date, end: date) -> list[date]:
    result: list[date] = []
    cursor = start
    while cursor <= end:
        result.append(cursor)
        cursor += timedelta(days=1)
    return result


def get_overview(start_date: Any, end_date: Any, account_keys: Iterable[Any] | None = None) -> dict[str, Any]:
    start, end = _validate_date_range(start_date, end_date)
    selected = _normalize_account_keys(account_keys)
    now = _utc_now()
    conn = database._get_conn()
    try:
        accounts = _account_query(conn, start, end, selected)
        keys = [row["account_key"] for row in accounts]
        empty = {
            "accounts": [], "daily": [], "symbols": [], "trend": [], "gaps": [],
            "range": {"start_date": start.isoformat(), "end_date": end.isoformat(), "grain": "day"},
        }
        if not keys:
            return empty
        marks = _placeholders(keys)
        params: list[Any] = [start.isoformat(), end.isoformat(), *keys]
        daily_rows = _row_dicts(conn.execute(
            f"""
            SELECT d.*, a.broker_type, a.broker_account_id, a.currency
            FROM finance_daily_accounts d
            JOIN finance_accounts a ON a.account_key=d.account_key
            WHERE d.trade_date BETWEEN ? AND ? AND d.account_key IN ({marks})
            ORDER BY d.trade_date, a.broker_type, a.broker_account_id
            """,
            params,
        ).fetchall())
        snapshot_rows = _row_dicts(conn.execute(
            f"""
            SELECT account_key, trade_date, bucket_at, collected_at, net_liquidating_value,
                   cash_balance, buying_power, realized_pnl, unrealized_pnl, equity_open
            FROM finance_snapshots
            WHERE trade_date BETWEEN ? AND ? AND account_key IN ({marks})
            ORDER BY bucket_at, account_key
            """,
            params,
        ).fetchall())
        snapshot_buckets: dict[tuple[str, str], set[str]] = {}
        snapshot_lookup: dict[tuple[str, str], dict[str, Any]] = {}
        for row in snapshot_rows:
            identity = (row["account_key"], row["trade_date"])
            snapshot_buckets.setdefault(identity, set()).add(row["bucket_at"])
            snapshot_lookup[(row["account_key"], row["bucket_at"])] = row
        account_map = {account["account_key"]: account for account in accounts}
        for row in daily_rows:
            coverage = _coverage_for_daily(
                account_map[row["account_key"]],
                date.fromisoformat(row["trade_date"]),
                snapshot_buckets.get((row["account_key"], row["trade_date"]), set()),
                now,
            )
            row["coverage_pct"] = coverage["coverage_pct"]
            try:
                stored_coverage = json.loads(row.pop("completeness_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                stored_coverage = {}
            row["coverage"] = {**stored_coverage, **coverage}

        symbol_rows = _row_dicts(conn.execute(
            f"""
            SELECT symbol,
                   SUM(buy_quantity) AS buy_quantity,
                   SUM(sell_quantity) AS sell_quantity,
                   SUM(buy_amount) AS buy_amount,
                   SUM(sell_amount) AS sell_amount,
                   CASE WHEN COUNT(fees)=COUNT(*) THEN SUM(fees) ELSE NULL END AS fees,
                   CASE WHEN COUNT(trade_net_flow)=COUNT(*) THEN SUM(trade_net_flow) ELSE NULL END AS trade_net_flow,
                   SUM(trade_count) AS trade_count,
                   COUNT(DISTINCT account_key) AS account_count
            FROM finance_daily_symbols
            WHERE trade_date BETWEEN ? AND ? AND account_key IN ({marks})
            GROUP BY symbol
            ORDER BY (SUM(buy_amount)+SUM(sell_amount)) DESC, symbol
            LIMIT 1000
            """,
            params,
        ).fetchall())

        use_intraday = start == end
        trend: list[dict[str, Any]] = []
        if use_intraday:
            all_buckets: set[str] = set()
            expected_by_account: dict[str, set[str]] = {}
            for account in accounts:
                expected = set(_expected_buckets(account, start, now=now))
                expected_by_account[account["account_key"]] = expected
                all_buckets.update(expected)
            for bucket in sorted(all_buckets):
                expected_accounts = [key for key, expected in expected_by_account.items() if bucket in expected]
                rows = [snapshot_lookup.get((key, bucket)) for key in expected_accounts]
                equity_known = bool(rows) and all(row and row["net_liquidating_value"] is not None for row in rows)
                cash_known = bool(rows) and all(row and row["cash_balance"] is not None for row in rows)
                trend.append({
                    "time": bucket,
                    "equity": sum(float(row["net_liquidating_value"]) for row in rows if row) if equity_known else None,
                    "cash": sum(float(row["cash_balance"]) for row in rows if row) if cash_known else None,
                    "reported_accounts": sum(1 for row in rows if row and row["net_liquidating_value"] is not None),
                    "expected_accounts": len(expected_accounts),
                    "complete": equity_known,
                })
        else:
            latest_by_day_account: dict[tuple[str, str], dict[str, Any]] = {}
            for row in snapshot_rows:
                identity = (row["account_key"], row["trade_date"])
                current = latest_by_day_account.get(identity)
                if current is None or row["collected_at"] >= current["collected_at"]:
                    latest_by_day_account[identity] = row
            for day in _range_days(start, end):
                if not _is_us_equity_trading_day(day):
                    continue
                expected_accounts = [
                    account["account_key"] for account in accounts
                    if _expected_buckets(account, day, now=now)
                ]
                rows = [latest_by_day_account.get((key, day.isoformat())) for key in expected_accounts]
                equity_known = bool(rows) and all(row and row["net_liquidating_value"] is not None for row in rows)
                cash_known = bool(rows) and all(row and row["cash_balance"] is not None for row in rows)
                trend.append({
                    "time": day.isoformat(),
                    "equity": sum(float(row["net_liquidating_value"]) for row in rows if row) if equity_known else None,
                    "cash": sum(float(row["cash_balance"]) for row in rows if row) if cash_known else None,
                    "reported_accounts": sum(1 for row in rows if row and row["net_liquidating_value"] is not None),
                    "expected_accounts": len(expected_accounts),
                    "complete": equity_known,
                })

        windows = _row_dicts(conn.execute(
            f"""
            SELECT account_key, start_at, end_at, start_trade_date, end_trade_date, deleted_at, deleted_by
            FROM finance_deletion_windows
            WHERE start_trade_date<=? AND end_trade_date>=? AND account_key IN ({marks})
            ORDER BY start_at, account_key
            """,
            [end.isoformat(), start.isoformat(), *keys],
        ).fetchall())
        existing_daily = {(row["account_key"], row["trade_date"]) for row in daily_rows}
        gaps: list[dict[str, Any]] = []
        for window in windows:
            gaps.append({**window, "trade_date": window["start_trade_date"], "status": "deleted"})
        for account in accounts:
            activation_day = _account_activation(account).astimezone(NY_TZ).date()
            try:
                last_seen = _parse_datetime(account["last_seen_at"], "last_seen_at").astimezone(NY_TZ).date()
            except FinanceValidationError:
                last_seen = end
            for day in _range_days(max(start, activation_day), min(end, last_seen)):
                if not _is_us_equity_trading_day(day):
                    continue
                identity = (account["account_key"], day.isoformat())
                if identity not in existing_daily and not any(
                    window["account_key"] == account["account_key"]
                    and window["start_trade_date"] <= day.isoformat() <= window["end_trade_date"]
                    for window in windows
                ):
                    gaps.append({"account_key": account["account_key"], "trade_date": day.isoformat(), "status": "missing"})
        return {
            "accounts": accounts,
            "daily": daily_rows,
            "symbols": symbol_rows,
            "trend": trend,
            "gaps": gaps,
            "range": {"start_date": start.isoformat(), "end_date": end.isoformat(), "grain": "15m" if use_intraday else "day"},
        }
    finally:
        conn.close()


def preview_delete(start_at: Any, end_at: Any, account_keys: Iterable[Any], *, now: datetime | None = None) -> dict[str, Any]:
    start, end = _validate_delete_range(start_at, end_at, now=now)
    keys = _normalize_account_keys(account_keys)
    if not keys:
        raise FinanceValidationError("At least one broker account is required for deletion")
    start_day = start.astimezone(NY_TZ).date()
    end_day = end.astimezone(NY_TZ).date()
    conn = database._get_conn()
    try:
        marks = _placeholders(keys)
        exists = conn.execute(
            f"SELECT account_key FROM finance_accounts WHERE account_key IN ({marks})",
            keys,
        ).fetchall()
        if len(exists) != len(keys):
            raise FinanceValidationError("Deletion includes an unknown broker account")
        event_count = int(conn.execute(
            f"""
            SELECT COUNT(*) FROM finance_trade_events
            WHERE account_key IN ({marks}) AND executed_at BETWEEN ? AND ?
            """,
            [*keys, _as_iso(start), _as_iso(end)],
        ).fetchone()[0])
        snapshot_count = int(conn.execute(
            f"""
            SELECT COUNT(*) FROM finance_snapshots
            WHERE account_key IN ({marks}) AND bucket_at BETWEEN ? AND ?
            """,
            [*keys, _as_iso(start), _as_iso(end)],
        ).fetchone()[0])
        daily_count = int(conn.execute(
            f"""
            SELECT COUNT(*) FROM finance_daily_accounts
            WHERE account_key IN ({marks}) AND trade_date BETWEEN ? AND ?
            """,
            [*keys, start_day.isoformat(), end_day.isoformat()],
        ).fetchone()[0])
        symbol_count = int(conn.execute(
            f"""
            SELECT COUNT(*) FROM finance_daily_symbols
            WHERE account_key IN ({marks}) AND trade_date BETWEEN ? AND ?
            """,
            [*keys, start_day.isoformat(), end_day.isoformat()],
        ).fetchone()[0])
        return {
            "start_at": _as_iso(start),
            "end_at": _as_iso(end),
            "start_date": start_day.isoformat(),
            "end_date": end_day.isoformat(),
            "account_keys": keys,
            "account_count": len(keys),
            "calendar_days": (end_day - start_day).days + 1,
            "trade_events": event_count,
            "snapshots": snapshot_count,
            "daily_accounts": daily_count,
            "daily_symbols": symbol_count,
        }
    finally:
        conn.close()


def delete_data(start_at: Any, end_at: Any, account_keys: Iterable[Any], deleted_by: str, *, now: datetime | None = None) -> dict[str, Any]:
    preview = preview_delete(start_at, end_at, account_keys, now=now)
    start = _parse_datetime(preview["start_at"], "start_at")
    end = _parse_datetime(preview["end_at"], "end_at")
    keys = preview["account_keys"]
    now_iso = _as_iso((now or _utc_now()).astimezone(timezone.utc))
    conn = database._get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        marks = _placeholders(keys)
        affected = conn.execute(
            f"""
            SELECT DISTINCT account_key, trade_date FROM finance_trade_events
            WHERE account_key IN ({marks}) AND executed_at BETWEEN ? AND ?
            UNION
            SELECT DISTINCT account_key, trade_date FROM finance_snapshots
            WHERE account_key IN ({marks}) AND bucket_at BETWEEN ? AND ?
            """,
            [*keys, _as_iso(start), _as_iso(end), *keys, _as_iso(start), _as_iso(end)],
        ).fetchall()
        for account_key in keys:
            conn.execute(
                """
                INSERT INTO finance_deletion_windows (
                    account_key, start_at, end_at, start_trade_date, end_trade_date, deleted_by, deleted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_key, _as_iso(start), _as_iso(end),
                    preview["start_date"], preview["end_date"], _safe_text(deleted_by, max_length=120), now_iso,
                ),
            )
        event_result = conn.execute(
            f"DELETE FROM finance_trade_events WHERE account_key IN ({marks}) AND executed_at BETWEEN ? AND ?",
            [*keys, _as_iso(start), _as_iso(end)],
        )
        snapshot_result = conn.execute(
            f"DELETE FROM finance_snapshots WHERE account_key IN ({marks}) AND bucket_at BETWEEN ? AND ?",
            [*keys, _as_iso(start), _as_iso(end)],
        )
        affected_set = {(row["account_key"], row["trade_date"]) for row in affected}
        for account_key in keys:
            cursor = date.fromisoformat(preview["start_date"])
            while cursor <= date.fromisoformat(preview["end_date"]):
                affected_set.add((account_key, cursor.isoformat()))
                cursor += timedelta(days=1)
        for account_key, trade_day in affected_set:
            before = conn.execute(
                "SELECT 1 FROM finance_trade_events WHERE account_key=? AND trade_date=? LIMIT 1",
                (account_key, trade_day),
            ).fetchone()
            snapshot = _latest_snapshot(conn, account_key, trade_day)
            if not before and snapshot is None:
                conn.execute("DELETE FROM finance_daily_symbols WHERE account_key=? AND trade_date=?", (account_key, trade_day))
                conn.execute("DELETE FROM finance_daily_accounts WHERE account_key=? AND trade_date=?", (account_key, trade_day))
            else:
                _rebuild_daily_from_ledger(
                    conn, account_key, trade_day, report=None, server_id="", now_iso=now_iso,
                )
        conn.commit()
        return {
            **preview,
            "deleted": {
                "trade_events": max(0, int(event_result.rowcount or 0)),
                "snapshots": max(0, int(snapshot_result.rowcount or 0)),
                "daily_accounts": len(affected_set),
                "daily_symbols": len(affected_set),
            },
            "blocked_windows": len(keys),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def source_candidates(account_key: Any) -> list[str]:
    keys = _normalize_account_keys([account_key])
    if not keys:
        return []
    conn = database._get_conn()
    try:
        rows = conn.execute(
            """
            SELECT server_id FROM finance_account_sources
            WHERE account_key=?
            ORDER BY last_success_at DESC, last_seen_at DESC, server_id
            """,
            (keys[0],),
        ).fetchall()
        return [str(row[0]) for row in rows]
    finally:
        conn.close()


def reserve_manual_collection(account_key: Any, server_id: str, *, now: datetime | None = None) -> tuple[bool, int]:
    keys = _normalize_account_keys([account_key])
    if not keys:
        raise FinanceValidationError("Broker account is not available")
    now_utc = (now or _utc_now()).astimezone(timezone.utc)
    now_iso = _as_iso(now_utc)
    conn = database._get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT last_manual_requested_at FROM finance_collection_status WHERE account_key=?",
            (keys[0],),
        ).fetchone()
        if not row:
            raise FinanceValidationError("Broker account has not completed initial collection")
        raw = str(row[0] or "")
        if raw:
            try:
                elapsed = (now_utc - _parse_datetime(raw, "last_manual_requested_at")).total_seconds()
            except FinanceValidationError:
                elapsed = MANUAL_COLLECTION_COOLDOWN_SECONDS
            if elapsed < MANUAL_COLLECTION_COOLDOWN_SECONDS:
                conn.rollback()
                return False, max(1, int(math.ceil(MANUAL_COLLECTION_COOLDOWN_SECONDS - elapsed)))
        conn.execute(
            """
            UPDATE finance_collection_status
            SET last_manual_requested_at=?, last_status='requested',
                last_source_server_id=?, updated_at=?
            WHERE account_key=?
            """,
            (now_iso, _safe_text(server_id, max_length=120), now_iso, keys[0]),
        )
        conn.commit()
        return True, 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def cleanup_retention(*, now: datetime | None = None, retention_months: int = 3) -> dict[str, int]:
    now_date = (now or _utc_now()).astimezone(NY_TZ).date()
    keep_months = max(1, int(retention_months or 3))
    month_index = now_date.year * 12 + now_date.month - keep_months
    cutoff = date(month_index // 12, month_index % 12 + 1, 1).isoformat()
    conn = database._get_conn()
    deleted: dict[str, int] = {}
    try:
        conn.execute("BEGIN IMMEDIATE")
        for table in (
            "finance_trade_events",
            "finance_snapshots",
            "finance_daily_symbols",
            "finance_daily_accounts",
            "finance_deletion_blocks",
        ):
            result = conn.execute(f"DELETE FROM {table} WHERE trade_date < ?", (cutoff,))
            deleted[table] = max(0, int(result.rowcount or 0))
        result = conn.execute(
            "DELETE FROM finance_deletion_windows WHERE end_trade_date < ?",
            (cutoff,),
        )
        deleted["finance_deletion_windows"] = max(0, int(result.rowcount or 0))
        result = conn.execute(
            """
            DELETE FROM finance_accounts
            WHERE substr(last_seen_at, 1, 10) < ?
              AND NOT EXISTS(SELECT 1 FROM finance_snapshots s WHERE s.account_key=finance_accounts.account_key)
              AND NOT EXISTS(SELECT 1 FROM finance_daily_accounts d WHERE d.account_key=finance_accounts.account_key)
              AND NOT EXISTS(SELECT 1 FROM finance_trade_events e WHERE e.account_key=finance_accounts.account_key)
              AND NOT EXISTS(SELECT 1 FROM finance_deletion_windows w WHERE w.account_key=finance_accounts.account_key)
            """,
            (cutoff,),
        )
        deleted["finance_accounts"] = max(0, int(result.rowcount or 0))
        conn.commit()
        return deleted
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
