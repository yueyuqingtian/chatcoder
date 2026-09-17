"""ta3 额度服务（对齐参考项目 Ta+3 v0.4.6 quotaService.js）。

端点（业务接口契约：Authorization 裸 token、不带 Bearer；Accept: application/json）：
- GET  {apiBase}/ai/v1/quota               本人额度（窗口百分比、重置时刻、可执行动作）
- GET  {apiBase}/ai/v1/quota/trend         用量趋势（DAILY/MONTHLY；单区间 ≤366 天）
- POST {apiBase}/ai/v1/quota/overdraft     日窗透支（form: remark；仅用户手动触发）
- POST {apiBase}/ai/v1/quota/reset         周/月窗重置（form: windowType+remark；APPLIED/PENDING）
- GET  {apiBase}/ai/v1/model-status        模型状态卡（倍率/负载/可用性）
- POST {apiBase}/api/ai-sso/generate-token 后台网页 SSO 免登 token（form: loginId+appId）

缓存对齐参考实现：quota 10s、model-status 30s（按 provider+token 失效，force 跳过）。
401 自动 refresh 后重试一次（复用 session.ensure_token，与 catalog 同步同款模式）。
"""
from __future__ import annotations

import logging
import time
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.ta3 import session as ta3_session

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 10.0
_SSO_TIMEOUT = 8.0  # 对齐参考项目 fetchFirstJson(…, '生成后台登录 token', 8000)
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
    """业务接口鉴权头：Authorization 裸值（不带 Bearer）；GET 不带 Content-Type。"""
    headers = {"Accept": "application/json", "Authorization": token}
    if form:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    return headers


async def _current_token(db: AsyncSession, provider_id: int) -> str:
    token = await ta3_session.get_access_token(db, provider_id)
    if not token:
        raise ta3_session.Ta3AuthError("请先登录 Ta+3 账号", "login_required")
    return token


async def _request_json(db: AsyncSession, provider_id: int, api_base: str, path: str, *,
                        method: str = "GET", form: dict | None = None,
                        token: str | None = None, timeout: float = _REQUEST_TIMEOUT) -> dict | list:
    """发一次额度请求；401 时 refresh（ensure_token）后重试一次。

    - token 参数用于复用调用方已读取的 token（避免重复查库），缺省时自行读取。
    - 网络/超时/HTTP/解析错误统一转 Ta3QuotaError（认证失败保持 Ta3AuthError）。
    """
    url = f"{api_base.rstrip('/')}{path}"
    if token is None:
        token = await _current_token(db, provider_id)

    async def _do(t: str) -> httpx.Response:
        async with httpx.AsyncClient(timeout=timeout) as client:
            if form is None:
                return await client.request(method, url, headers=_headers(t))
            return await client.request(method, url, content=urlencode(form),
                                        headers=_headers(t, form=True))

    try:
        resp = await _do(token)
    except httpx.TimeoutException as e:
        raise Ta3QuotaError("额度服务请求超时", "timeout") from e
    except httpx.HTTPError as e:
        raise Ta3QuotaError("无法连接额度服务，请检查网络", "network") from e

    if resp.status_code == 401:
        # 刷新后重试一次；IM 静默登录（无 refresh_token）会原样返回旧 token，
        # 仍 401 则判定登录过期（对齐 catalog 同步的 401 重试模式）。
        token = await ta3_session.ensure_token(db, provider_id, api_base)
        try:
            resp = await _do(token)
        except httpx.TimeoutException as e:
            raise Ta3QuotaError("额度服务请求超时", "timeout") from e
        except httpx.HTTPError as e:
            raise Ta3QuotaError("无法连接额度服务，请检查网络", "network") from e
        if resp.status_code == 401:
            raise ta3_session.Ta3AuthError("登录已过期，请重新登录 Ta+3 账号", "login_required")

    if not resp.is_success:
        raise Ta3QuotaError(f"额度服务请求失败：HTTP {resp.status_code}", "http")
    try:
        payload = resp.json() if resp.content else {}
    except ValueError as e:
        raise Ta3QuotaError("额度服务返回了无法解析的数据", "parse") from e
    return payload


def _as_payload(value: dict | list) -> dict:
    """统一成 dict 返回：裸数组包装为 {"data": [...]}（服务端两种形态都出现过）。"""
    return value if isinstance(value, dict) else {"data": value}


# ─────────────────────────── 额度 / 模型状态 ───────────────────────────


async def get_quota(db: AsyncSession, provider_id: int, api_base: str, *,
                    force: bool = False) -> dict:
    """本人额度（窗口百分比、重置时刻、可执行动作）。身份即本人，不传 userId。"""
    token = await _current_token(db, provider_id)
    cached = _quota_cache.get(provider_id)
    if (not force and cached and cached[0] == token
            and time.monotonic() - cached[1] < _QUOTA_CACHE_SECONDS):
        return cached[2]
    value = _as_payload(await _request_json(db, provider_id, api_base, "/ai/v1/quota", token=token))
    _quota_cache[provider_id] = (token, time.monotonic(), value)
    return value


async def get_quota_trend(db: AsyncSession, provider_id: int, api_base: str, *,
                          period: str | None = None, start_date: str | None = None,
                          end_date: str | None = None, call_source: str | None = None) -> dict:
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
        db, provider_id, api_base, f"/ai/v1/quota/trend{('?' + qs) if qs else ''}")
    return _as_payload(value)


async def request_overdraft(db: AsyncSession, provider_id: int, api_base: str,
                            remark: str = "") -> dict:
    """日窗透支。只能由用户点击确认后调用（幂等性由服务端保证）。"""
    form = {"remark": remark} if remark else {}
    value = await _request_json(db, provider_id, api_base, "/ai/v1/quota/overdraft",
                                method="POST", form=form)
    clear_cache(provider_id)
    return _as_payload(value)


async def request_reset(db: AsyncSession, provider_id: int, api_base: str,
                        window_type: str, remark: str = "") -> dict:
    """周/月窗重置。同样只能由用户手动触发；返回 APPLIED（立即生效）/ PENDING（转人工审批）。"""
    form = {"windowType": "MONTHLY" if window_type == "MONTHLY" else "WEEKLY"}
    if remark:
        form["remark"] = remark
    value = await _request_json(db, provider_id, api_base, "/ai/v1/quota/reset",
                                method="POST", form=form)
    clear_cache(provider_id)
    return _as_payload(value)


async def get_model_status(db: AsyncSession, provider_id: int, api_base: str, *,
                           model: str | None = None, protocol: str | None = None,
                           force: bool = False) -> dict:
    """模型状态卡（倍率 / 负载 / 可用性）。三端公共契约，用于模型列表与错峰提示。"""
    token = await _current_token(db, provider_id)
    cacheable = not model and not protocol
    cached = _model_status_cache.get(provider_id)
    if (not force and cacheable and cached and cached[0] == token
            and time.monotonic() - cached[1] < _MODEL_STATUS_CACHE_SECONDS):
        return cached[2]
    params: dict[str, str] = {}
    if model:
        params["model"] = str(model)
    if protocol in ("OPENAI", "ANTHROPIC"):
        params["protocol"] = str(protocol)
    qs = urlencode(params)
    value = _as_payload(await _request_json(
        db, provider_id, api_base, f"/ai/v1/model-status{('?' + qs) if qs else ''}", token=token))
    if cacheable:
        _model_status_cache[provider_id] = (token, time.monotonic(), value)
    return value


# ─────────────────────────── 后台网页 SSO 链接 ───────────────────────────


def _front_base(api_base: str) -> str:
    """由 apiBase 推导前台地址（对齐 getYinhaiFrontBase：{host}{contextPath}-front）。

    例：https://lc.yinhaiyun.com/newcoder → https://lc.yinhaiyun.com/newcoder-front
    本地开发端口替换（8081→8080）为参考项目专用逻辑，当前项目不涉及。
    """
    parts = urlsplit(api_base)
    path = (parts.path or "").rstrip("/")
    if not path:
        return api_base.rstrip("/")
    seg = path.rsplit("/", 1)[-1]
    front_path = path if seg.endswith("-front") else f"{path}-front"
    return urlunsplit((parts.scheme, parts.netloc, front_path, "", ""))


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


async def build_admin_web_url(db: AsyncSession, provider_id: int, api_base: str) -> str:
    """生成「后台网页」SSO 免登链接（对齐 openYinhaiAdminSystem）。

    流程：POST {apiBase}/api/ai-sso/generate-token（form: loginId[+appId]）
    → 打开 {frontBase}/aiSsoLogin.html?token=…&redirect={frontBase}/。
    """
    row = await ta3_session.load_auth(db, provider_id)
    account = (row.account if row else None) or {}
    login_id = str(account.get("loginId") or account.get("id") or "").strip()
    if not login_id:
        raise Ta3QuotaError("账号信息缺少 loginId，请重新登录 Ta+3 账号", "http")

    form: dict[str, str] = {"loginId": login_id}
    app_id = await resolve_app_id(db, provider_id)
    if app_id:
        form["appId"] = app_id

    body = await _request_json(db, provider_id, api_base, "/api/ai-sso/generate-token",
                               method="POST", form=form, timeout=_SSO_TIMEOUT)
    data = body.get("data") if isinstance(body, dict) else None
    sso_token = ""
    if isinstance(data, dict):
        sso_token = str(data.get("token") or "").strip()
    if not sso_token and isinstance(body, dict):
        sso_token = str(body.get("token") or "").strip()
    if not sso_token:
        raise Ta3QuotaError("生成后台登录 token 失败：服务未返回 token", "http")

    front_base = _front_base(api_base)
    query = urlencode({"token": sso_token, "redirect": f"{front_base}/"})
    return f"{front_base}/aiSsoLogin.html?{query}"
