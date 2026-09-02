"""审批管理器。

v1.0 (对齐 Claude Code):
- 审批队列管理(基于 approval_id)
- 等待审批结果(asyncio.Future + 超时机制)
- 回调注册(新审批请求时通知外部,如写库/推 WS)

v2.2 (对齐 zcode 3.12/3.14):
- 支持结构化提问(kind == "question")：answer 回填至 detail["answer"]
- 自动批准配置(auto_approve_tools)：只读/低风险工具免确认，降低中断率
- 执行策略规则匹配(ExecutionPolicyManager)：会话级/全局规则自动放行/阻断
"""

import asyncio
import logging
import uuid

from app.core.config import settings

logger = logging.getLogger(__name__)


class _PendingApproval:
    def __init__(self, approval_id: str, detail: dict):
        self.approval_id = approval_id
        self.detail = detail
        # v1.0: 不在 __init__ 中绑 loop (跨线程/跨 loop 安全)
        self._future: asyncio.Future | None = None

    def ensure_future(self) -> asyncio.Future:
        if self._future is None or self._future.get_loop() != asyncio.get_running_loop():
            self._future = asyncio.get_running_loop().create_future()
        return self._future


class ApprovalManager:
    """集中管理运行中的审批请求。单例。"""

    def __init__(self):
        self._pending: dict[str, _PendingApproval] = {}
        self._lock = asyncio.Lock()
        self._on_request: callable | None = None

    def set_on_request(self, cb: callable) -> None:
        """注册新请求回调: cb(approval_id, detail) -> 可为 async/sync"""
        self._on_request = cb

    def new_id(self) -> str:
        return f"appr_{uuid.uuid4().hex[:12]}"

    async def request(
        self,
        detail: dict,
        approval_id: str | None = None,
        is_forced: bool = False,
    ) -> bool:
        """发起一个审批请求并挂起等待结果。

        - 若已配置 auto_approve_tools 且匹配当前工具，直接放行不挂起；
        - 若 detail.kind == "question" 或 is_forced=True，绝不跳过；
        - 若超时未处理，按 settings.approval_timeout_sec 自动拒绝。
        """
        tool_name = detail.get("tool", "")
        kind = detail.get("kind", "tool_call")
        risk_level = detail.get("risk_level", "low")

        # 提问(kind == "question")与强制列表(is_forced=True)必须等待用户交互
        if kind != "question" and not is_forced:
            if settings.auto_approve_tools is True:
                logger.info("auto_approve_tools 为 True，自动放行工具: %s", tool_name)
                return True
            elif settings.auto_approve_tools:
                if isinstance(settings.auto_approve_tools, str):
                    auto_tools = {t.strip() for t in settings.auto_approve_tools.split(",") if t.strip()}
                elif isinstance(settings.auto_approve_tools, (list, set, tuple)):
                    auto_tools = set(settings.auto_approve_tools)
                else:
                    auto_tools = set()
                if tool_name and (tool_name in auto_tools or "*" in auto_tools):
                    logger.info("自动放行免审批工具: %s", tool_name)
                    return True

        if is_forced:
            logger.info("强制审批/提问(高风险/强制列表/问卷): %s (risk=%s)", tool_name, risk_level)
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
        elif detail.get("session_id"):
            # v2.2 兜底: 工具自行调用 approval_manager.request 时未注册回调，直接通过 ws_manager 广播
            try:
                from app.gateway.ws import manager as ws_manager
                sid = int(detail["session_id"])
                asyncio.create_task(ws_manager.broadcast(
                    sid,
                    {"event": "approval.request", "payload": {"approval_id": approval_id, "detail": detail}},
                ))
                logger.info("approval_manager 自适应广播 approval.request 到 session %s", sid)
            except Exception:
                logger.exception("自适应广播 approval.request 失败 %s", approval_id)

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
