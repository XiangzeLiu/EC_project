"""Declarative hotkey actions, contexts, and editable runtime settings."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Iterable


class HotkeyAction(str, Enum):
    PANEL_CYCLE = "panel.cycle"
    PANEL_ACTIVATE = "panel.activate"
    ORDER_MARKET = "order.market"
    ORDER_PREPARE_LIMIT = "order.prepare_limit"
    ORDER_PREPARE_RULE = "order.prepare_rule"
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


@dataclass(frozen=True)
class QuantityHotkey:
    key: str
    quantity: int
    enabled: bool = True


@dataclass(frozen=True)
class OrderHotkeyRule:
    id: str
    key: str | None
    enabled: bool = False
    side: str = "buy"
    order_type: str = "limit"
    tif: str = "Day"
    route: str = "DEFAULT"
    price_offset: float = 0.0
    hidden: bool = False


@dataclass(frozen=True)
class HotkeyRuntimeConfig:
    default_route: str = "SMART"
    quantity_hotkeys: tuple[QuantityHotkey, ...] = ()
    order_hotkeys: tuple[OrderHotkeyRule, ...] = ()


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
MAX_ORDER_HOTKEY_RULES = 15

VALID_ORDER_SIDES = {"buy", "sell"}
VALID_ORDER_TYPES = {"limit", "market"}
VALID_TIFS = {"Day", "GTC", "IOC", "EXT", "GTC_EXT"}
DEFAULT_ORDER_KEYS = tuple(f"Shift+F{index}" for index in range(1, 13))
NUMPAD_QUANTITY_KEYS = tuple(f"Num+{index}" for index in range(1, 10))
_ROUTE_RE = re.compile(r"^[A-Z0-9._-]+$")


ACTION_POLICIES: dict[HotkeyAction, RateLimitPolicy] = {
    HotkeyAction.ORDER_MARKET: ORDER_HOTKEY_POLICY,
    HotkeyAction.ORDER_PREPARE_RULE: ORDER_HOTKEY_POLICY,
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


def _binding(
    binding_id: str,
    key: str | None,
    action: HotkeyAction,
    context: HotkeyContext,
    enabled: bool = True,
    **params: Any,
) -> HotkeyBinding:
    return HotkeyBinding(
        id=binding_id,
        key=key,
        action=action,
        context=context,
        enabled=enabled,
        params=params,
    )


def _default_quantity_hotkeys() -> tuple[QuantityHotkey, ...]:
    return tuple(
        QuantityHotkey(key=f"Num+{index}", quantity=index * 100, enabled=True)
        for index in range(1, 10)
    )


def _default_order_hotkeys() -> tuple[OrderHotkeyRule, ...]:
    return tuple(
        OrderHotkeyRule(id=f"order_rule_{index}", key=f"Shift+F{index}")
        for index in range(1, 13)
    )


DEFAULT_HOTKEY_CONFIG = HotkeyRuntimeConfig(
    default_route="SMART",
    quantity_hotkeys=_default_quantity_hotkeys(),
    order_hotkeys=_default_order_hotkeys(),
)


FIXED_HOTKEY_BINDINGS: tuple[HotkeyBinding, ...] = (
    _binding("panel_cycle", "Space", HotkeyAction.PANEL_CYCLE, HotkeyContext.MAIN_WINDOW),
    _binding("cancel_symbol_live", "Esc", HotkeyAction.ORDER_CANCEL_SYMBOL_LIVE, HotkeyContext.MAIN_WINDOW),
    _binding("price_increase_large", "Up", HotkeyAction.PRICE_ADJUST, HotkeyContext.PRICE_INPUT, delta=0.05),
    _binding("price_decrease_large", "Down", HotkeyAction.PRICE_ADJUST, HotkeyContext.PRICE_INPUT, delta=-0.05),
    _binding("price_decrease_small", "Left", HotkeyAction.PRICE_ADJUST, HotkeyContext.PRICE_INPUT, delta=-0.01),
    _binding("price_increase_small", "Right", HotkeyAction.PRICE_ADJUST, HotkeyContext.PRICE_INPUT, delta=0.01),
)


FIXED_HOTKEY_DESCRIPTIONS: tuple[tuple[str, str], ...] = (
    ("Space", "切换左右交易栏"),
    ("Esc", "清除当前栏待确认状态，并撤销当前股票全部活动订单"),
    ("Enter", "在 SYMBOL 中查询股票；在待确认订单中提交订单"),
    ("Up", "PRICE +0.05"),
    ("Down", "PRICE -0.05"),
    ("Left", "PRICE -0.01"),
    ("Right", "PRICE +0.01"),
)


def bindings_from_config(config: HotkeyRuntimeConfig) -> tuple[HotkeyBinding, ...]:
    bindings: list[HotkeyBinding] = list(FIXED_HOTKEY_BINDINGS)
    for hotkey in config.quantity_hotkeys:
        bindings.append(
            _binding(
                f"quantity_{hotkey.key.lower().replace('+', '_')}",
                hotkey.key,
                HotkeyAction.QUANTITY_SET,
                HotkeyContext.MAIN_WINDOW,
                bool(hotkey.enabled),
                value=int(hotkey.quantity),
            )
        )
    for rule in config.order_hotkeys:
        bindings.append(
            _binding(
                rule.id,
                rule.key,
                HotkeyAction.ORDER_PREPARE_RULE,
                HotkeyContext.MAIN_WINDOW,
                bool(rule.enabled),
                side=rule.side,
                order_type=rule.order_type,
                tif=rule.tif,
                route=rule.route,
                price_offset=float(rule.price_offset),
                hidden=bool(rule.hidden),
            )
        )
    return tuple(bindings)


HOTKEY_BINDINGS: tuple[HotkeyBinding, ...] = bindings_from_config(DEFAULT_HOTKEY_CONFIG)


def _normalize_key(value: str | None) -> str:
    return str(value or "").strip().casefold()


def _validate_order_rule(rule: OrderHotkeyRule, errors: list[str]) -> None:
    if not rule.id:
        errors.append("下单快捷键规则缺少 id")
    if rule.side not in VALID_ORDER_SIDES:
        errors.append(f"{rule.id} 方向无效")
    if rule.order_type not in VALID_ORDER_TYPES:
        errors.append(f"{rule.id} 类型无效")
    if rule.tif not in VALID_TIFS:
        errors.append(f"{rule.id} TIF 无效")
    if not str(rule.route or "").strip():
        errors.append(f"{rule.id} ROUTE 不能为空")
    elif not _ROUTE_RE.fullmatch(str(rule.route).strip().upper()):
        errors.append(f"{rule.id} ROUTE 格式无效")
    try:
        float(rule.price_offset)
    except (TypeError, ValueError):
        errors.append(f"{rule.id} 价格偏移必须是数字")
    if rule.enabled and not str(rule.key or "").strip():
        errors.append(f"{rule.id} 已启用但没有快捷键")


def validate_hotkey_config(config: HotkeyRuntimeConfig) -> list[str]:
    errors: list[str] = []
    if not str(config.default_route or "").strip():
        errors.append("默认 ROUTE 不能为空")
    elif not _ROUTE_RE.fullmatch(str(config.default_route).strip().upper()):
        errors.append("默认 ROUTE 格式无效")

    quantities = tuple(config.quantity_hotkeys)
    expected_keys = set(NUMPAD_QUANTITY_KEYS)
    if {item.key for item in quantities} != expected_keys:
        errors.append("股数快捷键必须且只能包含 Num 1 到 Num 9")
    for item in quantities:
        if item.key not in expected_keys:
            errors.append(f"不支持的股数快捷键：{item.key}")
        if not isinstance(item.quantity, int) or item.quantity <= 0:
            errors.append(f"{item.key} 股数必须是正整数")

    rules = tuple(config.order_hotkeys)
    if len(rules) > MAX_ORDER_HOTKEY_RULES:
        errors.append(f"下单快捷键最多 {MAX_ORDER_HOTKEY_RULES} 条")
    ids: set[str] = set()
    for rule in rules:
        if rule.id in ids:
            errors.append(f"下单快捷键规则 id 重复：{rule.id}")
        ids.add(rule.id)
        _validate_order_rule(rule, errors)

    errors.extend(validate_bindings(bindings_from_config(config)))
    return errors


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
            errors.append(f"快捷键冲突：{key}")
        keys.add(conflict_key)

        params = binding.params
        if binding.action in {HotkeyAction.ORDER_MARKET, HotkeyAction.ORDER_PREPARE_LIMIT, HotkeyAction.ORDER_PREPARE_RULE}:
            if params.get("side") not in {"buy", "sell"}:
                errors.append(f"invalid side for {binding.id}")
            if binding.action == HotkeyAction.ORDER_PREPARE_RULE:
                if params.get("order_type") not in {"limit", "market"}:
                    errors.append(f"invalid order type for {binding.id}")
                if params.get("tif") not in VALID_TIFS:
                    errors.append(f"invalid tif for {binding.id}")
                if not str(params.get("route") or "").strip():
                    errors.append(f"invalid route for {binding.id}")
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
            if params.get("mode") not in {"live", "filled", "inactive", "all"}:
                errors.append(f"invalid order mode for {binding.id}")

    return errors


def update_quantity(config: HotkeyRuntimeConfig, key: str, quantity: int) -> HotkeyRuntimeConfig:
    items = tuple(
        replace(item, quantity=quantity) if item.key == key else item
        for item in config.quantity_hotkeys
    )
    return replace(config, quantity_hotkeys=items)
