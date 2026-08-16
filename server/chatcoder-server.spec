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

# 安全说明：server/.env 含真实 API Key / 内部网关地址，禁止打包进产物
# （隐私红线：.env 已在 .gitignore 中，也不得进入 dist/ 安装包）
# 打包产物运行时 pydantic-settings 自动从环境变量 / 工作目录 .env 读取配置，
# 未配置默认模型时用户在应用设置中添加 BYOK 模型即可。
datas = [
    (str(SERVER_DIR / ".env.example"), ".env.example"),
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
    "h11",
    "uvicorn.protocols.http.h11_impl",
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
