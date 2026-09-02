"""使用统计（plan-152-704：全软件 token 用量，支持自定义时间区间 + 供应商分组）。"""
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.database import get_db

router = APIRouter(prefix="/usage", tags=["usage"])


def _local_date_str(dt: datetime) -> str:
    """把 UTC 时间转成本地日期字符串（YYYY-MM-DD）。created_at 存的是 UTC。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().date().isoformat()


def _local_midnight_utc_naive(d: date) -> datetime:
    """本地某日 00:00 对应的 UTC 时刻（naive，便于与 created_at 列比较）。"""
    local_dt = datetime(d.year, d.month, d.day).astimezone()  # naive 按本地解释
    return local_dt.astimezone(timezone.utc).replace(tzinfo=None)


@router.get("/stats")
async def usage_stats(
    start: str | None = Query(default=None, description="自定义起始日期 YYYY-MM-DD"),
    end: str | None = Query(default=None, description="自定义截止日期 YYYY-MM-DD"),
    days: int = Query(default=30, description="预设区间天数（无自定义日期时生效）"),
    db: AsyncSession = Depends(get_db),
):
    """全软件 token 用量统计。

    - start/end 提供时作为自定义本地日期区间；否则用 days（默认 30 天）。
    - 按模型分组（model_id 优先，缺省回退 model_name），并回填历史流水的供应商名。
    - 返回：总量、按模型分布、区间逐日聚合、区间逐日×模型、全历史逐日聚合（热力图/连续天数）、峰值与连续天数。
    """
    from app.persistence.models.model_reg import Model, Provider
    from app.persistence.models.usage_record import UsageRecord

    # ── 时间区间（本地日期，闭区间）──
    today = datetime.now().astimezone().date()
    if start:
        start_d = date.fromisoformat(start)
        end_d = date.fromisoformat(end) if end else today
        if end_d < start_d:
            start_d, end_d = end_d, start_d
    else:
        end_d = today
        start_d = today - timedelta(days=max(1, days or 30) - 1)
    t_start = _local_midnight_utc_naive(start_d)
    t_end = _local_midnight_utc_naive(end_d + timedelta(days=1))  # 右开区间

    rows = (
        (await db.execute(
            select(UsageRecord).where(
                UsageRecord.created_at >= t_start,
                UsageRecord.created_at < t_end,
            )
        )).scalars().all()
    )

    # ── 历史行供应商名回填（model_id → Model → Provider）──
    ids = {r.model_id for r in rows if r.model_id is not None}
    model_map: dict[int, tuple[str, str]] = {}
    if ids:
        mrows = (await db.execute(select(Model).where(Model.id.in_(ids)))).scalars().all()
        pid_set = {m.provider_id for m in mrows if m.provider_id is not None}
        prov = {}
        if pid_set:
            prows = (await db.execute(select(Provider).where(Provider.id.in_(pid_set)))).scalars().all()
            prov = {p.id: p.name for p in prows}
        model_map = {m.id: (m.name, prov.get(m.provider_id, "")) for m in mrows}

    def resolve(r) -> tuple[str, str, str, str]:
        """返回 (key, model, provider, display_name)。"""
        if r.model_id is not None:
            key = str(r.model_id)
            name = r.model_name or (model_map.get(r.model_id, ("", ""))[0] if r.model_id in model_map else "")
            provider = r.provider_name or (model_map.get(r.model_id, ("", ""))[1] if r.model_id in model_map else "")
        else:
            key = f"name::{r.model_name or '未知模型'}"
            name = r.model_name or "未知模型"
            provider = r.provider_name or ""
        display = f"{provider}/{name}" if provider else (name or "未知模型")
        return key, name, provider, display

    # ── 汇总 ──
    total = {"prompt": 0, "completion": 0, "reasoning": 0, "cached": 0}
    by_model: dict[str, dict] = {}
    daily_map: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # date -> [tokens, calls]
    daily_by_model: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))  # key -> date -> tokens
    info: dict[str, dict] = {}
    for r in rows:
        total["prompt"] += r.prompt_tokens or 0
        total["completion"] += r.completion_tokens or 0
        total["reasoning"] += r.reasoning_tokens or 0
        total["cached"] += r.cached_tokens or 0
        key, name, provider, display = resolve(r)
        slot = by_model.setdefault(key, {"prompt": 0, "completion": 0, "reasoning": 0, "cached": 0, "calls": 0})
        slot["prompt"] += r.prompt_tokens or 0
        slot["completion"] += r.completion_tokens or 0
        slot["reasoning"] += r.reasoning_tokens or 0
        slot["cached"] += r.cached_tokens or 0
        slot["calls"] += 1
        info[key] = {"model": name, "provider_name": provider, "display_name": display}
        d = _local_date_str(r.created_at)
        daily_map[d][0] += (r.prompt_tokens or 0) + (r.completion_tokens or 0)
        daily_map[d][1] += 1
        daily_by_model[key][d] += (r.prompt_tokens or 0) + (r.completion_tokens or 0)

    by_model_out = [
        {"key": k, **info[k], **v, "total": v["prompt"] + v["completion"]}
        for k, v in sorted(by_model.items(), key=lambda kv: -(kv[1]["prompt"] + kv[1]["completion"]))
    ]
    daily_out = [{"date": d, "tokens": v[0], "calls": v[1]} for d, v in sorted(daily_map.items())]
    daily_by_model_out = [
        {"date": d, "key": k, "display_name": info[k]["display_name"], "tokens": t}
        for k, m in sorted(daily_by_model.items())
        for d, t in sorted(m.items())
        if t > 0
    ]

    # ── 全历史逐日聚合（热力图 + 峰值 + 连续天数）──
    all_rows = (
        await db.execute(
            select(UsageRecord.created_at, UsageRecord.prompt_tokens, UsageRecord.completion_tokens)
        )
    ).all()
    all_map: dict[str, int] = defaultdict(int)
    for dt, p, c in all_rows:
        all_map[_local_date_str(dt)] += (p or 0) + (c or 0)
    daily_all = [{"date": d, "tokens": t} for d, t in sorted(all_map.items())]
    peak_tokens = max((v["tokens"] for v in daily_all), default=0)

    active_dates = sorted({v["date"] for v in daily_all if v["tokens"] > 0})
    longest, cur, prev = 0, 0, None
    for d in active_dates:
        cur = cur + 1 if prev and (date.fromisoformat(d) - prev).days == 1 else 1
        longest = max(longest, cur)
        prev = date.fromisoformat(d)
    dset = set(active_dates)
    today_iso = today.isoformat()
    cursor = today if today_iso in dset else today - timedelta(days=1)
    streak_current = 0
    while cursor.isoformat() in dset:
        streak_current += 1
        cursor -= timedelta(days=1)

    return {
        "total": {**total, "total": total["prompt"] + total["completion"], "calls": len(rows)},
        "by_model": by_model_out,
        "daily": daily_out,
        "daily_by_model": daily_by_model_out,
        "daily_all": daily_all,
        "peak_tokens": peak_tokens,
        "streak_current": streak_current,
        "streak_longest": longest,
    }
