"""Qt theme tokens for the Trader_Server desktop UI."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QFont, QFontDatabase


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FONT_DIR = PROJECT_ROOT / "Client" / "assets" / "fonts"

FONT_UI = "Inter"
FONT_MONO = "JetBrains Mono"

TERM_BG = "#0B0E11"
HEADER_BG = "#12161A"
PANEL_BG = "#161B21"
PANEL_ALT_BG = "#1C2024"
CARD_SOFT_BG = "#171C22"
INPUT_BG = "#0B0E11"

BORDER = "#2A2E39"
BORDER_SOFT = "#23272F"
BORDER_WARN = "#5A4423"

ACCENT_GREEN = "#00C076"
ACCENT_RED = "#FF334B"
ACCENT_YELLOW = "#F5BD43"
ACCENT_BLUE = "#8FD0FF"

TEXT_PRIMARY = "#E2E8F0"
TEXT_DIM = "#A2ADB8"
TEXT_MUTED = "#8A95A5"
TEXT_LOW = "#677281"

BUY_BUTTON_FG = "#06140E"


def load_fonts() -> None:
    for name in ("Inter-Variable.ttf", "JetBrainsMono-Variable.ttf"):
        path = FONT_DIR / name
        if path.exists():
            QFontDatabase.addApplicationFont(str(path))


def ui_font(size: int = 10, *, bold: bool = False) -> QFont:
    font = QFont(FONT_UI, size)
    if bold:
        font.setBold(True)
    return font


def mono_font(size: int = 10, *, bold: bool = False) -> QFont:
    font = QFont(FONT_MONO, size)
    if bold:
        font.setBold(True)
    return font


APP_QSS = f"""
QWidget {{
    color: {TEXT_PRIMARY};
    font-family: "{FONT_UI}";
    font-size: 10pt;
}}

QMainWindow, QWidget#root {{
    background: {TERM_BG};
}}

QFrame#topHeader {{
    background: {HEADER_BG};
    border-bottom: 1px solid {BORDER};
}}

QFrame#sidePanel, QFrame#dataPanel {{
    background: {PANEL_BG};
    border: 1px solid {BORDER};
    border-radius: 12px;
}}

QFrame#softPanel {{
    background: {CARD_SOFT_BG};
    border: 1px solid {BORDER_SOFT};
    border-radius: 10px;
}}

QFrame#overviewBand {{
    background: {CARD_SOFT_BG};
    border: 1px solid {BORDER_SOFT};
    border-radius: 12px;
}}

QFrame#overviewInfoRow, QFrame#compactMetricCard {{
    background: {PANEL_ALT_BG};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}

QFrame#metricCard {{
    background: {PANEL_BG};
    border: 1px solid {BORDER_WARN};
    border-radius: 10px;
}}

QFrame#panelHeader {{
    background: {PANEL_ALT_BG};
    border-top-left-radius: 11px;
    border-top-right-radius: 11px;
}}

QLineEdit, QComboBox, QTextEdit, QPlainTextEdit {{
    background: {INPUT_BG};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 7px 11px;
    color: {TEXT_PRIMARY};
    min-height: 28px;
}}

QLineEdit:disabled, QComboBox:disabled, QTextEdit:disabled, QPlainTextEdit:disabled {{
    background: {PANEL_ALT_BG};
    border: 1px solid {BORDER_SOFT};
    color: {TEXT_LOW};
}}

QTextEdit[readOnly="true"], QPlainTextEdit[readOnly="true"] {{
    background: {INPUT_BG};
}}

QComboBox::drop-down {{
    width: 18px;
    border: none;
}}

QPushButton {{
    background: {PANEL_ALT_BG};
    border: 1px solid {PANEL_ALT_BG};
    border-radius: 8px;
    color: {TEXT_DIM};
    padding: 7px 13px;
    min-height: 28px;
}}

QPushButton#primaryButton {{
    background: {ACCENT_BLUE};
    color: #07121B;
    font-weight: 700;
}}

QPushButton#dangerButton {{
    background: {ACCENT_RED};
    color: white;
    font-weight: 700;
}}

QPushButton#warnButton {{
    background: {ACCENT_YELLOW};
    color: #161005;
    font-weight: 700;
}}

QPushButton:disabled {{
    background: {PANEL_ALT_BG};
    border: 1px solid {BORDER};
    color: {TEXT_LOW};
}}

QPushButton#primaryButton:disabled, QPushButton#warnButton:disabled, QPushButton#dangerButton:disabled {{
    background: {PANEL_ALT_BG};
    border: 1px solid {BORDER};
    color: {TEXT_LOW};
    font-weight: 600;
}}

QPushButton#refreshButton {{
    background: {PANEL_ALT_BG};
    border-radius: 8px;
    min-width: 36px;
    max-width: 42px;
}}

QLabel#caption {{
    color: {TEXT_DIM};
    font-size: 9pt;
}}

QLabel#lowText {{
    color: {TEXT_LOW};
}}

QLabel#metricTitle {{
    color: {TEXT_LOW};
    font-size: 9pt;
}}

QLabel#metricValue {{
    font-size: 15pt;
    font-weight: 700;
}}

QLabel#sectionHint {{
    color: {TEXT_DIM};
    font-size: 9pt;
}}

QLabel#infoValue {{
    font-size: 10pt;
}}

QLabel#monoText {{
    font-family: "{FONT_MONO}";
}}

QTabWidget::pane {{
    border: none;
}}

QTabBar::tab {{
    background: {PANEL_ALT_BG};
    color: {TEXT_DIM};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 7px 16px;
    margin-right: 6px;
}}

QTabBar::tab:selected {{
    background: {PANEL_BG};
    color: {TEXT_PRIMARY};
}}

QTableWidget {{
    background: {PANEL_BG};
    border: none;
    gridline-color: {BORDER_SOFT};
    color: {TEXT_DIM};
    selection-background-color: #20262E;
}}

QHeaderView::section {{
    background: #0E1217;
    color: {TEXT_LOW};
    border: none;
    border-right: 1px solid {BORDER_SOFT};
    padding: 10px 6px;
    font-family: "{FONT_MONO}";
    font-size: 9pt;
}}

QProgressBar {{
    background: {PANEL_ALT_BG};
    border: 1px solid {BORDER};
    border-radius: 7px;
    min-height: 16px;
    max-height: 16px;
    text-align: center;
    color: {TEXT_LOW};
}}

QProgressBar::chunk {{
    background: {ACCENT_BLUE};
    border-radius: 6px;
}}
"""

