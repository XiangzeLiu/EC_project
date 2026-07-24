"""PySide6 Client candidate UI wired to the existing Client business layer."""

from __future__ import annotations

import datetime as dt
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QProgressBar,
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
    DEFAULT_TS_HOST,
    DEFAULT_TS_PORT,
    DEFAULT_TS_WS_URL,
    HEARTBEAT_INTERVAL,
    ORDERS_INTERVAL,
    POSITIONS_INTERVAL,
    TS_RECONNECT_ENABLED,
    TS_RECONNECT_MAX_ATTEMPTS,
)
from Client.network.http_client import HttpClient
from Client.network.ts_websocket import TSWebSocketClient
from Client.services.trading_session import TradingSession, sanitize


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


def default_ts_target() -> str:
    return DEFAULT_TS_WS_URL or f"{DEFAULT_TS_HOST}:{DEFAULT_TS_PORT}"


def encode_query_value(value: str) -> str:
    return quote(value or "", safe="")


def localize_user_message(msg: str) -> str:
    text = sanitize(msg).strip()
    if not text:
        return ""

    replacements = {
        "Trade service login succeeded": "交易服务登录成功",
        "Trade service login required": "请先登录交易服务",
        "Trade service login expired": "交易服务登录已过期",
        "Trade service login cleared": "交易服务登录已清除",
        "Trade service username and password are required": "请输入交易服务账号和密码",
        "Trade service login request timed out": "交易服务登录请求超时",
        "Trade service status query timed out": "交易服务状态查询超时",
        "Trade service logout request timed out": "交易服务登出请求超时",
        "Trade service broker not connected": "交易服务未登录",
        "Quote subscribe failed": "行情订阅失败",
        "Quote unsubscribe failed": "行情取消订阅失败",
        "Position fetch failed": "持仓获取失败",
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
        suffix = reconnect_match.group(2).strip()
        if suffix.startswith("|"):
            suffix = f" | {suffix[1:].strip()}"
        elif suffix:
            suffix = f" {suffix}"
        return f"重连中（{reconnect_match.group(1)}）...{suffix}"

    connect_target_match = re.fullmatch(r"Connecting to (.+)\.\.\.", text)
    if connect_target_match:
        return f"正在连接：{connect_target_match.group(1)}"

    authenticated_match = re.fullmatch(r"Authenticated! Session: (.+)", text)
    if authenticated_match:
        return f"鉴权成功，会话：{authenticated_match.group(1)}"

    startswith_replacements = (
        ("Trade service login failed:", "交易服务登录失败："),
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
            return repl + text[len(prefix):]

    text = text.replace("TS not connected", "交易服务器未连接")
    text = text.replace("SE not connected", "交易服务器未连接")
    text = text.replace("remote host refused connection (port may not be ready)", "远程主机拒绝连接（端口可能尚未就绪）")
    text = text.replace("broker", "券商")
    return text

def make_label(text: str, *, color: str | None = None, font=None, object_name: str | None = None) -> QLabel:
    label = QLabel(text)
    if color:
        label.setStyleSheet(f"color: {color};")
    if font:
        label.setFont(font)
    if object_name:
        label.setObjectName(object_name)
    return label


def make_pill(text: str, bg: str, fg: str, *, min_height: int = 24) -> QLabel:
    label = QLabel(text)
    label.setAlignment(Qt.AlignCenter)
    label.setMinimumHeight(min_height)
    label.setStyleSheet(f"background: {bg}; color: {fg}; border-radius: 7px; padding: 3px 9px;")
    label.setFont(theme.mono_font(9, bold=True))
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


def make_input(text: str = "", *, password: bool = False, placeholder: str = "") -> QLineEdit:
    field = QLineEdit()
    field.setText(text)
    field.setPlaceholderText(placeholder)
    field.setMinimumHeight(40)
    if password:
        field.setEchoMode(QLineEdit.Password)
    return field


def make_select(value: str, values: list[str] | None = None) -> QComboBox:
    combo = QComboBox()
    combo.addItems(values or [value])
    combo.setCurrentText(value)
    combo.setMinimumHeight(44)
    return combo


class UiSignals(QObject):
    call = Signal(object)


class DataTableModel(QAbstractTableModel):
    def __init__(self, headers: list[str], rows: list[list[object]] | None = None):
        super().__init__()
        self.headers = headers
        self.rows = rows or []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self.rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self.headers)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid() or role not in (Qt.DisplayRole, Qt.EditRole):
            return None
        try:
            return self.rows[index.row()][index.column()]
        except Exception:
            return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.headers[section]
        return None

    def set_rows(self, rows: list[list[object]]) -> None:
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()


class TradingSlot:
    def __init__(self, panel_id: int):
        self.panel_id = panel_id
        self.current_symbol = ""
        self.symbol: QComboBox | None = None
        self.order_type: QComboBox | None = None
        self.tif: QComboBox | None = None
        self.qty_label: QLabel | None = None
        self.price: QLineEdit | None = None
        self.last: QLabel | None = None
        self.bid: QLabel | None = None
        self.ask: QLabel | None = None
        self.buy: QPushButton | None = None
        self.sell: QPushButton | None = None
        self.minus: QPushButton | None = None
        self.plus: QPushButton | None = None

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
            self.qty_label.setText(str(max(0, int(qty))))

    def price_value(self) -> float:
        if self.order_type and self.order_type.currentText() == "Market":
            return 0.0
        text = self.price.text().strip() if self.price else ""
        try:
            return float(text)
        except ValueError:
            return 0.0

    def set_trade_enabled(self, enabled: bool) -> None:
        for widget in (self.buy, self.sell):
            if widget:
                widget.setEnabled(enabled)

    def update_quote(self, quote: dict) -> None:
        if self.last:
            self.last.setText(f"{float(quote.get('last', 0) or 0):.2f}")
        if self.bid:
            self.bid.setText(f"{float(quote.get('bid', 0) or 0):.2f}")
        if self.ask:
            self.ask.setText(f"{float(quote.get('ask', 0) or 0):.2f}")



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
        self._log_rows: list[tuple[str, str, str]] = []
        self._main_ui_built = False
        self._init_ready = False
        self._startup_login_required = True
        self._login_dialog_open = False
        self._login_username = ""
        self._login_password = ""
        self._last_heartbeat = 0.0
        self._last_pos_time = 0.0
        self._last_orders_time = 0.0
        self._last_ui_error_message = ""
        self._last_ui_error_at = 0.0
        self._last_reconnect_notice_attempt = 0
        self._reconnect_failed = False
        self._order_mode = "live"
        self._orders_raw: list[dict] = []
        self._positions_raw: list[dict] = []
        self.current_quote: dict[str, dict] = {}
        self._se_client = None
        self._se_generation = 0
        self._se_connected = False
        self._se_target_address = ""
        self._se_server_id = ""
        self._se_connection_id = ""
        self._quote_requested_symbols: set[str] = set()
        self._quote_subscribed_symbols: set[str] = set()
        self._quote_sub_lock = threading.Lock()
        self._ui_backdoor_mode = False

        root = QWidget()
        root.setObjectName("root")
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

        title = make_label("SC  登录", color=theme.ACCENT_BLUE, font=theme.mono_font(30, bold=True))
        title.setAlignment(Qt.AlignCenter)
        title.setMinimumHeight(40)
        title.setStyleSheet(f'color: {theme.ACCENT_BLUE}; font-size: 30px; font-weight: 900; letter-spacing: 1px; line-height: 1.0;')
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
            field.setStyleSheet(
                f"background: {theme.INPUT_BG}; color: {theme.TEXT_PRIMARY}; border: 1px solid {theme.BORDER}; "
                "border-radius: 8px; padding: 4px 10px; font-size: 14px; font-weight: 700;"
            )
            row_layout.addWidget(label, 0, 0, alignment=Qt.AlignRight | Qt.AlignVCenter)
            row_layout.addWidget(field, 0, 1, alignment=Qt.AlignCenter)
            return row

        self._login_user_entry = make_input("")
        self._login_pass_entry = make_input("", password=True)
        form_wrap_layout.addWidget(login_field("账号", self._login_user_entry))
        form_wrap_layout.addWidget(login_field("密码", self._login_pass_entry))
        login_layout.addWidget(form_wrap, alignment=Qt.AlignHCenter)
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
        self._login_exit_btn.setStyleSheet(
            f"background: {theme.PANEL_ALT_BG}; color: {theme.TEXT_DIM}; border: 1px solid {theme.PANEL_ALT_BG}; "
            "border-radius: 8px; font-size: 14px; font-weight: 700; padding: 8px 16px; min-height: 34px;"
        )
        self._login_submit_btn = make_button("登录", object_name="loginButton", min_width=128)
        self._login_submit_btn.setStyleSheet(
            f"background: {theme.ACCENT_BLUE}; color: #07121B; border: 1px solid {theme.ACCENT_BLUE}; "
            "border-radius: 8px; font-size: 14px; font-weight: 700; padding: 8px 16px; min-height: 34px;"
        )
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
        self._init_progress.setStyleSheet(
            f"QProgressBar {{ background: #05070A; border: none; border-radius: 4px; }} "
            f"QProgressBar::chunk {{ background: {theme.ACCENT_BLUE}; border-radius: 4px; }}"
        )
        status_layout.addWidget(self._init_progress)

        self._init_steps = {}
        for key, caption, default, color in (
            ("auth", "账号登录", "等待中", theme.TEXT_MUTED),
            ("sm", "管理服务", "等待中", theme.TEXT_MUTED),
            ("se", "交易服务", "等待中", theme.TEXT_MUTED),
        ):
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

        self._init_hint_label = make_label("", color=theme.ACCENT_RED, font=theme.ui_font(10))
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
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)
        self._tick()

        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll)
        self._poll_timer.start(150)

        QTimer.singleShot(200, self._show_startup_login)

    def _ui(self, fn) -> None:
        self._signals.call.emit(fn)

    def _run_bg(self, fn) -> None:
        threading.Thread(target=fn, daemon=True).start()

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
        if username == "dev" and password == "dev":
            self._set_init_hint("")
            self._enter_dev_main_interface(username)
            return
        self._set_init_hint("")
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

    def _set_init_actions_visible(self, visible: bool) -> None:
        for btn in (self._retry_btn, self._cancel_btn):
            if btn:
                btn.setVisible(visible)

    def _build_root(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_header())
        layout.addWidget(self._build_workspace(), 1)

    def _build_header(self) -> QFrame:
        header = QFrame()
        header.setObjectName("topHeader")
        header.setFixedHeight(58)
        layout = QHBoxLayout(header)
        layout.setContentsMargins(14, 8, 14, 8)
        layout.setSpacing(16)

        logo = QLabel("S C")
        logo.setAlignment(Qt.AlignCenter)
        logo.setMinimumWidth(46)
        logo.setMaximumHeight(32)
        logo.setStyleSheet(
            f"background: {theme.ACCENT_BLUE}; color: #07121B; "
            f"border: 1px solid {theme.ACCENT_BLUE}; border-radius: 8px; padding: 2px 10px;"
        )
        logo.setFont(theme.ui_font(10, bold=True))
        layout.addWidget(logo)

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
        status_layout.addWidget(self.status_dot)
        status_layout.addWidget(self.status_text)
        layout.addWidget(status)


        broker = QWidget()
        broker_layout = QHBoxLayout(broker)
        broker_layout.setContentsMargins(0, 0, 0, 0)
        broker_layout.setSpacing(7)
        self.broker_user_entry = make_input("", placeholder="账号")
        self.broker_user_entry.setFixedWidth(168)
        self.broker_pass_entry = make_input("", password=True, placeholder="密码")
        self.broker_pass_entry.setFixedWidth(168)
        self.broker_login_btn = make_button("登录", object_name="loginButton", min_width=64)
        self.broker_login_btn.clicked.connect(self._broker_login)
        self.broker_pass_entry.returnPressed.connect(self._broker_login)
        broker_layout.addWidget(self.broker_user_entry)
        broker_layout.addWidget(self.broker_pass_entry)
        broker_layout.addWidget(self.broker_login_btn)
        layout.addWidget(broker)

        layout.addStretch(1)
        self.latency_label = make_label("--ms", color=theme.TEXT_LOW, font=theme.mono_font(9))
        layout.addWidget(self.latency_label)

        clock = QWidget()
        clock_layout = QHBoxLayout(clock)
        clock_layout.setContentsMargins(0, 0, 0, 0)
        clock_layout.setSpacing(8)
        clock_layout.addWidget(make_label("CN Time", color=theme.TEXT_MUTED, font=theme.ui_font(9)))
        clock_box = QFrame()
        clock_box.setStyleSheet(f"background: #080A0D; border: 1px solid {theme.BORDER_SOFT}; border-radius: 7px; padding: 3px 8px;")
        clock_box_layout = QHBoxLayout(clock_box)
        clock_box_layout.setContentsMargins(8, 3, 8, 3)
        self._clock = QLabel()
        self._clock.setFont(theme.mono_font(9))
        self._clock.setStyleSheet("color: #F1F3F5;")
        self._tick()
        clock_box_layout.addWidget(self._clock)
        clock_layout.addWidget(clock_box)
        layout.addWidget(clock)
        return header


    def _build_workspace(self) -> QWidget:
        workspace = QWidget()
        layout = QVBoxLayout(workspace)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(14)

        slot_row = QHBoxLayout()
        slot_row.setSpacing(20)
        slot_row.addWidget(self._build_slot(1, "", "100", "", -10, 10))
        slot_row.addWidget(self._build_slot(2, "", "1", "", -1, 1))
        layout.addLayout(slot_row)

        middle = QHBoxLayout()
        middle.setSpacing(20)
        middle.addWidget(self._build_orders_panel(), 1)
        middle.addWidget(self._build_positions_panel(), 1)
        layout.addLayout(middle, 1)

        layout.addWidget(self._build_console())
        return workspace

    def _build_slot(self, idx: int, symbol: str, qty: str, price: str, minus_step: int, plus_step: int) -> QFrame:
        slot = TradingSlot(idx)
        self.slots[idx] = slot
        card = QFrame()
        card.setObjectName("slotCard")
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
        slot.symbol.currentTextChanged.connect(lambda _text, pid=idx: self._schedule_quote_sync(pid))
        slot_grid.addWidget(self._control_block("SYMBOL", slot.symbol), 0, 0)

        quote_box, slot.last, slot.bid, slot.ask = self._build_quote_box()
        slot_grid.addWidget(quote_box, 0, 1)

        slot.order_type = make_select("Limit", ["Limit", "Market"])
        slot.order_type.currentTextChanged.connect(lambda _text, pid=idx: self._on_order_type_change(pid))
        slot_grid.addWidget(self._control_block("TYPE", slot.order_type), 1, 0)

        right_config = QWidget()
        right_config_layout = QHBoxLayout(right_config)
        right_config_layout.setContentsMargins(0, 0, 0, 0)
        right_config_layout.setSpacing(14)
        slot.tif = make_select("Day", ["Day", "GTC", "IOC", "EXT", "GTC_EXT"])
        right_config_layout.addWidget(self._control_block("TIF", slot.tif), 1)

        qty_box, slot.qty_label, slot.minus, slot.plus = self._build_qty(qty, minus_step, plus_step)
        slot.minus.clicked.connect(lambda _checked=False, pid=idx, delta=minus_step: self._adj_qty(delta, pid))
        slot.plus.clicked.connect(lambda _checked=False, pid=idx, delta=plus_step: self._adj_qty(delta, pid))
        right_config_layout.addWidget(self._control_block("QTY", qty_box), 1)
        slot_grid.addWidget(right_config, 1, 1)

        slot.price = make_input(price)
        slot.price.returnPressed.connect(lambda pid=idx: self._place_order("Buy to Open", pid))
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
        slot.buy.setMinimumHeight(44)
        slot.sell.setMinimumHeight(44)
        slot.buy.clicked.connect(lambda _checked=False, pid=idx: self._place_order("Buy to Open", pid))
        slot.sell.clicked.connect(lambda _checked=False, pid=idx: self._place_order("Sell to Close", pid))
        button_row_layout.addWidget(slot.buy, 1)
        button_row_layout.addWidget(slot.sell, 1)
        buttons_layout.addWidget(button_row)
        slot_grid.addWidget(buttons, 2, 1)

        layout.addLayout(slot_grid)
        return card

    def _build_quote_box(self) -> tuple[QFrame, QLabel, QLabel, QLabel]:
        box = QFrame()
        box.setObjectName("inputBox")
        box.setMinimumHeight(72)
        layout = QGridLayout(box)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setHorizontalSpacing(8)
        labels: list[QLabel] = []
        for col, (caption, color) in enumerate((("LAST", theme.TEXT_MUTED), ("BID", theme.ACCENT_GREEN), ("ASK", theme.ACCENT_RED))):
            layout.addWidget(make_label(caption, color=theme.TEXT_LOW, font=theme.mono_font(9)), 0, col, alignment=Qt.AlignHCenter | Qt.AlignBottom)
            value = make_label("--", color=color, font=theme.mono_font(11))
            value.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
            layout.addWidget(value, 1, col, alignment=Qt.AlignHCenter | Qt.AlignTop)
            layout.setColumnStretch(col, 1)
            labels.append(value)
        return box, labels[0], labels[1], labels[2]

    def _build_qty(self, value: str, minus_step: int, plus_step: int) -> tuple[QFrame, QLabel, QPushButton, QPushButton]:
        box = QFrame()
        box.setObjectName("inputBox")
        box.setMinimumHeight(44)
        layout = QHBoxLayout(box)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(3)
        minus = make_button(str(minus_step), object_name="qtyStepButton", min_width=28)
        minus.setFixedWidth(34)
        plus = make_button(f"+{plus_step}" if plus_step > 0 else str(plus_step), object_name="qtyStepButton", min_width=28)
        plus.setFixedWidth(34)
        qty = make_label(value, color=theme.TEXT_PRIMARY, font=theme.mono_font(11))
        qty.setAlignment(Qt.AlignCenter)
        qty.setMinimumWidth(30)
        layout.addWidget(minus)
        layout.addWidget(qty, 1)
        layout.addWidget(plus)
        return box, qty, minus, plus

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
        self.live_orders_btn = make_button("\u25cf \u5b9e\u65f6")
        self.all_orders_btn = make_button("All")
        self.order_count_label = make_label("\u6682\u65e0\u8ba2\u5355", color=theme.TEXT_DIM, font=theme.ui_font(9))
        self.cancel_order_btn = make_button("\u64a4\u5355", object_name="consoleButton", min_width=60)
        self.orders_refresh_btn = make_button("\u5237\u65b0", object_name="refreshButton")
        self.live_orders_btn.clicked.connect(lambda: self._switch_order_mode("live"))
        self.all_orders_btn.clicked.connect(lambda: self._switch_order_mode("all"))
        self.cancel_order_btn.clicked.connect(self._cancel_selected_order)
        self.orders_refresh_btn.clicked.connect(self._refresh_orders)
        tabs.addWidget(self.live_orders_btn)
        tabs.addWidget(self.all_orders_btn)
        tabs.addWidget(self.order_count_label)
        tabs.addStretch(1)
        tabs.addWidget(self.cancel_order_btn)
        tabs.addWidget(self.orders_refresh_btn)
        body.addWidget(head)
        self.orders_model = DataTableModel(["\u4ee3\u7801", "\u65b9\u5411", "\u4ef7\u683c", "\u6570\u91cf", "\u7c7b\u578b", "\u6709\u6548\u671f", "\u72b6\u6001"])
        self.orders_table = self._make_table(self.orders_model)
        body.addWidget(self.orders_table, 1)
        return panel

    def _build_positions_panel(self) -> QFrame:
        panel, body = self._make_data_panel()
        head = self._make_panel_header()
        head_layout = QHBoxLayout(head)
        head_layout.setContentsMargins(12, 7, 12, 7)
        head_layout.addWidget(make_label("\u6301\u4ed3\u4e0e\u76c8\u4e8f", color=theme.TEXT_PRIMARY, font=theme.ui_font(10, bold=True)))
        head_layout.addStretch(1)
        self.positions_refresh_btn = make_button("\u5237\u65b0", object_name="refreshButton")
        self.positions_refresh_btn.clicked.connect(self._refresh_positions)
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
        self.positions_table.clicked.connect(self._on_position_clicked)
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
        table.setModel(model)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        table.verticalHeader().hide()
        table.setShowGrid(False)
        table.setSelectionBehavior(QTableView.SelectRows)
        table.setSelectionMode(QTableView.SingleSelection)
        return table

    def _build_console(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("dataPanel")
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

    def _poll(self) -> None:
        now = time.time()
        if self.session and self.session.connected:
            if now - self._last_heartbeat > HEARTBEAT_INTERVAL / 1000:
                self._last_heartbeat = now
                threading.Thread(target=self._heartbeat_check, daemon=True).start()
            if now - self._last_pos_time > POSITIONS_INTERVAL / 1000 and self._trade_controls_enabled():
                self._last_pos_time = now
                self._refresh_positions()
            if now - self._last_orders_time > ORDERS_INTERVAL / 1000 and self._trade_controls_enabled():
                self._last_orders_time = now
                self._refresh_orders()

    def _heartbeat_check(self) -> None:
        if not self.http.is_connected:
            return
        if not self.http.health_check():
            self._ui(lambda: self._on_server_disconnect())
    def _set_ts_connection_state(self, state: str, detail: str = "") -> None:
        state = (state or "offline").strip().lower()
        self._se_connected = state == "online"
        if state == "online":
            self._reconnect_failed = False
            self._last_reconnect_notice_attempt = 0
        if not self._main_ui_built:
            return
        if state == "online":
            self.status_dot.setStyleSheet(f"color: {theme.ACCENT_GREEN};")
            self.status_text.setText("ONLINE")
            self.status_text.setStyleSheet(f"color: {theme.ACCENT_GREEN};")
        elif state == "reconnecting":
            self.status_dot.setStyleSheet(f"color: {theme.ACCENT_YELLOW};")
            self.status_text.setText("RECONNECTING")
            self.status_text.setStyleSheet(f"color: {theme.ACCENT_YELLOW};")
            self.latency_label.setText("重连中")
            self.latency_label.setStyleSheet(f"color: {theme.ACCENT_YELLOW};")
        elif state == "failed":
            self.status_dot.setStyleSheet(f"color: {theme.ACCENT_RED};")
            self.status_text.setText("FAILED")
            self.status_text.setStyleSheet(f"color: {theme.ACCENT_RED};")
            self.latency_label.setText("--ms")
            self.latency_label.setStyleSheet(f"color: {theme.TEXT_LOW};")
        else:
            self.status_dot.setStyleSheet(f"color: {theme.ACCENT_RED};")
            self.status_text.setText("OFFLINE")
            self.status_text.setStyleSheet(f"color: {theme.ACCENT_RED};")
            self.latency_label.setText("--ms")
            self.latency_label.setStyleSheet(f"color: {theme.TEXT_LOW};")
        self._apply_broker_gate_ui()

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

    def _recover_to_login_after_reconnect_failure_bg(self) -> None:
        try:
            if self.session:
                try:
                    self.session.bind_se_client(None)
                    self.session.broker_logout()
                except Exception:
                    pass
            if self._se_client:
                try:
                    self._se_client.stop()
                except Exception:
                    pass
                self._se_client = None
            with self._quote_sub_lock:
                self._quote_subscribed_symbols.clear()
            self._release_se_occupation(sync=True)
            if self.session:
                try:
                    self.session.logout()
                except Exception:
                    pass
        finally:
            self._ui(lambda: self._reset_to_login_page("交易服务器重连失败，已释放占用，请重新登录。"))

    def _reset_to_login_page(self, hint: str = "") -> None:
        self.session = None
        self._main_ui_built = False
        self._init_ready = False
        self._startup_login_required = True
        self._login_dialog_open = False
        self._login_username = ""
        self._login_password = ""
        self._last_heartbeat = 0.0
        self._last_pos_time = 0.0
        self._last_orders_time = 0.0
        self._last_ui_error_message = ""
        self._last_ui_error_at = 0.0
        self._last_reconnect_notice_attempt = 0
        self._reconnect_failed = False
        self._order_mode = "live"
        self._orders_raw = []
        self._positions_raw = []
        self.current_quote = {}
        self.slots = {}
        self._se_client = None
        self._se_generation += 1
        self._se_connected = False
        self._se_target_address = ""
        self._se_server_id = ""
        self._se_connection_id = ""
        self._quote_requested_symbols.clear()
        with self._quote_sub_lock:
            self._quote_subscribed_symbols.clear()
        self._log_rows = []
        self._build_login_root(hint)

    def _build_login_root(self, hint: str = "") -> None:
        root = QWidget()
        root.setObjectName("root")
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
        title = make_label("SC  登录", color=theme.ACCENT_BLUE, font=theme.mono_font(30, bold=True))
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
        login_layout.addSpacing(46)
        login_buttons = QWidget()
        login_buttons.setMaximumWidth(560)
        login_button_layout = QGridLayout(login_buttons)
        login_button_layout.setContentsMargins(0, 0, 0, 0)
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


    def _broker_gate_state(self) -> dict:
        raw = getattr(self.session, "broker_gate", None) if self.session else None
        if isinstance(raw, dict):
            return raw
        return {"active": False, "status": "not_logged_in", "display": "\u4ea4\u6613\u670d\u52a1\u672a\u767b\u5f55"}

    def _trade_controls_enabled(self) -> bool:
        return bool(self.session and self.session.connected and self._se_connected and getattr(self.session, "broker_gate_active", False))

    def _apply_broker_gate_ui(self) -> None:
        if not self._main_ui_built:
            return
        gate = self._broker_gate_state()
        active = bool(gate.get("active") and self.session and self.session.connected and self._se_connected)
        enabled = self._trade_controls_enabled()
        display = "DEV UI 后门已解锁" if self._ui_backdoor_mode else (gate.get("display") or ("\u4ea4\u6613\u670d\u52a1\u5df2\u767b\u5f55" if active else "\u4ea4\u6613\u670d\u52a1\u672a\u767b\u5f55"))
        if hasattr(self, "account_state"):
            style_status_pill(self.account_state, "DEV" if self._ui_backdoor_mode else ("Connect" if active else "Disconnect"), active=True, danger=not active)
        self.broker_login_btn.setEnabled(bool(self.session and self.session.connected and self._se_connected))
        self.broker_login_btn.setText("\u5df2\u767b\u5f55" if active else "登录")
        self.broker_user_entry.setEnabled(not active and not self._ui_backdoor_mode)
        self.broker_pass_entry.setEnabled(not active and not self._ui_backdoor_mode)
        for slot in self.slots.values():
            slot.set_trade_enabled(enabled)
        self.orders_refresh_btn.setEnabled(enabled)
        self.positions_refresh_btn.setEnabled(enabled)
        self.cancel_order_btn.setEnabled(enabled)
        if active:
            self._append_log(str(display), "ok", dedupe=True)

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

    def _log_user_error_once(self, msg: str, tag: str = "err", window_seconds: float = 3.0) -> None:
        text = localize_user_message(msg)
        now = time.time()
        if text == self._last_ui_error_message and now - self._last_ui_error_at < window_seconds:
            return
        self._last_ui_error_message = text
        self._last_ui_error_at = now
        self._append_log(text, tag)

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
            if username == "dev" and password == "dev":
                self._startup_login_required = False
                self._enter_dev_main_interface(username)
                return
            self._startup_login_required = startup
            self._run_bg(lambda: self._login_manager(username, password, False))
        finally:
            self._login_dialog_open = False

    def login_manager_for_test(self, username: str, password: str, force: bool = False) -> None:
        self._run_bg(lambda: self._login_manager(username, password, force))

    def _login_manager(self, username: str, password: str, force: bool = False) -> None:
        self._ui_backdoor_mode = False
        self.session = TradingSession(self.http)
        ok, msg = self.session.login(username, password, force=force)
        self._ui(lambda: self._handle_manager_login_result(ok, msg, username, password, force))

    def _enter_dev_main_interface(self, username: str) -> None:
        self._ui_backdoor_mode = True
        self.session = TradingSession(self.http)
        self.session.connected = True
        self.session.mock_mode = False
        self.session.se_address = default_ts_target()
        self.http.token = "dev-ui-backdoor"
        self._login_username = username
        self._login_password = ""
        self._startup_login_required = False
        self._se_target_address = self.session.se_address
        self._se_connected = False
        self._show_connection_page()
        self._update_init_step("auth", "本地后门", theme.ACCENT_BLUE)
        self._update_init_step("sm", "已跳过", theme.TEXT_MUTED)
        self._update_init_step("se", "未连接", theme.TEXT_MUTED)
        self._enter_main_interface()

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
        if self._se_client and self._se_client.is_active:
            return
        target_addr = self._se_target_address or default_ts_target()
        self._append_log("\u6b63\u5728\u8fde\u63a5\u4ea4\u6613\u670d\u52a1\u5668", "inf")
        self._run_bg(lambda: self._validate_and_connect_ts(target_addr))

    def _validate_and_connect_ts(self, target_addr: str) -> None:
        try:
            self._ui(lambda: self._update_init_step("se", "\u6821\u9a8c\u4e2d...", theme.ACCENT_YELLOW))
            status_code, resp_data = self.http.get(f"/api/accounts/se-status?address={encode_query_value(target_addr)}")
            if status_code == 200 and resp_data.get("ok"):
                if not resp_data.get("online"):
                    self._ui(lambda: self._on_init_failed("se", "\u4ea4\u6613\u670d\u52a1\u5668\u5f53\u524d\u79bb\u7ebf", "\u6240\u5206\u914d\u7684\u4ea4\u6613\u670d\u52a1\u5668\u76ee\u524d\u79bb\u7ebf\uff0c\u8bf7\u8054\u7cfb\u7ba1\u7406\u5458\u3002"))
                    return
                occupied_by = (resp_data.get("occupied_by") or "").strip()
                if occupied_by and occupied_by != self._login_username:
                    self._ui(lambda ob=occupied_by: self._on_init_failed("se", "\u4ea4\u6613\u670d\u52a1\u5668\u5df2\u88ab\u5360\u7528", f"\u5f53\u524d\u4ea4\u6613\u670d\u52a1\u5668\u5df2\u88ab\u8d26\u6237\u201c{ob}\u201d\u5360\u7528\uff0c\u65e0\u6cd5\u8fde\u63a5\u3002"))
                    return
                self._se_server_id = resp_data.get("server_id", "")
                self._se_target_address = target_addr
            else:
                self._ui(lambda: self._on_init_failed("se", "\u4ea4\u6613\u670d\u52a1\u5668\u6821\u9a8c\u5931\u8d25", ""))
                return
            self._connect_ts_with_retry(target_addr)
        except Exception as exc:
            self._ui(lambda e=exc: self._on_init_failed("se", "\u4ea4\u6613\u670d\u52a1\u5668\u6821\u9a8c\u5931\u8d25", str(e)))

    def _connect_ts_with_retry(self, target_addr: str) -> None:
        target = target_addr or default_ts_target()
        endpoint = TSWebSocketClient.normalize_endpoint(target, default_port=DEFAULT_TS_PORT)
        self._last_connected_se = endpoint
        self._se_generation += 1
        generation = self._se_generation
        client = TSWebSocketClient(
            ws_url=endpoint,
            port=DEFAULT_TS_PORT,
            token=self.http.token,
            server_id=self._se_server_id,
            on_message_callback=self._wrap_se_message_handler(generation),
            on_status_callback=self._wrap_se_status_handler(generation),
            on_latency_callback=self._wrap_ts_latency_handler(generation),
            on_reconnect_prepare_callback=lambda attempt, connection_id, gen=generation: self._prepare_ts_reconnect(gen, attempt, connection_id),
            on_state_callback=self._wrap_se_state_handler(generation),
            reconnect_enabled=TS_RECONNECT_ENABLED,
        )
        self._se_client = client
        self._se_connection_id = client.connection_id
        if not self._occupy_se_node(connection_id=client.connection_id, sync=True):
            self._se_client = None
            self._ui(lambda: self._on_init_failed(
                "se",
                "\u4ea4\u6613\u670d\u52a1\u5668\u9501\u5b9a\u5931\u8d25",
                "\u8282\u70b9\u5360\u7528\u6ce8\u518c\u672a\u6210\u529f\uff0c\u65e0\u6cd5\u786e\u4fdd\u72ec\u5360\u6743\u3002",
                release_occupation=False,
            ))
            return
        client.start()

    def _wrap_se_status_handler(self, generation: int):
        def handler(msg: str) -> None:
            def apply() -> None:
                if generation != self._se_generation:
                    return
                if self._init_ready:
                    self._handle_se_status_ui(msg)
                else:
                    self._handle_init_se_status_ui(msg)
            self._ui(apply)
        return handler

    def _wrap_se_state_handler(self, generation: int):
        def handler(state: str, detail: dict) -> None:
            def apply() -> None:
                if generation != self._se_generation:
                    return
                self._handle_se_connection_state_ui(state, detail or {})
            self._ui(apply)
        return handler

    def _wrap_se_message_handler(self, generation: int):
        def handler(msg: dict) -> None:
            def apply() -> None:
                if generation != self._se_generation:
                    return
                if self._init_ready:
                    self._handle_se_message_ui(msg)
                else:
                    self._handle_init_se_message_ui(msg)
            self._ui(apply)
        return handler

    def _wrap_ts_latency_handler(self, generation: int):
        def handler(latency_ms: int) -> None:
            def apply() -> None:
                if generation != self._se_generation or not self._main_ui_built or not self._se_connected:
                    return
                color = theme.ACCENT_GREEN if latency_ms < 120 else theme.ACCENT_YELLOW if latency_ms < 300 else theme.ACCENT_RED
                self.latency_label.setText(f"{latency_ms}ms")
                self.latency_label.setStyleSheet(f"color: {color};")
            self._ui(apply)
        return handler

    def _prepare_ts_reconnect(self, generation: int, attempt: int, connection_id: str) -> bool:
        if generation != self._se_generation or self._reconnect_failed:
            return False
        if not self.session or not self.session.connected:
            return False
        target = self._se_target_address or getattr(self, "_last_connected_se", "") or default_ts_target()
        try:
            status_code, resp_data = self.http.get(f"/api/accounts/se-status?address={encode_query_value(target)}")
        except Exception:
            return False
        if status_code != 200 or not (resp_data or {}).get("ok") or not (resp_data or {}).get("online"):
            return False
        occupied_by = ((resp_data or {}).get("occupied_by") or "").strip()
        if occupied_by and occupied_by != self._login_username:
            return False
        server_id = ((resp_data or {}).get("server_id") or "").strip()
        if self._se_server_id and server_id and server_id != self._se_server_id:
            return False
        if server_id:
            self._se_server_id = server_id
            self._se_target_address = target
        if not self._se_server_id:
            return False
        return self._occupy_se_node(connection_id=connection_id, sync=True)

    def _handle_se_connection_state_ui(self, state: str, detail: dict) -> None:
        if state == "authenticated":
            if self._init_ready:
                was_reconnecting = self._last_reconnect_notice_attempt > 0
                self._set_se_connection_ui(True)
                if self.session:
                    self.session.bind_se_client(self._se_client)
                self._append_log("交易服务器重连成功" if was_reconnecting else "交易服务器已连接", "ok", dedupe=True)
                self._refresh_broker_gate_async(log_errors=False)
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
            if self._init_ready:
                self._handle_ts_reconnecting(f"Reconnecting ({attempt})")
            else:
                self._update_init_step("se", f"连接中 ({attempt}/{TS_RECONNECT_MAX_ATTEMPTS})...", theme.ACCENT_YELLOW)
            return

        if state in ("auth_failed", "retry_exhausted"):
            reason = str(detail.get("message") or detail.get("reason") or "连接失败")
            if self._init_ready:
                self._start_reconnect_failure_recovery(reason)
            else:
                self._on_init_failed("se", "无法连接到交易服务器", reason)
            return

        if state == "force_disconnected":
            self._set_ts_connection_state("offline")

    def _handle_init_se_status_ui(self, msg: str) -> None:
        if "Connecting" in msg or "连接" in msg:
            self._update_init_step("se", "连接中...", theme.ACCENT_YELLOW)

    def _handle_init_se_message_ui(self, msg: dict) -> None:
        msg_type = msg.get("type", "")
        payload = msg.get("payload", {}) if isinstance(msg.get("payload", {}), dict) else {}
        if msg_type == "CONNECT_ACK" or msg.get("event") == "connected":
            if msg.get("event") == "connected" and isinstance(msg.get("data"), dict):
                payload = msg["data"].get("payload", {}) or {}
            gate = payload.get("broker_gate")
            if self.session and isinstance(gate, dict):
                self.session._set_broker_gate(gate)

    def _handle_se_status_ui(self, msg: str) -> None:
        if not any(key in msg for key in ("Authenticated", "Reconnect failed after", "Reconnecting", "Connecting", "Disconnected:")):
            self._append_log(msg, "inf", dedupe=True)

    def _handle_se_message_ui(self, msg: dict) -> None:
        msg_type = msg.get("type", "")
        payload = msg.get("payload", {}) if isinstance(msg.get("payload", {}), dict) else {}
        if msg_type in ("CONNECT_ACK", "STATUS_RESPONSE"):
            gate = payload.get("broker_gate")
            if self.session and isinstance(gate, dict):
                self.session._set_broker_gate(gate)
            self._apply_broker_gate_ui()
        elif msg_type in ("BROKER_LOGIN_RESPONSE", "BROKER_STATUS_RESPONSE", "BROKER_LOGOUT_RESPONSE"):
            gate = payload.get("gate") or payload.get("broker_gate")
            if self.session and isinstance(gate, dict):
                self.session._set_broker_gate(gate)
            self._apply_broker_gate_ui()
            if msg_type == "BROKER_LOGIN_RESPONSE" and payload.get("success"):
                self._refresh_positions()
                self._refresh_orders()
        elif msg_type == "QUOTE_DATA":
            self._handle_quote_payload(payload)
        elif msg_type == "FORCE_DISCONNECT":
            reason = payload.get("reason", "admin_force_release")
            self._log_user_error_once(f"交易服务器连接被强制断开，原因：{reason}", "warn")
            self._se_disconnect()
        elif msg_type == "ERROR":
            if payload.get("code") in ("BROKER_LOGIN_REQUIRED", "BROKER_CREDENTIALS_REQUIRED"):
                self._refresh_broker_gate_async(log_errors=False)
            code = payload.get("code", "")
            message = localize_user_message(payload.get("message", ""))
            self._log_user_error_once(f"交易服务器错误[{code}]：{message}")
    def _on_init_se_status(self, msg: str) -> None:
        self._ui(lambda: self._handle_init_se_status_ui(msg))

    def _on_init_se_message(self, msg: dict) -> None:
        self._ui(lambda: self._handle_init_se_message_ui(msg))

    def _on_init_failed(self, step_key: str, reason: str, hint: str = "", release_occupation: bool = True) -> None:
        self._se_generation += 1
        if release_occupation:
            self._release_se_occupation()
        self._update_init_step(step_key, "\u5931\u8d25", theme.ACCENT_RED)
        msg = localize_user_message(reason)
        if hint:
            msg = f"{msg}\n{localize_user_message(hint)}"
        if self._main_ui_built:
            self._log_user_error_once(msg)
        else:
            self._set_init_hint(msg)
            self._set_init_actions_visible(True)
        if self._se_client:
            try:
                self._se_client.stop(wait=False)
            except Exception:
                pass
            self._se_client = None
        if self.session:
            self.session.bind_se_client(None)
        self._se_connected = False

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
        sid = self._se_server_id
        if not sid:
            return False
        username = self._login_username
        requested_connection_id = (connection_id or self._se_connection_id or "").strip()
        if not requested_connection_id:
            return False

        def do_with_retry() -> bool:
            for attempt in range(1, max_retries + 1):
                try:
                    code, resp = self.http.post(f"/api/nodes/{sid}/occupy", {
                        "username": username,
                        "connection_id": requested_connection_id,
                    })
                    if code == 200 and (resp or {}).get("ok"):
                        self._se_connection_id = requested_connection_id
                        return True
                    err_msg = (resp or {}).get("error", "") or (resp or {}).get("message", "") or f"HTTP {code}"
                    lower_msg = err_msg.lower()
                    if "occupied" in lower_msg or "not found" in lower_msg or "unauthorized" in lower_msg or code in (401, 403):
                        return False
                except Exception:
                    pass
                if attempt < max_retries:
                    time.sleep(min(1.0 * (2 ** (attempt - 1)), 5))
            return False

        if sync:
            return do_with_retry()
        self._run_bg(do_with_retry)
        return False

    def _release_se_occupation(self, sync: bool = False, clear_server_id: bool = True) -> bool:
        sid = self._se_server_id
        if not sid:
            return True
        connection_id = self._se_connection_id
        generation = self._se_generation

        def do_release() -> bool:
            try:
                code, _resp = self.http.post(f"/api/nodes/{sid}/release", {
                    "connection_id": connection_id,
                })
                if code == 200:
                    if clear_server_id and generation == self._se_generation and connection_id == self._se_connection_id:
                        self._se_server_id = ""
                        self._se_connection_id = ""
                    return True
            except Exception:
                pass
            return False

        if sync:
            return do_release()
        self._run_bg(do_release)
        return False

    def _enter_main_interface(self, generation: int | None = None) -> None:
        if generation is not None and generation != self._se_generation:
            return
        if self._init_ready:
            return
        self._init_ready = True
        if self._se_client:
            self._se_client.on_message = self._wrap_se_message_handler(self._se_generation)
            self._se_client.on_status = self._wrap_se_status_handler(self._se_generation)
            if self.session:
                self.session.bind_se_client(self._se_client)
        self._build_root()
        self._main_ui_built = True
        self._set_se_connection_ui(self._se_connected)
        if self._ui_backdoor_mode:
            self._append_log("DEV UI 后门已启用", "warn")
            self._append_log("交易服务器未连接，仅用于界面操作", "inf")
        else:
            self._append_log("SM\u767b\u5f55\u6210\u529f", "ok")
            self._append_log("\u4ea4\u6613\u670d\u52a1\u5668\u5df2\u8fde\u63a5", "ok")
        self._apply_broker_gate_ui()
        self._refresh_broker_gate_async(log_errors=False)
        self._sync_quote_subscriptions_async()

    def _se_disconnect(self) -> None:
        if self.session:
            try:
                self.session.broker_logout()
            except Exception:
                pass
            self.session.bind_se_client(None)
        if self._se_client:
            self._se_generation += 1
            self._se_client.stop(wait=False)
            self._se_client = None
        self._se_connection_id = ""
        with self._quote_sub_lock:
            self._quote_subscribed_symbols.clear()
        self._last_reconnect_notice_attempt = 0
        self._set_se_connection_ui(False)
        self._append_log("\u4ea4\u6613\u670d\u52a1\u5668\u5df2\u65ad\u5f00", "warn")

    def _on_se_status(self, msg: str) -> None:
        self._ui(lambda: self._handle_se_status_ui(msg))

    def _on_se_message(self, msg: dict) -> None:
        self._ui(lambda: self._handle_se_message_ui(msg))

    def _handle_quote_payload(self, payload: dict) -> None:
        sym = str(payload.get("symbol", "")).strip().upper()
        if not sym:
            return
        try:
            bid = float(payload.get("bid", 0) or 0)
            ask = float(payload.get("ask", 0) or 0)
            last = float(payload.get("last", 0) or 0)
            if last <= 0 and bid > 0 and ask > 0:
                last = round((bid + ask) / 2, 2)
            quote = {"symbol": sym, "bid": bid, "ask": ask, "last": last, "volume": int(float(payload.get("volume", 0) or 0))}
        except Exception:
            return
        self.current_quote[sym] = quote
        for slot in self.slots.values():
            if slot.symbol_text() == sym:
                slot.update_quote(quote)
    def _broker_login(self) -> None:
        if not self.session or not self.session.connected or not self._se_connected:
            self._log_user_error_once("\u8bf7\u5148\u8fde\u63a5\u4ea4\u6613\u670d\u52a1\u5668", "warn")
            return
        username = self.broker_user_entry.text().strip()
        password = self.broker_pass_entry.text()
        if not username or not password:
            self._log_user_error_once("\u8bf7\u8f93\u5165\u4ea4\u6613\u670d\u52a1\u8d26\u53f7\u548c\u5bc6\u7801", "warn")
            return
        self.broker_login_btn.setEnabled(False)
        self.broker_login_btn.setText("登录中...")
        self._run_bg(lambda: self._broker_login_bg(username, password))

    def _broker_login_bg(self, username: str, password: str) -> None:
        ok, msg, payload = self.session.broker_login(username, password) if self.session else (False, "\u672a\u8fde\u63a5", {})
        self._ui(lambda: self._handle_broker_login_result(ok, msg, payload, username, password))

    def _handle_broker_login_result(self, ok: bool, msg: str, payload: dict | None = None, username: str = "", password: str = "") -> None:
        self.broker_login_btn.setEnabled(True)
        self.broker_login_btn.setText("登录")
        self._apply_broker_gate_ui()
        if ok:
            self._append_log("\u4ea4\u6613\u670d\u52a1\u767b\u5f55\u6210\u529f", "ok")
            self._refresh_positions()
            self._refresh_orders()
        elif isinstance(payload, dict) and payload.get("code") == "BROKER_DEVICE_CHALLENGE_REQUIRED":
            self._prompt_broker_otp(username, password, str(payload.get("challenge_token") or ""))
        else:
            self._log_user_error_once(f"\u4ea4\u6613\u670d\u52a1\u767b\u5f55\u5931\u8d25\uff1a{localize_user_message(msg)}")

    def _prompt_broker_otp(self, username: str, password: str, challenge_token: str) -> None:
        if not challenge_token:
            self._log_user_error_once("券商设备验证失败：缺少 challenge token")
            return
        dialog = BrokerOtpDialog(self)
        accepted = dialog.exec() == QDialog.Accepted
        otp = dialog.otp()
        otp = (otp or "").strip()
        if not accepted or not otp:
            self._log_user_error_once("券商登录已取消", "warn")
            return
        self.broker_login_btn.setEnabled(False)
        self.broker_login_btn.setText("验证中...")
        self._run_bg(lambda: self._broker_login_otp_bg(username, password, challenge_token, otp))

    def _broker_login_otp_bg(self, username: str, password: str, challenge_token: str, otp: str) -> None:
        ok, msg, payload = self.session.broker_login(username, password, challenge_token=challenge_token, otp=otp) if self.session else (False, "\u672a\u8fde\u63a5", {})
        self._ui(lambda: self._handle_broker_login_result(ok, msg, payload, username, password))

    def _refresh_broker_gate_async(self, log_errors: bool = False) -> None:
        if not self.session or not self.session.connected or not self._se_connected:
            self._apply_broker_gate_ui()
            return
        self._run_bg(lambda: self._refresh_broker_gate_bg(log_errors))

    def _refresh_broker_gate_bg(self, log_errors: bool) -> None:
        ok, _gate, msg = self.session.broker_status_query() if self.session else (False, {}, "\u672a\u8fde\u63a5")
        self._ui(lambda: (self._apply_broker_gate_ui(), self._log_user_error_once(msg, "warn") if (not ok and log_errors and msg) else None))

    def _on_symbol_enter(self, pid: int) -> None:
        slot = self.slots[pid]
        sym = slot.symbol_text()
        if not sym:
            return
        slot.current_symbol = sym
        if sym in self.current_quote:
            slot.update_quote(self.current_quote[sym])
        self._sync_quote_subscriptions_async()

    def _schedule_quote_sync(self, pid: int) -> None:
        QTimer.singleShot(250, lambda: self._on_symbol_enter(pid))

    def _sync_quote_subscriptions_async(self) -> None:
        if not self.session or not self._se_connected:
            return
        self._run_bg(self._sync_quote_subscriptions_bg)

    def _sync_quote_subscriptions_bg(self) -> None:
        symbols = {slot.symbol_text() for slot in self.slots.values() if slot.symbol_text()}
        with self._quote_sub_lock:
            current = set(self._quote_subscribed_symbols)
            to_unsub = sorted(current - symbols)
            to_sub = sorted(symbols - current)
            if to_unsub and self.session:
                ok, msg = self.session.unsubscribe_quotes(to_unsub, timeout=6.0)
                if ok:
                    self._quote_subscribed_symbols.difference_update(to_unsub)
                else:
                    self._ui(lambda m=msg: self._log_user_error_once(f"\u884c\u60c5\u53d6\u6d88\u8ba2\u9605\u5931\u8d25\uff1a{localize_user_message(m)}", "warn"))
            if to_sub and self.session:
                ok, msg = self.session.subscribe_quotes(to_sub, timeout=6.0)
                if ok:
                    self._quote_subscribed_symbols.update(to_sub)
                else:
                    self._ui(lambda m=msg: self._log_user_error_once(f"\u884c\u60c5\u8ba2\u9605\u5931\u8d25\uff1a{localize_user_message(m)}", "warn"))

    def _on_order_type_change(self, pid: int) -> None:
        slot = self.slots[pid]
        is_market = slot.order_type.currentText() == "Market" if slot.order_type else False
        if slot.price:
            slot.price.setEnabled(not is_market)
            if is_market:
                slot.price.setText("Market")
            elif slot.price.text() == "Market":
                slot.price.setText("")

    def _adj_qty(self, delta: int, pid: int) -> None:
        slot = self.slots[pid]
        slot.set_qty(slot.qty_value() + delta)

    def _place_order(self, action: str, pid: int) -> None:
        if not self.session or not self._trade_controls_enabled():
            self._log_user_error_once("\u8bf7\u5148\u767b\u5f55\u4ea4\u6613\u670d\u52a1", "warn")
            return
        slot = self.slots[pid]
        sym = slot.symbol_text()
        qty = slot.qty_value()
        order_type = "market" if slot.order_type and slot.order_type.currentText() == "Market" else "limit"
        price = slot.price_value()
        tif = slot.tif.currentText() if slot.tif else "Day"
        if not sym:
            self._log_user_error_once("\u4e0b\u5355\u5931\u8d25\uff1a\u8bf7\u8f93\u5165\u4ee3\u7801")
            return
        if qty <= 0:
            self._log_user_error_once("\u4e0b\u5355\u5931\u8d25\uff1a\u6570\u91cf\u5fc5\u987b\u5927\u4e8e 0")
            return
        if order_type != "market" and price <= 0:
            self._log_user_error_once("\u4e0b\u5355\u5931\u8d25\uff1a\u9650\u4ef7\u5fc5\u987b\u5927\u4e8e 0")
            return
        price_str = "Market" if order_type == "market" else f"${price:.2f}"
        action_label = ACTION_LABELS.get(action, action)
        tif_label = TIF_LABELS.get(tif, tif)
        self._append_log(f"{action_label} {qty} \u80a1 {sym} @ {price_str} | {tif_label}", "inf")
        self._run_bg(lambda: self._submit_order_bg(sym, qty, price, action, order_type, tif))

    def _submit_order_bg(self, symbol: str, qty: int, price: float, action: str, order_type: str, tif: str) -> None:
        ok, msg = self.session.place_order(symbol, qty, price, action, order_type, tif=tif) if self.session else (False, "\u672a\u8fde\u63a5")
        self._ui(lambda: self._handle_order_result(ok, msg))

    def _handle_order_result(self, ok: bool, msg: str) -> None:
        if ok:
            self._append_log(msg, "ok")
            self._refresh_orders()
            QTimer.singleShot(1200, self._refresh_positions)
        else:
            self._log_user_error_once(msg)

    def _switch_order_mode(self, mode: str) -> None:
        self._order_mode = mode
        self._refresh_orders()

    def _refresh_orders(self) -> None:
        if not self.session:
            return
        self._run_bg(self._refresh_orders_bg)

    def _refresh_orders_bg(self) -> None:
        orders = self.session.get_orders(self._order_mode) if self.session else []
        self._ui(lambda: self._update_orders(orders))

    def _update_orders(self, orders: list[dict]) -> None:
        self._orders_raw = orders
        rows = []
        for order in orders:
            rows.append([
                order.get("symbol", ""),
                order.get("action", ""),
                order.get("price", ""),
                order.get("qty", ""),
                order.get("otype", ""),
                order.get("tif", "Day"),
                order.get("status", ""),
            ])
        self.orders_model.set_rows(rows)
        self.order_count_label.setText(f"{len(rows)} \u7b14\u8ba2\u5355" if rows else "\u6682\u65e0\u8ba2\u5355")

    def _selected_order_id(self) -> str:
        indexes = self.orders_table.selectionModel().selectedRows() if self.orders_table.selectionModel() else []
        if not indexes:
            return ""
        row = indexes[0].row()
        if 0 <= row < len(self._orders_raw):
            return str(self._orders_raw[row].get("id", ""))
        return ""

    def _cancel_selected_order(self) -> None:
        if not self.session or not self._trade_controls_enabled():
            self._log_user_error_once("\u8bf7\u5148\u767b\u5f55\u4ea4\u6613\u670d\u52a1", "warn")
            return
        order_id = self._selected_order_id()
        if not order_id:
            self._log_user_error_once("\u8bf7\u9009\u62e9\u4e00\u7b14\u8981\u64a4\u9500\u7684\u8ba2\u5355", "warn")
            return
        self._run_bg(lambda: self._cancel_order_bg(order_id))

    def _cancel_order_bg(self, order_id: str) -> None:
        ok, msg = self.session.cancel_order(order_id) if self.session else (False, "\u672a\u8fde\u63a5")
        self._ui(lambda: self._handle_cancel_result(ok, msg))

    def _handle_cancel_result(self, ok: bool, msg: str) -> None:
        if ok:
            self._append_log(msg, "ok")
            self._refresh_orders()
        else:
            self._log_user_error_once(msg)

    def _refresh_positions(self) -> None:
        if not self.session:
            return
        self._run_bg(self._refresh_positions_bg)

    def _refresh_positions_bg(self) -> None:
        positions = self.session.get_today_activity() if self.session else []
        err = getattr(self.session, "_pos_error", "") if self.session else ""
        self._ui(lambda: self._update_positions(positions, err))

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
            close_px = float(self.current_quote.get(sym, {}).get("last", position.get("close_px", 0)) or 0)
            realized = float(position.get("realized_today", 0) or 0)
            direction = position.get("direction", "")
            if qty and avg and close_px:
                unrealized = round((close_px - avg) * qty * (1 if direction == "Long" else -1), 2)
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

    def _on_position_clicked(self, index: QModelIndex) -> None:
        row = index.row()
        if 0 <= row < len(self._positions_raw):
            sym = str(self._positions_raw[row].get("symbol", "")).strip().upper()
            if sym and 1 in self.slots:
                slot = self.slots[1]
                if slot.symbol and slot.symbol.findText(sym) < 0:
                    slot.symbol.addItem(sym)
                if slot.symbol:
                    slot.symbol.setCurrentText(sym)
                self._on_symbol_enter(1)

    def _on_server_disconnect(self) -> None:
        if self.session:
            self.session.connected = False
        self._set_se_connection_ui(False)
        self._log_user_error_once("Server disconnected")

    def closeEvent(self, event) -> None:
        try:
            if self.session:
                try:
                    self.session.broker_logout()
                except Exception:
                    pass
            self._release_se_occupation(sync=True)
            if self.session:
                try:
                    self.session.logout()
                except Exception:
                    pass
            if self._se_client:
                self._se_client.stop()
        finally:
            event.accept()


class DuplicateLoginDialog(QDialog):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("登录接管")
        self.setModal(True)
        self.setMinimumWidth(420)
        self.setStyleSheet(theme.APP_QSS)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(18)
        title = make_label("账号已在其他位置登录", color=theme.TEXT_PRIMARY, font=theme.ui_font(15, bold=True))
        message = make_label("是否使旧登录失效，并在当前 Client 继续登录？", color=theme.TEXT_DIM, font=theme.ui_font(11))
        message.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(message)

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Ok)
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.button(QDialogButtonBox.Ok).setText("确认接管")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class ManagerLoginDialog(QDialog):
    def __init__(self, parent: QWidget | None = None, *, startup: bool = False):
        super().__init__(parent)
        self.setWindowTitle("SM??")
        self.setModal(True)
        self.setMinimumWidth(420)
        self.setStyleSheet(theme.APP_QSS)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(14)

        title = make_label("SC  登录", color=theme.ACCENT_BLUE, font=theme.mono_font(24, bold=True))
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
        ok_button = buttons.button(QDialogButtonBox.Ok)
        cancel_button = buttons.button(QDialogButtonBox.Cancel)
        if ok_button:
            ok_button.setText("??")
            ok_button.setStyleSheet(f"background: {theme.ACCENT_BLUE}; color: #07121B; border: 1px solid {theme.ACCENT_BLUE}; border-radius: 8px; padding: 7px 16px; font-weight: 700;")
        if cancel_button:
            cancel_button.setText("??" if startup else "??")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._password.returnPressed.connect(self.accept)
        self._username.setFocus()

    def credentials(self) -> tuple[str, str]:
        return self._username.text().strip(), self._password.text()


class BrokerOtpDialog(QDialog):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("券商验证")
        self.setModal(True)
        self.setMinimumWidth(360)
        self.setMaximumWidth(420)
        self.setStyleSheet(
            theme.APP_QSS
            + f"""
            QDialog {{
                background: {theme.TERM_BG};
                color: {theme.TEXT_PRIMARY};
                border: none;
            }}
            QLineEdit {{
                background: {theme.INPUT_BG};
                color: {theme.TEXT_PRIMARY};
                border: 1px solid {theme.BORDER};
                border-radius: 8px;
                padding: 9px 12px;
                selection-background-color: {theme.ACCENT_BLUE};
                selection-color: #07121B;
            }}
            QPushButton {{
                background: {theme.PANEL_ALT_BG};
                color: {theme.TEXT_PRIMARY};
                border: 1px solid {theme.BORDER};
                border-radius: 8px;
                padding: 8px 16px;
                min-height: 30px;
            }}
            QPushButton#loginButton {{
                background: {theme.PANEL_ALT_BG};
                color: {theme.ACCENT_BLUE};
                border: 1px solid {theme.ACCENT_BLUE};
                font-weight: 700;
            }}
            """
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        panel = QFrame()
        panel.setStyleSheet(f"background: {theme.TERM_BG}; border: none;")
        layout.addWidget(panel)

        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(24, 22, 24, 22)
        panel_layout.setSpacing(12)

        hint = make_label("请输入验证码", color=theme.TEXT_DIM, font=theme.ui_font(12, bold=True))
        hint.setAlignment(Qt.AlignCenter)
        panel_layout.addWidget(hint)

        self._otp = make_input("", placeholder="验证码")
        self._otp.setAlignment(Qt.AlignCenter)
        self._otp.setFixedWidth(210)
        self._otp.setFont(theme.mono_font(14, bold=True))
        self._otp.returnPressed.connect(self.accept)
        panel_layout.addWidget(self._otp, alignment=Qt.AlignCenter)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 10, 0, 0)
        buttons.setSpacing(0)
        cancel = make_button("取消", min_width=128)
        ok = make_button("确认", object_name="loginButton", min_width=128)
        cancel.clicked.connect(self.reject)
        ok.clicked.connect(self.accept)
        buttons.addWidget(cancel, alignment=Qt.AlignCenter)
        buttons.addWidget(ok, alignment=Qt.AlignCenter)
        panel_layout.addLayout(buttons)
        self._otp.setFocus()

    def otp(self) -> str:
        return self._otp.text().strip()


def run() -> int:
    app = QApplication(sys.argv)
    window = TradingTerminalQt()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(run())

