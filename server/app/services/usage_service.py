"""用量流水的辅助逻辑（plan-20261021-usage-update）。

- 账号 / Key 快照：调用记录表要显示「这次调用用的哪个账号 / Key」，凭据之后可能被改名或删除，
  所以落库时把展示用的备注名与掩码 Key 一并冻结在流水里；
- 历史流水的速度估算：旧流水没有首字计时，按会话消息的时间戳估算整次请求耗时。
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# 速度口径（与前端脚注一致）：实测只算输出 ≥ 100 token 的请求，估算只算 ≥ 200 token 的请求
MIN_TOKENS_MEASURED = 100
MIN_TOKENS_ESTIMATED = 200


def mask_key(key: str | None) -> str:
    """Key 掩码预览（与供应商页凭据列表同一写法，不泄露完整 Key）。"""
    if not key:
        return ""
    if len(key) <= 8:
        return "••••" + key[-2:]
    return f"{key[:4]}••••{key[-4:]}"


def _is_placeholder(key: str | None) -> bool:
    """OAuth 类供应商（workbuddy / ta3 / trae / kiro）的 api_key 是 `__xxx_session__` 占位值，不是真实 Key。"""
    k = (key or "").strip()
    return not k or (k.startswith("__") and k.endswith("__"))


async def credential_snapshot(
    db: AsyncSession, credential_id: int | None, provider_row, model_row,
) -> tuple[int | None, str, str]:
    """返回 (凭据 id, 备注名 / 账号名, 掩码 Key)，供用量流水落库。失败返回空值，不影响主流程。"""
    label = ""
    key: str | None = None
    try:
        cred = None
        if credential_id is not None:
            from app.persistence.models.model_reg import ProviderCredential

            cred = await db.get(ProviderCredential, credential_id)
        if cred is not None:
            label = (cred.label or "").strip()
            key = cred.api_key
        else:
            # 没有凭据行（单 Key 供应商 / 独立模型）：回落供应商或模型自带的 Key
            key = getattr(provider_row, "api_key", None) or getattr(model_row, "api_key", None)
        if not label:
            label = (getattr(provider_row, "account_label", None) or "").strip()
    except Exception:  # noqa: BLE001 - 快照失败只丢展示信息
        logger.debug("[usage] 账号快照失败(非阻塞)", exc_info=True)
    hint = "" if _is_placeholder(key) else mask_key(key)
    return credential_id, label[:120], hint[:40]


def _to_naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _parse_ts(value) -> datetime | None:
    """messages.created_at 是字符串列（秒精度），usage_records.created_at 是 DateTime，统一成 naive UTC。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return _to_naive_utc(value)
    text_value = str(value).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text_value[:26] if "." in text_value else text_value[:19], fmt)
        except ValueError:
            continue
    return None


async def estimate_elapsed_seconds(db: AsyncSession, records: list) -> dict[int, float]:
    """按会话消息时间戳，估算历史流水每次请求的耗时（秒）。返回 {流水 id: 秒数}。

    历史流水没有首字计时。一次模型请求的起点 = 同一轮里上一条「用户消息 / 工具结果」落库的时刻，
    终点 = 本条流水落库的时刻——所以估算值把「等首字」的时间也算了进去，偏低（脚注说明），
    且时间戳只有秒精度，只对输出足够多、耗时足够长的请求有参考价值。

    起点的取法（按同一会话 + 同一轮 + 同一代理）：
    - 取比本条流水早的最近一条用户消息 / 工具结果；
    - 若同一代理上一次流水更晚，则不早于它（上一次请求结束之后才可能开始下一次）；
    - 都没有就退回上一次流水的时刻；仍没有则放弃（不估算）。
    """
    from app.persistence.models.message import Message
    from app.persistence.models.usage_record import UsageRecord

    todo = [r for r in records if r.session_id is not None and r.turn_id is not None]
    if not todo:
        return {}
    result: dict[int, float] = {}
    by_turn: dict[tuple[int, int], list] = defaultdict(list)
    for r in todo:
        by_turn[(r.session_id, r.turn_id)].append(r)

    for (sid, tid), recs in by_turn.items():
        try:
            msg_rows = (await db.execute(
                select(Message.msg_type, Message.sender_type, Message.sender_id, Message.created_at)
                .where(Message.session_id == sid, Message.turn_id == tid,
                       Message.msg_type.in_(("tool_result", "text")))
            )).all()
            # 起点候选：用户消息（sender_type=user）与工具结果
            starts: list[tuple[datetime, int | None]] = []
            for mtype, stype, sender_id, created in msg_rows:
                if mtype == "tool_result" or stype == "user":
                    ts = _parse_ts(created)
                    if ts is not None:
                        starts.append((ts, sender_id if stype != "user" else None))
            # 同一轮全部流水（含不在本页的），用来取「上一次流水」
            turn_usage = (await db.execute(
                select(UsageRecord.id, UsageRecord.agent_id, UsageRecord.created_at)
                .where(UsageRecord.session_id == sid, UsageRecord.turn_id == tid)
                .order_by(UsageRecord.id)
            )).all()
        except Exception:  # noqa: BLE001
            logger.debug("[usage] 估算耗时查询失败(非阻塞)", exc_info=True)
            continue
        usage_ts = [(uid, aid, _parse_ts(created)) for uid, aid, created in turn_usage]
        for r in recs:
            end = _parse_ts(r.created_at)
            if end is None:
                continue
            prev_usage = None
            for uid, aid, ts in usage_ts:
                if uid >= r.id:
                    break
                if aid == r.agent_id and ts is not None:
                    prev_usage = ts
            cands = [ts for ts, owner in starts
                     if ts < end and (owner is None or r.agent_id is None or owner == r.agent_id)
                     and (prev_usage is None or ts >= prev_usage)]
            start = max(cands) if cands else prev_usage
            if start is None:
                continue
            elapsed = (end - start).total_seconds()
            if elapsed > 0:
                result[r.id] = elapsed
    return result


def compute_speed(completion_tokens: int, seconds: float | None, *, estimated: bool) -> float | None:
    """输出速度（token/s）。不满足口径（输出太少 / 没有耗时）返回 None。"""
    floor = MIN_TOKENS_ESTIMATED if estimated else MIN_TOKENS_MEASURED
    if not seconds or seconds <= 0 or (completion_tokens or 0) < floor:
        return None
    return round(completion_tokens / seconds, 1)
