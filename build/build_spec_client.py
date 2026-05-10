"""
Client 打包配置 — PyInstaller Spec
产物: 单文件可执行, 窗口模式 (tkinter GUI)
跨平台: Windows 产出 .exe / macOS 产出 .app
"""

import PyInstaller.__main__
import sys
import os

_SEP = os.pathsep

_PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT_DIR = os.path.join(_PROJECT, "Client")

PyInstaller.__main__.run([
    "--name=TradingTerminal",
    "--onefile",
    "--windowed",
    "--icon=",
    "--clean",
    # ── 子模块数据 ──
    f"--add-data={os.path.join(CLIENT_DIR, 'network')}{_SEP}network",
    f"--add-data={os.path.join(CLIENT_DIR, 'services')}{_SEP}services",
    f"--add-data={os.path.join(CLIENT_DIR, 'ui')}{_SEP}ui",
    f"--add-data={os.path.join(CLIENT_DIR, 'config.py')}{_SEP}.",
    f"--add-data={os.path.join(CLIENT_DIR, 'constants.py')}{_SEP}.",
    f"--add-data={os.path.join(CLIENT_DIR, '.tt_config.json')}{_SEP}.",
    # ── 隐藏导入 ──
    "--hidden-import=tkinter",
    "--hidden-import=tkinter.ttk",
    "--hidden-import=tkinter.messagebox",
    "--hidden-import=tkinter.scrolledtext",
    "--hidden-import=websockets",
    # ── 入口 ──
    os.path.join(CLIENT_DIR, "main.py"),
])
