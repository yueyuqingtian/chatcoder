"""使用统计（plan-152-704：全软件 token 用量，支持自定义时间区间 + 供应商分组）。

plan-20261021-usage-update：新增 /usage/records —— 按调用逐条列出（时间 / 供应商 / 账号 / 模型 /
token / 首字 / 耗时 / 速度），分页，口径与 /usage/stats 的区间、供应商筛选一致。
"""
import math
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.database import get_db
from app.services import usage_service

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


def _resolve_range(start: str | None, end: str | None, days: int) -> tuple[date, date, date, datetime, datetime]:
    """把 start/end/days 解析成本地日期闭区间，返回 (start_d, end_d, today, t_start, t_end[右开 UTC])。"""
    today = datetime.now().astimezone().date()
    if start:
        start_d = date.fromisoformat(start)
        end_d = date.fromisoformat(end) if end else today
        if end_d < start_d:
            start_d, end_d = end_d, start_d
    else:
        end_d = today
        start_d = today - timedelta(days=max(1, days or 30) - 1)
    return (start_d, end_d, today,
            _local_midnight_utc_naive(start_d),
            _local_midnight_utc_naive(end_d + timedelta(days=1)))


@router.get("/stats")
async def usage_stats(
    start: str | None = Query(default=None, description="自定义起始日期 YYYY-MM-DD"),
    end: str | None = Query(default=None, description="自定义截止日期 YYYY-MM-DD"),
    days: int = Query(default=30, description="预设区间天数（无自定义日期时生效）"),
    # plan-308-1542 需求6：按供应商筛选。用普通默认值（而非 Query(default=None)）——
    # 这样单测直接调用本函数时不会拿到 Query 对象，FastAPI 侧行为不变。
    provider: str | None = None,
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
    start_d, end_d, today, t_start, t_end = _resolve_range(start, end, days)

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
            provider_name = r.provider_name or (model_map.get(r.model_id, ("", ""))[1] if r.model_id in model_map else "")
        else:
            key = f"name::{r.model_name or '未知模型'}"
            name = r.model_name or "未知模型"
            provider_name = r.provider_name or ""
        display = f"{provider_name}/{name}" if provider_name else (name or "未知模型")
        return key, name, provider_name, display

    # plan-308-1542 需求6：先算出"本次区间内出现过的全部供应商"（不受 provider 筛选影响），
    # 供前端下拉框展示可选项；再进行实际筛选。
    all_providers = sorted({resolve(r)[2] for r in rows if resolve(r)[2]})
    want = (provider or "").strip()
    if want:
        rows = [r for r in rows if resolve(r)[2] == want]

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
    # plan-308-1542 需求6：与区间口径保持一致——选定供应商时，热力图/峰值/连续天数
    # 也只统计该供应商，否则"筛了供应商但热力图没变"会让用户以为筛选没生效。
    all_rows = (
        await db.execute(
            select(UsageRecord.created_at, UsageRecord.prompt_tokens, UsageRecord.completion_tokens,
                   UsageRecord.model_id, UsageRecord.model_name, UsageRecord.provider_name)
        )
    ).all()
    all_prov_ids = {r[3] for r in all_rows if r[3] is not None}
    all_model_map: dict[int, str] = {}
    if all_prov_ids:
        _mrows = (await db.execute(select(Model).where(Model.id.in_(all_prov_ids)))).scalars().all()
        _pids = {m.provider_id for m in _mrows if m.provider_id is not None}
        _prov = {}
        if _pids:
            _prows = (await db.execute(select(Provider).where(Provider.id.in_(_pids)))).scalars().all()
            _prov = {p.id: p.name for p in _prows}
        all_model_map = {m.id: _prov.get(m.provider_id, "") for m in _mrows}

    all_map: dict[str, int] = defaultdict(int)
    for dt, p, c, mid, mname, pname in all_rows:
        if want:
            prov_name = pname or (all_model_map.get(mid, "") if mid is not None else "")
            if prov_name != want:
                continue
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

    perf_conds = [UsageRecord.created_at >= t_start, UsageRecord.created_at < t_end]
    if want:
        perf_conds.append(_provider_condition(want))
    perf = await _perf_summary(db, perf_conds)

    return {
        "total": {**total, "total": total["prompt"] + total["completion"], "calls": len(rows)},
        "by_model": by_model_out,
        "daily": daily_out,
        "daily_by_model": daily_by_model_out,
        "daily_all": daily_all,
        "peak_tokens": peak_tokens,
        "streak_current": streak_current,
        "streak_longest": longest,
        # plan-308-1542 需求6：供应商筛选 + 可选列表
        "providers": all_providers,
        "provider": want or None,
        # plan-20261021-usage-update：平均首字 / 平均速度（与调用记录表同口径）
        "perf": perf,
    }


# ── plan-20261021-usage-update：调用记录（分页） ──

PAGE_SIZE_DEFAULT = 20
PAGE_SIZE_MAX = 100


def _provider_condition(want: str):
    """供应商筛选条件（SQL 侧）：优先用流水自带的 provider_name，历史流水回落 model_id → 模型 → 供应商。"""
    from app.persistence.models.model_reg import Model, Provider
    from app.persistence.models.usage_record import UsageRecord

    legacy_model_ids = (
        select(Model.id).join(Provider, Provider.id == Model.provider_id).where(Provider.name == want)
    )
    return or_(
        UsageRecord.provider_name == want,
        and_(
            or_(UsageRecord.provider_name.is_(None), UsageRecord.provider_name == ""),
            UsageRecord.model_id.in_(legacy_model_ids),
        ),
    )


async def _perf_summary(db: AsyncSession, conds: list) -> dict:
    """区间内的平均首字 / 平均速度（调用记录表与统计总览共用同一口径）。

    - 平均首字：所有有实测首字计时的记录；
    - 平均速度：输出 ≥ 100 token 且有生成耗时的实测记录，按 token 加权（总输出 ÷ 总生成耗时）。
    """
    from app.persistence.models.usage_record import UsageRecord

    ttft_avg = (await db.execute(
        select(func.avg(UsageRecord.first_token_ms)).where(*conds, UsageRecord.first_token_ms.is_not(None))
    )).scalar()
    sp = (await db.execute(
        select(func.count(), func.sum(UsageRecord.completion_tokens), func.sum(UsageRecord.gen_ms)).where(
            *conds, UsageRecord.first_token_ms.is_not(None), UsageRecord.gen_ms > 0,
            UsageRecord.completion_tokens >= usage_service.MIN_TOKENS_MEASURED,
        )
    )).one()
    speed_calls, speed_tokens, speed_ms = int(sp[0] or 0), int(sp[1] or 0), int(sp[2] or 0)
    avg_speed = round(speed_tokens / (speed_ms / 1000), 1) if speed_calls and speed_ms > 0 else None
    return {
        "avg_first_token_ms": int(ttft_avg) if ttft_avg is not None else None,
        "avg_speed": avg_speed,
        "speed_calls": speed_calls,
    }


def _iso_utc(dt: datetime | None) -> str | None:
    """库里的 created_at 是 UTC naive，带 Z 返回，前端按本地时区格式化。"""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.replace(microsecond=0).isoformat() + "Z"


@router.get("/records")
async def usage_records(
    start: str | None = Query(default=None, description="自定义起始日期 YYYY-MM-DD"),
    end: str | None = Query(default=None, description="自定义截止日期 YYYY-MM-DD"),
    days: int = Query(default=30, description="预设区间天数（无自定义日期时生效）"),
    provider: str | None = None,
    page: int = 1,
    page_size: int = PAGE_SIZE_DEFAULT,
    db: AsyncSession = Depends(get_db),
):
    """调用记录：按时间倒序逐条列出区间内的模型调用，分页（默认每页 20 条）。

    - 区间、供应商筛选与 /usage/stats 同一口径；
    - 速度 = 输出 token ÷ 首字到结束的耗时；实测只算输出 ≥ 100 token 的请求；
    - 没有首字计时的历史记录按会话消息时间戳估算（estimated=true）：把等首字的时间也算进去，
      所以偏低，且只估输出 ≥ 200 token 的请求；
    - summary 是整个筛选结果（不止当前页）的平均首字与平均速度。
    """
    from app.persistence.models.model_reg import Model, Provider
    from app.persistence.models.usage_record import UsageRecord

    page = max(1, int(page or 1))
    page_size = max(1, min(PAGE_SIZE_MAX, int(page_size or PAGE_SIZE_DEFAULT)))
    _, _, _, t_start, t_end = _resolve_range(start, end, days)

    conds = [UsageRecord.created_at >= t_start, UsageRecord.created_at < t_end]
    want = (provider or "").strip()
    if want:
        conds.append(_provider_condition(want))

    total = (await db.execute(select(func.count()).select_from(UsageRecord).where(*conds))).scalar() or 0
    pages = max(1, math.ceil(total / page_size))

    rows = (await db.execute(
        select(UsageRecord).where(*conds)
        .order_by(UsageRecord.created_at.desc(), UsageRecord.id.desc())
        .limit(page_size).offset((page - 1) * page_size)
    )).scalars().all()

    # 历史流水的供应商名 / 模型名回填（model_id → Model → Provider）
    ids = {r.model_id for r in rows if r.model_id is not None}
    model_map: dict[int, tuple[str, str]] = {}
    if ids:
        mrows = (await db.execute(select(Model).where(Model.id.in_(ids)))).scalars().all()
        pids = {m.provider_id for m in mrows if m.provider_id is not None}
        prov: dict[int, str] = {}
        if pids:
            prov = {p.id: p.name for p in (await db.execute(
                select(Provider).where(Provider.id.in_(pids)))).scalars().all()}
        model_map = {m.id: (m.name, prov.get(m.provider_id, "")) for m in mrows}

    # 没有实测首字计时的记录，按会话消息时间戳估算整次请求耗时
    need_est = [r for r in rows if r.first_token_ms is None and (r.completion_tokens or 0) >= usage_service.MIN_TOKENS_ESTIMATED]
    elapsed_map = await usage_service.estimate_elapsed_seconds(db, need_est) if need_est else {}

    items = []
    for r in rows:
        fallback_model, fallback_provider = model_map.get(r.model_id, ("", "")) if r.model_id is not None else ("", "")
        measured = r.first_token_ms is not None and r.gen_ms is not None
        est_s = None if measured else elapsed_map.get(r.id)
        speed = (
            usage_service.compute_speed(r.completion_tokens or 0, (r.gen_ms or 0) / 1000, estimated=False)
            if measured else usage_service.compute_speed(r.completion_tokens or 0, est_s, estimated=True)
        )
        items.append({
            "id": r.id,
            "time": _iso_utc(r.created_at),
            "provider": r.provider_name or fallback_provider or "",
            "model": r.model_name or fallback_model or "未知模型",
            "account": r.credential_label or "",
            "account_hint": r.credential_hint or "",
            "prompt": r.prompt_tokens or 0,
            "cached": r.cached_tokens or 0,
            "completion": r.completion_tokens or 0,
            "reasoning": r.reasoning_tokens or 0,
            "first_token_ms": r.first_token_ms if measured else None,
            "gen_ms": r.gen_ms if measured else None,
            # 估算行：整次请求耗时（含等首字），前端以「≈」标注
            "est_ms": int(est_s * 1000) if est_s else None,
            "speed": speed,
            "estimated": (not measured) and speed is not None,
            "session_id": r.session_id,
        })

    summary = await _perf_summary(db, conds)

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
        "provider": want or None,
        "summary": summary,
        "items": items,
    }
