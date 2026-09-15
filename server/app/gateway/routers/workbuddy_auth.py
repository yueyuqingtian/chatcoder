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
from app.persistence.database import commit_with_retry, get_db
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
        account = result.get("account") or {}
        label = account.get("label") or account.get("nickname") or account.get("id") or ""
        _label = str(label)[:80] or None
        from app.persistence.database import run_write_locked

        def _p(s):
            from app.persistence.models.model_reg import Provider
            row = s.get(Provider, provider_id)
            if row is not None:
                row.auth_status = "logged_in"
                row.account_label = _label
            s.commit()

        await run_write_locked(_p, label=f"workbuddy.login_state.{provider_id}")
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

    from app.persistence.database import run_write_locked

    def _p(s):
        from app.persistence.models.model_reg import Provider
        row = s.get(Provider, provider_id)
        if row is not None:
            row.auth_status = "pending"
            row.account_label = None
        s.execute(update(Model).where(Model.provider_id == provider_id).values(api_key=None))
        s.commit()

    await run_write_locked(_p, label=f"workbuddy.logout.{provider_id}")
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
        account = auth.account or {}
        label = account.get("label") or account.get("nickname") or account.get("id") or ""
        _label = str(label)[:80] or None
        from app.persistence.database import run_write_locked

        def _p(s):
            from app.persistence.models.model_reg import Provider
            row = s.get(Provider, provider_id)
            if row is not None:
                row.auth_status = "logged_in"
                row.account_label = _label
            s.commit()

        await run_write_locked(_p, label=f"workbuddy.sync_state.{provider_id}")
    return WorkBuddySyncOut(synced=len(entries), models=entries)


# ── plan-248-1258 M2.6: 积分余额与每日签到 ──


@router.get("/providers/{provider_id}/workbuddy/credits")
async def workbuddy_credits(provider_id: int, credential_id: int | None = None,
                           refresh: bool = False, db: AsyncSession = Depends(get_db)):
    """查询（或读取缓存）workbuddy 账号积分余额。

    - credential_id 为空：返回该供应商全部凭据的积分概览；
    - refresh=true 时实时拉取并写回凭据缓存（否则读缓存/首次拉取）。
    """
    provider = await _get_workbuddy_provider(db, provider_id)
    api_base = _resolve_api_base(provider)
    from app.auth.workbuddy import credits as wb_credits
    from app.auth.workbuddy import session as wb_session
    from app.services import credential_service

    creds = await credential_service.list_credentials(db, provider_id)
    if credential_id is not None:
        creds = [c for c in creds if c.id == credential_id]
    out: list[dict] = []
    # 兼容未创建 provider_credentials 的旧安装：直接使用 provider 级 auth 返回一条账号余额。
    if not creds:
        legacy_auth = await wb_session.load_auth(db, provider_id)
        if legacy_auth is not None and legacy_auth.access_token:
            credits_val = await wb_credits.fetch_credits(api_base, legacy_auth.access_token, legacy_auth.account or {})
            out.append({
                "credential_id": None,
                "label": (legacy_auth.account or {}).get("label") or (legacy_auth.account or {}).get("nickname") or "WorkBuddy 账号",
                "credits": credits_val,
                "account": legacy_auth.account or {},
                "logged_in": True,
            })
        return {"credentials": out}
    for c in creds:
        auth = None
        try:
            from app.models.registry import _load_auth_for_credential
            auth = await _load_auth_for_credential(db, "workbuddy", provider_id, c.id)
        except Exception:  # noqa: BLE001
            auth = None
        # 兼容旧版本：登录态可能仍挂在 provider_id、credential_id 为空的 auth 行。
        # 不能用 `and not creds` 判断，因为迁移后通常已经存在一条空/占位凭据。
        if auth is None or not auth.access_token:
            auth = await wb_session.load_auth(db, provider_id)
        if auth is None or not auth.access_token:
            out.append({"credential_id": c.id, "label": c.label, "credits": None,
                        "account": None, "logged_in": False})
            continue
        credits_val = float(c.credits) if c.credits is not None else None
        if refresh or credits_val is None:
            credits_val = await wb_credits.fetch_credits(api_base, auth.access_token, auth.account or {})
            if credits_val is not None:
                try:
                    await credential_service.set_credits(db, c.id, credits_val)
                except Exception:  # noqa: BLE001
                    logger.debug("[workbuddy] 积分缓存写入失败", exc_info=True)
        out.append({
            "credential_id": c.id,
            "label": c.label or (auth.account or {}).get("label"),
            "credits": credits_val,
            "account": auth.account or {},
            "logged_in": True,
        })
    return {"credentials": out}


@router.post("/providers/{provider_id}/workbuddy/checkin")
async def workbuddy_checkin(provider_id: int, credential_id: int | None = None,
                            db: AsyncSession = Depends(get_db)):
    """执行一次「Buddy 加油站」每日签到（按账号），并返回领取结果与新积分余额。"""
    provider = await _get_workbuddy_provider(db, provider_id)
    from app.auth.workbuddy import credits as wb_credits
    from app.services import credential_service

    creds = await credential_service.list_credentials(db, provider_id)
    if credential_id is not None:
        creds = [c for c in creds if c.id == credential_id]
    if not creds:
        # 未迁移出凭据的旧数据：用空凭据占位走 provider 级账号
        result = await wb_credits.claim_for_credential(db, provider, None)
        return {"results": [{"credential_id": None, **result}]}
    results = []
    for c in creds:
        try:
            r = await wb_credits.claim_for_credential(db, provider, c)
        except Exception as e:  # noqa: BLE001 - 单个账号失败不影响其他账号
            logger.warning("[workbuddy] 凭据 #%s 签到异常: %s", c.id, e)
            r = {"status": "error", "error": str(e)[:200]}
        results.append({"credential_id": c.id, "label": c.label, **r})
    return {"results": results}


