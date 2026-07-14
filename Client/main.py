"""Client official entry point.

This entry launches the PySide6 client UI.
"""

from __future__ import annotations

import ctypes
import os
import sys


def _enable_windows_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _ensure_project_root_on_path() -> None:
    client_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(client_dir)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)


def main() -> int:
    _enable_windows_dpi_awareness()
    _ensure_project_root_on_path()

    from Client.ui_qt.main_window import run

    return run()


if __name__ == "__main__":
    raise SystemExit(main())
