import asyncio
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Server_manager"))
sys.path.insert(0, str(ROOT))

import database
import main as sm_main
from services import finance_service
from Trader_Server.api.base import FinanceCollectionSkipped
from Trader_Server.api.interactive_brokers import IBBroker
from Trader_Server.api.tastytrade import TastytradeBroker, _build_tastytrade_finance_report
from Trader_Server.config import state as ts_state
from Trader_Server.services import config_sync, finance_reporter


NY = ZoneInfo("America/New_York")


def sample_report(
    now: datetime,
    *,
    account_id: str = "ACCT-001",
    broker_type: str = "tastytrade",
    buy_amount: float = 100.0,
    sell_amount: float = 140.0,
    fees: float | None = 2.0,
    report_kind: str = "current",
    trade_date: str | None = None,
    trade_events: list[dict] | None = None,
    schema_version: int = 2,
) -> dict:
    day = trade_date or now.astimezone(NY).date().isoformat()
    report_day = date.fromisoformat(day)
    event_at = now.astimezone(timezone.utc).replace(second=0, microsecond=0)
    if now.astimezone(NY).date() != report_day:
        event_at = datetime.combine(report_day, time(10, 0), NY).astimezone(timezone.utc)
    if trade_events is None:
        trade_events = [
            {
                "execution_key": f"{account_id}:{day}:buy",
                "executed_at": event_at.isoformat(),
                "symbol": "AAPL",
                "side": "buy",
                "quantity": 1,
                "gross_amount": buy_amount,
                "fee": 0.0 if fees is not None else None,
                "realized_pnl": None,
            },
            {
                "execution_key": f"{account_id}:{day}:sell",
                "executed_at": event_at.isoformat(),
                "symbol": "AAPL",
                "side": "sell",
                "quantity": 1,
                "gross_amount": sell_amount,
                "fee": fees,
                "realized_pnl": 38.0,
            },
        ]
    return {
        "schema_version": schema_version,
        "server_id": "ts-1",
        "broker_type": broker_type,
        "broker_account_id": account_id,
        "currency": "USD",
        "trade_date": day,
        "collected_at": now.isoformat(),
        "report_kind": report_kind,
        "data_status": "completed" if report_kind == "finalization" else "in_progress",
        "balances": {
            "net_liquidating_value": 10000.0,
            "cash_balance": 5000.0,
            "buying_power": 8000.0,
        },
        "trades": {
            "buy_amount": buy_amount,
            "sell_amount": sell_amount,
            "fees": fees,
            "trade_net_flow": None if fees is None else sell_amount - buy_amount - fees,
            "trade_count": 2,
        },
        "cash_flows": {
            "deposits": 0.0,
            "withdrawals": 0.0,
            "dividends": 1.5,
            "interest": 0.2,
            "other_cash_flow": 0.0,
        },
        "pnl": {
            "realized_pnl": 38.0,
            "unrealized_pnl": 12.0,
            "equity_open": 9900.0,
            "equity_close": 10000.0,
        },
        "symbols": [
            {
                "symbol": "AAPL",
                "buy_quantity": 1,
                "sell_quantity": 1,
                "buy_amount": buy_amount,
                "sell_amount": sell_amount,
                "fees": fees,
                "trade_net_flow": None if fees is None else sell_amount - buy_amount - fees,
                "trade_count": 2,
            }
        ],
        "trade_events": trade_events,
        "coverage": {
            "transactions_complete": True,
            "cash_flows_available": broker_type == "tastytrade",
            "fees_available": fees is not None,
            "pnl_available": True,
        },
    }


class FinanceDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database._DB_PATH
        database._DB_PATH = str(Path(self.temp_dir.name) / "finance.db")
        database.init_db()
        current = datetime.now(timezone.utc)
        stable_minute = (current.minute // 15) * 15 + 1
        self.now = current.replace(minute=stable_minute, second=0, microsecond=0)
        self.day = self.now.astimezone(NY).date().isoformat()

    def tearDown(self):
        database._DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_v7_database_migrates_to_v9(self):
        old_path = Path(self.temp_dir.name) / "v7.db"
        conn = sqlite3.connect(old_path)
        try:
            conn.execute("PRAGMA user_version=7")
            conn.commit()
        finally:
            conn.close()
        database._DB_PATH = str(old_path)
        reports = database.init_db()
        conn = sqlite3.connect(old_path)
        try:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 9)
            names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        self.assertIn("finance_daily_accounts", names)
        self.assertIn("finance_trade_events", names)
        self.assertIn("finance_deletion_windows", names)
        self.assertTrue(any(item.get("to_version") == 8 for item in reports))
        self.assertTrue(any(item.get("to_version") == 9 for item in reports))

    def test_same_account_from_two_ts_is_overwritten_not_summed(self):
        first = finance_service.ingest_report("ts-1", sample_report(self.now), now=self.now)
        second_time = self.now + timedelta(minutes=2)
        second_payload = sample_report(second_time, buy_amount=220, sell_amount=260)
        second = finance_service.ingest_report("ts-2", second_payload, now=second_time)
        self.assertEqual(first["account_key"], second["account_key"])
        conn = database._get_conn()
        try:
            daily = conn.execute("SELECT buy_amount, sell_amount FROM finance_daily_accounts").fetchall()
            snapshots = conn.execute("SELECT COUNT(*) FROM finance_snapshots").fetchone()[0]
            sources = conn.execute("SELECT COUNT(*) FROM finance_account_sources").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(len(daily), 1)
        self.assertEqual(daily[0]["buy_amount"], 220)
        self.assertEqual(daily[0]["sell_amount"], 260)
        self.assertEqual(snapshots, 1)
        self.assertEqual(sources, 2)

    def test_new_account_cannot_backfill_before_onboarding(self):
        yesterday = (self.now.astimezone(NY).date() - timedelta(days=1)).isoformat()
        with self.assertRaises(finance_service.FinanceValidationError):
            finance_service.ingest_report(
                "ts-1",
                sample_report(self.now, trade_date=yesterday),
                now=self.now,
            )

    def test_previous_day_reconciles_once_after_rollover(self):
        day_one_noon = datetime(2026, 8, 25, 16, 0, tzinfo=timezone.utc)
        day_two_noon = day_one_noon + timedelta(days=1)
        first = finance_service.ingest_report("ts-1", sample_report(day_one_noon), now=day_one_noon)
        second = finance_service.ingest_report("ts-1", sample_report(day_two_noon), now=day_two_noon)
        self.assertEqual(second["reconcile_dates"], [day_one_noon.astimezone(NY).date().isoformat()])
        finance_service.ingest_report(
            "ts-1",
            sample_report(
                day_two_noon,
                report_kind="finalization",
                trade_date=day_one_noon.astimezone(NY).date().isoformat(),
            ),
            now=day_two_noon,
        )
        third = finance_service.ingest_report("ts-1", sample_report(day_two_noon + timedelta(minutes=15)), now=day_two_noon + timedelta(minutes=15))
        self.assertEqual(third["reconcile_dates"], [])
        self.assertTrue(first["account_created"])

    def test_reconciliation_crosses_weekend_or_restart_gap(self):
        friday = datetime(2026, 8, 21, 16, 0, tzinfo=timezone.utc)
        monday = friday + timedelta(days=3)
        finance_service.ingest_report("ts-1", sample_report(friday), now=friday)
        result = finance_service.ingest_report("ts-1", sample_report(monday), now=monday)
        self.assertEqual(result["reconcile_dates"], [friday.astimezone(NY).date().isoformat()])

    def test_ib_does_not_request_cross_day_reconciliation(self):
        day_one = datetime(2026, 8, 25, 16, 0, tzinfo=timezone.utc)
        day_two = day_one + timedelta(days=1)
        finance_service.ingest_report(
            "ib-ts",
            sample_report(day_one, broker_type="interactive_brokers"),
            now=day_one,
        )
        result = finance_service.ingest_report(
            "ib-ts",
            sample_report(day_two, broker_type="interactive_brokers"),
            now=day_two,
        )
        self.assertEqual(result["reconcile_dates"], [])

    def test_multi_day_trend_uses_last_snapshot_per_account_day(self):
        day_one = datetime(2026, 8, 25, 14, 0, tzinfo=timezone.utc)
        day_two = day_one + timedelta(days=1)
        first = sample_report(day_one)
        first["balances"]["net_liquidating_value"] = 10000
        finance_service.ingest_report("ts-1", first, now=day_one)
        later = sample_report(day_one + timedelta(minutes=15))
        later["balances"]["net_liquidating_value"] = 10500
        finance_service.ingest_report("ts-1", later, now=day_one + timedelta(minutes=15))
        finance_service.ingest_report("ts-1", sample_report(day_two), now=day_two)
        overview = finance_service.get_overview(
            day_one.astimezone(NY).date().isoformat(),
            day_two.astimezone(NY).date().isoformat(),
        )
        self.assertEqual(overview["range"]["grain"], "day")
        self.assertEqual(len(overview["trend"]), 2)
        self.assertEqual(overview["trend"][0]["equity"], 10500)
        self.assertEqual(len(overview["activity_trend"]), 2)
        self.assertEqual(overview["activity_trend"][0]["trade_net_flow"], 38.0)
        self.assertEqual(overview["activity_trend"][0]["realized_pnl"], 38.0)

    def test_whole_day_delete_blocks_event_replay_and_legacy_aggregate(self):
        report_time = datetime(2026, 8, 25, 16, 0, tzinfo=timezone.utc)
        day = report_time.astimezone(NY).date().isoformat()
        ingested = finance_service.ingest_report("ts-1", sample_report(report_time), now=report_time)
        deletion_now = datetime(2026, 8, 27, 16, 0, tzinfo=timezone.utc)
        preview = finance_service.preview_delete(day, day, [ingested["account_key"]], now=deletion_now)
        self.assertEqual(preview["daily_accounts"], 1)
        deleted = finance_service.delete_data(day, day, [ingested["account_key"]], "admin", now=deletion_now)
        self.assertEqual(deleted["deleted"]["daily_accounts"], 1)
        replay_time = report_time + timedelta(minutes=15)
        replay = finance_service.ingest_report("ts-2", sample_report(replay_time), now=replay_time)
        self.assertFalse(replay["accepted"])
        self.assertTrue(replay["blocked"])
        legacy = sample_report(replay_time + timedelta(minutes=15), schema_version=1)
        legacy.pop("trade_events")
        legacy_result = finance_service.ingest_report(
            "ts-2", legacy, now=replay_time + timedelta(minutes=15),
        )
        self.assertFalse(legacy_result["accepted"])
        self.assertTrue(legacy_result["blocked"])
        overview = finance_service.get_overview(day, day, [ingested["account_key"]])
        self.assertEqual(overview["daily"], [])
        self.assertEqual(overview["gaps"][0]["status"], "deleted")
        self.assertEqual(overview["activity_trend"][0]["gap_reason"], "deleted")
        self.assertIsNone(overview["activity_trend"][0]["trade_net_flow"])
        self.assertIsNone(overview["activity_trend"][0]["realized_pnl"])

    def test_minute_delete_uses_event_and_snapshot_observation_time(self):
        report_time = datetime(2026, 8, 25, 16, 0, tzinfo=timezone.utc)
        day = report_time.astimezone(NY).date().isoformat()
        result = finance_service.ingest_report("ts-1", sample_report(report_time), now=report_time)
        finalization_time = report_time + timedelta(days=1)
        finance_service.ingest_report(
            "ts-1",
            sample_report(
                finalization_time,
                report_kind="finalization",
                trade_date=day,
            ),
            now=finalization_time,
        )
        start = datetime(2026, 8, 25, 23, 44, tzinfo=NY)
        end = datetime(2026, 8, 25, 23, 46, tzinfo=NY)
        deletion_now = datetime(2026, 8, 27, 16, 0, tzinfo=timezone.utc)
        preview = finance_service.preview_delete(start, end, [result["account_key"]], now=deletion_now)
        self.assertEqual(preview["trade_events"], 0)
        self.assertEqual(preview["snapshots"], 1)
        finance_service.delete_data(start, end, [result["account_key"]], "admin", now=deletion_now)
        overview = finance_service.get_overview(day, day, [result["account_key"]])
        deleted = [gap for gap in overview["gaps"] if gap["status"] == "deleted"]
        self.assertEqual(len(deleted), 1)
        self.assertEqual(deleted[0]["start_at"], start.astimezone(timezone.utc).isoformat())
        self.assertEqual(deleted[0]["end_at"], end.astimezone(timezone.utc).isoformat())

    def test_future_minute_delete_is_rejected(self):
        result = finance_service.ingest_report("ts-1", sample_report(self.now), now=self.now)
        with self.assertRaises(finance_service.FinanceValidationError):
            finance_service.preview_delete(
                self.now,
                self.now + timedelta(minutes=1),
                [result["account_key"]],
                now=self.now,
            )

    def test_null_capabilities_remain_null_and_gap_is_explicit(self):
        report = sample_report(self.now, broker_type="interactive_brokers", fees=None)
        report["cash_flows"] = {key: None for key in report["cash_flows"]}
        result = finance_service.ingest_report("ib-ts", report, now=self.now)
        overview = finance_service.get_overview(self.day, self.day, [result["account_key"]])
        row = overview["daily"][0]
        self.assertIsNone(row["fees"])
        self.assertIsNone(row["trade_net_flow"])
        self.assertIsNone(row["deposits"])
        activity = overview["activity_trend"][0]
        self.assertFalse(activity["flow_complete"])
        self.assertIsNone(activity["trade_net_flow"])
        self.assertEqual(activity["flow_gap_reason"], "unavailable")

    def test_activity_trend_does_not_sum_partial_multi_account_data(self):
        first = finance_service.ingest_report(
            "ts-1",
            sample_report(self.now, account_id="ACCT-A"),
            now=self.now,
        )
        second = finance_service.ingest_report(
            "ts-2",
            sample_report(self.now, account_id="ACCT-B"),
            now=self.now,
        )
        finance_service.delete_data(
            self.day,
            self.day,
            [second["account_key"]],
            "admin",
            now=self.now + timedelta(days=2),
        )

        combined = finance_service.get_overview(self.day, self.day)
        combined_point = combined["activity_trend"][0]
        self.assertEqual(combined_point["expected_accounts"], 2)
        self.assertEqual(combined_point["reported_accounts"], 1)
        self.assertIsNone(combined_point["trade_net_flow"])
        self.assertIsNone(combined_point["realized_pnl"])
        self.assertEqual(combined_point["gap_reason"], "deleted")

        selected = finance_service.get_overview(self.day, self.day, [first["account_key"]])
        selected_point = selected["activity_trend"][0]
        self.assertTrue(selected_point["flow_complete"])
        self.assertEqual(selected_point["trade_net_flow"], 38.0)

    def test_activity_trend_stops_after_account_last_seen_date(self):
        day_one = datetime(2026, 8, 25, 14, 0, tzinfo=timezone.utc)
        day_two = day_one + timedelta(days=1)
        finance_service.ingest_report("ts-1", sample_report(day_one), now=day_one)

        overview = finance_service.get_overview(
            day_one.astimezone(NY).date().isoformat(),
            day_two.astimezone(NY).date().isoformat(),
        )
        self.assertEqual(
            [point["date"] for point in overview["activity_trend"]],
            [day_one.astimezone(NY).date().isoformat()],
        )

    def test_incomplete_current_report_preserves_completed_ledger_totals(self):
        initial = sample_report(self.now, buy_amount=500, sell_amount=650)
        result = finance_service.ingest_report("ib-ts", initial, now=self.now)
        reconciliation_time = self.now + timedelta(minutes=5)
        finance_service.ingest_report(
            "ib-ts",
            sample_report(
                reconciliation_time,
                buy_amount=500,
                sell_amount=650,
                report_kind="finalization",
                trade_date=self.day,
            ),
            now=reconciliation_time,
        )
        incomplete = sample_report(
            reconciliation_time + timedelta(minutes=5),
            buy_amount=0,
            sell_amount=0,
            trade_events=[],
            trade_date=self.day,
        )
        incomplete["coverage"]["transactions_complete"] = False
        finance_service.ingest_report("ib-ts", incomplete, now=reconciliation_time + timedelta(minutes=5))
        overview = finance_service.get_overview(self.day, self.day, [result["account_key"]])
        row = overview["daily"][0]
        self.assertEqual(row["buy_amount"], 500)
        self.assertEqual(row["sell_amount"], 650)
        self.assertEqual(row["data_status"], "completed")
        self.assertEqual(overview["symbols"][0]["buy_amount"], 500)

    def test_node_deletion_does_not_delete_finance_history(self):
        conn = database._get_conn()
        try:
            now_iso = self.now.isoformat()
            conn.execute(
                """
                INSERT INTO nodes (server_id, node_name, broker_type, token, created_at, updated_at)
                VALUES ('ts-history', 'History TS', 'TT', 'history-token', ?, ?)
                """,
                (now_iso, now_iso),
            )
            conn.commit()
        finally:
            conn.close()
        result = finance_service.ingest_report("ts-history", sample_report(self.now), now=self.now)
        self.assertTrue(database.delete_node("ts-history"))
        overview = finance_service.get_overview(self.day, self.day, [result["account_key"]])
        self.assertEqual(len(overview["daily"]), 1)

    def test_manual_collection_has_account_level_cooldown(self):
        result = finance_service.ingest_report("ts-1", sample_report(self.now), now=self.now)
        reserved, retry = finance_service.reserve_manual_collection(
            result["account_key"], "ts-1", now=self.now,
        )
        self.assertTrue(reserved)
        self.assertEqual(retry, 0)
        reserved, retry = finance_service.reserve_manual_collection(
            result["account_key"], "ts-1", now=self.now + timedelta(seconds=1),
        )
        self.assertFalse(reserved)
        self.assertGreaterEqual(retry, 59)

    def test_three_natural_month_retention(self):
        result = finance_service.ingest_report("ts-1", sample_report(self.now), now=self.now)
        conn = database._get_conn()
        try:
            conn.execute(
                "UPDATE finance_daily_accounts SET trade_date='2026-05-31' WHERE account_key=?",
                (result["account_key"],),
            )
            conn.commit()
        finally:
            conn.close()
        deleted = finance_service.cleanup_retention(
            now=datetime(2026, 8, 26, tzinfo=timezone.utc),
            retention_months=3,
        )
        self.assertEqual(deleted["finance_daily_accounts"], 1)


class FinanceBrokerSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_tastytrade_reversal_does_not_double_count_trade(self):
        day = datetime.now(NY).date()

        def transaction(item_id, value, *, reverses_id=None):
            return SimpleNamespace(
                id=item_id,
                reverses_id=reverses_id,
                transaction_date=day,
                executed_at=None,
                action="Buy to Open",
                transaction_type="Trade",
                transaction_sub_type="",
                description="",
                quantity=1,
                price=abs(value),
                value=-abs(value),
                net_value=-abs(value),
                regulatory_fees=0,
                clearing_fees=0,
                commission=0,
                proprietary_index_option_fees=0,
                other_charge=0,
                symbol="AAPL",
                underlying_symbol="AAPL",
            )

        report = _build_tastytrade_finance_report(
            account=SimpleNamespace(account_number="TT-1"),
            trade_date=day,
            transactions=[
                transaction(1, 100),
                transaction(2, 100, reverses_id=1),
                transaction(3, 110),
            ],
            transactions_complete=True,
            balance=None,
            positions=[],
            equity_open=None,
            equity_close=None,
        )
        self.assertEqual(report["trades"]["buy_amount"], 110)
        self.assertEqual(report["trades"]["trade_count"], 1)

    async def test_tastytrade_excludes_non_equity_trades_from_phase_one(self):
        day = datetime.now(NY).date()

        def transaction(item_id, symbol, instrument_type):
            return SimpleNamespace(
                id=item_id,
                reverses_id=None,
                transaction_date=day,
                executed_at=None,
                action="Buy to Open",
                transaction_type="Trade",
                transaction_sub_type="",
                description="",
                instrument_type=instrument_type,
                quantity=1,
                price=100,
                value=-100,
                net_value=-100,
                regulatory_fees=0,
                clearing_fees=0,
                commission=0,
                proprietary_index_option_fees=0,
                other_charge=0,
                symbol=symbol,
                underlying_symbol=symbol,
            )

        report = _build_tastytrade_finance_report(
            account=SimpleNamespace(account_number="TT-1"),
            trade_date=day,
            transactions=[
                transaction(1, "AAPL", "Equity"),
                transaction(2, "AAPL  260918C00200000", "Equity Option"),
            ],
            transactions_complete=True,
            balance=None,
            positions=[],
            equity_open=None,
            equity_close=None,
        )
        self.assertEqual(report["trades"]["buy_amount"], 100)
        self.assertEqual(len(report["trade_events"]), 1)
        self.assertIn("Skipped 1 non-equity", report["warnings"][0])

    async def test_tastytrade_fallback_event_timestamp_uses_new_york_timezone(self):
        for day, expected_utc_hour in ((date(2026, 1, 15), 5), (date(2026, 7, 15), 4)):
            transaction = SimpleNamespace(
                id=f"fallback-{day.isoformat()}",
                reverses_id=None,
                transaction_date=day,
                executed_at=None,
                action="Buy to Open",
                transaction_type="Trade",
                transaction_sub_type="",
                description="",
                instrument_type="Equity",
                quantity=1,
                price=100,
                value=-100,
                net_value=-100,
                regulatory_fees=0,
                clearing_fees=0,
                commission=0,
                proprietary_index_option_fees=0,
                other_charge=0,
                symbol="AAPL",
                underlying_symbol="AAPL",
            )
            report = _build_tastytrade_finance_report(
                account=SimpleNamespace(account_number="TT-1"),
                trade_date=day,
                transactions=[transaction],
                transactions_complete=True,
                balance=None,
                positions=[],
                equity_open=None,
                equity_close=None,
            )
            observed = datetime.fromisoformat(report["trade_events"][0]["executed_at"])
            self.assertEqual(observed.hour, expected_utc_hour)
            self.assertEqual(observed.astimezone(NY).date(), day)

    async def test_tastytrade_uses_existing_session_and_never_reconnects(self):
        current_day = datetime.now(NY).date()

        class Account:
            account_number = "TT-1"

            async def get_history(self, session, **kwargs):
                return [SimpleNamespace(
                    id=1,
                    transaction_date=current_day,
                    executed_at=None,
                    action="Buy to Open",
                    transaction_type="Trade",
                    transaction_sub_type="",
                    description="",
                    quantity=1,
                    price=100,
                    value=-100,
                    net_value=-101,
                    regulatory_fees=0,
                    clearing_fees=0,
                    commission=1,
                    proprietary_index_option_fees=0,
                    other_charge=0,
                    symbol="AAPL",
                    underlying_symbol="AAPL",
                )]

            async def get_balances(self, session, **kwargs):
                return SimpleNamespace(net_liquidating_value=10000, cash_balance=5000, equity_buying_power=8000)

            async def get_positions(self, session, **kwargs):
                return []

            async def get_balance_snapshots(self, session, **kwargs):
                return [SimpleNamespace(net_liquidating_value=9900)]

        broker = TastytradeBroker()
        broker._session = object()
        broker._account = Account()
        broker._connected = True
        broker.reconnect = AsyncMock(side_effect=AssertionError("finance must not reconnect"))
        report = await broker.collect_finance_report(current_day.isoformat())
        self.assertEqual(report["trades"]["buy_amount"], 100)
        broker.reconnect.assert_not_awaited()

    async def test_ib_skips_when_normal_account_query_is_busy(self):
        broker = IBBroker()
        await broker._orders_lock.acquire()
        try:
            with self.assertRaises(FinanceCollectionSkipped) as ctx:
                await broker.collect_finance_report(datetime.now(NY).date().isoformat())
            self.assertEqual(ctx.exception.code, "FINANCE_BUSY")
        finally:
            broker._orders_lock.release()

    async def test_ib_finance_collection_yields_before_a_followup_read_for_a_trade(self):
        current = datetime.now(NY)
        executions_started = asyncio.Event()
        release_executions = asyncio.Event()

        class App:
            def __init__(self):
                self.closed_event = asyncio.Event()
                self.summary_requested = False

            def isConnected(self):
                return True

            async def request_finance_executions(self, account, start_time, timeout=8):
                executions_started.set()
                await release_executions.wait()
                return []

            async def request_account_summary(self, timeout=8):
                self.summary_requested = True
                return {}

        broker = IBBroker()
        app = App()
        broker._ib_app = app
        broker._runtime_state = "ready"
        broker._connected = True
        broker._account_verified = True
        broker._account_id = "DU123"
        collection = asyncio.create_task(broker.collect_finance_report(current.date().isoformat()))
        await asyncio.wait_for(executions_started.wait(), timeout=1)
        broker._trade_activity += 1
        release_executions.set()
        try:
            with self.assertRaises(FinanceCollectionSkipped) as ctx:
                await collection
            self.assertEqual(ctx.exception.code, "FINANCE_PREEMPTED")
            self.assertFalse(app.summary_requested)
        finally:
            broker._trade_activity = max(0, broker._trade_activity - 1)

    async def test_ib_finance_read_uses_request_scoped_queries(self):
        current = datetime.now(NY)
        commission = SimpleNamespace(execId="A.B.01", commissionAndFees=1.25, realizedPNL=20)
        execution = SimpleNamespace(
            acctNumber="DU123",
            execId="A.B.01",
            time=current.strftime("%Y%m%d %H:%M:%S America/New_York"),
            shares=2,
            price=100,
            side="BOT",
        )
        corrected_execution = SimpleNamespace(
            acctNumber="DU123",
            execId="A.B.02",
            time=current.strftime("%Y%m%d %H:%M:%S America/New_York"),
            shares=2,
            price=110,
            side="BOT",
        )
        corrected_commission = SimpleNamespace(execId="A.B.02", commissionAndFees=1.5, realizedPNL=22)
        contract = SimpleNamespace(symbol="AAPL", currency="USD", multiplier="1")

        class App:
            closed_event = asyncio.Event()

            def isConnected(self):
                return True

            async def request_finance_executions(self, account, start_time, timeout=8):
                return [
                    {"execution": execution, "contract": contract, "commission_report": commission},
                    {"execution": corrected_execution, "contract": contract, "commission_report": corrected_commission},
                ]

            async def request_account_summary(self, timeout=8):
                return {"DU123": {
                    "NetLiquidation": {"value": "10000", "currency": "USD"},
                    "TotalCashValue": {"value": "5000", "currency": "USD"},
                    "BuyingPower": {"value": "8000", "currency": "USD"},
                    "RealizedPnL": {"value": "20", "currency": "USD"},
                    "UnrealizedPnL": {"value": "5", "currency": "USD"},
                }}

        broker = IBBroker()
        broker._ib_app = App()
        broker._runtime_state = "ready"
        broker._connected = True
        broker._account_verified = True
        broker._account_id = "DU123"
        report = await broker.collect_finance_report(current.date().isoformat())
        self.assertEqual(report["trades"]["buy_amount"], 220)
        self.assertEqual(report["trades"]["fees"], 1.5)
        self.assertEqual(report["balances"]["net_liquidating_value"], 10000)
        self.assertIsNone(report["cash_flows"]["deposits"])

    async def test_reporter_discards_broker_generation_change_before_upload(self):
        original = (config_sync._current_broker, config_sync._local_config_version)
        original_state = (ts_state.server_id, ts_state.token, ts_state.manager_url)

        class Broker:
            broker_type = "tastytrade"

            async def is_connected(self):
                return True

            async def collect_finance_report(self, trade_date, timeout=12):
                config_sync._current_broker = object()
                return {
                    "broker_account_id": "TT-1",
                    "currency": "USD",
                    "balances": {}, "trades": {}, "cash_flows": {}, "pnl": {}, "symbols": [], "coverage": {},
                }

        try:
            config_sync._current_broker = Broker()
            config_sync._local_config_version = 3
            ts_state.server_id = "ts-1"
            ts_state.token = "token"
            ts_state.manager_url = "https://example.invalid"
            with patch.object(finance_reporter, "_post_report", side_effect=AssertionError("stale report uploaded")):
                with self.assertRaises(FinanceCollectionSkipped) as ctx:
                    await finance_reporter._collect_one(datetime.now(NY).date(), "current")
            self.assertEqual(ctx.exception.code, "BROKER_CHANGED")
        finally:
            config_sync._current_broker, config_sync._local_config_version = original
            ts_state.server_id, ts_state.token, ts_state.manager_url = original_state

    async def test_manual_collection_account_mismatch_is_rejected_before_upload(self):
        original = (config_sync._current_broker, config_sync._local_config_version)
        original_state = (ts_state.server_id, ts_state.token, ts_state.manager_url)

        class Broker:
            broker_type = "tastytrade"

            async def is_connected(self):
                return True

            async def collect_finance_report(self, trade_date, timeout=12):
                return {
                    "broker_account_id": "TT-ACTUAL",
                    "currency": "USD",
                    "balances": {}, "trades": {}, "cash_flows": {}, "pnl": {}, "symbols": [], "coverage": {},
                }

        try:
            config_sync._current_broker = Broker()
            config_sync._local_config_version = 4
            ts_state.server_id = "ts-1"
            ts_state.token = "token"
            ts_state.manager_url = "https://example.invalid"
            with patch.object(finance_reporter, "_post_report", side_effect=AssertionError("wrong account uploaded")):
                with self.assertRaises(FinanceCollectionSkipped) as ctx:
                    await finance_reporter._collect_one(
                        datetime.now(NY).date(),
                        "current",
                        "fin_00000000000000000000000000000000",
                    )
            self.assertEqual(ctx.exception.code, "ACCOUNT_CHANGED")
        finally:
            config_sync._current_broker, config_sync._local_config_version = original
            ts_state.server_id, ts_state.token, ts_state.manager_url = original_state


class FinanceAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database._DB_PATH
        database._DB_PATH = str(Path(self.temp_dir.name) / "access.db")
        database.init_db()
        database.create_account("normal-admin", "password", role="admin")
        sm_main._admin_sessions.clear()
        self.client = TestClient(sm_main.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        sm_main._admin_sessions.clear()
        database._DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _session(self, role: str) -> str:
        sid = f"sid-{role}"
        sm_main._admin_sessions[sid] = {
            "id": 1,
            "username": role,
            "role": role,
            "created_at": datetime.now().timestamp(),
            "csrf_token": "csrf-token",
        }
        self.client.cookies.set("admin_sid", sid)
        return "csrf-token"

    def test_only_super_admin_sees_and_loads_finance_page(self):
        self._session("admin")
        dashboard = self.client.get("/admin/dashboard")
        self.assertNotIn('<button data-module="funds"', dashboard.text)
        self.assertEqual(self.client.get("/admin/funds/content").status_code, 403)

        self._session("super_admin")
        dashboard = self.client.get("/admin/dashboard")
        self.assertIn('<button data-module="funds"', dashboard.text)
        content = self.client.get("/admin/funds/content")
        self.assertEqual(content.status_code, 200)
        self.assertIn("数据管理", content.text)
        self.assertIn("funds-flow-line-chart", content.text)
        self.assertIn("funds-pnl-line-chart", content.text)

    def test_chart_axis_labels_are_not_scaled_with_svg(self):
        self._session("super_admin")
        dashboard = self.client.get("/admin/dashboard")
        content = self.client.get("/admin/funds/content")

        self.assertIn("function _fundsChartCanvas", dashboard.text)
        self.assertIn("function _fundsAxisMoney", dashboard.text)
        self.assertIn("function _fundsAxisTime", dashboard.text)
        self.assertIn("function _fundsBuildAxis", dashboard.text)
        self.assertIn("function _fundsAttachChartHover", dashboard.text)
        self.assertIn("function _fundsFocusBars", dashboard.text)
        self.assertIn("funds-chart-canvas", content.text)
        self.assertIn("funds-axis-label", content.text)
        self.assertIn("funds-chart-hover-line", content.text)
        self.assertIn("funds-chart-tooltip", content.text)

    def test_dashboard_overview_keeps_status_tables_and_full_height_audit_panel(self):
        self._session("super_admin")
        dashboard = self.client.get("/admin/dashboard")

        self.assertIn('class="overview-primary-stack"', dashboard.text)
        self.assertIn('class="panel ov-audit-panel"', dashboard.text)
        self.assertIn('id="ov-acct-list"', dashboard.text)
        self.assertIn('id="ov-node-list"', dashboard.text)
        self.assertIn('id="ov-audit-list"', dashboard.text)
        self.assertIn('class="ov-audit-foot"', dashboard.text)

    def test_account_management_uses_tabbed_views_without_removing_controls(self):
        self._session("super_admin")
        dashboard = self.client.get("/admin/dashboard")

        self.assertIn('id="accounts-tabs"', dashboard.text)
        self.assertIn('data-account-tab="status"', dashboard.text)
        self.assertIn('data-account-tab="register"', dashboard.text)
        self.assertIn('data-account-view="status"', dashboard.text)
        self.assertIn('data-account-view="register"', dashboard.text)
        self.assertIn('id="inp-username"', dashboard.text)
        self.assertIn('id="inp-se-addr"', dashboard.text)
        self.assertIn('id="accounts-trader-body"', dashboard.text)
        self.assertIn("function switchAccountTab", dashboard.text)
        self.assertIn("overflow-wrap:anywhere", dashboard.text)

    def test_node_and_domain_management_use_tabbed_views_without_removing_controls(self):
        self._session("super_admin")
        dashboard = self.client.get("/admin/dashboard")

        self.assertIn('id="nodes-tabs"', dashboard.text)
        self.assertIn('data-node-tab="status"', dashboard.text)
        self.assertIn('data-node-tab="review"', dashboard.text)
        self.assertIn('id="nodes-body"', dashboard.text)
        self.assertIn('id="pending-body"', dashboard.text)
        self.assertIn('id="btn-refresh-nodes"', dashboard.text)
        self.assertIn("function switchNodeTab", dashboard.text)
        self.assertIn("function renderNodes", dashboard.text)
        self.assertIn("function renderPending", dashboard.text)

        self.assertIn('id="domain-tabs"', dashboard.text)
        self.assertIn('data-domain-tab="status"', dashboard.text)
        self.assertIn('data-domain-tab="import"', dashboard.text)
        self.assertIn('data-domain-tab="dns"', dashboard.text)
        self.assertIn('id="domain-pool-body"', dashboard.text)
        self.assertIn('id="domain-import-input"', dashboard.text)
        self.assertIn('id="dns-config-panel"', dashboard.text)
        self.assertIn("function switchDomainTab", dashboard.text)
        self.assertIn("function importDomainPool", dashboard.text)

        self._session("admin")
        admin_dashboard = self.client.get("/admin/dashboard")
        self.assertNotIn('data-domain-tab="dns"', admin_dashboard.text)
        self.assertNotIn('id="dns-config-panel"', admin_dashboard.text)

    def test_mutations_require_csrf_and_delete_requires_confirmation(self):
        self._session("super_admin")
        response = self.client.post("/api/admin/finance/delete-preview", json={})
        self.assertEqual(response.status_code, 403)
        response = self.client.post(
            "/api/admin/finance/delete",
            headers={"X-SM-CSRF": "csrf-token"},
            json={"confirm": False},
        )
        self.assertEqual(response.status_code, 400)

    def test_node_report_requires_valid_node_token(self):
        response = self.client.post("/nodes/finance/report", json=sample_report(datetime.now(timezone.utc)))
        self.assertEqual(response.status_code, 401)

    def test_node_report_and_super_admin_query_success(self):
        now = datetime.now(timezone.utc)
        conn = database._get_conn()
        try:
            conn.execute(
                """
                INSERT INTO nodes (server_id, node_name, broker_type, token, created_at, updated_at)
                VALUES ('ts-1', 'Finance TS', 'TT', 'node-token', ?, ?)
                """,
                (now.isoformat(), now.isoformat()),
            )
            conn.execute(
                """
                INSERT INTO node_broker_config (
                    server_id, broker_type, credentials_json, enabled,
                    config_version, updated_at
                ) VALUES ('ts-1', 'TT', '{}', 1, 1, ?)
                """,
                (now.isoformat(),),
            )
            conn.commit()
        finally:
            conn.close()
        report = self.client.post(
            "/nodes/finance/report",
            headers={"Authorization": "Bearer node-token"},
            json=sample_report(now),
        )
        self.assertEqual(report.status_code, 200, report.text)
        self._session("super_admin")
        day = now.astimezone(NY).date().isoformat()
        overview = self.client.get(
            f"/api/admin/finance/overview?start_date={day}&end_date={day}"
        )
        self.assertEqual(overview.status_code, 200, overview.text)
        self.assertEqual(len(overview.json()["data"]["daily"]), 1)
        mismatched = sample_report(now, broker_type="interactive_brokers")
        rejected = self.client.post(
            "/nodes/finance/report",
            headers={"Authorization": "Bearer node-token"},
            json=mismatched,
        )
        self.assertEqual(rejected.status_code, 400)


if __name__ == "__main__":
    unittest.main()
