"""后续任务建议路由（plan-156-619 S10）。

三条端点覆盖建议卡的完整生命周期：
  · GET  /sessions/{id}/suggestions/latest   取当前待处理建议（刷新后重建卡片）
  · POST /suggestions/{sid}/accept           接受 → 在当前会话直接发起一轮执行
  · POST /suggestions/{sid}/dismiss          取消 → 永久不再展示

为什么 latest 走 GET 而 accept/dismiss 走 POST：前者是幂等读取，
后者有副作用（发起执行轮 / 改状态），语义上必须分开。

plan-549-2281：子会话创建能力下线后 accept 不再新建会话，改为把建议内容当作
一条用户消息发进当前会话（会话已有运行中的 turn 时改为注入该 turn），
由主代理自己主导；需要并行时它再派子代理。
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.database import get_db
from app.services import session_service, suggestion_service

logger = logging.getLogger(__name__)
router = APIRouter(tags=["suggestions"])


@router.get("/sessions/{session_id}/suggestions/latest")
async def get_latest_suggestion(session_id: int, db: AsyncSession = Depends(get_db)):
    """取该会话当前待处理建议；没有则返回 {suggestion: null}。"""
    if await session_service.get_session(db, session_id) is None:
        raise HTTPException(404, "会话不存在")
    return {"suggestion": await suggestion_service.latest_pending(db, session_id)}


@router.post("/suggestions/{suggestion_id}/accept")
async def accept_suggestion(suggestion_id: int, db: AsyncSession = Depends(get_db)):
    """接受建议 → 在当前会话直接发起一轮执行；返回 turn id（plan-549-2281，不再开子会话）。"""
    try:
        return await suggestion_service.accept(db, suggestion_id)
    except suggestion_service.SuggestionError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/suggestions/{suggestion_id}/dismiss")
async def dismiss_suggestion(suggestion_id: int, db: AsyncSession = Depends(get_db)):
    """取消建议 → 置 dismissed，此后不再展示（刷新 / 重启均不复活）。"""
    try:
        await suggestion_service.dismiss(db, suggestion_id)
    except suggestion_service.SuggestionError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True}
