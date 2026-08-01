"""RAG 检索层：从知识库中检索相关文档并注入 agent 上下文。

MVP 阶段使用关键词检索，后续可扩展为向量检索（Qdrant/FAISS）。
"""
import logging
import re
from typing import TYPE_CHECKING

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.knowledge import KnowledgeBase, KnowledgeDoc

if TYPE_CHECKING:
    from app.persistence.models.task import Task

logger = logging.getLogger(__name__)

_RAG_RESULT_LIMIT = 3
_RAG_CONTENT_MAX_LENGTH = 500


async def _search_all_kbs(db: AsyncSession, query: str, tenant_id: int = 1) -> list[KnowledgeDoc]:
    """跨知识库检索：在所有知识库中搜索关键词。

    v3.1: 支持多关键词组合检索。将查询拆分为多个关键词，
    每个关键词独立 ILIKE，用 OR 组合，提升召回率。
    例如 "登录验证" → 拆为 ["登录", "验证"]，任一匹配即返回。
    """
    if not query.strip():
        return []

    # v3.1: 多关键词拆分 —— 中文按 2 字滑窗 + 英文按空格/词边界
    keywords = _extract_keywords(query)
    if not keywords:
        keywords = [query.strip()]

    conditions = []
    for kw in keywords:
        pattern = f"%{kw.lower()}%"
        conditions.append(
            (KnowledgeDoc.title.ilike(pattern))
            | (KnowledgeDoc.content.ilike(pattern))
        )

    # 用 OR 组合所有关键词条件
    if len(conditions) == 1:
        where_clause = conditions[0]
    else:
        where_clause = or_(*conditions)

    res = await db.execute(
        select(KnowledgeDoc)
        .join(KnowledgeBase, KnowledgeDoc.kb_id == KnowledgeBase.id)
        .where(
            KnowledgeBase.tenant_id == tenant_id,
            where_clause,
        )
        .limit(_RAG_RESULT_LIMIT)
    )
    return list(res.scalars().all())


def _extract_keywords(text: str) -> list[str]:
    """v3.1: 从查询文本中提取关键词（中英文混合）。

    策略：英文按空格分词取长度≥2 的词；中文按 2-3 字滑窗。
    """
    keywords: list[str] = []
    # 英文部分
    for word in re.findall(r"[a-zA-Z]{2,}", text):
        keywords.append(word)
    # 中文部分：2-3 字滑窗
    chinese_chars = re.findall(r"[\u4e00-\u9fff]+", text)
    for seg in chinese_chars:
        if len(seg) <= 3:
            keywords.append(seg)
        else:
            for i in range(len(seg) - 1):
                keywords.append(seg[i:i + 2])
    # 去重保持顺序
    seen: set[str] = set()
    uniq: list[str] = []
    for kw in keywords:
        if kw.lower() not in seen:
            seen.add(kw.lower())
            uniq.append(kw)
    return uniq[:8]  # 最多 8 个关键词，避免 SQL 过长


async def _build_rag_context(docs: list[KnowledgeDoc]) -> str:
    """将检索结果格式化为上下文文本。"""
    if not docs:
        return ""
    
    lines = ["## 知识库参考"]
    for doc in docs:
        kb_name = "未知知识库"
        content = doc.content[:_RAG_CONTENT_MAX_LENGTH]
        if len(doc.content) > _RAG_CONTENT_MAX_LENGTH:
            content += "..."
        lines.append(f"\n### [{doc.title}]")
        lines.append(content)
        if doc.meta:
            meta_str = ", ".join(f"{k}={v}" for k, v in doc.meta.items())
            lines.append(f"元数据: {meta_str}")
    
    return "\n".join(lines)


async def retrieve_knowledge(
    db: AsyncSession,
    *,
    task: "Task",
    tenant_id: int = 1,
) -> str:
    """检索与任务相关的知识库内容。

    检索策略：
    1. 使用任务标题作为关键词
    2. 使用任务描述作为关键词
    3. 返回最相关的文档（合并去重）
    """
    queries = []
    if task.title:
        queries.append(task.title)
    if task.description:
        queries.append(task.description[:200])
    
    all_docs: list[KnowledgeDoc] = []
    seen_titles = set()
    
    for query in queries:
        docs = await _search_all_kbs(db, query, tenant_id)
        for doc in docs:
            if doc.title not in seen_titles:
                seen_titles.add(doc.title)
                all_docs.append(doc)
            if len(all_docs) >= _RAG_RESULT_LIMIT:
                break
        if len(all_docs) >= _RAG_RESULT_LIMIT:
            break
    
    logger.info("RAG 检索完成: task=%s, found=%d docs", task.id, len(all_docs))
    return await _build_rag_context(all_docs)
