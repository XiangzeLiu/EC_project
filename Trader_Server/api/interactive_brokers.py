"""Interactive Brokers adapter backed by the official TWS Python API."""

from __future__ import annotations

import asyncio
import logging
import math
import re
import threading
from datetime import datetime, timezone
from typing import Any, Callable

from .base import BaseBrokerAPI

log = logging.getLogger("trader_server.api.ib")


class IBRequestError(RuntimeError):
    def __init__(self, code: int | str, message: str):
        self.code = str(code)
        self.message = str(message or "Interactive Brokers request failed")
        super().__init__(f"IB {self.code}: {self.message}")


_IB_AVAILABLE = False
try:
    from ibapi.client import EClient
    from ibapi.contract import Contract
    from ibapi.execution import ExecutionFilter
    from ibapi.order import Order
    from ibapi.wrapper import EWrapper

    _IB_AVAILABLE = True
except ImportError:
    EClient = object  # type: ignore[assignment,misc]
    EWrapper = object  # type: ignore[assignment,misc]
    Contract = Any  # type: ignore[assignment,misc]
    Order = Any  # type: ignore[assignment,misc]
    ExecutionFilter = Any  # type: ignore[assignment,misc]
    log.warning("ibapi not installed, IBBroker will be unavailable")


_INFO_CODES = {2104, 2106, 2107, 2108, 2119, 2158}
_CONNECTION_CODES = {326, 502, 504, 1100, 1101, 1102, 1300}
_MARKET_DATA_CODES = {354, 10089, 10167, 10168}
_ACTION_TO_IB = {
    "Buy to Open": "BUY",
    "Buy to Close": "BUY",
    "Sell to Open": "SELL",
    "Sell to Close": "SELL",
}
_ACTION_TO_REF = {
    "Buy to Open": "BTO",
    "Buy to Close": "BTC",
    "Sell to Open": "STO",
    "Sell to Close": "STC",
}
_REF_TO_ACTION = {value: key for key, value in _ACTION_TO_REF.items()}
_LIVE_STATUSES = {"Received", "Routing", "Live", "Cancelling", "Partial"}
_ACCOUNT_SUMMARY_TAGS = ",".join(
    (
        "AccountType",
        "NetLiquidation",
        "TotalCashValue",
        "BuyingPower",
        "AvailableFunds",
        "ExcessLiquidity",
        "MaintMarginReq",
        "Currency",
    )
)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _format_quantity(value: Any) -> str:
    number = _to_float(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.8f}".rstrip("0").rstrip(".")


def _ib_time_to_iso(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).isoformat()
    except ValueError:
        pass

    match = re.match(r"^(\d{8})\s+(\d{2}:\d{2}:\d{2})(?:\s+(.+))?$", raw)
    if not match:
        return raw
    try:
        parsed = datetime.strptime(f"{match.group(1)} {match.group(2)}", "%Y%m%d %H:%M:%S")
        zone_name = (match.group(3) or "").strip()
        if zone_name:
            try:
                from zoneinfo import ZoneInfo

                parsed = parsed.replace(tzinfo=ZoneInfo(zone_name))
            except Exception:
                parsed = parsed.replace(tzinfo=timezone.utc)
        else:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.isoformat()
    except ValueError:
        return raw


def _normalize_status(status: Any, filled: Any = 0, remaining: Any = 0) -> str:
    raw = str(status or "").strip()
    filled_value = _to_float(filled)
    remaining_value = _to_float(remaining)
    if filled_value > 0 and remaining_value > 0:
        return "Partial"
    mapping = {
        "PendingSubmit": "Received",
        "ApiPending": "Received",
        "PreSubmitted": "Routing",
        "Submitted": "Live",
        "PendingCancel": "Cancelling",
        "ApiCancelled": "Cancelled",
        "Cancelled": "Cancelled",
        "Filled": "Filled",
        "Inactive": "Rejected",
    }
    return mapping.get(raw, raw or "Received")


def _tif_to_ib(value: str) -> tuple[str, bool]:
    mapping = {
        "Day": ("DAY", False),
        "GTC": ("GTC", False),
        "IOC": ("IOC", False),
        "EXT": ("DAY", True),
        "GTC_EXT": ("GTC", True),
    }
    try:
        return mapping[value]
    except KeyError as exc:
        raise ValueError(f"Unsupported IB time in force: {value}") from exc


def _tif_from_ib(tif: Any, outside_rth: Any) -> str:
    raw = str(tif or "DAY").strip().upper()
    outside = bool(outside_rth)
    if raw == "DAY" and outside:
        return "EXT"
    if raw == "GTC" and outside:
        return "GTC_EXT"
    if raw == "DAY":
        return "Day"
    return raw


def _action_from_order_ref(order_ref: Any) -> str:
    raw = str(order_ref or "").strip()
    if not raw.startswith("EC:"):
        return ""
    code = raw.split(":", 2)[1]
    return _REF_TO_ACTION.get(code, "")


if _IB_AVAILABLE:

    class _IBApp(EWrapper, EClient):
        def __init__(self, loop: asyncio.AbstractEventLoop, quote_queue: asyncio.Queue):
            EWrapper.__init__(self)
            EClient.__init__(self, self)
            self._loop = loop
            self._quote_queue = quote_queue
            self.ready_event = asyncio.Event()
            self.accounts_event = asyncio.Event()
            self.closed_event = asyncio.Event()
            self.managed_accounts: list[str] = []
            self.connection_error: IBRequestError | None = None
            self.last_error: dict[str, Any] = {}

            self._request_id = 1000
            self._next_order_id: int | None = None
            self._contract_waiters: dict[int, tuple[list[Any], asyncio.Future]] = {}
            self._summary_waiters: dict[int, tuple[dict[str, dict], asyncio.Future]] = {}
            self._execution_waiters: dict[int, tuple[list[dict], asyncio.Future]] = {}
            self._positions_waiter: tuple[list[dict], asyncio.Future] | None = None
            self._portfolio_waiter: tuple[str, dict[str, dict], asyncio.Future] | None = None
            self._open_orders_waiter: tuple[list[dict], asyncio.Future] | None = None
            self._completed_orders_waiter: tuple[list[dict], asyncio.Future] | None = None
            self._submit_waiters: dict[int, asyncio.Future] = {}
            self._cancel_waiters: dict[int, asyncio.Future] = {}
            self._quote_waiters: dict[int, asyncio.Future] = {}
            self._symbol_req_id: dict[str, int] = {}
            self._req_id_symbol: dict[int, str] = {}
            self._quotes: dict[str, dict] = {}
            self.known_orders: dict[int, dict] = {}
            self.order_updates: dict[int, str] = {}

        def _soon(self, callback: Callable, *args: Any) -> None:
            self._loop.call_soon_threadsafe(callback, *args)

        @staticmethod
        def _resolve(future: asyncio.Future | None, value: Any = None, error: Exception | None = None) -> None:
            if not future or future.done():
                return
            if error:
                future.set_exception(error)
            else:
                future.set_result(value)

        def next_request_id(self) -> int:
            self._request_id += 1
            return self._request_id

        def allocate_order_id(self) -> int:
            if self._next_order_id is None:
                raise RuntimeError("IB nextValidId has not been received")
            order_id = self._next_order_id
            self._next_order_id += 1
            return order_id

        # IB callbacks run on the API reader thread. State changes are marshalled
        # back to the asyncio loop so request dictionaries do not need locks.
        def nextValidId(self, orderId: int) -> None:
            self._soon(self._on_next_valid_id, int(orderId))

        def _on_next_valid_id(self, order_id: int) -> None:
            if self._next_order_id is None or order_id > self._next_order_id:
                self._next_order_id = order_id
            self.ready_event.set()

        def managedAccounts(self, accountsList: str) -> None:
            accounts = [item.strip() for item in str(accountsList or "").split(",") if item.strip()]
            self._soon(self._on_managed_accounts, accounts)

        def _on_managed_accounts(self, accounts: list[str]) -> None:
            self.managed_accounts = list(dict.fromkeys(accounts))
            self.accounts_event.set()

        def connectionClosed(self) -> None:
            self._soon(self._on_connection_closed)

        def _on_connection_closed(self) -> None:
            self.closed_event.set()
            error = IBRequestError("IB_CONNECTION_CLOSED", "IB Gateway connection closed")
            for _, future in self._contract_waiters.values():
                self._resolve(future, error=error)
            for _, future in self._summary_waiters.values():
                self._resolve(future, error=error)
            for _, future in self._execution_waiters.values():
                self._resolve(future, error=error)
            if self._positions_waiter:
                self._resolve(self._positions_waiter[1], error=error)
            if self._portfolio_waiter:
                self._resolve(self._portfolio_waiter[2], error=error)
            if self._open_orders_waiter:
                self._resolve(self._open_orders_waiter[1], error=error)
            if self._completed_orders_waiter:
                self._resolve(self._completed_orders_waiter[1], error=error)
            for future in self._submit_waiters.values():
                self._resolve(future, error=error)
            for future in self._cancel_waiters.values():
                self._resolve(future, error=error)

        def error(
            self,
            reqId: int,
            errorCode: int,
            errorString: str,
            advancedOrderRejectJson: str = "",
        ) -> None:
            self._soon(self._on_error, int(reqId), int(errorCode), str(errorString or ""))

        def _on_error(self, req_id: int, code: int, message: str) -> None:
            if code in _INFO_CODES:
                return
            if code == 202 and req_id in self._cancel_waiters:
                self._resolve(
                    self._cancel_waiters.get(req_id),
                    {"order_id": req_id, "status": "Cancelled"},
                )
                return
            error = IBRequestError(code, message)
            self.last_error = {"req_id": req_id, "code": code, "message": message, "at": _iso_now()}
            log.warning("IB error reqId=%s code=%s: %s", req_id, code, message)
            if code in _CONNECTION_CODES:
                self.connection_error = error
            if req_id in self._contract_waiters:
                self._resolve(self._contract_waiters[req_id][1], error=error)
            if req_id in self._summary_waiters:
                self._resolve(self._summary_waiters[req_id][1], error=error)
            if req_id in self._execution_waiters:
                self._resolve(self._execution_waiters[req_id][1], error=error)
            if req_id in self._quote_waiters:
                self._resolve(self._quote_waiters[req_id], error=error)
            if req_id in self._submit_waiters:
                self._resolve(self._submit_waiters[req_id], error=error)
            if req_id in self._cancel_waiters:
                self._resolve(self._cancel_waiters[req_id], error=error)

        def contractDetails(self, reqId: int, contractDetails: Any) -> None:
            self._soon(self._on_contract_details, int(reqId), contractDetails)

        def _on_contract_details(self, req_id: int, details: Any) -> None:
            waiter = self._contract_waiters.get(req_id)
            if waiter:
                waiter[0].append(details)

        def contractDetailsEnd(self, reqId: int) -> None:
            self._soon(self._on_contract_details_end, int(reqId))

        def _on_contract_details_end(self, req_id: int) -> None:
            waiter = self._contract_waiters.get(req_id)
            if waiter:
                self._resolve(waiter[1], list(waiter[0]))

        async def request_contract_details(self, contract: Any, timeout: float = 8.0) -> list[Any]:
            req_id = self.next_request_id()
            future = self._loop.create_future()
            self._contract_waiters[req_id] = ([], future)
            self.reqContractDetails(req_id, contract)
            try:
                return await asyncio.wait_for(future, timeout=timeout)
            finally:
                self._contract_waiters.pop(req_id, None)

        def accountSummary(self, reqId: int, account: str, tag: str, value: str, currency: str) -> None:
            self._soon(self._on_account_summary, int(reqId), str(account), str(tag), str(value), str(currency))

        def _on_account_summary(self, req_id: int, account: str, tag: str, value: str, currency: str) -> None:
            waiter = self._summary_waiters.get(req_id)
            if not waiter:
                return
            account_data = waiter[0].setdefault(account, {})
            account_data[tag] = {"value": value, "currency": currency}

        def accountSummaryEnd(self, reqId: int) -> None:
            self._soon(self._on_account_summary_end, int(reqId))

        def _on_account_summary_end(self, req_id: int) -> None:
            waiter = self._summary_waiters.get(req_id)
            if waiter:
                self._resolve(waiter[1], dict(waiter[0]))

        async def request_account_summary(self, timeout: float = 8.0) -> dict[str, dict]:
            req_id = self.next_request_id()
            future = self._loop.create_future()
            self._summary_waiters[req_id] = ({}, future)
            self.reqAccountSummary(req_id, "All", _ACCOUNT_SUMMARY_TAGS)
            try:
                return await asyncio.wait_for(future, timeout=timeout)
            finally:
                try:
                    self.cancelAccountSummary(req_id)
                except Exception:
                    pass
                self._summary_waiters.pop(req_id, None)

        def position(self, account: str, contract: Any, position: Any, avgCost: float) -> None:
            self._soon(
                self._on_position,
                {"account": str(account), "contract": contract, "position": position, "average_cost": avgCost},
            )

        def _on_position(self, item: dict) -> None:
            if self._positions_waiter:
                self._positions_waiter[0].append(item)

        def positionEnd(self) -> None:
            self._soon(self._on_position_end)

        def _on_position_end(self) -> None:
            if self._positions_waiter:
                self._resolve(self._positions_waiter[1], list(self._positions_waiter[0]))

        async def request_positions(self, timeout: float = 10.0) -> list[dict]:
            if self._positions_waiter and not self._positions_waiter[1].done():
                raise RuntimeError("IB position request already in progress")
            future = self._loop.create_future()
            self._positions_waiter = ([], future)
            self.reqPositions()
            try:
                return await asyncio.wait_for(future, timeout=timeout)
            finally:
                try:
                    self.cancelPositions()
                except Exception:
                    pass
                self._positions_waiter = None

        def updatePortfolio(
            self,
            contract: Any,
            position: Any,
            marketPrice: float,
            marketValue: float,
            averageCost: float,
            unrealizedPNL: float,
            realizedPNL: float,
            accountName: str,
        ) -> None:
            self._soon(
                self._on_portfolio,
                str(accountName),
                contract,
                position,
                marketPrice,
                marketValue,
                averageCost,
                unrealizedPNL,
                realizedPNL,
            )

        def _on_portfolio(
            self,
            account: str,
            contract: Any,
            position: Any,
            market_price: float,
            market_value: float,
            average_cost: float,
            unrealized_pnl: float,
            realized_pnl: float,
        ) -> None:
            waiter = self._portfolio_waiter
            if not waiter or waiter[0] != account:
                return
            key = str(getattr(contract, "conId", 0) or getattr(contract, "symbol", ""))
            waiter[1][key] = {
                "account": account,
                "contract": contract,
                "position": position,
                "market_price": market_price,
                "market_value": market_value,
                "average_cost": average_cost,
                "unrealized_pnl": unrealized_pnl,
                "realized_pnl": realized_pnl,
            }

        def accountDownloadEnd(self, accountName: str) -> None:
            self._soon(self._on_account_download_end, str(accountName))

        def _on_account_download_end(self, account: str) -> None:
            waiter = self._portfolio_waiter
            if waiter and waiter[0] == account:
                self._resolve(waiter[2], dict(waiter[1]))

        async def request_portfolio(self, account: str, timeout: float = 10.0) -> dict[str, dict]:
            if self._portfolio_waiter and not self._portfolio_waiter[2].done():
                raise RuntimeError("IB portfolio request already in progress")
            future = self._loop.create_future()
            self._portfolio_waiter = (account, {}, future)
            self.reqAccountUpdates(True, account)
            try:
                return await asyncio.wait_for(future, timeout=timeout)
            finally:
                try:
                    self.reqAccountUpdates(False, account)
                except Exception:
                    pass
                self._portfolio_waiter = None

        def tickPrice(self, reqId: int, tickType: int, price: float, attrib: Any = None) -> None:
            self._soon(self._on_tick_price, int(reqId), int(tickType), float(price))

        def _on_tick_price(self, req_id: int, tick_type: int, price: float) -> None:
            symbol = self._req_id_symbol.get(req_id)
            if not symbol or price <= 0:
                return
            quote = self._quotes.setdefault(symbol, {"bid": 0.0, "ask": 0.0, "last": 0.0, "volume": 0})
            if tick_type == 1:
                quote["bid"] = price
            elif tick_type == 2:
                quote["ask"] = price
            elif tick_type in {4, 9}:
                quote["last"] = price
            else:
                return
            self._resolve(self._quote_waiters.get(req_id), True)
            payload = {"symbol": symbol, **quote, "ts": _iso_now()}
            self._quote_queue.put_nowait(payload)

        def tickSize(self, reqId: int, tickType: int, size: Any) -> None:
            self._soon(self._on_tick_size, int(reqId), int(tickType), size)

        def _on_tick_size(self, req_id: int, tick_type: int, size: Any) -> None:
            symbol = self._req_id_symbol.get(req_id)
            if not symbol or tick_type != 8:
                return
            quote = self._quotes.setdefault(symbol, {"bid": 0.0, "ask": 0.0, "last": 0.0, "volume": 0})
            quote["volume"] = int(_to_float(size))

        async def subscribe_market_data(self, symbol: str, contract: Any, timeout: float = 2.0) -> None:
            if symbol in self._symbol_req_id:
                return
            req_id = self.next_request_id()
            future = self._loop.create_future()
            self._quote_waiters[req_id] = future
            self._symbol_req_id[symbol] = req_id
            self._req_id_symbol[req_id] = symbol
            self.reqMktData(req_id, contract, "", False, False, [])
            try:
                await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
            except asyncio.TimeoutError:
                # A quiet market is not a subscription failure. Immediate IB
                # permission/contract errors are delivered before this timeout.
                pass
            except Exception:
                self.unsubscribe_market_data(symbol)
                raise
            finally:
                self._quote_waiters.pop(req_id, None)

        def unsubscribe_market_data(self, symbol: str) -> None:
            req_id = self._symbol_req_id.pop(symbol, None)
            if req_id is None:
                return
            self._req_id_symbol.pop(req_id, None)
            self._quote_waiters.pop(req_id, None)
            self._quotes.pop(symbol, None)
            try:
                self.cancelMktData(req_id)
            except Exception:
                pass

        def openOrder(self, orderId: int, contract: Any, order: Any, orderState: Any) -> None:
            self._soon(self._on_open_order, int(orderId), contract, order, orderState)

        def _on_open_order(self, order_id: int, contract: Any, order: Any, order_state: Any) -> None:
            item = {
                "order_id": order_id,
                "contract": contract,
                "order": order,
                "order_state": order_state,
                "status": str(getattr(order_state, "status", "") or ""),
                "filled": 0,
                "remaining": getattr(order, "totalQuantity", 0),
                "updated_at": self.order_updates.get(order_id) or _iso_now(),
            }
            self.known_orders[order_id] = item
            self.order_updates[order_id] = item["updated_at"]
            if self._open_orders_waiter:
                self._open_orders_waiter[0].append(item)

        def openOrderEnd(self) -> None:
            self._soon(self._on_open_order_end)

        def _on_open_order_end(self) -> None:
            if self._open_orders_waiter:
                self._resolve(self._open_orders_waiter[1], list(self._open_orders_waiter[0]))

        def orderStatus(
            self,
            orderId: int,
            status: str,
            filled: Any,
            remaining: Any,
            avgFillPrice: float,
            permId: int,
            parentId: int,
            lastFillPrice: float,
            clientId: int,
            whyHeld: str,
            mktCapPrice: float,
        ) -> None:
            self._soon(
                self._on_order_status,
                int(orderId),
                str(status),
                filled,
                remaining,
                avgFillPrice,
                int(permId),
                lastFillPrice,
                int(clientId),
            )

        def _on_order_status(
            self,
            order_id: int,
            status: str,
            filled: Any,
            remaining: Any,
            avg_fill_price: float,
            perm_id: int,
            last_fill_price: float,
            client_id: int,
        ) -> None:
            item = self.known_orders.setdefault(order_id, {"order_id": order_id})
            item.update(
                {
                    "status": status,
                    "filled": filled,
                    "remaining": remaining,
                    "avg_fill_price": avg_fill_price,
                    "perm_id": perm_id,
                    "last_fill_price": last_fill_price,
                    "client_id": client_id,
                    "updated_at": _iso_now(),
                }
            )
            self.order_updates[order_id] = item["updated_at"]
            normalized = _normalize_status(status, filled, remaining)
            if normalized in {"Routing", "Live", "Partial", "Filled", "Cancelled", "Rejected"}:
                if normalized == "Rejected":
                    self._resolve(self._submit_waiters.get(order_id), error=IBRequestError("ORDER_REJECTED", status))
                else:
                    self._resolve(
                        self._submit_waiters.get(order_id),
                        {"order_id": order_id, "status": normalized},
                    )
            if normalized == "Cancelled":
                self._resolve(self._cancel_waiters.get(order_id), {"order_id": order_id, "status": normalized})

        def completedOrder(self, contract: Any, order: Any, orderState: Any) -> None:
            self._soon(self._on_completed_order, contract, order, orderState)

        def _on_completed_order(self, contract: Any, order: Any, order_state: Any) -> None:
            item = {
                "order_id": int(getattr(order, "orderId", 0) or 0),
                "contract": contract,
                "order": order,
                "order_state": order_state,
                "status": str(
                    getattr(order_state, "completedStatus", "")
                    or getattr(order_state, "status", "")
                    or "Filled"
                ),
                "filled": getattr(order, "filledQuantity", 0),
                "remaining": 0,
                "perm_id": int(getattr(order, "permId", 0) or 0),
                "updated_at": _ib_time_to_iso(getattr(order_state, "completedTime", "")) or _iso_now(),
            }
            if item["order_id"]:
                self.known_orders[item["order_id"]] = item
            if self._completed_orders_waiter:
                self._completed_orders_waiter[0].append(item)

        def completedOrdersEnd(self) -> None:
            self._soon(self._on_completed_orders_end)

        def _on_completed_orders_end(self) -> None:
            if self._completed_orders_waiter:
                self._resolve(self._completed_orders_waiter[1], list(self._completed_orders_waiter[0]))

        def execDetails(self, reqId: int, contract: Any, execution: Any) -> None:
            self._soon(self._on_exec_details, int(reqId), contract, execution)

        def _on_exec_details(self, req_id: int, contract: Any, execution: Any) -> None:
            waiter = self._execution_waiters.get(req_id)
            if not waiter:
                return
            waiter[0].append({"contract": contract, "execution": execution})

        def execDetailsEnd(self, reqId: int) -> None:
            self._soon(self._on_exec_details_end, int(reqId))

        def _on_exec_details_end(self, req_id: int) -> None:
            waiter = self._execution_waiters.get(req_id)
            if waiter:
                self._resolve(waiter[1], list(waiter[0]))

        async def request_open_orders(self, timeout: float = 8.0) -> list[dict]:
            if self._open_orders_waiter and not self._open_orders_waiter[1].done():
                raise RuntimeError("IB open-order request already in progress")
            future = self._loop.create_future()
            self._open_orders_waiter = ([], future)
            self.reqOpenOrders()
            try:
                return await asyncio.wait_for(future, timeout=timeout)
            finally:
                self._open_orders_waiter = None

        async def request_completed_orders(self, timeout: float = 8.0) -> list[dict]:
            if self._completed_orders_waiter and not self._completed_orders_waiter[1].done():
                raise RuntimeError("IB completed-order request already in progress")
            future = self._loop.create_future()
            self._completed_orders_waiter = ([], future)
            self.reqCompletedOrders(True)
            try:
                return await asyncio.wait_for(future, timeout=timeout)
            finally:
                self._completed_orders_waiter = None

        async def request_executions(self, account: str, timeout: float = 8.0) -> list[dict]:
            req_id = self.next_request_id()
            future = self._loop.create_future()
            self._execution_waiters[req_id] = ([], future)
            filter_obj = ExecutionFilter()
            filter_obj.acctCode = account
            filter_obj.secType = "STK"
            self.reqExecutions(req_id, filter_obj)
            try:
                return await asyncio.wait_for(future, timeout=timeout)
            finally:
                self._execution_waiters.pop(req_id, None)

        async def place_order_and_wait(
            self,
            order_id: int,
            contract: Any,
            order: Any,
            timeout: float = 12.0,
        ) -> dict:
            future = self._loop.create_future()
            self._submit_waiters[order_id] = future
            self.order_updates[order_id] = _iso_now()
            self.placeOrder(order_id, contract, order)
            try:
                return await asyncio.wait_for(future, timeout=timeout)
            finally:
                self._submit_waiters.pop(order_id, None)

        async def cancel_order_and_wait(self, order_id: int, timeout: float = 10.0) -> dict:
            future = self._loop.create_future()
            self._cancel_waiters[order_id] = future
            try:
                try:
                    self.cancelOrder(order_id, "")
                except TypeError:
                    self.cancelOrder(order_id)
                return await asyncio.wait_for(future, timeout=timeout)
            finally:
                self._cancel_waiters.pop(order_id, None)


class IBBroker(BaseBrokerAPI):
    """One-account US-stock adapter for an IB Gateway on the TS host."""

    @classmethod
    def credential_profiles(cls) -> list[tuple[str, ...]]:
        return [("host", "port", "client_id")]

    @classmethod
    def capabilities(cls) -> dict[str, bool]:
        return {
            "quotes": True,
            "orders": True,
            "cancel_order": True,
            "positions": True,
            "order_query": True,
        }

    def __init__(self):
        super().__init__(broker_type="interactive_brokers")
        self._ib_app: Any | None = None
        self._ib_thread: threading.Thread | None = None
        self._quote_queue: asyncio.Queue | None = None
        self._quote_task: asyncio.Task | None = None
        self._host = "127.0.0.1"
        self._port = 4001
        self._client_id = 1
        self._account_id = ""
        self._managed_accounts: list[str] = []
        self._account_verified = False
        self._contract_cache: dict[str, Any] = {}
        self._positions_lock = asyncio.Lock()
        self._orders_lock = asyncio.Lock()
        self._order_id_lock = asyncio.Lock()

    def normalize_credentials(self, credentials: dict | None) -> dict:
        data = super().normalize_credentials(credentials)
        data["host"] = str(data.get("host") or "127.0.0.1").strip()
        try:
            data["port"] = int(data.get("port", 4001))
        except (TypeError, ValueError):
            data["port"] = 0
        try:
            data["client_id"] = int(data.get("client_id", 1))
        except (TypeError, ValueError):
            data["client_id"] = -1
        data["account_id"] = str(data.get("account_id") or "").strip()
        return data

    @staticmethod
    def _validate_gateway_config(credentials: dict) -> tuple[bool, str]:
        if credentials.get("host") != "127.0.0.1":
            return False, "IB Gateway host must be 127.0.0.1"
        if credentials.get("port") != 4001:
            return False, "IB Gateway port must be 4001"
        if credentials.get("client_id") != 1:
            return False, "IB Gateway client_id must be 1"
        return True, ""

    async def connect(self, credentials: dict) -> bool:
        await self.disconnect()
        self.clear_connection_error()
        normalized = self.normalize_credentials(credentials)
        valid, reason = self._validate_gateway_config(normalized)
        if not valid:
            self.set_connection_error("IB_CONFIG_INVALID", reason, retryable=False)
            return False
        if not _IB_AVAILABLE:
            self.set_connection_error("IB_SDK_MISSING", "ibapi is not installed on Trader Server", retryable=False)
            return False

        self._credentials = dict(normalized)
        self._host = normalized["host"]
        self._port = normalized["port"]
        self._client_id = normalized["client_id"]
        self._account_id = normalized["account_id"]
        self._managed_accounts = []
        self._account_verified = False
        self._contract_cache.clear()

        loop = asyncio.get_running_loop()
        self._quote_queue = asyncio.Queue()
        app = _IBApp(loop, self._quote_queue)
        self._ib_app = app

        try:
            await asyncio.to_thread(app.connect, self._host, self._port, self._client_id)
            if not app.isConnected():
                raise IBRequestError("IB_GATEWAY_UNREACHABLE", "IB Gateway socket is not available")
            self._ib_thread = threading.Thread(target=app.run, name="ibapi-reader", daemon=True)
            self._ib_thread.start()
            await asyncio.wait_for(app.ready_event.wait(), timeout=10.0)
            try:
                app.reqManagedAccts()
            except Exception:
                pass
            await asyncio.wait_for(app.accounts_event.wait(), timeout=8.0)
            self._managed_accounts = list(app.managed_accounts)
            if not self._managed_accounts:
                raise IBRequestError("IB_NO_MANAGED_ACCOUNTS", "IB Gateway returned no managed accounts")
            if self._account_id and self._account_id not in self._managed_accounts:
                raise IBRequestError(
                    "IB_ACCOUNT_NOT_MANAGED",
                    f"Configured account is not managed by this IB Gateway: {self._account_id}",
                )

            self._account_verified = bool(self._account_id)
            self._connected = True
            self._start_quote_forwarder()
            log.info(
                "IB connected host=%s port=%s client_id=%s accounts=%s selected=%s",
                self._host,
                self._port,
                self._client_id,
                len(self._managed_accounts),
                self._account_id or "(validation-only)",
            )
            return True
        except asyncio.TimeoutError:
            detail = app.connection_error.message if app.connection_error else "IB API handshake timed out"
            code = "IB_CLIENT_ID_IN_USE" if app.connection_error and app.connection_error.code == "326" else "IB_API_HANDSHAKE_TIMEOUT"
            self.set_connection_error(code, detail, retryable=True)
        except IBRequestError as exc:
            retryable = exc.code not in {"IB_ACCOUNT_NOT_MANAGED", "IB_CONFIG_INVALID"}
            self.set_connection_error(exc.code, exc.message, retryable=retryable)
        except Exception as exc:
            message = str(exc) or "Unable to connect to IB Gateway"
            code = "IB_CLIENT_ID_IN_USE" if "client id" in message.lower() else "IB_GATEWAY_UNREACHABLE"
            self.set_connection_error(code, message, retryable=True)

        await self.disconnect()
        return False

    async def disconnect(self) -> None:
        quote_task = self._quote_task
        self._quote_task = None
        if quote_task and not quote_task.done():
            quote_task.cancel()
            try:
                await quote_task
            except (asyncio.CancelledError, Exception):
                pass

        app = self._ib_app
        thread = self._ib_thread
        self._ib_app = None
        self._ib_thread = None
        if app:
            for symbol in list(getattr(app, "_symbol_req_id", {}).keys()):
                app.unsubscribe_market_data(symbol)
            try:
                app.disconnect()
            except Exception:
                pass
        if thread and thread.is_alive() and thread is not threading.current_thread():
            await asyncio.to_thread(thread.join, 2.0)

        self._connected = False
        self._account_verified = False
        self._managed_accounts = []
        self._contract_cache.clear()

    async def is_connected(self) -> bool:
        app = self._ib_app
        connected = bool(self._connected and app and app.isConnected() and not app.closed_event.is_set())
        if not connected:
            self._connected = False
        return connected

    def effective_capabilities(self) -> dict[str, bool]:
        capabilities = dict(self.capabilities())
        account_ready = bool(self._account_id and self._account_verified)
        if not account_ready:
            capabilities["orders"] = False
            capabilities["cancel_order"] = False
            capabilities["positions"] = False
            capabilities["order_query"] = False
        return capabilities

    def status_detail(self) -> dict[str, Any]:
        return {
            "gateway": {"host": self._host, "port": self._port, "client_id": self._client_id},
            "account": {
                "account_id": self._account_id,
                "configured": bool(self._account_id),
                "managed": bool(self._account_verified),
            },
            "managed_account_count": len(self._managed_accounts),
        }

    def _require_app(self, require_account: bool = False) -> Any:
        app = self._ib_app
        if not self._connected or not app or not app.isConnected():
            raise RuntimeError("Unable to connect to IB Gateway")
        if require_account and not self._account_verified:
            raise RuntimeError("IB account is not selected or managed by this Gateway")
        return app

    async def get_accounts(self) -> list[dict]:
        app = self._require_app(require_account=False)
        accounts = list(app.managed_accounts or self._managed_accounts)
        return [{"account_id": account_id} for account_id in accounts]

    async def get_account_summary(self, account_id: str = "") -> dict:
        app = self._require_app(require_account=False)
        selected = str(account_id or self._account_id or "").strip()
        if not selected:
            raise ValueError("account_id is required")
        if selected not in self._managed_accounts:
            raise ValueError("account_id is not managed by this IB Gateway")
        summaries = await app.request_account_summary()
        return {"account_id": selected, "values": summaries.get(selected, {})}

    @staticmethod
    def _stock_contract(symbol: str) -> Any:
        if not _IB_AVAILABLE:
            raise RuntimeError("ibapi is not installed")
        contract = Contract()
        contract.symbol = symbol
        contract.secType = "STK"
        contract.exchange = "SMART"
        contract.currency = "USD"
        return contract

    async def _resolve_stock_contract(self, symbol: str) -> Any:
        app = self._require_app(require_account=False)
        normalized = str(symbol or "").strip().upper()
        if not normalized:
            raise ValueError("symbol is required")
        if normalized in self._contract_cache:
            return self._contract_cache[normalized]
        details = await app.request_contract_details(self._stock_contract(normalized))
        stock_details = [
            item
            for item in details
            if str(getattr(getattr(item, "contract", None), "secType", "")).upper() == "STK"
            and str(getattr(getattr(item, "contract", None), "currency", "")).upper() == "USD"
        ]
        if not stock_details:
            raise IBRequestError("IB_CONTRACT_NOT_FOUND", f"US stock contract not found: {normalized}")
        unique = {}
        for item in stock_details:
            contract = item.contract
            key = int(getattr(contract, "conId", 0) or 0) or repr(contract)
            unique[key] = contract
        if len(unique) != 1:
            raise IBRequestError("IB_CONTRACT_AMBIGUOUS", f"US stock symbol is ambiguous: {normalized}")
        contract = next(iter(unique.values()))
        self._contract_cache[normalized] = contract
        return contract

    async def subscribe_quotes(self, symbols: list[str]) -> None:
        app = self._require_app(require_account=False)
        for symbol in symbols or []:
            normalized = str(symbol or "").strip().upper()
            if not normalized:
                continue
            contract = await self._resolve_stock_contract(normalized)
            await app.subscribe_market_data(normalized, contract)

    async def unsubscribe_quotes(self, symbols: list[str]) -> None:
        app = self._ib_app
        if not app:
            return
        for symbol in symbols or []:
            normalized = str(symbol or "").strip().upper()
            if normalized:
                app.unsubscribe_market_data(normalized)

    async def get_positions(self, filters: dict | None = None) -> list[dict]:
        app = self._require_app(require_account=True)
        async with self._positions_lock:
            positions = await app.request_positions()
            try:
                portfolio = await app.request_portfolio(self._account_id)
            except Exception as exc:
                log.warning("IB portfolio enrichment unavailable: %s", exc)
                portfolio = {}

        portfolio_by_symbol = {
            str(getattr(item.get("contract"), "symbol", "")).upper(): item
            for item in portfolio.values()
            if item.get("contract") is not None
        }
        result = []
        for item in positions:
            contract = item["contract"]
            if item["account"] != self._account_id:
                continue
            if str(getattr(contract, "secType", "")).upper() != "STK":
                continue
            if str(getattr(contract, "currency", "")).upper() != "USD":
                continue
            symbol = str(getattr(contract, "symbol", "")).upper()
            quantity = _to_float(item["position"])
            enriched = portfolio_by_symbol.get(symbol, {})
            average_cost = _to_float(enriched.get("average_cost"), _to_float(item["average_cost"]))
            result.append(
                {
                    "symbol": symbol,
                    "quantity": quantity,
                    "direction": "Long" if quantity >= 0 else "Short",
                    "average_open_price": average_cost,
                    "close_price": _to_float(enriched.get("market_price")),
                    "unrealized": _to_float(enriched.get("unrealized_pnl")),
                    "realized_today": _to_float(enriched.get("realized_pnl")),
                }
            )

        if filters and filters.get("symbols"):
            allowed = {str(symbol).strip().upper() for symbol in filters["symbols"]}
            result = [item for item in result if item["symbol"] in allowed]
        return result

    async def place_order(self, order_params: dict) -> dict:
        app = self._require_app(require_account=True)
        symbol = str(order_params.get("symbol") or "").strip().upper()
        action_label = str(order_params.get("action") or "").strip()
        order_type = str(order_params.get("order_type") or "limit").strip().lower()
        tif_label = str(order_params.get("tif") or "Day").strip()
        quantity = int(order_params.get("qty") or 0)
        price = _to_float(order_params.get("price"))
        if action_label not in _ACTION_TO_IB:
            raise ValueError(f"Unsupported IB order action: {action_label}")
        if order_type not in {"limit", "market"}:
            raise ValueError(f"Unsupported IB order type: {order_type}")
        if quantity <= 0:
            raise ValueError("Order quantity must be greater than zero")

        contract = await self._resolve_stock_contract(symbol)
        ib_tif, outside_rth = _tif_to_ib(tif_label)
        async with self._order_id_lock:
            order_id = app.allocate_order_id()

        order = Order()
        order.action = _ACTION_TO_IB[action_label]
        order.totalQuantity = quantity
        order.orderType = "LMT" if order_type == "limit" else "MKT"
        if order.orderType == "LMT":
            order.lmtPrice = price
        order.tif = ib_tif
        order.outsideRth = outside_rth
        order.account = self._account_id
        order.orderRef = f"EC:{_ACTION_TO_REF[action_label]}"
        order.transmit = True
        result = await app.place_order_and_wait(order_id, contract, order)
        return {"success": True, "order_id": str(order_id), "status": result.get("status", "Live")}

    async def cancel_order(self, order_id: str) -> dict:
        app = self._require_app(require_account=True)
        try:
            numeric_id = int(str(order_id).strip())
        except ValueError as exc:
            raise ValueError("IB order_id must be numeric") from exc

        item = app.known_orders.get(numeric_id)
        if not self._is_owned_order(item):
            async with self._orders_lock:
                await app.request_open_orders()
            item = app.known_orders.get(numeric_id)
        if not self._is_owned_order(item):
            raise PermissionError("Order is not owned by this Trader Server account connection")
        result = await app.cancel_order_and_wait(numeric_id)
        return {"success": True, "order_id": str(numeric_id), "status": result.get("status", "Cancelled")}

    def _is_owned_order(self, item: dict | None) -> bool:
        if not item:
            return False
        order = item.get("order")
        if not order:
            return False
        return bool(
            str(getattr(order, "account", "") or "") == self._account_id
            and str(getattr(order, "orderRef", "") or "").startswith("EC:")
        )

    async def get_orders(self, mode: str = "live") -> list[dict]:
        app = self._require_app(require_account=True)
        mode = "all" if str(mode or "").lower() == "all" else "live"
        async with self._orders_lock:
            open_orders = await app.request_open_orders()
            completed_orders: list[dict] = []
            executions: list[dict] = []
            if mode == "all":
                completed_orders = await app.request_completed_orders()
                executions = await app.request_executions(self._account_id)

        execution_map: dict[tuple[str, int], list[dict]] = {}
        for item in executions:
            execution = item["execution"]
            account = str(getattr(execution, "acctNumber", "") or "")
            if account != self._account_id:
                continue
            order_key = int(getattr(execution, "orderId", 0) or 0)
            perm_key = int(getattr(execution, "permId", 0) or 0)
            fill = {
                "fill_price": str(_to_float(getattr(execution, "price", 0))),
                "quantity": _format_quantity(getattr(execution, "shares", 0)),
                "filled_at": _ib_time_to_iso(getattr(execution, "time", "")),
            }
            if order_key:
                execution_map.setdefault(("order", order_key), []).append(fill)
            if perm_key:
                execution_map.setdefault(("perm", perm_key), []).append(fill)

        merged: dict[tuple[str, int], dict] = {}
        for item in open_orders + completed_orders:
            if not self._is_owned_order(item):
                continue
            contract = item.get("contract")
            if str(getattr(contract, "secType", "")).upper() != "STK":
                continue
            order_id = int(item.get("order_id") or 0)
            perm_id = int(item.get("perm_id") or getattr(item.get("order"), "permId", 0) or 0)
            key = ("perm", perm_id) if perm_id else ("order", order_id)
            merged[key] = item

        result = []
        for key, item in merged.items():
            serialized = self._serialize_order(item, execution_map)
            if not serialized:
                continue
            if mode == "live" and serialized["status"] not in _LIVE_STATUSES:
                continue
            result.append(serialized)
        result.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
        return result

    def _serialize_order(self, item: dict, execution_map: dict[tuple[str, int], list[dict]]) -> dict:
        order = item.get("order")
        contract = item.get("contract")
        if not order or not contract:
            return {}
        action = _action_from_order_ref(getattr(order, "orderRef", ""))
        if not action:
            return {}
        order_id = int(item.get("order_id") or getattr(order, "orderId", 0) or 0)
        perm_id = int(item.get("perm_id") or getattr(order, "permId", 0) or 0)
        fills = execution_map.get(("order", order_id), [])
        if not fills and perm_id:
            fills = execution_map.get(("perm", perm_id), [])
        filled = item.get("filled", 0)
        remaining = item.get("remaining", 0)
        if fills and not _to_float(filled):
            filled = sum(_to_float(fill.get("quantity")) for fill in fills)
            remaining = max(0.0, _to_float(getattr(order, "totalQuantity", 0)) - _to_float(filled))
        status = _normalize_status(item.get("status"), filled, remaining)
        order_type = str(getattr(order, "orderType", "") or "").upper()
        price = "MKT"
        if order_type == "LMT":
            price = f"{_to_float(getattr(order, 'lmtPrice', 0)):.2f}"
        symbol = str(getattr(contract, "symbol", "") or "").upper()
        quantity = _format_quantity(getattr(order, "totalQuantity", 0))
        return {
            "id": str(order_id),
            "symbol": symbol,
            "action": action,
            "qty": quantity,
            "price": price,
            "type": "LIMIT" if order_type == "LMT" else "MARKET",
            "tif": _tif_from_ib(getattr(order, "tif", "DAY"), getattr(order, "outsideRth", False)),
            "status": status,
            "updated_at": str(item.get("updated_at") or _iso_now()),
            "legs": [
                {
                    "symbol": symbol,
                    "action": action,
                    "quantity": quantity,
                    "fills": fills,
                }
            ],
        }

    def _start_quote_forwarder(self) -> None:
        if self._quote_task and not self._quote_task.done():
            return

        async def forward() -> None:
            queue = self._quote_queue
            while queue is not None and self._ib_app is not None:
                quote = await queue.get()
                self._on_quote_data(quote)

        self._quote_task = asyncio.create_task(forward(), name="ib-quote-forwarder")


__all__ = [
    "IBBroker",
    "IBRequestError",
    "_action_from_order_ref",
    "_normalize_status",
    "_tif_from_ib",
    "_tif_to_ib",
]
