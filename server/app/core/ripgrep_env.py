"""ripgrep（rg）的定位：优先用安装包内置的 rg，其次系统 PATH；都没有时返回 None，由调用方回退纯 Python 搜索。

- 打包运行：<_MEIPASS>/ripgrep/rg.exe（chatcoder-server.spec 把 server/vendor/ripgrep 打进 datas）
- 源码运行：server/vendor/ripgrep/rg.exe（没有时退到 PATH 上的 rg）
"""
from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_BUNDLED_DIR_NAME = "ripgrep"
_EXE_NAME = "rg.exe" if sys.platform == "win32" else "rg"


@dataclass(frozen=True)
class RipgrepInfo:
    path: str
    source: str  # "bundled"（随包内置）| "path"（系统 PATH）


def _bundled_candidates() -> list[Path]:
    out: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        out.append(Path(meipass) / _BUNDLED_DIR_NAME / _EXE_NAME)
    # 源码运行：app/core/ripgrep_env.py → parents[2] 即 server 目录
    out.append(Path(__file__).resolve().parents[2] / "vendor" / _BUNDLED_DIR_NAME / _EXE_NAME)
    return out


@lru_cache(maxsize=1)
def locate_ripgrep() -> RipgrepInfo | None:
    """定位 rg 可执行文件；结果进程内缓存，装了新的 rg 需重启应用后才会被发现。"""
    for candidate in _bundled_candidates():
        if candidate.is_file():
            return RipgrepInfo(path=str(candidate), source="bundled")
    found = shutil.which("rg")
    if found:
        return RipgrepInfo(path=found, source="path")
    return None
