"""
Tastytrade 券商适配器

从 origin_demo/server.py 移植核心逻辑:
  - Session 缓存与复用 (get_fresh 模式)
  - ACTION/TIF 枚举映射
  - Leg 构建 + 下单/撤单/持仓查询
  - Order 序列化
"""

import asyncio
import datetime
import hashlib
import logging
import time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from .base import BaseBrokerAPI, FinanceCollectionSkipped

log = logging.getLogger("trader_server.api.tastytrade")
QUOTE_STREAM_MAX_AGE_SECONDS = 6 * 60 * 60
QUOTE_STREAM_IDLE_SECONDS = 15 * 60
# SDK 导入标记
_SDK_AVAILABLE = False
_DX_AVAILABLE = False
try:
    from tastytrade import AlertStreamer, Session, DXLinkStreamer
    from tastytrade.account import Account, CurrentPosition
    from tastytrade.instruments import Equity
    from tastytrade.order import (
        NewOrder, OrderAction, OrderTimeInForce, OrderType, PlacedOrder,
    )
    try:
        from tastytrade.dxfeed import Quote as DXQuote
        _DX_AVAILABLE = True
    except ImportError:
        DXQuote = None
    _SDK_AVAILABLE = True
except ImportError:
    AlertStreamer = None
    CurrentPosition = None
    PlacedOrder = None
    DXQuote = None
    log.warning("Tastytrade SDK not available, TastytradeBroker will be non-functional")


# ── 映射表（与 origin_demo 一致）─────────────────────────────────────

if _SDK_AVAILABLE:
    ACTION_MAP = {
        "Buy to Open":   OrderAction.BUY_TO_OPEN,
        "Sell to Close": OrderAction.SELL_TO_CLOSE,
        "Sell to Open":  OrderAction.SELL_TO_OPEN,
        "Buy to Close":  OrderAction.BUY_TO_CLOSE,
    }

    TIF_MAP = {
        "Day":     OrderTimeInForce.DAY,
        "GTC":     OrderTimeInForce.GTC,
        "EXT":     OrderTimeInForce.EXT,
        "GTC_EXT": OrderTimeInForce.GTC_EXT,
    }
else:
    ACTION_MAP = {}
    TIF_MAP = {}


def _finance_float(value: Any, *, nullable: bool = False) -> float | None:
    if value is None or value == "":
        return None if nullable else 0.0
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None if nullable else 0.0
    if result != result or result in {float("inf"), float("-inf")}:
        return None if nullable else 0.0
    return result


def _enum_text(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip()


def _transaction_trade_date(transaction: Any) -> datetime.date | None:
    value = getattr(transaction, "transaction_date", None)
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=datetime.timezone.utc)
        return value.astimezone(ZoneInfo("America/New_York")).date()
    if isinstance(value, datetime.date):
        return value
    executed = getattr(transaction, "executed_at", None)
    if isinstance(executed, datetime.datetime):
        try:
            if executed.tzinfo is None:
                executed = executed.replace(tzinfo=datetime.timezone.utc)
            return executed.astimezone(ZoneInfo("America/New_York")).date()
        except Exception:
            return executed.date()
    return None


def _transaction_executed_at(transaction: Any, fallback_day: datetime.date) -> datetime.datetime:
    """Return a stable UTC timestamp for the TS-to-SM execution ledger."""
    executed = getattr(transaction, "executed_at", None)
    if isinstance(executed, datetime.datetime):
        if executed.tzinfo is None:
            executed = executed.replace(tzinfo=datetime.timezone.utc)
        return executed.astimezone(datetime.timezone.utc)
    if isinstance(executed, str) and executed.strip():
        try:
            parsed = datetime.datetime.fromisoformat(executed.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=datetime.timezone.utc)
            return parsed.astimezone(datetime.timezone.utc)
        except ValueError:
            pass
    return datetime.datetime.combine(
        fallback_day,
        datetime.time.min,
        ZoneInfo("America/New_York"),
    ).astimezone(
        datetime.timezone.utc
    )


def _is_equity_transaction(transaction: Any) -> bool:
    """Keep phase-one accounting scoped to US equity trades only."""
    instrument_type = _enum_text(getattr(transaction, "instrument_type", None)).lower()
    return not instrument_type or instrument_type == "equity"


def _transaction_execution_key(
    transaction: Any,
    *,
    account_id: str,
    executed_at: datetime.datetime,
    symbol: str,
    side: str,
    quantity: float,
    gross_amount: float,
    occurrence: int,
) -> str:
    transaction_id = str(getattr(transaction, "id", "") or "").strip()
    if transaction_id:
        return f"tt:{transaction_id}"
    fingerprint = ":".join((
        account_id,
        executed_at.isoformat(),
        symbol,
        side,
        str(quantity),
        str(gross_amount),
        str(occurrence),
    ))
    return f"tt:missing:{hashlib.sha256(fingerprint.encode('utf-8')).hexdigest()[:40]}"


def _build_tastytrade_finance_report(
    *,
    account: Any,
    trade_date: datetime.date,
    transactions: list[Any],
    transactions_complete: bool,
    balance: Any | None,
    positions: list[Any],
    equity_open: float | None,
    equity_close: float | None,
) -> dict:
    buy_amount = 0.0
    sell_amount = 0.0
    total_fees = 0.0
    trade_count = 0
    symbols: dict[str, dict[str, Any]] = {}
    trade_events: list[dict[str, Any]] = []
    seen_transactions: set[str] = set()
    synthetic_occurrences: dict[str, int] = {}
    skipped_non_equity = 0
    reversed_ids = {
        str(getattr(transaction, "reverses_id", "") or "")
        for transaction in transactions
        if getattr(transaction, "reverses_id", None)
    }

    for transaction in transactions:
        if _transaction_trade_date(transaction) not in {None, trade_date}:
            continue
        transaction_id = str(getattr(transaction, "id", "") or "")
        if transaction_id in reversed_ids or getattr(transaction, "reverses_id", None):
            continue
        if transaction_id and transaction_id in seen_transactions:
            continue
        if transaction_id:
            seen_transactions.add(transaction_id)

        action = _enum_text(getattr(transaction, "action", None))
        transaction_type = str(getattr(transaction, "transaction_type", "") or "")
        transaction_sub_type = str(getattr(transaction, "transaction_sub_type", "") or "")
        description = str(getattr(transaction, "description", "") or "")
        classification = " ".join((transaction_type, transaction_sub_type, description)).lower()
        quantity = abs(_finance_float(getattr(transaction, "quantity", 0)) or 0.0)
        price = abs(_finance_float(getattr(transaction, "price", 0)) or 0.0)
        value = _finance_float(getattr(transaction, "value", 0)) or 0.0
        net_value = _finance_float(getattr(transaction, "net_value", value)) or 0.0
        fees = sum(
            abs(_finance_float(getattr(transaction, field, 0)) or 0.0)
            for field in (
                "regulatory_fees",
                "clearing_fees",
                "commission",
                "proprietary_index_option_fees",
                "other_charge",
            )
        )

        is_trade = bool(action and ("buy" in action.lower() or "sell" in action.lower()))
        is_trade = is_trade or transaction_type.strip().lower() == "trade"
        if not is_trade:
            continue
        if not _is_equity_transaction(transaction):
            skipped_non_equity += 1
            continue

        gross = abs(value) or abs(quantity * price)
        is_buy = action.lower().startswith("buy")
        is_sell = action.lower().startswith("sell")
        if not is_buy and not is_sell:
            is_buy = value < 0
            is_sell = not is_buy
        side = "buy" if is_buy else "sell"
        if is_buy:
            buy_amount += gross
        else:
            sell_amount += gross
        total_fees += fees
        trade_count += 1
        symbol = str(
            getattr(transaction, "symbol", "")
            or getattr(transaction, "underlying_symbol", "")
            or "UNKNOWN"
        ).strip().upper()[:64] or "UNKNOWN"
        row = symbols.setdefault(symbol, {
            "symbol": symbol,
            "buy_quantity": 0.0,
            "sell_quantity": 0.0,
            "buy_amount": 0.0,
            "sell_amount": 0.0,
            "fees": 0.0,
            "trade_count": 0,
        })
        if is_buy:
            row["buy_quantity"] += quantity
            row["buy_amount"] += gross
        else:
            row["sell_quantity"] += quantity
            row["sell_amount"] += gross
        row["fees"] += fees
        row["trade_count"] += 1
        executed_at = _transaction_executed_at(transaction, trade_date)
        synthetic_base = ":".join((executed_at.isoformat(), symbol, side, str(quantity), str(gross)))
        occurrence = synthetic_occurrences.get(synthetic_base, 0) + 1
        synthetic_occurrences[synthetic_base] = occurrence
        trade_events.append({
            "execution_key": _transaction_execution_key(
                transaction,
                account_id=str(getattr(account, "account_number", "") or ""),
                executed_at=executed_at,
                symbol=symbol,
                side=side,
                quantity=quantity,
                gross_amount=gross,
                occurrence=occurrence,
            ),
            "executed_at": executed_at.isoformat(),
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "gross_amount": gross,
            "fee": fees,
            "realized_pnl": None,
        })

    realized_pnl = None
    unrealized_pnl = None
    if positions:
        realized_pnl = 0.0
        unrealized_pnl = 0.0
        for position in positions:
            realized_date = getattr(position, "realized_today_date", None)
            if realized_date in {None, trade_date}:
                realized_pnl += _finance_float(getattr(position, "realized_today", 0)) or 0.0
            quantity = abs(_finance_float(getattr(position, "quantity", 0)) or 0.0)
            direction = str(getattr(position, "quantity_direction", "Long") or "Long").lower()
            signed_quantity = -quantity if direction.startswith("short") else quantity
            mark = _finance_float(
                getattr(position, "mark_price", None),
                nullable=True,
            )
            if mark is None:
                mark = _finance_float(getattr(position, "mark", None), nullable=True)
            if mark is None:
                mark = _finance_float(getattr(position, "close_price", None), nullable=True)
            average = _finance_float(getattr(position, "average_open_price", None), nullable=True)
            multiplier = _finance_float(getattr(position, "multiplier", 1)) or 1.0
            if mark is not None and average is not None:
                unrealized_pnl += (mark - average) * signed_quantity * multiplier

    symbol_rows = []
    for row in symbols.values():
        row["trade_net_flow"] = row["sell_amount"] - row["buy_amount"] - row["fees"]
        symbol_rows.append(row)
    symbol_rows.sort(key=lambda item: item["buy_amount"] + item["sell_amount"], reverse=True)

    net_liquidating = (
        _finance_float(getattr(balance, "net_liquidating_value", None), nullable=True)
        if balance is not None else None
    )
    if equity_close is None:
        equity_close = net_liquidating
    return {
        "broker_account_id": str(getattr(account, "account_number", "") or ""),
        "currency": "USD",
        "balances": {
            "net_liquidating_value": net_liquidating,
            "cash_balance": _finance_float(getattr(balance, "cash_balance", None), nullable=True) if balance is not None else None,
            "buying_power": _finance_float(getattr(balance, "equity_buying_power", None), nullable=True) if balance is not None else None,
        },
        "trades": {
            "buy_amount": buy_amount,
            "sell_amount": sell_amount,
            "fees": total_fees,
            "trade_net_flow": sell_amount - buy_amount - total_fees,
            "trade_count": trade_count,
        },
        "cash_flows": {
            "deposits": None,
            "withdrawals": None,
            "dividends": None,
            "interest": None,
            "other_cash_flow": None,
        },
        "pnl": {
            "realized_pnl": realized_pnl,
            "unrealized_pnl": unrealized_pnl,
            "equity_open": equity_open,
            "equity_close": equity_close,
        },
        "symbols": symbol_rows,
        "trade_events": trade_events,
        "coverage": {
            "transactions_complete": bool(transactions_complete),
            "balances_available": balance is not None,
            "cash_flows_available": False,
            "fees_available": True,
            "pnl_available": bool(positions),
            "equity_open_scope": "broker_bod_snapshot" if equity_open is not None else "first_collected_snapshot",
            "broker_scope": "oauth_account_api",
        },
        "warnings": (
            [f"Skipped {skipped_non_equity} non-equity transaction(s) in phase-one finance reporting"]
            if skipped_non_equity else []
        ),
    }



class TastytradeBroker(BaseBrokerAPI):
    """
    Tastytrade 券商 API 适配器
    
    凭证格式 (credentials dict):
        secret (str): 必填 - Tastytrade Session Secret
        token (str):  必填 - Tastytrade Session Token
        account_number (str): 可选 - 账号号，留空则使用默认账户

    secret/token 分别对应 OAuth Client Secret 和 Refresh Token。
    """

    @classmethod
    def credential_profiles(cls) -> list[tuple[str, ...]]:
        return [("token", "secret")]

    @classmethod
    def supported_tifs(cls) -> tuple[str, ...]:
        return ("Day", "GTC", "EXT", "GTC_EXT")

    @staticmethod
    def _classify_connect_exception(exc: Exception) -> tuple[str, str, bool]:
        message = str(exc or "")[:240]
        lower = message.lower()
        if any(flag in lower for flag in (
            "invalid_grant",
            "invalid jwt",
            "invalid credentials",
            "invalid login",
            "invalid token",
            "please check your username and password",
            "unauthorized",
            "401",
        )):
            return "BROKER_AUTH_INVALID", message, False
        if "forbidden" in lower or "403" in lower:
            return "BROKER_AUTH_FORBIDDEN", message, False
        if "no accounts found" in lower:
            return "BROKER_ACCOUNT_MISSING", message, False
        return "BROKER_CONNECT_FAILED", message, True
    def __init__(self):
        super().__init__(broker_type="tastytrade")

        # Session 缓存（复用 origin_demo 的 session_store 模式）
        self._session: Any | None = None
        self._account: Any | None = None
        self._account_authority = "unknown"
        self._equity_cache: dict[str, Any] = {}

        # TT DX 行情流状态
        self._quote_streamer: Any | None = None
        self._quote_streamer_cm: Any | None = None
        self._quote_task: asyncio.Task | None = None
        self._quote_owner_task: asyncio.Task | None = None
        self._quote_stop_event = asyncio.Event()
        self._quote_ready_event = asyncio.Event()
        self._quote_stream_error: Exception | None = None
        self._subscribed_symbols: set[str] = set()
        self._quote_lock = asyncio.Lock()
        self._quote_refresh_waiters: dict[str, list[tuple[str, asyncio.Future]]] = {}
        self._quote_stream_started_at = 0.0
        self._account_streamer: Any | None = None
        self._account_streamer_cm: Any | None = None
        self._account_event_tasks: list[asyncio.Task] = []
        self._account_stream_owner_task: asyncio.Task | None = None
        self._account_stream_stop_event = asyncio.Event()
        self._account_stream_ready_event = asyncio.Event()
        self._account_stream_error: Exception | None = None
        self._account_event_restart_task: asyncio.Task | None = None
        self._account_event_lock = asyncio.Lock()
        self._finance_lock = asyncio.Lock()
        self._trade_activity = 0
        self._finance_equity_open_cache: dict[str, float] = {}
        self._last_connect_detail: dict[str, Any] = {}

    def set_connection_error(
        self,
        code: str,
        message: str,
        retryable: bool = True,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().set_connection_error(code, message, retryable=retryable)
        self._last_connect_detail = dict(detail or {})

    def clear_connection_error(self) -> None:
        super().clear_connection_error()
        self._last_connect_detail = {}

    def get_connection_error(self) -> dict[str, Any]:
        err = super().get_connection_error()
        if self._last_connect_detail:
            err.update(self._last_connect_detail)
        return err



    async def connect(self, credentials: dict) -> bool:
        """Create the TS-managed tastytrade OAuth session."""
        # Reconnect can be requested by a normal broker operation when the
        # cached session expires. Stop old streams before replacing the
        # session so their owner tasks can close their contexts cleanly.
        if self._account_stream_owner_task or self._quote_owner_task:
            await self.stop_account_events()
            await self._stop_quote_stream()
        self._connected = False
        self._session = None
        self._account = None
        self._account_authority = "unknown"
        self._equity_cache = {}
        self._finance_equity_open_cache.clear()

        if not _SDK_AVAILABLE:
            self.set_connection_error("BROKER_SDK_MISSING", "Tastytrade SDK not installed", retryable=False)
            log.error("Tastytrade SDK not installed")
            return False

        normalized = self.normalize_credentials(credentials)
        valid, reason = self.validate_credentials(normalized)
        if not valid:
            self.set_connection_error("BROKER_CREDENTIALS_INVALID", reason, retryable=False)
            log.error(f"Tastytrade credentials invalid: {reason}")
            return False

        self._credentials = normalized
        secret = normalized.get("secret", "")
        token = normalized.get("token", "")
        acct_num = str(normalized.get("account_number", "") or "").strip()

        try:
            self._session = Session(secret, token)
            account_records = await self._get_account_records(self._session)
            if acct_num:
                selected_record = next(
                    (
                        record
                        for record in account_records
                        if str(getattr(record["account"], "account_number", "")) == acct_num
                    ),
                    None,
                )
                if selected_record is None:
                    self.set_connection_error(
                        "BROKER_ACCOUNT_NOT_FOUND",
                        f"Configured account {acct_num} is not accessible with this OAuth grant",
                        retryable=False,
                    )
                    return False
            else:
                selected_record = next(
                    (
                        record
                        for record in account_records
                        if not bool(getattr(record["account"], "is_closed", False))
                    ),
                    None,
                )

            if not selected_record:
                self.set_connection_error("BROKER_ACCOUNT_MISSING", "No accounts found for this session", retryable=False)
                log.error("No accounts found for this session")
                return False
            self._account = selected_record["account"]
            self._account_authority = selected_record["authority_level"]
            if bool(getattr(self._account, "is_closed", False)):
                self.set_connection_error(
                    "BROKER_ACCOUNT_CLOSED",
                    f"Configured account {acct_num} is closed",
                    retryable=False,
                )
                self._account = None
                return False

            self._connected = True
            self.clear_connection_error()
            account_num = getattr(self._account, "account_number", "?")
            log.info(f"TastytradeBroker connected, account={account_num}")
            return True

        except Exception as e:
            code, message, retryable = self._classify_connect_exception(e)
            self.set_connection_error(code, message, retryable=retryable)
            log.error(f"TastytradeBroker connect failed [{code}]: {message}")
            self._account = None
            return False
        finally:
            if not self._connected and self._session is not None:
                session = self._session
                self._session = None
                client = getattr(session, "_client", None)
                if client is not None:
                    try:
                        await client.aclose()
                    except Exception:
                        pass

    async def _get_account_records(self, session: Any) -> list[dict[str, Any]]:
        data = await session._get("/customers/me/accounts")
        items = data.get("items", []) if isinstance(data, dict) else []
        records: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("account"), dict):
                continue
            records.append({
                "account": Account(**item["account"]),
                "authority_level": str(
                    item.get("authority-level") or item.get("authority_level") or "unknown"
                ).strip().lower(),
            })
        return records

    def effective_capabilities(self) -> dict[str, bool]:
        capabilities = super().effective_capabilities()
        if self._account_authority in {"read-only", "read_only", "readonly"}:
            capabilities["orders"] = False
            capabilities["cancel_order"] = False
        return capabilities

    def status_detail(self) -> dict[str, Any]:
        account = self._account
        return {
            "account": {
                "account_number": str(getattr(account, "account_number", "") or ""),
                "nickname": str(getattr(account, "nickname", "") or ""),
                "account_type": str(getattr(account, "account_type_name", "") or ""),
                "authority_level": self._account_authority,
                "is_closed": bool(getattr(account, "is_closed", False)) if account else False,
            },
            "order_options": {
                "default_route": "SMART",
                "routes": ["SMART"],
                "route_editable": False,
                "hidden_order": False,
                "supported_tifs": list(self.supported_tifs()),
            },
        }

    async def disconnect(self) -> None:
        """断开连接，清除缓存"""
        await self.stop_account_events()
        await self._stop_quote_stream()
        session = self._session
        self._session = None
        self._account = None
        self._account_authority = "unknown"
        self._equity_cache.clear()
        self._connected = False
        client = getattr(session, "_client", None)
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass
        log.info("TastytradeBroker disconnected")

    async def start_account_events(self) -> None:
        if not AlertStreamer or not PlacedOrder or not CurrentPosition:
            raise RuntimeError("Tastytrade account alert stream is unavailable")
        if not self._connected or not self._session or not self._account:
            return
        async with self._account_event_lock:
            if self._account_stream_owner_task and not self._account_stream_owner_task.done():
                return
            await self._stop_account_events_locked()
            self._account_stream_stop_event = asyncio.Event()
            self._account_stream_ready_event = asyncio.Event()
            self._account_stream_error = None
            self._account_stream_owner_task = asyncio.create_task(
                self._account_stream_owner(),
                name="tt-account-stream-owner",
            )
            try:
                await asyncio.wait_for(self._account_stream_ready_event.wait(), timeout=15)
            except Exception:
                owner = self._account_stream_owner_task
                if owner and not owner.done():
                    owner.cancel()
                raise
            owner = self._account_stream_owner_task
            if owner and owner.done() and not owner.cancelled():
                owner.result()

    async def _account_stream_owner(self) -> None:
        """Own the TT account context from enter through exit in one task."""
        try:
            streamer = AlertStreamer(self._session)
            async with streamer as entered:
                self._account_streamer_cm = streamer
                self._account_streamer = entered if entered is not None else streamer
                await self._account_streamer.subscribe_accounts([self._account])
                self._account_event_tasks = [
                    asyncio.create_task(
                        self._consume_account_alerts(PlacedOrder, self._handle_order_alert),
                        name="tt-order-alerts",
                    ),
                    asyncio.create_task(
                        self._consume_account_alerts(CurrentPosition, self._handle_position_alert),
                        name="tt-position-alerts",
                    ),
                ]
                self._account_stream_ready_event.set()
                log.info("TT account event stream started")
                await self._account_stream_stop_event.wait()
        except Exception as exc:
            self._account_stream_error = exc
            raise
        finally:
            current = asyncio.current_task()
            tasks = list(self._account_event_tasks)
            self._account_event_tasks = []
            for task in tasks:
                if task is not current and not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*[task for task in tasks if task is not current], return_exceptions=True)
            self._account_streamer = None
            self._account_streamer_cm = None
            if not self._account_stream_ready_event.is_set():
                self._account_stream_ready_event.set()

    async def stop_account_events(self) -> None:
        restart_task = self._account_event_restart_task
        self._account_event_restart_task = None
        if restart_task and restart_task is not asyncio.current_task() and not restart_task.done():
            restart_task.cancel()
        async with self._account_event_lock:
            await self._stop_account_events_locked()

    async def _stop_account_events_locked(self) -> None:
        owner = self._account_stream_owner_task
        if owner and not owner.done():
            self._account_stream_stop_event.set()
            if owner is not asyncio.current_task():
                try:
                    await owner
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    log.warning("TT account streamer close failed: %s", exc)
        self._account_stream_owner_task = None
        self._account_event_tasks = []
        self._account_streamer = None
        self._account_streamer_cm = None
        self._account_stream_error = None

    async def _consume_account_alerts(self, alert_type: Any, handler: Any) -> None:
        try:
            stream = self._account_streamer.listen(alert_type)
            async for alert in stream:
                if not self._connected:
                    return
                handler(alert)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("TT account event stream failed (%s): %s", getattr(alert_type, "__name__", alert_type), exc)
            self._schedule_account_event_restart()

    def _schedule_account_event_restart(self) -> None:
        if not self._connected:
            return
        task = self._account_event_restart_task
        if task and not task.done():
            return
        self._account_event_restart_task = asyncio.create_task(
            self._restart_account_events(),
            name="tt-account-events-restart",
        )

    async def _restart_account_events(self) -> None:
        try:
            await asyncio.sleep(1.0)
            await self.stop_account_events()
            if self._connected:
                await self.start_account_events()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("TT account event stream restart failed: %s", exc)
        finally:
            if self._account_event_restart_task is asyncio.current_task():
                self._account_event_restart_task = None

    @staticmethod
    def _filled_quantity(order: Any) -> float:
        total = 0.0
        for leg in getattr(order, "legs", []) or []:
            for fill in getattr(leg, "fills", []) or []:
                try:
                    total += float(getattr(fill, "quantity", 0) or 0)
                except (TypeError, ValueError):
                    pass
        return total

    def _handle_order_alert(self, order: Any) -> None:
        account_number = str(getattr(order, "account_number", "") or "")
        selected = str(getattr(self._account, "account_number", "") or "")
        if selected and account_number != selected:
            return
        status = str(getattr(order, "status", "") or "")
        filled = self._filled_quantity(order)
        try:
            quantity = float(getattr(order, "size", 0) or 0)
        except (TypeError, ValueError):
            quantity = 0.0
        remaining = max(0.0, quantity - filled)
        if filled > 0 and remaining > 0:
            status = "Partial"
        elif status == "Routed":
            status = "Routing"
        elif status == "In Flight":
            status = "Routing"
        elif status == "Cancel Requested":
            status = "Cancelling"
        self._on_order_event({
            "order_id": str(getattr(order, "id", "") or ""),
            "symbol": str(getattr(order, "underlying_symbol", "") or "").upper(),
            "status": status,
            "status_message": str(getattr(order, "reject_reason", "") or ""),
            "filled_qty": filled,
            "remaining_qty": remaining,
            "avg_fill_price": 0.0,
            "can_cancel": bool(getattr(order, "cancellable", False)),
            "updated_at": str(getattr(order, "updated_at", "") or ""),
        })

    def _handle_position_alert(self, position: Any) -> None:
        account_number = str(getattr(position, "account_number", "") or "")
        selected = str(getattr(self._account, "account_number", "") or "")
        if selected and account_number != selected:
            return
        self._on_position_event({
            "reason": "position_update",
            "symbol": str(getattr(position, "symbol", "") or "").upper(),
            "updated_at": str(getattr(position, "updated_at", "") or ""),
        })


    async def is_connected(self) -> bool:
        """检查 Session 是否有效"""
        if not self._connected or not self._session:
            return False
        # TT Session 对象有 is_active 或类似属性
        return hasattr(self._session, "session_token") and bool(getattr(
            self._session, "session_token", None
        ))

    async def reconnect(self) -> bool:
        """重新创建 Session 连接"""
        return await self.connect(self._credentials)

    def _ensure_session(self) -> tuple[Any, Any]:
        """
        确保返回有效的 (session, account)，类似 origin_demo.get_fresh()
        
        Raises:
            RuntimeError: 未连接时抛出
        """
        if not self._connected or not self._session or not self._account:
            raise RuntimeError("TastytradeBroker not connected. Call connect() first.")
        return self._session, self._account

    @staticmethod
    def _normalize_place_order_response(response: Any) -> dict:
        placed_order = getattr(response, "order", None) if response else None
        if placed_order is None:
            return {
                "success": False,
                "code": "ORDER_RESPONSE_INVALID",
                "order_id": "",
                "status": "Rejected",
                "status_message": "券商未返回订单信息",
            }
        order_id = str(getattr(placed_order, "id", "") or "")
        order_status = str(getattr(placed_order, "status", "") or "").split(".")[-1]
        status_message = str(getattr(placed_order, "reject_reason", "") or "")
        if order_status == "Rejected":
            return {
                "success": False,
                "code": "ORDER_REJECTED",
                "order_id": order_id,
                "status": order_status,
                "status_message": status_message or "订单被券商拒绝",
            }
        return {
            "success": True,
            "order_id": order_id,
            "status": order_status or "Received",
            "status_message": status_message,
        }

    async def place_order(self, order_params: dict) -> dict:
        """下单"""
        self._trade_activity += 1
        try:
            return await self._place_order_impl(order_params)
        finally:
            self._trade_activity = max(0, self._trade_activity - 1)

    async def _place_order_impl(self, order_params: dict) -> dict:
        tif_str = str(order_params.get("tif") or "Day").strip() or "Day"
        if tif_str not in self.supported_tifs():
            return {
                "success": False,
                "code": "ORDER_UNSUPPORTED_TIF",
                "order_id": "",
                "status": "Rejected",
                "status_message": f"当前交易通道不支持 {tif_str} 订单",
            }

        s, a = await self._get_fresh()
        symbol = order_params["symbol"]
        qty = order_params.get("qty", 1)
        price = float(order_params.get("price", 0.0))
        action_str = order_params.get("action", "Buy to Open")
        order_type_str = order_params.get("order_type", "limit")
        route = str(order_params.get("route") or "SMART").strip().upper()
        hidden = bool(order_params.get("hidden", False))
        if route and route not in {"SMART", "DEFAULT"}:
            raise ValueError("tastytrade only supports SMART route in this Client")
        if hidden:
            raise ValueError("tastytrade does not support hidden orders in this Client")

        act = ACTION_MAP.get(action_str, OrderAction.BUY_TO_OPEN)
        tif_enum = TIF_MAP[tif_str]
        is_buy = "Buy" in action_str

        equity = await self._get_equity(s, symbol)
        leg = equity.build_leg(Decimal(str(qty)), act)

        if order_type_str == "market":
            order = NewOrder(time_in_force=tif_enum, order_type=OrderType.MARKET, legs=[leg])
            sdk_price = "MKT"
        else:
            signed = Decimal(str(price)) * (-1 if is_buy else 1)
            order = NewOrder(
                time_in_force=tif_enum, order_type=OrderType.LIMIT,
                legs=[leg], price=signed,
            )
            sdk_price = str(signed)

        log.info(
            "[ORDER_DIAG][TT_SUBMIT] symbol=%s action=%s qty=%s client_price=%s sdk_price=%s order_type=%s tif=%s route=%s hidden=%s is_buy=%s",
            symbol,
            action_str,
            qty,
            price,
            sdk_price,
            order_type_str,
            tif_str,
            route,
            hidden,
            is_buy,
        )

        resp = await a.place_order(s, order, dry_run=False)
        result = self._normalize_place_order_response(resp)
        if not result["success"]:
            log.warning(
                "Order rejected: %s %s %s @ %s reason=%s",
                action_str,
                qty,
                symbol,
                price,
                result["status_message"] or "unknown",
            )
            log.warning(
                "[ORDER_DIAG][TT_REJECT] symbol=%s action=%s qty=%s client_price=%s sdk_price=%s order_type=%s tif=%s route=%s hidden=%s code=%s status=%s reason=%s",
                symbol,
                action_str,
                qty,
                price,
                sdk_price,
                order_type_str,
                tif_str,
                route,
                hidden,
                result.get("code", ""),
                result.get("status", ""),
                result.get("status_message", ""),
            )
            return result
        log.info(f"Order placed: {action_str} {qty} {symbol} @ {price}")
        return result

    async def cancel_order(self, order_id: str) -> dict:
        """撤单"""
        self._trade_activity += 1
        try:
            s, a = await self._get_fresh()
            await a.delete_order(s, order_id)
            log.info(f"Order cancelled: {order_id}")
            return {"success": True}
        finally:
            self._trade_activity = max(0, self._trade_activity - 1)

    async def collect_finance_report(self, trade_date: str, timeout: float = 12.0) -> dict:
        """Collect account finance data without refreshing or replacing the TT session."""
        if self._finance_lock.locked() or self._trade_activity:
            raise FinanceCollectionSkipped("FINANCE_BUSY", "Tastytrade finance read already in progress")
        try:
            await asyncio.wait_for(self._finance_lock.acquire(), timeout=0.05)
        except asyncio.TimeoutError as exc:
            raise FinanceCollectionSkipped("FINANCE_BUSY", "Tastytrade finance read lock unavailable") from exc

        try:
            session = self._session
            account = self._account
            if self._trade_activity or not self._connected or session is None or account is None:
                raise FinanceCollectionSkipped("BROKER_NOT_READY", "Tastytrade session is not connected")

            try:
                day = datetime.date.fromisoformat(str(trade_date or ""))
            except ValueError as exc:
                raise ValueError("trade_date must use YYYY-MM-DD") from exc

            async def collect() -> dict:
                from zoneinfo import ZoneInfo

                def ensure_idle() -> None:
                    if self._trade_activity:
                        raise FinanceCollectionSkipped(
                            "FINANCE_PREEMPTED",
                            "Tastytrade trade request preempted finance collection",
                        )

                ny_today = datetime.datetime.now(ZoneInfo("America/New_York")).date()
                is_current = day == ny_today
                transactions: list[Any] = []
                transactions_complete = True
                page_size = 250
                for page_offset in range(20):
                    ensure_idle()
                    page = await account.get_history(
                        session,
                        per_page=page_size,
                        page_offset=page_offset,
                        sort="Asc",
                        start_date=day,
                        end_date=day,
                    )
                    transactions.extend(page)
                    if len(page) < page_size:
                        break
                else:
                    transactions_complete = False

                balance = None
                equity_open = None
                equity_close = None
                positions: list[Any] = []
                if is_current:
                    ensure_idle()
                    balance = await account.get_balances(session, currency="USD")
                    ensure_idle()
                    positions = await account.get_positions(
                        session,
                        include_closed=True,
                        include_marks=True,
                    )
                    equity_close = _finance_float(getattr(balance, "net_liquidating_value", None), nullable=True)
                    equity_open = self._finance_equity_open_cache.get(day.isoformat())
                    if equity_open is None:
                        ensure_idle()
                        bod = await account.get_balance_snapshots(
                            session,
                            snapshot_date=day,
                            time_of_day="BOD",
                            currency="USD",
                        )
                        if bod:
                            equity_open = _finance_float(
                                getattr(bod[-1], "net_liquidating_value", None),
                                nullable=True,
                            )
                            if equity_open is not None:
                                self._finance_equity_open_cache[day.isoformat()] = equity_open
                else:
                    ensure_idle()
                    bod = await account.get_balance_snapshots(
                        session,
                        snapshot_date=day,
                        time_of_day="BOD",
                        currency="USD",
                    )
                    ensure_idle()
                    eod = await account.get_balance_snapshots(
                        session,
                        snapshot_date=day,
                        time_of_day="EOD",
                        currency="USD",
                    )
                    if bod:
                        equity_open = _finance_float(getattr(bod[-1], "net_liquidating_value", None), nullable=True)
                    if eod:
                        balance = eod[-1]
                        equity_close = _finance_float(getattr(balance, "net_liquidating_value", None), nullable=True)

                ensure_idle()
                return _build_tastytrade_finance_report(
                    account=account,
                    trade_date=day,
                    transactions=transactions,
                    transactions_complete=transactions_complete,
                    balance=balance,
                    positions=positions,
                    equity_open=equity_open,
                    equity_close=equity_close,
                )

            try:
                report = await asyncio.wait_for(collect(), timeout=max(2.0, float(timeout)))
            except asyncio.TimeoutError as exc:
                raise FinanceCollectionSkipped("FINANCE_TIMEOUT", "Tastytrade finance read timed out") from exc
            if self._session is not session or self._account is not account or not self._connected:
                raise FinanceCollectionSkipped("BROKER_CHANGED", "Tastytrade session changed during finance read")
            return report
        finally:
            self._finance_lock.release()

    async def get_positions(self, filters: dict | None = None) -> list[dict]:
        """获取持仓列表"""
        s, a = await self._get_fresh()
        raw_positions = await a.get_positions(s)

        result = []
        for p in raw_positions:
            result.append({
                "symbol": p.symbol,
                "quantity": float(p.quantity),
                "direction": getattr(p, "quantity_direction", "Long"),
                "average_open_price": float(p.average_open_price or 0),
                "close_price": float(p.close_price or 0),
                "realized_today": float(getattr(p, "realized_today", 0) or 0),
            })

        # 可选过滤
        if filters and "symbols" in filters:
            sym_set = set(filters["symbols"])
            result = [p for p in result if p["symbol"] in sym_set]

        log.info(f"Positions retrieved: {len(result)} items")
        return result

    async def get_orders(self, mode: str = "live") -> list[dict]:
        """查询订单列表"""
        s, a = await self._get_fresh()
        mode = (mode or "live").lower()
        if mode == "all":
            raw = await a.get_order_history(s)
        else:
            raw = await a.get_live_orders(s)
        return [self.serialize_order(o) for o in raw]

    async def subscribe_quotes(self, symbols: list[str]) -> None:
        """订阅 TT 行情（DXLink）"""
        if not _SDK_AVAILABLE or not _DX_AVAILABLE or not DXQuote:
            raise RuntimeError("Tastytrade DX quote stream is unavailable")

        valid = {str(s).strip().upper() for s in (symbols or []) if str(s).strip()}
        if not valid:
            return

        async with self._quote_lock:
            _, _ = await self._get_fresh()
            wanted_symbols = set(self._subscribed_symbols) | valid

            if self._quote_stream_needs_rebuild_locked():
                await self._rebuild_quote_stream_locked(wanted_symbols)
                log.info(f"TT quote stream rebuilt; subscribed: {sorted(wanted_symbols)}")
                return

            await self._ensure_quote_streamer_locked(start_consumer=False)

            new_syms = sorted(valid - self._subscribed_symbols)
            if not new_syms:
                await self._ensure_quote_streamer_locked(start_consumer=True)
                return

            await self._streamer_subscribe(new_syms)
            self._subscribed_symbols.update(new_syms)
            await self._ensure_quote_streamer_locked(start_consumer=True)
            log.info(f"TT quote subscribed: {new_syms}")



    async def unsubscribe_quotes(self, symbols: list[str]) -> None:
        valid = {str(s).strip().upper() for s in (symbols or []) if str(s).strip()}
        if not valid:
            return

        async with self._quote_lock:
            remove_syms = sorted(valid & self._subscribed_symbols)
            if not remove_syms:
                return

            if self._quote_streamer and hasattr(self._quote_streamer, "unsubscribe"):
                await self._streamer_unsubscribe(remove_syms)



            self._subscribed_symbols.difference_update(remove_syms)
            log.info(f"TT quote unsubscribed: {remove_syms}")

    async def refresh_quote(
        self,
        symbol: str,
        price_source: str,
        timeout: float = 5.0,
    ) -> dict:
        """Wait for a post-request quote event without rebuilding DXLink."""
        normalized = str(symbol or "").strip().upper()
        source = str(price_source or "").strip().lower()
        if not normalized or source not in {"bid", "ask"}:
            raise ValueError("Invalid quote refresh parameters")
        if normalized not in self._subscribed_symbols:
            await self.subscribe_quotes([normalized])
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        async with self._quote_lock:
            self._quote_refresh_waiters.setdefault(normalized, []).append((source, future))
            try:
                await self._ensure_quote_streamer_locked(start_consumer=True)
            except Exception:
                waiters = self._quote_refresh_waiters.get(normalized, [])
                self._quote_refresh_waiters[normalized] = [
                    item for item in waiters if item[1] is not future
                ]
                if not self._quote_refresh_waiters[normalized]:
                    self._quote_refresh_waiters.pop(normalized, None)
                raise
        try:
            return dict(await asyncio.wait_for(asyncio.shield(future), timeout=max(0.1, float(timeout))))
        finally:
            waiters = self._quote_refresh_waiters.get(normalized, [])
            self._quote_refresh_waiters[normalized] = [
                item for item in waiters if item[1] is not future
            ]
            if not self._quote_refresh_waiters[normalized]:
                self._quote_refresh_waiters.pop(normalized, None)

    async def _ensure_quote_streamer_locked(self, start_consumer: bool = True) -> None:
        if self._quote_owner_task is None or self._quote_owner_task.done():
            self._quote_stop_event = asyncio.Event()
            self._quote_ready_event = asyncio.Event()
            self._quote_stream_error = None
            self._quote_owner_task = asyncio.create_task(
                self._quote_stream_owner(),
                name="tt-quote-stream-owner",
            )
            try:
                await asyncio.wait_for(self._quote_ready_event.wait(), timeout=15)
            except Exception:
                owner = self._quote_owner_task
                if owner and not owner.done():
                    owner.cancel()
                raise
            owner = self._quote_owner_task
            if owner and owner.done() and not owner.cancelled():
                owner.result()

        if self._quote_streamer is None:
            raise RuntimeError("Tastytrade quote stream failed to start")

    async def _quote_stream_owner(self) -> None:
        """Own the TT quote context from enter through exit in one task."""
        streamer = None
        cm = None
        try:
            streamer = await self._create_quote_streamer()
            cm = streamer if hasattr(streamer, "__aenter__") else None
            if cm is None:
                self._quote_streamer = streamer
                self._quote_streamer_cm = None
                self._quote_stream_started_at = time.monotonic()
                self._quote_ready_event.set()
                self._quote_task = asyncio.create_task(
                    self._quote_consume_loop(),
                    name="tt-quote-consumer",
                )
                await self._quote_stop_event.wait()
                close_fn = getattr(streamer, "close", None)
                if close_fn:
                    await self._maybe_await(close_fn())
                return

            async with cm as entered:
                self._quote_streamer_cm = cm
                self._quote_streamer = entered if entered is not None else cm
                self._quote_stream_started_at = time.monotonic()
                self._quote_ready_event.set()
                self._quote_task = asyncio.create_task(
                    self._quote_consume_loop(),
                    name="tt-quote-consumer",
                )
                await self._quote_stop_event.wait()
        except Exception as exc:
            self._quote_stream_error = exc
            raise
        finally:
            task = self._quote_task
            self._quote_task = None
            if task and task is not asyncio.current_task() and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self._quote_streamer = None
            self._quote_streamer_cm = None
            self._quote_stream_started_at = 0.0
            if not self._quote_ready_event.is_set():
                self._quote_ready_event.set()

    def _quote_stream_needs_rebuild_locked(self) -> bool:
        if self._quote_streamer is None:
            return False
        if self._quote_task is not None and self._quote_task.done():
            return True
        age = time.monotonic() - self._quote_stream_started_at if self._quote_stream_started_at else 0
        return age > QUOTE_STREAM_MAX_AGE_SECONDS

    async def _create_quote_streamer(self):
        if hasattr(DXLinkStreamer, "create"):
            streamer = await self._maybe_await(DXLinkStreamer.create(self._session))
        else:
            streamer = await self._maybe_await(DXLinkStreamer(self._session))

        return streamer

    async def _stop_quote_stream(self) -> None:
        async with self._quote_lock:
            self._subscribed_symbols.clear()
            await self._stop_quote_stream_locked()

    async def _stop_quote_stream_locked(self) -> None:
        for waiters in self._quote_refresh_waiters.values():
            for _source, future in waiters:
                if not future.done():
                    future.set_exception(RuntimeError("TT quote stream stopped"))
        self._quote_refresh_waiters.clear()
        owner = self._quote_owner_task
        if owner and not owner.done():
            self._quote_stop_event.set()
            if owner is not asyncio.current_task():
                try:
                    await owner
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    log.warning(f"TT quote streamer close failed: {e}")

        self._quote_owner_task = None
        self._quote_task = None
        self._quote_streamer = None
        self._quote_streamer_cm = None
        self._quote_stream_started_at = 0.0
        self._quote_stream_error = None

    async def _rebuild_quote_stream_locked(self, symbols: set[str] | list[str] | tuple[str, ...]) -> None:
        wanted = sorted({str(sym).strip().upper() for sym in symbols if str(sym).strip()})
        await self._stop_quote_stream_locked()
        self._subscribed_symbols = set()
        await self._ensure_quote_streamer_locked(start_consumer=True)
        if wanted:
            await self._streamer_subscribe(wanted)
            self._subscribed_symbols.update(wanted)

    async def _restart_quote_stream_after_consume_error(self, reason: str) -> None:
        async with self._quote_lock:
            wanted = set(self._subscribed_symbols)
            if not self._connected or not wanted:
                await self._stop_quote_stream_locked()
                return
            try:
                await self._rebuild_quote_stream_locked(wanted)
                log.info("TT quote stream restarted after %s; subscribed: %s", reason, sorted(wanted))
            except Exception as exc:
                log.warning("TT quote stream restart failed after %s: %s", reason, exc)

    async def _quote_consume_loop(self) -> None:
        while self._connected and self._quote_streamer is not None:
            try:
                if hasattr(self._quote_streamer, "get_event"):
                    waitable = self._streamer_get_event()
                    if self._subscribed_symbols:
                        event = await asyncio.wait_for(waitable, timeout=QUOTE_STREAM_IDLE_SECONDS)
                    else:
                        event = await waitable
                    quote = self._normalize_quote_event(event)
                    self._publish_quote(quote)
                    continue

                if hasattr(self._quote_streamer, "listen"):
                    stream = await self._streamer_listen()
                    async for event in stream:
                        if not self._connected:
                            return
                        quote = self._normalize_quote_event(event)
                        self._publish_quote(quote)
                    continue

                log.error("TT quote streamer has no supported consume API")
                return
            except asyncio.CancelledError:
                break
            except asyncio.TimeoutError:
                log.warning("TT quote stream idle for %ss; restarting", QUOTE_STREAM_IDLE_SECONDS)
                asyncio.create_task(
                    self._restart_quote_stream_after_consume_error("idle timeout"),
                    name="tt-quote-stream-restart",
                )
                return
            except Exception as e:
                log.warning(f"TT quote consume loop error: {e}")
                asyncio.create_task(
                    self._restart_quote_stream_after_consume_error(type(e).__name__),
                    name="tt-quote-stream-restart",
                )
                return

    def _publish_quote(self, quote: dict | None) -> None:
        if not quote:
            return
        symbol = str(quote.get("symbol") or "").strip().upper()
        if not symbol:
            return
        for source, future in list(self._quote_refresh_waiters.get(symbol, [])):
            if not future.done() and float(quote.get(source, 0) or 0) > 0:
                future.set_result(dict(quote))
        if self._quote_callback:
            self._quote_callback(quote)

    async def _streamer_subscribe(self, symbols: list[str]) -> None:
        fn = getattr(self._quote_streamer, "subscribe", None)
        if not fn:
            raise RuntimeError("TT quote streamer missing subscribe()")

        last_err: Exception | None = None
        for args in ((DXQuote, symbols), (symbols,)):
            try:
                await self._maybe_await(fn(*args))
                return
            except TypeError as e:
                last_err = e
        raise last_err or RuntimeError("TT quote subscribe failed")

    async def _streamer_unsubscribe(self, symbols: list[str]) -> None:
        fn = getattr(self._quote_streamer, "unsubscribe", None)
        if not fn:
            return

        last_err: Exception | None = None
        for args in ((DXQuote, symbols), (symbols,)):
            try:
                await self._maybe_await(fn(*args))
                return
            except TypeError as e:
                last_err = e
        if last_err:
            raise last_err

    async def _streamer_get_event(self):
        fn = self._quote_streamer.get_event
        last_err: Exception | None = None
        for args in ((DXQuote,), tuple()):
            try:
                return await self._maybe_await(fn(*args))
            except TypeError as e:
                last_err = e
        raise last_err or RuntimeError("TT quote get_event failed")

    async def _streamer_listen(self):
        fn = self._quote_streamer.listen
        last_err: Exception | None = None
        for args in ((DXQuote,), tuple()):
            try:
                stream = fn(*args)
                return await self._maybe_await(stream)
            except TypeError as e:
                last_err = e
        raise last_err or RuntimeError("TT quote listen failed")



    @staticmethod
    async def _maybe_await(value):
        if asyncio.iscoroutine(value):
            return await value
        return value

    @staticmethod
    def _normalize_quote_event(event: Any) -> dict | None:

        try:
            symbol = str(getattr(event, "event_symbol", "") or getattr(event, "symbol", "")).strip().upper()
            if not symbol:
                return None

            bid = float(getattr(event, "bid_price", None) or getattr(event, "bid", 0) or 0)
            ask = float(getattr(event, "ask_price", None) or getattr(event, "ask", 0) or 0)
            last = float(getattr(event, "last_price", None) or getattr(event, "price", 0) or 0)
            if last <= 0 and bid > 0 and ask > 0:
                last = round((bid + ask) / 2, 4)

            volume = int(
                float(
                    getattr(event, "day_volume", None)
                    or getattr(event, "volume", None)
                    or 0
                )
            )

            if bid <= 0 and ask <= 0 and last <= 0:
                return None

            return {
                "symbol": symbol,
                "bid": bid,
                "ask": ask,
                "last": last,
                "volume": volume,
                "ts": datetime.datetime.now().strftime("%H:%M:%S"),
            }
        except Exception:
            return None

    # ── 内部辅助 ───────────────────────────────────────────────


    async def _get_fresh(self) -> tuple[Any, Any]:
        """
        确保返回有效的 session/account（带自动重建能力）
        
        与 origin_demo server.py 第143-153行 get_fresh() 逻辑一致:
          - 有缓存且有效 → 直接返回
          - 无缓存或失效 → 用凭证重新创建
        """
        if self._session and self._account and await self.is_connected():
            return self._session, self._account

        # fallback: 重新连接
        ok = await self.reconnect()
        if not ok:
            raise RuntimeError("Failed to refresh Tastytrade session")
        return self._session, self._account

    async def _get_equity(self, session: Any, symbol: str) -> Any:
        sym = (symbol or "").strip().upper()
        if not sym:
            raise ValueError("symbol is required")
        cached = self._equity_cache.get(sym)
        if cached is not None:
            return cached
        equity = await Equity.get(session, sym)
        self._equity_cache[sym] = equity
        return equity

    @staticmethod
    def serialize_order(order_obj: Any) -> dict:
        """
        序列化 Order 对象为字典
        
        与 origin_demo server.py 第485-517行 _serialize_order() 逻辑一致。
        用于订单详情展示和 P&L 计算。
        """
        try:
            leg = order_obj.legs[0] if order_obj.legs else None
            
            legs_data = []
            for l in (order_obj.legs or []):
                fills_data = []
                for f in (getattr(l, "fills", []) or []):
                    fills_data.append({
                        "fill_price": str(getattr(f, "fill_price", 0) or 0),
                        "quantity":   str(getattr(f, "quantity", 0) or 0),
                        "filled_at":  str(getattr(f, "filled_at", "") or ""),
                    })
                legs_data.append({
                    "symbol":   str(l.symbol),
                    "action":   str(l.action),
                    "quantity": str(l.quantity),
                    "fills":    fills_data,
                })

            return {
                "id":         str(order_obj.id),
                "symbol":     leg.symbol if leg else "\u2014",
                "action":     str(leg.action) if leg else "\u2014",
                "qty":        str(leg.quantity) if leg else "\u2014",
                "price":      f"{abs(float(order_obj.price)):.2f}" if order_obj.price else "MKT",
                "type":       str(order_obj.order_type).split(".")[-1] if order_obj.order_type else "\u2014",
                "tif":        str(order_obj.time_in_force).split(".")[-1] if hasattr(order_obj, "time_in_force") else "\u2014",
                "status":     str(order_obj.status).split(".")[-1] if order_obj.status else "\u2014",
                "status_message": str(getattr(order_obj, "reject_reason", "") or ""),
                "can_cancel": bool(getattr(order_obj, "cancellable", False)),
                "updated_at": str(getattr(order_obj, "updated_at", "") or ""),
                "legs":       legs_data,
            }
        except Exception as e:
            log.warning(f"serialize_order error: {e}")
            return {}

