"""PySide6 Client candidate UI wired to the existing Client business layer."""

from __future__ import annotations

import datetime as dt
import logging
import re
import sys
import threading
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QEasingCurve, QEvent, QModelIndex, QObject, QPropertyAnimation, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPalette, QPen
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListView,
    QMainWindow,
    QPushButton,
    QProgressBar,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
    QVBoxLayout,
    QWidget,
)

if __package__:
    from . import theme
else:
    project_root = Path(__file__).resolve().parents[2]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    from Client.ui_qt import theme

from Client.constants import (
    AUTH_EXPIRY_WARNING_SECONDS,
    DEFAULT_TS_HOST,
    DEFAULT_TS_PORT,
    DEFAULT_TS_WS_URL,
    HEARTBEAT_INTERVAL,
    LIVE_STATUSES,
    TS_RECONNECT_MAX_ATTEMPTS,
)

log = logging.getLogger("client.ui.main_window")
from Client.network.http_client import HttpClient
from Client.services.trading_session import TradingSession, safe_user_message, sanitize
from Client.ui_qt.action_rate_limiter import ActionRateLimiter
from Client.ui_qt.order_refresh_coordinator import OrderRefreshCoordinator
from Client.ui_qt.quote_subscription_coordinator import QuoteSubscriptionCoordinator
from Client.ui_qt.ts_connection_coordinator import TSConnectionCoordinator
from Client.ui_qt.hotkey_config_store import (
    HotkeyConfigLoadResult,
    load_hotkey_config,
    save_hotkey_config,
)
from Client.ui_qt.settings_overlay import ORDER_TIFS, SettingsOverlay, set_combo_item_enabled
from Client.ui_qt.hotkey_config import (
    BATCH_CANCEL_POLICY,
    DEFAULT_HOTKEY_CONFIG,
    ENTER_INPUT_GUARD_MS,
    HOTKEY_BINDINGS,
    IDENTICAL_ORDER_COOLDOWN_MS,
    IOC_QUOTE_REFRESH_TIMEOUT_MS,
    LIMIT_IOC_QUOTE_FRESHNESS_MS,
    ORDER_CANCEL_POLICY,
    ORDER_SUBMIT_POLICY,
    REFRESH_POLICY,
    HotkeyAction,
    HotkeyBinding,
    HotkeyContext,
    HotkeyRuntimeConfig,
    OrderHotkeyRule,
    bindings_from_config,
    format_hotkey_validation_errors,
    validate_hotkey_config,
)
from Client.ui_qt.shortcut_controller import ShortcutController, validate_shortcut_sequences


APP_ICON_PATH = Path(__file__).resolve().parents[1] / "assets" / "icons" / "sc-client.ico"


ACTION_LABELS = {
    "Buy to Open": "买开",
    "Buy to Close": "买平",
    "Sell to Open": "卖开",
    "Sell to Close": "卖平",
}

TIF_LABELS = {
    "Day": "当日有效",
    "GTC": "撤单前有效",
    "IOC": "立即成交或取消",
    "EXT": "盘前盘后",
    "GTC_EXT": "长期盘前盘后",
}

ORDER_STATUS_COLORS = {
    "Received": theme.ACCENT_BLUE,
    "Routing": theme.ACCENT_BLUE,
    "Live": theme.ACCENT_GREEN,
    "Partial": theme.ACCENT_YELLOW,
    "Cancelling": theme.ACCENT_YELLOW,
    "Filled": theme.ACCENT_GREEN,
    "Cancelled": theme.TEXT_MUTED,
    "Rejected": theme.ACCENT_RED,
    "Expired": theme.TEXT_LOW,
}


def default_ts_target() -> str:
    return DEFAULT_TS_WS_URL or f"{DEFAULT_TS_HOST}:{DEFAULT_TS_PORT}"


def localize_user_message(msg: str) -> str:
    text = sanitize(msg).strip()
    if not text:
        return ""

    replacements = {
        "Broker not connected": "交易服务未连接",
        "Broker status query timed out": "交易状态查询超时",
        "Quote subscribe failed": "行情订阅失败",
        "Quote unsubscribe failed": "行情取消订阅失败",
        "Position fetch failed": "持仓获取失败",
        "Duplicate order blocked by TS safety window": "相同订单提交过于频繁",
        "Server disconnected": "管理服务连接已断开",
        "Trade server connected": "交易服务器已连接",
        "Trade server disconnected": "交易服务器已断开",
        "Trade server connect failed": "交易服务器连接失败",
        "Trade server is offline": "交易服务器当前离线",
        "Trade server validation failed": "交易服务器校验失败",
        "Trade server lock failed; connection aborted": "交易服务器锁定失败，连接已中止",
        "Connected, sending auth...": "已连接，正在发送鉴权...",
        "Connected": "已连接",
        "Not connected": "未连接",
        "Order failed": "下单失败",
        "Cancel failed": "撤单失败",
    }
    if text in replacements:
        return replacements[text]

    reconnect_match = re.fullmatch(r"Reconnecting \((\d+)\)\.\.\.(.*)", text)
    if reconnect_match:
        suffix = ""
        if suffix.startswith("|"):
            suffix = f" | {suffix[1:].strip()}"
        elif suffix:
            suffix = f" {suffix}"
        return f"重连中（{reconnect_match.group(1)}）...{suffix}"

    connect_target_match = re.fullmatch(r"Connecting to (.+)\.\.\.", text)
    if connect_target_match:
        return "正在连接交易服务..."

    authenticated_match = re.fullmatch(r"Authenticated! Session: (.+)", text)
    if authenticated_match:
        return "交易服务鉴权成功"

    startswith_replacements = (
        ("Trade server is occupied by ", "交易服务器已被占用："),
        ("Trade server validation failed:", "交易服务器校验失败："),
        ("Trade server connect failed:", "交易服务器连接失败："),
        ("Quote subscribe failed:", "行情订阅失败："),
        ("Quote unsubscribe failed:", "行情取消订阅失败："),
        ("Position fetch failed:", "持仓获取失败："),
        ("Login failed (HTTP ", "登录失败（HTTP "),
        ("Order failed:", "下单失败："),
        ("Order submitted", "下单已提交"),
        ("Disconnected:", "连接断开："),
        ("Connection error", "连接错误"),
        ("Error [", "交易服务器错误["),
        ("Auth failed", "鉴权失败"),
    )
    for prefix, repl in startswith_replacements:
        if text.startswith(prefix):
            safe_prefix_messages = {
                "Auth failed": "交易服务认证失败，请重新登录",
                "Error [": "交易服务暂时不可用，请稍后重试",
                "Connection error": "交易服务连接异常，请稍后重试",
                "Disconnected:": "交易服务连接已断开",
                "Trade server validation failed:": "交易服务器校验失败，请稍后重试",
                "Trade server connect failed:": "交易服务器连接失败，请稍后重试",
                "Trade server is occupied by ": "交易服务器当前不可用，请联系管理员",
                "Login failed (HTTP ": "登录失败，请检查账号或稍后重试",
            }
            if prefix in safe_prefix_messages:
                return safe_prefix_messages[prefix]
            return safe_user_message(repl + text[len(prefix):], repl.rstrip("："))

    text = text.replace("TS not connected", "交易服务器未连接")
    text = text.replace("SE not connected", "交易服务器未连接")
    text = text.replace("remote host refused connection (port may not be ready)", "远程主机拒绝连接（端口可能尚未就绪）")
    text = re.sub(r"\bbroker\b", "交易服务", text, flags=re.I)
    text = text.replace("券商", "交易服务")
    return safe_user_message(text)

def make_label(text: str, *, color: str | None = None, font=None, object_name: str | None = None) -> QLabel:
    label = QLabel(text)
    if color:
        label.setStyleSheet(f"color: {color};")
    if font:
        label.setFont(font)
    if object_name:
        label.setObjectName(object_name)
    return label


def style_status_pill(label: QLabel, text: str, *, active: bool = False, danger: bool = False) -> None:
    if active:
        bg = theme.ACCENT_RED if danger else theme.ACCENT_GREEN
        fg = "#FFFFFF" if danger else theme.BUY_BUTTON_FG
        border = bg
    else:
        bg = theme.PANEL_ALT_BG
        fg = theme.TEXT_LOW
        border = theme.BORDER
    label.setText(text)
    label.setAlignment(Qt.AlignCenter)
    label.setMinimumHeight(30)
    label.setMinimumWidth(86)
    label.setStyleSheet(
        f"background: {bg}; color: {fg}; border: 1px solid {border}; "
        "border-radius: 8px; padding: 4px 10px;"
    )
    label.setFont(theme.mono_font(9, bold=True))


def make_status_pill(text: str, *, active: bool = False, danger: bool = False) -> QLabel:
    label = QLabel(text)
    style_status_pill(label, text, active=active, danger=danger)
    return label


def make_button(text: str, *, object_name: str | None = None, min_width: int | None = None) -> QPushButton:
    button = QPushButton(text)
    button.setCursor(Qt.PointingHandCursor)
    button.setMinimumHeight(34)
    if min_width:
        button.setMinimumWidth(min_width)
    if object_name:
        button.setObjectName(object_name)
    return button


class SettingsGearButton(QPushButton):
    def __init__(self, parent: QWidget | None = None):
        super().__init__("", parent)
        self.setObjectName("settingsGearButton")
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(28, 28)
        self.setToolTip("设置")
        self.setAccessibleName("设置")

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        color = QColor(theme.TEXT_PRIMARY if self.isDown() else theme.TEXT_LOW)
        if not self.isEnabled():
            color = QColor(theme.TEXT_LOW)
            color.setAlpha(120)
        painter.setPen(QPen(color, 1.4))
        center_x = self.width() / 2
        center_y = self.height() / 2
        radius = 5.0
        for dx, dy in (
            (0, -8), (5.7, -5.7), (8, 0), (5.7, 5.7),
            (0, 8), (-5.7, 5.7), (-8, 0), (-5.7, -5.7),
        ):
            painter.drawLine(
                int(center_x + dx * 0.62),
                int(center_y + dy * 0.62),
                int(center_x + dx),
                int(center_y + dy),
            )
        painter.drawEllipse(int(center_x - radius), int(center_y - radius), int(radius * 2), int(radius * 2))
        painter.drawEllipse(int(center_x - 1.7), int(center_y - 1.7), 3, 3)
        painter.end()


class TradePriceInput(QLineEdit):
    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and event.isAutoRepeat():
            event.accept()
            return
        super().keyPressEvent(event)


def make_input(
    text: str = "",
    *,
    password: bool = False,
    placeholder: str = "",
    field_type: type[QLineEdit] = QLineEdit,
) -> QLineEdit:
    field = field_type()
    field.setText(text)
    field.setPlaceholderText(placeholder)
    field.setMinimumHeight(40)
    if password:
        field.setEchoMode(QLineEdit.Password)
    return field


def make_select(value: str, values: list[str] | None = None) -> QComboBox:
    combo = QComboBox()
    popup = QListView(combo)
    popup.setObjectName("comboPopup")
    popup.setUniformItemSizes(True)
    popup.setMouseTracking(True)
    popup.setStyleSheet(theme.COMBO_POPUP_QSS)
    combo.setView(popup)
    combo.addItems(values or [value])
    combo.setCurrentText(value)
    combo.setMinimumHeight(44)
    combo.setMaxVisibleItems(8)
    return combo


class UiSignals(QObject):
    call = Signal(object)


class TradingPanelFrame(QFrame):
    activated = Signal()

    def mousePressEvent(self, event) -> None:
        self.activated.emit()
        super().mousePressEvent(event)


class DataTableModel(QAbstractTableModel):
    def __init__(self, headers: list[str], rows: list[list[object]] | None = None):
        super().__init__()
        self.headers = headers
        self.rows = rows or []
        self.cell_colors: list[list[str | None]] = []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self.rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self.headers)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        if role == Qt.TextAlignmentRole:
            return Qt.AlignCenter
        if role == Qt.ForegroundRole:
            try:
                color = self.cell_colors[index.row()][index.column()]
                return QColor(color) if color else None
            except (IndexError, TypeError):
                return None
        if role not in (Qt.DisplayRole, Qt.EditRole):
            return None
        try:
            return self.rows[index.row()][index.column()]
        except Exception:
            return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.headers[section]
        if role == Qt.TextAlignmentRole and orientation == Qt.Horizontal:
            return Qt.AlignCenter
        return None

    def set_rows(
        self,
        rows: list[list[object]],
        cell_colors: list[list[str | None]] | None = None,
    ) -> None:
        self.beginResetModel()
        self.rows = rows
        self.cell_colors = cell_colors or []
        self.endResetModel()


class NoFocusRectItemDelegate(QStyledItemDelegate):
    """Keep item-view focus behavior while suppressing its dotted focus frame."""

    def paint(self, painter, option, index) -> None:
        clean_option = QStyleOptionViewItem(option)
        clean_option.state &= ~QStyle.State_HasFocus
        super().paint(painter, clean_option, index)


class TradingSlot:
    def __init__(self, panel_id: int):
        self.panel_id = panel_id
        self.current_symbol = ""
        self.container: QFrame | None = None
        self.qty_box: QFrame | None = None
        self.symbol: QComboBox | None = None
        self.order_type: QComboBox | None = None
        self.route: QComboBox | None = None
        self.tif: QComboBox | None = None
        self.qty_label: QLineEdit | None = None
        self.hidden_order: QCheckBox | None = None
        self.hidden_order_caption: QLabel | None = None
        self.price: QLineEdit | None = None
        self.bid: QLabel | None = None
        self.ask: QLabel | None = None
        self.buy: QPushButton | None = None
        self.sell: QPushButton | None = None
        self.pending_action = ""
        self.enter_target = "NONE"
        self.pending_symbol = ""
        self.pending_order_type = ""
        self.pending_route = ""
        self.pending_hidden = False
        self.pending_created_at = 0.0
        self.confirm_guard_until = 0.0
        self.trade_enabled = False
        self.symbol_pending = False
        self.quote_query_button: QPushButton | None = None

    def symbol_text(self) -> str:
        return self.symbol.currentText().strip().upper() if self.symbol else ""

    def qty_value(self) -> int:
        text = self.qty_label.text().strip() if self.qty_label else "0"
        try:
            return int(float(text))
        except ValueError:
            return 0

    def set_qty(self, qty: int) -> None:
        if self.qty_label:
            self.qty_label.setText(str(max(1, int(qty))))

    def price_value(self) -> float:
        if self.order_type and self.order_type.currentText() == "Market":
            return 0.0
        text = self.price.text().strip() if self.price else ""
        try:
            return float(text)
        except ValueError:
            return 0.0

    def set_trade_enabled(self, enabled: bool) -> None:
        self.trade_enabled = bool(enabled)
        self._apply_trade_enabled()

    def _apply_trade_enabled(self) -> None:
        symbol_text_fn = getattr(self.symbol, "currentText", None)
        symbol_confirmed = True
        if callable(symbol_text_fn):
            symbol_confirmed = bool(
                self.current_symbol
                and self.current_symbol == self.symbol_text()
            )
        enabled = bool(
            self.trade_enabled
            and not self.symbol_pending
            and symbol_confirmed
        )
        for widget in (self.buy, self.sell):
            if widget:
                widget.setEnabled(enabled)

    def set_symbol_pending(self, pending: bool) -> None:
        self.symbol_pending = bool(pending)
        self._apply_trade_enabled()

    def clear_quote(self) -> None:
        for label in (self.bid, self.ask):
            if label:
                label.setText("--")

    def update_quote(self, quote: dict) -> None:
        if self.bid:
            text = f"{float(quote.get('bid', 0) or 0):.2f}"
            if self.bid.text() != text:
                self.bid.setText(text)
        if self.ask:
            text = f"{float(quote.get('ask', 0) or 0):.2f}"
            if self.ask.text() != text:
                self.ask.setText(text)



class TradingTerminalQt(QMainWindow):
    def __init__(self):
        super().__init__()
        theme.load_fonts()
        self.setWindowTitle("SC - Qt Client")
        self.resize(1360, 860)
        self.setMinimumSize(1180, 740)
        self.setStyleSheet(theme.APP_QSS)

        self.http = HttpClient()
        self.session: TradingSession | None = None
        self._signals = UiSignals()
        self._signals.call.connect(lambda fn: fn())
        self._clock: QLabel | None = None
        self.slots: dict[int, TradingSlot] = {}
        self._active_panel_id = 1
        self._shortcut_controller: ShortcutController | None = None
        self._hotkey_bindings: tuple[HotkeyBinding, ...] = HOTKEY_BINDINGS
        self._hotkey_config_result: HotkeyConfigLoadResult | None = None
        self._hotkey_config: HotkeyRuntimeConfig = DEFAULT_HOTKEY_CONFIG
        self._settings_overlay: SettingsOverlay | None = None
        self._action_limiter = ActionRateLimiter()
        self._ts_connection = TSConnectionCoordinator(
            http_client=self.http,
            session_provider=lambda: self.session,
            username_provider=lambda: self._login_username,
            reconnect_allowed_provider=lambda: not self._reconnect_failed,
            background_runner=self._run_bg,
            parent=self,
        )
        self._ts_connection.validation_started.connect(
            self._on_ts_validation_started
        )
        self._ts_connection.connection_failed.connect(self._on_ts_connection_failed)
        self._ts_connection.status_received.connect(self._route_ts_status)
        self._ts_connection.message_received.connect(self._route_ts_message)
        self._ts_connection.latency_received.connect(self._route_ts_latency)
        self._ts_connection.state_changed.connect(self._route_ts_state)
        self._order_refresh = OrderRefreshCoordinator(
            session_provider=lambda: self.session,
            generation_provider=lambda: self._se_generation,
            background_runner=self._run_bg,
            parent=self,
        )
        self._order_refresh.orders_ready.connect(self._update_orders)
        self._order_refresh.positions_ready.connect(self._update_positions)
        self._order_refresh.orders_failed.connect(self._handle_orders_refresh_failed)
        self._order_refresh.positions_failed.connect(self._handle_positions_refresh_failed)
        self._quote_subscriptions = QuoteSubscriptionCoordinator(
            session_provider=lambda: self.session,
            generation_provider=lambda: self._se_generation,
            connected_provider=lambda: self._se_connected,
            background_runner=self._run_bg,
            parent=self,
        )
        self._quote_subscriptions.symbol_result.connect(self._handle_quote_symbol_result)
        self._quote_subscriptions.sync_failed.connect(self._handle_quote_sync_failed)
        self._quote_subscriptions.subscriptions_changed.connect(self._handle_quote_subscriptions_changed)
        self._canceling_order_ids: set[str] = set()
        self._batch_canceling_symbols: set[str] = set()
        self._direct_quote_refresh_requests: dict[int, str] = {}
        self._direct_quote_refresh_serial = 0
        self._quick_order_flash_tokens: dict[int, tuple[int, str]] = {}
        self._quick_order_flash_serial = 0
        self._quick_order_flash_timers: dict[int, QTimer] = {}
        self._log_rows: list[tuple[str, str, str]] = []
        self._main_ui_built = False
        self._init_ready = False
        self._startup_login_required = True
        self._login_dialog_open = False
        self._login_username = ""
        self._login_password = ""
        self._last_heartbeat = 0.0
        self._last_ui_error_message = ""
        self._last_ui_error_at = 0.0
        self._last_reconnect_notice_attempt = 0
        self._reconnect_failed = False
        self._auth_warning_shown = False
        self._auth_exit_started = False
        self._logout_in_progress = False
        self._connection_status_label = "OFFLINE"
        self._orders_raw: list[dict] = []
        self._positions_raw: list[dict] = []
        self.current_quote: dict[str, dict] = {}
        self._toast_widgets: list[QFrame] = []
        self._toast_animations: list[QPropertyAnimation] = []
        self._resize_effect_timer = QTimer(self)
        self._resize_effect_timer.setSingleShot(True)
        self._resize_effect_timer.timeout.connect(self._restore_resize_effects)
        self._window_state_layout_timer = QTimer(self)
        self._window_state_layout_timer.setSingleShot(True)
        self._window_state_layout_timer.timeout.connect(self._finalize_window_state_layout)

        app = QApplication.instance()
        if app is not None:
            app.focusChanged.connect(self._on_focus_changed)

        self._build_login_root()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)
        self._tick()

        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll)
        self._poll_timer.start(150)

        self._quote_ui_timer = QTimer(self)
        self._quote_ui_timer.timeout.connect(self._flush_quote_updates)
        self._quote_ui_timer.start(50)

        QTimer.singleShot(200, self._show_startup_login)

    def _ui(self, fn) -> None:
        self._signals.call.emit(fn)

    def _run_bg(self, fn) -> None:
        threading.Thread(target=fn, daemon=True).start()

    @property
    def _se_client(self):
        return self._ts_connection.client

    @property
    def _se_generation(self) -> int:
        return self._ts_connection.generation

    @property
    def _se_connected(self) -> bool:
        return self._ts_connection.connected

    @_se_connected.setter
    def _se_connected(self, value: bool) -> None:
        self._ts_connection.connected = value

    @property
    def _se_target_address(self) -> str:
        return self._ts_connection.target_address

    @_se_target_address.setter
    def _se_target_address(self, value: str) -> None:
        self._ts_connection.target_address = value

    @property
    def _se_server_id(self) -> str:
        return self._ts_connection.server_id

    @property
    def _se_connection_id(self) -> str:
        return self._ts_connection.connection_id

    @property
    def _last_connected_se(self) -> str:
        return self._ts_connection.last_endpoint

    def _show_login_page(self) -> None:
        if hasattr(self, "_login_form") and self._login_form:
            self._login_form.show()
        if hasattr(self, "_init_status") and self._init_status:
            self._init_status.hide()
        self._set_init_actions_visible(False)
        self._set_init_hint("")
        self._update_init_step("auth", "\u7b49\u5f85\u4e2d", theme.TEXT_MUTED)
        self._update_init_step("sm", "\u7b49\u5f85\u4e2d", theme.TEXT_MUTED)
        self._update_init_step("se", "\u7b49\u5f85\u4e2d", theme.TEXT_MUTED)
        if self._login_submit_btn:
            self._login_submit_btn.setEnabled(True)
            self._login_submit_btn.setText("\u767b\u5f55")
        if self._login_exit_btn:
            self._login_exit_btn.setEnabled(True)
        if self._login_user_entry:
            self._login_user_entry.setFocus()

    def _show_connection_page(self) -> None:
        if hasattr(self, "_login_form") and self._login_form:
            self._login_form.hide()
        if hasattr(self, "_init_status") and self._init_status:
            self._init_status.show()

    def _submit_inline_login(self) -> None:
        username = self._login_user_entry.text().strip() if self._login_user_entry else ""
        password = self._login_pass_entry.text() if self._login_pass_entry else ""
        if not username or not password:
            self._set_init_hint("请输入账号和密码")
            return
        self._set_init_hint("")
        self._set_auth_notice("")
        self._show_connection_page()
        self._startup_login_required = True
        self._update_init_step("auth", "登录中...", theme.ACCENT_YELLOW)
        if self._login_submit_btn:
            self._login_submit_btn.setEnabled(False)
            self._login_submit_btn.setText("登录中...")
        if self._login_exit_btn:
            self._login_exit_btn.setEnabled(False)
        self._run_bg(lambda: self._login_manager(username, password, False))

    def _update_init_step(self, key: str, status: str, color: str | None = None) -> None:
        if key not in self._init_steps:
            return
        _name, label = self._init_steps[key]
        label.setText(status)
        if color:
            label.setStyleSheet(f"color: {color};")

    def _set_init_hint(self, text: str) -> None:
        if self._init_hint_label:
            self._init_hint_label.setText(text)

    @staticmethod
    def _make_auth_notice_label(text: str) -> QLabel:
        label = make_label(text, color=theme.ACCENT_YELLOW, font=theme.ui_font(11, bold=True))
        label.setAlignment(Qt.AlignCenter)
        label.setWordWrap(True)
        label.setMinimumHeight(24)
        label.setStyleSheet(f"color: {theme.ACCENT_YELLOW}; background: transparent; border: none;")
        return label

    def _set_auth_notice(self, text: str) -> None:
        label = getattr(self, "_auth_notice_label", None)
        if label is not None:
            label.setText(str(text or ""))

    def _set_init_actions_visible(self, visible: bool) -> None:
        for btn in (self._retry_btn, self._cancel_btn):
            if btn:
                btn.setVisible(visible)

    def _build_root(self) -> None:
        root = self._create_root_widget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_header())
        layout.addWidget(self._build_workspace(), 1)

    @staticmethod
    def _create_root_widget() -> QWidget:
        root = QWidget()
        root.setObjectName("root")
        root.setAttribute(Qt.WA_StyledBackground, True)
        palette = root.palette()
        palette.setColor(QPalette.Window, QColor(theme.TERM_BG))
        root.setPalette(palette)
        root.setAutoFillBackground(True)
        return root

    @staticmethod
    def _repolish(widget: QWidget | None) -> None:
        if widget is None:
            return
        style = widget.style()
        style.unpolish(widget)
        style.polish(widget)
        widget.update()

    def _activate_panel(self, panel_id: int) -> None:
        if panel_id not in self.slots:
            return
        self._active_panel_id = panel_id
        for pid, slot in self.slots.items():
            if slot.container:
                active = pid == panel_id
                slot.container.setProperty("activePanel", active)
                effect = slot.container.graphicsEffect()
                if isinstance(effect, QGraphicsDropShadowEffect):
                    effect.setEnabled(active)
                    effect.setBlurRadius(16)
                    effect.setColor(QColor(245, 189, 67, 104))
                self._repolish(slot.container)

    def _set_live_orders_online(self, online: bool) -> None:
        if not hasattr(self, "live_orders_btn"):
            return
        self.live_orders_btn.setProperty("online", bool(online))
        self._repolish(self.live_orders_btn)

    def _active_slot(self) -> TradingSlot | None:
        return self.slots.get(self._active_panel_id)

    def _cycle_active_panel(self) -> None:
        if not self.slots:
            return
        ids = sorted(self.slots)
        try:
            current_index = ids.index(self._active_panel_id)
        except ValueError:
            current_index = -1
        next_id = ids[(current_index + 1) % len(ids)]
        self._activate_panel(next_id)
        slot = self._active_slot()
        if slot and slot.symbol:
            slot.symbol.setFocus()

    @staticmethod
    def _widget_is_within(widget: QWidget | None, container: QWidget | None) -> bool:
        if widget is None or container is None:
            return False
        return widget is container or container.isAncestorOf(widget)

    def _on_focus_changed(self, _old: QWidget | None, current: QWidget | None) -> None:
        if not self._main_ui_built or current is None:
            return
        for pid, slot in self.slots.items():
            if self._widget_is_within(current, slot.container):
                self._activate_panel(pid)
                if slot.symbol and (current is slot.symbol or current is slot.symbol.lineEdit()):
                    self._set_enter_target(pid, "SYMBOL")
                return

    def _setup_shortcuts(self) -> None:
        self._teardown_shortcuts()
        scope_id = self._active_config_scope()
        if scope_id:
            load_result = load_hotkey_config(HOTKEY_BINDINGS, scope_id=scope_id)
            self._hotkey_config_result = load_result
            self._hotkey_config = load_result.config
            self._hotkey_bindings = load_result.bindings
            if load_result.errors:
                self._append_log("快捷键配置无效，已恢复默认配置", "warn", dedupe=True)
        else:
            # This is only reachable in tests or before a completed SM login.
            self._hotkey_config_result = None
            self._hotkey_config = DEFAULT_HOTKEY_CONFIG
            self._hotkey_bindings = HOTKEY_BINDINGS
        controller = ShortcutController(
            self,
            self._hotkey_bindings,
            self._dispatch_hotkey,
            self._shortcut_context_matches,
        )
        self._shortcut_controller = controller
        if controller.errors:
            messages = format_hotkey_validation_errors(controller.errors)
            self._append_log(f"快捷键配置无效：{'；'.join(messages)}", "warn")
            return
        controller.install()

    def _teardown_shortcuts(self) -> None:
        if self._shortcut_controller:
            self._shortcut_controller.shutdown()
            self._shortcut_controller.deleteLater()
            self._shortcut_controller = None

    def _open_settings_overlay(self) -> None:
        if not self._main_ui_built:
            return
        if self._settings_overlay is not None:
            self._settings_overlay.raise_()
            self._settings_overlay.setFocus()
            return
        self._teardown_shortcuts()
        parent = self.centralWidget()
        if parent is None:
            return
        overlay = SettingsOverlay(
            parent,
            config=self._hotkey_config,
            route_options=self._current_route_options(),
            route_effective=self._route_editable(),
            hidden_effective=self._hidden_order_supported(),
            supported_tifs=self._broker_supported_tifs(),
        )
        overlay.close_requested.connect(self._close_settings_overlay)
        overlay.save_requested.connect(self._save_settings_config)
        self._settings_overlay = overlay
        self._sync_settings_overlay_geometry()
        overlay.show()
        overlay.raise_()
        overlay.setFocus()

    def _close_settings_overlay(self) -> None:
        overlay = self._settings_overlay
        self._settings_overlay = None
        if overlay is not None:
            overlay.hide()
            overlay.deleteLater()
        if self._main_ui_built:
            self._setup_shortcuts()

    def _save_settings_config(self, config: HotkeyRuntimeConfig) -> None:
        errors = validate_hotkey_config(config)
        errors.extend(validate_shortcut_sequences(bindings_from_config(config)))
        if errors:
            message = "；".join(format_hotkey_validation_errors(errors))
            if self._settings_overlay:
                self._settings_overlay.set_error(message)
            self._show_weak_tip(f"快捷键配置未保存：{message}", "warn")
            return
        scope_id = self._active_config_scope()
        if not scope_id:
            message = "当前登录信息未准备完成，快捷键配置未保存"
            if self._settings_overlay:
                self._settings_overlay.set_error(message)
            self._show_weak_tip(message, "warn")
            return
        try:
            save_hotkey_config(config, scope_id=scope_id)
        except Exception as exc:
            message = "快捷键配置保存失败，请检查文件权限或稍后重试"
            if self._settings_overlay:
                self._settings_overlay.set_error(message)
            self._show_weak_tip(message, "warn")
            return
        self._hotkey_config = config
        self._hotkey_bindings = bindings_from_config(config)
        self._append_log("快捷键配置已保存", "ok")
        self._show_weak_tip("快捷键配置已保存", "ok")
        self._close_settings_overlay()

    def _active_config_scope(self) -> str:
        session = self.session
        scope_id = str(getattr(session, "config_scope", "") or "").strip().lower()
        return scope_id if re.fullmatch(r"[a-f0-9]{64}", scope_id) else ""

    def _sync_settings_overlay_geometry(self) -> None:
        if self._settings_overlay is None:
            return
        parent = self._settings_overlay.parentWidget()
        if parent is not None:
            self._settings_overlay.setGeometry(parent.rect())

    def _shortcut_context_matches(self, binding: HotkeyBinding) -> bool:
        if not self._main_ui_built or QApplication.activeWindow() is not self:
            return False
        if QApplication.activeModalWidget() is not None or QApplication.activePopupWidget() is not None:
            return False
        focus = QApplication.focusWidget()
        slot = self._active_slot()
        context = binding.context
        if context == HotkeyContext.MAIN_WINDOW:
            return True
        if slot is None:
            return False
        if context == HotkeyContext.TRADE_PANEL:
            return self._widget_is_within(focus, slot.container)
        if context == HotkeyContext.SYMBOL_INPUT:
            return bool(slot.symbol and (focus is slot.symbol or focus is slot.symbol.lineEdit()))
        if context == HotkeyContext.QUANTITY_CONTROL:
            return self._widget_is_within(focus, slot.qty_box)
        if context == HotkeyContext.PRICE_INPUT:
            return focus is slot.price
        if context == HotkeyContext.ORDERS_TABLE:
            return hasattr(self, "orders_table") and self._widget_is_within(focus, self.orders_table)
        if context == HotkeyContext.POSITIONS_TABLE:
            return hasattr(self, "positions_table") and self._widget_is_within(focus, self.positions_table)
        return False

    def _dispatch_hotkey(self, binding: HotkeyBinding) -> None:
        action = binding.action
        params = binding.params
        panel_id = int(params.get("panel_id") or self._active_panel_id)
        if action == HotkeyAction.PANEL_CYCLE:
            self._cycle_active_panel()
        elif action == HotkeyAction.PANEL_ACTIVATE:
            self._activate_panel(panel_id)
            slot = self._active_slot()
            if slot and slot.symbol:
                slot.symbol.setFocus()
        elif action == HotkeyAction.ORDER_MARKET:
            self._submit_market_order(str(params.get("side") or ""), panel_id)
        elif action == HotkeyAction.ORDER_PREPARE_LIMIT:
            self._prepare_limit_order(
                str(params.get("side") or ""),
                panel_id,
                str(params.get("price_source") or ""),
            )
        elif action == HotkeyAction.ORDER_PREPARE_RULE:
            self._prepare_configured_order(params, panel_id)
        elif action == HotkeyAction.ORDER_CONFIRM_PENDING:
            self._confirm_pending_order(panel_id)
        elif action == HotkeyAction.ORDER_CANCEL_PENDING:
            self._cancel_pending_order(panel_id, log=True, clear_enter_target=True)
        elif action == HotkeyAction.ENTER_TARGET:
            self._handle_enter_target(panel_id)
        elif action == HotkeyAction.ORDER_CANCEL_SELECTED:
            self._cancel_selected_order()
        elif action == HotkeyAction.ORDER_CANCEL_SYMBOL_LIVE:
            self._cancel_symbol_live_orders(panel_id)
        elif action == HotkeyAction.QUANTITY_SET:
            self._set_qty(int(params.get("value") or 0), panel_id)
        elif action == HotkeyAction.QUANTITY_ADJUST:
            self._adj_qty(int(params.get("delta") or 0), panel_id)
        elif action == HotkeyAction.PRICE_ADJUST:
            self._adj_price(float(params.get("delta") or 0), panel_id)
        elif action == HotkeyAction.ORDERS_SWITCH_MODE:
            self._switch_order_mode(str(params.get("mode") or "live"))
        elif action == HotkeyAction.REFRESH_ORDERS:
            self._refresh_orders(force=True)
        elif action == HotkeyAction.REFRESH_POSITIONS:
            self._refresh_positions(force_orders=True)
        elif action == HotkeyAction.REFRESH_ALL:
            self._refresh_broker_status_async(log_errors=False)
            self._refresh_orders(force=True)
            self._refresh_positions(force_orders=True)
        elif action == HotkeyAction.LOGS_CLEAR:
            self._clear_logs()

    def _build_header(self) -> QFrame:
        header = QFrame()
        header.setObjectName("topHeader")
        header.setFixedHeight(58)
        layout = QHBoxLayout(header)
        layout.setContentsMargins(14, 8, 14, 8)
        layout.setSpacing(16)

        switch = QFrame()
        switch.setStyleSheet(f"background: {theme.PANEL_ALT_BG}; border: 1px solid {theme.BORDER}; border-radius: 8px;")
        switch_layout = QHBoxLayout(switch)
        switch_layout.setContentsMargins(3, 3, 3, 3)
        switch_layout.setSpacing(3)
        self.account_state = make_status_pill("Disconnect", active=True, danger=True)
        switch_layout.addWidget(self.account_state)
        layout.addWidget(switch)

        status = QWidget()
        status_layout = QHBoxLayout(status)
        status_layout.setContentsMargins(0, 0, 0, 0)
        status_layout.setSpacing(8)
        self.status_dot = make_label("\u25cf", color=theme.ACCENT_RED, font=theme.ui_font(9, bold=True))
        self.status_text = make_label("OFFLINE", color=theme.ACCENT_RED, font=theme.mono_font(9, bold=True))
        self.read_only_label = make_label("READ ONLY", color=theme.ACCENT_YELLOW, font=theme.mono_font(9, bold=True))
        self.read_only_label.hide()
        status_layout.addWidget(self.status_dot)
        status_layout.addWidget(self.status_text)
        status_layout.addWidget(self.read_only_label)
        layout.addWidget(status)

        self.latency_label = make_label("--ms", color=theme.TEXT_LOW, font=theme.mono_font(9))
        layout.addWidget(self.latency_label)

        layout.addStretch(1)
        self.settings_btn = SettingsGearButton()
        self.settings_btn.clicked.connect(self._open_settings_overlay)
        layout.addWidget(self.settings_btn)
        self.logout_btn = make_button("退出登录", object_name="logoutButton", min_width=80)
        self.logout_btn.setMinimumHeight(28)
        self.logout_btn.setMaximumHeight(28)
        self.logout_btn.setFont(theme.ui_font(9, bold=True))
        self.logout_btn.clicked.connect(self._confirm_logout)
        layout.addWidget(self.logout_btn, 0, Qt.AlignVCenter)

        clock = QWidget()
        clock_layout = QHBoxLayout(clock)
        clock_layout.setContentsMargins(0, 0, 0, 0)
        clock_layout.setSpacing(8)
        clock_layout.addWidget(make_label("CN Time", color=theme.TEXT_MUTED, font=theme.ui_font(9)))
        self._clock = QLabel()
        self._clock.setFont(theme.mono_font(9))
        self._clock.setAlignment(Qt.AlignCenter)
        self._clock.setMinimumWidth(158)
        self._clock.setMinimumHeight(34)
        self._clock.setStyleSheet(
            f"background: #080A0D; border: 1px solid {theme.BORDER_SOFT}; "
            "border-radius: 7px; padding: 3px 10px; color: #F1F3F5;"
        )
        self._tick()
        clock_layout.addWidget(self._clock)
        layout.addWidget(clock)
        return header


    def _build_workspace(self) -> QWidget:
        workspace = QWidget()
        layout = QVBoxLayout(workspace)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(14)

        slot_row = QHBoxLayout()
        slot_row.setSpacing(20)
        slot_row.addWidget(self._build_slot(1, "", "100", ""))
        slot_row.addWidget(self._build_slot(2, "", "1", ""))
        layout.addLayout(slot_row)

        middle = QHBoxLayout()
        middle.setSpacing(20)
        middle.addWidget(self._build_orders_panel(), 1)
        middle.addWidget(self._build_positions_panel(), 1)
        layout.addLayout(middle, 1)

        layout.addWidget(self._build_console())
        self._activate_panel(1)
        return workspace

    def _build_slot(self, idx: int, symbol: str, qty: str, price: str) -> QFrame:
        slot = TradingSlot(idx)
        self.slots[idx] = slot
        card = TradingPanelFrame()
        card.setObjectName("slotCard")
        card.setProperty("activePanel", False)
        glow = QGraphicsDropShadowEffect(card)
        glow.setBlurRadius(14)
        glow.setOffset(0, 0)
        glow.setColor(QColor(245, 189, 67, 72))
        card.setGraphicsEffect(glow)
        card.activated.connect(lambda pid=idx: self._activate_panel(pid))
        slot.container = card
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 17, 18, 17)
        layout.setSpacing(0)

        slot_grid = QGridLayout()
        slot_grid.setHorizontalSpacing(14)
        slot_grid.setVerticalSpacing(14)
        slot_grid.setColumnStretch(0, 5)
        slot_grid.setColumnStretch(1, 9)

        slot.symbol = make_select(symbol, [symbol])
        slot.symbol.setEditable(True)
        slot.symbol.lineEdit().returnPressed.connect(lambda pid=idx: self._on_symbol_enter(pid))
        symbol_control = QWidget()
        symbol_layout = QHBoxLayout(symbol_control)
        symbol_layout.setContentsMargins(0, 0, 0, 0)
        symbol_layout.setSpacing(8)
        symbol_layout.addWidget(slot.symbol, 1)
        slot.quote_query_button = self._build_quote_query_button(idx)
        symbol_layout.addWidget(slot.quote_query_button, 0)
        slot.symbol.currentTextChanged.connect(lambda _text, pid=idx: self._on_symbol_text_changed(pid))
        slot_grid.addWidget(self._control_block("SYMBOL", symbol_control), 0, 0)

        quote_box, slot.bid, slot.ask = self._build_quote_box()
        slot_grid.addWidget(quote_box, 0, 1)

        slot.order_type = make_select("Limit", ["Limit", "Market"])
        slot.order_type.currentTextChanged.connect(lambda _text, pid=idx: self._on_order_type_change(pid))
        slot_grid.addWidget(self._control_block("TYPE", slot.order_type), 1, 0)

        right_config = QWidget()
        right_config_layout = QHBoxLayout(right_config)
        right_config_layout.setContentsMargins(0, 0, 0, 0)
        right_config_layout.setSpacing(10)
        slot.route = make_select("SMART", ["SMART"])
        right_config_layout.addWidget(self._control_block("ROUTE", slot.route), 1)
        slot.tif = make_select("Day", ["Day", "GTC", "IOC", "EXT", "GTC_EXT"])
        right_config_layout.addWidget(self._control_block("TIF", slot.tif), 1)

        qty_box, slot.qty_label = self._build_qty(qty)
        slot.qty_box = qty_box
        right_config_layout.addWidget(self._control_block("QTY", qty_box), 1)

        hide_block = QWidget()
        hide_block.setFixedWidth(52)
        hide_layout = QVBoxLayout(hide_block)
        hide_layout.setContentsMargins(0, 0, 0, 0)
        hide_layout.setSpacing(7)
        slot.hidden_order_caption = make_label("HIDE", color=theme.TEXT_LOW, object_name="hiddenOrderCaption")
        slot.hidden_order_caption.setAlignment(Qt.AlignCenter)
        slot.hidden_order = QCheckBox()
        slot.hidden_order.setObjectName("hiddenOrderCheck")
        slot.hidden_order.setEnabled(False)
        slot.hidden_order.setToolTip("当前交易通道不支持 HIDE 订单")
        hide_layout.addWidget(slot.hidden_order_caption)
        hide_layout.addWidget(slot.hidden_order, 0, Qt.AlignCenter)
        right_config_layout.addWidget(hide_block, 0)
        slot_grid.addWidget(right_config, 1, 1)

        slot.price = make_input(price, field_type=TradePriceInput)
        slot.price.returnPressed.connect(lambda pid=idx: self._handle_enter_target(pid))
        slot_grid.addWidget(self._control_block("PRICE", slot.price), 2, 0)

        buttons = QWidget()
        buttons_layout = QVBoxLayout(buttons)
        buttons_layout.setContentsMargins(0, 0, 0, 0)
        buttons_layout.setSpacing(7)
        buttons_layout.addWidget(make_label("", object_name="caption"))
        button_row = QWidget()
        button_row_layout = QHBoxLayout(button_row)
        button_row_layout.setContentsMargins(0, 0, 0, 0)
        button_row_layout.setSpacing(14)
        slot.buy = make_button("BUY", object_name="buyButton")
        slot.sell = make_button("SELL", object_name="sellButton")
        slot.buy.setFocusPolicy(Qt.NoFocus)
        slot.sell.setFocusPolicy(Qt.NoFocus)
        slot.buy.setMinimumHeight(44)
        slot.sell.setMinimumHeight(44)
        slot.buy.clicked.connect(lambda _checked=False, pid=idx: self._place_order_from_panel("Buy to Open", pid))
        slot.sell.clicked.connect(lambda _checked=False, pid=idx: self._place_order_from_panel("Sell to Close", pid))
        button_row_layout.addWidget(slot.buy, 1)
        button_row_layout.addWidget(slot.sell, 1)
        buttons_layout.addWidget(button_row)
        slot_grid.addWidget(buttons, 2, 1)

        layout.addLayout(slot_grid)
        return card

    def _build_quote_box(self) -> tuple[QFrame, QLabel, QLabel]:
        box = QFrame()
        box.setObjectName("inputBox")
        box.setMinimumHeight(72)
        layout = QGridLayout(box)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setHorizontalSpacing(8)
        labels: list[QLabel] = []
        for col, (caption, color) in enumerate((("BID", theme.ACCENT_GREEN), ("ASK", theme.ACCENT_RED))):
            layout.addWidget(make_label(caption, color=theme.TEXT_LOW, font=theme.mono_font(9)), 0, col, alignment=Qt.AlignHCenter | Qt.AlignBottom)
            value = make_label("--", color=color, font=theme.mono_font(11))
            value.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
            layout.addWidget(value, 1, col, alignment=Qt.AlignHCenter | Qt.AlignTop)
            layout.setColumnStretch(col, 1)
            labels.append(value)
        return box, labels[0], labels[1]

    def _build_qty(self, value: str) -> tuple[QFrame, QLineEdit]:
        box = QFrame()
        box.setObjectName("inputBox")
        box.setMinimumHeight(44)
        layout = QHBoxLayout(box)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(3)
        qty = QLineEdit(value)
        qty.setObjectName("qtyInput")
        qty.setAlignment(Qt.AlignCenter)
        qty.setFont(theme.mono_font(11))
        qty.setMinimumWidth(34)
        qty.setMaxLength(7)
        qty.editingFinished.connect(lambda field=qty: self._normalize_qty_field(field))
        layout.addWidget(qty, 1)
        return box, qty

    def _build_quote_query_button(self, panel_id: int) -> QPushButton:
        button = QPushButton()
        button.setObjectName("quoteQueryButton")
        button.setFixedSize(40, 44)
        button.setFocusPolicy(Qt.NoFocus)
        button.setCursor(Qt.PointingHandCursor)
        button.setToolTip("查询行情")
        button.setAccessibleName("查询行情")
        icon_path = Path(__file__).resolve().parents[1] / "assets" / "icons" / "search.svg"
        if icon_path.is_file():
            button.setIcon(QIcon(str(icon_path)))
            button.setIconSize(QSize(18, 18))
        button.clicked.connect(lambda _checked=False, pid=panel_id: self._on_symbol_query_requested(pid))
        return button

    def _normalize_qty_field(self, field: QLineEdit) -> None:
        try:
            qty = int(float(field.text().strip() or "0"))
        except ValueError:
            qty = 1
        field.setText(str(max(1, qty)))

    def _control_block(self, caption: str, widget: QWidget) -> QWidget:
        block = QWidget()
        layout = QVBoxLayout(block)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(7)
        layout.addWidget(make_label(caption, object_name="caption"))
        layout.addWidget(widget)
        return block
    def _build_orders_panel(self) -> QFrame:
        panel, body = self._make_data_panel()
        head = self._make_panel_header()
        tabs = QHBoxLayout(head)
        tabs.setContentsMargins(12, 7, 12, 7)
        tabs.setSpacing(6)
        self.live_orders_btn = make_button("\u25cf \u8fdb\u884c\u4e2d", object_name="liveOrdersButton")
        self.live_orders_btn.setProperty("online", False)
        self.filled_orders_btn = make_button("成交", object_name="orderTabButton")
        self.inactive_orders_btn = make_button("失效", object_name="orderTabButton")
        self.all_orders_btn = make_button("All")
        self.all_orders_btn.setObjectName("orderTabButton")
        self._order_mode_buttons = {
            "live": self.live_orders_btn,
            "filled": self.filled_orders_btn,
            "inactive": self.inactive_orders_btn,
            "all": self.all_orders_btn,
        }
        self.order_count_label = make_label("\u6682\u65e0\u8ba2\u5355", color=theme.TEXT_DIM, font=theme.ui_font(9))
        self.cancel_order_btn = make_button("\u64a4\u5355", object_name="cancelOrderButton", min_width=60)
        self.cancel_order_btn.setFixedHeight(21)
        self.cancel_order_btn.setFont(theme.ui_font(8, bold=True))
        self.orders_refresh_btn = make_button("\u21bb", object_name="refreshIconButton")
        self.orders_refresh_btn.setFixedSize(32, 32)
        self.orders_refresh_btn.setToolTip("刷新订单")
        self.orders_refresh_btn.setAccessibleName("刷新订单")
        self.live_orders_btn.clicked.connect(lambda: self._switch_order_mode("live"))
        self.filled_orders_btn.clicked.connect(lambda: self._switch_order_mode("filled"))
        self.inactive_orders_btn.clicked.connect(lambda: self._switch_order_mode("inactive"))
        self.all_orders_btn.clicked.connect(lambda: self._switch_order_mode("all"))
        self.cancel_order_btn.clicked.connect(self._cancel_selected_order)
        self.orders_refresh_btn.clicked.connect(self._manual_refresh_orders)
        tabs.addWidget(self.live_orders_btn)
        tabs.addWidget(self.filled_orders_btn)
        tabs.addWidget(self.inactive_orders_btn)
        tabs.addWidget(self.all_orders_btn)
        tabs.addWidget(self.order_count_label)
        tabs.addStretch(1)
        tabs.addWidget(self.cancel_order_btn)
        tabs.addSpacing(36)
        tabs.addWidget(self.orders_refresh_btn)
        body.addWidget(head)
        self.orders_model = DataTableModel(["\u4ee3\u7801", "\u65b9\u5411", "\u4ef7\u683c", "\u6570\u91cf", "\u7c7b\u578b", "\u6709\u6548\u671f", "\u72b6\u6001"])
        self.orders_table = self._make_table(self.orders_model)
        self.orders_table.doubleClicked.connect(lambda _index: self._cancel_selected_order())
        body.addWidget(self.orders_table, 1)
        self._update_order_mode_buttons("live")
        return panel

    def _build_positions_panel(self) -> QFrame:
        panel, body = self._make_data_panel()
        head = self._make_panel_header()
        head_layout = QHBoxLayout(head)
        head_layout.setContentsMargins(12, 7, 12, 7)
        head_layout.addWidget(make_label("\u6301\u4ed3\u4e0e\u76c8\u4e8f", color=theme.TEXT_PRIMARY, font=theme.ui_font(10, bold=True)))
        head_layout.addStretch(1)
        self.positions_refresh_btn = make_button("\u21bb", object_name="refreshIconButton")
        self.positions_refresh_btn.setFixedSize(32, 32)
        self.positions_refresh_btn.setToolTip("刷新持仓")
        self.positions_refresh_btn.setAccessibleName("刷新持仓")
        self.positions_refresh_btn.clicked.connect(self._manual_refresh_positions)
        head_layout.addWidget(self.positions_refresh_btn)
        body.addWidget(head)

        stats = QWidget()
        stats.setStyleSheet("background: #0E1217;")
        stats_layout = QHBoxLayout(stats)
        stats_layout.setContentsMargins(8, 8, 8, 8)
        stats_layout.setSpacing(8)
        self.metric_shares = self._metric_card("\u4eca\u65e5\u80a1\u6570", "0")
        self.metric_realized = self._metric_card("\u4eca\u65e5\u5df2\u5b9e\u73b0", "$0.00")
        self.metric_unrealized = self._metric_card("\u5f53\u524d\u672a\u5b9e\u73b0", "$0.00")
        stats_layout.addWidget(self.metric_shares[0])
        stats_layout.addWidget(self.metric_realized[0])
        stats_layout.addWidget(self.metric_unrealized[0])
        body.addWidget(stats)

        self.positions_model = DataTableModel(["\u4ee3\u7801", "\u4e70\u5165", "\u5356\u51fa", "\u6301\u4ed3", "\u5747\u4ef7", "\u73b0\u4ef7", "\u672a\u5b9e\u73b0", "\u5df2\u5b9e\u73b0", "\u6210\u4ea4"])
        self.positions_table = self._make_table(self.positions_model)
        self.positions_table.doubleClicked.connect(self._on_position_double_clicked)
        body.addWidget(self.positions_table, 1)
        return panel

    def _metric_card(self, title: str, value: str) -> tuple[QFrame, QLabel]:
        card = QFrame()
        card.setObjectName("inputBox")
        card.setMinimumHeight(72)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)
        title_label = make_label(title, color=theme.TEXT_LOW, font=theme.mono_font(9))
        title_label.setAlignment(Qt.AlignHCenter | Qt.AlignBottom)
        value_label = make_label(value, color=theme.TEXT_PRIMARY, font=theme.mono_font(12, bold=True))
        value_label.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        layout.addWidget(title_label)
        layout.addWidget(value_label)
        return card, value_label

    def _make_data_panel(self) -> tuple[QFrame, QVBoxLayout]:
        panel = QFrame()
        panel.setObjectName("dataPanel")
        body = QVBoxLayout(panel)
        body.setContentsMargins(1, 1, 1, 1)
        body.setSpacing(0)
        return panel, body

    def _make_panel_header(self) -> QFrame:
        header = QFrame()
        header.setObjectName("panelHeader")
        header.setFixedHeight(56)
        return header

    def _make_table(self, model: DataTableModel) -> QTableView:
        table = QTableView()
        table.setObjectName("tradeDataTable")
        table.setModel(model)
        table.setItemDelegate(NoFocusRectItemDelegate(table))
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        table.verticalHeader().hide()
        table.setShowGrid(False)
        table.setSelectionBehavior(QTableView.SelectRows)
        table.setSelectionMode(QTableView.SingleSelection)
        return table

    def _build_console(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("dataPanel")
        self.console_panel = panel
        panel.setFixedHeight(166)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)
        head = QFrame()
        head.setObjectName("topHeader")
        head.setFixedHeight(48)
        head_layout = QHBoxLayout(head)
        head_layout.setContentsMargins(12, 5, 10, 5)
        head_layout.addWidget(make_label("Console", color=theme.TEXT_DIM, font=theme.mono_font(9, bold=True)))
        head_layout.addStretch(1)
        clear_button = make_button("\u6e05\u7a7a", object_name="consoleButton", min_width=60)
        clear_button.clicked.connect(self._clear_logs)
        head_layout.addWidget(clear_button)
        layout.addWidget(head)
        self.log_body = QWidget()
        self.log_layout = QVBoxLayout(self.log_body)
        self.log_layout.setContentsMargins(12, 10, 12, 10)
        self.log_layout.setSpacing(7)
        self.log_layout.addStretch(1)
        layout.addWidget(self.log_body, 1)
        return panel

    def _log_line(self, timestamp: str, message: str, color: str) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(make_label(timestamp, color="#4B5563", font=theme.mono_font(9)))
        layout.addWidget(make_label(message, color=color, font=theme.mono_font(9)))
        layout.addStretch(1)
        return row

    def _tick(self) -> None:
        if self._clock is not None:
            try:
                self._clock.setText(dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            except RuntimeError:
                self._clock = None
        self._check_auth_lifetime()

    def _check_auth_lifetime(self) -> None:
        session = self.session
        if not session or not session.connected or self._auth_exit_started:
            return
        remaining = session.auth_seconds_remaining()
        if remaining is None:
            return
        if remaining <= 0:
            self._start_authentication_recovery("AUTH_EXPIRED")
            return
        if (
            AUTH_EXPIRY_WARNING_SECONDS > 0
            and remaining <= AUTH_EXPIRY_WARNING_SECONDS
            and not self._auth_warning_shown
        ):
            self._auth_warning_shown = True
            minutes = max(1, int((AUTH_EXPIRY_WARNING_SECONDS + 59) // 60))
            self._show_weak_tip(
                f"认证将在{minutes}分钟后到期，到期后需要重新登录",
                "warn",
            )

    def _start_authentication_recovery(self, code: str) -> None:
        if self._auth_exit_started:
            return
        self._auth_exit_started = True
        normalized = str(code or "AUTH_INVALID").strip().upper()
        notice = (
            "认证已过期，请重新认证登录"
            if normalized == "AUTH_EXPIRED"
            else "认证已失效，请重新认证登录"
        )
        self._teardown_shortcuts()
        self._reset_runtime_action_state()
        if self.session:
            self.session.bind_se_client(None)
            self.session.clear_local_auth()
        self._ts_connection.abort(release=False, wait=False)
        self._reset_to_login_page(auth_notice=notice)

    def _poll(self) -> None:
        now = time.time()
        if self.session and self.session.connected:
            if now - self._last_heartbeat > HEARTBEAT_INTERVAL / 1000:
                self._last_heartbeat = now
                threading.Thread(target=self._heartbeat_check, daemon=True).start()
            if self._main_ui_built:
                self._order_refresh.poll(
                    connected=True,
                    order_query_enabled=self._broker_capability_enabled("order_query"),
                    positions_enabled=self._broker_capability_enabled("positions"),
                )

    def _heartbeat_check(self) -> None:
        if not self.http.is_connected:
            return
        if not self.http.health_check():
            self._ui(lambda: self._on_server_disconnect())
    def _set_ts_connection_state(self, state: str, detail: str = "") -> None:
        state = (state or "offline").strip().lower()
        self._se_connected = state == "online"
        if state != "online":
            self._order_refresh.reset()
            self._quote_subscriptions.reset(clear_desired=False)
            self._invalidate_quote_freshness()
            self._cancel_all_pending_orders()
        if state == "online":
            self._reconnect_failed = False
            self._last_reconnect_notice_attempt = 0
        if not self._main_ui_built:
            return
        if state == "online":
            self._connection_status_label = "ONLINE"
            self.status_dot.setStyleSheet(f"color: {theme.ACCENT_GREEN};")
            self.status_text.setStyleSheet(f"color: {theme.ACCENT_GREEN};")
        elif state == "reconnecting":
            self._connection_status_label = "RECONNECTING"
            self.status_dot.setStyleSheet(f"color: {theme.ACCENT_YELLOW};")
            self.status_text.setStyleSheet(f"color: {theme.ACCENT_YELLOW};")
            self.latency_label.setText("重连中")
            self.latency_label.setStyleSheet(f"color: {theme.ACCENT_YELLOW};")
        elif state == "failed":
            self._connection_status_label = "FAILED"
            self.status_dot.setStyleSheet(f"color: {theme.ACCENT_RED};")
            self.status_text.setStyleSheet(f"color: {theme.ACCENT_RED};")
            self.latency_label.setText("--ms")
            self.latency_label.setStyleSheet(f"color: {theme.TEXT_LOW};")
        else:
            self._connection_status_label = "OFFLINE"
            self.status_dot.setStyleSheet(f"color: {theme.ACCENT_RED};")
            self.status_text.setStyleSheet(f"color: {theme.ACCENT_RED};")
            self.latency_label.setText("--ms")
            self.latency_label.setStyleSheet(f"color: {theme.TEXT_LOW};")
        self._apply_broker_status_ui()

    def _set_se_connection_ui(self, connected: bool) -> None:
        self._set_ts_connection_state("online" if connected else "offline")


    def _on_ts_latency(self, latency_ms: int) -> None:
        def apply() -> None:
            if not self._main_ui_built or not self._se_connected:
                return
            color = theme.ACCENT_GREEN if latency_ms < 120 else theme.ACCENT_YELLOW if latency_ms < 300 else theme.ACCENT_RED
            self.latency_label.setText(f"{latency_ms}ms")
            self.latency_label.setStyleSheet(f"color: {color};")
        self._ui(apply)


    def _handle_ts_reconnecting(self, msg: str) -> None:
        self._set_ts_connection_state("reconnecting")
        match = re.search(r"Reconnecting \((\d+)\)", msg or "")
        attempt = int(match.group(1)) if match else 0
        max_attempts = TS_RECONNECT_MAX_ATTEMPTS if TS_RECONNECT_MAX_ATTEMPTS > 0 else "无限"
        if attempt and attempt == self._last_reconnect_notice_attempt:
            return
        self._last_reconnect_notice_attempt = attempt
        if attempt:
            self._append_log(f"交易服务器连接中断，正在第 {attempt}/{max_attempts} 次重连", "warn", dedupe=True)
        else:
            self._append_log("交易服务器连接中断，正在重连", "warn", dedupe=True)

    def _start_reconnect_failure_recovery(self, msg: str = "") -> None:
        if self._reconnect_failed:
            return
        self._reconnect_failed = True
        self._set_ts_connection_state("failed")
        self._append_log("交易服务器重连失败，正在释放占用并返回登录界面", "err", dedupe=True)
        self._run_bg(self._recover_to_login_after_reconnect_failure_bg)

    def _confirm_logout(self) -> None:
        if not self._main_ui_built or self._logout_in_progress:
            return
        dialog = SessionActionDialog(
            self,
            title="退出登录",
            message="退出后将断开当前交易服务连接，并返回登录界面。",
            confirm_text="确认退出",
        )
        if dialog.exec() != QDialog.Accepted:
            return
        self._logout_in_progress = True
        self._teardown_shortcuts()
        self._reset_runtime_action_state()
        if self._settings_overlay is not None:
            self._close_settings_overlay()
        if hasattr(self, "logout_btn"):
            self.logout_btn.setEnabled(False)
        if hasattr(self, "settings_btn"):
            self.settings_btn.setEnabled(False)
        self._run_bg(self._logout_to_login_bg)

    def _logout_to_login_bg(self) -> None:
        session = self.session
        try:
            if session:
                session.bind_se_client(None)
            # The SM release call must run while the Client token is still valid.
            self._ts_connection.shutdown(release=True, wait=True)
            if session:
                session.logout()
        finally:
            self._ui(self._finish_logout_to_login)

    def _finish_logout_to_login(self) -> None:
        if not self._logout_in_progress:
            return
        self._reset_to_login_page("已退出登录。")

    def _return_to_login_after_force_disconnect(self) -> None:
        if self._logout_in_progress or self.session is None:
            return
        self._auth_exit_started = True
        self._teardown_shortcuts()
        self._reset_runtime_action_state()
        if self._settings_overlay is not None:
            self._close_settings_overlay()
        if self.session:
            self.session.bind_se_client(None)
            self.session.clear_local_auth()
        # A forced disconnect can belong to a replacement session. Do not send a
        # release request here because it could clear that replacement's lease.
        self._ts_connection.abort(release=False, wait=False)
        self._reset_to_login_page("当前会话已失效，请重新登录。")

    def _recover_to_login_after_reconnect_failure_bg(self) -> None:
        try:
            if self.session:
                try:
                    self.session.bind_se_client(None)
                except Exception:
                    pass
            self._ts_connection.shutdown(release=True, wait=True)
            if self.session:
                try:
                    self.session.logout()
                except Exception:
                    pass
        finally:
            self._ui(lambda: self._reset_to_login_page("交易服务器重连失败，已释放占用，请重新登录。"))

    def _reset_to_login_page(self, hint: str = "", auth_notice: str = "") -> None:
        self._teardown_shortcuts()
        self._reset_runtime_action_state()
        overlay = self._settings_overlay
        self._settings_overlay = None
        if overlay is not None:
            overlay.hide()
            overlay.deleteLater()
        self.session = None
        self._main_ui_built = False
        self._init_ready = False
        self._startup_login_required = True
        self._login_dialog_open = False
        self._login_username = ""
        self._login_password = ""
        self._last_heartbeat = 0.0
        self._last_ui_error_message = ""
        self._last_ui_error_at = 0.0
        self._last_reconnect_notice_attempt = 0
        self._reconnect_failed = False
        self._auth_warning_shown = False
        self._auth_exit_started = False
        self._logout_in_progress = False
        self._order_refresh.set_order_mode("live", refresh=False)
        self._orders_raw = []
        self._positions_raw = []
        self.current_quote = {}
        self.slots = {}
        self._clock = None
        self._ts_connection.reset()
        self._quote_subscriptions.reset(clear_desired=True)
        self._log_rows = []
        self._build_login_root(hint, auth_notice=auth_notice)

    def _build_login_root(self, hint: str = "", auth_notice: str = "") -> None:
        root = self._create_root_widget()
        self.setCentralWidget(root)
        shell = QVBoxLayout(root)
        shell.setContentsMargins(22, 22, 22, 22)
        shell.setSpacing(16)
        shell.addStretch(1)
        card = QFrame()
        card.setObjectName("slotCard")
        card.setMinimumWidth(760)
        card.setMaximumWidth(820)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(34, 34, 34, 34)
        card_layout.setSpacing(0)
        title = make_label("登录", color=theme.ACCENT_BLUE, font=theme.mono_font(30, bold=True))
        title.setObjectName("loginTitle")
        title.setAlignment(Qt.AlignCenter)
        title.setMinimumHeight(40)
        title.setStyleSheet(f"color: {theme.ACCENT_BLUE}; font-size: 30px; font-weight: 900; letter-spacing: 1px; line-height: 1.0;")
        card_layout.addWidget(title)
        card_layout.addSpacing(36)
        self._login_form = QWidget()
        login_layout = QVBoxLayout(self._login_form)
        login_layout.setContentsMargins(0, 0, 0, 0)
        login_layout.setSpacing(14)
        form_wrap = QWidget()
        form_wrap.setMinimumWidth(340)
        form_wrap.setMaximumWidth(340)
        form_wrap_layout = QVBoxLayout(form_wrap)
        form_wrap_layout.setContentsMargins(0, 0, 0, 0)
        form_wrap_layout.setSpacing(8)
        def login_field(label_text: str, field: QLineEdit) -> QWidget:
            row = QWidget()
            row_layout = QGridLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setHorizontalSpacing(14)
            row_layout.setColumnMinimumWidth(0, 54)
            row_layout.setColumnMinimumWidth(1, 210)
            row_layout.setColumnMinimumWidth(2, 54)
            label = make_label(label_text, color=theme.TEXT_DIM, font=theme.ui_font(14, bold=True))
            label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 14px; font-weight: 800;")
            field.setMinimumHeight(40)
            field.setFixedWidth(210)
            field.setFont(theme.ui_font(14))
            field.setStyleSheet(f"background: {theme.INPUT_BG}; color: {theme.TEXT_PRIMARY}; border: 1px solid {theme.BORDER}; border-radius: 8px; padding: 4px 10px; font-size: 14px; font-weight: 700;")
            row_layout.addWidget(label, 0, 0, alignment=Qt.AlignRight | Qt.AlignVCenter)
            row_layout.addWidget(field, 0, 1, alignment=Qt.AlignCenter)
            return row
        self._login_user_entry = make_input("")
        self._login_pass_entry = make_input("", password=True)
        form_wrap_layout.addWidget(login_field("账号", self._login_user_entry))
        form_wrap_layout.addWidget(login_field("密码", self._login_pass_entry))
        login_layout.addWidget(form_wrap, alignment=Qt.AlignHCenter)
        self._auth_notice_label = self._make_auth_notice_label(auth_notice)
        login_layout.addWidget(self._auth_notice_label)
        login_layout.addSpacing(46)
        login_buttons = QWidget()
        login_buttons.setMaximumWidth(560)
        login_button_layout = QGridLayout(login_buttons)
        login_button_layout.setContentsMargins(0, 0, 0, 0)
        login_button_layout.setHorizontalSpacing(0)
        login_button_layout.setColumnStretch(0, 1)
        login_button_layout.setColumnStretch(1, 1)
        login_button_layout.setColumnMinimumWidth(0, 250)
        login_button_layout.setColumnMinimumWidth(1, 250)
        self._login_exit_btn = make_button("退出", min_width=128)
        self._login_exit_btn.setStyleSheet(f"background: {theme.PANEL_ALT_BG}; color: {theme.TEXT_DIM}; border: 1px solid {theme.PANEL_ALT_BG}; border-radius: 8px; font-size: 14px; font-weight: 700; padding: 8px 16px; min-height: 34px;")
        self._login_submit_btn = make_button("登录", object_name="loginButton", min_width=128)
        self._login_submit_btn.setStyleSheet(f"background: {theme.ACCENT_BLUE}; color: #07121B; border: 1px solid {theme.ACCENT_BLUE}; border-radius: 8px; font-size: 14px; font-weight: 700; padding: 8px 16px; min-height: 34px;")
        self._login_submit_btn.clicked.connect(self._submit_inline_login)
        self._login_exit_btn.clicked.connect(self.close)
        self._login_pass_entry.returnPressed.connect(self._submit_inline_login)
        login_button_layout.addWidget(self._login_exit_btn, 0, 0, alignment=Qt.AlignCenter)
        login_button_layout.addWidget(self._login_submit_btn, 0, 1, alignment=Qt.AlignCenter)
        login_layout.addWidget(login_buttons, alignment=Qt.AlignHCenter)
        card_layout.addWidget(self._login_form)
        self._init_status = QFrame()
        self._init_status.setStyleSheet("background: transparent; border: none;")
        status_layout = QVBoxLayout(self._init_status)
        status_layout.setContentsMargins(0, 4, 0, 0)
        status_layout.setSpacing(16)
        subtitle = make_label("正在鉴权并连接...", color=theme.TEXT_DIM, font=theme.ui_font(12))
        subtitle.setAlignment(Qt.AlignCenter)
        status_layout.addWidget(subtitle)
        self._init_progress = QProgressBar()
        self._init_progress.setRange(0, 0)
        self._init_progress.setTextVisible(False)
        self._init_progress.setFixedHeight(8)
        self._init_progress.setStyleSheet(f"QProgressBar {{ background: #05070A; border: none; border-radius: 4px; }} QProgressBar::chunk {{ background: {theme.ACCENT_BLUE}; border-radius: 4px; }}")
        status_layout.addWidget(self._init_progress)
        self._init_steps = {}
        for key, caption, default, color in (("auth", "账号登录", "等待中", theme.TEXT_MUTED), ("sm", "管理服务", "等待中", theme.TEXT_MUTED), ("se", "交易服务", "等待中", theme.TEXT_MUTED)):
            row = QWidget()
            row.setStyleSheet("background: transparent; border: none;")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(14)
            name = make_label(caption, color=theme.TEXT_DIM, font=theme.ui_font(11))
            name.setMinimumWidth(110)
            status = make_label(default, color=color, font=theme.mono_font(10, bold=True))
            row_layout.addWidget(name)
            row_layout.addWidget(status, 1)
            status_layout.addWidget(row)
            self._init_steps[key] = (name, status)
        card_layout.addWidget(self._init_status)
        self._init_status.hide()
        self._init_hint_label = make_label(hint, color=theme.ACCENT_RED, font=theme.ui_font(10))
        self._init_hint_label.setWordWrap(True)
        self._init_hint_label.setAlignment(Qt.AlignCenter)
        card_layout.addWidget(self._init_hint_label)
        btn_row = QWidget()
        btn_layout = QHBoxLayout(btn_row)
        btn_layout.setContentsMargins(0, 18, 0, 2)
        btn_layout.setSpacing(16)
        self._retry_btn = make_button("重试", min_width=92)
        self._retry_btn.clicked.connect(self._on_init_retry)
        self._cancel_btn = make_button("取消", min_width=92)
        self._cancel_btn.clicked.connect(self._on_init_cancel)
        btn_layout.addStretch(1)
        btn_layout.addWidget(self._retry_btn)
        btn_layout.addWidget(self._cancel_btn)
        btn_layout.addStretch(1)
        card_layout.addWidget(btn_row)
        self._retry_btn.hide()
        self._cancel_btn.hide()
        center = QWidget()
        center_layout = QHBoxLayout(center)
        center_layout.setContentsMargins(24, 0, 24, 0)
        center_layout.addStretch(1)
        center_layout.addWidget(card)
        center_layout.addStretch(1)
        shell.addWidget(center, alignment=Qt.AlignCenter)
        shell.addStretch(1)
        self._login_user_entry.setFocus()


    def _broker_detail_state(self) -> dict:
        raw = getattr(self.session, "broker_detail", None) if self.session else None
        if isinstance(raw, dict):
            return raw
        return {"connected": False, "capabilities": {}, "read_only": False, "account": {}}

    def _update_header_broker_status(self, read_only: bool) -> None:
        if not hasattr(self, "status_text"):
            return
        status = self._connection_status_label or "OFFLINE"
        self.status_text.setText(status)
        if hasattr(self, "read_only_label"):
            self.read_only_label.setVisible(read_only)

    def _broker_capability_enabled(self, capability: str) -> bool:
        if not self.session:
            return False
        if getattr(self.session, "mock_mode", False):
            return True
        return bool(
            self.session.connected
            and self._se_connected
            and self.session.has_broker_capability(capability)
        )

    def _trade_controls_enabled(self) -> bool:
        return self._broker_capability_enabled("orders")

    def _broker_order_options(self, symbol: str = "") -> dict:
        normalized_symbol = str(symbol or "").strip().upper()
        if normalized_symbol and self.session:
            getter = getattr(self.session, "symbol_order_options", None)
            if callable(getter):
                try:
                    symbol_options = getter(normalized_symbol)
                except Exception:
                    symbol_options = {}
                if isinstance(symbol_options, dict) and symbol_options:
                    return symbol_options
        detail = self._broker_detail_state()
        options = detail.get("order_options")
        return options if isinstance(options, dict) else {}

    def _current_route_options(self, symbol: str = "") -> list[str]:
        detail = self._broker_detail_state()
        global_options = detail.get("order_options")
        options = (
            global_options
            if isinstance(global_options, dict)
            else self._broker_order_options(symbol)
        )
        routes = options.get("routes") or options.get("available_routes") or []
        normalized: list[str] = []
        for route in routes:
            value = str(route or "").strip().upper()
            if value and value not in normalized:
                normalized.append(value)
        default_route = str(options.get("default_route") or self._hotkey_config.default_route or "SMART").upper()
        if default_route and default_route not in normalized:
            normalized.insert(0, default_route)
        if "SMART" not in normalized:
            normalized.insert(0, "SMART")
        return normalized

    def _route_editable(self, symbol: str = "") -> bool:
        options = self._broker_order_options(symbol)
        return bool(options.get("route_editable", False))

    def _hidden_order_supported(self, symbol: str = "") -> bool:
        options = self._broker_order_options(symbol)
        return bool(options.get("hidden_order", False))

    def _broker_supported_tifs(self, symbol: str = "") -> set[str]:
        options = self._broker_order_options(symbol)
        values = options.get("supported_tifs")
        if values is None and symbol:
            detail = self._broker_detail_state()
            global_options = detail.get("order_options")
            if isinstance(global_options, dict):
                values = global_options.get("supported_tifs")
        return {
            str(value or "").strip().upper()
            for value in values or []
            if str(value or "").strip()
        }

    def _broker_supports_tif(self, tif: str, symbol: str = "") -> bool:
        return str(tif or "").strip().upper() in self._broker_supported_tifs(symbol)

    def _resolve_route_value(self, route: str, symbol: str = "") -> str:
        options = self._broker_order_options(symbol)
        if not bool(options.get("route_editable", False)):
            return str(options.get("default_route") or "SMART").strip().upper()
        value = str(route or "").strip().upper()
        if not value or value == "DEFAULT":
            return str(self._hotkey_config.default_route or "SMART").strip().upper()
        return value

    def _route_available_for_symbol(self, symbol: str, route: str) -> bool:
        normalized_route = str(route or "SMART").strip().upper() or "SMART"
        if normalized_route == "SMART":
            return True
        options = self._broker_order_options(symbol)
        if not bool(options.get("routes_validated", False)):
            return False
        routes = {
            str(value or "").strip().upper()
            for value in options.get("routes") or []
            if str(value or "").strip()
        }
        return normalized_route in routes

    def _apply_broker_status_ui(self) -> None:
        if not self._main_ui_built:
            return
        detail = self._broker_detail_state()
        active = bool(detail.get("connected") and self.session and self.session.connected and self._se_connected)
        account = detail.get("account") if isinstance(detail.get("account"), dict) else {}
        authority = str(account.get("authority_level") or "unknown").lower()
        read_only = bool(detail.get("read_only")) or authority in {"read-only", "read_only", "readonly"}
        if hasattr(self, "account_state"):
            style_status_pill(self.account_state, "Connect" if active else "Offline", active=True, danger=not active)
        self._update_header_broker_status(read_only)
        self._set_live_orders_online(self._connection_status_label == "ONLINE")
        orders_enabled = self._broker_capability_enabled("orders")
        for slot in self.slots.values():
            slot.set_trade_enabled(orders_enabled)
            self._apply_order_options_to_slot(slot)
        if not orders_enabled:
            self._cancel_all_pending_orders()
        self.orders_refresh_btn.setEnabled(self._broker_capability_enabled("order_query"))
        self.positions_refresh_btn.setEnabled(self._broker_capability_enabled("positions"))
        self.cancel_order_btn.setEnabled(self._broker_capability_enabled("cancel_order"))

    def _apply_order_options_to_slot(self, slot: TradingSlot) -> None:
        symbol = slot.current_symbol
        routes = self._current_route_options(symbol)
        route_editable = self._route_editable(symbol)
        hidden_supported = self._hidden_order_supported(symbol)
        supported_tifs = self._broker_supported_tifs(symbol)
        if slot.route:
            current = slot.route.currentText().strip().upper() or self._resolve_route_value("DEFAULT", symbol)
            slot.route.blockSignals(True)
            slot.route.clear()
            slot.route.addItems(routes)
            if current in routes:
                slot.route.setCurrentText(current)
            else:
                fallback = self._resolve_route_value("DEFAULT", symbol)
                if fallback not in routes:
                    fallback = str(
                        self._broker_order_options(symbol).get("default_route") or "SMART"
                    ).strip().upper()
                if fallback not in routes and routes:
                    fallback = routes[0]
                slot.route.setCurrentText(fallback)
            slot.route.blockSignals(False)
            slot.route.setFocusPolicy(Qt.StrongFocus if route_editable else Qt.NoFocus)
            slot.route.setAttribute(Qt.WA_TransparentForMouseEvents, not route_editable)
            slot.route.setProperty("locked", not route_editable)
            self._repolish(slot.route)
        if slot.tif:
            declared_tifs = bool(supported_tifs)
            current_tif = slot.tif.currentText().strip().upper()
            current_supported = (
                current_tif in supported_tifs
                if declared_tifs
                else current_tif != "IOC"
            )
            if not current_supported:
                fallback = "DAY" if not declared_tifs or "DAY" in supported_tifs else ""
                if not fallback and supported_tifs:
                    fallback = next(
                        (value.upper() for value in ORDER_TIFS if value.upper() in supported_tifs),
                        "",
                    )
                if fallback:
                    slot.tif.setCurrentText(next(
                        (value for value in ORDER_TIFS if value.upper() == fallback),
                        "Day",
                    ))
            for value in ORDER_TIFS:
                enabled_for_channel = (
                    value.upper() in supported_tifs
                    if declared_tifs
                    else value.upper() != "IOC"
                )
                set_combo_item_enabled(
                    slot.tif,
                    value,
                    enabled_for_channel,
                    f"当前交易通道不支持 {value} 订单",
                )
        if slot.hidden_order:
            slot.hidden_order.setEnabled(hidden_supported)
            slot.hidden_order.setToolTip(
                "以 HIDE 订单方式提交" if hidden_supported else "当前交易通道不支持 HIDE 订单"
            )
            if not hidden_supported:
                slot.hidden_order.setChecked(False)
        if slot.hidden_order_caption:
            slot.hidden_order_caption.setStyleSheet(
                f"color: {theme.TEXT_MUTED if hidden_supported else theme.TEXT_LOW};"
            )

    def _append_log(self, message: str, tag: str = "inf", *, dedupe: bool = False) -> None:
        msg = localize_user_message(message)
        if not msg:
            return
        if dedupe and self._log_rows and self._log_rows[-1][1] == msg:
            return
        color = {
            "ok": theme.ACCENT_GREEN,
            "err": theme.ACCENT_RED,
            "warn": theme.ACCENT_YELLOW,
            "inf": theme.TEXT_DIM,
        }.get(tag, theme.TEXT_DIM)
        stamp = dt.datetime.now().strftime("[%H:%M:%S]")
        self._log_rows.append((stamp, msg, color))
        self._log_rows = self._log_rows[-5:]
        if self._main_ui_built and hasattr(self, "log_layout"):
            self._render_logs()
        if tag == "warn":
            self._show_weak_tip(msg, "warn")

    def _log_user_error_once(self, msg: str, tag: str = "err", window_seconds: float = 3.0) -> None:
        text = localize_user_message(msg)
        now = time.time()
        if text == self._last_ui_error_message and now - self._last_ui_error_at < window_seconds:
            return
        self._last_ui_error_message = text
        self._last_ui_error_at = now
        self._append_log(text, tag)

    def _show_weak_tip(self, message: str, level: str = "inf", duration_ms: int = 3000) -> None:
        text = localize_user_message(message)
        if not text or not self._main_ui_built:
            return
        parent = self.centralWidget()
        if parent is None:
            return
        for active_toast in self._toast_widgets:
            if (
                active_toast.property("weakMessage") == text
                and active_toast.property("weakLevel") == level
            ):
                return
        color = {
            "ok": theme.ACCENT_GREEN,
            "warn": theme.ACCENT_YELLOW,
            "err": theme.ACCENT_RED,
            "inf": theme.TEXT_DIM,
        }.get(level, theme.TEXT_DIM)
        toast = QFrame(parent)
        toast.setObjectName("weakToast")
        toast.setProperty("weakMessage", text)
        toast.setProperty("weakLevel", level)
        toast.setStyleSheet(theme.TOAST_QSS)
        opacity = QGraphicsOpacityEffect(toast)
        opacity.setOpacity(1.0)
        toast.setGraphicsEffect(opacity)
        layout = QHBoxLayout(toast)
        layout.setContentsMargins(26, 18, 26, 18)
        label = make_label(text, color=color, font=theme.ui_font(12, bold=True))
        label.setWordWrap(True)
        label.setMinimumWidth(640)
        label.setMaximumWidth(720)
        layout.addWidget(label)
        toast.adjustSize()
        toast.setMinimumHeight(max(72, toast.height()))
        self._toast_widgets.append(toast)
        self._position_toasts()
        toast.show()
        toast.raise_()

        def remove_toast(animation: QPropertyAnimation | None = None) -> None:
            if animation is not None and animation in self._toast_animations:
                self._toast_animations.remove(animation)
            if toast in self._toast_widgets:
                self._toast_widgets.remove(toast)
            toast.deleteLater()
            self._position_toasts()

        def fade_toast() -> None:
            if toast not in self._toast_widgets:
                return
            animation = QPropertyAnimation(opacity, b"opacity", toast)
            animation.setDuration(350)
            animation.setStartValue(1.0)
            animation.setEndValue(0.0)
            animation.setEasingCurve(QEasingCurve.OutCubic)
            animation.finished.connect(lambda anim=animation: remove_toast(anim))
            self._toast_animations.append(animation)
            animation.start()

        QTimer.singleShot(max(0, int(duration_ms) - 350), fade_toast)

    def _position_toasts(self) -> None:
        parent = self.centralWidget()
        if parent is None:
            return
        margin = 22
        console = getattr(self, "console_panel", None)
        console_top = console.mapTo(parent, console.rect().topLeft()).y() if console is not None else parent.height() - 188
        visible_toasts = list(self._toast_widgets)
        total_height = 0
        for toast in visible_toasts:
            toast.adjustSize()
            total_height += max(toast.height(), toast.minimumHeight())
        total_height += max(0, len(visible_toasts) - 1) * 10
        y = max(80, console_top - total_height - 10)
        for toast in visible_toasts:
            if toast is None:
                continue
            toast.adjustSize()
            width = min(max(toast.width(), 700), max(320, parent.width() - margin * 2))
            height = max(toast.height(), toast.minimumHeight())
            x = max(margin, parent.width() - width - margin)
            toast.setGeometry(x, y, width, height)
            y += height + 10

    def _set_trade_card_effects_enabled(self, enabled: bool) -> None:
        for slot in self.slots.values():
            effect = slot.container.graphicsEffect() if slot.container else None
            if isinstance(effect, QGraphicsDropShadowEffect):
                effect.setEnabled(bool(enabled and slot.container.property("activePanel")))

    def _restore_resize_effects(self) -> None:
        self._set_trade_card_effects_enabled(True)

    def _render_logs(self) -> None:
        while self.log_layout.count():
            item = self.log_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        for stamp, msg, color in self._log_rows:
            self.log_layout.addWidget(self._log_line(stamp, msg, color))
        self.log_layout.addStretch(1)

    def _clear_logs(self) -> None:
        self._log_rows.clear()
        self._render_logs()

    def _show_startup_login(self) -> None:
        if self.session and self.session.connected:
            return
        if self._main_ui_built:
            self._show_manager_login(startup=True)
        else:
            self._show_login_page()

    def _show_manager_login(self, *, startup: bool = False) -> None:
        if not self._main_ui_built:
            self._show_login_page()
            return
        if self._login_dialog_open:
            return
        self._login_dialog_open = True
        try:
            dialog = ManagerLoginDialog(self, startup=startup)
            if dialog.exec() != QDialog.Accepted:
                if startup:
                    self.close()
                return
            username, password = dialog.credentials()
            if not username or not password:
                self._log_user_error_once("请输入账号和密码", "warn")
                if startup:
                    QTimer.singleShot(80, self._show_startup_login)
                return
            self._startup_login_required = startup
            self._run_bg(lambda: self._login_manager(username, password, False))
        finally:
            self._login_dialog_open = False

    def login_manager_for_test(self, username: str, password: str, force: bool = False) -> None:
        self._run_bg(lambda: self._login_manager(username, password, force))

    def _login_manager(self, username: str, password: str, force: bool = False) -> None:
        self.session = TradingSession(self.http)
        ok, msg = self.session.login(username, password, force=force)
        self._ui(lambda: self._handle_manager_login_result(ok, msg, username, password, force))

    def _handle_manager_login_result(self, ok: bool, msg: str, username: str, password: str, force: bool = False) -> None:
        if not ok:
            login_error = getattr(self.session, "last_login_error", {}) if self.session else {}
            if not force and login_error.get("code") == "already_logged_in":
                if DuplicateLoginDialog(self).exec() == QDialog.Accepted:
                    self._update_init_step("auth", "正在接管...", theme.ACCENT_YELLOW)
                    self._run_bg(lambda: self._login_manager(username, password, True))
                    return
            self._update_init_step("auth", "\u5931\u8d25", theme.ACCENT_RED)
            if self._main_ui_built:
                self._log_user_error_once(f"SM\u767b\u5f55\u5931\u8d25\uff1a{localize_user_message(msg)}")
            else:
                self._set_init_hint(f"SM\u767b\u5f55\u5931\u8d25\uff1a{localize_user_message(msg)}")
            if self._startup_login_required and not self._main_ui_built:
                QTimer.singleShot(300, self._show_login_page)
            elif self._startup_login_required:
                QTimer.singleShot(300, self._show_startup_login)
            return
        self._startup_login_required = False
        self._auth_warning_shown = False
        self._auth_exit_started = False
        self._login_username = username
        self._login_password = ""
        self._update_init_step("auth", "\u5df2\u767b\u5f55", theme.ACCENT_GREEN)
        if self._main_ui_built:
            self._append_log("SM\u767b\u5f55\u6210\u529f", "ok")
        self._se_target_address = getattr(self.session, "se_address", "") or default_ts_target()
        self._start_connection_flow()

    def _start_connection_flow(self) -> None:
        self._set_init_hint("")
        self._set_init_actions_visible(False)
        self._update_init_step("sm", "\u8fde\u63a5\u4e2d...", theme.ACCENT_YELLOW)
        self._run_bg(self._check_sm_then_connect_ts)

    def _check_sm_then_connect_ts(self) -> None:
        try:
            ok = self.http.health_check()
        except Exception:
            ok = False
        if not ok:
            self._ui(lambda: self._on_init_failed("sm", "\u65e0\u6cd5\u8fde\u63a5\u5230\u7ba1\u7406\u670d\u52a1", "\u8bf7\u786e\u4fdd\u7ba1\u7406\u670d\u52a1\u5df2\u542f\u52a8\u4e14\u7f51\u7edc\u901a\u7545\u3002"))
            return
        self._ui(lambda: self._update_init_step("sm", "\u5df2\u8fde\u63a5", theme.ACCENT_GREEN))
        target = self._se_target_address or getattr(self.session, "se_address", "") or default_ts_target()
        self._validate_and_connect_ts(target)

    def _se_connect(self) -> None:
        if not self.session or not self.session.connected:
            self._log_user_error_once("\u8bf7\u5148\u767b\u5f55SM")
            return
        if self._ts_connection.client_is_active():
            return
        target_addr = self._se_target_address or default_ts_target()
        self._append_log("\u6b63\u5728\u8fde\u63a5\u4ea4\u6613\u670d\u52a1\u5668", "inf")
        self._run_bg(lambda: self._validate_and_connect_ts(target_addr))

    def _validate_and_connect_ts(self, target_addr: str) -> None:
        self._ts_connection.validate_and_connect(target_addr)

    def _connect_ts_with_retry(self, target_addr: str) -> None:
        self._ts_connection.connect_validated(target_addr)

    def _prepare_ts_reconnect(self, generation: int, attempt: int, connection_id: str) -> bool:
        return self._ts_connection.prepare_reconnect(generation, attempt, connection_id)

    def _handle_se_connection_state_ui(self, state: str, detail: dict) -> None:
        if state == "authenticated":
            if self._init_ready:
                was_reconnecting = self._last_reconnect_notice_attempt > 0
                self._set_se_connection_ui(True)
                if self.session:
                    self.session.bind_se_client(self._se_client)
                self._append_log("交易服务器重连成功" if was_reconnecting else "交易服务器已连接", "ok", dedupe=True)
                self._refresh_broker_status_async(log_errors=False)
                self._sync_quote_subscriptions_async()
            else:
                self._update_init_step("se", "已连接", theme.ACCENT_GREEN)
                self._se_connected = True
                if self.session:
                    self.session.bind_se_client(self._se_client)
                QTimer.singleShot(400, lambda gen=self._se_generation: self._enter_main_interface(gen))
            return

        if state == "connecting":
            if not self._init_ready:
                self._update_init_step("se", "连接中...", theme.ACCENT_YELLOW)
            return

        if state == "reconnecting":
            attempt = int(detail.get("attempt") or 0)
            if self.session:
                self.session.clear_symbol_order_options()
            if self._init_ready:
                self._handle_ts_reconnecting(f"Reconnecting ({attempt})")
            else:
                self._update_init_step("se", f"连接中 ({attempt}/{TS_RECONNECT_MAX_ATTEMPTS})...", theme.ACCENT_YELLOW)
            return

        if state in ("auth_expired", "auth_invalid"):
            self._start_authentication_recovery(
                str(detail.get("code") or ("AUTH_EXPIRED" if state == "auth_expired" else "AUTH_INVALID"))
            )
            return

        if state in ("auth_failed", "retry_exhausted"):
            reason = str(detail.get("message") or detail.get("reason") or "连接失败")
            if self._init_ready:
                self._start_reconnect_failure_recovery(reason)
            else:
                self._on_init_failed("se", "无法连接到交易服务器", reason)
            return

        if state == "force_disconnected":
            self._return_to_login_after_force_disconnect()

    def _on_ts_validation_started(self, generation: int) -> None:
        if generation == self._se_generation:
            self._update_init_step("se", "校验中...", theme.ACCENT_YELLOW)

    def _on_ts_connection_failed(
        self,
        generation: int,
        reason: str,
        hint: str,
        release_occupation: bool,
    ) -> None:
        if generation != self._se_generation:
            return
        self._on_init_failed(
            "se",
            reason,
            hint,
            release_occupation=release_occupation,
        )

    def _route_ts_status(self, generation: int, message: str) -> None:
        if generation != self._se_generation:
            return
        if self._init_ready:
            self._handle_se_status_ui(message)
        else:
            self._handle_init_se_status_ui(message)

    def _route_ts_message(self, generation: int, message: dict) -> None:
        if generation != self._se_generation:
            return
        if self._init_ready:
            self._handle_se_message_ui(message)
        else:
            self._handle_init_se_message_ui(message)

    def _route_ts_latency(self, generation: int, latency_ms: int) -> None:
        if generation == self._se_generation:
            self._on_ts_latency(latency_ms)

    def _route_ts_state(self, generation: int, state: str, detail: dict) -> None:
        if generation == self._se_generation:
            self._handle_se_connection_state_ui(state, detail)

    def _handle_init_se_status_ui(self, msg: str) -> None:
        if "Connecting" in msg or "连接" in msg:
            self._update_init_step("se", "连接中...", theme.ACCENT_YELLOW)

    def _handle_init_se_message_ui(self, msg: dict) -> None:
        msg_type = msg.get("type", "")
        payload = msg.get("payload", {}) if isinstance(msg.get("payload", {}), dict) else {}
        if msg_type == "CONNECT_ACK" or msg.get("event") == "connected":
            if msg.get("event") == "connected" and isinstance(msg.get("data"), dict):
                payload = msg["data"].get("payload", {}) or {}
            detail = payload.get("broker_detail")
            if self.session and isinstance(detail, dict):
                self.session.set_broker_detail(detail)

    def _handle_se_status_ui(self, msg: str) -> None:
        if not any(key in msg for key in ("Authenticated", "Reconnect failed after", "Reconnecting", "Connecting", "Disconnected:")):
            self._append_log(msg, "inf", dedupe=True)

    def _handle_se_message_ui(self, msg: dict) -> None:
        msg_type = msg.get("type", "")
        payload = msg.get("payload", {}) if isinstance(msg.get("payload", {}), dict) else {}
        if msg_type in ("CONNECT_ACK", "STATUS_RESPONSE"):
            detail = payload.get("broker_detail")
            if self.session and isinstance(detail, dict):
                self.session.set_broker_detail(detail)
            self._apply_broker_status_ui()
        elif msg_type == "BROKER_STATUS_RESPONSE":
            detail = payload.get("broker_detail")
            if self.session and isinstance(detail, dict):
                self.session.set_broker_detail(detail)
            self._apply_broker_status_ui()
        elif msg_type == "QUOTE_DATA":
            self._handle_quote_payload(payload)
        elif msg_type == "BROKER_STATUS_CHANGE":
            status = str(payload.get("status") or "").lower()
            invalidate_quotes = getattr(self, "_invalidate_quote_freshness", None)
            if callable(invalidate_quotes):
                invalidate_quotes()
            detail = payload.get("broker_detail")
            if self.session and isinstance(detail, dict):
                self.session.set_broker_detail(detail)
            if self.session and status in {"connected", "reconnected", "reloaded"}:
                self.session.clear_symbol_order_options()
            self._apply_broker_status_ui()
            if status in {"connected", "reconnected", "reloaded"}:
                self._sync_quote_subscriptions_async(force_resubscribe=True)
                if self.session:
                    self.session.invalidate_order_cache()
                self._refresh_positions()
                self._refresh_orders()
        elif msg_type == "ORDER_STATUS_UPDATE":
            status_message = str(payload.get("status_message") or "").strip()
            if str(payload.get("status") or "") == "Rejected" and status_message:
                self._log_user_error_once(
                    f"订单被拒绝：{localize_user_message(status_message)}",
                    "warn",
                )
            self._order_refresh.handle_order_status_event(payload)
        elif msg_type == "POSITION_INVALIDATED":
            self._order_refresh.handle_position_event(payload)
        elif msg_type == "FORCE_DISCONNECT":
            self._return_to_login_after_force_disconnect()
        elif msg_type == "ERROR":
            message = localize_user_message(payload.get("message", ""))
            self._log_user_error_once(f"交易服务错误：{message or '操作失败，请稍后重试'}")
    def _on_init_se_status(self, msg: str) -> None:
        self._ui(lambda: self._handle_init_se_status_ui(msg))

    def _on_init_se_message(self, msg: dict) -> None:
        self._ui(lambda: self._handle_init_se_message_ui(msg))

    def _on_init_failed(self, step_key: str, reason: str, hint: str = "", release_occupation: bool = True) -> None:
        self._ts_connection.abort(release=release_occupation, wait=False)
        self._update_init_step(step_key, "\u5931\u8d25", theme.ACCENT_RED)
        msg = localize_user_message(reason)
        if hint:
            msg = f"{msg}\n{localize_user_message(hint)}"
        if self._main_ui_built:
            self._log_user_error_once(msg)
        else:
            self._set_init_hint(msg)
            self._set_init_actions_visible(True)
        if self.session:
            self.session.bind_se_client(None)

    def _on_init_retry(self) -> None:
        self._set_init_actions_visible(False)
        self._set_init_hint("")
        self._update_init_step("se", "\u91cd\u8bd5\u4e2d...", theme.ACCENT_YELLOW)
        target = self._se_target_address or getattr(self.session, "se_address", "") or default_ts_target()
        self._run_bg(lambda: self._validate_and_connect_ts(target))

    def _on_init_cancel(self) -> None:
        self._release_se_occupation()
        self._set_init_actions_visible(False)
        self._set_init_hint("")
        self._update_init_step("auth", "\u7b49\u5f85\u4e2d", theme.TEXT_MUTED)
        self._update_init_step("sm", "\u7b49\u5f85\u4e2d", theme.TEXT_MUTED)
        self._update_init_step("se", "\u7b49\u5f85\u4e2d", theme.TEXT_MUTED)
        QTimer.singleShot(80, self._show_startup_login)

    def _occupy_se_node(self, connection_id: str = "", max_retries: int = 3, sync: bool = True) -> bool:
        return self._ts_connection.occupy(
            connection_id=connection_id,
            max_retries=max_retries,
            sync=sync,
        )

    def _release_se_occupation(self, sync: bool = False, clear_server_id: bool = True) -> bool:
        return self._ts_connection.release(
            sync=sync,
            clear_server_id=clear_server_id,
        )

    def _enter_main_interface(self, generation: int | None = None) -> None:
        if generation is not None and generation != self._se_generation:
            return
        if self._init_ready:
            return
        self._init_ready = True
        self._logout_in_progress = False
        if self._se_client:
            if self.session:
                self.session.bind_se_client(self._se_client)
        self._build_root()
        self._main_ui_built = True
        self._setup_shortcuts()
        self._set_se_connection_ui(self._se_connected)
        self._append_log("SM\u767b\u5f55\u6210\u529f", "ok")
        self._append_log("\u4ea4\u6613\u670d\u52a1\u5668\u5df2\u8fde\u63a5", "ok")
        self._apply_broker_status_ui()
        self._refresh_broker_status_async(log_errors=False)
        self._sync_quote_subscriptions_async()

    def _se_disconnect(self) -> None:
        self._reset_runtime_action_state()
        if self.session:
            self.session.bind_se_client(None)
            self.session.set_broker_detail(None)
        self._ts_connection.disconnect(wait=False)
        self._last_reconnect_notice_attempt = 0
        self._set_se_connection_ui(False)
        self._append_log("\u4ea4\u6613\u670d\u52a1\u5668\u5df2\u65ad\u5f00", "warn")

    def _on_se_status(self, msg: str) -> None:
        self._ui(lambda: self._handle_se_status_ui(msg))

    def _on_se_message(self, msg: dict) -> None:
        self._ui(lambda: self._handle_se_message_ui(msg))

    @staticmethod
    def _normalize_quote_payload(payload: dict) -> dict:
        sym = str(payload.get("symbol", "")).strip().upper()
        if not sym:
            return {}
        try:
            bid = float(payload.get("bid", 0) or 0)
            ask = float(payload.get("ask", 0) or 0)
            last = float(payload.get("last", 0) or 0)
            if last <= 0 and bid > 0 and ask > 0:
                last = round((bid + ask) / 2, 2)
            return {
                "symbol": sym,
                "bid": bid,
                "ask": ask,
                "last": last,
                "volume": int(float(payload.get("volume", 0) or 0)),
                "received_monotonic": float(
                    payload.get("_client_received_monotonic")
                    or payload.get("received_monotonic")
                    or time.monotonic()
                ),
                "connection_generation": int(
                    payload.get("_client_connection_generation")
                    if payload.get("_client_connection_generation") is not None
                    else payload.get("connection_generation", -1)
                ),
            }
        except Exception:
            return {}

    def _flush_quote_updates(self) -> None:
        for payload in self._ts_connection.drain_quote_updates():
            self._handle_quote_payload(payload)

    def _latest_quote_snapshot(self, symbol: str) -> dict:
        normalized = str(symbol or "").strip().upper()
        latest = self._normalize_quote_payload(self._ts_connection.latest_quote(normalized))
        if latest:
            return latest
        return dict(self.current_quote.get(normalized) or {})

    def _handle_quote_payload(self, payload: dict) -> None:
        quote = self._normalize_quote_payload(payload)
        sym = str(quote.get("symbol") or "")
        if not sym:
            return
        self.current_quote[sym] = quote
        for slot in self.slots.values():
            if slot.current_symbol == sym:
                slot.update_quote(quote)

    def _invalidate_quote_freshness(self) -> None:
        self._ts_connection.clear_quote_cache()
        for quote_data in self.current_quote.values():
            if isinstance(quote_data, dict):
                quote_data["received_monotonic"] = 0.0

    def _refresh_broker_status_async(self, log_errors: bool = False) -> None:
        if not self.session or not self.session.connected or not self._se_connected:
            self._apply_broker_status_ui()
            return
        self._run_bg(lambda: self._refresh_broker_status_bg(log_errors))

    def _refresh_broker_status_bg(self, log_errors: bool) -> None:
        ok, _detail, msg = self.session.broker_status_query() if self.session else (False, {}, "\u672a\u8fde\u63a5")
        self._ui(lambda: (self._apply_broker_status_ui(), self._log_user_error_once(msg, "warn") if (not ok and log_errors and msg) else None))

    def _on_symbol_enter(self, pid: int) -> None:
        slot = self.slots.get(pid)
        if not slot:
            return
        self._set_enter_target(pid, "SYMBOL")
        if slot.symbol_pending:
            return
        self._activate_panel(pid)
        sym = slot.symbol_text()
        if not sym:
            self._mark_symbol_pending(pid, "")
            self._quote_subscriptions.clear_panel(pid)
            return
        if slot.current_symbol == sym and not slot.symbol_pending:
            quote = self._latest_quote_snapshot(sym)
            if quote:
                slot.update_quote(quote)
            return
        self._mark_symbol_pending(pid, sym)
        self._quote_subscriptions.request_symbol(pid, sym)

    def _on_symbol_query_requested(self, pid: int) -> None:
        self._set_enter_target(pid, "SYMBOL")
        self._on_symbol_enter(pid)

    def _on_symbol_text_changed(self, pid: int) -> None:
        slot = self.slots.get(pid)
        if not slot:
            return
        symbol = slot.symbol_text()
        if symbol == slot.current_symbol and not slot.symbol_pending:
            return
        self._cancel_pending_order(pid)
        self._set_enter_target(pid, "NONE")
        if symbol != slot.current_symbol:
            slot.current_symbol = ""
            slot.clear_quote()
        slot.set_symbol_pending(False)

    def _mark_symbol_pending(self, pid: int, symbol: str) -> None:
        slot = self.slots.get(pid)
        if not slot:
            return
        self._cancel_pending_order(pid)
        if symbol != slot.current_symbol:
            slot.current_symbol = ""
            slot.clear_quote()
        slot.set_symbol_pending(bool(symbol))

    def _handle_quote_symbol_result(self, result: dict) -> None:
        self._handle_symbol_confirm_result(
            int(result.get("panel_id") or 0),
            str(result.get("symbol") or ""),
            bool(result.get("success")),
            str(result.get("message") or ""),
            int(result.get("generation") or 0),
        )

    def _handle_quote_sync_failed(self, message: str) -> None:
        self._log_user_error_once(
            f"\u884c\u60c5\u8ba2\u9605\u540c\u6b65\u5931\u8d25\uff1a{localize_user_message(message)}",
            "warn",
        )

    def _handle_quote_subscriptions_changed(self, symbols: set[str]) -> None:
        keep = {str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()}
        self.current_quote = {
            symbol: quote for symbol, quote in self.current_quote.items() if symbol in keep
        }
        self._ts_connection.prune_quote_cache(keep)

    def _handle_symbol_confirm_result(
        self,
        pid: int,
        symbol: str,
        ok: bool,
        message: str,
        generation: int,
    ) -> None:
        if generation != self._se_generation:
            return
        slot = self.slots.get(pid)
        if not slot or slot.symbol_text() != symbol:
            return
        if not ok:
            slot.current_symbol = ""
            slot.clear_quote()
            slot.set_symbol_pending(False)
            self._log_user_error_once(f"股票查询失败：{localize_user_message(message)}", "warn")
            return
        slot.current_symbol = symbol
        slot.set_symbol_pending(False)
        quote = self._latest_quote_snapshot(symbol)
        if quote:
            self.current_quote[symbol] = quote
            slot.update_quote(quote)
        self._apply_order_options_to_slot(slot)

    def _sync_quote_subscriptions_async(self, force_resubscribe: bool = False) -> None:
        if not self.session or not self._se_connected:
            return
        for panel_id, slot in self.slots.items():
            if slot.symbol_pending and slot.symbol_text():
                self._quote_subscriptions.request_symbol(panel_id, slot.symbol_text())
        self._quote_subscriptions.reconcile(force_resubscribe=force_resubscribe)

    def _on_order_type_change(self, pid: int) -> None:
        slot = self.slots[pid]
        is_market = slot.order_type.currentText() == "Market" if slot.order_type else False
        if is_market:
            self._cancel_pending_order(pid)
            self._set_enter_target(pid, "NONE")
        if slot.price:
            slot.price.setEnabled(not is_market)
            if is_market:
                slot.price.setText("Market")
            elif slot.price.text() == "Market":
                slot.price.setText("")

    def _adj_qty(self, delta: int, pid: int) -> None:
        if pid not in self.slots or delta == 0:
            return
        self._activate_panel(pid)
        slot = self.slots[pid]
        slot.set_qty(slot.qty_value() + delta)

    def _set_qty(self, value: int, pid: int) -> None:
        if pid not in self.slots or value <= 0:
            return
        self._activate_panel(pid)
        self.slots[pid].set_qty(value)

    def _adj_price(self, delta: float, pid: int) -> None:
        if pid not in self.slots or delta == 0:
            return
        self._activate_panel(pid)
        slot = self.slots[pid]
        if not slot.price or not slot.order_type or slot.order_type.currentText() == "Market":
            return
        try:
            current = Decimal(slot.price.text().strip() or "0")
            adjusted = max(Decimal("0"), current + Decimal(str(delta)))
            slot.price.setText(f"{adjusted.quantize(Decimal('0.01')):.2f}")
        except (InvalidOperation, ValueError):
            slot.price.setText("0.00")

    def _place_order_from_panel(self, action: str, pid: int) -> None:
        self._activate_panel(pid)
        self._set_enter_target(pid, "BUY" if action.startswith("Buy") else "SELL")
        self._cancel_pending_order(pid)
        self._place_order(action, pid)

    def _on_price_enter(self, pid: int) -> None:
        self._handle_enter_target(pid)

    def _set_enter_target(self, pid: int, target: str) -> None:
        slot = self.slots.get(pid)
        if not slot:
            return
        normalized = str(target or "NONE").upper()
        if normalized not in {"NONE", "SYMBOL", "BUY", "SELL"}:
            normalized = "NONE"
        slot.enter_target = normalized
        if slot.buy:
            slot.buy.setProperty("enterSelected", normalized == "BUY")
            self._repolish(slot.buy)
        if slot.sell:
            slot.sell.setProperty("enterSelected", normalized == "SELL")
            self._repolish(slot.sell)

    def _flash_quick_order_target(self, pid: int, side: str) -> None:
        slot = self.slots.get(pid)
        if not slot or side not in {"buy", "sell"}:
            return
        target = "BUY" if side == "buy" else "SELL"
        button = slot.buy if target == "BUY" else slot.sell
        if not button:
            return
        self._quick_order_flash_serial += 1
        token = self._quick_order_flash_serial
        self._quick_order_flash_tokens[pid] = (token, target)
        existing_timer = self._quick_order_flash_timers.pop(pid, None)
        if existing_timer:
            existing_timer.stop()
            existing_timer.deleteLater()
        button.setProperty("enterSelected", True)
        self._repolish(button)

        def clear_flash() -> None:
            if self._quick_order_flash_tokens.get(pid) != (token, target):
                return
            self._quick_order_flash_tokens.pop(pid, None)
            timer = self._quick_order_flash_timers.pop(pid, None)
            if timer:
                timer.deleteLater()
            current_slot = self.slots.get(pid)
            if not current_slot or current_slot.enter_target == target:
                return
            current_button = current_slot.buy if target == "BUY" else current_slot.sell
            if current_button:
                try:
                    current_button.setProperty("enterSelected", False)
                    self._repolish(current_button)
                except RuntimeError:
                    return

        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(clear_flash)
        self._quick_order_flash_timers[pid] = timer
        timer.start(180)

    def _handle_enter_target(self, pid: int) -> None:
        if pid not in self.slots:
            return
        self._activate_panel(pid)
        slot = self.slots[pid]
        focus = QApplication.focusWidget()
        if slot.symbol and (focus is slot.symbol or focus is slot.symbol.lineEdit()):
            self._set_enter_target(pid, "SYMBOL")
        now = time.monotonic()
        if now < slot.confirm_guard_until:
            return
        slot.confirm_guard_until = now + ENTER_INPUT_GUARD_MS / 1000
        target = slot.enter_target
        if target == "SYMBOL":
            self._on_symbol_enter(pid)
        elif target in {"BUY", "SELL"} and slot.pending_action:
            self._confirm_pending_order(pid)
        elif target == "BUY":
            self._place_order("Buy to Open", pid)
        elif target == "SELL":
            self._place_order("Sell to Close", pid)

    def _shortcut_symbol(self, pid: int) -> str:
        slot = self.slots.get(pid)
        if not slot:
            return ""
        symbol = slot.symbol_text()
        if not symbol:
            self._log_user_error_once("请先输入并确认股票代码", "warn")
            return ""
        if slot.current_symbol != symbol:
            self._log_user_error_once("请先确认股票代码", "warn")
            return ""
        return symbol

    def _submit_market_order(self, side: str, pid: int) -> None:
        if side not in {"buy", "sell"} or not self._shortcut_symbol(pid):
            return
        self._cancel_pending_order(pid)
        self._set_enter_target(pid, "NONE")
        action = "Buy to Open" if side == "buy" else "Sell to Close"
        self._place_order(action, pid, order_type_override="market", price_override=0.0, source="hotkey")

    def _prepare_limit_order(self, side: str, pid: int, price_source: str = "") -> None:
        self._prepare_order_entry(
            side=side,
            pid=pid,
            order_type="limit",
            tif="Day",
            route="DEFAULT",
            hidden=False,
            price_offset=0.0,
            price_source=price_source,
        )

    def _prepare_configured_order(self, params: dict, pid: int) -> None:
        tif = str(params.get("tif") or "Day")
        is_ioc = tif.strip().upper() == "IOC"
        quick_submit = params.get("quick_submit") is True
        self._prepare_order_entry(
            side=str(params.get("side") or ""),
            pid=pid,
            order_type=str(params.get("order_type") or "limit"),
            tif=tif,
            route=str(params.get("route") or "DEFAULT"),
            hidden=bool(params.get("hidden", False)),
            price_offset=float(params.get("price_offset") or 0.0),
            price_source=str(params.get("price_source") or ""),
            configured_price_source=True,
            submit_immediately=is_ioc or quick_submit,
            submit_source="hotkey_quick" if quick_submit and not is_ioc else "hotkey",
        )

    @staticmethod
    def _quote_price_source(side: str, *, immediate: bool) -> str:
        if immediate:
            return "ask" if side == "buy" else "bid"
        return "bid" if side == "buy" else "ask"

    def _start_direct_quote_refresh(
        self,
        *,
        pid: int,
        symbol: str,
        side: str,
        order_type: str,
        tif: str,
        route: str,
        hidden: bool,
        price_offset: float,
        price_source: str,
        submit_source: str,
    ) -> None:
        is_ioc = tif.strip().upper() == "IOC"
        request_label = "IOC" if is_ioc else "快速下单"
        failure_label = "IOC订单" if is_ioc else "快速下单"
        if pid in self._direct_quote_refresh_requests:
            self._log_user_error_once(f"当前{request_label}行情正在刷新，请稍候", "warn", window_seconds=1.0)
            return
        self._direct_quote_refresh_serial += 1
        token = f"direct_quote_{self._direct_quote_refresh_serial}"
        generation = self._se_generation
        session = self.session
        self._direct_quote_refresh_requests[pid] = token

        def refresh_bg() -> None:
            try:
                refresh = getattr(session, "refresh_quote", None) if session else None
                if not callable(refresh):
                    ok, quote, message = False, {}, "行情刷新暂不可用"
                else:
                    ok, quote, message = refresh(
                        symbol,
                        price_source,
                        timeout=IOC_QUOTE_REFRESH_TIMEOUT_MS / 1000,
                    )
            except Exception:
                ok, quote, message = False, {}, f"行情刷新失败，{failure_label}未提交"
            self._ui(
                lambda: self._finish_direct_quote_refresh(
                    token,
                    pid,
                    symbol,
                    side,
                    order_type,
                    tif,
                    route,
                    hidden,
                    price_offset,
                    price_source,
                    submit_source,
                    failure_label,
                    generation,
                    bool(ok),
                    dict(quote or {}) if isinstance(quote, dict) else {},
                    str(message or ""),
                )
            )

        self._run_bg(refresh_bg)

    def _finish_direct_quote_refresh(
        self,
        token: str,
        pid: int,
        symbol: str,
        side: str,
        order_type: str,
        tif: str,
        route: str,
        hidden: bool,
        price_offset: float,
        price_source: str,
        submit_source: str,
        failure_label: str,
        generation: int,
        ok: bool,
        quote: dict,
        message: str,
    ) -> None:
        if self._direct_quote_refresh_requests.get(pid) != token:
            return
        self._direct_quote_refresh_requests.pop(pid, None)
        slot = self.slots.get(pid)
        if generation != self._se_generation or not slot or slot.symbol_text() != symbol or slot.current_symbol != symbol:
            return
        if not ok:
            self._log_user_error_once(message or f"行情刷新失败，{failure_label}未提交", "warn")
            return
        quote_value = float(quote.get(price_source, 0) or 0)
        if quote_value <= 0:
            self._log_user_error_once(f"未获取到有效行情，{failure_label}未提交", "warn")
            return
        self._ts_connection._cache_quote_message({"payload": quote}, generation)
        confirmed = self._latest_quote_snapshot(symbol)
        self._handle_quote_payload(confirmed)
        quote_value = float(confirmed.get(price_source, 0) or 0)
        if quote_value <= 0:
            self._log_user_error_once(f"未获取到有效行情，{failure_label}未提交", "warn")
            return
        adjusted = Decimal(str(quote_value)) + Decimal(str(price_offset or 0.0))
        quote_value = float(max(Decimal("0"), adjusted).quantize(Decimal("0.01")))
        if slot.price:
            slot.price.setEnabled(order_type != "market")
            if order_type != "market":
                slot.price.setText(f"{quote_value:.2f}")
        action = "Buy to Open" if side == "buy" else "Sell to Close"
        if submit_source == "hotkey_quick":
            self._flash_quick_order_target(pid, side)
        self._place_order(
            action,
            pid,
            order_type_override=order_type,
            price_override=0.0 if order_type == "market" else quote_value,
            tif_override=tif,
            route_override=route,
            hidden_override=hidden,
            source=submit_source,
        )

    def _prepare_order_entry(
        self,
        *,
        side: str,
        pid: int,
        order_type: str,
        tif: str,
        route: str,
        hidden: bool,
        price_offset: float = 0.0,
        price_source: str = "",
        configured_price_source: bool = False,
        submit_immediately: bool = False,
        submit_source: str = "hotkey",
    ) -> None:
        if side not in {"buy", "sell"} or pid not in self.slots:
            return
        slot = self.slots[pid]
        if not self.session or not self._trade_controls_enabled():
            message = self.session.broker_unavailable_message("orders") if self.session else "交易服务不可用"
            self._log_user_error_once(message, "warn")
            return
        symbol = self._shortcut_symbol(pid)
        if not symbol:
            return
        is_ioc = tif.strip().upper() == "IOC"
        if is_ioc and not self._broker_supports_tif(tif, symbol):
            self._log_user_error_once("当前交易通道不支持 IOC 订单，订单未提交", "warn")
            return

        self._activate_panel(pid)
        slot = self.slots[pid]
        self._cancel_pending_order(pid, clear_enter_target=True)
        if submit_immediately:
            self._set_enter_target(pid, "NONE")
        normalized_type = "market" if str(order_type).lower() == "market" else "limit"
        if slot.order_type:
            slot.order_type.setCurrentText("Market" if normalized_type == "market" else "Limit")
        if slot.tif:
            slot.tif.setCurrentText(tif)
        resolved_route = self._resolve_route_value(route, symbol)
        effective_hidden = bool(hidden and self._hidden_order_supported(symbol))
        if slot.route:
            if slot.route.findText(resolved_route) < 0:
                slot.route.addItem(resolved_route)
            slot.route.setCurrentText(resolved_route)
        if slot.hidden_order:
            slot.hidden_order.setChecked(effective_hidden)

        source = str(price_source or "").strip().lower() if configured_price_source else ""
        if source not in {"bid", "ask"}:
            source = self._quote_price_source(side, immediate=submit_immediately)
        quote_price = 0.0
        quote_needs_refresh = False
        if normalized_type == "limit":
            quote = self._latest_quote_snapshot(symbol)
            received_at = float(quote.get("received_monotonic", 0) or 0)
            quote_price = float(quote.get(source, 0) or 0)
            quote_age_ms = round((time.monotonic() - received_at) * 1000) if received_at > 0 else None
            generation_matches = int(quote.get("connection_generation", -1)) == self._se_generation
            fresh = (
                received_at > 0
                and generation_matches
                and quote_age_ms is not None
                and quote_age_ms <= LIMIT_IOC_QUOTE_FRESHNESS_MS
            )
            if submit_immediately and (not fresh or quote_price <= 0):
                quote_needs_refresh = True
            if quote_price > 0:
                adjusted = Decimal(str(quote_price)) + Decimal(str(price_offset or 0.0))
                quote_price = float(max(Decimal("0"), adjusted).quantize(Decimal("0.01")))
        if slot.price:
            slot.price.setEnabled(normalized_type != "market")
            slot.price.setText("Market" if normalized_type == "market" else (f"{quote_price:.2f}" if quote_price > 0 else ""))
            if not submit_immediately:
                slot.price.setFocus()
                slot.price.selectAll()
        if submit_immediately:
            if normalized_type == "limit" and quote_needs_refresh:
                self._start_direct_quote_refresh(
                    pid=pid,
                    symbol=symbol,
                    side=side,
                    order_type=normalized_type,
                    tif=tif,
                    route=resolved_route,
                    hidden=effective_hidden,
                    price_offset=price_offset,
                    price_source=source,
                    submit_source=submit_source,
                )
                return
            if normalized_type == "limit" and quote_price <= 0:
                failure_label = "IOC订单" if is_ioc else "快速下单"
                self._log_user_error_once(f"行情不可用，{failure_label}未提交", "warn")
                return
            action = "Buy to Open" if side == "buy" else "Sell to Close"
            if submit_source == "hotkey_quick":
                self._flash_quick_order_target(pid, side)
            self._place_order(
                action,
                pid,
                order_type_override=normalized_type,
                price_override=0.0 if normalized_type == "market" else quote_price,
                tif_override=tif,
                route_override=resolved_route,
                hidden_override=effective_hidden,
                source=submit_source,
            )
            return
        slot.pending_action = "Buy to Open" if side == "buy" else "Sell to Close"
        self._set_enter_target(pid, "BUY" if side == "buy" else "SELL")
        slot.pending_symbol = symbol
        slot.pending_order_type = normalized_type
        slot.pending_route = resolved_route
        slot.pending_hidden = effective_hidden
        slot.pending_created_at = time.monotonic()
        direction = "买入" if side == "buy" else "卖出"
        if normalized_type == "market":
            self._append_log(f"市价{direction}待确认：{symbol}", "inf")
        elif quote_price > 0:
            self._append_log(f"限价{direction}待确认：{symbol} @ ${quote_price:.2f}", "inf")
        else:
            self._append_log(f"限价{direction}待确认：{symbol}，请填写价格", "warn")

    def _confirm_pending_order(self, pid: int) -> None:
        slot = self.slots.get(pid)
        if not slot or not slot.pending_action:
            return
        if slot.pending_symbol != slot.symbol_text():
            self._cancel_pending_order(pid)
            self._log_user_error_once("股票代码已变化，待提交订单已取消", "warn")
            return
        action = slot.pending_action
        slot.confirm_guard_until = max(
            slot.confirm_guard_until,
            time.monotonic() + ENTER_INPUT_GUARD_MS / 1000,
        )
        order_type = slot.pending_order_type or "limit"
        route = slot.pending_route or self._resolve_route_value("DEFAULT", slot.pending_symbol)
        hidden = bool(slot.pending_hidden)
        self._cancel_pending_order(pid)
        self._place_order(
            action,
            pid,
            order_type_override=order_type,
            price_override=0.0 if order_type == "market" else None,
            route_override=route,
            hidden_override=hidden,
            source="hotkey",
        )

    def _cancel_pending_order(self, pid: int, log: bool = False, clear_enter_target: bool = False) -> None:
        slot = self.slots.get(pid)
        if not slot or not slot.pending_action:
            return
        slot.pending_action = ""
        slot.pending_symbol = ""
        slot.pending_order_type = ""
        slot.pending_route = ""
        slot.pending_hidden = False
        slot.pending_created_at = 0.0
        if clear_enter_target:
            self._set_enter_target(pid, "NONE")
        if slot.price:
            is_market = bool(slot.order_type and slot.order_type.currentText() == "Market")
            slot.price.setEnabled(not is_market)
            if is_market:
                slot.price.setText("Market")
        if log:
            self._append_log("已取消待提交状态", "inf")

    def _cancel_all_pending_orders(self) -> None:
        for pid in list(self.slots):
            self._cancel_pending_order(pid)

    def _reset_runtime_action_state(self) -> None:
        self._cancel_all_pending_orders()
        for pid in self.slots:
            self._set_enter_target(pid, "NONE")
        self._direct_quote_refresh_requests.clear()
        self._quick_order_flash_tokens.clear()
        for timer in self._quick_order_flash_timers.values():
            timer.stop()
            timer.deleteLater()
        self._quick_order_flash_timers.clear()
        self._action_limiter.reset()
        self._order_refresh.reset()
        self._canceling_order_ids.clear()
        self._batch_canceling_symbols.clear()

    @staticmethod
    def _order_signature(
        symbol: str,
        qty: int,
        price: float,
        action: str,
        order_type: str,
        tif: str,
        route: str = "",
        hidden: bool = False,
    ) -> str:
        return "|".join((symbol, action, str(qty), f"{price:.6f}", order_type, tif, route, str(bool(hidden))))

    def _rate_limit_message(self, reason: str) -> str:
        return {
            "duplicate": "相同订单提交过于频繁",
            "burst": "短时间订单过多，本次订单未提交",
            "in_flight": "已有多笔订单正在提交，请稍后",
            "cooldown": "操作过快，本次订单未提交",
        }.get(reason, "操作过快，本次请求未执行")

    def _place_order(
        self,
        action: str,
        pid: int,
        *,
        order_type_override: str | None = None,
        price_override: float | None = None,
        tif_override: str | None = None,
        route_override: str | None = None,
        hidden_override: bool | None = None,
        source: str = "button",
    ) -> None:
        if not self.session or not self._trade_controls_enabled():
            message = self.session.broker_unavailable_message("orders") if self.session else "交易服务不可用"
            self._log_user_error_once(message, "warn")
            return
        slot = self.slots[pid]
        sym = slot.symbol_text()
        qty = slot.qty_value()
        order_type = order_type_override or ("market" if slot.order_type and slot.order_type.currentText() == "Market" else "limit")
        price = float(price_override) if price_override is not None else slot.price_value()
        tif = tif_override or (slot.tif.currentText() if slot.tif else "Day")
        requested_route = route_override or (slot.route.currentText() if slot.route else "DEFAULT")
        requested_hidden = bool(hidden_override) if hidden_override is not None else bool(slot.hidden_order and slot.hidden_order.isChecked())
        if not sym:
            self._log_user_error_once("\u4e0b\u5355\u5931\u8d25\uff1a\u8bf7\u8f93\u5165\u4ee3\u7801")
            return
        if slot.current_symbol != sym:
            self._log_user_error_once("下单失败：请先确认股票代码", "warn")
            return
        if tif.strip().upper() == "IOC" and not self._broker_supports_tif(tif, sym):
            self._log_user_error_once("当前交易通道不支持 IOC 订单，订单未提交", "warn")
            return
        route = self._resolve_route_value(requested_route, sym)
        hidden = bool(requested_hidden and self._hidden_order_supported(sym))
        if not self._route_available_for_symbol(sym, route):
            self._log_user_error_once(
                f"{sym} 当前账户或股票不支持所选ROUTE {route}，订单未提交，请改用SMART",
                "warn",
            )
            return
        if qty <= 0:
            self._log_user_error_once("\u4e0b\u5355\u5931\u8d25\uff1a\u6570\u91cf\u5fc5\u987b\u5927\u4e8e 0")
            return
        if order_type != "market" and price <= 0:
            self._log_user_error_once("\u4e0b\u5355\u5931\u8d25\uff1a\u9650\u4ef7\u5fc5\u987b\u5927\u4e8e 0")
            return
        signature = self._order_signature(sym, qty, price, action, order_type, tif, route, hidden)
        decision = self._action_limiter.acquire(
            "order.submit",
            f"panel:{pid}",
            ORDER_SUBMIT_POLICY,
            signature=signature,
            identical_cooldown_ms=IDENTICAL_ORDER_COOLDOWN_MS,
        )
        if not decision.allowed:
            self._log_user_error_once(self._rate_limit_message(decision.reason), "warn", window_seconds=1.0)
            return
        price_str = "Market" if order_type == "market" else f"${price:.2f}"
        action_label = ACTION_LABELS.get(action, action)
        tif_label = TIF_LABELS.get(tif, tif)
        prefix = {
            "hotkey": "[快捷] ",
            "hotkey_quick": "[快捷直发] ",
        }.get(source, "")
        route_label = f" | {route}" if route else ""
        hidden_label = " | HIDE" if hidden else ""
        self._append_log(f"{prefix}{action_label} {qty} \u80a1 {sym} @ {price_str} | {tif_label}{route_label}{hidden_label}", "inf")
        quote = self._latest_quote_snapshot(sym)
        log.info(
            "[ORDER_DIAG][CLIENT_SUBMIT] source=%s panel=%s symbol=%s action=%s qty=%s price=%s order_type=%s tif=%s route=%s hidden=%s bid=%s ask=%s last=%s quote_age_ms=%s quote_generation=%s current_generation=%s",
            source,
            pid,
            sym,
            action,
            qty,
            f"{price:.4f}" if order_type != "market" else "MKT",
            order_type,
            tif,
            route,
            hidden,
            quote.get("bid", ""),
            quote.get("ask", ""),
            quote.get("last", ""),
            round((time.monotonic() - float(quote.get("received_monotonic", 0) or 0)) * 1000) if quote.get("received_monotonic") else "",
            quote.get("connection_generation", ""),
            self._se_generation,
        )
        generation = self._se_generation
        session = self.session
        self._run_bg(
            lambda: self._submit_order_bg(
                sym,
                qty,
                price,
                action,
                order_type,
                tif,
                route,
                hidden,
                decision.token,
                generation,
                session,
            )
        )

    def _submit_order_bg(
        self,
        symbol: str,
        qty: int,
        price: float,
        action: str,
        order_type: str,
        tif: str,
        route: str = "",
        hidden: bool = False,
        limiter_token: str = "",
        generation: int | None = None,
        session: TradingSession | None = None,
    ) -> None:
        active_session = session or self.session
        try:
            ok, msg = active_session.place_order(
                symbol,
                qty,
                price,
                action,
                order_type,
                tif=tif,
                route=route,
                hidden=hidden,
            ) if active_session else (False, "\u672a\u8fde\u63a5")
        except Exception:
            ok, msg = False, "下单失败，请稍后重试"
        self._ui(lambda: self._handle_order_result(ok, msg, limiter_token, generation))

    def _handle_order_result(self, ok: bool, msg: str, limiter_token: str = "", generation: int | None = None) -> None:
        self._action_limiter.release(limiter_token)
        if generation is not None and generation != self._se_generation:
            return
        if ok:
            self._append_log(msg, "ok")
            self._show_weak_tip(msg, "ok")
        else:
            self._log_user_error_once(msg)
            self._show_weak_tip(msg, "err")
        self._order_refresh.handle_action_result()

    def _switch_order_mode(self, mode: str) -> None:
        self._order_refresh.set_order_mode(mode)
        self._update_order_mode_buttons(self._order_refresh.order_mode)

    def _update_order_mode_buttons(self, active_mode: str) -> None:
        for mode, button in getattr(self, "_order_mode_buttons", {}).items():
            button.setProperty("selected", mode == active_mode)
            self._repolish(button)

    def _manual_refresh_allowed(self, scope: str) -> bool:
        decision = self._action_limiter.acquire("refresh.manual", scope, REFRESH_POLICY)
        self._action_limiter.release(decision.token)
        return decision.allowed

    def _manual_refresh_orders(self, _checked: bool = False) -> None:
        if self._manual_refresh_allowed("orders"):
            self._refresh_orders(force=True)

    def _manual_refresh_positions(self, _checked: bool = False) -> None:
        if self._manual_refresh_allowed("positions"):
            self._refresh_positions(force_orders=True)

    def _refresh_orders(self, *, force: bool = False) -> None:
        self._order_refresh.refresh_orders(force=force)

    def _update_orders(self, orders: list[dict]) -> None:
        self._orders_raw = orders
        rows = []
        cell_colors: list[list[str | None]] = []
        for order in orders:
            status = str(order.get("raw_status") or order.get("status") or "")
            rows.append([
                order.get("symbol", ""),
                order.get("action", ""),
                order.get("price", ""),
                order.get("qty", ""),
                order.get("otype", ""),
                order.get("tif", "Day"),
                order.get("status", ""),
            ])
            row_colors: list[str | None] = [None] * 7
            row_colors[6] = ORDER_STATUS_COLORS.get(status)
            cell_colors.append(row_colors)
        self.orders_model.set_rows(rows, cell_colors)
        self.order_count_label.setText(f"{len(rows)} \u7b14\u8ba2\u5355" if rows else "\u6682\u65e0\u8ba2\u5355")

    def _handle_orders_refresh_failed(self, message: str) -> None:
        self._log_user_error_once(f"订单刷新失败：{localize_user_message(message)}", "warn")
        count = len(self._orders_raw)
        self.order_count_label.setText(f"刷新失败 · 上次 {count} 笔" if count else "刷新失败 · 暂无可用数据")

    def _selected_order(self) -> dict:
        indexes = self.orders_table.selectionModel().selectedRows() if self.orders_table.selectionModel() else []
        if not indexes:
            return {}
        row = indexes[0].row()
        if 0 <= row < len(self._orders_raw):
            return self._orders_raw[row]
        return {}

    def _selected_order_id(self) -> str:
        return str(self._selected_order().get("id", ""))

    def _cancel_selected_order(self) -> None:
        if not self.session or not self._broker_capability_enabled("cancel_order"):
            message = self.session.broker_unavailable_message("cancel_order") if self.session else "交易服务不可用"
            self._log_user_error_once(message, "warn")
            return
        selected_order = self._selected_order()
        if selected_order and not bool(selected_order.get("can_cancel", True)):
            status = str(
                selected_order.get("raw_status")
                or selected_order.get("status")
                or ""
            ).strip()
            status_message = str(selected_order.get("status_message") or "").strip()
            status_text = {
                "Rejected": "订单未被接受，无需撤销",
                "Filled": "订单已成交，无法撤销",
                "Cancelled": "订单已经撤销",
                "Expired": "订单已过期，无法撤销",
            }.get(status, "该订单当前不可撤销")
            if status == "Rejected" and status_message:
                status_text = f"{status_text}：{localize_user_message(status_message)}"
            self._log_user_error_once(status_text, "warn")
            return
        order_id = str(selected_order.get("id", ""))
        if not order_id:
            self._log_user_error_once("\u8bf7\u9009\u62e9\u4e00\u7b14\u8981\u64a4\u9500\u7684\u8ba2\u5355", "warn")
            return
        symbol = str(selected_order.get("symbol") or "").strip().upper()
        if symbol and symbol in self._batch_canceling_symbols:
            self._log_user_error_once("当前股票批量撤单正在执行", "warn", window_seconds=1.0)
            return
        if order_id in self._canceling_order_ids:
            self._log_user_error_once("该订单正在撤销", "warn", window_seconds=1.0)
            return
        decision = self._action_limiter.acquire("order.cancel", order_id, ORDER_CANCEL_POLICY)
        if not decision.allowed:
            self._log_user_error_once("撤单操作过快", "warn", window_seconds=1.0)
            return
        self._canceling_order_ids.add(order_id)
        session = self.session
        generation = self._se_generation
        self._run_bg(lambda: self._cancel_order_bg(order_id, decision.token, generation, session))

    def _cancel_order_bg(
        self,
        order_id: str,
        limiter_token: str = "",
        generation: int | None = None,
        session: TradingSession | None = None,
    ) -> None:
        active_session = session or self.session
        try:
            ok, msg = active_session.cancel_order(order_id) if active_session else (False, "\u672a\u8fde\u63a5")
        except Exception:
            ok, msg = False, "撤单失败，请刷新订单确认"
        self._ui(lambda: self._handle_cancel_result(ok, msg, order_id, limiter_token, generation))

    def _handle_cancel_result(
        self,
        ok: bool,
        msg: str,
        order_id: str = "",
        limiter_token: str = "",
        generation: int | None = None,
    ) -> None:
        self._action_limiter.release(limiter_token)
        if order_id:
            self._canceling_order_ids.discard(order_id)
        if generation is not None and generation != self._se_generation:
            return
        if ok:
            self._append_log(msg, "ok")
        else:
            self._log_user_error_once(msg)
        self._order_refresh.handle_action_result()

    def _cancel_symbol_live_orders(self, pid: int) -> None:
        self._cancel_pending_order(pid)
        if not self.session or not self._broker_capability_enabled("cancel_order"):
            message = self.session.broker_unavailable_message("cancel_order") if self.session else "交易服务不可用"
            self._log_user_error_once(message, "warn")
            return
        if not self._broker_capability_enabled("order_query"):
            self._log_user_error_once("当前账户不支持订单查询，无法批量撤单", "warn")
            return
        symbol = self._shortcut_symbol(pid)
        if not symbol:
            return
        if symbol in self._batch_canceling_symbols:
            self._log_user_error_once("当前股票批量撤单正在执行", "warn", window_seconds=1.0)
            return
        decision = self._action_limiter.acquire("order.cancel.batch", symbol, BATCH_CANCEL_POLICY)
        if not decision.allowed:
            self._log_user_error_once("批量撤单操作过快", "warn", window_seconds=1.0)
            return
        self._batch_canceling_symbols.add(symbol)
        session = self.session
        generation = self._se_generation
        skip_order_ids = set(self._canceling_order_ids)
        self._run_bg(
            lambda: self._cancel_symbol_live_orders_bg(
                symbol,
                decision.token,
                generation,
                session,
                skip_order_ids,
            )
        )

    def _cancel_symbol_live_orders_bg(
        self,
        symbol: str,
        limiter_token: str,
        generation: int,
        session: TradingSession | None,
        skip_order_ids: set[str] | None = None,
    ) -> None:
        skipped = skip_order_ids or set()
        query_error = ""
        try:
            query = getattr(session, "query_orders", None) if session else None
            if callable(query):
                query_result = query("live")
                if query_result.success:
                    orders = list(query_result.data or [])
                else:
                    orders = []
                    query_error = str(query_result.message or "订单查询失败")
            else:
                orders = session.get_orders("live") if session else []
        except Exception:
            orders = []
            query_error = "订单查询失败，请稍后刷新"
        order_ids = list(dict.fromkeys(
            str(order.get("id") or "")
            for order in orders
            if str(order.get("symbol") or "").strip().upper() == symbol
            and order.get("id")
            and bool(order.get("can_cancel", True))
            and str(order.get("id") or "") not in skipped
            and (not order.get("raw_status") or order.get("raw_status") in LIVE_STATUSES)
        ))
        success = 0
        failures: list[str] = []
        for order_id in order_ids:
            try:
                ok, msg = session.cancel_order(order_id) if session else (False, "未连接")
            except Exception:
                ok, msg = False, "撤单失败，请刷新订单确认"
            if ok:
                success += 1
            else:
                failures.append(localize_user_message(msg))
        self._ui(
            lambda: self._handle_batch_cancel_result(
                symbol,
                len(order_ids),
                success,
                failures,
                limiter_token,
                generation,
                query_error,
            )
        )

    def _handle_batch_cancel_result(
        self,
        symbol: str,
        total: int,
        success: int,
        failures: list[str],
        limiter_token: str,
        generation: int,
        query_error: str = "",
    ) -> None:
        self._action_limiter.release(limiter_token)
        self._batch_canceling_symbols.discard(symbol)
        if generation != self._se_generation:
            return
        tip_message = ""
        tip_level = "inf"
        if query_error:
            tip_message = f"批量撤单失败：无法获取最新订单（{localize_user_message(query_error)}）"
            tip_level = "warn"
            self._log_user_error_once(
                tip_message,
                "warn",
            )
        elif total == 0:
            tip_message = f"{symbol} 暂无活动订单"
            self._append_log(tip_message, "inf")
        elif failures:
            tip_message = f"{symbol} 批量撤单：成功 {success}，失败 {len(failures)}"
            tip_level = "warn"
            self._append_log(tip_message, "warn")
        else:
            tip_message = f"{symbol} 已撤销 {success} 笔活动订单"
            tip_level = "ok"
            self._append_log(tip_message, "ok")
        if tip_message and tip_level != "warn":
            self._show_weak_tip(tip_message, tip_level)
        self._refresh_orders(force=True)

    def _refresh_positions(self, *, force_orders: bool = False) -> None:
        self._order_refresh.refresh_positions(force_orders=force_orders)

    def _update_positions(self, positions: list[dict], err: str = "") -> None:
        self._positions_raw = positions
        if err:
            self._log_user_error_once(f"Position fetch failed: {err}")
        rows = []
        total_shares = 0
        total_realized = 0.0
        total_unrealized = 0.0
        for position in positions:
            sym = position.get("symbol", "")
            qty = int(float(position.get("qty", 0) or 0))
            avg = float(position.get("avg_open", 0) or 0)
            close_px = float(self._latest_quote_snapshot(sym).get("last", position.get("close_px", 0)) or 0)
            realized = float(position.get("realized_today", 0) or 0)
            direction = position.get("direction", "")
            if qty and avg and close_px:
                unrealized = round((close_px - avg) * abs(qty) * (1 if direction == "Long" else -1), 2)
            else:
                unrealized = float(position.get("unrealized", 0) or 0)
            total_shares += abs(qty)
            total_realized += realized
            total_unrealized += unrealized
            rows.append([
                sym,
                int(position.get("qty_bot", 0) or 0),
                int(position.get("qty_sld", 0) or 0),
                qty,
                f"{avg:.4f}" if avg else "--",
                f"{close_px:.2f}" if close_px else "--",
                f"{unrealized:+.2f}",
                f"{realized:+.2f}",
                position.get("exes", 0),
            ])
        self.positions_model.set_rows(rows)
        self.metric_shares[1].setText(str(total_shares))
        self.metric_realized[1].setText(f"${total_realized:+.2f}")
        self.metric_unrealized[1].setText(f"${total_unrealized:+.2f}")

    def _handle_positions_refresh_failed(self, message: str) -> None:
        self._log_user_error_once(
            f"持仓刷新失败，显示上次数据：{localize_user_message(message)}",
            "warn",
        )

    def _on_position_double_clicked(self, index: QModelIndex) -> None:
        row = index.row()
        if 0 <= row < len(self._positions_raw):
            sym = str(self._positions_raw[row].get("symbol", "")).strip().upper()
            slot = self._active_slot()
            if sym and slot:
                panel_id = slot.panel_id
                if slot.symbol and slot.symbol.findText(sym) < 0:
                    slot.symbol.addItem(sym)
                if slot.symbol:
                    slot.symbol.setCurrentText(sym)
                self._on_symbol_enter(panel_id)

    def _on_server_disconnect(self) -> None:
        if self.session and self.session.is_auth_expired():
            self._start_authentication_recovery("AUTH_EXPIRED")
            return
        self._log_user_error_once("管理服务暂时不可用，当前交易连接保持不变", "warn")

    def closeEvent(self, event) -> None:
        try:
            if self._settings_overlay is not None:
                self._settings_overlay.hide()
                self._settings_overlay.deleteLater()
                self._settings_overlay = None
            self._teardown_shortcuts()
            self._reset_runtime_action_state()
            self._quote_subscriptions.shutdown()
            self._ts_connection.shutdown(release=True, wait=True)
            if self.session:
                try:
                    self.session.logout()
                except Exception:
                    pass
        finally:
            event.accept()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._main_ui_built:
            self._set_trade_card_effects_enabled(False)
            self._resize_effect_timer.start(120)
        self._sync_settings_overlay_geometry()
        self._position_toasts()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.WindowStateChange:
            self._window_state_layout_timer.start(0)

    def _finalize_window_state_layout(self) -> None:
        central = self.centralWidget()
        if central is not None:
            layout = central.layout()
            if layout is not None:
                layout.invalidate()
                layout.activate()
            central.update()
        self._sync_settings_overlay_geometry()
        self._position_toasts()
        self.update()


class SessionActionDialog(QDialog):
    """Small confirmation dialog aligned with the Client's main window controls."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        title: str,
        message: str,
        confirm_text: str,
    ):
        super().__init__(parent)
        self.setObjectName("sessionActionDialog")
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(420)
        self.setMaximumWidth(460)
        self.setStyleSheet(theme.POPUP_QSS)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 24, 26, 22)
        layout.setSpacing(14)
        heading = make_label(title, color=theme.TEXT_PRIMARY, font=theme.ui_font(16, bold=True))
        heading.setObjectName("dialogTitle")
        heading.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        body = make_label(message, color=theme.TEXT_DIM, font=theme.ui_font(11))
        body.setObjectName("dialogBody")
        body.setWordWrap(True)
        body.setMinimumHeight(42)
        layout.addWidget(heading)
        layout.addWidget(body)
        layout.addSpacing(4)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(10)
        actions.addStretch(1)
        cancel = make_button("取消", min_width=92)
        cancel.setObjectName("dialogCancelButton")
        cancel.setMinimumHeight(32)
        cancel.setMaximumHeight(32)
        confirm = make_button(confirm_text, min_width=96)
        confirm.setObjectName("dialogConfirmButton")
        confirm.setMinimumHeight(32)
        confirm.setMaximumHeight(32)
        cancel.clicked.connect(self.reject)
        confirm.clicked.connect(self.accept)
        actions.addWidget(cancel)
        actions.addWidget(confirm)
        layout.addLayout(actions)


class DuplicateLoginDialog(SessionActionDialog):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(
            parent,
            title="账号已在其他位置登录",
            message="确认后，旧会话将失效，并在当前 Client 继续登录。",
            confirm_text="确认接管",
        )


class ManagerLoginDialog(QDialog):
    def __init__(self, parent: QWidget | None = None, *, startup: bool = False):
        super().__init__(parent)
        self.setObjectName("managerLoginDialog")
        self.setWindowTitle("SM??")
        self.setModal(True)
        self.setMinimumWidth(420)
        self.setStyleSheet(theme.POPUP_QSS)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(14)

        title = make_label("登录", color=theme.ACCENT_BLUE, font=theme.mono_font(24, bold=True))
        title.setObjectName("loginTitle")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._username = make_input("")
        self._password = make_input("", password=True)
        form.addRow("??", self._username)
        form.addRow("??", self._password)
        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.setObjectName("dialogButtonBox")
        ok_button = buttons.button(QDialogButtonBox.Ok)
        cancel_button = buttons.button(QDialogButtonBox.Cancel)
        if ok_button:
            ok_button.setText("??")
            ok_button.setObjectName("loginConfirmButton")
        if cancel_button:
            cancel_button.setText("??" if startup else "??")
            cancel_button.setObjectName("dialogCancelButton")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._password.returnPressed.connect(self.accept)
        self._username.setFocus()

    def credentials(self) -> tuple[str, str]:
        return self._username.text().strip(), self._password.text()


def run() -> int:
    app = QApplication(sys.argv)
    app_icon = QIcon(str(APP_ICON_PATH))
    if not app_icon.isNull():
        app.setWindowIcon(app_icon)
    window = TradingTerminalQt()
    if not app_icon.isNull():
        window.setWindowIcon(app_icon)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(run())

