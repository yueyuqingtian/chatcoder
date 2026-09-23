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
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.ta3 import oauth as ta3_oauth
from app.auth.ta3 import quota as ta3_quota
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
        account = result.get("account") or {}
        _label = str(account.get("label") or account.get("id") or "")[:80] or None
        from app.persistence.database import run_write_locked

        def _p(s):
            from app.persistence.models.model_reg import Provider
            row = s.get(Provider, provider_id)
            if row is not None:
                row.auth_status = "logged_in"
                row.account_label = _label
            s.commit()

        await run_write_locked(_p, label=f"ta3.login_state.{provider_id}")
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
    from sqlalchemy import update

    from app.persistence.database import run_write_locked
    from app.persistence.models.model_reg import Model

    await ta3_session.clear_auth(db, provider_id)
    ta3_quota.clear_cache(provider_id)  # 登出即清额度缓存，防退出后残留展示

    def _p(s):
        from app.persistence.models.model_reg import Provider
        row = s.get(Provider, provider_id)
        if row is not None:
            row.auth_status = "pending"
            row.account_label = None
        s.execute(update(Model).where(Model.provider_id == provider_id).values(api_key=None))
        s.commit()

    await run_write_locked(_p, label=f"ta3.logout.{provider_id}")
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
        account = auth.account or {}
        _label = str(account.get("label") or account.get("id") or "")[:80] or None
        from app.persistence.database import run_write_locked

        def _p(s):
            from app.persistence.models.model_reg import Provider
            row = s.get(Provider, provider_id)
            if row is not None:
                row.auth_status = "logged_in"
                row.account_label = _label
            s.commit()

        await run_write_locked(_p, label=f"ta3.sync_state.{provider_id}")
    return Ta3SyncOut(synced=len(entries), models=entries)


# ── 额度与用量 / 模型状态 / 后台网页（对齐 Ta+3 v0.4.6 quotaService）──


class Ta3QuotaRemarkBody(BaseModel):
    remark: str = ""


class Ta3QuotaResetBody(BaseModel):
    window_type: str = "WEEKLY"
    remark: str = ""


def _raise_quota_http(e: Exception, provider_id: int, stage: str) -> None:
    """额度类错误 → HTTP 映射：登录问题 401、其余 502；未知异常原样重抛。"""
    if isinstance(e, ta3_session.Ta3AuthError):
        if e.kind == "login_required":
            raise HTTPException(401, str(e))
        raise HTTPException(502, str(e))
    if isinstance(e, ta3_quota.Ta3QuotaError):
        logger.warning("[ta3] provider=%s %s失败: %s", provider_id, stage, e)
        raise HTTPException(502, str(e))
    raise


@router.get("/providers/{provider_id}/ta3/quota")
async def ta3_quota_get(provider_id: int, force: bool = False,
                        db: AsyncSession = Depends(get_db)):
    """本人额度（窗口百分比、重置时刻、可执行动作）；force=true 跳过 10s 缓存。"""
    provider = await _get_ta3_provider(db, provider_id)
    api_base = _resolve_api_base(provider)
    try:
        return await ta3_quota.get_quota(db, provider_id, api_base, force=force)
    except Exception as e:  # noqa: BLE001
        _raise_quota_http(e, provider_id, "额度查询")


@router.get("/providers/{provider_id}/ta3/quota/trend")
async def ta3_quota_trend(provider_id: int, period: str | None = None,
                          start_date: str | None = None, end_date: str | None = None,
                          call_source: str | None = None,
                          db: AsyncSession = Depends(get_db)):
    """用量趋势（DAILY/MONTHLY；单区间 ≤366 天，超限服务端返回 400 range_too_large）。"""
    provider = await _get_ta3_provider(db, provider_id)
    api_base = _resolve_api_base(provider)
    try:
        return await ta3_quota.get_quota_trend(
            db, provider_id, api_base,
            period=period, start_date=start_date, end_date=end_date, call_source=call_source,
        )
    except Exception as e:  # noqa: BLE001
        _raise_quota_http(e, provider_id, "用量趋势查询")


@router.post("/providers/{provider_id}/ta3/quota/overdraft")
async def ta3_quota_overdraft(provider_id: int, body: Ta3QuotaRemarkBody,
                              db: AsyncSession = Depends(get_db)):
    """日窗透支（仅用户点击确认后调用；幂等性由服务端保证）。"""
    provider = await _get_ta3_provider(db, provider_id)
    api_base = _resolve_api_base(provider)
    try:
        return await ta3_quota.request_overdraft(db, provider_id, api_base, body.remark)
    except Exception as e:  # noqa: BLE001
        _raise_quota_http(e, provider_id, "日窗透支")


@router.post("/providers/{provider_id}/ta3/quota/reset")
async def ta3_quota_reset(provider_id: int, body: Ta3QuotaResetBody,
                          db: AsyncSession = Depends(get_db)):
    """周/月窗重置（返回 APPLIED 立即生效 / PENDING 转人工审批）。"""
    provider = await _get_ta3_provider(db, provider_id)
    api_base = _resolve_api_base(provider)
    try:
        return await ta3_quota.request_reset(db, provider_id, api_base, body.window_type, body.remark)
    except Exception as e:  # noqa: BLE001
        _raise_quota_http(e, provider_id, "额度重置")


@router.get("/providers/{provider_id}/ta3/model-status")
async def ta3_model_status(provider_id: int, model: str | None = None,
                           protocol: str | None = None, force: bool = False,
                           db: AsyncSession = Depends(get_db)):
    """模型状态卡（倍率/负载/可用性）；不带 model/protocol 时可缓存 30s。"""
    provider = await _get_ta3_provider(db, provider_id)
    api_base = _resolve_api_base(provider)
    try:
        return await ta3_quota.get_model_status(
            db, provider_id, api_base, model=model, protocol=protocol, force=force)
    except Exception as e:  # noqa: BLE001
        _raise_quota_http(e, provider_id, "模型状态查询")


async def _silent_relogin(db: AsyncSession, provider_id: int, api_base: str) -> bool:
    """静默重登录（v36 plan-321-1600 R3）：先无感刷新 token；无 refresh_token 或刷新
    失败时再走一次登录流程（IM 静默登录可用时立即成功）。失败不抛错。

    返回是否拿到新的登录态（供调用方判断是否值得重试）。
    """
    refreshed = False
    try:
        row = await ta3_session.load_auth(db, provider_id)
        if row is not None and row.refresh_token:
            await ta3_session.ensure_token(db, provider_id, api_base)
            refreshed = True
    except Exception as e:  # noqa: BLE001
        logger.info("[ta3] provider=%s token 刷新失败(%s)，降级尝试静默登录", provider_id, e)

    if refreshed:
        ta3_quota.clear_cache(provider_id)
        return True

    try:
        result = await ta3_oauth.start_login(db, provider_id, api_base)
    except Exception:  # noqa: BLE001
        logger.warning("[ta3] provider=%s 静默重登录失败(非阻塞)", provider_id, exc_info=True)
        return False

    ta3_quota.clear_cache(provider_id)
    if result.get("status") != "logged_in":
        # 需要浏览器 PKCE 的场合不在静默重试里拉起浏览器，交给用户手动登录
        logger.info("[ta3] provider=%s 静默重登录未完成(status=%s)", provider_id, result.get("status"))
        return False

    # 登录态回写供应商（与 login/start 同口径）
    account = result.get("account") or {}
    _label = str(account.get("label") or account.get("id") or "")[:80] or None
    from app.persistence.database import run_write_locked

    def _p(s):
        from app.persistence.models.model_reg import Provider
        row = s.get(Provider, provider_id)
        if row is not None:
            row.auth_status = "logged_in"
            row.account_label = _label
        s.commit()

    await run_write_locked(_p, label=f"ta3.relogin_state.{provider_id}")
    return True


@router.post("/providers/{provider_id}/ta3/admin-web")
async def ta3_admin_web(provider_id: int, db: AsyncSession = Depends(get_db)):
    """生成「后台网页」SSO 免登链接（进入网页查看剩余额度）；前端用系统浏览器打开。

    v36 (plan-321-1600 R3): 偶发失败（登录态过期/服务端抖动）时**先不报错**——
    静默重登录一次并重试一次；仍失败才把错误抛给前端（用户要求的重试口径）。
    """
    provider = await _get_ta3_provider(db, provider_id)
    api_base = _resolve_api_base(provider)
    try:
        url = await ta3_quota.build_admin_web_url(db, provider_id, api_base)
    except Exception as first_err:  # noqa: BLE001
        logger.info(
            "[ta3] provider=%s 后台网页链接生成失败，静默重登录后重试: %s", provider_id, first_err,
        )
        await _silent_relogin(db, provider_id, api_base)
        try:
            url = await ta3_quota.build_admin_web_url(db, provider_id, api_base)
        except Exception as e:  # noqa: BLE001
            _raise_quota_http(e, provider_id, "后台网页链接生成")
    return {"ok": True, "url": url}

