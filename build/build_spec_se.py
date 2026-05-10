"""
Server_economic 打包配置 — PyInstaller Spec
产物: 单文件可执行, 窗口模式 (GUI控制面板 + 后台API服务)
跨平台: Windows 产出 .exe / macOS 产出 .app
"""

import PyInstaller.__main__
import sys
import os

_SEP = os.pathsep

_PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SE_DIR = os.path.join(_PROJECT, "Server_economic")
SE_DATA = os.path.join(SE_DIR, "data")

PyInstaller.__main__.run([
    "--name=SE_Node",
    "--onefile",
    "--windowed",
    "--icon=",
    "--clean",
    # ── 子模块数据 ──
    f"--add-data={os.path.join(SE_DIR, 'gui')}{_SEP}gui",
    f"--add-data={os.path.join(SE_DIR, 'network')}{_SEP}network",
    f"--add-data={os.path.join(SE_DIR, 'services')}{_SEP}services",
    f"--add-data={os.path.join(SE_DIR, 'models.py')}{_SEP}.",
    # ── data 目录（运行时创建 config.json / logs）──
    f"--add-data={SE_DATA}{_SEP}data",
    # ── 隐藏导入（FastAPI + WebSocket + GUI）──
    "--hidden-import=fastapi",
    "--hidden-import=uvicorn",
    "--hidden-import=websockets",
    "--hidden-import=pydantic",
    "--hidden-import=httpx",
    "--hidden-import=tkinter",
    "--hidden-import=tkinter.ttk",
    "--hidden-import=tkinter.messagebox",
    # ── uvicorn 内部依赖 ──
    "--hidden-import=uvicorn.logging",
    "--hidden-import=uvicorn.loops",
    "--hidden-import=uvicorn.loops.auto",
    "--hidden-import=uvicorn.protocols",
    "--hidden-import=uvicorn.protocols.http",
    "--hidden-import=uvicorn.protocols.websockets",
    "--hidden-import=uvicorn.lifespan",
    # ── 入口 ──
    os.path.join(SE_DIR, "main.py"),
])
