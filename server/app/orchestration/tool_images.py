"""工具截图的持久化（plan-20261015-browser-agent）。

浏览器与电脑操控类工具的结果里带截图（`data.base64`）时，把图片落到 uploads 并返回预览信息
`{"url", "width", "height"}`，写进工具结果消息（落库）与 `tool.result` 实时事件：
消息流据此把截图卡**固定在该次工具调用的时间线位置**，刷新、回放后位置不变，点击可看大图。

为什么不复用工具自己落的文件：电脑操控截图在工作区 `.chatcoder/desktop-shots/`，不在静态
服务目录内；浏览器截图已在 uploads/browser-shots，直接复用其相对路径，不重复写盘。
"""
from __future__ import annotations

import base64
import io
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)

_SHOT_PREFIXES = ("browser_", "desktop_")


def persist_tool_image(tool_name: str, result: Any) -> dict | None:
    if not tool_name.startswith(_SHOT_PREFIXES):
        return None
    if not getattr(result, "ok", False):
        return None
    data = getattr(result, "data", None) or {}
    b64 = data.get("base64")
    if not isinstance(b64, str) or not b64:
        return None
    try:
        raw = base64.b64decode(b64)
    except (ValueError, TypeError):
        return None
    width = data.get("width")
    height = data.get("height")
    if not (isinstance(width, int) and isinstance(height, int)):
        try:
            from PIL import Image

            with Image.open(io.BytesIO(raw)) as img:
                width, height = img.size
        except Exception:  # noqa: BLE001
            width = height = None
    rel = str(data.get("screenshot_path") or "").replace("\\", "/").strip("/")
    if not rel:
        try:
            root = Path(settings.uploads_dir).resolve()
            shot_dir = root / "tool-shots"
            shot_dir.mkdir(parents=True, exist_ok=True)
            ext = "png" if raw[:4] == b"\x89PNG" else "jpg"
            name = f"{tool_name}_{int(time.time())}_{uuid.uuid4().hex[:6]}.{ext}"
            (shot_dir / name).write_bytes(raw)
            rel = f"tool-shots/{name}"
        except Exception as e:  # noqa: BLE001
            logger.warning("[tool-image] 截图落盘失败 tool=%s: %s", tool_name, e)
            return None
    out: dict = {"url": f"/api/uploads/{rel}"}
    if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
        out["width"] = width
        out["height"] = height
    page = data.get("url") if tool_name.startswith("browser_") else None
    if isinstance(page, str) and page:
        out["page"] = page[:300]
    title = data.get("title")
    if isinstance(title, str) and title:
        out["title"] = title[:120]
    return out
