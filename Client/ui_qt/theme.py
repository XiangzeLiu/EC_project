"""Qt theme tokens for the temporary Client draft."""

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

QFrame#slotCard {{
    background: {PANEL_BG};
    border: 1px solid {BORDER_WARN};
    border-radius: 10px;
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

QPushButton#sellButton {{
    background: {ACCENT_RED};
    color: white;
    font-weight: 700;
}}

QPushButton#loginButton {{
    background: {ACCENT_BLUE};
    color: #07121B;
    font-weight: 700;
}}

QPushButton#refreshButton {{
    background: {PANEL_ALT_BG};
    border-radius: 8px;
    min-width: 36px;
    max-width: 42px;
}}

QPushButton#qtyStepButton {{
    padding: 3px 4px;
    min-width: 28px;
    max-width: 34px;
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
    font-family: "{FONT_MONO}";
}}

QTableView {{
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
"""

