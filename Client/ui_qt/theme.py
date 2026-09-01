"""Qt theme tokens for the temporary Client draft."""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtGui import QFont, QFontDatabase


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FONT_DIR = PROJECT_ROOT / "Client" / "assets" / "fonts"

FONT_UI = "Inter"
FONT_MONO = "JetBrains Mono"
FONT_CJK_FALLBACKS = ("SimHei", "Microsoft YaHei UI", "Microsoft YaHei")

TERM_BG = "#0B0E11"
HEADER_BG = "#12161A"
PANEL_BG = "#161B21"
PANEL_ALT_BG = "#1C2024"
CARD_SOFT_BG = "#171C22"
INPUT_BG = "#0B0E11"

BORDER = "#2A2E39"
BORDER_SOFT = "#23272F"
BORDER_WARN = "#A47B31"

ACCENT_GREEN = "#00C076"
ACCENT_RED = "#FF334B"
ACCENT_YELLOW = "#F5BD43"
ACCENT_BLUE = "#8FD0FF"

TEXT_PRIMARY = "#E2E8F0"
TEXT_DIM = "#A2ADB8"
TEXT_MUTED = "#8A95A5"
TEXT_LOW = "#677281"

BUY_BUTTON_FG = "#06140E"
TOAST_BG = "rgba(44, 48, 56, 185)"
_FONTS_LOADED = False


def load_fonts() -> None:
    global _FONTS_LOADED
    if _FONTS_LOADED:
        return
    for name in ("Inter-Variable.ttf", "JetBrainsMono-Variable.ttf"):
        path = FONT_DIR / name
        if path.exists():
            QFontDatabase.addApplicationFont(str(path))
    windows_fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    for name in ("simhei.ttf", "msyh.ttc", "msyhbd.ttc"):
        path = windows_fonts / name
        if path.exists():
            QFontDatabase.addApplicationFont(str(path))
    _FONTS_LOADED = True


def ui_font(size: int = 10, *, bold: bool = False) -> QFont:
    font = QFont(FONT_UI, size)
    font.setFamilies([FONT_UI, *FONT_CJK_FALLBACKS])
    if bold:
        font.setBold(True)
    return font


def mono_font(size: int = 10, *, bold: bool = False) -> QFont:
    font = QFont(FONT_MONO, size)
    font.setFamilies([FONT_MONO, *FONT_CJK_FALLBACKS])
    if bold:
        font.setBold(True)
    return font


SCROLLBAR_QSS = f"""
QScrollBar:vertical {{
    background: transparent;
    border: none;
    width: 10px;
    margin: 3px 2px 3px 0;
}}

QScrollBar:horizontal {{
    background: transparent;
    border: none;
    height: 10px;
    margin: 0 3px 2px 3px;
}}

QScrollBar::handle:vertical,
QScrollBar::handle:horizontal {{
    background: #353C46;
    border: 1px solid #424B57;
    border-radius: 4px;
}}

QScrollBar::handle:vertical {{
    min-height: 32px;
}}

QScrollBar::handle:horizontal {{
    min-width: 32px;
}}

QScrollBar::handle:vertical:hover,
QScrollBar::handle:horizontal:hover {{
    background: #4A5461;
    border-color: #596575;
}}

QScrollBar::handle:vertical:pressed,
QScrollBar::handle:horizontal:pressed {{
    background: #5A6675;
    border-color: #6B7889;
}}

QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical {{
    height: 0;
}}

QScrollBar::add-line:horizontal,
QScrollBar::sub-line:horizontal {{
    width: 0;
}}

QScrollBar::add-page:vertical,
QScrollBar::sub-page:vertical,
QScrollBar::add-page:horizontal,
QScrollBar::sub-page:horizontal {{
    background: transparent;
}}
"""


COMBO_POPUP_QSS = f"""
QListView#comboPopup {{
    background: {INPUT_BG};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    border-radius: 4px;
    outline: none;
    padding: 3px 0;
}}

QListView#comboPopup::item {{
    background: {INPUT_BG};
    color: {TEXT_PRIMARY};
    border: none;
    min-height: 30px;
    padding: 4px 10px;
}}

QListView#comboPopup::item:hover,
QListView#comboPopup::item:selected {{
    background: {PANEL_ALT_BG};
    color: #FFFFFF;
}}

QListView#comboPopup::item:disabled {{
    background: {INPUT_BG};
    color: {TEXT_MUTED};
}}
""" + SCROLLBAR_QSS


POPUP_QSS = f"""
QDialog,
QMessageBox {{
    background: {PANEL_BG};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    font-family: "{FONT_UI}", "SimHei", "Microsoft YaHei UI", "Microsoft YaHei";
    font-size: 10pt;
}}

QDialog QLabel,
QMessageBox QLabel {{
    color: {TEXT_PRIMARY};
}}

QDialog QLabel#dialogBody {{
    color: {TEXT_DIM};
}}

QDialog QLineEdit {{
    background: {INPUT_BG};
    border: 1px solid {BORDER};
    border-radius: 7px;
    color: {TEXT_PRIMARY};
    min-height: 28px;
    padding: 5px 9px;
    selection-background-color: {ACCENT_BLUE};
    selection-color: #07121B;
}}

QDialog QLineEdit:focus {{
    border-color: {ACCENT_BLUE};
}}

QDialog QDialogButtonBox {{
    background: transparent;
}}

QDialog QPushButton,
QMessageBox QPushButton {{
    background: {PANEL_ALT_BG};
    border: 1px solid {BORDER};
    border-radius: 7px;
    color: {TEXT_PRIMARY};
    min-height: 30px;
    padding: 5px 14px;
}}

QDialog QPushButton:hover,
QMessageBox QPushButton:hover {{
    background: #252C34;
    border-color: {TEXT_LOW};
}}

QDialog QPushButton:pressed,
QMessageBox QPushButton:pressed {{
    background: {INPUT_BG};
    border-color: {BORDER_WARN};
}}

QDialog QPushButton#dialogConfirmButton,
QDialog QPushButton#loginConfirmButton {{
    background: {ACCENT_BLUE};
    border-color: {ACCENT_BLUE};
    color: #07121B;
    font-weight: 700;
}}

QDialog QPushButton#dialogConfirmButton:hover,
QDialog QPushButton#loginConfirmButton:hover {{
    background: #A5DFFF;
    border-color: #A5DFFF;
}}

QDialog QPushButton#dialogConfirmButton:pressed,
QDialog QPushButton#loginConfirmButton:pressed {{
    background: #5CB8E8;
    border-color: #5CB8E8;
}}

QDialog QPushButton#dialogCancelButton {{
    color: {TEXT_DIM};
}}
"""


TOAST_QSS = f"""
QFrame#weakToast {{
    background: {TOAST_BG};
    border: 1px solid {BORDER_SOFT};
    border-radius: 14px;
}}
"""


APP_QSS = f"""
{POPUP_QSS}
{TOAST_QSS}

QWidget {{
    color: {TEXT_PRIMARY};
    font-family: "{FONT_UI}", "SimHei", "Microsoft YaHei UI", "Microsoft YaHei";
    font-size: 10pt;
}}

QPushButton:focus,
QCheckBox:focus,
QRadioButton:focus,
QTabBar:focus,
QTabBar::tab:focus,
QAbstractItemView:focus,
QAbstractItemView::item:focus {{
    outline: none;
}}

{SCROLLBAR_QSS}

QMainWindow, QWidget#root {{
    background: {TERM_BG};
}}

QFrame#topHeader {{
    background: {HEADER_BG};
    border-bottom: 1px solid {BORDER};
}}

QFrame#slotCard {{
    background: {PANEL_BG};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}

QFrame#slotCard[activePanel="true"] {{
    border: 2px solid {ACCENT_YELLOW};
}}

QFrame#dataPanel {{
    background: {PANEL_BG};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}

QFrame#panelHeader {{
    background: {PANEL_ALT_BG};
    border-top-left-radius: 9px;
    border-top-right-radius: 9px;
}}

QFrame#inputBox, QLineEdit, QComboBox {{
    background: {INPUT_BG};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 7px 11px;
    color: {TEXT_PRIMARY};
    min-height: 28px;
}}

QComboBox::drop-down {{
    width: 18px;
    border: none;
}}

QComboBox:hover,
QComboBox:focus {{
    border-color: {TEXT_LOW};
}}

QComboBox:disabled {{
    background: {PANEL_ALT_BG};
    color: {TEXT_LOW};
}}

{COMBO_POPUP_QSS}

QPushButton {{
    background: {PANEL_ALT_BG};
    border: 1px solid {PANEL_ALT_BG};
    border-radius: 8px;
    color: {TEXT_DIM};
    padding: 7px 13px;
    min-height: 28px;
}}

QPushButton#dangerButton {{
    background: #B83246;
    color: white;
}}

QPushButton#buyButton {{
    background: {ACCENT_GREEN};
    color: {BUY_BUTTON_FG};
    font-weight: 700;
}}

QPushButton#buyButton:hover {{
    background: #10D689;
    border-color: #10D689;
}}

QPushButton#buyButton:pressed {{
    background: #08A96B;
    border-color: #08A96B;
    padding-top: 8px;
    padding-bottom: 6px;
}}

QPushButton#sellButton {{
    background: {ACCENT_RED};
    color: white;
    font-weight: 700;
}}

QPushButton#sellButton:hover {{
    background: #FF455F;
    border-color: #FF455F;
}}

QPushButton#sellButton:pressed {{
    background: #D92740;
    border-color: #D92740;
    padding-top: 8px;
    padding-bottom: 6px;
}}

QPushButton#buyButton[enterSelected="true"],
QPushButton#sellButton[enterSelected="true"] {{
    border: 1.5px solid #F5A623;
}}

QPushButton#buyButton:disabled,
QPushButton#sellButton:disabled {{
    background: {PANEL_ALT_BG};
    border: 1px solid {BORDER};
    color: {TEXT_LOW};
}}

QPushButton#loginButton {{
    background: {ACCENT_BLUE};
    color: #07121B;
    font-weight: 700;
}}

QPushButton#logoutButton {{
    background: {ACCENT_YELLOW};
    border: 1px solid {ACCENT_YELLOW};
    border-radius: 8px;
    color: #17130A;
    font-weight: 700;
    padding: 0 10px;
    min-height: 28px;
    max-height: 28px;
}}

QPushButton#logoutButton:hover {{
    background: #FFD166;
    border-color: #FFD166;
}}

QPushButton#logoutButton:pressed {{
    background: #D8A334;
    border-color: #D8A334;
}}

QPushButton#logoutButton:disabled {{
    background: {PANEL_ALT_BG};
    border-color: {BORDER};
    color: {TEXT_LOW};
}}

QPushButton#settingsGearButton {{
    background: {PANEL_ALT_BG};
    border: 1px solid {BORDER};
    border-radius: 14px;
    color: {TEXT_LOW};
    font-family: "Segoe UI Symbol", "{FONT_UI}", "SimHei", "Microsoft YaHei UI", "Microsoft YaHei";
    font-size: 12pt;
    padding: 0;
    min-width: 28px;
    max-width: 28px;
    min-height: 28px;
    max-height: 28px;
}}

QPushButton#settingsGearButton:hover {{
    background: {BORDER_SOFT};
    color: {TEXT_DIM};
}}

QPushButton#settingsGearButton:pressed {{
    background: {INPUT_BG};
    color: {TEXT_PRIMARY};
}}

QPushButton#refreshIconButton {{
    background: {PANEL_ALT_BG};
    border: 1px solid {BORDER};
    border-radius: 16px;
    color: {TEXT_DIM};
    font-family: "Segoe UI Symbol";
    font-size: 15pt;
    padding: 0;
    min-width: 32px;
    max-width: 32px;
    min-height: 32px;
    max-height: 32px;
}}

QPushButton#refreshIconButton:hover {{
    background: {BORDER_SOFT};
    border-color: {TEXT_LOW};
    color: {TEXT_PRIMARY};
}}

QPushButton#refreshIconButton:pressed {{
    background: {INPUT_BG};
}}

QPushButton#liveOrdersButton {{
    background: transparent;
    border-color: transparent;
    color: {TEXT_LOW};
    font-weight: 700;
}}

QPushButton#liveOrdersButton[online="true"] {{
    color: {ACCENT_GREEN};
}}

QPushButton#orderTabButton {{
    background: transparent;
    border-color: transparent;
    color: {TEXT_LOW};
    font-weight: 700;
}}

QPushButton#orderTabButton[selected="true"],
QPushButton#liveOrdersButton[selected="true"] {{
    color: {TEXT_PRIMARY};
    border-color: {BORDER_WARN};
}}

QPushButton#liveOrdersButton[selected="true"] {{
    color: {ACCENT_GREEN};
}}

QLineEdit#qtyInput {{
    background: transparent;
    border: none;
    padding: 0 2px;
    color: {TEXT_PRIMARY};
}}

QCheckBox#hiddenOrderCheck {{
    color: {TEXT_MUTED};
    spacing: 6px;
}}

QCheckBox#hiddenOrderCheck::indicator,
QWidget#settingsOverlay QCheckBox::indicator {{
    width: 14px;
    height: 14px;
    border-radius: 3px;
    border: 1px solid {BORDER};
    background: {INPUT_BG};
}}

QCheckBox#hiddenOrderCheck::indicator:hover,
QWidget#settingsOverlay QCheckBox::indicator:hover {{
    border-color: {TEXT_LOW};
}}

QCheckBox#hiddenOrderCheck::indicator:checked,
QWidget#settingsOverlay QCheckBox::indicator:checked {{
    background: {ACCENT_YELLOW};
    border-color: {ACCENT_YELLOW};
}}

QCheckBox#hiddenOrderCheck::indicator:disabled,
QWidget#settingsOverlay QCheckBox::indicator:disabled {{
    background: #AEB4BC;
    border-color: #6C737C;
}}

QWidget#settingsOverlay QCheckBox::indicator:checked:disabled {{
    background: #8C7A4E;
    border-color: #A08D5A;
}}

QCheckBox#hiddenOrderCheck:disabled,
QWidget#settingsOverlay QCheckBox:disabled {{
    color: {TEXT_MUTED};
}}

QPushButton#cancelOrderButton {{
    background: {ACCENT_RED};
    color: white;
    font-weight: 700;
    font-size: 8pt;
    padding: 0 10px;
    min-height: 21px;
    max-height: 21px;
}}

QPushButton#cancelOrderButton:hover {{
    background: #FF455F;
    border-color: #FF455F;
}}

QPushButton#cancelOrderButton:pressed {{
    background: #D92740;
    border-color: #D92740;
    padding-top: 1px;
}}

QPushButton#consoleButton {{
    padding: 3px 8px;
    min-height: 24px;
    max-height: 30px;
}}

QLabel#caption {{
    color: {TEXT_DIM};
    font-size: 9pt;
}}

QLabel#lowText {{
    color: {TEXT_LOW};
}}

QLabel#monoText {{
    font-family: "{FONT_MONO}", "SimHei", "Microsoft YaHei UI", "Microsoft YaHei";
}}

QWidget#settingsOverlay {{
    background: rgba(0, 0, 0, 145);
}}

QFrame#settingsPanel {{
    background: {PANEL_BG};
    border: 1px solid {BORDER};
    border-radius: 12px;
}}

QFrame#settingsHeader {{
    background: {PANEL_ALT_BG};
    border-top-left-radius: 12px;
    border-top-right-radius: 12px;
    border-bottom: 1px solid {BORDER_SOFT};
}}

QLabel#settingsTitle,
QLabel#settingsPageTitle,
QLabel#settingsAboutName {{
    color: {TEXT_PRIMARY};
}}

QPushButton#settingsCloseButton {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: 14px;
    color: {TEXT_MUTED};
    font-size: 14pt;
    padding: 0;
    min-width: 28px;
    max-width: 28px;
    min-height: 28px;
    max-height: 28px;
}}

QPushButton#settingsCloseButton:hover {{
    background: {BORDER_SOFT};
    color: {TEXT_PRIMARY};
}}

QFrame#settingsSidebar {{
    background: #12161A;
    border-right: 1px solid {BORDER_SOFT};
    min-width: 150px;
    max-width: 150px;
}}

QPushButton#settingsTabButton {{
    background: transparent;
    border: 1px solid transparent;
    color: {TEXT_MUTED};
    text-align: left;
    padding: 8px 10px;
    min-height: 34px;
}}

QPushButton#settingsTabButton:hover {{
    background: {PANEL_ALT_BG};
    color: {TEXT_DIM};
}}

QPushButton#settingsTabButton:checked {{
    background: {PANEL_ALT_BG};
    border-color: {BORDER_WARN};
    color: {TEXT_PRIMARY};
}}

QWidget#settingsPage {{
    background: {PANEL_BG};
}}

QLabel#settingsMutedText {{
    color: {TEXT_MUTED};
}}

QLabel#settingsErrorText {{
    color: {ACCENT_YELLOW};
}}

QLabel#settingsCapabilityNotice {{
    background: rgba(164, 123, 49, 32);
    border: 1px solid {BORDER_WARN};
    border-radius: 6px;
    color: {TEXT_DIM};
    padding: 6px 9px;
}}

QLabel#settingsTableHeader {{
    color: {TEXT_MUTED};
}}

QWidget#settingsOverlay QWidget#settingsOrderRows QLineEdit,
QWidget#settingsOverlay QWidget#settingsOrderRows QComboBox,
QWidget#settingsOverlay QWidget#settingsOrderRows QSpinBox,
QWidget#settingsOverlay QWidget#settingsOrderRows QDoubleSpinBox,
QWidget#settingsOverlay QWidget#settingsOrderRows QPushButton,
QWidget#settingsOverlay QWidget#settingsOrderRows QCheckBox {{
    font-size: 9pt;
}}

QWidget#settingsOverlay QWidget#settingsOrderRows QLineEdit,
QWidget#settingsOverlay QWidget#settingsOrderRows QComboBox {{
    padding: 4px 6px;
    min-height: 26px;
}}

QWidget#settingsOverlay QWidget#settingsOrderRows QDoubleSpinBox {{
    padding: 4px 6px;
    min-height: 26px;
}}

QWidget#settingsOverlay QWidget#settingsOrderRows QPushButton#settingsDangerButton {{
    padding: 4px 6px;
    min-height: 26px;
}}

QLabel#settingsKeyCell {{
    color: {TEXT_PRIMARY};
    background: {INPUT_BG};
    border: 1px solid {BORDER_SOFT};
    border-radius: 6px;
    padding: 5px 8px;
}}

QWidget#settingsOverlay QSpinBox,
QWidget#settingsOverlay QDoubleSpinBox {{
    background: {INPUT_BG};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_SOFT};
    border-radius: 6px;
    padding: 5px 8px;
    selection-background-color: {ACCENT_BLUE};
    selection-color: #07121B;
}}

QWidget#settingsOverlay QSpinBox::up-button,
QWidget#settingsOverlay QSpinBox::down-button,
QWidget#settingsOverlay QDoubleSpinBox::up-button,
QWidget#settingsOverlay QDoubleSpinBox::down-button {{
    background: {PANEL_ALT_BG};
    color: {TEXT_MUTED};
    border: none;
    width: 16px;
}}

QScrollArea#settingsScrollArea {{
    background: {PANEL_BG};
    border: none;
}}

QScrollArea#settingsScrollArea QWidget#settingsOrderRows,
QScrollArea#settingsScrollArea QWidget#qt_scrollarea_viewport {{
    background: {PANEL_BG};
}}

QTabWidget#hotkeyInnerTabs::pane {{
    border: 1px solid {BORDER_SOFT};
    border-radius: 8px;
    top: -1px;
}}

QTabWidget#hotkeyInnerTabs QTabBar::tab {{
    background: transparent;
    color: {TEXT_MUTED};
    padding: 8px 14px;
    border: 1px solid transparent;
}}

QTabWidget#hotkeyInnerTabs QTabBar::tab:selected {{
    color: {TEXT_PRIMARY};
    background: {PANEL_ALT_BG};
    border-color: {BORDER_WARN};
}}

QPushButton#settingsPrimaryButton {{
    background: {ACCENT_BLUE};
    color: #07121B;
    font-weight: 700;
}}

QPushButton#settingsPrimaryButton:hover {{
    background: #A5DFFF;
    border-color: #A5DFFF;
}}

QPushButton#settingsPrimaryButton:pressed {{
    background: #5CB8E8;
    border-color: #5CB8E8;
    padding-top: 8px;
    padding-bottom: 6px;
}}

QPushButton#settingsSecondaryButton {{
    background: {PANEL_ALT_BG};
    color: {TEXT_DIM};
}}

QPushButton#settingsSecondaryButton:hover {{
    background: #252C34;
    border-color: {TEXT_LOW};
    color: {TEXT_PRIMARY};
}}

QPushButton#settingsSecondaryButton:pressed {{
    background: {INPUT_BG};
    border-color: {BORDER_WARN};
    color: {TEXT_PRIMARY};
    padding-top: 8px;
    padding-bottom: 6px;
}}

QPushButton#settingsDangerButton {{
    background: transparent;
    color: {ACCENT_RED};
}}

QPushButton#quoteQueryButton {{
    background: {PANEL_ALT_BG};
    border: 1px solid {BORDER};
    border-radius: 8px;
    color: {TEXT_MUTED};
    padding: 0;
    min-height: 44px;
    max-height: 44px;
}}

QPushButton#quoteQueryButton:hover {{
    background: #252C34;
    border-color: {TEXT_LOW};
    color: {TEXT_PRIMARY};
}}

QPushButton#quoteQueryButton:pressed {{
    background: {INPUT_BG};
    border-color: {BORDER};
    color: {ACCENT_BLUE};
}}

QTableView {{
    background: {PANEL_BG};
    border: none;
    gridline-color: {BORDER_SOFT};
    color: {TEXT_DIM};
    selection-background-color: #20262E;
}}

QTableView#tradeDataTable QTableCornerButton::section {{
    background: {PANEL_BG};
    border: none;
}}

QHeaderView::section {{
    background: #0E1217;
    color: {TEXT_LOW};
    border: none;
    border-right: 1px solid {BORDER_SOFT};
    padding: 10px 6px;
    font-family: "{FONT_MONO}", "SimHei", "Microsoft YaHei UI", "Microsoft YaHei";
    font-size: 9pt;
}}
"""

