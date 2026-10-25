"""v0.3: 工具抽象基类与数据结构。"""
import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

RiskLevel = Literal["low", "medium", "high"]


@dataclass
class ToolContext:
    """工具执行上下文。"""

    workspace_root: str
    session_id: int
    task_id: int
    agent_id: int
    agent_name: str
    # v0.9: 任务中断事件,set 后 agent loop 与长工具应主动退出
    cancel_event: asyncio.Event | None = None
    # P0 修复: agent 主循环同连接 db 会话,供工具直接落库（避免跨连接 SQLite 写锁）
    db: "AsyncSession | None" = None
    # plan-75-332: 执行模式（readonly / plan / agent）——"能做什么"，越界即拒绝
    permission_mode: str = "agent"
    # plan-75-332: 权限模式（ask / auto / full）——"要不要问"，与执行模式正交
    approval_mode: str = "ask"
    # plan-88 引入的沙箱模式字段。plan-75-332 起不再有任何判定消费它
    # （read-only / danger-full-access 的硬边界已由执行模式×权限模式取代）；
    # 保留仅为兼容 agent_loop / subagent_context 的既有赋值，避免改动面外溢。
    sandbox_mode: str = "workspace-write"
    # plan-75-332: 审批卡「解释」专用思考深度默认取本 turn 的档位
    # （未指定时回落全局 agent_reasoning_effort，见 approval_explain_service）。
    reasoning_effort: str | None = None
    # v7(B): 运行时输出回调——长命令（terminal_exec 同步模式）执行期间逐帧上报增量输出，
    # 由 agent_loop 注入闭包并广播 tool.output WS 事件（前端运行中实时展示）。
    on_tool_output: Callable[[str], Awaitable[None]] | None = None
    # plan-20261019-desktop-precision：当前模型名。电脑操控按模型族选择坐标口径
    # （Claude / GPT 习惯截图像素，DeepSeek / Qwen3 / GLM 等原生是 0–1000 比例）。
    model_name: str = ""
    # plan-474-2105：邮箱打断——set 后长命令应把「已在运行的进程」转后台并立即返回，
    # 而不是被取消（命令本身有价值，取消等于白跑）。只有 terminal_exec 消费这个信号。
    detach_event: asyncio.Event | None = None
    # plan-474-2105：所属 turn（后台命令完成回报据此标注归属；可为空）
    turn_id: int | None = None
    # fs_read 未变化短路的会话级缓存（见 tools/read_cache.py）；为空时不做短路
    read_cache: Any = None
    # plan-522-2209：调用方身份（子代理 / 子会话）。审批等待策略据此区分：
    # 子任务的审批可能发生在用户并未停留的会话里，超时自动拒绝等于让用户既看不到请求
    # 也看不到失败原因，只看到子任务莫名中断——这类审批不设静默超时。
    is_subagent: bool = False
    is_subsession: bool = False
    # plan-549-2281：子代理的任务标题。审批卡据此显示「来自子代理 #N「标题」」，
    # 让用户判断这张卡是哪个派发在等，而不是只看到一个 agent 名字。
    subagent_title: str = ""


# 失败类别：模型据此选择恢复动作（而不是只看到一段裸报错文本）
ERROR_CODES = (
    "NOT_FOUND",       # 路径 / 工具 / 资源不存在
    "ARG_INVALID",     # 参数缺失或非法
    "TIMEOUT",         # 执行超时
    "PERMISSION",      # 审批拒绝 / 权限不足
    "STALE_READ",      # 修改前未读取或文件已变化
    "SANDBOX",         # 沙箱 / 策略拒绝（不是命令本身的缺陷）
    "EXEC_FAILED",     # 工具执行后返回失败（默认类别）
    "INTERNAL",        # 工具内部异常
)

# 各类别的恢复指引：拼入给模型的文本，告诉它下一步该怎么做
ERROR_HINTS = {
    "NOT_FOUND": "目标不存在。先用 fs_list / fs_grep 确认路径（相对工作区）后再操作，不要凭记忆臆测路径。",
    "ARG_INVALID": "参数缺失或格式不对。对照工具说明修正参数后再调用一次。",
    "TIMEOUT": "执行超时。可拆分任务或缩小范围，必要时调大超时；不要用相同参数立即重试。",
    "PERMISSION": "操作未获批准或权限不足。不要绕过；改用只读方式完成，或说明需要用户授权的部分。",
    "STALE_READ": "修改前必须先读取该文件的当前内容。先 fs_read 再修改。",
    "SANDBOX": "被沙箱 / 策略拒绝，这不是命令本身的缺陷。不要换写法绕过，改用允许的方式或告知用户。",
    "EXEC_FAILED": "工具执行失败。根据错误信息定位原因，修正后重试；仍失败则换一种方式或说明阻塞点。",
    "INTERNAL": "工具内部异常。先检查参数与路径；若仍失败，改用其它工具或说明阻塞点。",
}


@dataclass
class ToolResult:
    """工具执行结果(转 OpenAI tool message content)。"""

    ok: bool
    output: str  # 给 LLM 看的文本输出
    data: dict[str, Any] = field(default_factory=dict)  # 结构化附加信息
    error: str = ""
    error_code: str = ""  # ERROR_CODES 之一；失败时由工具或执行器填写
    hint: str = ""  # 可选的具体恢复建议；为空时按 error_code 取默认指引

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "output": self.output, "data": self.data, "error": self.error,
            "error_code": self.error_code, "hint": self.hint,
        }

    def failure_hint_line(self) -> str:
        """失败时附在模型可见文本末尾的一行：错误类别 + 恢复指引。"""
        code = self.error_code if self.error_code in ERROR_CODES else "EXEC_FAILED"
        hint = self.hint or ERROR_HINTS[code]
        return f"[错误类别: {code}] {hint}"

    def failure_text(self) -> str:
        """模型可见的失败文本：原始报错 + 类别 + 恢复指引（成功结果不使用）。"""
        if self.ok:
            return self.error or ""
        body = self.error or "工具执行失败（无错误信息）"
        return f"{body}\n{self.failure_hint_line()}"

    def classify_if_missing(self) -> None:
        """工具只返回 ok=False 而未标注类别时，补上默认类别（不覆盖工具自己的标注）。"""
        if not self.ok and self.error_code not in ERROR_CODES:
            self.error_code = "EXEC_FAILED"


class Tool(ABC):
    """所有工具的抽象基类。"""

    name: str = "base"
    risk_level: RiskLevel = "low"
    # plan-128-518：会话态工具标记——True 时不随全量工具下发，由引擎在会话满足条件时显式放行
    session_scoped: bool = False
    # plan-522-2209：是否可与其他并发安全的调用同时执行。
    # 默认 False（独占）——只有明确声明安全的工具才并行，未知一律串行。
    # 判定口径参考 deepseek-harness：不是「看起来只读」就并行，而是「调用主体之间
    # 不存在共享可变状态」。因此这里只对不写文件、不改库、不改变进程状态的工具置 True。
    concurrency_safe: bool = False
    description: str = ""

    @abstractmethod
    def function_schema(self) -> dict:
        """返回 OpenAI function-calling 的 tools 元素 dict。"""

    @abstractmethod
    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """执行工具。"""

    def approval_precheck(self, args: dict[str, Any], ctx: ToolContext) -> tuple[bool, str]:
        """v2.2 (对齐 zcode 3.12 命令安全分级): 审批门前置检查。

        plan-75-332 **已废弃**：工具不再自行决定是否免审——那等于工具在替用户
        做审批决策，与统一策略重复。是否需询问用户一律由
        `approval_policy.decide()` 按「执行模式 × 权限模式」判定。
        保留默认实现仅为兼容既有子类与测试的签名约定。
        """
        return False, ""
