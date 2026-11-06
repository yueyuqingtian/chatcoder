"""ta3（Ta+3 牛码）供应商专属路由：登录 / 状态 / 退出 / 目录同步。

端点契约见方案 §5.8：
- POST /providers/{id}/ta3/login/start   启动浏览器 PKCE(S256) 登录
- POST /providers/{id}/ta3/login/cancel  取消登录
- GET  /providers/{id}/ta3/login/status  查询登录状态
- POST /providers/{id}/ta3/logout        退出登录（清会话与模型 key）
- POST /providers/{id}/ta3/sync          同步远端模型目录 → Model 表
"""
from __future__ import annotations

import logging
import httpx

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.ta3 import oauth as ta3_oauth
from app.auth.ta3 import quota as ta3_quota
from app.auth.ta3 import session as ta3_session
from app.auth.ta3 import endpoints
from app.gateway.schemas import Ta3LoginStartOut, Ta3LoginStatusOut, Ta3SyncOut
from app.persistence.database import get_db
from app.services import provider_service
from app.services.credential_service import provider_client_kwargs

logger = logging.getLogger(__name__)
router = APIRouter()


async def _resolve_api_base(provider) -> str:
    try:
        return await endpoints.resolve(provider)
    except httpx.HTTPError as exc:
        raise HTTPException(502, "TA3 服务地址暂不可达，请检查地址、代理或网络") from exc


async def _get_ta3_provider(db: AsyncSession, provider_id: int):
    provider = await provider_service.get_provider(db, provider_id)
    if provider is None:
        raise HTTPException(404, "provider not found")
    if (provider.api_format or "openai").lower() != "ta3":
        raise HTTPException(400, "该供应商不是 ta3 类型")
    return provider


@router.post("/providers/{provider_id}/ta3/login/start", response_model=Ta3LoginStartOut)
async def ta3_login_start(provider_id: int, db: AsyncSession = Depends(get_db)):
    """plan-20261106-ta3-080：浏览器授权完成后统一写入账号状态。"""
    provider = await _get_ta3_provider(db, provider_id)
    try:
        result = await ta3_oauth.start_login(db, provider_id, endpoints.current_base(provider))
    except ta3_session.Ta3AuthError as exc:
        raise HTTPException(401 if exc.kind == "login_required" else 502, str(exc)) from exc
    except (httpx.HTTPError, OSError) as exc:
        raise HTTPException(502, "无法启动 TA3 网页登录，请检查网络和本地回调端口") from exc
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
    """plan-20261106-ta3-080：先在本地退出，再尽力吊销远端 refresh token。"""
    provider = await _get_ta3_provider(db, provider_id)
    row = await ta3_session.load_auth(db, provider_id)
    refresh = row.refresh_token if row else None
    base = endpoints.current_base(provider)
    await ta3_oauth.cancel_login(db, provider_id)
    await ta3_session.clear_auth(db, provider_id)
    endpoints.reset(provider_id)
    await ta3_session.revoke(refresh, base, provider_client_kwargs(provider))
    return {"ok": True}


@router.post("/providers/{provider_id}/ta3/sync", response_model=Ta3SyncOut)
async def ta3_sync(provider_id: int, db: AsyncSession = Depends(get_db)):
    """同步远端模型目录（list-organizations + list-assistants → upsert Model 表）。"""
    provider = await _get_ta3_provider(db, provider_id)
    api_base = await _resolve_api_base(provider)
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


async def _revive_credentials_quietly(db: AsyncSession, provider_id: int, stage: str) -> None:
    """额度恢复后解除该供应商凭据的冷却（尽力而为，失败只记日志）。

    v47 修复：ta3 的凭据选取对冷却行是硬过滤，额度耗尽导致的冷却会一直挡在
    选取阶段，表现为「刷新了额度还是一条消息都发不出去」。刷新额度、透支、
    重置都是用户的显式恢复意图，因此这三条路径统一在这里解除冷却。
    """
    try:
        from app.services import credential_service

        revived = await credential_service.revive_provider_credentials_by_id(db, provider_id)
        if revived:
            logger.info("[ta3] provider=%s %s后已解除 %d 条凭据的冷却",
                        provider_id, stage, revived)
    except Exception:  # noqa: BLE001 - 解除冷却失败不影响主流程
        logger.warning("[ta3] provider=%s %s后解除凭据冷却失败(非阻塞)",
                       provider_id, stage, exc_info=True)


@router.get("/providers/{provider_id}/ta3/quota")
async def ta3_quota_get(provider_id: int, force: bool = False,
                        db: AsyncSession = Depends(get_db)):
    """本人额度（窗口百分比、重置时刻、可执行动作）；force=true 跳过 10s 缓存。

    v47 修复：force=true（用户在设置页点「刷新」，或额度动作完成后的回刷）同时解除
    该供应商凭据的冷却——此前刷新只清了额度展示缓存，凭据行的 status=cooldown 原样
    保留，而 ta3 的选取路径对冷却行是硬过滤，于是刷新之后并发消息仍然在本地直接
    报 provider_credential_unavailable（表现为"请求根本没发出去"）。刷新是用户的
    显式恢复意图，冷却只是"暂缓"，这里不保留任何时间门槛。
    """
    provider = await _get_ta3_provider(db, provider_id)
    api_base = await _resolve_api_base(provider)
    # plan-110-438 M2: 额度查询按该供应商的代理配置出网
    _ckw = provider_client_kwargs(provider)
    try:
        payload = await ta3_quota.get_quota(db, provider_id, api_base, force=force,
                                            client_kwargs=_ckw)
    except Exception as e:  # noqa: BLE001
        _raise_quota_http(e, provider_id, "额度查询")
    if force:
        # v47: 手动刷新是用户的显式恢复意图——顺手解除凭据冷却（见 _revive_credentials_quietly）
        await _revive_credentials_quietly(db, provider_id, "手动刷新额度")
    return payload


@router.get("/providers/{provider_id}/ta3/quota/trend")
async def ta3_quota_trend(provider_id: int, period: str | None = None,
                          start_date: str | None = None, end_date: str | None = None,
                          call_source: str | None = None,
                          db: AsyncSession = Depends(get_db)):
    """用量趋势（DAILY/MONTHLY；单区间 ≤366 天，超限服务端返回 400 range_too_large）。"""
    provider = await _get_ta3_provider(db, provider_id)
    api_base = await _resolve_api_base(provider)
    _ckw = provider_client_kwargs(provider)  # plan-110-438 M2
    try:
        return await ta3_quota.get_quota_trend(
            db, provider_id, api_base,
            period=period, start_date=start_date, end_date=end_date, call_source=call_source,
            client_kwargs=_ckw,
        )
    except Exception as e:  # noqa: BLE001
        _raise_quota_http(e, provider_id, "用量趋势查询")


@router.post("/providers/{provider_id}/ta3/quota/overdraft")
async def ta3_quota_overdraft(provider_id: int, body: Ta3QuotaRemarkBody,
                              db: AsyncSession = Depends(get_db)):
    """日窗透支（仅用户点击确认后调用；幂等性由服务端保证）。"""
    provider = await _get_ta3_provider(db, provider_id)
    api_base = await _resolve_api_base(provider)
    _ckw = provider_client_kwargs(provider)  # plan-110-438 M2
    try:
        result = await ta3_quota.request_overdraft(db, provider_id, api_base, body.remark,
                                                  client_kwargs=_ckw)
    except Exception as e:  # noqa: BLE001
        _raise_quota_http(e, provider_id, "日窗透支")
    # v47: 额度已恢复 → 一并解除凭据冷却，否则后续请求仍在本地被挡在选取阶段
    await _revive_credentials_quietly(db, provider_id, "日窗透支")
    return result


@router.post("/providers/{provider_id}/ta3/quota/reset")
async def ta3_quota_reset(provider_id: int, body: Ta3QuotaResetBody,
                          db: AsyncSession = Depends(get_db)):
    """周/月窗重置（返回 APPLIED 立即生效 / PENDING 转人工审批）。"""
    provider = await _get_ta3_provider(db, provider_id)
    api_base = await _resolve_api_base(provider)
    _ckw = provider_client_kwargs(provider)  # plan-110-438 M2
    try:
        result = await ta3_quota.request_reset(db, provider_id, api_base, body.window_type,
                                              body.remark, client_kwargs=_ckw)
    except Exception as e:  # noqa: BLE001
        _raise_quota_http(e, provider_id, "额度重置")
    # v47: 同上——重置成功即恢复额度，凭据冷却必须随之解除
    await _revive_credentials_quietly(db, provider_id, "额度重置")
    return result


@router.get("/providers/{provider_id}/ta3/model-status")
async def ta3_model_status(provider_id: int, model: str | None = None,
                           protocol: str | None = None, force: bool = False,
                           db: AsyncSession = Depends(get_db)):
    """模型状态卡（倍率/负载/可用性）；不带 model/protocol 时可缓存 30s。"""
    provider = await _get_ta3_provider(db, provider_id)
    api_base = await _resolve_api_base(provider)
    _ckw = provider_client_kwargs(provider)  # plan-110-438 M2
    try:
        return await ta3_quota.get_model_status(
            db, provider_id, api_base, model=model, protocol=protocol, force=force,
            client_kwargs=_ckw)
    except Exception as e:  # noqa: BLE001
        _raise_quota_http(e, provider_id, "模型状态查询")


@router.post("/providers/{provider_id}/ta3/admin-web")
async def ta3_admin_web(provider_id: int, db: AsyncSession = Depends(get_db)):
    """plan-20261106-ta3-080：管理平台自行处理浏览器登录，不在 URL 传递桌面凭据。"""
    provider = await _get_ta3_provider(db, provider_id)
    base = await _resolve_api_base(provider)
    try:
        url = await ta3_quota.build_admin_web_url(db, provider_id, base)
    except ta3_session.Ta3AuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    return {"ok": True, "url": url}
