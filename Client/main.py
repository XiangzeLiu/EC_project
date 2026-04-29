"""
Client Entry Point
证券股票交易系统客户端启动入口
"""

import ctypes
import sys
import os


def main():
    """启动交易终端"""
    # ── Windows DPI 感知（4K清晰渲染）──
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # Per-Monitor DPI Aware v2
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    # 将 Client 的父目录加入 sys.path，使 "Client" 被识别为顶层包
    _client_dir = os.path.dirname(os.path.abspath(__file__))
    _project_root = os.path.dirname(_client_dir)
    if _project_root not in sys.path:
        sys.path.insert(0, _project_root)

    from Client.ui.main_window import TradingTerminal

    app = TradingTerminal()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()


if __name__ == "__main__":
    main()
