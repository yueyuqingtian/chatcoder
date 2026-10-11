"""kiro（AWS Kiro）供应商专属路由：登录 / 状态 / 退出 / 目录同步 / 积分。

端点契约：
- POST /providers/{id}/kiro/login/start   导入本机 Kiro 登录态
- POST /providers/{id}/kiro/login/cancel  取消登录（本机导入为即时完成，占位保持契约）
- GET  /providers/{id}/kiro/login/status  查询登录状态
- POST /providers/{id}/kiro/logout        退出登录（清 kiro_auth 与模型 key）
- POST /providers/{id}/kiro/sync          同步模型目录 → Model 表
- GET  /providers/{id}/kiro/quota         剩余积分（GetUsageLimits）
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.kiro import oauth as kiro_oauth
from app.auth.kiro import quota as kiro_quota
from app.auth.kiro import session as kiro_session
from app.gateway.schemas import KiroLoginStartOut, KiroLoginStatusOut, KiroSyncOut
from app.persistence.database import get_db
from app.services import provider_service
from app.services.credential_service import provider_client_kwargs

logger = logging.getLogger(__name__)
router = APIRouter()


async def _get_kiro_provider(db: AsyncSession, provider_id: int):
    provider = await provider_service.get_provider(db, provider_id)
    if provider is None:
        raise HTTPException(404, "provider not found")
    if (provider.api_format or "openai").lower() != "kiro":
        raise HTTPException(400, "该供应商不是 kiro 类型")
    return provider


def _mark_provider_state(provider_id: int, *, status: str, label: str | None,
                         default_base_url: str | None = None) -> None:
    """回写供应商登录态。

    plan-116-456：default_base_url 仅在库中为空时写入——Kiro 端点由区域推导，
    Base URL 留空虽可用，但界面显示空值会让用户困惑（实测反馈），「测试连接」
    等通用路径也据此判未配置。登录/同步成功顺手补上，用户可随时改成别的区域。
    """
    from app.persistence.models.model_reg import Provider

    def patch(s):
        row = s.get(Provider, provider_id)
        if row is not None:
            row.auth_status = status
            row.account_label = label
            if default_base_url and not (row.base_url or "").strip():
                row.base_url = default_base_url
        s.commit()

    return patch


@router.post("/providers/{provider_id}/kiro/login/start", response_model=KiroLoginStartOut)
async def kiro_login_start(provider_id: int, db: AsyncSession = Depends(get_db)):
    """导入本机 Kiro 登录态（Kiro IDE/CLI 已登录时即时成功）。"""
    await _get_kiro_provider(db, provider_id)
    try:
        result = await kiro_oauth.import_local_login(db, provider_id)
    except kiro_session.KiroAuthError as e:
        if e.kind == "login_required":
            return KiroLoginStartOut(status="failed", error=str(e))
        raise HTTPException(502, str(e))
    except Exception as e:  # noqa: BLE001
        logger.warning("[kiro] provider=%s 导入登录态失败: %s", provider_id, e)
        raise HTTPException(502, f"导入 Kiro 登录态失败：{str(e)[:300]}")

    account = result.get("account") or {}
    label = str(account.get("label") or account.get("id") or "")[:80] or None
    from app.auth.kiro.token import normalize_region
    from app.persistence.database import run_write_locked

    _region = normalize_region(result.get("region"))
    await run_write_locked(
        _mark_provider_state(provider_id, status="logged_in", label=label,
                             default_base_url=f"https://q.{_region}.amazonaws.com"),
        label=f"kiro.login_state.{provider_id}",
    )
    return KiroLoginStartOut(status="logged_in", account=account, region=result.get("region"))


@router.post("/providers/{provider_id}/kiro/login/cancel")
async def kiro_login_cancel(provider_id: int, db: AsyncSession = Depends(get_db)):
    """取消登录：本机导入为即时完成，无需后台轮询，占位返回以保持前端契约一致。"""
    await _get_kiro_provider(db, provider_id)
    return {"ok": True}


@router.get("/providers/{provider_id}/kiro/login/status", response_model=KiroLoginStatusOut)
async def kiro_login_status(provider_id: int, db: AsyncSession = Depends(get_db)):
    await _get_kiro_provider(db, provider_id)
    row = await kiro_session.load_auth(db, provider_id)
    if row is None or not row.access_token:
        return KiroLoginStatusOut(status="pending")
    account = row.account or {}
    return KiroLoginStatusOut(status="logged_in", account=account, region=row.region)


@router.post("/providers/{provider_id}/kiro/logout")
async def kiro_logout(provider_id: int, db: AsyncSession = Depends(get_db)):
    """退出登录：清 kiro_auth、清模型 key、复位供应商登录态。"""
    from sqlalchemy import update

    from app.persistence.database import run_write_locked
    from app.persistence.models.model_reg import Model

    await _get_kiro_provider(db, provider_id)
    await kiro_session.clear_auth(db, provider_id)
    kiro_quota.clear_cache(provider_id)

    def _p(s):
        from app.persistence.models.model_reg import Provider
        from app.persistence.models.kiro_auth import KiroAuth

        row = s.get(Provider, provider_id)
        if row is not None:
            row.auth_status = "pending"
            row.account_label = None
        s.execute(update(Model).where(Model.provider_id == provider_id).values(api_key=None))
        # 一并清掉残留的 auth 行（多账号时可能有带 credential_id 的行）
        s.query(KiroAuth).filter(KiroAuth.provider_id == provider_id).delete()
        s.commit()

    await run_write_locked(_p, label=f"kiro.logout.{provider_id}")
    return {"ok": True}


@router.post("/providers/{provider_id}/kiro/sync", response_model=KiroSyncOut)
async def kiro_sync(provider_id: int, db: AsyncSession = Depends(get_db)):
    """同步模型目录（内置全量映射 ∪ 服务端探测列表 → upsert Model 表）。"""
    provider = await _get_kiro_provider(db, provider_id)
    row = await kiro_session.load_auth(db, provider_id)
    if row is None or not row.access_token:
        raise HTTPException(401, "请先登录 Kiro 账号")

    try:
        from app.auth.kiro.catalog import sync_kiro_models

        entries = await sync_kiro_models(db, provider)
    except Exception as e:  # noqa: BLE001
        logger.warning("[kiro] provider=%s 目录同步失败: %s", provider_id, e)
        raise HTTPException(502, f"目录同步失败：{str(e)[:300]}")

    account = row.account or {}
    label = str(account.get("label") or account.get("id") or "")[:80] or None
    from app.auth.kiro.token import normalize_region
    from app.persistence.database import run_write_locked

    _region = normalize_region(row.region)
    await run_write_locked(
        _mark_provider_state(provider_id, status="logged_in", label=label,
                             default_base_url=f"https://q.{_region}.amazonaws.com"),
        label=f"kiro.sync_state.{provider_id}",
    )
    models = [{"name": e["name"], "model_id": e["model_id"]} for e in entries]
    return KiroSyncOut(synced=len(entries), models=models)


@router.get("/providers/{provider_id}/kiro/quota")
async def kiro_quota_get(provider_id: int, force: bool = False,
                         db: AsyncSession = Depends(get_db)):
    """剩余积分（GetUsageLimits）；force=true 跳过 10s 缓存。"""
    provider = await _get_kiro_provider(db, provider_id)
    _ckw = provider_client_kwargs(provider)
    try:
        return await kiro_quota.get_usage_limits(db, provider_id, provider,
                                                 force=force, client_kwargs=_ckw)
    except kiro_session.KiroAuthError as e:
        if e.kind == "login_required":
            raise HTTPException(401, str(e))
        raise HTTPException(502, str(e))
    except kiro_quota.KiroQuotaError as e:
        logger.warning("[kiro] provider=%s 积分查询失败: %s", provider_id, e)
        raise HTTPException(502, str(e))
