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
    id: str
    key: str | None
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
LIMIT_IOC_QUOTE_FRESHNESS_MS = 5000
IOC_QUOTE_REFRESH_TIMEOUT_MS = 5000
MAX_ORDER_HOTKEY_RULES = 15
MAX_QUANTITY_HOTKEY_RULES = 20

VALID_ORDER_SIDES = {"buy", "sell"}
VALID_ORDER_TYPES = {"limit", "market"}
VALID_TIFS = {"Day", "GTC", "IOC", "EXT", "GTC_EXT"}
DEFAULT_ORDER_KEYS = tuple(f"Shift+F{index}" for index in range(1, 13))
NUMPAD_QUANTITY_KEYS = tuple(f"Num+{index}" for index in range(1, 10))
DEFAULT_QUANTITY_HOTKEY_IDS = tuple(
    f"quantity_default_{index}" for index in range(1, 10)
)
DEFAULT_QUANTITY_KEY_BY_ID = dict(
    zip(DEFAULT_QUANTITY_HOTKEY_IDS, NUMPAD_QUANTITY_KEYS)
)
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
        QuantityHotkey(
            id=f"quantity_default_{index}",
            key=f"Num+{index}",
            quantity=index * 100,
            enabled=True,
        )
        for index in range(1, 10)
    )


def _default_order_hotkeys() -> tuple[OrderHotkeyRule, ...]:
    defaults = (
        ("buy", "limit", "Day", 0.0, False),
        ("sell", "limit", "Day", 0.0, False),
        ("buy", "limit", "GTC", 0.0, False),
        ("sell", "limit", "GTC", 0.0, False),
        ("buy", "limit", "EXT", 0.0, False),
        ("sell", "limit", "EXT", 0.0, False),
        ("buy", "limit", "GTC_EXT", 0.0, False),
        ("sell", "limit", "GTC_EXT", 0.0, False),
        ("buy", "limit", "IOC", 0.0, False),
        ("sell", "limit", "IOC", 0.0, False),
        ("buy", "market", "Day", 0.0, False),
        ("sell", "market", "Day", 0.0, False),
    )
    return tuple(
        OrderHotkeyRule(
            id=f"order_rule_{index}",
            key=f"Shift+F{index}",
            enabled=False,
            side=side,
            order_type=order_type,
            tif=tif,
            route="DEFAULT",
            price_offset=price_offset,
            hidden=hidden,
        )
        for index, (side, order_type, tif, price_offset, hidden) in enumerate(defaults, start=1)
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
                hotkey.id,
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
    if len(quantities) < len(DEFAULT_QUANTITY_HOTKEY_IDS):
        errors.append("股数快捷键必须保留 Num 1 到 Num 9")
    if len(quantities) > MAX_QUANTITY_HOTKEY_RULES:
        errors.append(f"股数快捷键最多 {MAX_QUANTITY_HOTKEY_RULES} 条")

    quantity_ids: set[str] = set()
    quantity_keys: set[str] = set()
    for item in quantities:
        item_id = str(item.id or "").strip()
        key = str(item.key or "").strip()
        if not item_id:
            errors.append("股数快捷键规则缺少 id")
        elif item_id in quantity_ids:
            errors.append(f"股数快捷键规则 id 重复：{item_id}")
        quantity_ids.add(item_id)

        fixed_key = DEFAULT_QUANTITY_KEY_BY_ID.get(item_id)
        if fixed_key is not None and key != fixed_key:
            errors.append(f"{item_id} 的固定按键必须是 {fixed_key}")
        elif fixed_key is None and key in NUMPAD_QUANTITY_KEYS:
            errors.append(f"自定义股数快捷键不能占用固定按键：{key}")
        if item.enabled and not key:
            errors.append(f"{item_id or '股数快捷键'} 已启用但没有快捷键")
        normalized_key = _normalize_key(key)
        if normalized_key:
            if normalized_key in quantity_keys:
                errors.append(f"股数快捷键重复：{key}")
            quantity_keys.add(normalized_key)
        if not isinstance(item.quantity, int) or item.quantity <= 0:
            errors.append(f"{key or item_id} 股数必须是正整数")

    for default_id, expected_key in DEFAULT_QUANTITY_KEY_BY_ID.items():
        if default_id not in quantity_ids:
            errors.append(f"缺少固定股数快捷键：{expected_key}")

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


def format_hotkey_validation_errors(errors: Iterable[str], limit: int = 3) -> list[str]:
    """Convert internal validation details to safe, user-facing messages."""
    messages: list[str] = []
    seen: set[str] = set()
    for raw_error in errors:
        error = str(raw_error or "").strip()
        lowered = error.casefold()
        if not error:
            continue
        if "冲突" in error or "conflict" in lowered:
            message = "快捷键冲突，请更换按键"
        elif "没有快捷键" in error or "no key" in lowered:
            message = "已启用的快捷键不能为空，请填写快捷键或取消启用"
        elif "快捷键无效" in error or "invalid key" in lowered:
            message = "快捷键格式无效，请使用单个按键或组合键"
        elif "quantity" in lowered or "股数" in error:
            message = "股数快捷键设置无效，请检查股数和按键"
        elif "order" in lowered or "下单" in error:
            message = "下单快捷键设置无效，请检查输入内容"
        elif "route" in lowered or "route" in error.casefold():
            message = "ROUTE 设置无效，请检查输入内容"
        elif "tif" in lowered:
            message = "TIF 设置无效，请检查输入内容"
        else:
            message = "快捷键配置无效，请检查输入内容"
        if message not in seen:
            messages.append(message)
            seen.add(message)
        if len(messages) >= max(1, int(limit)):
            break
    return messages


def update_quantity(config: HotkeyRuntimeConfig, key: str, quantity: int) -> HotkeyRuntimeConfig:
    items = tuple(
        replace(item, quantity=quantity) if item.key == key else item
        for item in config.quantity_hotkeys
    )
    return replace(config, quantity_hotkeys=items)
