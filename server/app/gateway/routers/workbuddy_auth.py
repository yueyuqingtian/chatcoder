"""workbuddy（腾讯 CodeBuddy/WorkBuddy）供应商专属路由：登录 / 状态 / 退出 / 目录同步。

端点契约见方案 §5.5：
- POST /providers/{id}/workbuddy/login/start   启动浏览器登录（auth/state → 轮询 token）
- POST /providers/{id}/workbuddy/login/cancel  取消登录
- GET  /providers/{id}/workbuddy/login/status  查询登录状态
- POST /providers/{id}/workbuddy/logout        退出登录（可指定账号）
- POST /providers/{id}/workbuddy/sync          同步远端模型目录（/v3/config → Model 表）

plan-271-1364 M2.2/M2.3：多账号 —— 每次登录新建一条账号凭据并绑定 workbuddy_auth 行；
退出、积分、签到均支持按 credential_id 定位，不再跨账号回落。
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


async def _refresh_provider_login_state(db: AsyncSession, provider_id: int) -> None:
    """按当前账号情况重算 Provider 级登录态（auth_status / account_label）。

    plan-271-1364：账号级状态由凭据行承载，Provider 级字段仅作列表徽标摘要——
    只要有任一账号已登录就是 logged_in，否则 pending。
    """
    from app.persistence.database import run_write_locked
    from app.services import credential_service

    creds = await credential_service.list_credentials(db, provider_id)
    logged_label: str | None = None
    for c in creds:
        row = await workbuddy_session.load_auth(db, provider_id, c.id)
        if row is not None and row.access_token:
            account = row.account or {}
            logged_label = (c.label
                            or account.get("nickname")
                            or account.get("label")
                            or None)
            break
    if logged_label is None:
        # 兼容旧式 provider 级账号（无凭据行）
        legacy = await workbuddy_session.load_auth(db, provider_id)
        if legacy is not None and legacy.access_token:
            account = legacy.account or {}
            logged_label = account.get("nickname") or account.get("label") or None

    state = "logged_in" if logged_label else "pending"
    label = (str(logged_label)[:80] or None) if logged_label else None

    def _p(s):
        from app.persistence.models.model_reg import Provider
        row = s.get(Provider, provider_id)
        if row is not None:
            row.auth_status = state
            row.account_label = label
        s.commit()

    await run_write_locked(_p, label=f"workbuddy.login_state.{provider_id}")


@router.post("/providers/{provider_id}/workbuddy/login/start",
             response_model=WorkBuddyLoginStartOut)
async def workbuddy_login_start(provider_id: int, db: AsyncSession = Depends(get_db)):
    """登录：发起 auth/state → 返回 auth_url（前端用系统浏览器打开）+ 后台轮询。

    plan-271-1364：每次调用都视为登录一个新账号，成功后自动建一条凭据行。
    """
    provider = await _get_workbuddy_provider(db, provider_id)
    api_base = _resolve_api_base(provider)
    result = await workbuddy_oauth.start_login(db, provider_id, api_base)
    if result.get("status") == "logged_in":
        await _refresh_provider_login_state(db, provider_id)
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
async def workbuddy_logout(provider_id: int, credential_id: int | None = None,
                           db: AsyncSession = Depends(get_db)):
    """退出登录。

    plan-271-1364 M2.3：
    - 传 credential_id：只退该账号（清其 auth 行 + 删该凭据行），其他账号不受影响；
    - 不传：退出全部账号（清全部 auth 行 + 全部凭据行），兼容旧行为；
    两种情况都会重算 Provider 级登录态。
    """
    provider = await _get_workbuddy_provider(db, provider_id)
    from sqlalchemy import select, update

    from app.persistence.models.model_reg import Model
    from app.services import credential_service

    await workbuddy_oauth.cancel_login(db, provider_id)

    if credential_id is not None:
        # 单账号退出：删凭据行（delete_credential 内部已级联清 auth 行）
        await credential_service.delete_credential(db, credential_id)
    else:
        # 全部退出：先逐条清 auth 行（含旧式 provider 级行），再删全部凭据行
        creds = await credential_service.list_credentials(db, provider_id)
        for c in creds:
            await workbuddy_session.clear_auth(db, provider_id, c.id)
        await workbuddy_session.clear_auth(db, provider_id)
        for c in creds:
            await credential_service.delete_credential(db, c.id)

    await _refresh_provider_login_state(db, provider_id)

    # 登录态全部消失时清模型占位 key（保持原有语义：退出后模型不可用）
    if not await _has_any_logged_in(db, provider_id):
        from app.persistence.database import run_write_locked

        def _p(s):
            s.execute(update(Model).where(Model.provider_id == provider_id).values(api_key=None))
            s.commit()

        await run_write_locked(_p, label=f"workbuddy.logout.models.{provider_id}")
    return {"ok": True}


async def _has_any_logged_in(db: AsyncSession, provider_id: int) -> bool:
    from app.services import credential_service

    creds = await credential_service.list_credentials(db, provider_id)
    for c in creds:
        row = await workbuddy_session.load_auth(db, provider_id, c.id)
        if row is not None and row.access_token:
            return True
    legacy = await workbuddy_session.load_auth(db, provider_id)
    return bool(legacy is not None and legacy.access_token)


@router.post("/providers/{provider_id}/workbuddy/sync", response_model=WorkBuddySyncOut)
async def workbuddy_sync(provider_id: int, credential_id: int | None = None,
                         db: AsyncSession = Depends(get_db)):
    """同步远端模型目录（/v3/config → upsert Model 表）。

    plan-271-1364 M2.3：目录是供应商级，用指定账号（缺省取首个已登录账号）的 token；
    401 刷新也只针对该账号。
    """
    provider = await _get_workbuddy_provider(db, provider_id)
    api_base = _resolve_api_base(provider)
    # 未显式指定账号时，挑首个已登录账号，避免误用无登录态的凭据
    if credential_id is None:
        _c, _auth = await workbuddy_oauth.first_logged_in_auth(db, provider_id)
        if _c is not None:
            credential_id = _c.id
    try:
        from app.auth.workbuddy.catalog import sync_workbuddy_models
        entries = await sync_workbuddy_models(db, provider, api_base, credential_id)
    except workbuddy_session.WorkBuddyAuthError as e:
        if e.kind == "login_required":
            raise HTTPException(401, str(e)) from None
        raise HTTPException(502, str(e)) from None
    except Exception as e:  # noqa: BLE001
        logger.warning("[workbuddy] provider=%s 目录同步失败: %s", provider_id, e)
        raise HTTPException(502, f"目录同步失败：{str(e)[:300]}") from None

    # 登录态刷新（目录同步成功即视为已登录）
    await _refresh_provider_login_state(db, provider_id)
    return WorkBuddySyncOut(synced=len(entries), models=entries)


# ── plan-248-1258 M2.6: 积分余额与每日签到 ──


@router.get("/providers/{provider_id}/workbuddy/credits")
async def workbuddy_credits(provider_id: int, credential_id: int | None = None,
                           refresh: bool = False, db: AsyncSession = Depends(get_db)):
    """查询（或读取缓存）workbuddy 账号积分余额。

    - credential_id 为空：返回该供应商全部凭据的积分概览；
    - refresh=true 时实时拉取并写回凭据缓存（否则读缓存/首次拉取）。

    plan-271-1364 M2.3（修 D4）：按凭据取 auth 时**不再**跨账号回落，
    避免读到别的账号的积分归属。
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
        auth = await wb_session.load_auth(db, provider_id, c.id)
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
