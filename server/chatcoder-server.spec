# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：chatcoder-server 单目录模式(onedir)。

构建产物：dist/chatcoder-server/chatcoder-server.exe
Electron 主进程通过 spawn 拉起该 exe,监听 127.0.0.1:8000。
"""
import os
import sys
from pathlib import Path

block_cipher = None

SERVER_DIR = Path(SPECPATH).resolve()

datas = [
    (str(SERVER_DIR / ".env"), "."),
]

hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "aiosqlite",
    "sqlalchemy.dialects.sqlite",
    "sqlalchemy.dialects.sqlite.aiosqlite",
    "asyncpg",
    "passlib.handlers.bcrypt",
    "jose",
    "orjson",
    "httptools",
    "websockets",
    "email_validator",
    "multipart",
    "python_multipart",
]

a = Analysis(
    [str(SERVER_DIR / "run_server.py")],
    pathex=[str(SERVER_DIR)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter", "matplotlib", "PyQt5", "PyQt6", "PySide2", "PySide6",
        "IPython", "notebook", "pytest", "ruff", "mypy",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="chatcoder-server",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="chatcoder-server",
)
