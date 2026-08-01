"""Declarative hotkey actions, contexts, and development-time bindings."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable


class HotkeyAction(str, Enum):
    PANEL_ACTIVATE = "panel.activate"
    ORDER_MARKET = "order.market"
    ORDER_PREPARE_LIMIT = "order.prepare_limit"
    ORDER_CONFIRM_PENDING = "order.confirm_pending"
    ORDER_CANCEL_PENDING = "order.cancel_pending"
    ORDER_CANCEL_SELECTED = "order.cancel_selected"
    ORDER_CANCEL_SYMBOL_LIVE = "order.cancel_symbol_live"
    QUANTITY_SET = "quantity.set"
    QUANTITY_ADJUST = "quantity.adjust"
    PRICE_ADJUST = "price.adjust"
    ORDERS_SWITCH_MODE = "orders.switch_mode"
    REFRESH_ORDERS = "refresh.orders"
    REFRESH_POSITIONS = "refresh.positions"
    REFRESH_ALL = "refresh.all"
    LOGS_CLEAR = "logs.clear"


class HotkeyContext(str, Enum):
    MAIN_WINDOW = "main_window"
    TRADE_PANEL = "active_trade_panel"
    SYMBOL_INPUT = "symbol_input"
    QUANTITY_CONTROL = "quantity_control"
    PRICE_INPUT = "price_input"
    ORDERS_TABLE = "orders_table"
    POSITIONS_TABLE = "positions_table"


@dataclass(frozen=True)
class RateLimitPolicy:
    allow_auto_repeat: bool = False
    cooldown_ms: int = 0
    burst_limit: int = 0
    burst_window_ms: int = 0
    max_in_flight: int = 0
    repeat_delay_ms: int = 300
    repeat_interval_ms: int = 80


@dataclass(frozen=True)
class HotkeyBinding:
    id: str
    key: str | None
    action: HotkeyAction
    context: HotkeyContext
    enabled: bool = False
    params: dict[str, Any] = field(default_factory=dict)


ORDER_HOTKEY_POLICY = RateLimitPolicy(cooldown_ms=300)
# Business-level order throttles stay disabled. Input-level keyboard protection
# is configured separately by ORDER_HOTKEY_POLICY.
ORDER_SUBMIT_POLICY = RateLimitPolicy()
IDENTICAL_ORDER_COOLDOWN_MS = 0
ENTER_INPUT_GUARD_MS = 300
ORDER_CANCEL_POLICY = RateLimitPolicy(cooldown_ms=500, max_in_flight=3)
BATCH_CANCEL_POLICY = RateLimitPolicy(cooldown_ms=1000, max_in_flight=1)
REFRESH_POLICY = RateLimitPolicy(cooldown_ms=1000, max_in_flight=1)
RATE_LIMIT_NOTICE_MS = 1000
QUOTE_FRESHNESS_MS = 5000


ACTION_POLICIES: dict[HotkeyAction, RateLimitPolicy] = {
    HotkeyAction.ORDER_MARKET: ORDER_HOTKEY_POLICY,
    HotkeyAction.ORDER_CONFIRM_PENDING: ORDER_HOTKEY_POLICY,
    HotkeyAction.ORDER_PREPARE_LIMIT: RateLimitPolicy(cooldown_ms=150),
    HotkeyAction.ORDER_CANCEL_SELECTED: ORDER_CANCEL_POLICY,
    HotkeyAction.ORDER_CANCEL_SYMBOL_LIVE: BATCH_CANCEL_POLICY,
    HotkeyAction.QUANTITY_ADJUST: RateLimitPolicy(
        allow_auto_repeat=True,
        repeat_delay_ms=300,
        repeat_interval_ms=80,
    ),
    HotkeyAction.PRICE_ADJUST: RateLimitPolicy(
        allow_auto_repeat=True,
        repeat_delay_ms=300,
        repeat_interval_ms=80,
    ),
    HotkeyAction.REFRESH_ORDERS: REFRESH_POLICY,
    HotkeyAction.REFRESH_POSITIONS: REFRESH_POLICY,
    HotkeyAction.REFRESH_ALL: REFRESH_POLICY,
}


def _reserved(
    binding_id: str,
    action: HotkeyAction,
    context: HotkeyContext,
    **params: Any,
) -> HotkeyBinding:
    return HotkeyBinding(
        id=binding_id,
        key=None,
        action=action,
        context=context,
        enabled=False,
        params=params,
    )


# Production keys intentionally stay empty until the mapping is approved.
HOTKEY_BINDINGS: tuple[HotkeyBinding, ...] = (
    _reserved("panel_1", HotkeyAction.PANEL_ACTIVATE, HotkeyContext.MAIN_WINDOW, panel_id=1),
    _reserved("panel_2", HotkeyAction.PANEL_ACTIVATE, HotkeyContext.MAIN_WINDOW, panel_id=2),
    _reserved("market_buy", HotkeyAction.ORDER_MARKET, HotkeyContext.TRADE_PANEL, side="buy"),
    _reserved("market_sell", HotkeyAction.ORDER_MARKET, HotkeyContext.TRADE_PANEL, side="sell"),
    _reserved("limit_buy_prepare", HotkeyAction.ORDER_PREPARE_LIMIT, HotkeyContext.TRADE_PANEL, side="buy", price_source="ask"),
    _reserved("limit_sell_prepare", HotkeyAction.ORDER_PREPARE_LIMIT, HotkeyContext.TRADE_PANEL, side="sell", price_source="bid"),
    _reserved("limit_confirm", HotkeyAction.ORDER_CONFIRM_PENDING, HotkeyContext.PRICE_INPUT),
    _reserved("pending_cancel", HotkeyAction.ORDER_CANCEL_PENDING, HotkeyContext.TRADE_PANEL),
    _reserved("cancel_selected", HotkeyAction.ORDER_CANCEL_SELECTED, HotkeyContext.ORDERS_TABLE),
    _reserved("cancel_symbol_live", HotkeyAction.ORDER_CANCEL_SYMBOL_LIVE, HotkeyContext.TRADE_PANEL),
    *(
        _reserved(f"quantity_preset_{index}", HotkeyAction.QUANTITY_SET, HotkeyContext.TRADE_PANEL)
        for index in range(1, 11)
    ),
    _reserved("quantity_increase_small", HotkeyAction.QUANTITY_ADJUST, HotkeyContext.QUANTITY_CONTROL),
    _reserved("quantity_decrease_small", HotkeyAction.QUANTITY_ADJUST, HotkeyContext.QUANTITY_CONTROL),
    _reserved("quantity_increase_large", HotkeyAction.QUANTITY_ADJUST, HotkeyContext.QUANTITY_CONTROL),
    _reserved("quantity_decrease_large", HotkeyAction.QUANTITY_ADJUST, HotkeyContext.QUANTITY_CONTROL),
    _reserved("price_increase_small", HotkeyAction.PRICE_ADJUST, HotkeyContext.PRICE_INPUT),
    _reserved("price_decrease_small", HotkeyAction.PRICE_ADJUST, HotkeyContext.PRICE_INPUT),
    _reserved("price_increase_large", HotkeyAction.PRICE_ADJUST, HotkeyContext.PRICE_INPUT),
    _reserved("price_decrease_large", HotkeyAction.PRICE_ADJUST, HotkeyContext.PRICE_INPUT),
    _reserved("orders_live", HotkeyAction.ORDERS_SWITCH_MODE, HotkeyContext.MAIN_WINDOW, mode="live"),
    _reserved("orders_all", HotkeyAction.ORDERS_SWITCH_MODE, HotkeyContext.MAIN_WINDOW, mode="all"),
    _reserved("refresh_orders", HotkeyAction.REFRESH_ORDERS, HotkeyContext.MAIN_WINDOW),
    _reserved("refresh_positions", HotkeyAction.REFRESH_POSITIONS, HotkeyContext.MAIN_WINDOW),
    _reserved("refresh_all", HotkeyAction.REFRESH_ALL, HotkeyContext.MAIN_WINDOW),
    _reserved("logs_clear", HotkeyAction.LOGS_CLEAR, HotkeyContext.MAIN_WINDOW),
)


def validate_bindings(bindings: Iterable[HotkeyBinding]) -> list[str]:
    """Return configuration errors without partially enabling a bad mapping."""
    errors: list[str] = []
    ids: set[str] = set()
    keys: set[tuple[HotkeyContext, str]] = set()

    for binding in bindings:
        if not binding.id or binding.id in ids:
            errors.append(f"duplicate or empty binding id: {binding.id!r}")
        ids.add(binding.id)

        if not binding.enabled:
            continue
        key = str(binding.key or "").strip()
        if not key:
            errors.append(f"enabled binding has no key: {binding.id}")
            continue
        conflict_key = (binding.context, key.casefold())
        if conflict_key in keys:
            errors.append(f"duplicate key in {binding.context.value}: {key}")
        keys.add(conflict_key)

        params = binding.params
        if binding.action in {HotkeyAction.ORDER_MARKET, HotkeyAction.ORDER_PREPARE_LIMIT}:
            if params.get("side") not in {"buy", "sell"}:
                errors.append(f"invalid side for {binding.id}")
        elif binding.action == HotkeyAction.QUANTITY_SET:
            value = params.get("value")
            if not isinstance(value, int) or value <= 0:
                errors.append(f"invalid quantity value for {binding.id}")
        elif binding.action == HotkeyAction.QUANTITY_ADJUST:
            delta = params.get("delta")
            if not isinstance(delta, int) or delta == 0:
                errors.append(f"invalid quantity delta for {binding.id}")
        elif binding.action == HotkeyAction.PRICE_ADJUST:
            delta = params.get("delta")
            if not isinstance(delta, (int, float)) or float(delta) == 0:
                errors.append(f"invalid price delta for {binding.id}")
        elif binding.action == HotkeyAction.PANEL_ACTIVATE:
            if params.get("panel_id") not in {1, 2}:
                errors.append(f"invalid panel id for {binding.id}")
        elif binding.action == HotkeyAction.ORDERS_SWITCH_MODE:
            if params.get("mode") not in {"live", "all"}:
                errors.append(f"invalid order mode for {binding.id}")

    return errors
