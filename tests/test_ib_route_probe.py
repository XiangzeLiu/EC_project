from Trader_Server.tools.probe_ib_routes import (
    _normalize_error_args,
    _normalize_symbols,
    _split_routes,
)


def test_route_probe_normalizes_and_deduplicates_ib_values():
    assert _split_routes("SMART, arca,NYSE,ARCA,, iex ") == [
        "SMART",
        "ARCA",
        "NYSE",
        "IEX",
    ]
    assert _normalize_symbols(["aapl, msft", "AAPL", " spy "]) == [
        "AAPL",
        "MSFT",
        "SPY",
    ]


def test_route_probe_accepts_current_and_legacy_ib_error_callbacks():
    assert _normalize_error_args((1234567890, 326, "client id in use")) == (
        326,
        "client id in use",
    )
    assert _normalize_error_args((200, "contract not found")) == (
        200,
        "contract not found",
    )
