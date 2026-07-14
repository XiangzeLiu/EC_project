"""PySide6 desktop UI for the Trader_Server control panel."""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

if __package__:
    from . import theme
else:
    project_root = Path(__file__).resolve().parents[2]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    from Trader_Server.ui_qt import theme

from Trader_Server.config import DEFAULT_MANAGER_URL, DEFAULT_NODE_NAME, DEFAULT_REGION, state
from Trader_Server.ui_qt.api_client import TSApiClient


STATUS_LABELS = {
    "uninitialized": "未初始化",
    "registering": "注册中",
    "approved": "已批准",
    "running": "运行中",
    "online": "在线",
    "error": "错误",
    "rejected": "已拒绝",
}

STATUS_COLORS = {
    "uninitialized": theme.TEXT_LOW,
    "registering": theme.ACCENT_YELLOW,
    "approved": theme.ACCENT_GREEN,
    "running": theme.ACCENT_GREEN,
    "online": theme.ACCENT_GREEN,
    "error": theme.ACCENT_RED,
    "rejected": theme.ACCENT_RED,
}

LOG_ROW_COLORS = {
    "recv": theme.ACCENT_BLUE,
    "send": theme.ACCENT_GREEN,
    "conn": theme.TEXT_DIM,
    "err": theme.ACCENT_RED,
}


class UiBridge(QObject):
    call = Signal(object)


def make_label(
    text: str,
    *,
    color: str | None = None,
    font=None,
    object_name: str | None = None,
    alignment: Qt.AlignmentFlag | None = None,
) -> QLabel:
    label = QLabel(text)
    if color:
        label.setStyleSheet(f"color: {color};")
    if font:
        label.setFont(font)
    if object_name:
        label.setObjectName(object_name)
    if alignment is not None:
        label.setAlignment(alignment)
    return label


def make_button(
    text: str,
    *,
    object_name: str | None = None,
    min_width: int | None = None,
) -> QPushButton:
    button = QPushButton(text)
    button.setCursor(Qt.PointingHandCursor)
    button.setMinimumHeight(36)
    if min_width:
        button.setMinimumWidth(min_width)
    if object_name:
        button.setObjectName(object_name)
    return button


def make_input(
    text: str = "",
    *,
    placeholder: str = "",
    readonly: bool = False,
) -> QLineEdit:
    field = QLineEdit()
    field.setText(text)
    field.setPlaceholderText(placeholder)
    field.setMinimumHeight(42)
    field.setReadOnly(readonly)
    return field


def make_status_pill(text: str, *, active: bool = False, danger: bool = False) -> QLabel:
    label = QLabel(text)
    style_status_pill(label, text, active=active, danger=danger)
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
    label.setMinimumWidth(88)
    label.setStyleSheet(
        f"background: {bg}; color: {fg}; border: 1px solid {border}; "
        "border-radius: 8px; padding: 4px 10px;"
    )
    label.setFont(theme.mono_font(9, bold=True))


def build_dialog_stylesheet(dialog_name: str) -> str:
    return f"""
        QDialog#{dialog_name} {{
            background: {theme.HEADER_BG};
            border: 1px solid {theme.BORDER};
            border-radius: 16px;
        }}
        QLabel {{
            color: {theme.TEXT_PRIMARY};
            font-family: "{theme.FONT_UI}";
            font-size: 10pt;
        }}
        QFrame#dialogHero {{
            background: {theme.CARD_SOFT_BG};
            border: 1px solid {theme.BORDER_SOFT};
            border-radius: 12px;
        }}
        QFrame#dialogStatus {{
            background: {theme.PANEL_ALT_BG};
            border: 1px solid {theme.BORDER};
            border-radius: 12px;
        }}
        QPushButton {{
            background: {theme.PANEL_ALT_BG};
            border: 1px solid {theme.PANEL_ALT_BG};
            border-radius: 8px;
            color: {theme.TEXT_DIM};
            padding: 7px 13px;
            min-height: 28px;
        }}
        QPushButton#primaryButton {{
            background: {theme.ACCENT_BLUE};
            color: #07121B;
            font-weight: 700;
        }}
        QPushButton#warnButton {{
            background: {theme.ACCENT_YELLOW};
            color: #161005;
            font-weight: 700;
        }}
        QPushButton#dangerButton {{
            background: {theme.ACCENT_RED};
            color: #FFFFFF;
            font-weight: 700;
        }}
        QPushButton:disabled {{
            background: {theme.PANEL_ALT_BG};
            border: 1px solid {theme.BORDER};
            color: {theme.TEXT_LOW};
        }}
        QPushButton#primaryButton:disabled, QPushButton#warnButton:disabled, QPushButton#dangerButton:disabled {{
            background: {theme.PANEL_ALT_BG};
            border: 1px solid {theme.BORDER};
            color: {theme.TEXT_LOW};
            font-weight: 600;
        }}
    """


class AppMessageDialog(QDialog):
    TONES = {
        "info": ("系统提示", theme.ACCENT_BLUE, "primaryButton"),
        "warn": ("请确认", theme.ACCENT_YELLOW, "warnButton"),
        "danger": ("注意", theme.ACCENT_RED, "dangerButton"),
    }

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        title: str,
        message: str,
        tone: str = "info",
        confirm_text: str = "确定",
        cancel_text: str | None = None,
    ) -> None:
        super().__init__(parent)
        badge_text, badge_color, confirm_object = self.TONES.get(tone, self.TONES["info"])

        self.setModal(True)
        self.setObjectName("messageDialog")
        self.setWindowTitle(title)
        self.setFixedWidth(500)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self.setWindowFlag(Qt.WindowCloseButtonHint, False)
        self.setStyleSheet(build_dialog_stylesheet("messageDialog"))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        hero = QFrame()
        hero.setObjectName("dialogHero")
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(18, 16, 18, 16)
        hero_layout.setSpacing(8)
        hero_layout.addWidget(make_label(badge_text, color=badge_color, font=theme.mono_font(9, bold=True)))

        title_label = make_label(title, font=theme.ui_font(13, bold=True))
        title_label.setWordWrap(True)
        hero_layout.addWidget(title_label)

        body_card = QFrame()
        body_card.setObjectName("dialogStatus")
        body_layout = QVBoxLayout(body_card)
        body_layout.setContentsMargins(16, 14, 16, 14)
        body_layout.setSpacing(10)
        body_layout.addWidget(
            make_label(
                "请确认操作" if cancel_text else "提示内容",
                color=theme.TEXT_DIM,
                font=theme.ui_font(9, bold=True),
            )
        )

        message_label = make_label(message, color=theme.TEXT_PRIMARY, font=theme.ui_font(11))
        message_label.setWordWrap(True)
        body_layout.addWidget(message_label)

        button_row = QHBoxLayout()
        button_row.setContentsMargins(0, 0, 0, 0)
        button_row.setSpacing(10)
        button_row.addStretch(1)

        self.cancel_button: QPushButton | None = None
        if cancel_text:
            self.cancel_button = make_button(cancel_text)
            self.cancel_button.setMinimumWidth(112)
            self.cancel_button.clicked.connect(self.reject)
            button_row.addWidget(self.cancel_button)

        self.confirm_button = make_button(confirm_text, object_name=confirm_object)
        self.confirm_button.setMinimumWidth(132)
        self.confirm_button.clicked.connect(self.accept)
        self.confirm_button.setDefault(True)
        button_row.addWidget(self.confirm_button)

        layout.addWidget(hero)
        layout.addWidget(body_card)
        layout.addLayout(button_row)


class WaitDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setModal(True)
        self.setObjectName("waitDialog")
        self.setWindowTitle("等待审批")
        self.setFixedWidth(500)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self.setWindowFlag(Qt.WindowCloseButtonHint, False)
        self.setStyleSheet(
            f"""
            QDialog#waitDialog {{
                background: {theme.HEADER_BG};
                border: 1px solid {theme.BORDER};
                border-radius: 16px;
            }}
            QLabel {{
                color: {theme.TEXT_PRIMARY};
                font-family: \"{theme.FONT_UI}\";
                font-size: 10pt;
            }}
            QFrame#dialogHero {{
                background: {theme.CARD_SOFT_BG};
                border: 1px solid {theme.BORDER_SOFT};
                border-radius: 12px;
            }}
            QFrame#dialogStatus {{
                background: {theme.PANEL_ALT_BG};
                border: 1px solid {theme.BORDER};
                border-radius: 12px;
            }}
            QPushButton {{
                background: {theme.PANEL_ALT_BG};
                border: 1px solid {theme.PANEL_ALT_BG};
                border-radius: 8px;
                color: {theme.TEXT_DIM};
                padding: 7px 13px;
                min-height: 28px;
            }}
            QPushButton#warnButton {{
                background: {theme.ACCENT_YELLOW};
                color: #161005;
                font-weight: 700;
            }}
            QPushButton#warnButton:disabled {{
                background: {theme.PANEL_ALT_BG};
                border: 1px solid {theme.BORDER};
                color: {theme.TEXT_LOW};
            }}
            QProgressBar#waitProgress {{
                background: {theme.PANEL_ALT_BG};
                border: 1px solid {theme.BORDER};
                border-radius: 8px;
                min-height: 14px;
                max-height: 14px;
            }}
            QProgressBar#waitProgress::chunk {{
                background: {theme.ACCENT_BLUE};
                border-radius: 7px;
            }}
            """
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        hero = QFrame()
        hero.setObjectName("dialogHero")
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(18, 16, 18, 16)
        hero_layout.setSpacing(8)
        hero_layout.addWidget(make_label("审批流程", color=theme.ACCENT_BLUE, font=theme.mono_font(9, bold=True)))

        self.title_label = make_label("注册申请已提交，正在等待管理员审核", font=theme.ui_font(13, bold=True))
        self.title_label.setWordWrap(True)
        hero_layout.addWidget(self.title_label)

        self.req_label = make_label("request_id: 提交中...", color=theme.TEXT_LOW, font=theme.mono_font(9))
        self.req_label.setWordWrap(True)
        hero_layout.addWidget(self.req_label)

        status_card = QFrame()
        status_card.setObjectName("dialogStatus")
        status_layout = QVBoxLayout(status_card)
        status_layout.setContentsMargins(16, 14, 16, 14)
        status_layout.setSpacing(10)
        status_layout.addWidget(make_label("审批进度", color=theme.TEXT_DIM, font=theme.ui_font(9, bold=True)))

        self.status_label = make_label("正在连接审批流...", color=theme.TEXT_PRIMARY, font=theme.ui_font(11))
        self.status_label.setWordWrap(True)
        status_layout.addWidget(self.status_label)

        self.progress = QProgressBar()
        self.progress.setObjectName("waitProgress")
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        status_layout.addWidget(self.progress)

        helper = make_label("审批完成前请保持 Trader Server 在线。", color=theme.TEXT_LOW, font=theme.ui_font(9))
        helper.setWordWrap(True)
        status_layout.addWidget(helper)

        self.cancel_button = make_button("取消本次申请", object_name="warnButton")
        self.cancel_button.setMinimumWidth(132)

        button_row = QHBoxLayout()
        button_row.setContentsMargins(0, 0, 0, 0)
        button_row.setSpacing(10)
        button_row.addStretch(1)
        button_row.addWidget(self.cancel_button)

        layout.addWidget(hero)
        layout.addWidget(status_card)
        layout.addLayout(button_row)

    def set_request(self, request_id: str) -> None:
        self.req_label.setText(f"request_id: {request_id}" if request_id else "request_id: 提交中...")
        self.cancel_button.setEnabled(bool(request_id))

    def set_status(self, text: str) -> None:
        self.status_label.setText(text)


class TraderServerWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()

        theme.load_fonts()
        self.setStyleSheet(theme.APP_QSS)
        self.setWindowTitle("Trader Server")
        self.resize(1560, 980)
        self.setMinimumSize(1280, 780)

        self.api = TSApiClient()
        self._signals = UiBridge()
        self._signals.call.connect(lambda fn: fn())

        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._refresh_status)
        self._uptime_timer = QTimer(self)
        self._uptime_timer.timeout.connect(self._tick_clock)
        self._uptime_started_at = time.time()

        self._current_logs: list[dict] = []
        self._registered = False
        self._register_in_progress = False
        self._sse_cancelled = False
        self._current_request_id = ""
        self._current_manager_url = ""
        self._abandoned_request_ids: dict[str, float] = {}
        self._wait_dialog: WaitDialog | None = None
        self._cancel_in_progress = False
        self._detected_saved_credentials_logged = False

        self.card_vars: dict[str, QLabel] = {}
        self.cred_info: dict[str, QLabel] = {}

        self._build_root()

        self._tick_clock()
        self._refresh_status()
        self._load_logs()
        self._poll_timer.start(8000)
        self._uptime_timer.start(1000)

    def _ui(self, fn) -> None:
        self._signals.call.emit(fn)

    def _run_bg(self, fn) -> None:
        threading.Thread(target=fn, daemon=True).start()

    def _show_message_dialog(
        self,
        title: str,
        message: str,
        *,
        tone: str = "info",
        confirm_text: str = "确定",
        cancel_text: str | None = None,
    ) -> bool:
        dialog = AppMessageDialog(
            self,
            title=title,
            message=message,
            tone=tone,
            confirm_text=confirm_text,
            cancel_text=cancel_text,
        )
        return dialog.exec() == QDialog.Accepted

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
        layout.setSpacing(14)

        logo = QLabel("Trader Server")
        logo.setAlignment(Qt.AlignCenter)
        logo.setMinimumWidth(126)
        logo.setMaximumHeight(32)
        logo.setStyleSheet(
            f"background: {theme.ACCENT_BLUE}; color: #07121B; "
            f"border: 1px solid {theme.ACCENT_BLUE}; border-radius: 8px; padding: 2px 10px;"
        )
        logo.setFont(theme.ui_font(10, bold=True))
        layout.addWidget(logo)

        layout.addStretch(1)

        self.latency_label = make_label("--ms", color=theme.TEXT_LOW, font=theme.mono_font(9))
        self.uptime_label = make_label("0h 0m 0s", color=theme.TEXT_LOW, font=theme.mono_font(9))
        self.clock_label = make_label("--:--:--", color=theme.TEXT_PRIMARY, font=theme.mono_font(10, bold=True))

        layout.addWidget(self.latency_label)
        layout.addWidget(self.uptime_label)
        layout.addWidget(self.clock_label)

        refresh_button = make_button("刷新", min_width=72)
        refresh_button.clicked.connect(self._refresh_all)
        layout.addWidget(refresh_button)

        return header

    def _build_workspace(self) -> QWidget:
        workspace = QWidget()
        layout = QHBoxLayout(workspace)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(16)

        left = self._build_side_column()
        left.setFixedWidth(420)
        layout.addWidget(left)

        right = self._build_main_column()
        layout.addWidget(right, 1)
        return workspace

    def _build_side_column(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        layout.addWidget(self._build_register_card())
        layout.addWidget(self._build_local_log_card(), 1)
        return panel

    def _build_register_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("sidePanel")

        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        layout.addWidget(make_label("注册设置", font=theme.ui_font(11, bold=True)))

        self.fm_mgr_url = make_input(DEFAULT_MANAGER_URL, placeholder="管理服务地址")
        self.fm_node_name = make_input(DEFAULT_NODE_NAME, placeholder="节点名称")

        self.fm_region = QComboBox()
        self.fm_region.addItems(["IB", "TT", "Test"])
        self.fm_region.setCurrentText(DEFAULT_REGION if DEFAULT_REGION in {"IB", "TT", "Test"} else "TT")
        self.fm_region.setMinimumHeight(42)

        self.fm_host = make_input(self._detect_host(), readonly=True)
        self.fm_host.setEnabled(False)

        for title, widget in (
            ("管理服务器地址 *", self.fm_mgr_url),
            ("节点名称 *", self.fm_node_name),
            ("券商类型 *", self.fm_region),
            ("主机地址", self.fm_host),
        ):
            layout.addWidget(make_label(title, object_name="caption"))
            layout.addWidget(widget)

        self.register_progress = QProgressBar()
        self.register_progress.setVisible(False)
        self.register_progress.setRange(0, 0)
        layout.addWidget(self.register_progress)

        button_row = QHBoxLayout()
        button_row.setContentsMargins(0, 0, 0, 0)
        button_row.setSpacing(10)

        self.register_button = make_button("提交注册", object_name="primaryButton")
        self.register_button.clicked.connect(self._do_register)
        button_row.addWidget(self.register_button, 1)

        self.reregister_button = make_button("重新注册", object_name="warnButton")
        self.reregister_button.clicked.connect(self._do_reregister)
        self.reregister_button.setEnabled(False)
        button_row.addWidget(self.reregister_button, 1)

        layout.addLayout(button_row)

        return card

    def _build_credentials_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("softPanel")

        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)

        layout.addWidget(make_label("凭证信息", font=theme.ui_font(10, bold=True)))
        layout.addWidget(
            make_label(
                "注册完成后，本地凭证和当前管理端地址会显示在这里。",
                color=theme.TEXT_DIM,
                font=theme.ui_font(9),
                object_name="sectionHint",
            )
        )

        info_grid = QGridLayout()
        info_grid.setContentsMargins(0, 0, 0, 0)
        info_grid.setHorizontalSpacing(10)
        info_grid.setVerticalSpacing(10)
        info_grid.setColumnStretch(0, 1)
        info_grid.setColumnStretch(1, 1)

        for index, label_text in enumerate(("服务端ID", "状态", "令牌", "管理服务器地址")):
            row, value = self._make_overview_info_card(label_text)
            self.cred_info[label_text] = value
            info_grid.addWidget(row, index // 2, index % 2)

        layout.addLayout(info_grid)

        return card

    def _build_local_log_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("sidePanel")

        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        head = QHBoxLayout()
        head.addWidget(make_label("注册日志", font=theme.ui_font(11, bold=True)))
        head.addStretch(1)
        clear_button = make_button("清空")
        clear_button.clicked.connect(self._clear_local_log)
        head.addWidget(clear_button)
        layout.addLayout(head)

        self.local_log = QPlainTextEdit()
        self.local_log.setReadOnly(True)
        self.local_log.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(self.local_log, 1)
        return card

    def _build_main_column(self) -> QWidget:
        card = QFrame()
        card.setObjectName("dataPanel")

        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        self.main_tabs = QTabWidget()
        self.main_tabs.setDocumentMode(True)
        self.main_tabs.tabBar().setDrawBase(False)
        self.main_tabs.currentChanged.connect(self._on_main_tab_changed)

        overview_page = QWidget()
        self._build_overview_page(overview_page)
        logs_page = QWidget()
        self._build_logs_page(logs_page)

        self.main_tabs.addTab(overview_page, "运行概览")
        self.main_tabs.addTab(logs_page, "运行日志")
        layout.addWidget(self.main_tabs, 1)

        return card

    def _build_overview_page(self, parent: QWidget) -> None:
        layout = QVBoxLayout(parent)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(14)

        status_band = QFrame()
        status_band.setObjectName("overviewBand")
        status_row = QHBoxLayout(status_band)
        status_row.setContentsMargins(16, 14, 16, 14)
        status_row.setSpacing(14)

        status_text = QVBoxLayout()
        status_text.setContentsMargins(0, 0, 0, 0)
        status_text.setSpacing(4)
        status_text.addWidget(make_label("运行状态", font=theme.ui_font(10, bold=True)))
        status_text.addWidget(
            make_label(
                "用于确认当前节点注册、管理连接与本地服务状态。",
                color=theme.TEXT_DIM,
                font=theme.ui_font(9),
                object_name="sectionHint",
            )
        )
        status_row.addLayout(status_text, 1)

        self.overview_status_pill = make_status_pill("未初始化")
        status_row.addWidget(self.overview_status_pill, 0, Qt.AlignVCenter)
        layout.addWidget(status_band)

        metric_grid = QGridLayout()
        metric_grid.setContentsMargins(0, 0, 0, 0)
        metric_grid.setHorizontalSpacing(12)
        metric_grid.setVerticalSpacing(12)

        metric_data = [
            ("node_name", "节点名称", "-"),
            ("region", "券商类型", "-"),
            ("server_id", "服务端ID", "-"),
            ("heartbeat", "心跳状态", "--"),
            ("connections", "Client连接", "0"),
            ("broker", "券商状态", "-"),
        ]

        for index, (key, title, default) in enumerate(metric_data):
            metric_grid.addWidget(self._make_metric_card(title, default, key), index // 3, index % 3)

        layout.addLayout(metric_grid)

        detail_grid = QGridLayout()
        detail_grid.setContentsMargins(0, 0, 0, 0)
        detail_grid.setHorizontalSpacing(12)
        detail_grid.setVerticalSpacing(12)
        detail_grid.setColumnStretch(0, 3)
        detail_grid.setColumnStretch(1, 2)
        detail_grid.addWidget(self._build_credentials_card(), 0, 0)
        detail_grid.addWidget(self._build_heartbeat_card(), 0, 1)

        layout.addLayout(detail_grid)
        layout.addStretch(1)

    def _build_logs_page(self, parent: QWidget) -> None:
        layout = QVBoxLayout(parent)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(12)

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(10)

        self.log_stats = make_label("- 条记录", color=theme.TEXT_LOW, font=theme.mono_font(9))
        toolbar.addWidget(self.log_stats)
        toolbar.addStretch(1)

        clear_button = make_button("清空", object_name="warnButton")
        clear_button.clicked.connect(self._clear_logs)
        refresh_button = make_button("刷新")
        refresh_button.clicked.connect(self._load_logs)

        toolbar.addWidget(clear_button)
        toolbar.addWidget(refresh_button)
        layout.addLayout(toolbar)

        self.log_table = QTableWidget(0, 5)
        self.log_table.setHorizontalHeaderLabels(["时间", "类型", "Session", "Trace", "摘要"])
        self.log_table.verticalHeader().setVisible(False)
        self.log_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.log_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.log_table.setAlternatingRowColors(False)
        self.log_table.setShowGrid(False)
        self.log_table.setWordWrap(False)
        self.log_table.itemSelectionChanged.connect(self._on_log_select)

        header = self.log_table.horizontalHeader()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Interactive)
        header.setSectionResizeMode(3, QHeaderView.Interactive)
        header.setSectionResizeMode(4, QHeaderView.Stretch)
        self.log_table.setColumnWidth(2, 180)
        self.log_table.setColumnWidth(3, 150)

        layout.addWidget(self.log_table, 1)

        detail = QFrame()
        detail.setObjectName("softPanel")
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(14, 12, 14, 12)
        detail_layout.setSpacing(8)
        detail_layout.addWidget(make_label("消息详情", color=theme.TEXT_DIM, font=theme.ui_font(10, bold=True)))

        self.log_detail = QPlainTextEdit()
        self.log_detail.setReadOnly(True)
        self.log_detail.setMaximumHeight(190)
        detail_layout.addWidget(self.log_detail)

        layout.addWidget(detail)

    def _make_metric_card(self, title: str, default: str, key: str) -> QFrame:
        frame = QFrame()
        frame.setObjectName("metricCard")

        layout = QVBoxLayout(frame)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(4)
        layout.addWidget(make_label(title.upper(), object_name="metricTitle", font=theme.mono_font(9)))

        value = make_label(default, object_name="metricValue", font=theme.ui_font(16, bold=True))
        value.setWordWrap(True)
        layout.addWidget(value)
        self.card_vars[key] = value
        return frame

    def _make_overview_info_card(self, title: str) -> tuple[QFrame, QLabel]:
        frame = QFrame()
        frame.setObjectName("overviewInfoRow")

        layout = QVBoxLayout(frame)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        title_label = make_label(title, color=theme.TEXT_LOW, font=theme.mono_font(8, bold=True), object_name="monoText")
        value = make_label("-", font=theme.mono_font(10, bold=True), object_name="infoValue")
        value.setWordWrap(True)

        layout.addWidget(title_label)
        layout.addWidget(value)
        return frame, value

    def _build_heartbeat_card(self) -> QFrame:
        heartbeat_card = QFrame()
        heartbeat_card.setObjectName("softPanel")

        hb_layout = QVBoxLayout(heartbeat_card)
        hb_layout.setContentsMargins(16, 14, 16, 14)
        hb_layout.setSpacing(12)
        hb_layout.addWidget(make_label("心跳信息", font=theme.ui_font(10, bold=True)))
        hb_layout.addWidget(
            make_label(
                "用于确认与管理服务之间的连接健康情况。",
                color=theme.TEXT_DIM,
                font=theme.ui_font(9),
                object_name="sectionHint",
            )
        )

        stat_grid = QGridLayout()
        stat_grid.setContentsMargins(0, 0, 0, 0)
        stat_grid.setHorizontalSpacing(10)
        stat_grid.setVerticalSpacing(10)

        hb_items = [
            ("hb_total", "总次数", "-", theme.TEXT_PRIMARY),
            ("hb_ok", "成功", "-", theme.ACCENT_GREEN),
            ("hb_fail", "失败", "-", theme.ACCENT_RED),
            ("hb_interval", "间隔", "20s", theme.ACCENT_BLUE),
        ]

        for index, (key, title, default, color) in enumerate(hb_items):
            stat_grid.addWidget(self._make_small_stat_card(title, default, key, color), index // 2, index % 2)

        hb_layout.addLayout(stat_grid)
        hb_layout.addStretch(1)
        return heartbeat_card

    def _make_small_stat_card(self, title: str, default: str, key: str, color: str) -> QFrame:
        frame = QFrame()
        frame.setObjectName("compactMetricCard")

        layout = QVBoxLayout(frame)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)
        layout.addWidget(make_label(title, color=theme.TEXT_LOW, font=theme.ui_font(9), object_name="sectionHint"))

        value = make_label(default, color=color, font=theme.ui_font(14, bold=True))
        value.setWordWrap(True)
        layout.addWidget(value)
        self.card_vars[key] = value
        return frame

    def _clear_local_log(self) -> None:
        self.local_log.setPlainText("")

    def _tick_clock(self) -> None:
        now = time.localtime()
        self.clock_label.setText(time.strftime("%Y-%m-%d %H:%M:%S", now))
        elapsed = int(time.time() - self._uptime_started_at)
        hours, rem = divmod(elapsed, 3600)
        minutes, seconds = divmod(rem, 60)
        self.uptime_label.setText(f"{hours}h {minutes}m {seconds}s")

    @staticmethod
    def _detect_host() -> str:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
            sock.close()
            return f"{ip}:8900"
        except Exception:
            return "127.0.0.1:8900"

    def _append_local_log(self, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.local_log.appendPlainText(f"[{stamp}]  {message}")
        cursor = self.local_log.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.local_log.setTextCursor(cursor)

    def _set_local_api_status(self, online: bool, latency_ms: str = "--") -> None:
        self.latency_label.setText(f"{latency_ms}ms" if online and latency_ms != "--" else "--ms")
        if not online and hasattr(self, "overview_status_pill"):
            style_status_pill(self.overview_status_pill, "本地离线", active=True, danger=True)

    def _refresh_all(self) -> None:
        self._refresh_status()
        if self.main_tabs.currentIndex() == 1:
            self._load_logs()

    def _on_main_tab_changed(self, index: int) -> None:
        if index == 1:
            self._load_logs()

    def _refresh_status(self) -> None:
        start = time.perf_counter()
        data = self.api.get_status()
        latency = int((time.perf_counter() - start) * 1000)

        if not data or not isinstance(data, dict):
            self._set_local_api_status(False)
            return

        self._set_local_api_status(True, str(latency))

        registration = data.get("registration", {})
        heartbeat = data.get("heartbeat", {})
        status_val = registration.get("status", "uninitialized")

        style_status_pill(
            self.overview_status_pill,
            STATUS_LABELS.get(status_val, status_val.upper()),
            active=status_val in {"approved", "running", "online", "registering", "rejected", "error"},
            danger=status_val in {"rejected", "error"},
        )

        self.card_vars["node_name"].setText(registration.get("node_name") or "-")
        self.card_vars["region"].setText(registration.get("region") or "-")
        self.card_vars["server_id"].setText(registration.get("server_id") or "-")
        self.card_vars["connections"].setText(str(data.get("connections", 0)))

        heartbeat_ok = bool(heartbeat.get("ok"))
        self.card_vars["heartbeat"].setText("OK" if heartbeat_ok else "--")
        self.card_vars["heartbeat"].setStyleSheet(
            f"color: {theme.ACCENT_GREEN if heartbeat_ok else theme.ACCENT_RED}; font-size: 15pt; font-weight: 700;"
        )

        broker_status = str(data.get("broker_status") or "-")
        broker_color = theme.ACCENT_GREEN if "connected" in broker_status.lower() else theme.ACCENT_YELLOW
        if broker_status in {"-", "--"}:
            broker_color = theme.TEXT_LOW
        self.card_vars["broker"].setText(broker_status)
        self.card_vars["broker"].setStyleSheet(f"color: {broker_color}; font-size: 15pt; font-weight: 700;")

        self.card_vars["hb_total"].setText(str(heartbeat.get("total", 0)))
        self.card_vars["hb_ok"].setText(str(heartbeat.get("ok_count", heartbeat.get("ok", 0))))
        self.card_vars["hb_fail"].setText(str(heartbeat.get("fail", 0)))
        self.card_vars["hb_interval"].setText(f"{heartbeat.get('interval', 20)}s")

        self._current_manager_url = registration.get("manager_url") or self.fm_mgr_url.text().strip()
        has_credentials = bool(registration.get("has_credentials"))
        server_id = registration.get("server_id") or ""
        was_registered = self._registered
        is_registered = bool(server_id and has_credentials and status_val in {"approved", "running", "online"})

        self.cred_info["服务端ID"].setText(server_id or "-")
        self.cred_info["令牌"].setText("(已保存)" if has_credentials else "-")
        self.cred_info["管理服务器地址"].setText(self._current_manager_url or "-")

        cred_status = STATUS_LABELS.get(status_val, status_val)
        cred_color = STATUS_COLORS.get(status_val, theme.TEXT_LOW)
        if is_registered and heartbeat_ok:
            cred_status = "在线（心跳正常）"
            cred_color = theme.ACCENT_GREEN
        elif is_registered and status_val == "approved" and not heartbeat_ok:
            cred_status = "已批准，等待心跳启动"
            cred_color = theme.ACCENT_YELLOW

        self.cred_info["状态"].setText(cred_status)
        self.cred_info["状态"].setStyleSheet(f"color: {cred_color};")

        if is_registered and not was_registered:
            self._registered = True
            self._lock_form(True)
            if self._register_in_progress:
                self._append_local_log("*** 注册成功并已锁定 ***")
            elif not self._detected_saved_credentials_logged:
                self._append_local_log("*** 检测到本地已保存凭证，表单已锁定（非本次新注册） ***")
                self._detected_saved_credentials_logged = True
        elif not is_registered and was_registered:
            self._registered = False
            self._lock_form(False)
            self._detected_saved_credentials_logged = False
        elif is_registered:
            self._registered = True
            self._lock_form(True)
        else:
            self._registered = False
            if not self._register_in_progress:
                self._lock_form(False)

    def _load_logs(self) -> None:
        data = self.api.get_logs(150)
        if not data or not isinstance(data, dict) or not data.get("ok"):
            return

        logs = data.get("logs", [])
        stats = data.get("stats", {})
        self._current_logs = list(logs)
        self.log_stats.setText(
            f"{stats.get('total', 0)} 条记录 | 连接:{stats.get('connections', 0)} 请求:{stats.get('requests', 0)} 响应:{stats.get('responses', 0)} 错误:{stats.get('errors', 0)}"
        )

        self.log_table.setRowCount(0)
        if not logs:
            self.log_table.setRowCount(1)
            for col, value in enumerate(["-", "-", "-", "-", "(暂无日志记录)"]):
                self.log_table.setItem(0, col, QTableWidgetItem(value))
            self.log_detail.setPlainText("")
            return

        for row, entry in enumerate(logs):
            self.log_table.insertRow(row)
            values = [
                str(entry.get("timestamp", "-")),
                str(entry.get("level", "info")),
                str(entry.get("session_id", "-")),
                str(entry.get("trace_id", "-")),
                str(entry.get("summary", "-")),
            ]
            color = LOG_ROW_COLORS.get(str(entry.get("level", "")), theme.TEXT_DIM)
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setForeground(QColor(color))
                self.log_table.setItem(row, col, item)

        if self.log_table.rowCount():
            self.log_table.selectRow(0)

    def _on_log_select(self) -> None:
        row = self.log_table.currentRow()
        if row < 0 or row >= len(self._current_logs):
            return

        entry = self._current_logs[row]
        payload = {
            "timestamp": entry.get("timestamp", "-"),
            "level": entry.get("level", "-"),
            "session_id": entry.get("session_id", "-"),
            "trace_id": entry.get("trace_id", "-"),
            "summary": entry.get("summary", "-"),
            "detail": entry.get("detail") or {},
        }
        self.log_detail.setPlainText(json.dumps(payload, ensure_ascii=False, indent=2))

    def _clear_logs(self) -> None:
        result = self.api.post("/api/logs/clear", {})
        if result and result.get("ok"):
            self._append_local_log("运行日志已清空")
            self._load_logs()

    def _lock_form(self, locked: bool) -> None:
        self.fm_mgr_url.setEnabled(not locked)
        self.fm_node_name.setEnabled(not locked)
        self.fm_region.setEnabled(not locked)

        if self._register_in_progress:
            self.register_button.setText("等待审批中...")
            self.register_button.setEnabled(False)
            self.reregister_button.setEnabled(False)
            return

        if locked:
            self.register_button.setText("已注册")
            self.register_button.setEnabled(False)
            self.reregister_button.setEnabled(True)
        else:
            self.register_button.setText("提交注册")
            self.register_button.setEnabled(True)
            self.reregister_button.setEnabled(False)

    def _do_register(self) -> None:
        if self._register_in_progress:
            return
        if self._registered:
            self._append_local_log("当前节点已完成注册，无需重复提交")
            return

        payload = {
            "manager_url": self.fm_mgr_url.text().strip(),
            "node_name": self.fm_node_name.text().strip(),
            "region": self.fm_region.currentText().strip(),
            "host": self.fm_host.text().strip(),
        }
        if not payload["manager_url"] or not payload["node_name"] or not payload["region"]:
            self._show_message_dialog(
                "参数不完整",
                "请完整填写注册参数后再提交。",
                tone="warn",
                confirm_text="我知道了",
            )
            return

        self._current_manager_url = payload["manager_url"]
        self._register_in_progress = True
        self._sse_cancelled = False
        self._current_request_id = ""
        self._cancel_in_progress = False

        self._lock_form(True)
        self.register_progress.setVisible(True)
        self._append_local_log("======== 注册开始 ========")
        self._append_local_log(f"[1/3] 参数: {json.dumps(payload, ensure_ascii=False)}")
        self._show_wait_dialog("", "正在提交注册申请，请稍候...")

        self._run_bg(lambda: self._register_thread(payload))

    def _register_thread(self, payload: dict) -> None:
        self._ui(lambda: self._append_local_log("[0/3] 检查本地子节点服务..."))
        local_result = self.api.ping_local()
        if local_result and local_result.get("ok"):
            self._ui(lambda: self._append_local_log("[0/3] 本地服务可用"))
        else:
            err_type = (local_result or {}).get("error_type", "") if isinstance(local_result, dict) else ""
            if err_type == "TS_LOCAL_UNREACHABLE":
                self._ui(lambda: self._append_local_log("[0/3] 本地服务不可达，切换直连 SM 注册"))
                self._register_thread_direct_fallback(payload)
                self._ui(self._finish_register_ui)
                return
            err = (local_result or {}).get("error", "本地服务响应异常")
            self._ui(lambda e=err: self._append_local_log(f"[0/3] 失败: {e}"))
            self._ui(self._finish_register_ui)
            return

        self._ui(lambda: self._append_local_log("[1/3] 测试管理端连通性..."))
        ping_result = self.api.ping_sm(payload["manager_url"])
        if not ping_result or not ping_result.get("ok"):
            err = (ping_result or {}).get("error", "管理端连接失败")
            self._ui(lambda e=err: self._append_local_log(f"[1/3] 失败: {e}"))
            self._ui(self._finish_register_ui)
            return

        latency = ping_result.get("latency", "?")
        self._ui(lambda l=latency: self._append_local_log(f"[1/3] 成功 ({l}ms)"))

        self._ui(lambda: self._append_local_log("[2/3] 提交注册请求..."))
        submit_result = self.api.submit_registration(payload)
        if not submit_result or not submit_result.get("ok"):
            err = (submit_result or {}).get("error", "注册提交失败")
            self._ui(lambda e=err: self._append_local_log(f"[2/3] 失败: {e}"))
            self._ui(self._finish_register_ui)
            return

        request_id = submit_result.get("request_id", "")
        self._current_request_id = request_id
        self._ui(lambda rid=request_id: self._append_local_log(f"[2/3] 提交成功 request_id={rid}"))
        self._ui(lambda rid=request_id: self._show_wait_dialog(rid, "注册申请已提交，正在等待管理员审核"))
        self._ui(lambda: self._append_local_log("[3/3] 等待审批 (SSE)..."))

        for event in self.api.sse_await_approval(request_id):
            if self._sse_cancelled:
                break
            self._ui(lambda e=event, rid=request_id: self._handle_sse_event(e, rid))
            approved_val = event.get("approved")
            if approved_val is True or approved_val is False or event.get("reason"):
                break

        self._ui(self._finish_register_ui)

    def _register_thread_direct_fallback(self, payload: dict) -> None:
        try:
            from Trader_Server.services.registration import await_approval, submit_registration, test_connection

            state.manager_url = payload.get("manager_url", "") or state.manager_url
            state.node_name = payload.get("node_name", "") or state.node_name
            state.region = payload.get("region", "") or state.region

            self._ui(lambda: self._append_local_log("[1/3] 测试管理端连通性..."))
            ok, message = test_connection()
            if not ok:
                self._ui(lambda msg=message: self._append_local_log(f"[1/3] 失败: {msg}"))
                return

            self._ui(lambda: self._append_local_log("[1/3] 成功 (fallback)"))
            self._ui(lambda: self._append_local_log("[2/3] 提交注册请求..."))
            result = submit_registration(
                node_name=payload.get("node_name"),
                region=payload.get("region"),
                host=payload.get("host"),
            )
            if not result:
                self._ui(lambda: self._append_local_log("[2/3] 失败: Registration submission failed"))
                return

            request_id = result.get("request_id", "")
            self._current_request_id = request_id
            self._ui(lambda rid=request_id: self._append_local_log(f"[2/3] 提交成功 request_id={rid}"))
            self._ui(lambda rid=request_id: self._show_wait_dialog(rid, "注册申请已提交，正在等待管理员审核"))
            self._ui(lambda: self._append_local_log("[3/3] 等待审批 (SSE)..."))

            event = await_approval(request_id=request_id, timeout=3600, shutdown_check=lambda: self._sse_cancelled)
            if self._sse_cancelled:
                return
            if not event:
                event = {"approved": False, "reason": "SSE等待失败或超时"}
            self._ui(lambda e=event, rid=request_id: self._handle_sse_event(e, rid))
        except Exception as exc:
            self._ui(lambda err=str(exc): self._append_local_log(f"[FALLBACK] 失败: {err}"))

    def _handle_sse_event(self, event: dict, request_id: str) -> None:
        if request_id and request_id in self._abandoned_request_ids:
            if event.get("approved"):
                self._append_local_log(f"[!] 收到已废弃申请的通过结果，正在丢弃 {request_id}")
                self._run_bg(lambda rid=request_id: self._discard_abandoned_approval(rid))
            else:
                self._abandoned_request_ids.pop(request_id, None)
            return

        approved = event.get("approved")
        if approved is True:
            server_id = event.get("server_id", "-")
            self._append_local_log(f"*** 已批准 server_id={server_id} ***")
            self.cred_info["服务端ID"].setText(server_id)
            self.cred_info["令牌"].setText("(已保存)")
            self.cred_info["管理服务器地址"].setText(self._current_manager_url or self.fm_mgr_url.text().strip() or "-")
            self.cred_info["状态"].setText("已批准，正在建立管理连接")
            self.cred_info["状态"].setStyleSheet(f"color: {theme.ACCENT_YELLOW};")
            self._registered = True
            self._close_wait_dialog()
            self._refresh_status()
            return

        reason = event.get("reason", "") or "管理员未通过审批"
        if self._wait_dialog:
            self._wait_dialog.set_status(reason)

        if reason.startswith("SSE") or "stream error" in reason.lower() or "连接中断" in reason:
            self._append_local_log(f"[!] 连接中断: {reason}")
        else:
            self._append_local_log(f"[X] 注册未通过: {reason}")

        self._registered = False

    def _show_wait_dialog(self, request_id: str, title: str) -> None:
        if self._wait_dialog is None:
            self._wait_dialog = WaitDialog(self)
            self._wait_dialog.cancel_button.clicked.connect(self._cancel_current_registration)

        self._wait_dialog.title_label.setText(title)
        self._wait_dialog.set_request(request_id)
        self._wait_dialog.set_status("正在连接审批流...")
        if not self._wait_dialog.isVisible():
            self._wait_dialog.show()
        self._wait_dialog.raise_()
        self._wait_dialog.activateWindow()

    def _close_wait_dialog(self) -> None:
        if self._wait_dialog and self._wait_dialog.isVisible():
            self._wait_dialog.close()

    def _remember_abandoned_request(self, request_id: str) -> None:
        rid = (request_id or "").strip()
        if not rid:
            return
        self._abandoned_request_ids[rid] = time.time()
        if len(self._abandoned_request_ids) > 200:
            for old_rid in list(self._abandoned_request_ids.keys())[:80]:
                self._abandoned_request_ids.pop(old_rid, None)

    def _purge_abandoned_requests(self, keep_seconds: int = 86400) -> None:
        now = time.time()
        for rid, stamp in list(self._abandoned_request_ids.items()):
            if now - stamp > keep_seconds:
                self._abandoned_request_ids.pop(rid, None)

    def _cancel_request_on_sm(self, request_id: str) -> dict:
        result = self.api.cancel_registration(request_id, self._current_manager_url)
        if result and isinstance(result, dict) and result.get("ok"):
            return result

        try:
            from Trader_Server.services.registration import cancel_registration_request

            if self._current_manager_url:
                state.manager_url = self._current_manager_url
            return cancel_registration_request(
                request_id=request_id,
                reason="node_cancelled_by_user",
                force_discard_approved=True,
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _discard_abandoned_approval(self, request_id: str) -> None:
        result = self._cancel_request_on_sm(request_id)
        if result.get("ok"):
            self._ui(lambda rid=request_id: self._append_local_log(f"[OK] 已丢弃废弃申请 {rid}"))
        else:
            err = result.get("error", "未知错误")
            self._ui(lambda e=err: self._append_local_log(f"[!] 丢弃废弃申请失败: {e}"))
        self._abandoned_request_ids.pop(request_id, None)

    def _cancel_current_registration_worker(self, request_id: str) -> None:
        result = self._cancel_request_on_sm(request_id)
        if result.get("ok"):
            self._remember_abandoned_request(request_id)
            self._sse_cancelled = True
            self._ui(lambda rid=request_id: self._append_local_log(f"[CANCEL] 已废弃申请: {rid}"))
            self._ui(self._close_wait_dialog)
            self._ui(self._finish_register_ui)
            return

        err = result.get("error", "未知错误")

        def restore() -> None:
            self._append_local_log(f"[CANCEL] 取消失败，继续等待审批: {err}")
            self._cancel_in_progress = False
            if self._wait_dialog:
                self._wait_dialog.cancel_button.setText("取消本次申请")
                self._wait_dialog.cancel_button.setEnabled(True)

        self._ui(restore)

    def _cancel_current_registration(self) -> None:
        request_id = (self._current_request_id or "").strip()
        if not request_id:
            self._close_wait_dialog()
            self._finish_register_ui()
            return

        if self._cancel_in_progress:
            return

        answer = self._show_message_dialog(
            "取消注册",
            "确定要取消当前注册申请吗？\n取消后将回到可重新注册状态。",
            tone="warn",
            confirm_text="确认取消",
            cancel_text="返回",
        )
        if not answer:
            return

        self._cancel_in_progress = True
        if self._wait_dialog:
            self._wait_dialog.cancel_button.setText("取消中...")
            self._wait_dialog.cancel_button.setEnabled(False)
        self._append_local_log(f"[CANCEL] 用户发起取消: {request_id}")
        self._run_bg(lambda rid=request_id: self._cancel_current_registration_worker(rid))

    def _finish_register_ui(self) -> None:
        self._register_in_progress = False
        self._cancel_in_progress = False
        self._close_wait_dialog()
        self.register_progress.setVisible(False)
        self._purge_abandoned_requests()
        self._lock_form(self._registered)
        if not self._registered:
            self.register_button.setText("提交注册")
            self.register_button.setEnabled(True)
        self._refresh_status()

    def _do_reregister(self) -> None:
        answer = self._show_message_dialog(
            "重新注册",
            "此操作将清除当前已保存的凭证。\n你需要重新提交注册并等待审批。\n\n是否继续？",
            tone="danger",
            confirm_text="确认重置",
            cancel_text="返回",
        )
        if not answer:
            return

        result = self.api.clear_credentials()
        if result and result.get("ok"):
            cleared = result.get("cleared", [])
            detail = ", ".join(cleared) if cleared else "无已保存文件"
            self._registered = False
            self._register_in_progress = False
            self._current_request_id = ""
            self._sse_cancelled = False
            self._detected_saved_credentials_logged = False
            self._close_wait_dialog()
            self.cred_info["服务端ID"].setText("-")
            self.cred_info["令牌"].setText("-")
            self.cred_info["管理服务器地址"].setText("-")
            self.cred_info["状态"].setText("-")
            self.local_log.setPlainText("")
            self._append_local_log(f"======== 凭证已清除（{detail}） ========")
            self._lock_form(False)
            self._refresh_status()
            self._show_message_dialog(
                "已清除",
                f"已删除凭证文件：{detail}\n请重新填写表单并提交注册。",
                tone="info",
                confirm_text="知道了",
            )
        else:
            err = result.get("error", "清除失败") if result else "无响应"
            self._append_local_log(f"[!] 清除凭证失败: {err}")
            self._show_message_dialog(
                "操作失败",
                err,
                tone="danger",
                confirm_text="知道了",
            )

    def closeEvent(self, event) -> None:
        self._poll_timer.stop()
        self._uptime_timer.stop()
        self._sse_cancelled = True
        self._close_wait_dialog()
        super().closeEvent(event)


def run() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    theme.load_fonts()
    window = TraderServerWindow()
    window.show()
    return app.exec()

