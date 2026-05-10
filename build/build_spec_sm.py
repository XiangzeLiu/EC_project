"""
Server Manager 打包配置 — PyInstaller Spec
产物: 单文件可执行, 控制台模式 (后台服务)
跨平台: Windows 产出 .exe / macOS 产出 可执行文件
"""

import PyInstaller.__main__
import sys
import os

_SEP = os.pathsep   # Windows=':' , macOS/Linux=':'

_PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SM_DIR = os.path.join(_PROJECT, "Server_manager")

PyInstaller.__main__.run([
    "--name=ServerManager",
    "--onefile",
    "--console",
    "--clean",
    # ── 数据文件 (用 os.pathsep 跨平台兼容) ──
    f"--add-data={os.path.join(SM_DIR, 'templates')}{_SEP}templates",
    f"--add-data={os.path.join(SM_DIR, 'data')}{_SEP}data",
    f"--add-data={os.path.join(SM_DIR, 'routers')}{_SEP}routers",
    f"--add-data={os.path.join(SM_DIR, 'services')}{_SEP}services",
    f"--add-data={os.path.join(SM_DIR, 'admin.json')}{_SEP}.",
    f"--add-data={os.path.join(SM_DIR, 'users.json')}{_SEP}.",
    # ── 隐藏导入 ──
    "--hidden-import=fastapi",
    "--hidden-import=uvicorn",
    "--hidden-import=uvicorn.logging",
    "--hidden-import=uvicorn.loops",
    "--hidden-import=uvicorn.loops.auto",
    "--hidden-import=starlette",
    "--hidden-import=jinja2",
    "--hidden-import=pydantic",
    "--hidden-import=python_multipart",
    "--hidden-import=certifi",
    # ── 入口 ──
    os.path.join(SM_DIR, "main.py"),
])
