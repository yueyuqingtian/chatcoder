"""记忆管线服务（D8：写入/使用/整合）。"""
import logging
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.memory import MemoryEntry

logger = logging.getLogger(__name__)

_TOP_N = 10


async def save_memories(db: AsyncSession, *, session_id: int, turn_id: int | None,
                        memories: list[dict]) -> int:
    """批量写入记忆（memory_entries 表；去重 + 写引擎单写线程）。"""
    count = 0
    _new_entries: list = []
    for m in memories:
        text = str(m.get("text", "")).strip()
        if not text or len(text) < 8:
            continue
        try:
            importance = float(m.get("importance", 1.0))
        except (TypeError, ValueError):
            importance = 0.0
        if importance < 0.65:
            continue
        kind = str(m.get("kind", "fact"))
        if kind not in ("fact", "convention", "pitfall", "decision"):
            kind = "fact"
        normalized = " ".join(text.casefold().split())
        existing = await db.execute(select(MemoryEntry).where(MemoryEntry.session_id == session_id))
        if any(" ".join(str(entry.text).casefold().split()) == normalized for entry in existing.scalars()):
            continue
        _new_entries.append(MemoryEntry(session_id=session_id, turn_id=turn_id, text=text[:500], kind=kind))
        count += 1
    if _new_entries:
        from app.persistence.database import run_write_locked

        def _persist(s):
            for e in _new_entries:
                s.add(e)
            s.commit()

        await run_write_locked(_persist, label="memory.consolidate_add")
    return count


async def load_memories(db: AsyncSession, session_id: int, top_n: int = _TOP_N) -> list[MemoryEntry]:
    """按使用频次降序 + 最近使用排序取 top-N，并累计使用计数。"""
    res = await db.execute(
        select(MemoryEntry).where(MemoryEntry.session_id == session_id)
        .order_by(MemoryEntry.usage_count.desc(), MemoryEntry.generated_at.desc())
        .limit(top_n)
    )
    entries = list(res.scalars().all())
    if entries:
        from sqlalchemy import func
        ids = [e.id for e in entries]
        from app.persistence.database import run_write_locked

        def _persist(s):
            rows = list(s.execute(
                select(MemoryEntry).where(MemoryEntry.id.in_(ids))
            ).scalars().all())
            for r in rows:
                r.usage_count = (r.usage_count or 0) + 1
                r.last_usage_at = str(func.now())
            s.commit()

        await run_write_locked(_persist, label="memory.usage_touch")
    return entries


async def list_memories(db: AsyncSession, session_id: int | None = None) -> list[MemoryEntry]:
    stmt = select(MemoryEntry).order_by(MemoryEntry.usage_count.desc(), MemoryEntry.generated_at.desc())
    if session_id is not None:
        stmt = stmt.where(MemoryEntry.session_id == session_id)
    res = await db.execute(stmt.limit(200))
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


async def consolidate(db: AsyncSession, session_id: int) -> str:
    """整合记忆 → 写 MEMORY.md（简版：汇总全部记忆文本，LLM 整合为异步可选）。

    返回生成的 MEMORY.md 内容（存于项目 .chatcoder/memory/ 目录）。
    """
    entries = await list_memories(db, session_id)
    if not entries:
        return ""
    sections: dict[str, list[str]] = {}
    for e in entries:
        sections.setdefault(e.kind, []).append(f"- {e.text}")
    lines = ["# Project Memory", ""]
    kind_label = {"fact": "Facts", "convention": "Conventions", "pitfall": "Pitfalls", "decision": "Decisions"}
    for kind, items in sections.items():
        lines.append(f"## {kind_label.get(kind, kind)}")
        lines.extend(items)
        lines.append("")
    return "\n".join(lines)


def write_memory_file(project_path: str, content: str) -> str:
    """落地 .chatcoder/memory/MEMORY.md，返回路径。"""
    memory_dir = Path(project_path) / ".chatcoder" / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    target = memory_dir / "MEMORY.md"
    target.write_text(content, encoding="utf-8")
    return str(target)
