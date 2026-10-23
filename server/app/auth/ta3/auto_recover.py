"""ta3 额度自动恢复（v46，前身为 v36 plan-321-1600 R3 的 auto_overdraft）。

口径（用户明确要求）：
- 配置项 `auto_reset_on_quota_exceeded` 开启后，模型请求报错时查询一次额度，
  对**某个已用尽（>=100%）的窗口**自动提交一次恢复动作：
    · 日窗（DAILY）：上游没有日窗重置能力，「透支本周额度」才是它的恢复手段，
      因此日窗走 POST /ai/v1/quota/overdraft；
    · 周/月窗（WEEKLY/MONTHLY）：走 POST /ai/v1/quota/reset（windowType）。
- `auto_overdraft_on_quota_exceeded` 是并列的旧开关，仍然保留，语义统一为
  「额度用尽时自动恢复」——两个开关任一开启即生效，互不冲突。
- **不再有固定时间冷却**（v47）：此前同一供应商 5 分钟内只尝试一次，用 kimi-k3
  这类高消耗模型时日额度可能在一两分钟内被打满，恢复后再次打满却要等到窗口过去
  才肯重试，用户看到的是"额度明明恢复了还是不停报错"。现在的节流口径是
  **按额度状态幂等**：同一 (窗口, 报错码, 用量阈值) 只提交一次；一旦观测到额度
  回落（重置/透支生效）或换了错误类型，立即允许再次恢复。
- 全程不抛出异常：自动恢复是"尽力而为"的补救，失败不影响原始错误上报。
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

# 同一额度状态的去重窗口（秒）：同一次"额度用尽"引发的报错风暴只提交一次恢复。
# 这是对**同一状态**的幂等去重，不是用户操作的频率限制——状态一变即可重试。
_DEDUPE_SECONDS = 20.0
# provider_id -> (窗口, 错误标签, 用量百分比, 首次尝试时刻)
_last_attempt: dict[int, tuple[str, str, float, float]] = {}

# 触发恢复的窗口使用率阈值
_TRIGGER_PERCENT = 100.0
# 用量回落到该百分比以下即视为"额度已恢复"，下次报错允许重新尝试
_REVIVE_PERCENT = 99.0


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
            from app.services.credential_service import provider_client_kwargs

            # plan-110-438 M2: 自动恢复请求同样按供应商代理配置出网
            _ckw = provider_client_kwargs(provider)

            # 报错后查一次最新额度（force 跳过 10s 缓存）
            payload = await ta3_quota.get_quota(db, provider_id, api_base, force=True,
                                                client_kwargs=_ckw)
            windows = payload.get("windows") if isinstance(payload, dict) else None
            target = _pick_exhausted_window(windows)
            if target is None:
                # 额度已经回落（重置/透支生效，或本来就是别的原因报错）→ 清掉去重状态，
                # 下次再打满时可以立即恢复，不必等任何时间窗口。
                _last_attempt.pop(provider_id, None)
                logger.debug("[ta3] 额度自动恢复：无已用尽且可操作的窗口，跳过")
                return False

            window = str(target.get("window") or "").upper()
            percent = _to_float(target.get("percentUsed")) or _TRIGGER_PERCENT
            if _dedupe_hit(provider_id, window, percent, reason):
                logger.debug("[ta3] 额度自动恢复：同一额度状态已尝试过，跳过 provider=%s", provider_id)
                return False

            remark = (f"额度自动恢复（{reason}）" if reason else "额度自动恢复")[:200]
            _last_attempt[provider_id] = (window, reason, percent, time.monotonic())

            if window == "DAILY":
                result = await ta3_quota.request_overdraft(db, provider_id, api_base, remark=remark,
                                                          client_kwargs=_ckw)
                logger.info("[ta3] provider=%s 已自动提交日额度透支: %s", provider_id, result)
            else:
                result = await ta3_quota.request_reset(db, provider_id, api_base, window,
                                                      remark=remark, client_kwargs=_ckw)
                logger.info("[ta3] provider=%s 已自动提交%s额度重置: %s", provider_id, window, result)

            # v47: 额度恢复动作已提交 → 同步解除凭据冷却。冷却行在 ta3 的选取路径里是
            # 硬过滤，不解除的话恢复额度后依然本地直接报 provider_credential_unavailable。
            try:
                from app.services import credential_service

                revived = await credential_service.revive_provider_credentials_by_id(db, provider_id)
                if revived:
                    logger.info("[ta3] provider=%s 额度自动恢复后已解除 %d 条凭据的冷却",
                                provider_id, revived)
            except Exception:  # noqa: BLE001 - 解除冷却失败不影响恢复结果
                logger.warning("[ta3] provider=%s 自动恢复后解除凭据冷却失败(非阻塞)",
                               provider_id, exc_info=True)
            return True
    except Exception:
        logger.warning("[ta3] 额度自动恢复失败(非阻塞)", exc_info=True)
        return False


def _dedupe_hit(provider_id: int, window: str, percent: float, reason: str) -> bool:
    """同一额度状态是否刚刚尝试过（状态幂等去重，不是时间限流）。

    命中条件三者同时成立：同一窗口、同一报错标签、用量没有回落。
    任一变化（额度被重置回 100% 以下、换成别的错误）都允许立即重试，
    所以高消耗模型短时间内反复打满额度时不会被卡住。
    """
    prev = _last_attempt.get(provider_id)
    if prev is None:
        return False
    prev_window, prev_reason, prev_percent, at = prev
    if percent <= _REVIVE_PERCENT:
        return False
    if window != prev_window or reason != prev_reason:
        return False
    if percent < prev_percent:
        return False  # 用量回落说明恢复动作生效了，允许再试
    return (time.monotonic() - at) < _DEDUPE_SECONDS
