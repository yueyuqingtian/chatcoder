"""知识库。embedding 在客户端生成，仅 vector_id 指向 Qdrant。"""
from sqlalchemy import BigInteger, ForeignKey, Integer, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    tenant_id: Mapped[int | None] = mapped_column(BigInteger)
    name: Mapped[str | None] = mapped_column(String(120))
    type: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[str] = mapped_column(server_default=func.now())


class KnowledgeDoc(Base):
    __tablename__ = "knowledge_docs"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    kb_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("knowledge_bases.id"))
    title: Mapped[str | None] = mapped_column(String(200))
    content: Mapped[str | None]
    vector_id: Mapped[str | None] = mapped_column(String(80))
    meta: Mapped[dict | None] = mapped_column(JSON)
