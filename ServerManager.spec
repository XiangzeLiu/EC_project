# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['Server_manager/main.py'],
    pathex=[],
    binaries=[],
    datas=[('Server_manager/templates', 'templates'), ('Server_manager/data', 'data'), ('Server_manager/routers', 'routers'), ('Server_manager/services', 'services'), ('Server_manager/admin.json', '.'), ('Server_manager/users.json', '.')],
    hiddenimports=['fastapi', 'uvicorn', 'starlette', 'jinja2', 'pydantic', 'python_multipart'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='ServerManager',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
