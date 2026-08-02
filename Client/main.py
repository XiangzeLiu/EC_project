"""Client official entry point.

This entry launches the PySide6 client UI.
"""

from __future__ import annotations

import ctypes
import os
import re
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


def _run_package_self_test() -> int:
    """Validate resources and imports without opening the trading window."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    _ensure_project_root_on_path()

    try:
        import struct
        from zoneinfo import ZoneInfo

        import PySide6
        import websockets
        from PySide6.QtWidgets import QApplication

        from Client.ui_qt import theme
        from Client.ui_qt.client_version import client_version, packaged_build_info_available
        from Client.ui_qt.hotkey_config_store import hotkey_config_path

        if struct.calcsize("P") * 8 != 64:
            raise RuntimeError("Client package requires 64-bit Python/Windows")
        ZoneInfo("America/New_York")
        if not PySide6.__version__ or not websockets.__version__:
            raise RuntimeError("runtime dependency version is unavailable")
        app = QApplication.instance() or QApplication([])
        theme.load_fonts()
        for font_name in ("Inter-Variable.ttf", "JetBrainsMono-Variable.ttf"):
            if not (theme.FONT_DIR / font_name).is_file():
                raise RuntimeError(f"missing bundled font: {font_name}")
        if getattr(sys, "frozen", False) and not packaged_build_info_available():
            raise RuntimeError("missing packaged build metadata")
        if not re.fullmatch(r"v_0_\d{14}", client_version()):
            raise RuntimeError("invalid packaged Client version")
        config_path = hotkey_config_path()
        if config_path.name != "hotkey.json" or config_path.parent.name != "SC Client":
            raise RuntimeError("invalid Client configuration path")
        del app
    except Exception as exc:
        print(f"Client package self-test failed: {exc}")
        return 1
    print("Client package self-test passed")
    return 0


def main() -> int:
    _enable_windows_dpi_awareness()
    if "--package-self-test" in sys.argv:
        return _run_package_self_test()
    _ensure_project_root_on_path()

    from Client.ui_qt.main_window import run

    return run()


if __name__ == "__main__":
    raise SystemExit(main())
