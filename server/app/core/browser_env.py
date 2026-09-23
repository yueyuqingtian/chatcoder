"""内置 Chromium（Playwright 浏览器二进制）定位与注入。

背景：PyInstaller 只收集 playwright 的 driver（node 子进程 + JS 脚本），浏览器
二进制（chrome.exe / chrome.dll 等）不在其中，运行时 Playwright 只会去
%LOCALAPPDATA%/ms-playwright 找。用户从未手动执行过 `playwright install chromium`
时，浏览器工具必然报「Chromium 浏览器未安装。请在服务端运行: playwright install chromium」。

方案：构建期由 server/prepare-playwright-browsers.ps1 把 chromium 装到
server/vendor/ms-playwright，chatcoder-server.spec 将其作为 datas 打进产物
（_internal/ms-playwright）；运行时在使用 Playwright 之前把环境变量
PLAYWRIGHT_BROWSERS_PATH 指向该内置目录，实现"开箱可用"。

注意：环境变量必须在 async_playwright().start() 之前设置——driver 是 node 子进程，
只在启动时继承环境变量，启动后再改对本进程无效。
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# 与 chatcoder-server.spec 中 datas 的目标目录保持一致
_BUNDLED_DIR_NAME = "ms-playwright"

_cache: str | None = None
_resolved = False


def _bundled_dir_candidates() -> list[Path]:
    """内置浏览器目录的候选位置（按优先级）。"""
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        # onedir 模式：spec 的 datas 落在 _internal/（sys._MEIPASS 即该目录）
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / _BUNDLED_DIR_NAME)
        # 兜底：exe 同级目录，便于现场手工放置 / 调试
        candidates.append(Path(sys.executable).resolve().parent / _BUNDLED_DIR_NAME)
    else:
        # 源码运行：server/vendor/ms-playwright（app/core/browser_env.py → parents[2] = server）
        candidates.append(Path(__file__).resolve().parents[2] / "vendor" / _BUNDLED_DIR_NAME)
    return candidates


def _has_chromium(path: Path) -> bool:
    """目录内是否存在安装完整的 chromium（Playwright 以 INSTALLATION_COMPLETE 标记）。"""
    try:
        for child in path.glob("chromium-*"):
            if child.is_dir() and (child / "INSTALLATION_COMPLETE").exists():
                return True
    except OSError:
        return False
    return False


def resolve_bundled_browsers_path() -> str | None:
    """返回内置浏览器目录（含完整 chromium）的绝对路径；未内置则 None。"""
    global _cache, _resolved
    if _resolved:
        return _cache
    _resolved = True
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        # 已显式指定（用户自备浏览器 / 内网镜像），尊重其配置不覆盖
        return None
    for candidate in _bundled_dir_candidates():
        if _has_chromium(candidate):
            _cache = str(candidate)
            return _cache
    return None


def ensure_bundled_browsers_path() -> str | None:
    """确保 PLAYWRIGHT_BROWSERS_PATH 指向内置浏览器目录，返回生效路径。

    幂等；未内置时返回 None 且不设置环境变量（Playwright 回退默认目录，
    用户仍可自行 `playwright install chromium`，行为与内置前一致）。
    """
    path = resolve_bundled_browsers_path()
    if path and os.environ.get("PLAYWRIGHT_BROWSERS_PATH") != path:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = path
        logger.info("启用内置 Chromium: %s", path)
    return path


def reset_cache() -> None:
    """清除解析缓存（测试用）。"""
    global _cache, _resolved
    _cache = None
    _resolved = False
