"""记忆管线服务（D8：写入/使用/整合）。

plan-230-1144 M4.1 三层化改造：
- **作用域**：global（跨项目规范/偏好）/ project（项目约定）/ session（会话事实），
  读取时按 session → project → global 合并注入，替代原先的扁平 top-N；
- **候选区**：低置信记忆（importance < 0.65）不再直接丢弃，降级为 candidate
  保存——不注入 prompt，但可被 memory_search 检索，避免高价值低置信信息永久丢失；
- **提升**：promote_memory 支持把会话记忆升级为 project/global 记忆；
- **去重**：从逐条 select 全表比对（O(n²)）改为一次性载入内存集合（O(n)）；
- **老化**：expires_at 到期的记忆读取时跳过。
"""
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.memory import MemoryEntry

logger = logging.getLogger(__name__)

# 注入预算（plan-537-2256）：三层各自限量，合计最多 12 条。
# 早前三层都用同一个 top_n，全局规范很容易刷满上限、把更具体的会话事实挤掉。
_SESSION_LIMIT = 5
_PROJECT_LIMIT = 4
_GLOBAL_LIMIT = 3
_KINDS = ("fact", "convention", "pitfall", "decision")
_SCOPES = ("session", "project", "global")
# candidate 门槛：低于该 importance 的记忆降级为候选（不丢弃）。
# plan-537-2256：提取提示词要求 importance 取 0.4–1.0，低于 0.65 落候选区。
_CANDIDATE_THRESHOLD = 0.65
# 会话记忆默认存活时间（天）；project/global 不设过期
_SESSION_TTL_DAYS = 30


def _normalize_text(text: str) -> str:
    """去重键归一化：大小写折叠 + 空白折叠。调用方必须传入已截断的文本。"""
    return " ".join(str(text or "").casefold().split())


def _normalize_kind(raw) -> str:
    kind = str(raw or "fact")
    return kind if kind in _KINDS else "fact"


def _normalize_scope(raw) -> str:
    scope = str(raw or "session")
    return scope if scope in _SCOPES else "session"


async def save_memories(db: AsyncSession, *, session_id: int, turn_id: int | None,
                        memories: list[dict], project_id: int | None = None) -> int:
    """批量写入记忆（按目标作用域去重 + 写引擎单写线程）。

    memories 每项：{text, importance?, kind?, scope?}。
    importance < 0.65 不再丢弃而是以 candidate=True 保存（可检索、不注入）。
    scope=project 的条目挂 `project_id`；项目为空时降级为会话级（与 memory_write 同口径）。

    plan-537-2256：
    - 开关必须挡在**唯一写入口**（本函数），只在调用方检查会绕过用户设置；
    - 去重按目标 scope 分组，且用**截断后**的文本算键，修「超长文本每次插入都被当新记忆」；
    - 查重与插入放在同一个写事务内，避免并发调用各自查重后双双插入。
    """
    from app.core.config import settings

    if not getattr(settings, "auto_memory_enabled", True):
        return 0

    # 1) 先规范化：text.strip() → text[:500]，kind/scope 归一，scope=project 但无项目则降级
    pending: list[dict] = []
    for m in memories:
        raw = str(m.get("text", "")).strip()
        if len(raw) < 8:
            continue
        try:
            importance = float(m.get("importance", 1.0))
        except (TypeError, ValueError):
            importance = 0.0
        text = raw[:500]
        scope = _normalize_scope(m.get("scope"))
        if scope == "project" and project_id is None:
            scope = "session"
        pending.append({
            "text": text,
            "kind": _normalize_kind(m.get("kind")),
            "scope": scope,
            "candidate": importance < _CANDIDATE_THRESHOLD,
            "key": _normalize_text(text),  # 用截断后的文本算去重键
        })
    if not pending:
        return 0

    from app.persistence.database import run_write_locked

    def _persist(s) -> int:
        def _load_seen(stmt) -> set[str]:
            return {_normalize_text(t) for (t,) in s.execute(stmt).all()}

        scopes = {p["scope"] for p in pending}
        seen: dict[str, set[str]] = {}
        if "session" in scopes:
            # 会话层：本会话的 scope=session（历史空值兼容为 session）
            seen["session"] = _load_seen(select(MemoryEntry.text).where(
                MemoryEntry.session_id == session_id,
                or_(MemoryEntry.scope == "session", MemoryEntry.scope.is_(None)),
            ))
        if "project" in scopes and project_id is not None:
            seen["project"] = _load_seen(select(MemoryEntry.text).where(
                MemoryEntry.scope == "project",
                MemoryEntry.project_id == project_id,
            ))
        if "global" in scopes:
            seen["global"] = _load_seen(select(MemoryEntry.text).where(
                MemoryEntry.scope == "global",
            ))

        added = 0
        for p in pending:
            bucket = seen.setdefault(p["scope"], set())
            if p["key"] in bucket:  # 库内重复 + 同批内重复都在这拦下
                continue
            bucket.add(p["key"])
            expires_at = None
            if p["scope"] == "session":
                expires_at = (datetime.now(timezone.utc)
                              + timedelta(days=_SESSION_TTL_DAYS)).isoformat()
            s.add(MemoryEntry(
                session_id=session_id, turn_id=turn_id, text=p["text"], kind=p["kind"],
                scope=p["scope"], project_id=(project_id if p["scope"] == "project" else None),
                candidate=p["candidate"], expires_at=expires_at,
            ))
            added += 1
        if added:
            s.commit()
        return added

    return int(await run_write_locked(_persist, label="memory.save"))


def _is_expired(entry: MemoryEntry) -> bool:
    if not entry.expires_at:
        return False
    try:
        return datetime.fromisoformat(str(entry.expires_at).replace("Z", "+00:00")) < datetime.now(timezone.utc)
    except ValueError:
        return False


async def load_memories(db: AsyncSession, session_id: int, *,
                        session_limit: int = 5, project_limit: int = 4, global_limit: int = 3,
                        project_id: int | None = None, top_n: int | None = None) -> list[MemoryEntry]:
    """三层合并读取：session（本会话）→ project（项目）→ global（全局）。

    plan-537-2256：分层预算——各层独立取值（默认 5/4/3，合计最多 12 条），
    避免「全局规范刷满 top_n」把更具体的会话事实挤掉；同层内也按归一化文本去重。
    排序为 usage_count 降序 + generated_at 降序：**注入不算使用**，本函数不写任何字段，
    否则「每轮注入都刷新排序键/计数」会让头部自我强化、老记忆永久锁位，
    且顺序逐轮抖动会破坏提示词缓存。真实使用计数由 memory_search 命中时累加。

    兼容：`top_n` 非空时覆盖三个分层上限（旧调用点行为不变）。
    """
    if top_n is not None:
        session_limit = project_limit = global_limit = int(top_n)

    def _base_stmt():
        return select(MemoryEntry).where(
            MemoryEntry.candidate.is_(False),
            (MemoryEntry.superseded_by.is_(None)),
        )

    def _merge(rows: list[MemoryEntry], seen: set[str]) -> None:
        """同层 + 跨层按归一化文本去重后并入结果。"""
        for e in _active(rows):
            key = _normalize_text(e.text)
            if key in seen:
                continue
            seen.add(key)
            entries.append(e)

    entries: list[MemoryEntry] = []
    seen: set[str] = set()

    # 1. 会话层（仅 scope=session 的本会话条目；project/global 由下方各自层负责，
    #    避免"从本会话提升为 global"的条目混入会话层造成重复）
    if session_limit > 0:
        res = await db.execute(
            _base_stmt().where(
                MemoryEntry.session_id == session_id,
                (MemoryEntry.scope == "session") | (MemoryEntry.scope.is_(None)),
            )
            .order_by(MemoryEntry.usage_count.desc(), MemoryEntry.generated_at.desc())
            .limit(session_limit)
        )
        _merge([e for e in res.scalars().all()], seen)

    # 2. 项目层
    if project_id is not None and project_limit > 0:
        res = await db.execute(
            _base_stmt().where(MemoryEntry.scope == "project", MemoryEntry.project_id == project_id)
            .order_by(MemoryEntry.usage_count.desc(), MemoryEntry.generated_at.desc())
            .limit(project_limit)
        )
        _merge([x for x in res.scalars().all()], seen)

    # 3. 全局层
    if global_limit > 0:
        res = await db.execute(
            _base_stmt().where(MemoryEntry.scope == "global")
            .order_by(MemoryEntry.usage_count.desc(), MemoryEntry.generated_at.desc())
            .limit(global_limit)
        )
        _merge([x for x in res.scalars().all()], seen)

    return entries


def _active(entries: list[MemoryEntry]) -> list[MemoryEntry]:
    return [e for e in entries if not _is_expired(e)]


async def list_memories(db: AsyncSession, session_id: int | None = None,
                        scope: str | None = None, project_id: int | None = None,
                        include_candidate: bool = True, q: str | None = None,
                        limit: int = 500) -> list[MemoryEntry]:
    """列出记忆（管理面板用）。可按 scope/project 过滤；默认含候选区。

    plan-memory-fix：列表有条数上限，关键词搜索放到服务端做，
    否则上限之外的记忆永远搜不到。"""
    stmt = select(MemoryEntry).order_by(MemoryEntry.usage_count.desc(), MemoryEntry.generated_at.desc())
    if q and q.strip():
        stmt = stmt.where(MemoryEntry.text.contains(q.strip()))
    if session_id is not None:
        stmt = stmt.where(MemoryEntry.session_id == session_id)
    if scope:
        stmt = stmt.where(MemoryEntry.scope == scope)
    if project_id is not None:
        stmt = stmt.where(MemoryEntry.project_id == project_id)
    if not include_candidate:
        stmt = stmt.where(MemoryEntry.candidate.is_(False))
    res = await db.execute(stmt.limit(max(1, min(limit, 2000))))
    return list(res.scalars().all())


async def count_memories(db: AsyncSession, include_candidate: bool = True) -> dict[str, int]:
    """按作用域统计记忆总数（plan-memory-fix）。

    作用域页签的计数必须与当前筛选无关；此前前端用"当前已加载列表"现算，
    切换页签后其它作用域的计数会变成 0，且全部视图受列表上限截断。"""
    stmt = select(MemoryEntry.scope, func.count()).group_by(MemoryEntry.scope)
    if not include_candidate:
        stmt = stmt.where(MemoryEntry.candidate.is_(False))
    res = await db.execute(stmt)
    out = {"global": 0, "project": 0, "session": 0}
    for scope, n in res.all():
        key = scope if scope in out else "session"
        out[key] += int(n)
    out["all"] = out["global"] + out["project"] + out["session"]
    return out


async def delete_memory(db: AsyncSession, memory_id: int) -> bool:
    from app.persistence.database import run_write_locked

    def patch(s):
        entry = s.get(MemoryEntry, memory_id)
        if entry is None:
            return False
        s.delete(entry)
        s.commit()
        return True

    return await run_write_locked(patch, label=f"memory.delete.{memory_id}")


async def update_memory(db: AsyncSession, memory_id: int, *, text: str | None = None,
                       kind: str | None = None) -> bool:
    """编辑记忆文本/类型（S8 / plan-41-197）。

    用户可在设置-记忆的详情弹窗里修正记忆文本；编辑即视为人工确认，
    候选标记随之清除（与 promote 同一语义）。
    """
    if text is not None and not text.strip():
        raise ValueError("记忆内容不能为空")

    from app.persistence.database import run_write_locked

    # plan-537-2256：编辑与写入复用同一归一化口径——文本截断 500、
    # kind 非法时忽略该参数（保持原值），绝不把非法枚举写进库。
    new_text = text.strip()[:500] if text is not None else None
    new_kind = kind if kind in _KINDS else None

    def patch(s):
        entry = s.get(MemoryEntry, memory_id)
        if entry is None:
            return False
        if new_text is not None:
            entry.text = new_text
            entry.candidate = False
        if new_kind is not None:
            entry.kind = new_kind
        s.commit()
        return True

    return await run_write_locked(patch, label=f"memory.update.{memory_id}")


async def promote_memory(db: AsyncSession, memory_id: int, target_scope: str,
                         project_id: int | None = None) -> bool:
    """把记忆提升/降级到目标作用域（session → project → global）。

    plan-537-2256：
    - 提升为 project 但调用方没给 project_id 时，按记忆的来源会话反查其所属项目，
      反查不到才报错（此前直接要求前端传 project_id，项目内的会话也提升不了）；
    - 反查放在调用 run_write_locked **之前**，写线程里不做额外查询；
    - 转到 session/global 时清空 project_id，避免残留旧项目的脏值让列表筛选串项目。
    """
    if target_scope not in _SCOPES:
        raise ValueError("target_scope 必须为 session/project/global")
    if target_scope == "project" and project_id is None:
        from app.persistence.models.message import Session

        row = (await db.execute(
            select(MemoryEntry.session_id).where(MemoryEntry.id == memory_id)
        )).first()
        source_session_id = int(row[0]) if row and row[0] is not None else None
        if source_session_id is not None:
            project_id = (await db.execute(
                select(Session.project_id).where(Session.id == source_session_id)
            )).scalar()
        if project_id is None:
            raise ValueError(
                "该记忆来源会话未绑定项目，无法提升为项目记忆，请在项目内的会话中提升或先绑定项目"
            )
        project_id = int(project_id)

    from app.persistence.database import run_write_locked

    def patch(s):
        entry = s.get(MemoryEntry, memory_id)
        if entry is None:
            return False
        entry.scope = target_scope
        entry.project_id = project_id if target_scope == "project" else None
        if target_scope == "session":
            entry.expires_at = (datetime.now(timezone.utc) + timedelta(days=_SESSION_TTL_DAYS)).isoformat()
        else:
            entry.expires_at = None  # 项目/全局不过期
        entry.candidate = False  # 人工提升/降级视为已确认
        s.commit()
        return True

    return await run_write_locked(patch, label=f"memory.promote.{memory_id}")


async def consolidate(db: AsyncSession, session_id: int) -> str:
    """整合记忆 → 写 MEMORY.md（汇总三层记忆文本）。"""
    entries = await list_memories(db, session_id, include_candidate=False)
    if not entries:
        return ""
    sections: dict[str, list[str]] = {}
    for e in entries:
        scope_tag = {"global": "全局", "project": "项目"}.get(e.scope or "session", "")
        key = f"## {e.kind}" + (f"（{scope_tag}）" if scope_tag else "")
        sections.setdefault(key, []).append(f"- {e.text}")
    lines = ["# Project Memory", ""]
    kind_label = {"fact": "Facts", "convention": "Conventions", "pitfall": "Pitfalls", "decision": "Decisions"}
    merged: dict[str, list[str]] = {}
    for key, items in sections.items():
        for k in kind_label:
            if key.startswith(f"## {k}"):
                merged[key.replace(f"## {k}", f"## {kind_label[k]}")] = items
                break
        else:
            merged[key] = items
    for key, items in merged.items():
        lines.append(key)
        lines.extend(items)
        lines.append("")
    return "\n".join(lines)


async def consolidate_memories(db: AsyncSession, *, session_id: int,
                               project_id: int | None = None) -> dict:
    """惰性整理记忆库（只做 DB 语义整理，不调用 LLM、不物理删行）。

    plan-537-2256 两条规则：
      1. 同作用域内归一化文本完全相同的多行 → 保留 usage_count 最高（相同取 id 最小）
         者，其余置 `superseded_by = 保留行 id`；
      2. 会话记忆（scope=session）已过期且尚未被取代的行 → `superseded_by = -1`。

    作用域边界：session 只限本会话；project 只限同 project_id；global 全表。
    整个流程在同一个写事务内完成，被取代/过期的行不再参与判断，故重复执行不产生新变更（幂等）。
    返回 {"merged": 被合并行数, "expired": 被判定过期的行数}。
    """
    from app.persistence.database import run_write_locked

    def _persist(s) -> dict:
        merged = 0
        expired = 0
        # 规则一：分作用域收集（一次性载入，避免逐行查询）
        buckets: dict[tuple[str, int | None], dict[str, list[MemoryEntry]]] = {}
        rows = s.execute(select(MemoryEntry).where(MemoryEntry.superseded_by.is_(None))).scalars().all()
        for e in rows:
            scope = e.scope or "session"
            if scope == "session":
                if e.session_id != session_id:
                    continue
                group = (scope, None)
            elif scope == "project":
                if project_id is None or e.project_id != project_id:
                    continue
                group = (scope, e.project_id)
            else:
                group = (scope, None)
            buckets.setdefault(group, {}).setdefault(_normalize_text(e.text), []).append(e)

        for items in buckets.values():
            for group in items.values():
                if len(group) < 2:
                    continue
                keep = min(group, key=lambda x: (-(x.usage_count or 0), x.id))
                for e in group:
                    if e.id == keep.id:
                        continue
                    e.superseded_by = keep.id
                    merged += 1

        # 规则二：会话记忆过期即退出有效集（不删行，只打标）
        now = datetime.now(timezone.utc)
        for e in s.execute(select(MemoryEntry).where(
            MemoryEntry.scope == "session",
            MemoryEntry.superseded_by.is_(None),
            MemoryEntry.expires_at.is_not(None),
        )).scalars().all():
            if _is_expired(e):
                e.superseded_by = -1
                expired += 1

        if merged or expired:
            s.commit()
        return {"merged": merged, "expired": expired}

    result = await run_write_locked(_persist, label="memory.consolidate_semantic")
    return result or {"merged": 0, "expired": 0}


def write_memory_file(project_path: str, content: str) -> str:
    """落地 .chatcoder/memory/MEMORY.md，返回路径。"""
    target = Path(project_path) / ".chatcoder" / "memory"
    target.mkdir(parents=True, exist_ok=True)
    path = target / "MEMORY.md"
    path.write_text(content, encoding="utf-8")
    return str(path)