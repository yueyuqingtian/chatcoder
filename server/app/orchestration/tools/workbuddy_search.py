"""web_search 的 WorkBuddy 云端引擎（复刻参考项目 CodeBuddy/WorkBuddy 的搜索实现）。

参考实现（WorkBuddy 桌面端 / CodeBuddy CLI 的 WebSearch 工具）走**云端网关**，而不是抓
搜索引擎的 HTML 结果页：

    POST {api_base}/agenttool/v1/search
      headers: Authorization: Bearer <accessToken>
               + X-Requested-With: XMLHttpRequest
               + User-Agent: <产品/版本>
               + 会话追踪头（requestId / conversationId / agentIntent / product / IDE 标识）
      body:    {"query": q, "type": "text2text", "max_results": N}
               （可选 allowed_domains / freshness）
      resp:    {provider, results: [{title, url, snippet, site, favicon}],
                total_results, response_time_ms}；带非空 code 即业务错误
      超时 20s。

为什么改用云端而不是继续抓 HTML：
  ① 结果经服务端清洗，噪音少，也不受出口 IP 的语言/地区影响；
  ② 不依赖页面结构（Bing/Google 改版即失效）与代理可用性；
  ③ 字段与参考项目的搜索输出（title/url/snippet）完全对齐。

凭据复用当前项目已登录的 WorkBuddy 账号（workbuddy_auth，多账号取首个已登录）；
401 时经 session.refresh_session 刷新一次后重试（对齐 catalog / credits 既有范式）。
未登录 / 网络异常一律返回 None —— 由调用方回退本地抓取引擎，保证搜索整体可用。
"""
from __future__ import annotations

import logging
import uuid
from urllib.parse import urlparse

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

# ── 参考实现常量（CodeBuddy CLI WebSearchTool）──
SEARCH_API_PATH = "/agenttool/v1/search"
SEARCH_TIMEOUT_S = 20.0
DEFAULT_API_BASE = "https://copilot.tencent.com"
_DEFAULT_UA = "WorkBuddy/5.3.14 WorkBuddy/5.3.14 CLI/2.115.0"


def _headers(token: str, api_base: str, account: dict) -> dict:
    """复刻参考实现的请求头（与 WorkBuddyProvider._base_headers 同口径）。"""
    request_id = uuid.uuid4().hex
    headers: dict = {
        "Accept": "application/json",
        "Content-Type": "application/json; charset=utf-8",
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent": getattr(settings, "workbuddy_user_agent", "") or _DEFAULT_UA,
        "Authorization": f"Bearer {token}",
        "X-Agent-Intent": "craft",
        "X-Conversation-ID": uuid.uuid4().hex,
        "X-Conversation-Request-ID": uuid.uuid4().hex,
        "X-Conversation-Message-ID": request_id,
        "X-Request-ID": request_id,
        "X-Product": "SaaS",
        "X-IDE-Type": "CLI",
        "X-IDE-Name": "workbuddy-desktop",
        "X-IDE-Version": "1.0.0",
        "X-Domain": urlparse(api_base).netloc,
    }
    uid = str(account.get("uid") or "")
    if uid:
        headers["X-User-Id"] = uid
    dept = str(account.get("departmentFullName") or "")
    if dept:
        headers["X-Department-Info"] = dept
    enterprise_id = str(account.get("enterpriseId") or "").strip()
    if enterprise_id:
        headers["X-Enterprise-Id"] = enterprise_id
        headers["X-Tenant-Id"] = enterprise_id
    return headers


def _normalize(data: dict) -> list[dict]:
    """云端结果 → 本项目统一结构 [{title, url, snippet}]。"""
    out: list[dict] = []
    for r in data.get("results") or []:
        if not isinstance(r, dict):
            continue
        url = str(r.get("url") or "").strip()
        if not url:
            continue
        out.append({
            "title": str(r.get("title") or url),
            "url": url,
            "snippet": str(r.get("snippet") or "").strip(),
        })
    return out


async def _post_search(api_base: str, token: str, account: dict,
                       query: str, max_results: int) -> tuple[int, list[dict] | None]:
    """发一次搜索请求。返回 (status_code, results|None)；status 0 表示网络层失败。"""
    url = f"{api_base.rstrip('/')}{SEARCH_API_PATH}"
    body = {
        "query": query,
        "type": "text2text",
        "max_results": max(1, min(15, max_results)),
    }
    try:
        async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT_S) as client:
            resp = await client.post(url, json=body, headers=_headers(token, api_base, account))
    except httpx.HTTPError as e:  # noqa: BLE001（超时/连接失败统一降级）
        logger.debug("[workbuddy-search] 请求失败：%s", e)
        return 0, None
    if resp.status_code != 200:
        return resp.status_code, None
    try:
        data = resp.json()
    except ValueError:
        return resp.status_code, None
    if not isinstance(data, dict) or data.get("code"):
        # 参考实现：带非空 code 即业务错误（额度/鉴权类）
        logger.debug("[workbuddy-search] 业务错误：%s", (data or {}).get("msg"))
        return resp.status_code, None
    return resp.status_code, _normalize(data)


async def _resolve_account(db) -> tuple[int, str, str, int | None, dict] | None:
    """取一个已登录的 WorkBuddy 账号。

    返回 (provider_id, api_base, token, credential_id, account)；无可用账号返回 None。
    多账号取「首个已登录」，与 catalog / credits 的 provider 级操作口径一致。
    """
    from sqlalchemy import select

    from app.auth.workbuddy import session as wb_session
    from app.persistence.models.model_reg import Provider
    from app.services import credential_service

    res = await db.execute(select(Provider).where(Provider.api_format == "workbuddy"))
    provider = next((p for p in res.scalars().all() if p.is_active), None)
    if provider is None:
        return None
    api_base = (getattr(provider, "base_url", None) or DEFAULT_API_BASE).rstrip("/")

    for c in await credential_service.list_credentials(db, provider.id):
        row = await wb_session.load_auth(db, provider.id, c.id)
        if row is not None and row.access_token:
            return provider.id, api_base, row.access_token, c.id, (row.account or {})
    # 旧式 provider 级单账号（未迁移数据）
    row = await wb_session.load_auth(db, provider.id)
    if row is not None and row.access_token:
        return provider.id, api_base, row.access_token, None, (row.account or {})
    return None


async def search_via_workbuddy(db, query: str, max_results: int) -> list[dict] | None:
    """经 WorkBuddy 网关搜索。不可用（未登录 / 网络 / 业务错误）返回 None，由调用方回退。"""
    if db is None:
        return None
    try:
        resolved = await _resolve_account(db)
    except Exception:  # noqa: BLE001（凭据读取失败不应让搜索整体失败）
        logger.debug("[workbuddy-search] 账号解析失败", exc_info=True)
        return None
    if resolved is None:
        return None

    provider_id, api_base, token, credential_id, account = resolved
    status, results = await _post_search(api_base, token, account, query, max_results)
    if status != 401:
        return results

    # 401 兜底：刷新一次后重试（对齐 catalog / credits 的既有刷新范式）
    try:
        from app.auth.workbuddy import session as wb_session

        token = await wb_session.refresh_session(db, provider_id, api_base, credential_id)
    except Exception:  # noqa: BLE001
        logger.debug("[workbuddy-search] token 刷新失败", exc_info=True)
        return None
    _, results = await _post_search(api_base, token, account, query, max_results)
    return results
