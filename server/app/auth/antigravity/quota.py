"""antigravity 配额查询（方案 §6 阶段四）。

两个端点（均已实测可用）：

- `POST /v1internal:retrieveUserQuotaSummary`：按模型组聚合，返回周限额与 5 小时限额。
  这是界面主视图——它把「Gemini 系」与「Claude/GPT 系」分成两组，每组共享
  一个周限额和一个 5 小时限额（配额按 token 成本比例消耗）。
- `POST /v1internal:retrieveUserQuota`：逐模型剩余比例（remainingFraction），
  用于展开明细。

两者都接受 `{"project": "..."}`，实测传 `{}` 也能返回，但显式带 project 更稳妥。

缓存 30s（按 provider + token 失效；force 跳过）——配额变化不快，避免频繁打网关。
"""
from __future__ import annotations

import logging
import time

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.antigravity.catalog import display_name
from app.auth.antigravity.token import AntigravityAuthError, build_headers, resolve_endpoints

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 20.0
_CACHE_SECONDS = 30.0

# (provider_id, credential_id) -> (token_fingerprint, at_monotonic, value)
_cache: dict[tuple[int, int | None], tuple[str, float, dict]] = {}


class AntigravityQuotaError(RuntimeError):
    """配额服务错误。kind: network | http | parse

    `status_code` 只在 kind=http 时有值，供调用方判断是否值得刷新令牌重试——
    此前靠 `"HTTP 401" in 错误文案` 字符串匹配，既脆弱又依赖文案格式。
    """

    def __init__(self, message: str, kind: str = "http", status_code: int | None = None):
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.status_code = status_code


def clear_cache(provider_id: int, credential_id: int | None = None) -> None:
    """清配额缓存（登出/退出登录时调用，防残留展示）。

    未传 credential_id 时清除该 provider 下全部缓存（含兼容裸 int key）；
    传入时仅清除对应的 `(provider_id, credential_id)` 条目。
    """
    if credential_id is not None:
        _cache.pop((provider_id, credential_id), None)
        return
    keys = [
        k for k in _cache
        if (not isinstance(k, bool) and k == provider_id)
        or (isinstance(k, tuple) and len(k) > 0 and k[0] == provider_id)
    ]
    for k in keys:
        _cache.pop(k, None)


def _as_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fingerprint(token: str) -> str:
    """令牌指纹（不落明文）：换账号/刷新后缓存自然失效。"""
    return f"{len(token)}:{token[-8:]}" if token else ""


def normalize_summary(body: dict) -> dict:
    """把 retrieveUserQuotaSummary 响应规范化为前端友好的分组视图。

    分组结构（实测）：
    {"groups": [{"displayName": "Gemini Models",
                 "description": "Models within this group: ...",
                 "buckets": [{"bucketId": "gemini-weekly", "displayName": "...",
                              "window": "weekly", "resetTime": "...",
                              "description": "...", "remainingFraction": 0.95}]}],
     "description": "..."}
    """
    groups: list[dict] = []
    raw_groups = body.get("groups") if isinstance(body, dict) else None
    for group in (raw_groups or []):
        if not isinstance(group, dict):
            continue
        buckets: list[dict] = []
        for bucket in (group.get("buckets") or []):
            if not isinstance(bucket, dict):
                continue
            fraction = _as_float(bucket.get("remainingFraction"))
            buckets.append({
                "bucket_id": str(bucket.get("bucketId") or ""),
                "display_name": str(bucket.get("displayName") or ""),
                "window": str(bucket.get("window") or ""),
                "reset_time": bucket.get("resetTime"),
                "description": bucket.get("description"),
                # 剩余百分比（0-100，保留一位小数）；无法解析时为 None
                "remaining_percent": None if fraction is None else round(fraction * 100, 1),
            })
        groups.append({
            "display_name": str(group.get("displayName") or ""),
            "description": group.get("description"),
            "buckets": buckets,
        })
    return {
        "groups": groups,
        "description": body.get("description") if isinstance(body, dict) else None,
    }


def normalize_models(body: dict) -> list[dict]:
    """把 retrieveUserQuota 响应规范化为逐模型剩余量列表。

    实测：{"buckets": [{"resetTime": "...", "tokenType": "WTUS",
                        "modelId": "gemini-3-flash", "remainingFraction": 1}]}
    """
    out: list[dict] = []
    raw = body.get("buckets") if isinstance(body, dict) else None
    for bucket in (raw or []):
        if not isinstance(bucket, dict):
            continue
        model_id = str(bucket.get("modelId") or "")
        if not model_id:
            continue
        fraction = _as_float(bucket.get("remainingFraction"))
        out.append({
            "model_id": model_id,
            "token_type": str(bucket.get("tokenType") or ""),
            "reset_time": bucket.get("resetTime"),
            "remaining_percent": None if fraction is None else round(fraction * 100, 1),
        })
    out.sort(key=lambda item: item["model_id"])
    return out


# ---- 额度池（照搬 magpie 插件的三层关联逻辑） ----------------------------------
#
# 背景（plan-antigravity-provider-integration §11.9）：服务端的 retrieveUserQuotaSummary
# 只给「组」这一层声明，而界面上要展示的池可能比组多——例如某模型读数与它所属组对不上，
# 就说明它是独立池（用户实测的「Gemini 4 Argon」池就是服务端没给组、由客户端本地造出来的）。
# 因此池不能直接透传 groups，必须重建：
#
#   1. 按名字挂池：池名带数字（Gemini 3）时，模型名以池名开头即归属；
#   2. 按 family + 读数：池名不带数字、组有 5 小时桶、组描述含该模型的 family
#      （Gemini / Claude / GPT-OSS），且模型读数与该组的某个桶一致；
#   3. 本地造池：语义属于该组但读数全对不上 → 用模型名单独造一个池。
#
# 这样池的数量完全由数据决定，不再假设「固定两个」。

_FIVE = 5 * 3600
_WEEK = 7 * 24 * 3600


def _pool_name(display_name: str) -> str:
    """组名归一：去掉尾部 " models"，" and " 换成 " & "。

    "Gemini Models" → "Gemini"，"Claude and GPT models" → "Claude & GPT"。
    """
    name = str(display_name or "").strip()
    if name.lower().endswith(" models"):
        name = name[:-7].strip()
    return name.replace(" and ", " & ")


def _versioned(pool: str) -> bool:
    """池名含数字即「版本化池」（如 "Gemini 3"），只允许按名字精确匹配。"""
    return any(ch.isdigit() for ch in pool)


def _pool_names(pool: str, model_name: str, model_id: str) -> bool:
    """版本化池的名字匹配：模型名/模型 id（连字符转空格）以池名开头。"""
    if not _versioned(pool):
        return False
    key = pool.lower()
    for raw in (model_name, str(model_id or "").replace("-", " ")):
        text = str(raw or "").lower()
        if text == key or text.startswith(key + " ") or text.startswith(key + "("):
            return True
    return False


def _vendor_of(model_id: str, model_name: str) -> str:
    """模型的 family 词，用于与组的描述文本比对。"""
    low = str(model_id or "").lower()
    if low.startswith("gemini"):
        return "Gemini"
    if low.startswith("claude"):
        return "Claude"
    if low.startswith("gpt-oss"):
        return "GPT-OSS"
    head = str(model_name or "").strip().split(" ")
    if head and head[0]:
        return head[0]
    return low.split("-")[0]


def _same_reading(used: float, resets_at: str | None,
                  pool_used: float, pool_resets_at: str | None) -> bool:
    """读数一致判定：
    - 双方都有重置时间且相差 < 60s（同一次窗口周期）时，容忍 ≤3.0pp 偏差——
      retrieveUserQuotaSummary 与 retrieveUserQuota 为两次独立 RPC，实测同池模型间常有
      0.8~1.5pp 的统计/取整滞后（例如 Gemini 5h 78.6% vs 逐模型 79.4%）；
    - 缺省重置时间时仅靠百分比比对，保持严格的 ≤0.5pp 阈值避免误合并。
    """
    if resets_at and pool_resets_at:
        try:
            delta = _parse_iso(resets_at) - _parse_iso(pool_resets_at)
        except (TypeError, ValueError):
            return abs(used - pool_used) <= 0.5
        if not (-60_000 < delta < 60_000):
            return False
        return abs(used - pool_used) <= 3.0
    return abs(used - pool_used) <= 0.5


def _parse_iso(value: str) -> float:
    """ISO 时间转毫秒时间戳（容错：解析失败抛 ValueError，由调用方按「不比较」处理）。"""
    from datetime import datetime

    text = str(value).replace("Z", "+00:00")
    return datetime.fromisoformat(text).timestamp() * 1000


def _model_pool_name(model_name: str) -> str:
    """造池用的名字：模型显示名去掉末尾的 " (…)" 档位括号。"""
    name = str(model_name or "").strip()
    idx = name.find(" (")
    if idx > 0:
        name = name[:idx].strip()
    return name


def build_pools(summary: dict, model_windows: list[dict]) -> list[dict]:
    """把「组声明 + 逐模型读数」重建为池列表（支持任意数量池）。

    model_windows 是逐模型窗口，元素需要 name / used / resets_at / model_id；
    本函数会给它们回填 pool（原地修改，与插件一致——返回前再统一展开）。
    返回只含池维度窗口，本地造出的独立池也在其中。
    """
    pools_out: list[dict] = []
    groups: list[dict] = []

    # 1) 组 → 池窗口（每个 bucket 一个窗口）
    for group in (summary.get("groups") if isinstance(summary, dict) else None) or []:
        if not isinstance(group, dict):
            continue
        pool = _pool_name(group.get("display_name") or "")
        if not pool:
            continue
        five_index = -1
        bucket_indexes: list[int] = []
        for bucket in group.get("buckets") or []:
            if not isinstance(bucket, dict) or bucket.get("disabled"):
                continue
            window_raw = str(bucket.get("window") or "").lower()
            name = ""
            span = None
            if window_raw == "5h":
                name, span = "5 小时", _FIVE
            elif window_raw == "weekly":
                name, span = "7 天", _WEEK
            else:
                name = str(bucket.get("display_name") or bucket.get("window")
                           or bucket.get("bucket_id") or "")
            if not name:
                continue
            remaining = _as_float(bucket.get("remaining_percent"))
            # remaining_percent 已按 0-100 存；缺值按「已用尽」处理（与插件一致）
            used = 100.0 if remaining is None else 100.0 - remaining
            entry = {
                "name": name,
                "pool": pool,
                "used_percent": round(used, 1),
                "remaining_percent": None if remaining is None else round(remaining, 1),
                "reset_time": bucket.get("reset_time"),
                "aside": True,
            }
            if span:
                entry["span_seconds"] = span
            if span == _FIVE and five_index < 0:
                five_index = len(pools_out)
            bucket_indexes.append(len(pools_out))
            pools_out.append(entry)
        if bucket_indexes:
            about = f"{group.get('display_name') or ''} {group.get('description') or ''}".lower()
            groups.append({
                "pool": pool, "about": about,
                "five": five_index, "all": bucket_indexes,
            })

    # 2) 逐个模型窗口挂池；挂不上且语义属于某组时本地造池
    own: set[str] = set()
    for window in model_windows:
        if window.get("pool"):
            continue
        model_id = str(window.get("model_id") or "")
        model_name = str(window.get("name") or "")
        family = _vendor_of(model_id, model_name)
        used = float(window.get("used_percent") or 0.0)
        resets_at = window.get("reset_time")

        matched = None
        for group in groups:
            if _pool_names(group["pool"], model_name, model_id):
                matched = group
                break
        if matched is not None:
            window["pool"] = matched["pool"]
            continue

        for group in groups:
            if group["five"] < 0 or _versioned(group["pool"]):
                continue
            if family.lower() not in group["about"]:
                continue
            five = pools_out[group["five"]]
            if _same_reading(used, resets_at,
                             float(five.get("used_percent") or 0.0), five.get("reset_time")):
                window["pool"] = group["pool"]
            else:
                hit = None
                for idx in group["all"]:
                    if idx == group["five"]:
                        continue
                    candidate = pools_out[idx]
                    if _same_reading(used, resets_at,
                                     float(candidate.get("used_percent") or 0.0),
                                     candidate.get("reset_time")):
                        hit = candidate
                        break
                if hit is not None:
                    window["pool"] = group["pool"]
                    if hit.get("span_seconds"):
                        window["span_seconds"] = hit["span_seconds"]
                else:
                    name = _model_pool_name(model_name)
                    if name:
                        window["pool"] = name
                        entry = {
                            "name": "5 小时",
                            "pool": name,
                            "used_percent": used,
                            "remaining_percent": round(max(0.0, 100.0 - used), 1),
                            "reset_time": resets_at,
                            "aside": True,
                            "span_seconds": _FIVE,
                        }
                        # 重置时间超过 5 小时 → 该池实际是周限额
                        if resets_at:
                            try:
                                remain = _parse_iso(resets_at) / 1000 - time.time()
                            except (TypeError, ValueError):
                                remain = 0.0
                            if remain > _FIVE + 60:
                                entry["name"] = "7 天"
                                entry["span_seconds"] = _WEEK
                                window["span_seconds"] = _WEEK
                        if name not in own:
                            own.add(name)
                            pools_out.append(entry)
            break

    return pools_out


async def _post_rpc(url: str, access_token: str, project: str,
                    client_kwargs: dict | None) -> dict:
    payload = {"project": project} if project else {}
    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT, **(client_kwargs or {})) as client:
            resp = await client.post(url, json=payload, headers=build_headers(access_token))
    except httpx.HTTPError as e:
        raise AntigravityQuotaError(f"配额查询请求失败：{e}", "network") from e
    if resp.status_code >= 400:
        body = (resp.text or "")[:300]
        raise AntigravityQuotaError(
            f"配额查询失败（HTTP {resp.status_code}）：{body}", "http",
            status_code=resp.status_code,
        )
    try:
        data = resp.json()
    except ValueError as e:
        raise AntigravityQuotaError("配额查询响应不是合法 JSON", "parse") from e
    return data if isinstance(data, dict) else {}


async def get_quota(
    db: AsyncSession,
    provider_id: int,
    provider=None,
    *,
    force: bool = False,
    credential_id: int | None = None,
    client_kwargs: dict | None = None,
) -> dict:
    """查询配额（summary 分组 + 逐模型明细），带 30s 缓存。

    令牌失效（401/403）时刷新一次并重试，刷新不了则抛 login_required。
    """
    from app.auth.antigravity import session as ag_session

    row = await ag_session.resolve_auth(db, provider_id, credential_id)
    if row is None or not row.access_token:
        raise AntigravityAuthError("请先导入 Antigravity 登录态或登录账号", "login_required")

    token = row.access_token
    cache_key = (provider_id, credential_id)
    cached = _cache.get(cache_key)
    if (not force and cached is not None
            and cached[0] == _fingerprint(token)
            and time.monotonic() - cached[1] < _CACHE_SECONDS):
        return cached[2]

    from app.core.config import settings

    project = row.project or settings.antigravity_default_project
    endpoints = resolve_endpoints(getattr(provider, "base_url", None) if provider else None)

    async def _fetch(tok: str) -> tuple[dict, list[dict]]:
        summary_raw = await _post_rpc(
            f"{endpoints.gateway}/v1internal:retrieveUserQuotaSummary", tok, project,
            client_kwargs,
        )
        models_raw = await _post_rpc(
            f"{endpoints.gateway}/v1internal:retrieveUserQuota", tok, project,
            client_kwargs,
        )
        return normalize_summary(summary_raw), normalize_models(models_raw)

    try:
        summary, models = await _fetch(token)
    except AntigravityQuotaError as e:
        # 只有 HTTP 401/403 才值得刷新令牌重试；网络/解析错误直接上抛。
        # 用 status_code 判断而不是匹配错误文案（文案一改就会静默失效）。
        unauthorized = e.kind == "http" and e.status_code in (401, 403)
        if not unauthorized:
            raise
        token = await ag_session.force_refresh(
            db, provider_id, provider, credential_id=credential_id,
        )
        summary, models = await _fetch(token)

    # 逐模型窗口：模型读数来自 retrieveUserQuota，池关联由 build_pools 回填。
    # 池数完全由数据决定（见 build_pools 注释），不假设固定两个池。
    model_windows: list[dict] = []
    for item in models:
        remaining = item.get("remaining_percent")
        model_id = str(item.get("model_id") or "")
        if not model_id:
            continue
        display = display_name(model_id)
        model_windows.append({
            "name": display,
            "model_id": model_id,
            "vendor": _vendor_of(model_id, display),
            "used_percent": 100.0 if remaining is None else round(100.0 - remaining, 1),
            "remaining_percent": remaining,
            "reset_time": item.get("reset_time"),
        })

    pools = build_pools(summary, model_windows)

    value = {
        "summary": summary,
        "models": models,
        # 池视图（池维度窗口 + 已回填 pool 的模型窗口），支持任意数量池
        "pools": pools,
        "model_windows": model_windows,
        "project": project,
        "tier": row.tier,
        "account": (row.account or {}).get("email") if isinstance(row.account, dict) else None,
    }
    _cache[cache_key] = (_fingerprint(token), time.monotonic(), value)
    return value
