"""TRAE SOLO CN 供应商专属路由：登录 / 状态 / 退出 / 目录同步。

端点契约见 docs/plan-trae-solo-provider-integration.md §5.6（对齐 workbuddy_auth）：
- POST /providers/{id}/trae/login/start   启动浏览器授权（返回授权 URL，后台等回调换 token）
- POST /providers/{id}/trae/login/cancel  取消登录
- GET  /providers/{id}/trae/login/status  查询登录状态
- POST /providers/{id}/trae/logout        退出登录（清会话与模型 key）
- POST /providers/{id}/trae/sync          同步远端模型目录（batch_get_detail_param → Model 表）
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.trae import oauth as trae_oauth
from app.auth.trae import session as trae_session
from app.core.config import settings
from app.gateway.schemas import (
    TraeLoginStartOut,
    TraeLoginStatusOut,
    TraeSyncOut,
)
from app.persistence.database import get_db
from app.services import provider_service

logger = logging.getLogger(__name__)
router = APIRouter()


def _agent_host(provider) -> str:
    """TRAE Agent API 主机：Provider.base_url 缺省用内置默认（用户无需手填）。"""
    return (provider.base_url or settings.trae_agent_endpoint).rstrip("/")


async def _get_trae_provider(db: AsyncSession, provider_id: int):
    provider = await provider_service.get_provider(db, provider_id)
    if provider is None:
        raise HTTPException(404, "provider not found")
    if (provider.api_format or "openai").lower() != "trae":
        raise HTTPException(400, "该供应商不是 trae 类型")
    return provider


def _ensure_device(auth):
    """设备指纹缺省兜底：首登录用占位 device_id=0 + 本机 machine_id（登录成功后可回填）。"""
    machine_id = (auth.machine_id if auth else None) or ""
    if not machine_id:
        from app.auth.trae.device import compute_machine_id
        machine_id = compute_machine_id()
    device_id = (auth.device_id if auth else None) or "0"
    return device_id, machine_id


@router.post("/providers/{provider_id}/trae/login/start",
             response_model=TraeLoginStartOut)
async def trae_login_start(provider_id: int, db: AsyncSession = Depends(get_db)):
    """登录：起本地回调 + 返回授权 URL（前端用系统浏览器打开）+ 后台等回调。"""
    await _get_trae_provider(db, provider_id)
    auth = await trae_session.load_auth(db, provider_id)
    device_id, machine_id = _ensure_device(auth)
    result = await trae_oauth.start_login(
        db, provider_id,
        api_host=settings.trae_account_endpoint,
        console_host=settings.trae_console_host,
        client_id=settings.trae_client_id,
        ide_version=settings.trae_ide_version,
        machine_id=machine_id,
        device_id=device_id,
    )
    return TraeLoginStartOut(**result)


@router.post("/providers/{provider_id}/trae/login/cancel")
async def trae_login_cancel(provider_id: int, db: AsyncSession = Depends(get_db)):
    await _get_trae_provider(db, provider_id)
    await trae_oauth.cancel_login(db, provider_id)
    return {"ok": True}


@router.get("/providers/{provider_id}/trae/login/status",
            response_model=TraeLoginStatusOut)
async def trae_login_status(provider_id: int, db: AsyncSession = Depends(get_db)):
    await _get_trae_provider(db, provider_id)
    return await trae_oauth.get_login_status(db, provider_id)


@router.post("/providers/{provider_id}/trae/logout")
async def trae_logout(provider_id: int, db: AsyncSession = Depends(get_db)):
    """退出登录：远端吊销 refresh token + 清 trae_auth + 清模型 key。"""
    provider = await _get_trae_provider(db, provider_id)
    from sqlalchemy import update

    from app.persistence.models.model_reg import Model

    auth = await trae_session.load_auth(db, provider_id)
    if auth is not None and auth.access_token:
        try:
            await trae_oauth.revoke_refresh_token(
                settings.trae_account_endpoint, auth.access_token,
                client_id=settings.trae_client_id,
                device_id=auth.device_id or "0",
                machine_id=auth.machine_id or "",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[trae] provider=%s 吊销远端 token 失败（忽略）: %s", provider_id, e)
    await trae_oauth.cancel_login(db, provider_id)
    await trae_session.clear_auth(db, provider_id)
    await db.execute(update(Model).where(Model.provider_id == provider_id).values(api_key=None))
    provider.auth_status = "pending"
    provider.account_label = None
    await db.flush()
    await db.commit()
    return {"ok": True}


@router.post("/providers/{provider_id}/trae/sync", response_model=TraeSyncOut)
async def trae_sync(provider_id: int, db: AsyncSession = Depends(get_db)):
    """同步远端模型目录（batch_get_detail_param → upsert Model 表）。"""
    provider = await _get_trae_provider(db, provider_id)
    agent_host = _agent_host(provider)
    try:
        from app.auth.trae.catalog import sync_trae_models
        entries = await sync_trae_models(db, provider, agent_host)
    except trae_session.TraeAuthError as e:
        if e.kind == "login_required":
            raise HTTPException(401, str(e)) from None
        raise HTTPException(502, str(e)) from None
    except Exception as e:  # noqa: BLE001
        logger.warning("[trae] provider=%s 目录同步失败: %s", provider_id, e)
        raise HTTPException(502, f"目录同步失败：{str(e)[:300]}") from None

    # 登录态刷新（目录同步成功即视为已登录）
    auth = await trae_session.load_auth(db, provider_id)
    if auth and auth.access_token:
        provider.auth_status = "logged_in"
        account = auth.account or {}
        label = account.get("label") or account.get("name") or account.get("user_id") or ""
        provider.account_label = str(label)[:80] or None
    await db.flush()
    await db.commit()
    return TraeSyncOut(synced=len(entries), models=entries)
