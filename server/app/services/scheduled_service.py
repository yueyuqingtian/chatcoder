"""定时任务 CRUD 与 cron 解析（plan-230-1144 M1.1）。

改造前本模块只有 CRUD，且 `parse_cron` 仅取 minute/hour 首值（`"*/15"` 被算成 0）、
day/month/dow 完全不参与计算，全仓库也没有任何调度循环消费这些任务——用户创建的
定时任务永远不会执行。本模块补齐：

1. `parse_cron`：完整 5 段解析（`*` / `*/n` / `a` / `a-b` / `a-b/n` / `a,b,c`），
   非法表达式抛 `CronParseError`；month/dow 支持英文名缩写。
2. `compute_next_run`：按 cron 语义推算下次触发时刻（本地时区墙钟语义，见下）。
3. 抢占式领取（`claim_due`）：多实例/并发 tick 下靠单写线程的
   `UPDATE ... WHERE next_run_at <= now` 保证同一时刻只触发一次。

时区语义：cron 表达式按**本地墙钟**匹配（用户写 `0 9 * * *` 期望"我本机的早上九点"），
而 `next_run_at` 落库统一用带偏移的 UTC ISO 字符串，与全仓库时间戳口径一致。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.scheduled import ScheduledTask

logger = logging.getLogger(__name__)

# cron 字段边界（含 dow 的 7≡0 归一）
_FIELD_SPECS: tuple[tuple[str, int, int], ...] = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day_of_month", 1, 31),
    ("month", 1, 12),
    ("day_of_week", 0, 7),
)

_MONTH_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_DOW_NAMES = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}

# 每天内的 (hour, minute) 组合数上限：24*60，用于估算日循环上界时做保护
_MAX_SEARCH_DAYS = 366 * 5


class CronParseError(ValueError):
    """非法 cron 表达式。"""


@dataclass(frozen=True)
class CronSpec:
    """解析后的 cron。`dom_restricted` / `dow_restricted` 记录字段是否被显式限定，
    用于实现 POSIX/Vixie cron 的"两者都限定则取并集"语义。
    """

    minutes: frozenset[int]
    hours: frozenset[int]
    days_of_month: frozenset[int]
    months: frozenset[int]
    days_of_week: frozenset[int]  # 0=周日，已把 7 归一为 0
    dom_restricted: bool = field(default=False)
    dow_restricted: bool = field(default=False)

    def matches_date(self, d: date) -> bool:
        if d.month not in self.months:
            return False
        # cron 惯例：day-of-month 与 day-of-week 同时被限定时，命中任一即可；
        # 只限定其一时，必须满足被限定的那个。
        dom_ok = (not self.dom_restricted) or (d.day in self.days_of_month)
        dow_ok = (not self.dow_restricted) or ((d.weekday() + 1) % 7 in self.days_of_week)
        if self.dom_restricted and self.dow_restricted:
            return dom_ok or dow_ok
        return dom_ok and dow_ok

    def day_times(self) -> list[tuple[int, int]]:
        """当天所有合法的 (hour, minute)，升序。"""
        return [(h, m) for h in sorted(self.hours) for m in sorted(self.minutes)]


def _parse_field(raw: str, lo: int, hi: int, names: dict[str, int] | None, label: str) -> tuple[frozenset[int], bool]:
    """解析单个 cron 字段。返回 (取值集合, 是否被显式限定)。"""
    raw = raw.strip()
    if not raw:
        raise CronParseError(f"{label} 字段为空")

    values: set[int] = set()
    restricted = raw != "*"

    for part in raw.split(","):
        part = part.strip()
        if not part:
            raise CronParseError(f"{label} 字段含空项: {raw!r}")

        step = 1
        step_match = re.fullmatch(r"(?P<body>.+?)/(?P<step>\d+)", part)
        if step_match:
            part = step_match.group("body")
            step = int(step_match.group("step"))
            if step <= 0:
                raise CronParseError(f"{label} 步长必须为正整数: {part!r}")

        if part == "*":
            start, end = lo, hi
        else:
            range_match = re.fullmatch(r"(?P<a>[^-]+)-(?P<b>[^-]+)", part)
            single_match = re.fullmatch(r"(?P<a>[^-]+)", part)
            if range_match:
                start = _to_int(range_match.group("a"), names, label)
                end = _to_int(range_match.group("b"), names, label)
            elif single_match:
                start = _to_int(single_match.group("a"), names, label)
                # 单值 + 步长（如 "5/10"）按"从该值到上界"处理，与 Vixie cron 一致
                end = hi if step > 1 else start
            else:
                raise CronParseError(f"{label} 字段无法解析: {part!r}")

            if start > end:
                # 允许环绕写法（如 dow "6-1" 表示周六到周一）
                wrapped = set()
                cur = start
                while cur != end:
                    wrapped.add(_norm(cur, lo, hi))
                    cur = _norm(cur + 1, lo, hi)
                wrapped.add(_norm(end, lo, hi))
                values |= wrapped
                continue

        if start < lo or end > hi:
            raise CronParseError(f"{label} 字段越界 [{lo},{hi}]: {part!r}")
        values.update(range(start, end + 1, step))

    if not values:
        raise CronParseError(f"{label} 字段无有效取值: {raw!r}")
    return frozenset(_norm(v, lo, hi) for v in values), restricted


def _to_int(token: str, names: dict[str, int] | None, label: str) -> int:
    token = token.strip().lower()
    if names:
        key = token[:3]
        if key in names:
            return names[key]
    if not re.fullmatch(r"\d{1,2}", token):
        raise CronParseError(f"{label} 字段含非法值: {token!r}")
    return int(token)


def _norm(value: int, lo: int, hi: int) -> int:
    """day-of-week 的 7 归一为 0（周日）。"""
    if lo == 0 and hi == 7 and value == 7:
        return 0
    return value


def parse_cron(cron: str) -> CronSpec:
    """解析 5 段 cron 表达式。非法时抛 `CronParseError`。"""
    parts = str(cron or "").strip().split()
    if len(parts) != 5:
        raise CronParseError(f"cron 必须为 5 段（分 时 日 月 周），收到 {len(parts)} 段: {cron!r}")

    minutes, _ = _parse_field(parts[0], *_FIELD_SPECS[0][1:], None, "分钟")
    hours, _ = _parse_field(parts[1], *_FIELD_SPECS[1][1:], None, "小时")
    dom, dom_restricted = _parse_field(parts[2], *_FIELD_SPECS[2][1:], None, "日")
    month, _ = _parse_field(parts[3], *_FIELD_SPECS[3][1:], _MONTH_NAMES, "月")
    dow, dow_restricted = _parse_field(parts[4], *_FIELD_SPECS[4][1:], _DOW_NAMES, "周")

    return CronSpec(
        minutes=minutes, hours=hours, days_of_month=dom, months=month, days_of_week=dow,
        dom_restricted=dom_restricted, dow_restricted=dow_restricted,
    )


def is_valid_cron(cron: str) -> bool:
    try:
        parse_cron(cron)
        return True
    except CronParseError:
        return False


def _local_now() -> datetime:
    return datetime.now().astimezone()


def compute_next_run(cron: str, after: datetime | None = None) -> datetime | None:
    """推算 `after`（默认当前本地时刻）之后的第一个触发时刻，返回带时区的 datetime。

    逐日推进（而非逐分钟），避免"每年 2 月 29 日"这类稀疏表达式扫上百万次分钟。
    找不到（表达式永不命中，如 2 月 30 日）返回 None。
    """
    spec = parse_cron(cron)
    base = (after or _local_now()).replace(second=0, microsecond=0)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)

    times = spec.day_times()
    day = base.date()
    for _ in range(_MAX_SEARCH_DAYS):
        if spec.matches_date(day):
            floor_minutes = (base.hour * 60 + base.minute + 1) if day == base.date() else -1
            for hour, minute in times:
                if hour * 60 + minute < floor_minutes:
                    continue
                return datetime(day.year, day.month, day.day, hour, minute, tzinfo=base.tzinfo)
        day = day + timedelta(days=1)
    return None


def next_run_iso(cron: str, after: datetime | None = None) -> str | None:
    """`compute_next_run` 的落库形态（UTC ISO 字符串）。"""
    nxt = compute_next_run(cron, after)
    return nxt.astimezone(timezone.utc).isoformat() if nxt else None


def parse_iso(value: str | None) -> datetime | None:
    """宽容解析落库时间戳（历史数据存在无偏移的裸 ISO 串，按本地时区补齐）。"""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ── CRUD ──────────────────────────────────────────────────────────────

async def list_scheduled(db: AsyncSession) -> list[ScheduledTask]:
    res = await db.execute(select(ScheduledTask).order_by(ScheduledTask.id))
    return list(res.scalars().all())


async def get_scheduled(db: AsyncSession, task_id: int) -> ScheduledTask | None:
    return await db.get(ScheduledTask, task_id)


async def create_scheduled(db: AsyncSession, *, session_id: int, name: str,
                           cron: str, prompt: str) -> int:
    """创建任务。cron 非法抛 `CronParseError`（API 层转 422）。"""
    from app.persistence.database import run_write_locked

    spec_cron = " ".join(str(cron).strip().split())
    parse_cron(spec_cron)  # 校验，失败即抛
    nxt = next_run_iso(spec_cron)

    def patch(s):
        st = ScheduledTask(
            session_id=session_id, name=name, cron=spec_cron, prompt=prompt,
            enabled=True, next_run_at=nxt,
        )
        s.add(st)
        s.flush()
        sid = st.id
        s.commit()
        return sid

    return await run_write_locked(patch, label="scheduled.create")


async def update_scheduled(db: AsyncSession, task_id: int, **kwargs) -> bool:
    """更新任务字段。cron 变更时重算 next_run_at；enabled 由关到开同样重算。"""
    from app.persistence.database import run_write_locked

    cron = kwargs.get("cron")
    if cron:
        cron = " ".join(str(cron).strip().split())
        parse_cron(cron)
        kwargs["cron"] = cron
        kwargs["next_run_at"] = next_run_iso(cron)

    def patch(s):
        st = s.get(ScheduledTask, task_id)
        if st is None:
            return False
        recompute = False
        if "enabled" in kwargs:
            # 由禁用改为启用：旧 next_run_at 多半已过期，重算避免立即补触发
            if kwargs["enabled"] and not st.enabled:
                recompute = True
        for k, v in kwargs.items():
            if v is not None and hasattr(st, k):
                setattr(st, k, v)
        if recompute and not cron:
            st.next_run_at = next_run_iso(st.cron)
        s.commit()
        return True

    return await run_write_locked(patch, label=f"scheduled.update.{task_id}")


async def delete_scheduled(db: AsyncSession, task_id: int) -> bool:
    from app.persistence.database import run_write_locked

    def patch(s):
        rule = s.get(ScheduledTask, task_id)
        if rule is None:
            return False
        s.delete(rule)
        s.commit()
        return True

    return await run_write_locked(patch, label=f"scheduled.delete.{task_id}")


async def mark_run(task_id: int, *, status: str, next_run_at: str | None,
                   error: str | None = None) -> None:
    """记录一次触发结果并推进 next_run_at（走单写线程，无需外部 db 会话）。"""
    from app.persistence.database import run_write_locked

    now_iso = datetime.now(timezone.utc).isoformat()

    def patch(s):
        st = s.get(ScheduledTask, task_id)
        if st is None:
            return
        st.last_run_at = now_iso
        st.last_status = status
        st.last_error = (error or "")[:300] if error else None
        st.next_run_at = next_run_at
        s.commit()

    await run_write_locked(patch, label=f"scheduled.mark_run.{task_id}")


# 超过该时长才算"错过"（应用关闭期间跨过的触发点）。小于它视为正常抖动，照常执行。
def _missed_grace() -> timedelta:
    from app.core.config import settings
    return timedelta(seconds=max(30, int(getattr(settings, "scheduler_missed_grace_sec", 300) or 300)))


async def claim_due(db: AsyncSession, now: datetime | None = None,
                    limit: int = 5) -> list[dict]:
    """领取到点任务并**原子推进** next_run_at（防并发/重启重复触发）。

    返回任务快照 dict 列表（脱离 ORM 会话，供后台执行使用），每项含 `missed` 标记：
    触发点过期超过 `MISSED_GRACE` 时视为错过，由调用方按 `missed_policy` 决定
    补跑一次（run_once）还是跳过（skip）。

    依赖 `run_write_locked` 的单写者串行化：读-改-写在同一写事务内完成，
    两个 tick 不会领到同一行。
    """
    from app.persistence.database import run_write_locked

    now = now or _local_now()
    now_utc = now.astimezone(timezone.utc)

    def patch(s):
        rows = list(s.execute(
            select(ScheduledTask).where(
                ScheduledTask.enabled.is_(True),
                ScheduledTask.next_run_at.is_not(None),
            ).order_by(ScheduledTask.next_run_at).limit(limit)
        ).scalars().all())

        claimed: list[dict] = []
        for st in rows:
            due = parse_iso(st.next_run_at)
            if due is None or due > now_utc:
                continue
            # 先推进再返回：本行立即不再"到点"，天然幂等
            advanced = next_run_iso(st.cron, now)
            missed = (now_utc - due) > _missed_grace()
            st.next_run_at = advanced
            st.last_run_at = now_utc.isoformat()
            st.last_status = "skipped" if (missed and (st.missed_policy or "skip") == "skip") else "triggered"
            claimed.append({
                "id": st.id,
                "session_id": st.session_id,
                "name": st.name,
                "cron": st.cron,
                "prompt": st.prompt,
                "missed_policy": st.missed_policy or "skip",
                "missed": missed,
                "next_run_at": advanced,
            })
        if claimed:
            s.commit()
        else:
            s.rollback()
        return claimed

    return await run_write_locked(patch, label="scheduled.claim_due")


def iter_cron_minutes(spec: CronSpec) -> Iterable[tuple[int, int]]:
    """测试辅助：展开某天的合法时刻序列。"""
    return spec.day_times()
