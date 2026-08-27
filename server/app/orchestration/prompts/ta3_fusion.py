"""ta3 系统提示词还原与融合（方案 §5.5）。

结构 = ① ta3 远端下发 baseAgentSystemMessage（主体还原）
      + ② ta3 任务列表纪律（原文移植自参考项目 chatService.ts:121-135）
      + ③ ta3 工具使用纪律（原文移植自 chatService.ts:137-157）
      + ④ <runtime-context> 快照（ta3 格式，chatService.ts:159-173）
      + ⑤ 当前项目流程规范追加段（不依赖工具名的通用约束）

上下文分层（developer 片段/压缩/审批）仍由当前项目 context_manager 注入，本模块
只负责 system 主体替换。
"""
from __future__ import annotations

from datetime import datetime


def _local_now() -> str:
    try:
        return datetime.now().astimezone().strftime("%Y/%m/%d %H:%M:%S")
    except Exception:  # noqa: BLE001
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ② 任务列表纪律（参考项目 chatService.ts:121-135 原文）
TA3_TASK_MANAGEMENT_SECTION = """## 任务列表
当用户请求包含多个步骤、需要修改代码、调试问题、迁移功能或长时间执行时，先调用 TodoWrite 创建当前会话的任务列表。
任务列表必须保持完整，后续每次推进时用 TodoWrite 更新全部列表；同一时间只能有一个任务是 in_progress。
in_progress 任务可以提供 activeForm 描述当前正在做的动作；未提供时系统会用 content 兜底。
任务完成时保留 completed 状态，不要因为全部完成就清空列表。只有用户明确要求清空或开始无关新任务时才清空。
回复正文只输出给用户看的结论、步骤和结果；不要把内部思考、自我分析或类似 "The user asked..." 的自言自语写进正文。
简单问答、无需跟踪步骤的小任务可以不创建任务列表。"""

# ③ 工具使用纪律（参考项目 chatService.ts:137-157 原文）
TA3_TOOL_DISCIPLINE_SECTION = """## 工具使用纪律
只有通过工具调用（tool_calls）实际执行的操作才会真实生效；你回复正文中的描述本身不代表操作已经发生。
不要编造工具调用，不要虚构工具执行结果，也不要声称执行了并未真正调用的操作（例如"已修改""已保存""已执行"）。
需要读取或修改文件、运行命令、搜索代码时，直接调用对应工具；在工具返回结果之前，不要宣称操作已完成。
工具返回的错误要如实报告，不要假装成功。
回答关于项目结构、文件路径、代码内容的问题前，先调用 Read/Search/List 等工具核实，禁止凭记忆描述或编造。
历史上下文中的 assistant 消息若带 tool_calls 及其 tool 返回结果，说明这些操作真实发生过，其返回内容是可查验的事实依据；不要重复声称已做过上下文中不存在的操作，也不要把上下文中的叙述性描述当成已发生的事实。
历史消息中出现 "tool call aborted before dispatch" 或 "TOOL_OUTCOME_UNKNOWN" 错误结果时，说明对应操作未完成或结果未知；在重新核实外部状态前，不得假定该操作已经成功。"""

# ⑤ 当前项目流程规范追加段（不依赖工具名的通用约束，与 ta3 提示词语言风格一致）
CHATCODER_ADDENDUM = """## 流程规范（本环境附加）
1. 本环境由 chatcoder 工作台托管：任务拆解、上下文压缩、工具审批由平台自动完成，你只需专注执行。
2. 工具调用按平台定义执行：Write/Edit/Bash 等写操作默认需用户审批，审批通过后才会真正生效；被拒绝时如实说明并调整方案。
3. 修改文件后应复查关键变更（重新 Read 确认），并遵循项目现有代码风格与目录结构，不引入未使用的依赖。
4. 重要决策与关键改动添加简短注释；交付前自查可运行性（编译/测试）。
5. 长驻进程（dev server、watch、后端服务）必须以后台模式启动（Bash 的 waitForCompletion=false）。
6. 执行类任务按步骤推进：探索 → 小步修改 → 验证 → 汇报；失败时先读完整错误定位根因，不要盲目重试。
7. 最终结论在主窗口以清晰摘要汇报：改了什么、如何验证。回复用简体中文、简洁直接，不使用表情符号或装饰符号。"""

# 子代理引导（启用子代理时追加）
SUBAGENT_GUIDE_SECTION = """## 子代理使用
SubAgent 用于并行调研多个相互独立的课题（如前端 + 后端 + 协议同时排查）。同一轮最多 2-3 个，
单文件读取、单关键词搜索等琐碎调研必须自己用直接工具调用完成，不得派发。子代理返回结论后
由你串行整合并亲自验证关键事实。"""


def build_runtime_snapshot(workspace: str = "") -> str:
    """④ <runtime-context> 快照（对齐参考项目 buildRuntimeSnapshotSection）。"""
    now = datetime.now()
    iso = now.isoformat()
    local = now.strftime("%Y-%m-%d %H:%M:%S")
    parts = [
        "<runtime-context>",
        "Current runtime context. This snapshot supersedes any earlier runtime-context snapshots.",
        f"- 当前时间：{local}（ISO: {iso}）",
        f"- 工作目录：{workspace}" if workspace else "",
        "</runtime-context>",
    ]
    return "\n".join(p for p in parts if p)


def build_ta3_system_prompt(model_meta: dict | None, workspace: str = "",
                            enable_subagents: bool = True) -> str:
    """组装 ta3 模式系统提示词（见模块 docstring 结构）。"""
    model_meta = model_meta or {}
    sections = [
        (model_meta.get("systemMessage") or "").strip(),
        TA3_TASK_MANAGEMENT_SECTION,
        TA3_TOOL_DISCIPLINE_SECTION,
        build_runtime_snapshot(workspace),
    ]
    if enable_subagents:
        sections.append(SUBAGENT_GUIDE_SECTION)
    sections.append(CHATCODER_ADDENDUM)
    return "\n\n".join(s for s in sections if s)
