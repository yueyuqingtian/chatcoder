"""TRAE 业务 API 公共层 — 请求头构造与 401 刷新重试。

业务请求头（对齐 ai_agent 日志 `[HTTPClient] add_header` 全集）：
- Authorization: Cloud-IDE-JWT {token}
- X-User-Region + 设备指纹头（x-app-id / x-device-id / x-machine-id / x-ide-version 等）
方案: docs/plan-trae-solo-provider-integration.md §1.4 / §5.3。
"""
from __future__ import annotations

import logging
import uuid

from app.core.config import settings

logger = logging.getLogger(__name__)


def build_business_headers(token: str, meta: dict | None = None) -> dict:
    """构造 TRAE 业务请求头。meta 取自 trae_auth/provider 元数据，缺省用 settings 默认值。"""
    meta = meta or {}
    trace_id = uuid.uuid4().hex
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream, application/json",
        "Authorization": f"Cloud-IDE-JWT {token}",
        "X-User-Region": meta.get("region") or "cn",
        "request-traffic-type": "prod",
        "x-app-id": meta.get("app_id") or settings.trae_app_id,
        "x-app-version-code": meta.get("app_version_code") or settings.trae_app_version_code,
        "x-app-version": "default",
        "x-custom-trace-id": trace_id,
        "x-device-id": meta.get("device_id") or "",
        "x-device-brand": meta.get("device_brand") or "",
        "x-device-cpu": meta.get("device_cpu") or "",
        "x-device-type": meta.get("device_type") or "windows",
        "x-machine-id": meta.get("machine_id") or "",
        "x-ide-version": meta.get("ide_version") or settings.trae_ide_version,
        "x-ide-version-type": "stable",
        # 必须与 x-ide-version 配套：缺此头时服务端不返回内置模型目录
        # （只回用户自定义模型，实测 2026-08-25）
        "x-ide-version-code": meta.get("app_version_code") or settings.trae_app_version_code,
        "x-os-version": meta.get("os_version") or "",
        "x-flow-traceparent": f"04-{trace_id}-{uuid.uuid4().hex[:16]}-01",
        "User-Agent": settings.trae_user_agent,
    }
    return headers
