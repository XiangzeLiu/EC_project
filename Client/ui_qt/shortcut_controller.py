"""Context-aware keyboard dispatcher for the Client main window."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable

from PySide6.QtCore import QEvent, QObject, QTimer
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QApplication, QWidget

from .hotkey_config import ACTION_POLICIES, HotkeyBinding, HotkeyContext, RateLimitPolicy, validate_bindings


_CONTEXT_PRIORITY = {
    HotkeyContext.MAIN_WINDOW: 0,
    HotkeyContext.TRADE_PANEL: 1,
    HotkeyContext.SYMBOL_INPUT: 2,
    HotkeyContext.QUANTITY_CONTROL: 2,
    HotkeyContext.PRICE_INPUT: 2,
    HotkeyContext.ORDERS_TABLE: 2,
    HotkeyContext.POSITIONS_TABLE: 2,
}


class ShortcutController(QObject):
    def __init__(
        self,
        window: QWidget,
        bindings: Iterable[HotkeyBinding],
        dispatch: Callable[[HotkeyBinding], None],
        context_matches: Callable[[HotkeyBinding], bool],
    ):
        super().__init__(window)
        self._window = window
        self._dispatch = dispatch
        self._context_matches = context_matches
        self._bindings = tuple(binding for binding in bindings if binding.enabled and binding.key)
        self._by_key: dict[str, list[HotkeyBinding]] = {}
        self._last_triggered: dict[str, float] = {}
        self._repeat_timers: dict[str, QTimer] = {}
        self._active_repeat_bindings: dict[str, HotkeyBinding] = {}
        self._installed = False
        self.errors = validate_bindings(bindings)
        if not self.errors:
            self.errors.extend(self._index_bindings())

    @staticmethod
    def _normalize_sequence(value: str) -> str:
        sequence = QKeySequence.fromString(value, QKeySequence.PortableText)
        if sequence.count() != 1:
            return ""
        return sequence.toString(QKeySequence.PortableText).casefold()

    @staticmethod
    def _event_sequence(event) -> str:
        try:
            sequence = QKeySequence(event.keyCombination())
        except Exception:
            sequence = QKeySequence(int(event.modifiers()) | int(event.key()))
        return sequence.toString(QKeySequence.PortableText).casefold()

    def _index_bindings(self) -> list[str]:
        errors: list[str] = []
        for binding in self._bindings:
            normalized = self._normalize_sequence(str(binding.key))
            if not normalized:
                errors.append(f"invalid key sequence for {binding.id}: {binding.key!r}")
                continue
            self._by_key.setdefault(normalized, []).append(binding)
        return errors

    def install(self) -> bool:
        app = QApplication.instance()
        if self.errors or app is None or self._installed:
            return False
        app.installEventFilter(self)
        self._installed = True
        return True

    def shutdown(self) -> None:
        app = QApplication.instance()
        if app is not None and self._installed:
            app.removeEventFilter(self)
        self._installed = False
        self._stop_repeats()

    def _policy_for(self, binding: HotkeyBinding) -> RateLimitPolicy:
        return ACTION_POLICIES.get(binding.action, RateLimitPolicy())

    def _activate(self, binding: HotkeyBinding) -> bool:
        policy = self._policy_for(binding)
        now = time.monotonic()
        last = self._last_triggered.get(binding.id)
        if policy.cooldown_ms > 0 and last is not None:
            if (now - last) * 1000 < policy.cooldown_ms:
                return False
        self._last_triggered[binding.id] = now
        self._dispatch(binding)
        return True

    def _start_repeat(self, key: str, binding: HotkeyBinding) -> None:
        policy = self._policy_for(binding)
        if not policy.allow_auto_repeat or key in self._repeat_timers:
            return
        timer = QTimer(self)
        timer.setSingleShot(True)

        def repeat_once() -> None:
            if key not in self._active_repeat_bindings or not self._context_matches(binding):
                self._stop_repeat(key)
                return
            self._dispatch(binding)

        def begin_interval() -> None:
            if key not in self._active_repeat_bindings or not self._context_matches(binding):
                self._stop_repeat(key)
                return
            self._dispatch(binding)
            timer.setSingleShot(False)
            timer.setInterval(max(20, policy.repeat_interval_ms))
            timer.timeout.disconnect()
            timer.timeout.connect(repeat_once)
            timer.start()

        timer.timeout.connect(begin_interval)
        self._active_repeat_bindings[key] = binding
        self._repeat_timers[key] = timer
        timer.start(max(0, policy.repeat_delay_ms))

    def _stop_repeat(self, key: str) -> None:
        self._active_repeat_bindings.pop(key, None)
        timer = self._repeat_timers.pop(key, None)
        if timer:
            timer.stop()
            timer.deleteLater()

    def _stop_repeats(self) -> None:
        for key in list(self._repeat_timers):
            self._stop_repeat(key)

    def eventFilter(self, watched, event):
        event_type = event.type()
        if event_type in (QEvent.ApplicationDeactivate, QEvent.WindowDeactivate):
            self._stop_repeats()
            return False
        if event_type not in (QEvent.KeyPress, QEvent.KeyRelease):
            return False
        if QApplication.activeWindow() is not self._window:
            return False

        key = self._event_sequence(event)
        candidates = self._by_key.get(key, ())
        matches = [item for item in candidates if self._context_matches(item)]
        binding = max(matches, key=lambda item: _CONTEXT_PRIORITY.get(item.context, 0), default=None)
        if binding is None:
            return False

        if event_type == QEvent.KeyRelease:
            if not event.isAutoRepeat():
                self._stop_repeat(key)
            event.accept()
            return True

        if event.isAutoRepeat():
            event.accept()
            return True

        self._activate(binding)
        self._start_repeat(key, binding)
        event.accept()
        return True
