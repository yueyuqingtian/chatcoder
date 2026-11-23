"""antigravity（Google Antigravity）供应商专属路由：登录 / 状态 / 退出 / 目录同步。

方案 docs/plan-antigravity-provider-integration.md §6。

端点契约：
- POST /providers/{id}/antigravity/login/import  导入本机登录态（主路径，即时完成）
- POST /providers/{id}/antigravity/login/start   应用内独立 Google OAuth 登录（返回授权页 URL）
- POST /providers/{id}/antigravity/login/cancel  取消当前登录尝试
- GET  /providers/{id}/antigravity/login/status  查询登录状态（前端 1.5s 轮询）
- POST /providers/{id}/antigravity/logout        退出登录（清 antigravity_auth 与模型 key）
- POST /providers/{id}/antigravity/sync          同步远端模型目录 → Model 表

两条登录路径共用同一套落库逻辑（auth/antigravity/oauth.py 的 _finish_login），
区别只在凭据来源：本机文件 vs 浏览器授权码。
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.antigravity import oauth as ag_oauth
from app.auth.antigravity import session as ag_session
from app.auth.antigravity.token import AntigravityAuthError
from app.core.config import settings
from app.gateway.schemas import (
    AntigravityLoginImportOut,
    AntigravityLoginStartOut,
    AntigravityLoginStatusOut,
    AntigravityQuotaOut,
    AntigravitySyncOut,
)
from app.persistence.database import get_db
from app.services import provider_service
from app.services.credential_service import provider_client_kwargs

logger = logging.getLogger(__name__)
router = APIRouter()

# 进行中的登录尝试（按 provider_id）。同一供应商同时只允许一个登录流程，
# 新尝试会取消旧尝试（对齐 ta3 的「回调实例归属一次登录」）。
_login_flows: dict[int, ag_oauth.LoginFlow] = {}
# 登录结果（供 status 轮询；消费一次后不清理，便于前端多次读取直到刷新页面）
_login_results: dict[int, dict] = {}


async def _get_antigravity_provider(db: AsyncSession, provider_id: int):
    provider = await provider_service.get_provider(db, provider_id)
    if provider is None:
        raise HTTPException(404, "provider not found")
    if (provider.api_format or "openai").lower() != "antigravity":
        raise HTTPException(400, "该供应商不是 antigravity 类型")
    return provider


def _mark_provider_state(provider_id: int, *, status: str, label: str | None,
                         default_base_url: str | None = None) -> None:
    """回写供应商登录态。

    default_base_url 仅在库中为空时写入：网关地址留空虽可用（后端按默认推导），
    但界面显示空值会让用户困惑；登录/同步成功顺手补上，用户可随时改成别的环境。
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


async def _mark_logged_in(provider_id: int, label: str | None) -> None:
    from app.persistence.database import run_write_locked

    await run_write_locked(
        _mark_provider_state(
            provider_id, status="logged_in", label=label,
            default_base_url=settings.antigravity_gateway_endpoint,
        ),
        label=f"antigravity.login_state.{provider_id}",
    )


# ─────────────────────────────────────────────
# 导入本机登录态
# ─────────────────────────────────────────────
@router.post(
    "/providers/{provider_id}/antigravity/login/import",
    response_model=AntigravityLoginImportOut,
)
async def antigravity_login_import(provider_id: int, db: AsyncSession = Depends(get_db)):
    """导入本机 Antigravity 客户端的登录态（客户端已登录时即时成功）。"""
    await _get_antigravity_provider(db, provider_id)
    try:
        result = await ag_oauth.import_local_login(db, provider_id)
    except AntigravityAuthError as e:
        if e.kind == "login_required":
            return AntigravityLoginImportOut(ok=False, error=e.message)
        raise HTTPException(502, e.message)
    except Exception as e:  # noqa: BLE001
        logger.warning("[antigravity] provider=%s 导入登录态失败: %s", provider_id, e)
        raise HTTPException(502, f"导入 Antigravity 登录态失败：{str(e)[:300]}")

    account = result.get("account") or {}
    label = str(account.get("label") or account.get("email") or "")[:80] or None
    await _mark_logged_in(provider_id, label)
    return AntigravityLoginImportOut(
        ok=True,
        account=account.get("email") or label,
        project=result.get("project"),
        tier=result.get("tier"),
    )


# ─────────────────────────────────────────────
# 应用内独立登录
# ─────────────────────────────────────────────
@router.post(
    "/providers/{provider_id}/antigravity/login/start",
    response_model=AntigravityLoginStartOut,
)
async def antigravity_login_start(provider_id: int, db: AsyncSession = Depends(get_db)):
    """启动应用内 Google OAuth 登录，返回授权页 URL（由前端打开浏览器）。"""
    await _get_antigravity_provider(db, provider_id)

    old = _login_flows.pop(provider_id, None)
    if old is not None:
        # 同一供应商只允许一个登录流程，旧尝试一律作废
        old.cancel()
        try:
            await old.stop()
        except Exception:  # noqa: BLE001 - 清理失败不影响新流程
            pass

    flow = ag_oauth.LoginFlow()
    try:
        auth_url = await flow.start()
    except Exception as e:  # noqa: BLE001
        logger.warning("[antigravity] provider=%s 启动登录失败: %s", provider_id, e)
        raise HTTPException(502, f"启动 Antigravity 登录失败：{str(e)[:300]}")

    _login_flows[provider_id] = flow
    _login_results[provider_id] = {"status": "pending"}
    # 后台等待回调，完成即写结果（前端靠 status 轮询取结论）
    flow._task = asyncio.create_task(_await_callback(provider_id, flow))
    return AntigravityLoginStartOut(ok=True, auth_url=auth_url, state=flow.callback.state)


async def _await_callback(provider_id: int, flow: ag_oauth.LoginFlow) -> None:
    """等待浏览器回调并完成登录（后台任务，失败写 failed 结果）。"""
    try:
        callback_result = await flow.callback.wait()
        if callback_result is None:
            _login_results[provider_id] = {
                "status": "failed", "message": "登录超时，请重新发起登录",
            }
            return
        if callback_result.get("error"):
            _login_results[provider_id] = {
                "status": "cancelled", "message": str(callback_result["error"]),
            }
            return
        # 独立短会话：后台任务不在请求的 db 上下文里
        from app.persistence.database import async_session_factory

        async with async_session_factory() as db:
            result = await ag_oauth.complete_login(db, provider_id, flow, callback_result)
        account = result.get("account") or {}
        label = str(account.get("email") or account.get("label") or "")[:80] or None
        await _mark_logged_in(provider_id, label)
        _login_results[provider_id] = {
            "status": "success",
            "account": account.get("email") or label,
        }
    except AntigravityAuthError as e:
        _login_results[provider_id] = {"status": "failed", "message": e.message}
    except asyncio.CancelledError:
        _login_results[provider_id] = {"status": "cancelled", "message": "登录已取消"}
    except Exception as e:  # noqa: BLE001
        logger.warning("[antigravity] provider=%s 登录收尾失败: %s", provider_id, e)
        _login_results[provider_id] = {
            "status": "failed", "message": f"登录失败：{str(e)[:300]}",
        }
    finally:
        if _login_flows.get(provider_id) is flow:
            _login_flows.pop(provider_id, None)
        try:
            await flow.stop()
        except Exception:  # noqa: BLE001
            pass


@router.post("/providers/{provider_id}/antigravity/login/cancel")
async def antigravity_login_cancel(provider_id: int, db: AsyncSession = Depends(get_db)):
    """取消当前登录尝试。"""
    await _get_antigravity_provider(db, provider_id)
    flow = _login_flows.pop(provider_id, None)
    if flow is not None:
        flow.cancel()
        try:
            await flow.stop()
        except Exception:  # noqa: BLE001
            pass
    _login_results[provider_id] = {"status": "cancelled", "message": "登录已取消"}
    return {"ok": True}


@router.get(
    "/providers/{provider_id}/antigravity/login/status",
    response_model=AntigravityLoginStatusOut,
)
async def antigravity_login_status(provider_id: int, db: AsyncSession = Depends(get_db)):
    """查询登录状态。

    优先级：进行中/刚结束的登录结果 > 库内已有登录态 > idle。
    这样「登录成功后前端刷新」与「已登录时直接展示账号」两种场景都能覆盖。
    """
    await _get_antigravity_provider(db, provider_id)
    pending = _login_results.get(provider_id)
    if pending is not None:
        if pending.get("status") in ("success", "failed", "cancelled"):
            return AntigravityLoginStatusOut(
                status=str(pending.get("status")),
                message=pending.get("message"),
                account=pending.get("account"),
            )
        return AntigravityLoginStatusOut(status="pending")

    row = await ag_session.resolve_auth(db, provider_id)
    if row is not None and row.access_token:
        account = row.account if isinstance(row.account, dict) else {}
        return AntigravityLoginStatusOut(
            status="success",
            account=str(account.get("email") or account.get("label") or "") or None,
        )
    return AntigravityLoginStatusOut(status="idle")


# ─────────────────────────────────────────────
# 登出
# ─────────────────────────────────────────────
@router.post("/providers/{provider_id}/antigravity/logout")
async def antigravity_logout(provider_id: int, db: AsyncSession = Depends(get_db)):
    """退出登录：清 antigravity_auth、清模型 key、复位供应商登录态。"""
    from sqlalchemy import update

    from app.persistence.database import run_write_locked
    from app.persistence.models.antigravity_auth import AntigravityAuth
    from app.persistence.models.model_reg import Model

    await _get_antigravity_provider(db, provider_id)

    flow = _login_flows.pop(provider_id, None)
    if flow is not None:
        flow.cancel()
        try:
            await flow.stop()
        except Exception:  # noqa: BLE001
            pass
    _login_results.pop(provider_id, None)

    await ag_session.clear_auth(db, provider_id)

    def _p(s):
        from app.persistence.models.model_reg import Provider

        row = s.get(Provider, provider_id)
        if row is not None:
            row.auth_status = "pending"
            row.account_label = None
        s.execute(update(Model).where(Model.provider_id == provider_id).values(api_key=None))
        # 一并清掉残留的 auth 行（多账号时可能有带 credential_id 的行）
        s.query(AntigravityAuth).filter(
            AntigravityAuth.provider_id == provider_id,
        ).delete()
        s.commit()

    await run_write_locked(_p, label=f"antigravity.logout.{provider_id}")
    return {"ok": True}


# ─────────────────────────────────────────────
# 模型目录同步
# ─────────────────────────────────────────────
@router.post("/providers/{provider_id}/antigravity/sync", response_model=AntigravitySyncOut)
async def antigravity_sync(provider_id: int, db: AsyncSession = Depends(get_db)):
    """同步远端模型目录（fetchAvailableModels → upsert Model 表）。

    只落对话与图像模型，补全专用模型（tab_* 等）不进库——它们出现在
    models 里但选作对话模型必然失败。
    """
    provider = await _get_antigravity_provider(db, provider_id)
    row = await ag_session.resolve_auth(db, provider_id)
    if row is None or not row.access_token:
        raise HTTPException(401, "请先导入 Antigravity 登录态或登录账号")

    try:
        access_token = await ag_session.ensure_token(db, provider_id, provider)
    except AntigravityAuthError as e:
        if e.kind == "login_required":
            raise HTTPException(401, e.message)
        access_token = row.access_token

    project = row.project or settings.antigravity_default_project
    try:
        payload = await ag_oauth.fetch_available_models(
            access_token, project,
            base_url=getattr(provider, "base_url", None),
            client_kwargs=provider_client_kwargs(provider),
        )
    except AntigravityAuthError as e:
        logger.warning("[antigravity] provider=%s 模型目录拉取失败: %s", provider_id, e.message)
        raise HTTPException(502, e.message)
    except Exception as e:  # noqa: BLE001
        logger.warning("[antigravity] provider=%s 目录同步失败: %s", provider_id, e)
        raise HTTPException(502, f"目录同步失败：{str(e)[:300]}")

    from app.auth.antigravity.catalog import catalog_model_rows, parse_available_models

    parsed = parse_available_models(payload)
    rows = catalog_model_rows(parsed)
    # plan-20261123-ux-fixes：同步模型保留已有模型的上下文窗口、思考深度与多模态配置，新模型默认开启多模态
    model_ids = await provider_service.bulk_upsert_models(
        db,
        provider_id,
        rows,
        preserve_existing_config=True,
        default_multimodal=True,
    )
    added = len(model_ids)

    account = row.account if isinstance(row.account, dict) else {}
    label = str(account.get("email") or account.get("label") or "")[:80] or None
    await _mark_logged_in(provider_id, label)
    # 缓存一份远端清单，便于排障与后续配额面板复用
    await ag_session.save_auth(
        db, provider_id,
        access_token=row.access_token,
        refresh_token=row.refresh_token,
        id_token=row.id_token,
        project=project,
        tier=row.tier,
        scope=row.scope,
        auth_kind=row.auth_kind or "oauth",
        account=row.account,
        catalog={"chat": parsed.get("chat") or [], "image": parsed.get("image") or [],
                 "completion": parsed.get("completion") or []},
        expires_at=row.expires_at,
        credential_id=getattr(row, "credential_id", None),
    )
    return AntigravitySyncOut(ok=True, added=added, total=len(rows))


# ─────────────────────────────────────────────
# 配额查询
# ─────────────────────────────────────────────
@router.get(
    "/providers/{provider_id}/antigravity/quota",
    response_model=AntigravityQuotaOut,
)
async def antigravity_quota(
    provider_id: int,
    force: bool = False,
    credential_id: int | None = None,
    db: AsyncSession = Depends(get_db),
):
    """查询该账号的配额（分组限额 + 逐模型剩余）。

    主视图用 summary（按模型组聚合的周限额 / 5 小时限额），models 为展开明细。
    pools / model_windows 为多池与逐模型窗口视图。
    force=true 跳过 30s 缓存；credential_id 指定具体账号凭据。
    """
    from app.auth.antigravity import quota as ag_quota

    provider = await _get_antigravity_provider(db, provider_id)
    try:
        data = await ag_quota.get_quota(
            db, provider_id, provider, force=force,
            credential_id=credential_id,
            client_kwargs=provider_client_kwargs(provider),
        )
    except AntigravityAuthError as e:
        if e.kind == "login_required":
            return AntigravityQuotaOut(ok=False, error=e.message)
        raise HTTPException(502, e.message)
    except ag_quota.AntigravityQuotaError as e:
        logger.warning("[antigravity] provider=%s 配额查询失败: %s", provider_id, e)
        return AntigravityQuotaOut(ok=False, error=str(e))
    except Exception as e:  # noqa: BLE001
        logger.warning("[antigravity] provider=%s 配额查询异常: %s", provider_id, e)
        return AntigravityQuotaOut(ok=False, error=f"配额查询失败：{str(e)[:300]}")

    return AntigravityQuotaOut(
        ok=True,
        summary=data.get("summary"),
        models=data.get("models") or [],
        pools=data.get("pools") or [],
        model_windows=data.get("model_windows") or [],
        project=data.get("project"),
        tier=data.get("tier"),
        account=data.get("account"),
    )


__all__ = ["router"]
