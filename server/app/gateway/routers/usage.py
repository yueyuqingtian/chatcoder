"""使用统计（v1.1：全软件 token 用量汇总）。"""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.database import get_db

router = APIRouter(prefix="/usage", tags=["usage"])


@router.get("/summary")
async def usage_summary(days: int = 0, db: AsyncSession = Depends(get_db)):
    """全软件 token 用量汇总：总数 + 按模型分组（days>0 时仅统计近 N 天）。"""
    from app.persistence.models.usage_record import UsageRecord

    stmt = select(UsageRecord)
    if days > 0:
        stmt = stmt.where(UsageRecord.created_at >= datetime.utcnow() - timedelta(days=days))
    rows = (await db.execute(stmt)).scalars().all()

    total = {"prompt": 0, "completion": 0, "reasoning": 0, "cached": 0}
    by_model: dict[str, dict] = {}
    for r in rows:
        total["prompt"] += r.prompt_tokens or 0
        total["completion"] += r.completion_tokens or 0
        total["reasoning"] += r.reasoning_tokens or 0
        total["cached"] += r.cached_tokens or 0
        key = r.model_name or f"model#{r.model_id or 0}" or "未知模型"
        slot = by_model.setdefault(key, {"prompt": 0, "completion": 0, "reasoning": 0, "cached": 0, "calls": 0})
        slot["prompt"] += r.prompt_tokens or 0
        slot["completion"] += r.completion_tokens or 0
        slot["reasoning"] += r.reasoning_tokens or 0
        slot["cached"] += r.cached_tokens or 0
        slot["calls"] += 1
    return {
        "total": {
            **total,
            "total": total["prompt"] + total["completion"],
            "calls": len(rows),
        },
        "by_model": [
            {"model": k, **v, "total": v["prompt"] + v["completion"]}
            for k, v in sorted(by_model.items(), key=lambda kv: -(kv[1]["prompt"] + kv[1]["completion"]))
        ],
    }
