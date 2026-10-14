"""归档内容的永久删除（plan-20261014-fix4）：设置 → 归档恢复的「批量删除」一次请求完成。"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services import archive_service

router = APIRouter(prefix="/archive", tags=["archive"])


class PurgeBody(BaseModel):
    project_ids: list[int] = Field(default_factory=list, max_length=2000)
    session_ids: list[int] = Field(default_factory=list, max_length=5000)


@router.post("/purge", response_model=dict)
async def purge_archived(body: PurgeBody) -> dict:
    """永久删除项目（含工作树子项目与其下全部会话）与会话，以及它们的全部关联数据。

    已不存在的 id 计入 missing 而不报错——重复提交或列表未及时刷新时不再提示「会话不存在」。
    """
    if not body.project_ids and not body.session_ids:
        raise HTTPException(400, "没有要删除的项目或会话")
    return await archive_service.purge(project_ids=body.project_ids, session_ids=body.session_ids)
