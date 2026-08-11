"""In-window settings overlay for the Client UI."""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .client_version import client_version
from .hotkey_config import (
    DEFAULT_QUANTITY_HOTKEY_IDS,
    DEFAULT_QUANTITY_KEY_BY_ID,
    DEFAULT_HOTKEY_CONFIG,
    FIXED_HOTKEY_DESCRIPTIONS,
    MAX_ORDER_HOTKEY_RULES,
    MAX_QUANTITY_HOTKEY_RULES,
    VALID_TIFS,
    HotkeyRuntimeConfig,
    OrderHotkeyRule,
    QuantityHotkey,
    bindings_from_config,
    format_hotkey_validation_errors,
    validate_hotkey_config,
)
from .shortcut_controller import validate_shortcut_sequences


ORDER_TYPES = ("limit", "market")
SIDES = ("buy", "sell")
SIDE_LABELS = {"buy": "BUY", "sell": "SELL"}
TYPE_LABELS = {"limit": "LMT", "market": "MKT"}
ORDER_TIFS = ("Day", "GTC", "IOC", "EXT", "GTC_EXT")


def set_combo_item_enabled(
    combo: QComboBox,
    value: str,
    enabled: bool,
    unavailable_tooltip: str = "",
) -> bool:
    target = str(value or "").strip().upper()
    model = combo.model()
    if not isinstance(model, QStandardItemModel):
        return False
    for index in range(combo.count()):
        if combo.itemText(index).strip().upper() != target:
            continue
        item = model.item(index)
        if item is None:
            return False
        item.setEnabled(bool(enabled))
        item.setToolTip("" if enabled else unavailable_tooltip)
        return True
    return False


class KeyCaptureEdit(QLineEdit):
    def __init__(self, text: str = "", parent: QWidget | None = None):
        super().__init__(text, parent)
        self.setPlaceholderText("点击后按键")
        self.setMinimumWidth(112)
        self.setAlignment(Qt.AlignCenter)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key_Backspace, Qt.Key_Delete):
            self.clear()
            event.accept()
            return
        if event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Tab, Qt.Key_Backtab):
            super().keyPressEvent(event)
            return
        sequence = QKeySequence(event.keyCombination()).toString(QKeySequence.PortableText)
        if sequence:
            self.setText(sequence)
            event.accept()
            return
        super().keyPressEvent(event)


class SettingsOverlay(QWidget):
    close_requested = Signal()
    save_requested = Signal(object)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        config: HotkeyRuntimeConfig = DEFAULT_HOTKEY_CONFIG,
        route_options: list[str] | tuple[str, ...] = ("SMART",),
        route_effective: bool = False,
        hidden_effective: bool = False,
        supported_tifs: set[str] | list[str] | tuple[str, ...] = ORDER_TIFS,
    ):
        super().__init__(parent)
        self.setObjectName("settingsOverlay")
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._config = config
        self._route_options = self._normalize_routes(route_options)
        self._route_effective = bool(route_effective)
        self._hidden_effective = bool(hidden_effective)
        self._supported_tifs = {
            str(value or "").strip().upper()
            for value in supported_tifs or ()
            if str(value or "").strip()
        }
        self._quantity_rows: list[dict[str, object]] = []
        self._order_rows: list[dict[str, object]] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.addStretch(1)

        center = QHBoxLayout()
        center.addStretch(1)
        self.panel = self._build_panel()
        center.addWidget(self.panel)
        center.addStretch(1)
        root.addLayout(center)
        root.addStretch(1)

        self._tab_buttons = (self.hotkey_tab_btn, self.about_tab_btn)
        self.select_tab(0)
        self.hotkey_tabs.setCurrentIndex(0)
        self._resize_panel()

    def _resize_panel(self) -> None:
        if not hasattr(self, "panel"):
            return
        width = max(900, min(1020, self.width() - 48))
        height = max(520, min(806, self.height() - 48))
        self.panel.setFixedSize(width, height)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._resize_panel()

    @staticmethod
    def _normalize_routes(routes: list[str] | tuple[str, ...]) -> list[str]:
        normalized = []
        for route in routes or ():
            value = str(route or "").strip().upper()
            if value and value not in normalized:
                normalized.append(value)
        if "SMART" not in normalized:
            normalized.insert(0, "SMART")
        return normalized

    def _build_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("settingsPanel")
        panel.setMinimumSize(900, 520)
        panel.setMaximumSize(1020, 806)
        panel.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QFrame()
        header.setObjectName("settingsHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(18, 12, 14, 12)
        title = QLabel("设置")
        title.setObjectName("settingsTitle")
        title.setFont(theme.ui_font(12, bold=True))
        self.close_btn = QPushButton("×")
        self.close_btn.setObjectName("settingsCloseButton")
        self.close_btn.setFixedSize(28, 28)
        self.close_btn.clicked.connect(self.close_requested.emit)
        header_layout.addWidget(title)
        header_layout.addStretch(1)
        header_layout.addWidget(self.close_btn)
        layout.addWidget(header)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName("settingsSidebar")
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(10, 12, 10, 12)
        sidebar_layout.setSpacing(8)
        self.hotkey_tab_btn = self._make_tab_button("快捷键设置", 0)
        self.about_tab_btn = self._make_tab_button("关于 client", 1)
        sidebar_layout.addWidget(self.hotkey_tab_btn)
        sidebar_layout.addWidget(self.about_tab_btn)
        sidebar_layout.addStretch(1)
        body.addWidget(sidebar)

        self.stack = QStackedWidget()
        self.stack.setObjectName("settingsStack")
        self.stack.addWidget(self._build_hotkey_page())
        self.stack.addWidget(self._build_about_page())
        body.addWidget(self.stack, 1)
        layout.addLayout(body, 1)
        return panel

    def _make_tab_button(self, text: str, index: int) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("settingsTabButton")
        button.setCheckable(True)
        button.clicked.connect(lambda _checked=False, tab=index: self.select_tab(tab))
        return button

    def _build_hotkey_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("settingsPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 18, 20, 14)
        layout.setSpacing(12)

        title = QLabel("快捷键设置")
        title.setObjectName("settingsPageTitle")
        title.setFont(theme.ui_font(14, bold=True))
        layout.addWidget(title)

        self.error_label = QLabel("")
        self.error_label.setObjectName("settingsErrorText")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        layout.addWidget(self.error_label)

        self.hotkey_tabs = QTabWidget()
        self.hotkey_tabs.setObjectName("hotkeyInnerTabs")
        self.hotkey_tabs.addTab(self._build_quantity_page(), "股数快捷键")
        self.hotkey_tabs.addTab(self._build_order_page(), "下单快捷键")
        self.hotkey_tabs.addTab(self._build_fixed_page(), "固定快捷键")
        layout.addWidget(self.hotkey_tabs, 1)

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 2, 0, 0)
        self.restore_btn = QPushButton("恢复默认")
        self.restore_btn.setObjectName("settingsSecondaryButton")
        self.restore_btn.clicked.connect(self._restore_defaults)
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setObjectName("settingsSecondaryButton")
        self.cancel_btn.clicked.connect(self.close_requested.emit)
        self.save_btn = QPushButton("保存并应用")
        self.save_btn.setObjectName("settingsPrimaryButton")
        self.save_btn.clicked.connect(self._emit_save)
        footer.addWidget(self.restore_btn)
        footer.addStretch(1)
        footer.addWidget(self.cancel_btn)
        footer.addWidget(self.save_btn)
        layout.addLayout(footer)
        return page

    def _build_quantity_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("settingsPage")
        outer = QVBoxLayout(page)
        outer.setContentsMargins(4, 8, 4, 10)
        outer.setSpacing(10)

        self.quantity_scroll = QScrollArea()
        self.quantity_scroll.setWidgetResizable(True)
        self.quantity_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.quantity_scroll.setObjectName("settingsScrollArea")
        self.quantity_rows_host = QWidget()
        self.quantity_rows_host.setObjectName("settingsOrderRows")
        self.quantity_rows_layout = QGridLayout(self.quantity_rows_host)
        self.quantity_rows_layout.setContentsMargins(0, 0, 0, 0)
        self.quantity_rows_layout.setHorizontalSpacing(16)
        self.quantity_rows_layout.setVerticalSpacing(8)
        self.quantity_scroll.setWidget(self.quantity_rows_host)
        outer.addWidget(self.quantity_scroll, 1)

        controls = QHBoxLayout()
        self.add_quantity_btn = QPushButton("+ 添加规则")
        self.add_quantity_btn.setObjectName("settingsSecondaryButton")
        self.add_quantity_btn.clicked.connect(self._add_quantity_rule)
        self.quantity_count_label = QLabel("")
        self.quantity_count_label.setObjectName("settingsMutedText")
        controls.addWidget(self.add_quantity_btn)
        controls.addStretch(1)
        controls.addWidget(self.quantity_count_label)
        outer.addLayout(controls)

        self._render_quantity_rules(list(self._config.quantity_hotkeys))
        return page

    def _render_quantity_rules(self, rules: list[QuantityHotkey]) -> None:
        while self.quantity_rows_layout.count():
            item = self.quantity_rows_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        self._quantity_rows.clear()

        by_id = {rule.id: rule for rule in rules}
        ordered_rules = [
            by_id[item_id]
            for item_id in DEFAULT_QUANTITY_HOTKEY_IDS
            if item_id in by_id
        ]
        ordered_rules.extend(
            rule for rule in rules if rule.id not in DEFAULT_QUANTITY_KEY_BY_ID
        )

        headers = ("启用", "快捷键", "股数", "操作")
        for col, header in enumerate(headers):
            self.quantity_rows_layout.addWidget(self._header_label(header), 0, col)
        for row_index, rule in enumerate(ordered_rules, start=1):
            self._add_quantity_row_widgets(row_index, rule)
        self.quantity_rows_layout.setColumnStretch(4, 1)
        self._update_quantity_count()

    def _add_quantity_row_widgets(self, row: int, rule: QuantityHotkey) -> None:
        enabled = QCheckBox()
        enabled.setChecked(bool(rule.enabled))
        fixed_key = DEFAULT_QUANTITY_KEY_BY_ID.get(rule.id)
        if fixed_key is not None:
            key_widget: QWidget = QLabel(fixed_key.replace("Num+", "Num "))
            key_widget.setObjectName("settingsKeyCell")
            key_widget.setToolTip("默认保底按键，不支持修改")
            key_widget.setAlignment(Qt.AlignCenter)  # type: ignore[union-attr]
            key_edit = None
        else:
            key_edit = KeyCaptureEdit(rule.key or "")
            key_widget = key_edit

        spin = QSpinBox()
        spin.setRange(1, 999999)
        spin.setSingleStep(100)
        spin.setButtonSymbols(QAbstractSpinBox.NoButtons)
        spin.setValue(max(1, int(rule.quantity)))
        spin.setMinimumWidth(120)
        spin.setAlignment(Qt.AlignCenter)

        row_data: dict[str, object] = {
            "id": rule.id,
            "fixed_key": fixed_key,
            "key": key_edit,
            "enabled": enabled,
            "quantity": spin,
        }
        if fixed_key is not None:
            action_widget: QWidget = QLabel("固定")
            action_widget.setObjectName("settingsMutedText")
            action_widget.setAlignment(Qt.AlignCenter)  # type: ignore[union-attr]
        else:
            delete_btn = QPushButton("删除")
            delete_btn.setObjectName("settingsDangerButton")
            delete_btn.clicked.connect(
                lambda _checked=False, data=row_data: self._delete_quantity_row(data)
            )
            action_widget = delete_btn
        row_data["action"] = action_widget
        self._quantity_rows.append(row_data)

        widgets = (enabled, key_widget, spin, action_widget)
        for col, widget in enumerate(widgets):
            alignment = Qt.AlignCenter if isinstance(widget, QCheckBox) else Qt.Alignment()
            self.quantity_rows_layout.addWidget(widget, row, col, alignment)

    def _build_order_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("settingsPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 12, 4, 10)
        layout.setSpacing(10)

        route_row = QHBoxLayout()
        route_row.addWidget(QLabel("默认 ROUTE"))
        self.default_route_combo = self._route_combo(include_default=False)
        self.default_route_combo.setCurrentText(self._config.default_route or "SMART")
        route_row.addWidget(self.default_route_combo)
        route_hint = QLabel("规则选择“默认”时继承此值")
        route_hint.setObjectName("settingsMutedText")
        route_row.addWidget(route_hint)
        route_row.addStretch(1)
        layout.addLayout(route_row)

        capability_note = self._order_capability_note()
        self.order_capability_note = QLabel(capability_note)
        self.order_capability_note.setObjectName("settingsCapabilityNotice")
        self.order_capability_note.setVisible(bool(capability_note))
        if capability_note:
            layout.addWidget(self.order_capability_note)

        self.order_scroll = QScrollArea()
        self.order_scroll.setWidgetResizable(True)
        self.order_scroll.setObjectName("settingsScrollArea")
        self.order_rows_host = QWidget()
        self.order_rows_host.setObjectName("settingsOrderRows")
        self.order_rows_layout = QGridLayout(self.order_rows_host)
        self.order_rows_layout.setContentsMargins(0, 0, 0, 0)
        self.order_rows_layout.setHorizontalSpacing(8)
        self.order_rows_layout.setVerticalSpacing(6)
        self.order_scroll.setWidget(self.order_rows_host)
        layout.addWidget(self.order_scroll, 1)

        controls = QHBoxLayout()
        self.add_order_btn = QPushButton("+ 添加规则")
        self.add_order_btn.setObjectName("settingsSecondaryButton")
        self.add_order_btn.clicked.connect(self._add_order_rule)
        self.order_count_label = QLabel("")
        self.order_count_label.setObjectName("settingsMutedText")
        controls.addWidget(self.add_order_btn)
        controls.addStretch(1)
        controls.addWidget(self.order_count_label)
        layout.addLayout(controls)

        self._render_order_rules(list(self._config.order_hotkeys))
        return page

    def _order_capability_note(self) -> str:
        notes: list[str] = []
        if not self._route_effective and not self._hidden_effective:
            notes.append("当前交易通道不应用 ROUTE / HIDE：配置可以保存，实际下单使用 SMART 且按普通订单执行")
        elif not self._route_effective:
            notes.append("当前交易通道不应用 ROUTE：配置可以保存，实际下单使用 SMART")
        elif not self._hidden_effective:
            notes.append("当前交易通道不应用 HIDE：配置可以保存，实际按普通订单执行")
        if "IOC" not in self._supported_tifs:
            notes.append("当前交易通道不支持 IOC，选项已置灰且不会提交")
        return "；".join(notes)

    def _build_fixed_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("settingsPage")
        outer = QVBoxLayout(page)
        outer.setContentsMargins(4, 8, 4, 10)
        outer.setSpacing(0)
        layout = QGridLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(18)
        layout.setVerticalSpacing(8)
        layout.addWidget(self._header_label("快捷键"), 0, 0)
        layout.addWidget(self._header_label("功能", Qt.AlignLeft | Qt.AlignVCenter), 0, 1)
        layout.setRowMinimumHeight(0, 22)
        for row, (key, desc) in enumerate(FIXED_HOTKEY_DESCRIPTIONS, start=1):
            key_label = QLabel(key)
            key_label.setObjectName("settingsKeyCell")
            key_label.setAlignment(Qt.AlignCenter)
            desc_label = QLabel(desc)
            desc_label.setObjectName("settingsMutedText")
            desc_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            layout.addWidget(key_label, row, 0)
            layout.addWidget(desc_label, row, 1)
        layout.setColumnStretch(1, 1)
        outer.addLayout(layout)
        outer.addStretch(1)
        return page

    def _build_about_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("settingsPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 26, 28, 26)
        layout.setSpacing(14)
        title = QLabel("client")
        title.setObjectName("settingsAboutName")
        title.setFont(theme.ui_font(18, bold=True))
        self.version_label = QLabel(client_version())
        self.version_label.setObjectName("settingsMutedText")
        self.version_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(title)
        layout.addWidget(self.version_label)
        layout.addStretch(1)
        return page

    def _header_label(self, text: str, alignment: Qt.AlignmentFlag | Qt.Alignment = Qt.AlignCenter) -> QLabel:
        label = QLabel(text)
        label.setObjectName("settingsTableHeader")
        label.setFont(theme.ui_font(9, bold=True))
        label.setAlignment(alignment)
        label.setFixedHeight(22)
        return label

    @staticmethod
    def _center_combo(combo: QComboBox) -> None:
        for index in range(combo.count()):
            combo.setItemData(index, Qt.AlignCenter, Qt.TextAlignmentRole)

    @staticmethod
    def _new_combo() -> QComboBox:
        combo = QComboBox()
        popup = QListView(combo)
        popup.setObjectName("comboPopup")
        popup.setUniformItemSizes(True)
        popup.setMouseTracking(True)
        popup.setStyleSheet(theme.COMBO_POPUP_QSS)
        combo.setView(popup)
        combo.setMaxVisibleItems(8)
        return combo

    def _route_combo(self, *, include_default: bool = True) -> QComboBox:
        combo = self._new_combo()
        combo.setMinimumWidth(98)
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.NoInsert)
        if combo.lineEdit():
            combo.lineEdit().setAlignment(Qt.AlignCenter)
            combo.lineEdit().setMaxLength(24)
        if include_default:
            combo.addItem("默认", "DEFAULT")
        for route in self._route_options:
            combo.addItem(route, route)
        self._center_combo(combo)
        combo.setToolTip(
            "该 ROUTE 将按配置执行" if self._route_effective
            else "配置可以保存；当前交易通道实际下单使用 SMART"
        )
        return combo

    def _render_order_rules(self, rules: list[OrderHotkeyRule]) -> None:
        while self.order_rows_layout.count():
            item = self.order_rows_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        self._order_rows.clear()
        headers = ("启用", "快捷键", "方向", "TYPE", "TIF", "ROUTE", "偏移", "HIDE", "操作")
        for col, header in enumerate(headers):
            self.order_rows_layout.addWidget(self._header_label(header), 0, col)
        for row_index, rule in enumerate(rules, start=1):
            self._add_order_row_widgets(row_index, rule)
        self.order_rows_layout.setColumnStretch(9, 1)
        self._update_order_count()

    def _add_order_row_widgets(self, row: int, rule: OrderHotkeyRule) -> None:
        enabled = QCheckBox()
        enabled.setChecked(bool(rule.enabled))
        key = KeyCaptureEdit(rule.key or "")
        side = self._new_combo()
        for value in SIDES:
            side.addItem(SIDE_LABELS[value], value)
        side.setCurrentText(SIDE_LABELS.get(rule.side, "BUY"))
        self._center_combo(side)
        order_type = self._new_combo()
        for value in ORDER_TYPES:
            order_type.addItem(TYPE_LABELS[value], value)
        order_type.setCurrentText(TYPE_LABELS.get(rule.order_type, "LMT"))
        self._center_combo(order_type)
        tif = self._new_combo()
        for value in ORDER_TIFS:
            tif.addItem(value)
        tif.setCurrentText(rule.tif if rule.tif in VALID_TIFS else "Day")
        self._center_combo(tif)
        declared_tifs = bool(self._supported_tifs)
        for value in ORDER_TIFS:
            enabled_for_channel = (
                value.upper() in self._supported_tifs
                if declared_tifs
                else value.upper() != "IOC"
            )
            set_combo_item_enabled(
                tif,
                value,
                enabled_for_channel,
                f"当前交易通道不支持 {value} 订单",
            )
        route = self._route_combo(include_default=True)
        self._set_combo_data(route, rule.route or "DEFAULT")
        offset = QDoubleSpinBox()
        offset.setRange(-99.99, 99.99)
        offset.setSingleStep(0.01)
        offset.setDecimals(2)
        offset.setButtonSymbols(QAbstractSpinBox.NoButtons)
        offset.setValue(float(rule.price_offset or 0.0))
        offset.setMinimumWidth(82)
        offset.setAlignment(Qt.AlignCenter)
        hidden = QCheckBox()
        hidden.setChecked(bool(rule.hidden))
        hidden.setEnabled(True)
        hidden.setToolTip(
            "该规则将使用 HIDE 订单" if self._hidden_effective
            else "配置可以保存；当前交易通道实际按普通订单执行"
        )
        delete_btn = QPushButton("删除")
        delete_btn.setObjectName("settingsDangerButton")
        row_data = {
            "id": rule.id,
            "enabled": enabled,
            "key": key,
            "side": side,
            "order_type": order_type,
            "tif": tif,
            "route": route,
            "offset": offset,
            "hidden": hidden,
            "delete": delete_btn,
        }
        delete_btn.clicked.connect(lambda _checked=False, data=row_data: self._delete_order_row(data))
        self._order_rows.append(row_data)
        widgets = (enabled, key, side, order_type, tif, route, offset, hidden, delete_btn)
        for col, widget in enumerate(widgets):
            alignment = Qt.AlignCenter if isinstance(widget, QCheckBox) else Qt.Alignment()
            self.order_rows_layout.addWidget(widget, row, col, alignment)

    def _set_combo_data(self, combo: QComboBox, value: str) -> None:
        target = str(value or "").strip().upper()
        for index in range(combo.count()):
            if str(combo.itemData(index) or combo.itemText(index)).upper() == target:
                combo.setCurrentIndex(index)
                return
        combo.addItem(target, target)
        combo.setItemData(combo.count() - 1, Qt.AlignCenter, Qt.TextAlignmentRole)
        combo.setCurrentIndex(combo.count() - 1)

    @staticmethod
    def _route_combo_value(combo: QComboBox, fallback: str) -> str:
        index = combo.currentIndex()
        if index >= 0 and combo.currentText() == combo.itemText(index):
            value = combo.itemData(index) or combo.itemText(index)
        else:
            value = combo.currentText()
        return str(value or fallback).strip().upper()

    def _collect_order_rules(self) -> tuple[OrderHotkeyRule, ...]:
        rules: list[OrderHotkeyRule] = []
        for index, row in enumerate(self._order_rows, start=1):
            rule_id = str(row.get("id") or f"order_rule_{index}")
            route_combo = row["route"]
            assert isinstance(route_combo, QComboBox)
            rules.append(
                OrderHotkeyRule(
                    id=rule_id,
                    key=str(row["key"].text()).strip() or None,  # type: ignore[union-attr]
                    enabled=bool(row["enabled"].isChecked()),  # type: ignore[union-attr]
                    side=str(row["side"].currentData() or "buy"),  # type: ignore[union-attr]
                    order_type=str(row["order_type"].currentData() or "limit"),  # type: ignore[union-attr]
                    tif=str(row["tif"].currentText()),  # type: ignore[union-attr]
                    route=self._route_combo_value(route_combo, "DEFAULT"),
                    price_offset=float(row["offset"].value()),  # type: ignore[union-attr]
                    hidden=bool(row["hidden"].isChecked()),  # type: ignore[union-attr]
                )
            )
        return tuple(rules)

    def _collect_quantity_rules(self) -> tuple[QuantityHotkey, ...]:
        rules: list[QuantityHotkey] = []
        for row in self._quantity_rows:
            fixed_key = row.get("fixed_key")
            key_edit = row.get("key")
            if fixed_key is not None:
                key = str(fixed_key)
            else:
                assert isinstance(key_edit, KeyCaptureEdit)
                key = key_edit.text().strip() or None
            enabled = row["enabled"]
            quantity = row["quantity"]
            assert isinstance(enabled, QCheckBox)
            assert isinstance(quantity, QSpinBox)
            rules.append(
                QuantityHotkey(
                    id=str(row.get("id") or ""),
                    key=key,
                    quantity=quantity.value(),
                    enabled=bool(enabled.isChecked()),
                )
            )
        return tuple(rules)

    def _collect_config(self) -> HotkeyRuntimeConfig:
        default_route = self._route_combo_value(self.default_route_combo, "SMART")
        return HotkeyRuntimeConfig(
            default_route=default_route,
            quantity_hotkeys=self._collect_quantity_rules(),
            order_hotkeys=self._collect_order_rules(),
        )

    def _add_quantity_rule(self) -> None:
        if len(self._quantity_rows) >= MAX_QUANTITY_HOTKEY_RULES:
            self.set_error(f"股数快捷键最多 {MAX_QUANTITY_HOTKEY_RULES} 条")
            return
        next_index = 1
        existing_ids = {str(row.get("id")) for row in self._quantity_rows}
        while f"quantity_custom_{next_index}" in existing_ids:
            next_index += 1
        rules = list(self._collect_quantity_rules())
        rules.append(
            QuantityHotkey(
                id=f"quantity_custom_{next_index}",
                key=None,
                quantity=100,
                enabled=False,
            )
        )
        self._render_quantity_rules(rules)

    def _delete_quantity_row(self, row_data: dict[str, object]) -> None:
        rules = [
            rule
            for rule, source in zip(self._collect_quantity_rules(), self._quantity_rows)
            if source is not row_data
        ]
        self._render_quantity_rules(rules)

    def _update_quantity_count(self) -> None:
        count = len(self._quantity_rows)
        self.quantity_count_label.setText(f"{count} / {MAX_QUANTITY_HOTKEY_RULES}")
        self.add_quantity_btn.setEnabled(count < MAX_QUANTITY_HOTKEY_RULES)

    def _add_order_rule(self) -> None:
        if len(self._order_rows) >= MAX_ORDER_HOTKEY_RULES:
            self.set_error(f"下单快捷键最多 {MAX_ORDER_HOTKEY_RULES} 条")
            return
        next_index = len(self._order_rows) + 1
        existing_ids = {str(row.get("id")) for row in self._order_rows}
        while f"order_rule_custom_{next_index}" in existing_ids:
            next_index += 1
        rules = list(self._collect_order_rules())
        rules.append(OrderHotkeyRule(id=f"order_rule_custom_{next_index}", key=None))
        self._render_order_rules(rules)

    def _delete_order_row(self, row_data: dict[str, object]) -> None:
        rules = [
            rule
            for rule, source in zip(self._collect_order_rules(), self._order_rows)
            if source is not row_data
        ]
        self._render_order_rules(rules)

    def _update_order_count(self) -> None:
        self.order_count_label.setText(f"{len(self._order_rows)} / {MAX_ORDER_HOTKEY_RULES}")
        self.add_order_btn.setEnabled(len(self._order_rows) < MAX_ORDER_HOTKEY_RULES)

    def _restore_defaults(self) -> None:
        self._config = DEFAULT_HOTKEY_CONFIG
        self._quantity_rows.clear()
        self.hotkey_tabs.removeTab(0)
        self.hotkey_tabs.insertTab(0, self._build_quantity_page(), "股数快捷键")
        self.hotkey_tabs.removeTab(1)
        self.hotkey_tabs.insertTab(1, self._build_order_page(), "下单快捷键")
        self.hotkey_tabs.setCurrentIndex(0)
        self.set_error("")

    def _emit_save(self) -> None:
        config = self._collect_config()
        errors = validate_hotkey_config(config)
        errors.extend(validate_shortcut_sequences(bindings_from_config(config)))
        if errors:
            self.set_error("；".join(format_hotkey_validation_errors(errors)))
            return
        self.set_error("")
        self.save_requested.emit(config)

    def set_error(self, message: str) -> None:
        message = str(message or "").strip()
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))

    def select_tab(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        for tab_index, button in enumerate(self._tab_buttons):
            button.setChecked(tab_index == index)

    def current_tab_index(self) -> int:
        return self.stack.currentIndex()

    def current_hotkey_tab_index(self) -> int:
        return self.hotkey_tabs.currentIndex()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape:
            event.accept()
            self.close_requested.emit()
            return
        super().keyPressEvent(event)
