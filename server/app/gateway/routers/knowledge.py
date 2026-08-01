"""知识库 API。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.schemas import (
    KnowledgeBaseCreate,
    KnowledgeBaseOut,
    KnowledgeDocCreate,
    KnowledgeDocOut,
)
from app.persistence.database import get_db
from app.services import knowledge_service

router = APIRouter()


@router.post("/knowledge-bases", response_model=KnowledgeBaseOut)
async def create_kb(body: KnowledgeBaseCreate, db: AsyncSession = Depends(get_db)):
    kb = await knowledge_service.create_knowledge_base(
        db, name=body.name, kb_type=body.type
    )
    await db.commit()
    return KnowledgeBaseOut(id=kb.id, name=kb.name or "", type=kb.type or "")


@router.get("/knowledge-bases", response_model=list[KnowledgeBaseOut])
async def list_kbs(db: AsyncSession = Depends(get_db)):
    kbs = await knowledge_service.list_knowledge_bases(db)
    return [
        KnowledgeBaseOut(id=kb.id, name=kb.name or "", type=kb.type or "")
        for kb in kbs
    ]


@router.get("/knowledge-bases/{kb_id}", response_model=KnowledgeBaseOut)
async def get_kb(kb_id: int, db: AsyncSession = Depends(get_db)):
    kb = await knowledge_service.get_knowledge_base(db, kb_id)
    if kb is None:
        raise HTTPException(404, "knowledge base not found")
    return KnowledgeBaseOut(id=kb.id, name=kb.name or "", type=kb.type or "")


@router.delete("/knowledge-bases/{kb_id}")
async def delete_kb(kb_id: int, db: AsyncSession = Depends(get_db)):
    ok = await knowledge_service.delete_knowledge_base(db, kb_id)
    if not ok:
        raise HTTPException(404, "knowledge base not found")
    await db.commit()
    return {"ok": True}


@router.post("/knowledge-bases/{kb_id}/docs", response_model=KnowledgeDocOut)
async def add_doc(
    kb_id: int, body: KnowledgeDocCreate, db: AsyncSession = Depends(get_db)
):
    kb = await knowledge_service.get_knowledge_base(db, kb_id)
    if kb is None:
        raise HTTPException(404, "knowledge base not found")
    doc = await knowledge_service.add_doc(
        db,
        kb_id=kb_id,
        title=body.title,
        content=body.content,
        meta=body.meta,
    )
    await db.commit()
    return KnowledgeDocOut(
        id=doc.id,
        kb_id=doc.kb_id or 0,
        title=doc.title or "",
        content=doc.content or "",
        meta=doc.meta,
        created_at=doc.created_at if hasattr(doc, "created_at") else None,
    )


@router.get("/knowledge-bases/{kb_id}/docs", response_model=list[KnowledgeDocOut])
async def list_docs(kb_id: int, db: AsyncSession = Depends(get_db)):
    docs = await knowledge_service.list_docs(db, kb_id)
    return [
        KnowledgeDocOut(
            id=d.id,
            kb_id=d.kb_id or 0,
            title=d.title or "",
            content=d.content or "",
            meta=d.meta,
            created_at=d.created_at if hasattr(d, "created_at") else None,
        )
        for d in docs
    ]


@router.get("/knowledge-bases/{kb_id}/docs/search", response_model=list[KnowledgeDocOut])
async def search_docs(
    kb_id: int, q: str, limit: int = 20, db: AsyncSession = Depends(get_db)
):
    docs = await knowledge_service.search_docs_by_keyword(db, kb_id, q, limit)
    return [
        KnowledgeDocOut(
            id=d.id,
            kb_id=d.kb_id or 0,
            title=d.title or "",
            content=d.content or "",
            meta=d.meta,
            created_at=d.created_at if hasattr(d, "created_at") else None,
        )
        for d in docs
    ]


@router.get("/knowledge-bases/{kb_id}/docs/{doc_id}", response_model=KnowledgeDocOut)
async def get_doc(kb_id: int, doc_id: int, db: AsyncSession = Depends(get_db)):
    doc = await knowledge_service.get_doc(db, doc_id)
    if doc is None or doc.kb_id != kb_id:
        raise HTTPException(404, "doc not found")
    return KnowledgeDocOut(
        id=doc.id,
        kb_id=doc.kb_id or 0,
        title=doc.title or "",
        content=doc.content or "",
        meta=doc.meta,
        created_at=doc.created_at if hasattr(doc, "created_at") else None,
    )


@router.delete("/knowledge-bases/{kb_id}/docs/{doc_id}")
async def delete_doc(kb_id: int, doc_id: int, db: AsyncSession = Depends(get_db)):
    doc = await knowledge_service.get_doc(db, doc_id)
    if doc is None or doc.kb_id != kb_id:
        raise HTTPException(404, "doc not found")
    ok = await knowledge_service.delete_doc(db, doc_id)
    await db.commit()
    return {"ok": ok}
