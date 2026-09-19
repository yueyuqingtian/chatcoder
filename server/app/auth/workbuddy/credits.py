"""plan-248-1258 M2.6: WorkBuddy 积分余额查询与每日签到。

端点来源（从本地安装 WorkBuddy 客户端 app.asar 逆向提取，src 注释明确标注）：

- POST {endpoint}/billing/meter/get-user-resource-summary   积分/套餐资源聚合
      → data.Packages[]{PackageCode, CycleTotalCapacity, CycleRemainCapacity,
        CycleUsedCapacity}（数值为字符串），总积分 = 各包 CycleRemainCapacity 之和
      （对齐客户端 parseResourceSummary + sumSummaryCapacity；resourcePrefix 为空，
       Web/Desktop 两端都走无前缀路径）
- POST {endpoint}/v2/billing/meter/get-enterprise-user-usage  企业账号回退
      → data.limit_num / data.used_num（limit_num=-1 表示不限量）
- POST {endpoint}/v2/billing/meter/checkin-activity-status  每日签到状态
- POST {endpoint}/v2/billing/meter/daily-checkin            执行每日签到

请求头：Authorization: Bearer {accessToken} + X-User-Id + X-Domain（+ 企业版
X-Enterprise-Id / X-Tenant-Id）；桌面端另可注入 X-Device-Token（图灵盾设备风控），
未取得时留空即可（服务端对其非强校验）。

注意：这是第三方客户端的私有计费接口，存在风控与契约变更风险。所有调用失败均
静默降级返回 None/错误字典，绝不抛出到会话主流程；签到按账号每日一次，失败限次。
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings

logger = logging.getLogger(__name__)

CHECKIN_STATUS_PATH = "/v2/billing/meter/checkin-activity-status"
DAILY_CHECKIN_PATH = "/v2/billing/meter/daily-checkin"
# 资源聚合接口：WorkBuddy 客户端 resourcePrefix 为空，Web/Desktop 都走无前缀路径
# （asar 源码注释明确「两端都走空前缀」）；/v2 前缀实测 404，仅作老网关兜底。
RESOURCE_SUMMARY_PATHS = (
    "/billing/meter/get-user-resource-summary",
    "/v2/billing/meter/get-user-resource-summary",
)
# 企业账号回退（客户端 getEnterpriseUsage 同款路径，需 X-Enterprise-Id 头）
ENTERPRISE_USAGE_PATH = "/v2/billing/meter/get-enterprise-user-usage"
_TIMEOUT = 12.0


def _root_base(api_base: str) -> str:
    """规范化 WorkBuddy 根地址，避免用户把 /v2 填入后出现 /v2/v2。"""
    base = api_base.rstrip("/")
    return base[:-3] if base.lower().endswith("/v2") else base


# 签到领取状态（对齐客户端 CheckinClaimStatus 枚举）
CLAIMED = "claimed"
ALREADY_CLAIMED = "already_claimed"
NOT_ELIGIBLE = "not_eligible"
EVENT_ENDED = "event_ended"


def _netloc(api_base: str) -> str:
    return urlparse(api_base).netloc or ""


def _headers(token: str, api_base: str, account: dict, device_token: str | None = None) -> dict:
    ua = getattr(settings, "workbuddy_user_agent", "") or "WorkBuddy/5.3.14 WorkBuddy/5.3.14 CLI/2.115.0"
    headers: dict = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "X-Domain": _netloc(api_base),
        "X-Product": "SaaS",
        "X-IDE-Type": "CLI",
        "X-IDE-Name": "workbuddy-desktop",
        "X-IDE-Version": "1.0.0",
        "User-Agent": ua,
    }
    uid = (account or {}).get("uid") or ""
    if uid:
        headers["X-User-Id"] = str(uid).strip()
    enterprise_id = (account or {}).get("enterpriseId") or ""
    if enterprise_id:
        headers["X-Enterprise-Id"] = str(enterprise_id).strip()
        headers["X-Tenant-Id"] = str(enterprise_id).strip()
    if device_token:
        headers["X-Device-Token"] = device_token
    return headers


async def _post_json(url: str, headers: dict) -> dict:
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers={"Accept-Encoding": "gzip, deflate"}) as client:
        resp = await client.post(url, json={}, headers=headers)
    body = {}
    try:
        body = resp.json() if resp.content else {}
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    return {"http_status": resp.status_code, "body": body}


def _extract_credits(body: dict) -> float | None:
    """从资源聚合响应提取可用积分（余额口径）。

    真实响应（实测 + asar parseResourceSummary/sumSummaryCapacity 对齐）：
        data.Packages[]{CycleTotalCapacity, CycleRemainCapacity, CycleUsedCapacity}
    数值为字符串；总积分 = 各包 CycleRemainCapacity 中正值之和。
    旧字段（resources[].left / usageLeft 等）保留兜底；企业账号走
    limit_num - used_num（见 fetch_credits 的企业回退分支）。
    """
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        return None

    def _sum_packages(packages: object) -> float | None:
        """对齐客户端 sumSummaryCapacity：累加各资源包余量（仅计正值，缺失按 0）。"""
        if not isinstance(packages, list) or not packages:
            return None
        total = 0.0
        seen = False
        for p in packages:
            if not isinstance(p, dict):
                continue
            raw = next((p.get(k) for k in ("CycleRemainCapacity", "CycleRemain", "cycleRemain")
                        if p.get(k) is not None), None)
            try:
                v = float(raw)
            except (TypeError, ValueError):
                continue
            if v > 0:
                total += v
                seen = True
        return round(total, 2) if seen else None

    # 首选：data.Packages（实测真实结构）；容错嵌套 data.data.Packages
    for candidate in (data, data.get("data")):
        if isinstance(candidate, dict):
            credits = _sum_packages(candidate.get("Packages"))
            if credits is not None:
                return credits

    # 兜底：老口径 resources[].left / usageLeft 等
    candidates: list[object] = [data]
    for key in ("data", "summary", "Summary", "userResource", "UserResource"):
        nested = data.get(key)
        if isinstance(nested, dict):
            candidates.append(nested)
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        resources = candidate.get("resources") or candidate.get("Resources") or candidate.get("resourceList")
        if isinstance(resources, list) and resources:
            total = 0.0
            seen = False
            for r in resources:
                if not isinstance(r, dict):
                    continue
                left = next((r.get(k) for k in ("left", "Left", "usageLeft", "UsageLeft", "remaining", "Remaining") if r.get(k) is not None), None)
                try:
                    total += float(left)
                    seen = True
                except (TypeError, ValueError):
                    continue
            if seen:
                return round(total, 2)
        for key in ("usageLeft", "UsageLeft", "credits", "Credits", "balance", "Balance", "left", "Left"):
            v = candidate.get(key)
            if v is not None:
                try:
                    return round(float(v), 2)
                except (TypeError, ValueError):
                    continue
    return None


async def _fetch_enterprise_credits(api_base: str, token: str, account: dict) -> float | None:
    """企业账号积分：POST /v2/billing/meter/get-enterprise-user-usage。

    响应 data.limit_num / data.used_num；limit_num=-1 表示不限量（返回 -1，
    前端按「不限」展示）。对齐客户端 getEnterpriseUsage。
    """
    enterprise_id = str((account or {}).get("enterpriseId") or "").strip()
    if not enterprise_id:
        return None
    url = f"{_root_base(api_base)}{ENTERPRISE_USAGE_PATH}"
    headers = _headers(token, api_base, account)
    headers["X-Enterprise-Id"] = enterprise_id
    headers["X-Tenant-Id"] = enterprise_id
    try:
        result = await _post_json(url, headers)
    except httpx.HTTPError as e:
        logger.warning("[workbuddy] 企业积分查询网络失败: %s", e)
        return None
    body = result.get("body") or {}
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    if result.get("http_status") != 200 or body.get("code") not in (0, "0"):
        return None
    try:
        limit = float(data.get("limit_num"))
        used = float(data.get("used_num") or 0)
    except (TypeError, ValueError):
        return None
    if limit == -1:
        return -1.0
    return round(max(0.0, limit - used), 2)


async def fetch_credits(api_base: str, token: str, account: dict) -> float | None:
    """查询账号可用积分余额；失败返回 None（不抛错）。"""
    # 企业账号优先走企业口径（个人聚合接口对企业账号不适用）
    if (account or {}).get("enterpriseId"):
        credits = await _fetch_enterprise_credits(api_base, token, account)
        if credits is not None:
            return credits

    last_result: dict | None = None
    for path in RESOURCE_SUMMARY_PATHS:
        url = f"{_root_base(api_base)}{path}"
        try:
            result = await _post_json(url, _headers(token, api_base, account))
        except httpx.HTTPError as e:
            logger.warning("[workbuddy] 积分查询网络失败 path=%s: %s", path, e)
            continue
        last_result = result
        body = result.get("body") or {}
        code = body.get("code")
        if result.get("http_status") in (200, 201) and code in (0, None, "0", "OK", "ok"):
            credits = _extract_credits(body)
            if credits is not None:
                return credits
    if last_result is not None:
        body = last_result.get("body") or {}
        logger.info("[workbuddy] 积分查询返回异常 http=%s code=%s",
                    last_result.get("http_status"), body.get("code"))
    return None


async def fetch_checkin_status(api_base: str, token: str, account: dict) -> dict | None:
    """查询每日签到状态（含活动信息/按钮文案）；失败返回 None。"""
    url = f"{_root_base(api_base)}{CHECKIN_STATUS_PATH}"
    try:
        result = await _post_json(url, _headers(token, api_base, account))
    except httpx.HTTPError as e:
        logger.warning("[workbuddy] 签到状态查询网络失败: %s", e)
        return None
    body = result.get("body") or {}
    if result.get("http_status") != 200 or body.get("code") != 0:
        return None
    data = body.get("data")
    return data if isinstance(data, dict) else None


async def daily_checkin(api_base: str, token: str, account: dict) -> dict:
    """执行每日签到。返回 {status, credit?, message?, error?}（失败不抛错）。"""
    url = f"{_root_base(api_base)}{DAILY_CHECKIN_PATH}"
    try:
        result = await _post_json(url, _headers(token, api_base, account))
    except httpx.HTTPError as e:
        return {"status": EVENT_ENDED, "error": f"网络失败：{e.__class__.__name__}"}
    body = result.get("body") or {}
    code = body.get("code")
    if code not in (0, None) or result.get("http_status") not in (200, 201):
        # 业务错误码 → 领取状态映射（对齐客户端 mapCheckinStatus）
        message = body.get("msg") or body.get("message") or f"http_{result.get('http_status')}"
        return {"status": ALREADY_CLAIMED if code in (1, "1") else NOT_ELIGIBLE,
                "message": str(message)[:200]}
    data = body.get("data")
    data = data if isinstance(data, dict) else {}
    credit = data.get("credit") or data.get("Credit")
    try:
        credit_val = float(credit) if credit is not None else None
    except (TypeError, ValueError):
        credit_val = None
    return {"status": CLAIMED, "credit": credit_val,
            "message": data.get("msg") or "签到成功"}


async def claim_for_credential(db: AsyncSession, provider, credential) -> dict:
    """按凭据（账号）执行一次签到，并刷新积分缓存。

    provider/credential 为 ORM 行；真实 token 取自该凭据关联的 workbuddy_auth。

    plan-271-1364 M2.3（修 D4）：credential 给定时**不再**回落到 provider 级账号，
    否则会拿别的账号的 token 去签到 / 查积分，导致归属错位。仅当 credential 为空
    （旧式未迁移数据）才读 provider 级旧行。
    """
    from app.auth.workbuddy import session as wb_session
    from app.services import credential_service

    api_base = (getattr(provider, "base_url", None)
                or "https://copilot.tencent.com").rstrip("/")
    if credential is not None:
        auth = await wb_session.load_auth(db, provider.id, credential.id)
    else:
        auth = await wb_session.load_auth(db, provider.id)
    if auth is None or not auth.access_token:
        return {"status": "login_required", "error": "账号未登录"}

    account = auth.account or {}
    result = await daily_checkin(api_base, auth.access_token, account)
    # 签到后顺手刷新积分余额（无论签到结果，账号额度都可能已变化）
    credits = await fetch_credits(api_base, auth.access_token, account)
    if credits is not None and credential is not None:
        try:
            await credential_service.set_credits(db, credential.id, credits)
        except Exception:  # noqa: BLE001
            logger.debug("[workbuddy] 积分缓存写入失败", exc_info=True)
    result["credits"] = credits
    return result
