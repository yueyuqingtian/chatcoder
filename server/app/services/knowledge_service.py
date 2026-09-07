"""知识库 CRUD 与业务逻辑。"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.knowledge import KnowledgeBase, KnowledgeDoc


async def create_knowledge_base(
    db: AsyncSession, *, name: str, kb_type: str = "project", tenant_id: int = 1
) -> int:
    from app.persistence.database import run_write_locked

    def patch(s):
        kb = KnowledgeBase(name=name, type=kb_type, tenant_id=tenant_id)
        s.add(kb)
        s.flush()
        kid = kb.id
        s.commit()
        return kid

    return await run_write_locked(patch, label="kb.create")


async def get_knowledge_base(db: AsyncSession, kb_id: int) -> KnowledgeBase | None:
    return await db.get(KnowledgeBase, kb_id)


async def list_knowledge_bases(db: AsyncSession, tenant_id: int = 1) -> list[KnowledgeBase]:
    res = await db.execute(
        select(KnowledgeBase)
        .where(KnowledgeBase.tenant_id == tenant_id)
        .order_by(KnowledgeBase.id.desc())
    )
    return list(res.scalars().all())


async def delete_knowledge_base(db: AsyncSession, kb_id: int) -> bool:
    from app.persistence.database import run_write_locked

    def patch(s):
        kb = s.get(KnowledgeBase, kb_id)
        if kb is None:
            return False
        s.delete(kb)
        s.commit()
        return True

    return await run_write_locked(patch, label=f"kb.delete.{kb_id}")


async def add_doc(
    db: AsyncSession,
    *,
    kb_id: int,
    title: str,
    content: str,
    meta: dict | None = None,
    vector_id: str | None = None,
) -> int:
    from app.persistence.database import run_write_locked

    def patch(s):
        doc = KnowledgeDoc(
            kb_id=kb_id,
            title=title,
            content=content,
            meta=meta,
            vector_id=vector_id,
        )
        s.add(doc)
        s.flush()
        did = doc.id
        s.commit()
        return did

    return await run_write_locked(patch, label="kb.doc.create")


async def get_doc(db: AsyncSession, doc_id: int) -> KnowledgeDoc | None:
    return await db.get(KnowledgeDoc, doc_id)


async def list_docs(db: AsyncSession, kb_id: int) -> list[KnowledgeDoc]:
    res = await db.execute(
        select(KnowledgeDoc)
        .where(KnowledgeDoc.kb_id == kb_id)
        .order_by(KnowledgeDoc.id.desc())
    )
    return list(res.scalars().all())


async def delete_doc(db: AsyncSession, doc_id: int) -> bool:
    from app.persistence.database import run_write_locked

    def patch(s):
        doc = s.get(KnowledgeDoc, doc_id)
        if doc is None:
            return False
        s.delete(doc)
        s.commit()
        return True

    return await run_write_locked(patch, label=f"kb.doc.delete.{doc_id}")


async def search_docs_by_keyword(
    db: AsyncSession, kb_id: int, keyword: str, limit: int = 20
) -> list[KnowledgeDoc]:
    """简单关键词检索（MVP 阶段用，后续接向量检索）。"""
    keyword_lower = f"%{keyword.lower()}%"
    res = await db.execute(
        select(KnowledgeDoc)
        .where(
            KnowledgeDoc.kb_id == kb_id,
            (KnowledgeDoc.title.ilike(keyword_lower))
            | (KnowledgeDoc.content.ilike(keyword_lower)),
        )
        .limit(limit)
    )
    return list(res.scalars().all())
