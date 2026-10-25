"""审批管理器。

v1.0 (对齐 Claude Code):
- 审批队列管理(基于 approval_id)
- 等待审批结果(asyncio.Future + 超时机制)
- 回调注册(新审批请求时通知外部,如写库/推 WS)

v2.2 (对齐 zcode 3.12/3.14):
- 支持结构化提问(kind == "question")：answer 回填至 detail["answer"]
- 执行策略规则匹配(ExecutionPolicyManager)：会话级/全局规则自动放行/阻断

plan-75-332：本模块**不再自行决定是否放行**（原来的 auto_approve_tools 自动放行
与 force_approval_tools / risk_level=="high" 强制审批已移除）——那些判定与 executor
侧的策略重复、又靠例外逻辑互相兜底。是否放行统一由 approval_policy.decide() 判定；
走到这里的请求一律意味着"需要询问用户"。
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
        """注册新请求回调: cb(approval_id, detail) -> 可为 async/sync。

        plan-230-1144: 仅作兼容保留——正常路径已改为 request() 内按
        detail.session_id 直接广播（多会话并发时全局单例回调会被覆盖，
        导致审批/提问串到其他会话）。
        """
        self._on_request = cb

    def new_id(self) -> str:
        return f"apr_{uuid.uuid4().hex[:12]}"

    async def request(
        self,
        detail: dict,
        approval_id: str | None = None,
        is_forced: bool = False,  # noqa: ARG002 —— plan-75-332：形参保留兼容，语义已由策略层承担
    ) -> bool:
        """发起一个审批请求并挂起等待结果。

        plan-75-332：本方法**不再自行决定是否放行**。改造前它会读全局配置
        `auto_approve_tools` 直接放行、并按 `force_approval_tools` / `risk_level=="high"`
        强制审批——与 executor 的策略判定重复且互相冲突（「自动批准」开关一开，
        「始终需要审批」就得靠另一套例外逻辑兜底）。

        现在：是否放行由 `approval_policy.decide()` 在 executor 侧判定，走到这里
        就意味着"需要询问用户"，本方法只负责挂起等待与超时。

        - detail.kind == "question"（结构化提问）**不设超时**——AI 保持暂停直到
          用户回答（取消 turn 可结束等待）；工具审批仍按
          settings.approval_timeout_sec 超时自动拒绝。
        - is_forced 形参保留以兼容既有调用方（语义已由策略层承担）。
        """
        kind = detail.get("kind", "tool_call")
        approval_id = approval_id or self.new_id()
        pa = _PendingApproval(approval_id=approval_id, detail=detail)
        pa.ensure_future()  # v1.0: 在运行中的事件循环内创建 future
        async with self._lock:
            self._pending[approval_id] = pa

        # plan-230-1144: 按 detail.session_id 精确路由广播。
        # 修复跨会话串线：此前优先调用全局单例 _on_request 回调（executor.set_on_request
        # 全局注册，多会话并发时后运行的会话会覆盖前者），A 会话的提问/审批会被广播进
        # B 会话的 WS 通道（表现为"问题在另一个会话弹出"）。
        # 现改为：只要有 session_id 就直发对应会话通道；回调仅作为无 session_id 的兼容路径。
        _sid = detail.get("session_id")
        if _sid is not None:
            try:
                from app.gateway.ws import manager as ws_manager
                asyncio.create_task(ws_manager.broadcast(
                    int(_sid),
                    {"event": "approval.request", "payload": {"approval_id": approval_id, "detail": detail}},
                ))
            except Exception:
                logger.exception("审批请求广播失败 %s", approval_id)
        elif self._on_request:
            try:
                # 兼容：无 session_id 的旧调用路径仍走注册回调
                result = self._on_request(approval_id, detail)
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
            except Exception:
                logger.exception("审批请求回调异常 %s", approval_id)

        try:
            if kind == "question":
                # plan-230-1144: 结构化提问不设超时——AI 保持暂停直到用户回答后继续；
                # 用户可随时取消整个 turn 来结束等待（turn 取消会连带取消本协程）。
                approved = await pa.ensure_future()
            else:
                # plan-522-2209：等待上限分两类。
                # 子会话 / 子代理的审批往往发生在用户并未停留的会话里，超时自动拒绝等于让用户
                # 两头都看不到（请求没看见、失败原因也没看见，只看到子任务莫名中断）——
                # 按 approval_timeout_sec_child 处理，默认 0 即不设超时，只等用户处理或 turn 取消。
                _is_child = bool(detail.get("is_subagent") or detail.get("is_subsession"))
                _timeout = (
                    settings.approval_timeout_sec_child if _is_child
                    else settings.approval_timeout_sec
                )
                if _timeout is None or _timeout <= 0:
                    approved = await pa.ensure_future()
                else:
                    approved = await asyncio.wait_for(
                        pa.ensure_future(), timeout=_timeout
                    )
            return approved
        except asyncio.TimeoutError:
            logger.warning(
                "审批超时自动拒绝 %s（kind=%s 身份=%s）",
                approval_id, kind,
                "子会话/子代理" if detail.get("is_subagent") or detail.get("is_subsession") else "主会话",
            )
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

    def pending_summaries(self) -> list[dict]:
        """plan-522-2209：全部待处理交互的摘要（供 GET /api/approvals/pending 重建索引）。

        带上 detail（剔除 answer）：用户在任意会话看到子会话的审批卡时，卡片要能显示
        工具名、动作短语与命令原文，否则只剩一句「有个审批在等你」，等于逼用户跳进去看。
        剔除 answer 是因为它是提问的作答内容，回填后属于已产生的结论，不该再被下发给别处。
        """
        out: list[dict] = []
        for approval_id, pa in self._pending.items():
            detail = dict(pa.detail or {})
            detail.pop("answer", None)
            kind = "question" if detail.get("kind") == "question" else "tool_call"
            out.append({
                "approval_id": approval_id,
                "session_id": detail.get("session_id"),
                "kind": kind,
                "tool": detail.get("tool"),
                "action_label": detail.get("action_label") or detail.get("summary"),
                "agent_name": detail.get("agent_name"),
                "is_subagent": bool(detail.get("is_subagent")),
                "is_subsession": bool(detail.get("is_subsession")),
                "detail": detail,
            })
        return out

    @property
    def pending_count(self) -> int:
        return len(self._pending)


approval_manager = ApprovalManager()
