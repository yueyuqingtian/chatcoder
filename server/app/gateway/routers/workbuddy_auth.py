"""workbuddy（腾讯 CodeBuddy/WorkBuddy）供应商专属路由：登录 / 状态 / 退出 / 目录同步。

端点契约见方案 §5.5：
- POST /providers/{id}/workbuddy/login/start   启动浏览器登录（auth/state → 轮询 token）
- POST /providers/{id}/workbuddy/login/cancel  取消登录
- GET  /providers/{id}/workbuddy/login/status  查询登录状态
- POST /providers/{id}/workbuddy/logout        退出登录（清会话与模型 key）
- POST /providers/{id}/workbuddy/sync          同步远端模型目录（/v3/config → Model 表）
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.workbuddy import oauth as workbuddy_oauth
from app.auth.workbuddy import session as workbuddy_session
from app.auth.workbuddy.oauth import DEFAULT_WORKBUDDY_API_BASE
from app.gateway.schemas import (
    WorkBuddyLoginStartOut,
    WorkBuddyLoginStatusOut,
    WorkBuddySyncOut,
)
from app.persistence.database import get_db
from app.services import provider_service

logger = logging.getLogger(__name__)
router = APIRouter()


def _resolve_api_base(provider) -> str:
    """workbuddy 服务端地址：Provider.base_url 缺省时用内置默认（用户无需手填）。"""
    return (provider.base_url or DEFAULT_WORKBUDDY_API_BASE).rstrip("/")


async def _get_workbuddy_provider(db: AsyncSession, provider_id: int):
    provider = await provider_service.get_provider(db, provider_id)
    if provider is None:
        raise HTTPException(404, "provider not found")
    if (provider.api_format or "openai").lower() != "workbuddy":
        raise HTTPException(400, "该供应商不是 workbuddy 类型")
    return provider


@router.post("/providers/{provider_id}/workbuddy/login/start",
             response_model=WorkBuddyLoginStartOut)
async def workbuddy_login_start(provider_id: int, db: AsyncSession = Depends(get_db)):
    """登录：发起 auth/state → 返回 auth_url（前端用系统浏览器打开）+ 后台轮询。"""
    provider = await _get_workbuddy_provider(db, provider_id)
    api_base = _resolve_api_base(provider)
    result = await workbuddy_oauth.start_login(db, provider_id, api_base)
    if result.get("status") == "logged_in":
        provider.auth_status = "logged_in"
        account = result.get("account") or {}
        label = account.get("label") or account.get("nickname") or account.get("id") or ""
        provider.account_label = str(label)[:80] or None
        await db.flush()
        await db.commit()
    return WorkBuddyLoginStartOut(**result)


@router.post("/providers/{provider_id}/workbuddy/login/cancel")
async def workbuddy_login_cancel(provider_id: int, db: AsyncSession = Depends(get_db)):
    await _get_workbuddy_provider(db, provider_id)
    await workbuddy_oauth.cancel_login(db, provider_id)
    return {"ok": True}


@router.get("/providers/{provider_id}/workbuddy/login/status",
            response_model=WorkBuddyLoginStatusOut)
async def workbuddy_login_status(provider_id: int, db: AsyncSession = Depends(get_db)):
    await _get_workbuddy_provider(db, provider_id)
    return await workbuddy_oauth.get_login_status(db, provider_id)


@router.post("/providers/{provider_id}/workbuddy/logout")
async def workbuddy_logout(provider_id: int, db: AsyncSession = Depends(get_db)):
    """退出登录：清 workbuddy_auth、清模型 llm-key、复位供应商登录态。"""
    provider = await _get_workbuddy_provider(db, provider_id)
    from sqlalchemy import update

    from app.persistence.models.model_reg import Model

    await workbuddy_oauth.cancel_login(db, provider_id)
    await workbuddy_session.clear_auth(db, provider_id)
    await db.execute(update(Model).where(Model.provider_id == provider_id).values(api_key=None))
    provider.auth_status = "pending"
    provider.account_label = None
    await db.flush()
    await db.commit()
    return {"ok": True}


@router.post("/providers/{provider_id}/workbuddy/sync", response_model=WorkBuddySyncOut)
async def workbuddy_sync(provider_id: int, db: AsyncSession = Depends(get_db)):
    """同步远端模型目录（/v3/config → upsert Model 表）。"""
    provider = await _get_workbuddy_provider(db, provider_id)
    api_base = _resolve_api_base(provider)
    try:
        from app.auth.workbuddy.catalog import sync_workbuddy_models
        entries = await sync_workbuddy_models(db, provider, api_base)
    except workbuddy_session.WorkBuddyAuthError as e:
        if e.kind == "login_required":
            raise HTTPException(401, str(e)) from None
        raise HTTPException(502, str(e)) from None
    except Exception as e:  # noqa: BLE001
        logger.warning("[workbuddy] provider=%s 目录同步失败: %s", provider_id, e)
        raise HTTPException(502, f"目录同步失败：{str(e)[:300]}") from None

    # 登录态刷新（目录同步成功即视为已登录）
    auth = await workbuddy_session.load_auth(db, provider_id)
    if auth and auth.access_token:
        provider.auth_status = "logged_in"
        account = auth.account or {}
        label = account.get("label") or account.get("nickname") or account.get("id") or ""
        provider.account_label = str(label)[:80] or None
    await db.flush()
    await db.commit()
    return WorkBuddySyncOut(synced=len(entries), models=entries)
