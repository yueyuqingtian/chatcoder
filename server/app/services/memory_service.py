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

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.memory import MemoryEntry

logger = logging.getLogger(__name__)

_TOP_N = 10
# candidate 门槛：低于该 importance 的记忆降级为候选（不丢弃）
_CANDIDATE_THRESHOLD = 0.65
# 会话记忆默认存活时间（天）；project/global 不设过期
_SESSION_TTL_DAYS = 30


async def save_memories(db: AsyncSession, *, session_id: int, turn_id: int | None,
                        memories: list[dict], project_id: int | None = None) -> int:
    """批量写入记忆（去重 + 写引擎单写线程）。

    memories 每项：{text, importance?, kind?, scope?}。
    importance < 0.65 不再丢弃而是以 candidate=True 保存（可检索、不注入）。
    scope=project 的条目挂 `project_id`（memory_write 工具传入）。
    """
    count = 0
    _new_entries: list = []
    # O(n) 去重：一次性载入候选会话的现有记忆文本集合
    existing = await db.execute(select(MemoryEntry.text).where(MemoryEntry.session_id == session_id))
    seen = {" ".join(str(t).casefold().split()) for (t,) in existing.all()}

    for m in memories:
        text = str(m.get("text", "")).strip()
        if not text or len(text) < 8:
            continue
        try:
            importance = float(m.get("importance", 1.0))
        except (TypeError, ValueError):
            importance = 0.0
        kind = str(m.get("kind", "fact"))
        if kind not in ("fact", "convention", "pitfall", "decision"):
            kind = "fact"
        scope = str(m.get("scope") or "session")
        if scope not in ("session", "project", "global"):
            scope = "session"
        normalized = " ".join(text.casefold().split())
        if normalized in seen:
            continue
        seen.add(normalized)
        expires_at = None
        if scope == "session":
            expires_at = (datetime.now(timezone.utc) + timedelta(days=_SESSION_TTL_DAYS)).isoformat()
        _new_entries.append(MemoryEntry(
            session_id=session_id, turn_id=turn_id, text=text[:500], kind=kind,
            scope=scope, project_id=(project_id if scope == "project" else None),
            candidate=(importance < _CANDIDATE_THRESHOLD),
            expires_at=expires_at,
        ))
        count += 1
    if _new_entries:
        from app.persistence.database import run_write_locked

        def _persist(s):
            for e in _new_entries:
                s.add(e)
            s.commit()

        await run_write_locked(_persist, label="memory.consolidate_add")
    return count


def _is_expired(entry: MemoryEntry) -> bool:
    if not entry.expires_at:
        return False
    try:
        return datetime.fromisoformat(str(entry.expires_at).replace("Z", "+00:00")) < datetime.now(timezone.utc)
    except ValueError:
        return False


async def load_memories(db: AsyncSession, session_id: int, top_n: int = _TOP_N,
                        project_id: int | None = None) -> list[MemoryEntry]:
    """三层合并读取：session（本会话）→ project（项目）→ global（全局）。

    各层按使用频次降序 + 最近生成排序取 top_n；跳过候选区与过期项。
    返回顺序：session 在前（更具体优先），供 prompt 分层渲染。
    """
    from sqlalchemy import func

    def _base_stmt():
        return select(MemoryEntry).where(
            MemoryEntry.candidate.is_(False),
            (MemoryEntry.superseded_by.is_(None)),
        )

    entries: list[MemoryEntry] = []

    # 1. 会话层（仅 scope=session 的本会话条目；project/global 由下方各自层负责，
    #    避免"从本会话提升为 global"的条目混入会话层造成重复）
    res = await db.execute(
        _base_stmt().where(
            MemoryEntry.session_id == session_id,
            (MemoryEntry.scope == "session") | (MemoryEntry.scope.is_(None)),
        )
        .order_by(MemoryEntry.usage_count.desc(), MemoryEntry.generated_at.desc())
        .limit(top_n)
    )
    entries.extend(_active([e for e in res.scalars().all()]))

    # 2. 项目层（session 已含的文本去重）
    if project_id is not None:
        res = await db.execute(
            _base_stmt().where(MemoryEntry.scope == "project", MemoryEntry.project_id == project_id)
            .order_by(MemoryEntry.usage_count.desc(), MemoryEntry.generated_at.desc())
            .limit(top_n)
        )
        seen = {" ".join(e.text.casefold().split()) for e in entries}
        for e in _active([x for x in res.scalars().all()]):
            key = " ".join(e.text.casefold().split())
            if key not in seen:
                seen.add(key)
                entries.append(e)

    # 3. 全局层
    res = await db.execute(
        _base_stmt().where(MemoryEntry.scope == "global")
        .order_by(MemoryEntry.usage_count.desc(), MemoryEntry.generated_at.desc())
        .limit(top_n)
    )
    seen = {" ".join(e.text.casefold().split()) for e in entries}
    for e in _active([x for x in res.scalars().all()]):
        key = " ".join(e.text.casefold().split())
        if key not in seen:
            seen.add(key)
            entries.append(e)

    # 使用计数回写（单写线程批量）
    if entries:
        ids = [e.id for e in entries]
        from app.persistence.database import run_write_locked

        def _persist(s):
            rows = list(s.execute(select(MemoryEntry).where(MemoryEntry.id.in_(ids))).scalars().all())
            for r in rows:
                r.usage_count = (r.usage_count or 0) + 1
                r.last_usage_at = str(func.now())
            s.commit()

        await run_write_locked(_persist, label="memory.usage_touch")
    return entries


def _active(entries: list[MemoryEntry]) -> list[MemoryEntry]:
    return [e for e in entries if not _is_expired(e)]


async def list_memories(db: AsyncSession, session_id: int | None = None,
                        scope: str | None = None, project_id: int | None = None,
                        include_candidate: bool = True) -> list[MemoryEntry]:
    """列出记忆（管理面板用）。可按 scope/project 过滤；默认含候选区。"""
    stmt = select(MemoryEntry).order_by(MemoryEntry.usage_count.desc(), MemoryEntry.generated_at.desc())
    if session_id is not None:
        stmt = stmt.where(MemoryEntry.session_id == session_id)
    if scope:
        stmt = stmt.where(MemoryEntry.scope == scope)
    if project_id is not None:
        stmt = stmt.where(MemoryEntry.project_id == project_id)
    if not include_candidate:
        stmt = stmt.where(MemoryEntry.candidate.is_(False))
    res = await db.execute(stmt.limit(500))
    return list(res.scalars().all())


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

    def patch(s):
        entry = s.get(MemoryEntry, memory_id)
        if entry is None:
            return False
        if text is not None:
            entry.text = text.strip()
            entry.candidate = False
        if kind is not None:
            entry.kind = kind
        s.commit()
        return True

    return await run_write_locked(patch, label=f"memory.update.{memory_id}")


async def promote_memory(db: AsyncSession, memory_id: int, target_scope: str,
                         project_id: int | None = None) -> bool:
    """把记忆提升/降级到目标作用域（session → project → global）。

    project 目标需提供 project_id；提升后清除候选标记与过期时间
    （用户显式提升 = 人工确认，具备长期价值）。
    """
    if target_scope not in ("session", "project", "global"):
        raise ValueError("target_scope 必须为 session/project/global")
    if target_scope == "project" and project_id is None:
        raise ValueError("提升为项目记忆需提供 project_id")

    from app.persistence.database import run_write_locked

    def patch(s):
        entry = s.get(MemoryEntry, memory_id)
        if entry is None:
            return False
        entry.scope = target_scope
        if target_scope == "project":
            entry.project_id = project_id
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


def write_memory_file(project_path: str, content: str) -> str:
    """落地 .chatcoder/memory/MEMORY.md，返回路径。"""
    target = Path(project_path) / ".chatcoder" / "memory"
    target.mkdir(parents=True, exist_ok=True)
    path = target / "MEMORY.md"
    path.write_text(content, encoding="utf-8")
    return str(path)