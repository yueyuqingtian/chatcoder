"""v0.3: 审批管理器 — 阻塞式审批门。

工作流:
1. ToolExecutor 遇 medium/high risk 工具 → ApprovalManager.request(approval_id, detail)
   - 创建 asyncio.Future 入 pending 字典
   - 同时调用方负责把 approval 消息入库 + WS 广播 approval.request
2. 用户在前端点同意/拒绝 → WS approval.response → ApprovalManager.resolve(id, approved)
3. request() 返回 (approved: bool),超时自动拒绝。
"""
import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from app.core.config import settings

logger = logging.getLogger(__name__)


@dataclass
class _PendingApproval:
    approval_id: str
    future: asyncio.Future[bool] | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def ensure_future(self) -> asyncio.Future[bool]:
        """v1.0: 延迟创建 future，避免 Python 3.12+ get_event_loop() 崩溃。"""
        if self.future is None:
            self.future = asyncio.get_running_loop().create_future()
        return self.future


class ApprovalManager:
    def __init__(self) -> None:
        self._pending: dict[str, _PendingApproval] = {}
        # approval_id 解析后的回调(供 ws.py 注册)
        self._on_request: Callable[[str, dict], Any] | None = None
        self._lock = asyncio.Lock()

    def set_on_request(self, cb: Callable[[str, dict], Any]) -> None:
        """注册审批请求回调(用于持久化 + WS 广播)。"""
        self._on_request = cb

    def new_id(self) -> str:
        return "apr_" + uuid.uuid4().hex[:16]

    async def request(
        self,
        *,
        detail: dict[str, Any],
        approval_id: str | None = None,
    ) -> bool:
        """发起审批请求并阻塞等待结果。返回是否批准。"""
        tool_name = detail.get("tool", "")
        risk_level = detail.get("risk_level", "low")

        # v1.0: auto_approve=True 时完全跳过审批，包括强制列表工具
        if settings.auto_approve_tools:
            logger.info("自动批准工具调用(auto_approve): %s (risk=%s)", tool_name, detail.get("risk_level"))
            return True

        # auto_approve=False 时，强制审批工具始终需要审批
        force_tools = settings.force_approval_tools_list
        is_forced = tool_name in force_tools or risk_level == "high"
        if is_forced:
            logger.info("强制审批(高风险/强制列表): %s (risk=%s)", tool_name, risk_level)
        approval_id = approval_id or self.new_id()
        pa = _PendingApproval(approval_id=approval_id, detail=detail)
        pa.ensure_future()  # v1.0: 在运行中的事件循环内创建 future
        async with self._lock:
            self._pending[approval_id] = pa

        if self._on_request:
            try:
                # 回调内做入库 + WS 广播(可异步也可同步)
                result = self._on_request(approval_id, detail)
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
            except Exception:
                logger.exception("审批请求回调异常 %s", approval_id)

        try:
            approved = await asyncio.wait_for(
                pa.ensure_future(), timeout=settings.approval_timeout_sec
            )
            return approved
        except asyncio.TimeoutError:
            logger.warning("审批超时自动拒绝 %s", approval_id)
            return False
        finally:
            async with self._lock:
                self._pending.pop(approval_id, None)

    def resolve(self, approval_id: str, approved: bool, answer: dict | None = None) -> bool:
        """解析审批。返回是否成功匹配到 pending(供 ws 判 404)。

        v2.2: answer 为结构化回答（ask_user_question 工具），
        回填到 pending 的 detail（与工具侧共享引用，工具从 detail["answer"] 读取）。
        """
        pa = self._pending.get(approval_id)
        if pa is None:
            return False
        if answer is not None:
            pa.detail["answer"] = answer
        fut = pa.ensure_future()
        if not fut.done():
            fut.set_result(approved)
        return True

    def get_detail(self, approval_id: str) -> dict | None:
        """v2.2: 取 pending 审批的 detail（供"始终允许"生成规则）。"""
        pa = self._pending.get(approval_id)
        return dict(pa.detail) if pa else None

    @property
    def pending_count(self) -> int:
        return len(self._pending)


approval_manager = ApprovalManager()
