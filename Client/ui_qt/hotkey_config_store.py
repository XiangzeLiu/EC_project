"""Load and save editable hotkey settings from the Windows user profile."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable

from .hotkey_config import (
    DEFAULT_HOTKEY_CONFIG,
    HOTKEY_BINDINGS,
    HotkeyBinding,
    HotkeyRuntimeConfig,
    OrderHotkeyRule,
    QuantityHotkey,
    bindings_from_config,
    validate_bindings,
    validate_hotkey_config,
)
from .shortcut_controller import validate_shortcut_sequences


CONFIG_VERSION = 2
LEGACY_CONFIG_VERSION = 1
APP_DIR_NAME = "SC Client"
HOTKEY_FILE_NAME = "hotkey.json"


@dataclass(frozen=True)
class HotkeyConfigLoadResult:
    bindings: tuple[HotkeyBinding, ...]
    path: Path
    errors: tuple[str, ...] = ()
    used_local_config: bool = False
    config: HotkeyRuntimeConfig = DEFAULT_HOTKEY_CONFIG


def hotkey_config_path() -> Path:
    override_dir = os.environ.get("SC_CLIENT_CONFIG_DIR", "").strip()
    if override_dir:
        return Path(override_dir) / HOTKEY_FILE_NAME
    appdata = os.environ.get("APPDATA", "").strip()
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / APP_DIR_NAME / HOTKEY_FILE_NAME


def load_hotkey_config(
    default_bindings: Iterable[HotkeyBinding] = HOTKEY_BINDINGS,
    *,
    path: Path | str | None = None,
    default_config: HotkeyRuntimeConfig = DEFAULT_HOTKEY_CONFIG,
) -> HotkeyConfigLoadResult:
    config_path = Path(path) if path is not None else hotkey_config_path()
    default_bindings_tuple = tuple(default_bindings)
    if not config_path.exists():
        return HotkeyConfigLoadResult(default_bindings_tuple, config_path, config=default_config)

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return HotkeyConfigLoadResult(
            default_bindings_tuple,
            config_path,
            (f"快捷键配置无法读取：{exc}",),
            config=default_config,
        )

    try:
        if isinstance(raw, dict) and raw.get("version") == LEGACY_CONFIG_VERSION:
            bindings = _merge_legacy_overrides(default_bindings_tuple, raw)
            validation_errors = validate_bindings(bindings)
            validation_errors.extend(validate_shortcut_sequences(bindings))
            errors = tuple(validation_errors)
            if errors:
                return HotkeyConfigLoadResult(default_bindings_tuple, config_path, errors, config=default_config)
            return HotkeyConfigLoadResult(bindings, config_path, used_local_config=True, config=default_config)

        config = _parse_config(raw, default_config)
        validation_errors = validate_hotkey_config(config)
        validation_errors.extend(validate_shortcut_sequences(bindings_from_config(config)))
        errors = tuple(validation_errors)
        if errors:
            return HotkeyConfigLoadResult(default_bindings_tuple, config_path, errors, config=default_config)
        return HotkeyConfigLoadResult(
            bindings_from_config(config),
            config_path,
            used_local_config=True,
            config=config,
        )
    except Exception as exc:
        return HotkeyConfigLoadResult(
            default_bindings_tuple,
            config_path,
            (f"快捷键配置无效：{exc}",),
            config=default_config,
        )


def save_hotkey_config(
    config_or_bindings: HotkeyRuntimeConfig | Iterable[HotkeyBinding],
    *,
    path: Path | str | None = None,
) -> Path:
    config_path = Path(path) if path is not None else hotkey_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(config_or_bindings, HotkeyRuntimeConfig):
        errors = validate_hotkey_config(config_or_bindings)
        errors.extend(validate_shortcut_sequences(bindings_from_config(config_or_bindings)))
        if errors:
            raise ValueError("; ".join(errors))
        payload = _serialize_config(config_or_bindings)
    else:
        bindings = tuple(config_or_bindings)
        errors = validate_bindings(bindings)
        errors.extend(validate_shortcut_sequences(bindings))
        if errors:
            raise ValueError("; ".join(errors))
        payload = {
            "version": LEGACY_CONFIG_VERSION,
            "bindings": [
                {
                    "id": binding.id,
                    "enabled": bool(binding.enabled),
                    "key": binding.key,
                }
                for binding in bindings
            ],
        }

    tmp_path = config_path.with_name(f"{config_path.name}.tmp")
    with tmp_path.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, config_path)
    return config_path


def _serialize_config(config: HotkeyRuntimeConfig) -> dict:
    return {
        "version": CONFIG_VERSION,
        "default_route": config.default_route,
        "quantity_hotkeys": [
            {"key": item.key, "quantity": int(item.quantity), "enabled": bool(item.enabled)}
            for item in config.quantity_hotkeys
        ],
        "order_hotkeys": [
            {
                "id": rule.id,
                "key": rule.key,
                "enabled": bool(rule.enabled),
                "side": rule.side,
                "order_type": rule.order_type,
                "tif": rule.tif,
                "route": rule.route,
                "price_offset": float(rule.price_offset),
                "hidden": bool(rule.hidden),
            }
            for rule in config.order_hotkeys
        ],
    }


def _parse_config(raw: object, default_config: HotkeyRuntimeConfig) -> HotkeyRuntimeConfig:
    if not isinstance(raw, dict):
        raise ValueError("根节点必须是对象")
    if raw.get("version") != CONFIG_VERSION:
        raise ValueError("配置版本不支持")

    default_route = str(raw.get("default_route") or default_config.default_route or "SMART").strip().upper()

    quantity_items = raw.get("quantity_hotkeys")
    if quantity_items is None:
        quantities = default_config.quantity_hotkeys
    elif not isinstance(quantity_items, list):
        raise ValueError("quantity_hotkeys 必须是列表")
    else:
        quantities = tuple(_parse_quantity(item, index) for index, item in enumerate(quantity_items))

    order_items = raw.get("order_hotkeys")
    if order_items is None:
        order_rules = default_config.order_hotkeys
    elif not isinstance(order_items, list):
        raise ValueError("order_hotkeys 必须是列表")
    else:
        order_rules = tuple(_parse_order_rule(item, index) for index, item in enumerate(order_items))

    return HotkeyRuntimeConfig(
        default_route=default_route,
        quantity_hotkeys=quantities,
        order_hotkeys=order_rules,
    )


def _parse_quantity(item: object, index: int) -> QuantityHotkey:
    if not isinstance(item, dict):
        raise ValueError(f"第 {index + 1} 条股数快捷键必须是对象")
    key = str(item.get("key") or "").strip()
    try:
        quantity = int(item.get("quantity") or 0)
    except (TypeError, ValueError):
        raise ValueError(f"{key or index + 1} 股数必须是整数") from None
    enabled = item.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError(f"{key} enabled 必须是布尔值")
    return QuantityHotkey(key=key, quantity=quantity, enabled=enabled)


def _parse_order_rule(item: object, index: int) -> OrderHotkeyRule:
    if not isinstance(item, dict):
        raise ValueError(f"第 {index + 1} 条下单快捷键必须是对象")
    rule_id = str(item.get("id") or f"order_rule_{index + 1}").strip()
    key = item.get("key")
    if key is not None and not isinstance(key, str):
        raise ValueError(f"{rule_id} key 必须是字符串或 null")
    enabled = item.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError(f"{rule_id} enabled 必须是布尔值")
    try:
        price_offset = float(item.get("price_offset") or 0.0)
    except (TypeError, ValueError):
        raise ValueError(f"{rule_id} 价格偏移必须是数字") from None
    hidden = item.get("hidden", False)
    if not isinstance(hidden, bool):
        raise ValueError(f"{rule_id} hidden 必须是布尔值")
    return OrderHotkeyRule(
        id=rule_id,
        key=key.strip() if isinstance(key, str) and key.strip() else None,
        enabled=enabled,
        side=str(item.get("side") or "buy").strip().lower(),
        order_type=str(item.get("order_type") or "limit").strip().lower(),
        tif=str(item.get("tif") or "Day").strip(),
        route=str(item.get("route") or "DEFAULT").strip().upper(),
        price_offset=price_offset,
        hidden=hidden,
    )


def _merge_legacy_overrides(
    defaults: tuple[HotkeyBinding, ...],
    raw: object,
) -> tuple[HotkeyBinding, ...]:
    if not isinstance(raw, dict):
        raise ValueError("根节点必须是对象")
    items = raw.get("bindings")
    if not isinstance(items, list):
        raise ValueError("bindings 必须是列表")

    overrides: dict[str, tuple[str | None, bool]] = {}
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index + 1} 条快捷键配置必须是对象")
        binding_id = str(item.get("id") or "").strip()
        if not binding_id:
            raise ValueError(f"第 {index + 1} 条快捷键配置缺少 id")
        key = item.get("key")
        if key is not None and not isinstance(key, str):
            raise ValueError(f"{binding_id} 的 key 必须是字符串或 null")
        enabled = item.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ValueError(f"{binding_id} 的 enabled 必须是布尔值")
        overrides[binding_id] = (key.strip() if isinstance(key, str) else None, enabled)

    merged = []
    for binding in defaults:
        if binding.id in overrides:
            key, enabled = overrides[binding.id]
            merged.append(replace(binding, key=key or None, enabled=enabled))
        else:
            merged.append(binding)
    return tuple(merged)
