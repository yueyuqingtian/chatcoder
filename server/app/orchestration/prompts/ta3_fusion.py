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

import logging
from datetime import datetime

logger = logging.getLogger(__name__)


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

# ⑤ 当前项目流程规范追加段（不依赖工具名的通用约束）
# plan-19-82: 回复语言不再写死「简体中文」——由 language 参数动态生成，
# 与远端 systemMessage 及主提示词口径统一（用户发什么语言就用什么语言回复）。
CHATCODER_ADDENDUM = """## 流程规范（本环境附加）
1. 本环境由 chatcoder 工作台托管：任务拆解、上下文压缩、工具审批由平台自动完成，你只需专注执行。
2. 工具调用按平台定义执行：Write/Edit/Bash 等写操作默认需用户审批，审批通过后才会真正生效；被拒绝时如实说明并调整方案。
3. 修改文件后应复查关键变更（重新 Read 确认），并遵循项目现有代码风格与目录结构，不引入未使用的依赖。
4. 重要决策与关键改动添加简短注释；交付前自查可运行性（编译/测试）。
5. 长驻进程（dev server、watch、后端服务）必须以后台模式启动（Bash 的 waitForCompletion=false），用完必须用 BashKill 主动停止；会自己结束的后台命令跑完后系统会主动回报，不必轮询。
6. 执行类任务按需验证：由你判断风险与已完成的工作量——风险较大（共享契约或类型、数据结构与迁移、依赖变更、并发与持久化逻辑、大范围重写）或未验证的改动已经堆得很多时，先跑一次最相关的检查再继续叠加；局部、低风险的小改动（文案、样式、同模块替换）不必中途验证。交付前对整个改动统一验证一次；失败时先读完整错误定位根因，不要盲目重试。
7. 最终结论在主窗口以清晰摘要汇报：改了什么、如何验证。回复用{lang_label}、简洁直接，不使用表情符号或装饰符号。"""

# 完全访问模式（danger-full-access）：免审批直接执行——流程规范去掉"默认需审批"条款，
# 与用户选择的完全访问语义一致（此前恒注入审批说明导致"完全访问仍像要审批"的观感）。
CHATCODER_ADDENDUM_FULL_ACCESS = """## 流程规范（本环境附加）
1. 本环境由 chatcoder 工作台托管：任务拆解、上下文压缩由平台自动完成。
2. 当前为【完全访问模式】：Write/Edit/Bash 等写操作与命令执行无需审批，直接生效；仍须对改动负责、避免越权操作与破坏性动作。
3. 修改文件后应复查关键变更（重新 Read 确认），并遵循项目现有代码风格与目录结构，不引入未使用的依赖。
4. 重要决策与关键改动添加简短注释；交付前自查可运行性（编译/测试）。
5. 长驻进程（dev server、watch、后端服务）必须以后台模式启动（Bash 的 waitForCompletion=false），用完必须用 BashKill 主动停止；会自己结束的后台命令跑完后系统会主动回报，不必轮询。
6. 执行类任务按需验证：由你判断风险与已完成的工作量——风险较大（共享契约或类型、数据结构与迁移、依赖变更、并发与持久化逻辑、大范围重写）或未验证的改动已经堆得很多时，先跑一次最相关的检查再继续叠加；局部、低风险的小改动（文案、样式、同模块替换）不必中途验证。交付前对整个改动统一验证一次；失败时先读完整错误定位根因，不要盲目重试。
7. 最终结论在主窗口以清晰摘要汇报：改了什么、如何验证。回复用{lang_label}、简洁直接，不使用表情符号或装饰符号。"""

# 只读模式（read-only / plan）：仅允许查看/搜索/分析。
# plan-562-2318 A23：旧版把「改完复查」「交付前自查可运行性」「后台进程管理」「交付前统一验证」
# 一并套进来，与第 2 条「禁止写入文件、运行命令」直接矛盾（只读会话根本不产生改动与进程）。
# 这里只保留平台托管、能力边界与汇报要求三条，不再出现指向不存在动作的句子。
CHATCODER_ADDENDUM_READONLY = """## 流程规范（本环境附加）
1. 本环境由 chatcoder 工作台托管：任务拆解、上下文压缩由平台自动完成。
2. 当前为【只读模式】：仅允许读取、搜索、分析代码或文档；禁止写入文件、运行命令、应用补丁。
3. 最终结论在主窗口以清晰摘要汇报：看了什么、结论是什么、依据是什么。回复用{lang_label}、简洁直接，不使用表情符号或装饰符号。"""


def _normalize_lang(language: str | None) -> str:
    """语言码归一（非 zh/en 一律按 auto 处理，沿用“跟随用户消息”表述）。"""
    return language if language in ("zh", "en") else "auto"


def _mode_kind(mode: str | None) -> str:
    """模式名 → 判定档（plan-560-2311 C5）。

    事实源在 `permission_profile_service.resolve_mode_kind`（与审批层、能力矩阵、
    agent_loop 护栏、context_manager 同一份口径）。延迟 import，避免配置读取
    在提示词模块加载期就发生；配置层异常时退回原名（旧行为）。
    """
    raw = str(mode or "").strip()
    try:
        from app.services.permission_profile_service import resolve_mode_kind

        return resolve_mode_kind(raw)
    except Exception:  # noqa: BLE001 — 配置层异常时按原名判定
        logger.warning("[ta3] 模式归一失败 mode=%s(按原名判定)", raw, exc_info=True)
        return raw


def _build_addendum(execution_mode: str = "agent", approval_mode: str = "ask",
                    language: str = "auto") -> str:
    """按「执行模式 × 权限模式」选择流程规范段。

    plan-75-332: 参数由 sandbox_mode 改为两条正交轴。

    plan-562-2318 A24：两条轴正交，**执行模式的硬边界优先**。此前先判
    `approval_mode == "full"`，导致「只读执行模式 + 完全访问权限模式」拿到完全访问版
    规范（写着 Write/Edit/Bash 无需审批、直接生效），与审批层对只读模式的硬拒绝相矛盾。
    当前判定顺序：
    1. execution_mode 判定档为 readonly / plan → 仅读版规范（不管权限模式是什么）；
    2. 否则 approval_mode == "full"（完全访问）→ 免询问版规范；
    3. 其余 → 默认规范（写操作按权限模式裁决）。

    plan-560-2311 C5：只读判定走**判定档**（按 kind 归一，事实源在
    `permission_profile_service.resolve_mode_kind`）。此前按模式名等值比较，自定义
    kind=readonly 模式拿不到仅读版规范——ta3 会话里会拿到「可以写文件」的规范，
    与审批层的硬拒绝自相矛盾。

    language（plan-19-82）：回复语言句按本轮语言动态生成（中文→简体中文；英文→English；
    auto→跟随用户消息），不再写死中文。
    """
    from app.orchestration.prompts.language import language_label

    if _mode_kind(execution_mode) in ("readonly", "plan"):
        # 只读与计划在流程规范上同档：都不许写业务文件（计划文档的例外由审批边界单独放行，
        # 不需要在规范段里另写一份，避免两处口径漂移）
        tpl = CHATCODER_ADDENDUM_READONLY
    elif approval_mode == "full":
        tpl = CHATCODER_ADDENDUM_FULL_ACCESS
    else:
        tpl = CHATCODER_ADDENDUM
    return tpl.format(lang_label=language_label(_normalize_lang(language)))

# 子代理引导（启用子代理时追加）
# plan-549-2281：子会话下线后，子代理是唯一的并行执行通道（调研 + 实现）。
# 旧文本有两处已失效：① 教模型「强依赖时用 SubAgent 同步等待」——同步派发已整体下线
# （出站只发 SubAgentAsync）；② 通篇不提「团队协作只有主代理一条线」，与内部通道口径不一致。
SUBAGENT_GUIDE_SECTION = """## 子代理使用
SubAgent 是唯一的并行执行通道（不存在子会话）：既能做只读调研，也能做可写实现。
`subagent_type=Explore` 为只读调研（对应内部 readonly 模式）；其余 / 缺省为可写（agent 模式）。
同一轮的派发量以「单会话同时运行上限」为准（设置 →「子代理」；每次派发结果会回带上限/占用/剩余），超出的派发会被立即拒绝；剩余为 0 时不要再派，先把已有结果收回来。单文件读取、单关键词搜索等琐碎调研必须自己用直接工具调用完成，不得派发。
写入范围互不相交的实现工作可以派出去——实现可以委派，不必自己逐处改。
派发方式：一律用 SubAgentAsync 后台派发，立即返回、完成后结果自动推送，不要轮询；
仅当下一步强依赖结论时才用 TaskQuery 取回（传入 SubAgentAsync 返回的 taskId）。
已派发给子代理的工作不要自己重复做；它的结论由你串行整合并亲自验证关键事实。
子代理的权限模式（要不要审批）继承当前会话；它需要决策时会向你提问，用 SendToSubagent 答复。
子代理永远不进计划模式、不用工作流工具、也不能再派子代理。"""


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
                            enable_subagents: bool = True,
                            execution_mode: str = "agent",
                            approval_mode: str = "ask",
                            language: str = "auto",
                            language_source: str = "user") -> str:
    """组装 ta3 模式系统提示词（见模块 docstring 结构）。

    plan-75-332：参数由 sandbox_mode 改为两条正交轴——execution_mode
    （readonly / plan / agent）决定能力边界，approval_mode（ask / auto / full）
    决定是否免询问；流程规范段按二者生成，避免注入与所选模式矛盾的审批条款。
    language（plan-19-82）：远端 systemMessage 可能是英文，与本地流程规范口径不一；
    这里把统一的语言纪律块置于**首尾双锚**，并按本轮语言生成「回复用 X」文案。
    """
    from app.orchestration.prompts.language import build_language_directive

    model_meta = model_meta or {}
    sections = [
        (model_meta.get("systemMessage") or "").strip(),
        TA3_TASK_MANAGEMENT_SECTION,
        TA3_TOOL_DISCIPLINE_SECTION,
        build_runtime_snapshot(workspace),
    ]
    if enable_subagents:
        sections.append(SUBAGENT_GUIDE_SECTION)
    sections.append(_build_addendum(execution_mode, approval_mode, language=language))
    body = "\n\n".join(s for s in sections if s)
    _lang_dir = build_language_directive(language, source=language_source)
    return f"{_lang_dir}\n\n{body}\n\n{_lang_dir}"
