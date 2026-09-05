"""ta3（Ta+3 牛码）供应商专属路由：登录 / 状态 / 退出 / 目录同步。

端点契约见方案 §5.8：
- POST /providers/{id}/ta3/login/start   启动浏览器 PKCE(SM3) 登录
- POST /providers/{id}/ta3/login/cancel  取消登录
- GET  /providers/{id}/ta3/login/status  查询登录状态
- POST /providers/{id}/ta3/logout        退出登录（清会话与模型 key）
- POST /providers/{id}/ta3/sync          同步远端模型目录 → Model 表
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.ta3 import oauth as ta3_oauth
from app.auth.ta3 import session as ta3_session
from app.auth.ta3.oauth import DEFAULT_TA3_API_BASE
from app.gateway.schemas import Ta3LoginStartOut, Ta3LoginStatusOut, Ta3SyncOut
from app.persistence.database import commit_with_retry, get_db
from app.services import provider_service

logger = logging.getLogger(__name__)
router = APIRouter()


def _resolve_api_base(provider) -> str:
    """ta3 服务端地址：Provider.base_url 缺省时用内置默认（用户无需手填）。"""
    return (provider.base_url or DEFAULT_TA3_API_BASE).rstrip("/")


async def _get_ta3_provider(db: AsyncSession, provider_id: int):
    provider = await provider_service.get_provider(db, provider_id)
    if provider is None:
        raise HTTPException(404, "provider not found")
    if (provider.api_format or "openai").lower() != "ta3":
        raise HTTPException(400, "该供应商不是 ta3 类型")
    return provider


@router.post("/providers/{provider_id}/ta3/login/start", response_model=Ta3LoginStartOut)
async def ta3_login_start(provider_id: int, db: AsyncSession = Depends(get_db)):
    """登录：优先银海通 IM 静默登录（本机有银海通时立即成功），失败降级浏览器 PKCE。

    返回 status=logged_in 表示已登录（前端直接同步模型）；pending 表示需打开浏览器。
    """
    provider = await _get_ta3_provider(db, provider_id)
    api_base = _resolve_api_base(provider)
    result = await ta3_oauth.start_login(db, provider_id, api_base)
    if result.get("status") == "logged_in":
        # 登录态立即回写供应商（IM 静默登录路径：前端无需打开浏览器即可见已登录）
        provider.auth_status = "logged_in"
        account = result.get("account") or {}
        provider.account_label = str(account.get("label") or account.get("id") or "")[:80] or None
        await db.flush()
        await commit_with_retry(db)
    return Ta3LoginStartOut(**result)


@router.post("/providers/{provider_id}/ta3/login/cancel")
async def ta3_login_cancel(provider_id: int, db: AsyncSession = Depends(get_db)):
    await _get_ta3_provider(db, provider_id)
    await ta3_oauth.cancel_login(db, provider_id)
    return {"ok": True}


@router.get("/providers/{provider_id}/ta3/login/status", response_model=Ta3LoginStatusOut)
async def ta3_login_status(provider_id: int, db: AsyncSession = Depends(get_db)):
    await _get_ta3_provider(db, provider_id)
    return await ta3_oauth.get_login_status(db, provider_id)


@router.post("/providers/{provider_id}/ta3/logout")
async def ta3_logout(provider_id: int, db: AsyncSession = Depends(get_db)):
    """退出登录：清 ta3_auth、清模型 llm-key、复位供应商登录态。"""
    provider = await _get_ta3_provider(db, provider_id)
    from sqlalchemy import select, update

    from app.persistence.models.model_reg import Model

    await ta3_session.clear_auth(db, provider_id)
    await db.execute(update(Model).where(Model.provider_id == provider_id).values(api_key=None))
    provider.auth_status = "pending"
    provider.account_label = None
    await db.flush()
    await commit_with_retry(db)
    return {"ok": True}


@router.post("/providers/{provider_id}/ta3/sync", response_model=Ta3SyncOut)
async def ta3_sync(provider_id: int, db: AsyncSession = Depends(get_db)):
    """同步远端模型目录（list-organizations + list-assistants → upsert Model 表）。"""
    provider = await _get_ta3_provider(db, provider_id)
    api_base = _resolve_api_base(provider)
    try:
        from app.auth.ta3.catalog import sync_ta3_models
        entries = await sync_ta3_models(db, provider, api_base)
    except ta3_session.Ta3AuthError as e:
        if e.kind == "login_required":
            raise HTTPException(401, str(e))
        raise HTTPException(502, str(e))
    except Exception as e:  # noqa: BLE001
        logger.warning("[ta3] provider=%s 目录同步失败: %s", provider_id, e)
        raise HTTPException(502, f"目录同步失败：{str(e)[:300]}")

    # 登录态刷新（目录同步成功即视为已登录）
    auth = await ta3_session.load_auth(db, provider_id)
    if auth and auth.access_token:
        provider.auth_status = "logged_in"
        account = auth.account or {}
        provider.account_label = str(account.get("label") or account.get("id") or "")[:80] or None
    await db.flush()
    await commit_with_retry(db)
    return Ta3SyncOut(synced=len(entries), models=entries)

