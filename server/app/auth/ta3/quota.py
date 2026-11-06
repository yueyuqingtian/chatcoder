"""ta3 额度服务（对齐参考项目 Ta+3 v0.4.6 quotaService.js）。

端点（业务接口契约：Authorization 标准 Bearer；Accept: application/json）：
- GET  {apiBase}/ai/v1/quota               本人额度（窗口百分比、重置时刻、可执行动作）
- GET  {apiBase}/ai/v1/quota/trend         用量趋势（DAILY/MONTHLY；单区间 ≤366 天）
- POST {apiBase}/ai/v1/quota/overdraft     日窗透支（form: remark；仅用户手动触发）
- POST {apiBase}/ai/v1/quota/reset         周/月窗重置（form: windowType+remark；APPLIED/PENDING）
- GET  {apiBase}/ai/v1/model-status        模型状态卡（倍率/负载/可用性）
- 管理平台入口：同主机 /newcoder/index.html（浏览器自行登录）

缓存对齐参考实现：quota 10s、model-status 30s（按 provider+token 失效，force 跳过）。
401 自动 refresh 后重试一次（复用 session.ensure_token，与 catalog 同步同款模式）。
"""
from __future__ import annotations

import logging
import time
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.ta3 import session as ta3_session
from app.auth.ta3 import endpoints
from app.persistence.models.model_reg import Provider

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 10.0
_QUOTA_CACHE_SECONDS = 10.0
_MODEL_STATUS_CACHE_SECONDS = 30.0

# 本地组织（对齐参考项目 LOCAL_ORG_ID）：非远端 app，不做 appId 透传
LOCAL_ORG_ID = "personal"


class Ta3QuotaError(RuntimeError):
    """额度服务错误。kind: network | timeout | http | parse"""

    def __init__(self, message: str, kind: str = "http"):
        super().__init__(message)
        self.kind = kind


# provider_id -> (token, at_monotonic, value)；换号/刷新后按 token 不匹配自然失效
_quota_cache: dict[int, tuple[str, float, dict]] = {}
_model_status_cache: dict[int, tuple[str, float, dict]] = {}


def clear_cache(provider_id: int) -> None:
    """清 provider 的额度缓存（登出/退出登录时调用，防残留展示）。"""
    _quota_cache.pop(provider_id, None)
    _model_status_cache.pop(provider_id, None)


def _headers(token: str, *, form: bool = False) -> dict:
    """业务接口鉴权头：Authorization 标准 Bearer；GET 不带 Content-Type。"""
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    if form:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    return headers


async def _current_token(db: AsyncSession, provider_id: int) -> str:
    provider = await db.get(Provider, provider_id, populate_existing=True)
    if provider is None:
        raise ta3_session.Ta3AuthError("供应商已删除")
    return await ta3_session.ensure_token(db, provider_id, endpoints.current_base(provider))


async def _cache_identity(db, provider_id, token):
    provider = await db.get(Provider, provider_id, populate_existing=True)
    auth = await ta3_session.load_auth(db, provider_id)
    return (token, endpoints.current_base(provider, "quota"), auth.session_generation if auth else None,
            str((auth.account or {}).get("id") or "") if auth else "")


async def _request_json(db: AsyncSession, provider_id: int, api_base: str, path: str, *,
                        method: str = "GET", form: dict | None = None,
                        token: str | None = None, timeout: float = _REQUEST_TIMEOUT,
                        client_kwargs: dict | None = None) -> dict | list:
    provider = await db.get(Provider, provider_id, populate_existing=True)
    if provider is None:
        raise ta3_session.Ta3AuthError("供应商已删除")
    token = token or await _current_token(db, provider_id)
    auth = await ta3_session.load_auth(db, provider_id)
    gen = auth.session_generation if auth else None
    account = str((auth.account or {}).get("id") or "") if auth else ""
    async def send(value):
        kwargs = {"headers": _headers(value, form=form is not None)}
        if form is not None:
            kwargs["content"] = urlencode(form)
        return await endpoints.request(provider, method, path, timeout=timeout, client_kwargs=client_kwargs,
                                       endpoint="quota", **kwargs)
    try:
        response = await send(token)
        if response.status_code == 401:
            fresh = await ta3_session.ensure_token(db, provider_id, api_base, failed_token=token)
            current = await ta3_session.load_auth(db, provider_id)
            if not current or current.session_generation != gen or str((current.account or {}).get("id") or "") != account:
                raise ta3_session.Ta3AuthError("账号已变化，请重新操作")
            response = await send(fresh)
        if response.status_code == 401:
            raise ta3_session.Ta3AuthError("登录验证失败，请重新网页登录")
        if not response.is_success:
            raise Ta3QuotaError(f"额度服务请求失败：HTTP {response.status_code}")
        payload = response.json()
        if not isinstance(payload, (dict, list)):
            raise ValueError("无效 JSON")
        return payload
    except httpx.TimeoutException as exc:
        raise Ta3QuotaError("额度请求超时；写操作结果可能未知，请先刷新额度确认", "timeout") from exc
    except httpx.HTTPError as exc:
        raise Ta3QuotaError("额度服务暂不可达，请检查网络", "network") from exc
    except ValueError as exc:
        raise Ta3QuotaError("额度服务返回了无法解析的数据", "parse") from exc



def _as_payload(value: dict | list) -> dict:
    """统一成 dict 返回：裸数组包装为 {"data": [...]}（服务端两种形态都出现过）。"""
    return value if isinstance(value, dict) else {"data": value}


# ─────────────────────────── 额度 / 模型状态 ───────────────────────────


async def get_quota(db: AsyncSession, provider_id: int, api_base: str, *,
                    force: bool = False, client_kwargs: dict | None = None) -> dict:
    """本人额度（窗口百分比、重置时刻、可执行动作）。身份即本人，不传 userId。"""
    token = await _current_token(db, provider_id)
    identity = await _cache_identity(db, provider_id, token)
    cached = _quota_cache.get(provider_id)
    if (not force and cached and cached[0] == identity
            and time.monotonic() - cached[1] < _QUOTA_CACHE_SECONDS):
        return cached[2]
    value = _as_payload(await _request_json(db, provider_id, api_base, "/ai/v1/quota",
                                           token=token, client_kwargs=client_kwargs))
    _quota_cache[provider_id] = (await _cache_identity(db, provider_id, token), time.monotonic(), value)
    return value


async def get_quota_trend(db: AsyncSession, provider_id: int, api_base: str, *,
                          period: str | None = None, start_date: str | None = None,
                          end_date: str | None = None, call_source: str | None = None,
                          client_kwargs: dict | None = None) -> dict:
    """用量趋势。只允许 DAILY / MONTHLY（不传 HOURLY）；区间校验交给服务端。"""
    params: dict[str, str] = {}
    if period in ("DAILY", "MONTHLY"):
        params["period"] = period
    if start_date:
        params["startDate"] = str(start_date)
    if end_date:
        params["endDate"] = str(end_date)
    if call_source in ("APP", "CLI", "PLUGIN"):
        params["callSource"] = str(call_source)
    qs = urlencode(params)
    value = await _request_json(
        db, provider_id, api_base, f"/ai/v1/quota/trend{('?' + qs) if qs else ''}",
        client_kwargs=client_kwargs)
    return _as_payload(value)


async def request_overdraft(db: AsyncSession, provider_id: int, api_base: str,
                            remark: str = "", client_kwargs: dict | None = None) -> dict:
    """日窗透支。只能由用户点击确认后调用（幂等性由服务端保证）。"""
    form = {"remark": remark} if remark else {}
    value = await _request_json(db, provider_id, api_base, "/ai/v1/quota/overdraft",
                                method="POST", form=form, client_kwargs=client_kwargs)
    clear_cache(provider_id)
    return _as_payload(value)


async def request_reset(db: AsyncSession, provider_id: int, api_base: str,
                        window_type: str, remark: str = "",
                        client_kwargs: dict | None = None) -> dict:
    """周/月窗重置。同样只能由用户手动触发；返回 APPLIED（立即生效）/ PENDING（转人工审批）。"""
    form = {"windowType": "MONTHLY" if window_type == "MONTHLY" else "WEEKLY"}
    if remark:
        form["remark"] = remark
    value = await _request_json(db, provider_id, api_base, "/ai/v1/quota/reset",
                                method="POST", form=form, client_kwargs=client_kwargs)
    clear_cache(provider_id)
    return _as_payload(value)


async def get_model_status(db: AsyncSession, provider_id: int, api_base: str, *,
                           model: str | None = None, protocol: str | None = None,
                           force: bool = False, client_kwargs: dict | None = None) -> dict:
    """模型状态卡（倍率 / 负载 / 可用性）。三端公共契约，用于模型列表与错峰提示。"""
    token = await _current_token(db, provider_id)
    cacheable = not model and not protocol
    identity = await _cache_identity(db, provider_id, token)
    cached = _model_status_cache.get(provider_id)
    if (not force and cacheable and cached and cached[0] == identity
            and time.monotonic() - cached[1] < _MODEL_STATUS_CACHE_SECONDS):
        return cached[2]
    params: dict[str, str] = {}
    if model:
        params["model"] = str(model)
    if protocol in ("OPENAI", "ANTHROPIC"):
        params["protocol"] = str(protocol)
    qs = urlencode(params)
    value = _as_payload(await _request_json(
        db, provider_id, api_base, f"/ai/v1/model-status{('?' + qs) if qs else ''}",
        token=token, client_kwargs=client_kwargs))
    if cacheable:
        _model_status_cache[provider_id] = (await _cache_identity(db, provider_id, token), time.monotonic(), value)
    return value


# ─────────────────────────── 后台网页 SSO 链接 ───────────────────────────


def _front_base(api_base: str) -> str:
    return endpoints.front_url(api_base)


async def resolve_app_id(db: AsyncSession, provider_id: int) -> str:
    """appId = 目录选中的组织 ID（本地组织 'personal' 不透传）。

    对齐参考项目 resolveAppId(rawSessionInfo)：catalog.selectedOrgId；
    兼容旧缓存（catalog 未存 selectedOrgId 时）回落任一 ta3 模型 meta.orgId。
    """
    row = await ta3_session.load_auth(db, provider_id)
    catalog = (row.catalog if row else None) or {}
    app_id = str(catalog.get("selectedOrgId") or "").strip()
    if app_id and app_id != LOCAL_ORG_ID:
        return app_id
    if app_id == LOCAL_ORG_ID:
        return ""
    from app.persistence.models.model_reg import Model

    res = await db.execute(
        select(Model).where(Model.provider_id == provider_id, Model.api_format == "ta3").limit(1))
    m = res.scalars().first()
    meta = (m.ta3_meta if m is not None else None) or {}
    app_id = str(meta.get("orgId") or "").strip()
    return app_id if app_id and app_id != LOCAL_ORG_ID else ""


async def build_admin_web_url(db: AsyncSession, provider_id: int, api_base: str,
                              client_kwargs: dict | None = None) -> str:
    """plan-20261106-ta3-080：管理平台独立处理浏览器会话，不再生成 SSO token。"""
    if not await ta3_session.get_access_token(db, provider_id):
        raise ta3_session.Ta3AuthError("请先网页登录后再打开管理平台")
    return endpoints.front_url(api_base)
