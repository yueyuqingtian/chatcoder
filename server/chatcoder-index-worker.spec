# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the isolated symbol index worker."""
from pathlib import Path

block_cipher = None
SERVER_DIR = Path(SPECPATH).resolve()

hiddenimports = [
    "sqlite3",
]

a = Analysis(
    [str(SERVER_DIR / "app" / "index_worker.py")],
    pathex=[str(SERVER_DIR)],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "PyQt5", "PyQt6", "IPython", "notebook", "pytest", "ruff", "mypy"],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="chatcoder-index-worker",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=True, disable_windowed_traceback=False,
)
coll = COLLECT(
    exe, a.binaries, a.zipfiles, a.datas, strip=False, upx=False,
    upx_exclude=[], name="chatcoder-index-worker",
)
