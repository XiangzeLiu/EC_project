"""Read-only IB Gateway route discovery utility.

This tool requests ContractDetails for US stocks and prints the
validExchanges values returned by the official IB API. It does not request
account data or submit orders.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ibapi.client import EClient
from ibapi.contract import Contract
from ibapi.wrapper import EWrapper


_INFO_CODES = {2104, 2106, 2107, 2108, 2119, 2158}


def _normalize_error_args(args: tuple[Any, ...]) -> tuple[int, str]:
    if len(args) >= 3 and isinstance(args[1], int) and not isinstance(args[1], bool):
        _, code, message = args[:3]
    elif len(args) >= 2:
        code, message = args[:2]
    else:
        return 0, "Incomplete IB error callback"
    try:
        return int(code), str(message or "")
    except (TypeError, ValueError):
        return 0, str(message or "Invalid IB error callback")


def _split_routes(value: Any) -> list[str]:
    routes: list[str] = []
    for item in str(value or "").split(","):
        route = item.strip().upper()
        if route and route not in routes:
            routes.append(route)
    return routes


def _normalize_symbols(values: list[str]) -> list[str]:
    symbols: list[str] = []
    for raw in values:
        for item in str(raw or "").split(","):
            symbol = item.strip().upper()
            if symbol and symbol not in symbols:
                symbols.append(symbol)
    return symbols


@dataclass
class ContractResult:
    symbol: str
    routes: list[str] = field(default_factory=list)
    contracts: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""


class RouteProbe(EWrapper, EClient):
    def __init__(self) -> None:
        EWrapper.__init__(self)
        EClient.__init__(self, self)
        self.ready = threading.Event()
        self.closed = threading.Event()
        self._request_id = 7000
        self._events: dict[int, threading.Event] = {}
        self._symbols: dict[int, str] = {}
        self._results: dict[int, ContractResult] = {}
        self._global_errors: list[str] = []

    def nextValidId(self, orderId: int) -> None:  # noqa: N802 - IB callback name
        del orderId
        self.ready.set()

    def connectionClosed(self) -> None:  # noqa: N802 - IB callback name
        self.closed.set()
        for event in self._events.values():
            event.set()

    def error(self, reqId: int, *args: Any) -> None:
        code, message = _normalize_error_args(args)
        if code in _INFO_CODES:
            return
        detail = f"IB {code}: {message}" if code else message
        result = self._results.get(int(reqId))
        if result is not None:
            result.error = detail
            event = self._events.get(int(reqId))
            if event:
                event.set()
        else:
            self._global_errors.append(detail)
            print(detail, file=sys.stderr)

    def contractDetails(self, reqId: int, contractDetails: Any) -> None:  # noqa: N802
        result = self._results.get(int(reqId))
        if result is None:
            return
        contract = getattr(contractDetails, "contract", None)
        discovered = _split_routes(getattr(contractDetails, "validExchanges", ""))
        for route in discovered:
            if route not in result.routes:
                result.routes.append(route)
        result.contracts.append(
            {
                "con_id": int(getattr(contract, "conId", 0) or 0),
                "primary_exchange": str(getattr(contract, "primaryExchange", "") or ""),
                "valid_exchanges": discovered,
            }
        )

    def contractDetailsEnd(self, reqId: int) -> None:  # noqa: N802
        event = self._events.get(int(reqId))
        if event:
            event.set()

    def request_routes(self, symbol: str, timeout: float) -> ContractResult:
        self._request_id += 1
        request_id = self._request_id
        event = threading.Event()
        result = ContractResult(symbol=symbol)
        self._events[request_id] = event
        self._symbols[request_id] = symbol
        self._results[request_id] = result

        contract = Contract()
        contract.symbol = symbol
        contract.secType = "STK"
        contract.exchange = "SMART"
        contract.currency = "USD"
        self.reqContractDetails(request_id, contract)

        if not event.wait(timeout):
            result.error = f"Timed out after {timeout:g}s"
        self._events.pop(request_id, None)
        self._symbols.pop(request_id, None)
        self._results.pop(request_id, None)
        return result


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect validExchanges for US stocks from IB Gateway/TWS.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Gateway/TWS API host")
    parser.add_argument("--port", type=int, default=4001, help="Gateway/TWS API port")
    parser.add_argument(
        "--client-id",
        type=int,
        default=91,
        help="Unused IB API client ID; do not reuse the running TS client ID",
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=["AAPL", "MSFT", "NVDA", "SPY", "QQQ", "IBM", "TSLA", "AMD"],
        help="Space- or comma-separated US stock symbols",
    )
    parser.add_argument("--timeout", type=float, default=10.0, help="Timeout per request")
    parser.add_argument("--connect-timeout", type=float, default=12.0)
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    symbols = _normalize_symbols(args.symbols)
    if not symbols:
        print("At least one symbol is required", file=sys.stderr)
        return 2
    if not 1 <= args.port <= 65535:
        print("Port must be between 1 and 65535", file=sys.stderr)
        return 2
    if args.timeout <= 0 or args.connect_timeout <= 0:
        print("Timeout values must be positive", file=sys.stderr)
        return 2

    probe = RouteProbe()
    try:
        probe.connect(args.host, args.port, clientId=args.client_id)
    except Exception as exc:
        print(f"IB API connection failed: {exc}", file=sys.stderr)
        return 3
    if not probe.isConnected():
        errors = "; ".join(probe._global_errors) or "socket connection was not established"
        print(f"IB API connection failed: {errors}", file=sys.stderr)
        return 3
    reader = threading.Thread(target=probe.run, name="ib-route-probe", daemon=True)
    reader.start()
    try:
        if not probe.ready.wait(args.connect_timeout):
            errors = "; ".join(probe._global_errors) or "nextValidId was not received"
            print(f"IB API connection was not ready: {errors}", file=sys.stderr)
            return 3

        results = [probe.request_routes(symbol, args.timeout) for symbol in symbols]
        route_union: list[str] = []
        for result in results:
            for route in result.routes:
                if route not in route_union:
                    route_union.append(route)
        if "SMART" in route_union:
            route_union.remove("SMART")
            route_union.insert(0, "SMART")

        payload = {
            "host": args.host,
            "port": args.port,
            "client_id": args.client_id,
            "collected_at_epoch": int(time.time()),
            "symbols": [
                {
                    "symbol": result.symbol,
                    "routes": result.routes,
                    "contracts": result.contracts,
                    "error": result.error,
                }
                for result in results
            ],
            "route_union": route_union,
        }
        if args.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            for result in results:
                routes = ", ".join(result.routes) or "-"
                suffix = f"  ERROR: {result.error}" if result.error else ""
                print(f"{result.symbol:<8} {routes}{suffix}")
            print(f"UNION    {', '.join(route_union) or '-'}")

        return 0 if route_union and not all(item.error for item in results) else 4
    finally:
        probe.disconnect()
        reader.join(timeout=2.0)


if __name__ == "__main__":
    raise SystemExit(main())
