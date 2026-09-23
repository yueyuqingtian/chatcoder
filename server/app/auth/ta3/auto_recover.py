"""ta3 额度自动恢复（v46，前身为 v36 plan-321-1600 R3 的 auto_overdraft）。

口径（用户明确要求）：
- 配置项 `auto_reset_on_quota_exceeded` 开启后，模型请求报错时查询一次额度，
  对**某个已用尽（>=100%）的窗口**自动提交一次恢复动作：
    · 日窗（DAILY）：上游没有日窗重置能力，「透支本周额度」才是它的恢复手段，
      因此日窗走 POST /ai/v1/quota/overdraft；
    · 周/月窗（WEEKLY/MONTHLY）：走 POST /ai/v1/quota/reset（windowType）。
- `auto_overdraft_on_quota_exceeded` 是并列的旧开关，仍然保留，语义统一为
  「额度用尽时自动恢复」——两个开关任一开启即生效，互不冲突。
- 带冷却窗口，避免连续报错时反复提交（远端幂等，但没必要刷请求）；
- 全程不抛出异常：自动恢复是"尽力而为"的补救，失败不影响原始错误上报。
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

# 同一 provider 的自动恢复冷却（秒）：报错风暴时只尝试一次
_COOLDOWN_SECONDS = 300.0
_last_attempt: dict[int, float] = {}

# 触发恢复的窗口使用率阈值
_TRIGGER_PERCENT = 100.0


def _to_float(value) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _config_enabled() -> bool:
    """两个额度恢复开关任一开启即视为启用。"""
    try:
        from app.core.config import settings

        return bool(
            getattr(settings, "auto_reset_on_quota_exceeded", False)
            or getattr(settings, "auto_overdraft_on_quota_exceeded", False)
        )
    except Exception:
        return False


def _pick_exhausted_window(windows) -> dict | None:
    """挑一个已用尽且允许服务端操作的窗口。

    日窗优先（当前请求多因日额触顶）：日窗用尽且 canOverdraft 未被显式否定才可选；
    周/月窗用尽且 canReset 未被显式否定（字段缺失时按旧行为放行，交给远端判定）。
    """
    by_window: dict[str, dict] = {}
    for w in windows or []:
        if isinstance(w, dict):
            name = str(w.get("window") or "").upper()
            if name:
                by_window[name] = w

    daily = by_window.get("DAILY")
    if daily is not None:
        percent = _to_float(daily.get("percentUsed"))
        if percent is not None and percent >= _TRIGGER_PERCENT and daily.get("canOverdraft") is not False:
            return daily

    for name in ("WEEKLY", "MONTHLY"):
        w = by_window.get(name)
        if w is None:
            continue
        percent = _to_float(w.get("percentUsed"))
        if percent is not None and percent >= _TRIGGER_PERCENT and w.get("canReset") is not False:
            return w
    return None


async def maybe_auto_recover(provider_id: int | None, *, reason: str = "") -> bool:
    """按配置尝试一次额度自动恢复。返回是否真的提交了恢复请求。"""
    if not provider_id:
        return False
    if not _config_enabled():
        return False

    now = time.monotonic()
    if now - _last_attempt.get(provider_id, 0.0) < _COOLDOWN_SECONDS:
        logger.debug("[ta3] 额度自动恢复冷却中，跳过 provider=%s", provider_id)
        return False
    _last_attempt[provider_id] = now

    try:
        from app.persistence.database import async_session_factory

        async with async_session_factory() as db:
            from app.persistence.models.model_reg import Provider

            provider = await db.get(Provider, provider_id)
            if provider is None or (provider.api_format or "").lower() != "ta3":
                return False
            from app.auth.ta3.oauth import DEFAULT_TA3_API_BASE

            api_base = (provider.base_url or DEFAULT_TA3_API_BASE).rstrip("/")
            if not api_base:
                return False

            from app.auth.ta3 import quota as ta3_quota

            # 报错后查一次最新额度（force 跳过 10s 缓存）
            payload = await ta3_quota.get_quota(db, provider_id, api_base, force=True)
            windows = payload.get("windows") if isinstance(payload, dict) else None
            target = _pick_exhausted_window(windows)
            if target is None:
                logger.debug("[ta3] 额度自动恢复：无已用尽且可操作的窗口，跳过")
                return False

            window = str(target.get("window") or "").upper()
            remark = (f"额度自动恢复（{reason}）" if reason else "额度自动恢复")[:200]

            if window == "DAILY":
                result = await ta3_quota.request_overdraft(db, provider_id, api_base, remark=remark)
                logger.info("[ta3] provider=%s 已自动提交日额度透支: %s", provider_id, result)
                return True

            result = await ta3_quota.request_reset(db, provider_id, api_base, window, remark=remark)
            logger.info("[ta3] provider=%s 已自动提交%s额度重置: %s", provider_id, window, result)
            return True
    except Exception:
        logger.warning("[ta3] 额度自动恢复失败(非阻塞)", exc_info=True)
        return False
