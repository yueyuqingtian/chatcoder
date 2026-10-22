"""待处理交互（审批 / 提问）查询路由（plan-522-2209）。

【为什么需要】
审批请求按 `detail.session_id` 只广播到发起会话的 WS 通道（见 approval.py 的路由注释）。
用户停留在别的会话时，那条事件到不了他的窗口，于是「子任务在等审批」这件事完全不可见。
全局通道虽然转发了 approval.request，但事件是一次性的——断线重连、页面重载后就没有任何
途径知道「现在还有哪些会话在等我」。

本接口提供**当前所有未决交互的轻量摘要**，前端在建立全局连接后拉一次即可重建索引。
刻意不返回 detail 里的命令原文、文件路径与提问选项：这个接口会被任意会话的前端调用，
带上这些内容等于把各会话的隐私信息暴露给当前窗口；真正的 detail 仍由会话通道在进入
该会话时下发。
"""
from __future__ import annotations

from fastapi import APIRouter

from app.orchestration.approval import approval_manager

router = APIRouter(prefix="/approvals", tags=["approvals"])


@router.get("/pending")
async def pending() -> dict:
    """全部未决交互的摘要列表（含发起会话 id、种类、工具名与动作短语）。

    顺序按 approval_manager 的插入顺序，等价于到达先后——前端据此挑「最早到达」的一条。
    """
    items = approval_manager.pending_summaries()
    return {"items": items, "total": len(items)}
