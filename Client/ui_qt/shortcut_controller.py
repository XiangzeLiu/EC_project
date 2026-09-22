"""Context-aware keyboard dispatcher for the Client main window."""

from __future__ import annotations

import ctypes
import sys
import time
from collections.abc import Callable, Iterable

from PySide6.QtCore import QAbstractNativeEventFilter, QEvent, QObject, QTimer
from PySide6.QtGui import QContextMenuEvent, QKeySequence
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
_KEYBOARD_CONTEXT_MENU_KEY = "shift+f10"
_CONTEXT_MENU_SUPPRESSION_SECONDS = 0.5
_NATIVE_KEY_SUPPRESSION_SECONDS = 0.5

_WM_KEYDOWN = 0x0100
_WM_SYSKEYDOWN = 0x0104
_VK_F10 = 0x79
_VK_SHIFT = 0x10
_VK_CONTROL = 0x11
_VK_MENU = 0x12
_VK_LWIN = 0x5B
_VK_RWIN = 0x5C


class _WinPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _WinMsg(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("message", ctypes.c_uint),
        ("wParam", ctypes.c_size_t),
        ("lParam", ctypes.c_ssize_t),
        ("time", ctypes.c_ulong),
        ("pt", _WinPoint),
        ("lPrivate", ctypes.c_ulong),
    ]


def _read_native_message(message: object) -> _WinMsg | None:
    if sys.platform != "win32":
        return None
    try:
        address = int(message)
        if not address:
            return None
        return ctypes.cast(address, ctypes.POINTER(_WinMsg)).contents
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _shift_only_pressed() -> bool:
    if sys.platform != "win32":
        return False
    try:
        user32 = ctypes.windll.user32
        down = lambda key: bool(user32.GetKeyState(key) & 0x8000)
        return (
            down(_VK_SHIFT)
            and not down(_VK_CONTROL)
            and not down(_VK_MENU)
            and not down(_VK_LWIN)
            and not down(_VK_RWIN)
        )
    except (AttributeError, OSError):
        return False


class WindowsShiftF10NativeFilter(QAbstractNativeEventFilter):
    """Consume Shift+F10 before Windows turns it into WM_CONTEXTMENU."""

    def __init__(self, handler: Callable[[], bool]):
        super().__init__()
        self._handler = handler

    def uninstall(self, app: QApplication) -> None:
        app.removeNativeEventFilter(self)
        self._handler = lambda: False

    @staticmethod
    def _is_windows_message(event_type: object) -> bool:
        try:
            return bytes(event_type) in {
                b"windows_generic_MSG",
                b"windows_dispatcher_MSG",
            }
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _is_shift_f10_message(message: object, *, shift_pressed: bool | None = None) -> bool:
        native = _read_native_message(message)
        if native is None:
            return False
        if native.message not in (_WM_KEYDOWN, _WM_SYSKEYDOWN) or native.wParam != _VK_F10:
            return False
        return _shift_only_pressed() if shift_pressed is None else bool(shift_pressed)

    def handle_keydown(self, key: int, *, shift_pressed: bool) -> bool:
        """Small testable equivalent of the native F10 decision."""
        if key != _VK_F10 or not shift_pressed:
            return False
        return bool(self._handler())

    def nativeEventFilter(self, event_type, message):
        if (
            sys.platform != "win32"
            or not self._is_windows_message(event_type)
            or not self._is_shift_f10_message(message)
        ):
            return False, 0
        try:
            if self._handler():
                return True, 0
        except Exception:
            return False, 0
        return False, 0


def normalize_shortcut_sequence(value: str) -> str:
    sequence = QKeySequence.fromString(str(value or ""), QKeySequence.PortableText)
    if sequence.count() != 1:
        return ""
    return sequence.toString(QKeySequence.PortableText).casefold()


def validate_shortcut_sequences(bindings: Iterable[HotkeyBinding]) -> list[str]:
    """Validate keys exactly as Qt will register them at runtime."""
    errors: list[str] = []
    keys: dict[str, HotkeyBinding] = {}
    for binding in bindings:
        key_value = str(binding.key or "").strip()
        if not key_value:
            continue
        normalized = normalize_shortcut_sequence(key_value)
        if not normalized:
            errors.append(f"快捷键无效：{binding.id} = {binding.key!r}")
            continue
        previous = keys.get(normalized)
        if previous is not None:
            errors.append(
                f"快捷键冲突：{binding.key}（{previous.id} 与 {binding.id}）"
            )
            continue
        keys[normalized] = binding
    return errors


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
        self._context_menu_target: QObject | None = None
        self._context_menu_suppression_until = 0.0
        self._native_key_target: QObject | None = None
        self._native_key_suppression_until = 0.0
        self._installed = False
        self.errors = validate_bindings(bindings)
        self.errors.extend(validate_shortcut_sequences(bindings))
        if not self.errors:
            self.errors.extend(self._index_bindings())

    @staticmethod
    def _normalize_sequence(value: str) -> str:
        return normalize_shortcut_sequence(value)

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
        self._clear_context_menu_suppression()

    def _clear_context_menu_suppression(self) -> None:
        self._context_menu_target = None
        self._context_menu_suppression_until = 0.0
        self._native_key_target = None
        self._native_key_suppression_until = 0.0

    def _suppress_keyboard_context_menu(self, watched: QObject) -> None:
        self._context_menu_target = watched
        self._context_menu_suppression_until = time.monotonic() + _CONTEXT_MENU_SUPPRESSION_SECONDS

    def _suppress_native_keypress(self, watched: QObject) -> None:
        self._native_key_target = watched
        self._native_key_suppression_until = time.monotonic() + _NATIVE_KEY_SUPPRESSION_SECONDS

    def _consume_native_keypress(self, watched: QObject) -> bool:
        target = self._native_key_target
        if target is None:
            return False
        if time.monotonic() > self._native_key_suppression_until:
            self._native_key_target = None
            self._native_key_suppression_until = 0.0
            return False
        if watched is not target:
            return False
        self._native_key_target = None
        self._native_key_suppression_until = 0.0
        return True

    def _consume_keyboard_context_menu(self, watched: QObject, event) -> bool:
        target = self._context_menu_target
        if target is None:
            return False
        if time.monotonic() > self._context_menu_suppression_until:
            self._clear_context_menu_suppression()
            return False
        if (
            watched is not target
            or QApplication.activeWindow() is not self._window
            or event.reason() != QContextMenuEvent.Keyboard
        ):
            return False
        self._clear_context_menu_suppression()
        event.accept()
        return True

    def handle_native_shift_f10(self, watched: QObject | None = None) -> bool:
        """Dispatch a native Shift+F10 without re-entering Qt's menu path."""
        if QApplication.activeWindow() is not self._window:
            return False
        target = watched or QApplication.focusWidget()
        if target is None:
            return False
        candidates = self._by_key.get(_KEYBOARD_CONTEXT_MENU_KEY, ())
        matches = [item for item in candidates if self._context_matches(item)]
        binding = max(matches, key=lambda item: _CONTEXT_PRIORITY.get(item.context, 0), default=None)
        if binding is None:
            return False
        self._suppress_keyboard_context_menu(target)
        self._suppress_native_keypress(target)
        self._activate(binding)
        return True

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
        if event_type == QEvent.ContextMenu:
            return self._consume_keyboard_context_menu(watched, event)
        if event_type in (QEvent.ApplicationDeactivate, QEvent.WindowDeactivate):
            self._stop_repeats()
            self._clear_context_menu_suppression()
            return False
        if event_type not in (QEvent.KeyPress, QEvent.KeyRelease):
            return False
        key = self._event_sequence(event)
        if event_type == QEvent.KeyPress:
            if key == _KEYBOARD_CONTEXT_MENU_KEY and self._consume_native_keypress(watched):
                event.accept()
                return True
            self._clear_context_menu_suppression()
        if QApplication.activeWindow() is not self._window:
            return False

        candidates = self._by_key.get(key, ())
        matches = [item for item in candidates if self._context_matches(item)]
        binding = max(matches, key=lambda item: _CONTEXT_PRIORITY.get(item.context, 0), default=None)
        if binding is None:
            return False

        if event_type == QEvent.KeyPress and key == _KEYBOARD_CONTEXT_MENU_KEY:
            self._suppress_keyboard_context_menu(watched)

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
