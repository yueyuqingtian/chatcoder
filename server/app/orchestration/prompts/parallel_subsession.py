"""并行分派子会话的优先提示词（plan-156-621；plan-20261018 强化优先级）。

【为什么需要】
用户要求：任务执行时最大限度利用子会话——只要进程中存在互相隔离、互不影响的工作点，
就优先分派子会话并行处理，而不是主代理自己串行做完。

【plan-20261018 强化：旧提示词为什么没生效】
实测一轮多问题修复（4 个互不相干的缺陷）时，主代理把**只读调研**派给了子代理，
却把所有**可写工作**都串行做完，理由听起来都成立：「改动集中在同几个文件」
「验证要共用同一个内置浏览器标签」「需求之后可能还会变」。
复盘后发现旧提示词有四个结构性缺陷，本版逐一修掉：
1. 判据是感受性的（「彼此隔离、互不影响」）——无法证伪，任何模糊理由都能否决它。
   改为两条**可判定**判据：写入路径集合是否相交、验证通道是否冲突。
2. 没有强制动作——「执行前先扫一遍」不留痕，可以被跳过而不被发现。
   改为要求把任务清单逐项标注「自做 / 派发」，并给出分派不足的自检条件。
3. 边界条款过宽——「不要为一两次工具调用的小事建子会话」容易被泛化成「改几个文件也不必派」。
   收紧为「单文件单处修改、纯问答、一两次工具调用」才算小事。
4. 缺反例清单——模型会用专业口吻的搪塞理由说服自己串行，尤其把「验证环境冲突」
   误当成「不能派」。现在点名列出，并给出正确做法：**串行派发**，而不是全部自做。

【注入时机】
1. 每轮 turn 开始（`build_main_context`）：完整提示进 developer 段；
2. 运行中注入的排队消息（agent_loop 每步 drain）：给该条用户消息附一行简短提醒。

【不注入的情况】
- 设置中心关闭「优先使用子会话并行处理」（`settings.parallel_subsession_hint=False`）；
- 当前执行模式不下发子会话工具（只读 / 计划 / 工作流，见 subagent_capabilities）——
  否则等于让模型调用一个不存在的工具。
"""
from __future__ import annotations

# 完整提示：放在 developer 段、紧随 Current Goal 之后（模型注意力最高的位置之一）
PARALLEL_SUBSESSION_HINT = (
    "## 并行分派子会话（默认策略，不是可选项）\n"
    "开始执行前，把任务拆成工作点，逐个过下面两条判据。**两条同时满足就派**，不需要再找别的理由：\n"
    "1. **写入范围零交集**：各自改动的文件/目录集合不相交。「改的是同一个模块」不算相交，"
    "只有真的写同一批文件才算。\n"
    "2. **验证通道不冲突**：不共用同一个内置浏览器标签、同一个测试库、同一个服务端口。\n"
    "- 若验证通道冲突但这项工作确实该派：**串行派发**——让 A 先跑完验证，再派 B。"
    "不要因为「只能一个个验证」就全部自己做。\n"
    "\n**强制动作**：任务清单必须逐项标注「自做」或「派发」。"
    "有两个以上满足判据的工作点却一个都没派，视为分派不足，必须说明为什么。\n"
    "\n**典型该派的工作**（写入零交集时一律派出去）：纯样式 / 纯文案调整、"
    "独立模块或独立文件组、补测试与回归用例、更新日志与文档归类、多步界面操作（purpose=operator）。\n"
    "\n**禁止用这些理由把工作全揽在自己手里**：\n"
    '- 「改动集中在少数文件」——只在写入集合真的相交时成立；两个不相交的文件就不是集中。\n'
    '- 「验证要共用同一个环境」——按上面的串行派发处理，不是取消派发。\n'
    '- 「需求之后可能还会变」——已确定的部分现在就派，新增的增量再自己接手。\n'
    '- 「自己写更快」——你的价值在设计、判断与整合；逐处敲代码会挤占这些所需的上下文。\n'
    "\n**派发口径**：默认 background=true，派发后立即返回、不要轮询，结论会自动回到你的上下文；"
    "只有下一步马上要用它的结论时才设 background=false。\n"
    "task 必须自包含（它看不到你的对话）；context 写清你已经完成了什么、明确它不要做什么。\n"
    "派出去的工作不要自己再做一遍；强依赖的步骤留在自己手里串行做。\n"
    "\n**只有这几种情况自己直接做**：单文件单处修改、纯问答或解释、"
    "一两次工具调用就能完成的小事；只读调研用只读子代理（spawn_subagent），不占用子会话。"
)

# 排队消息附带的简短提醒：完整提示在 turn 开始时已给过，这里只做「本轮仍适用」的复述，避免重复长文本
PARALLEL_SUBSESSION_REMINDER = (
    "（提醒：新出现的工作点若写入范围互不相交、验证通道不冲突，就用 create_subsession 派出去；"
    "验证冲突时改为串行派发，不要全部自己做。派出去的工作不要自己再做一遍。）"
)

# 计划阶段提示（plan-156-623）：计划模式不下发 create_subsession 工具，
# 但**制定计划时就必须把并行拆分想清楚并写进文档**——否则确认执行时只能串行补拆。
PLAN_PARALLEL_SECTION_HINT = (
    "## 计划中的并行拆分（优先子会话）\n"
    "你当前处于计划模式，不能也不应该创建子会话；但方案文档必须提前写好并行拆分，"
    "让确认执行阶段能直接按它分派。写文档时逐个步骤判断下面两条判据：\n"
    "- **写入范围**是否互斥（各自改动的文件/目录集合不相交）；\n"
    "- **验证通道**是否冲突（不要把需要同一个浏览器标签 / 测试库 / 端口的步骤放进同一组）。\n"
    "两条都满足的步骤，在文档里用「**可并行子会话执行**」小节列出分组，每组写清："
    "要做什么、写入范围（必须互斥）、依赖哪一步的产物（有依赖就说明为什么不能并行）。\n"
    "- 验证通道冲突的步骤不要硬放进同一组：按顺序排成「先 A 后 B」，执行期串行派发即可；\n"
    "- 有先后依赖的步骤按顺序排好，不要为了凑并行把强依赖的步骤硬拆开；\n"
    "- 单文件单处修改、一两个工具调用就能完成的小事不必拆分，直接写成串行步骤。\n"
    "计划文档没有这层拆分时，执行阶段只能串行推进，用户会失去并行提速的收益。"
)

# 确认执行阶段的提示（plan-156-623）：确认计划卡后注入 instruction，
# 与 `build_main_context` 的完整提示互补——即使模式判定未命中，执行期也明确按计划并行。
EXECUTE_PARALLEL_SUBSESSION_HINT = (
    "【并行执行要求】先按方案文档里的「可并行子会话执行」分组派发 create_subsession："
    "同一组内的独立步骤在一条消息里同时派发（默认 background=true，不要轮询），"
    "写入范围互斥的组才可以并行；有依赖的步骤自己串行做，或等前一组结论回来再派发。"
    "文档没有拆分、但你发现两个以上写入范围互不相交的工作点时，同样要派，不要因为文档没写就串行做完。"
    "验证通道冲突时串行派发（先 A 后 B），不要因此全部自己做。"
    "派出去的工作不要自己再做一遍；单文件单处修改、一两次工具调用的小事不必拆子会话。"
)


def _hint_enabled() -> bool:
    """设置中心「优先使用子会话并行处理」是否开启（显式 false 才关闭）。"""
    try:
        from app.core.config import settings

        # plan-156-622：只有显式关闭才不注入——默认开启。
        # 用 `is False` 而不是 `not`：配置项为 null / 属性缺失时都保持注入。
        return getattr(settings, "parallel_subsession_hint", True) is not False
    except Exception:  # noqa: BLE001 — 配置层不可用时不注入，避免误伤
        return False


def parallel_subsession_hint(mode: str | None, *, full: bool = True) -> str:
    """按设置与执行模式返回提示词；不应注入时返回空串。

    `full=False` 返回排队消息用的简短提醒（同一套开关判断）。
    """
    if not _hint_enabled():
        return ""

    from app.orchestration.subagent_capabilities import allows_subsession

    if not allows_subsession(mode):
        return ""
    return PARALLEL_SUBSESSION_HINT if full else PARALLEL_SUBSESSION_REMINDER


def plan_parallel_subsession_hint(mode: str | None) -> str:
    """计划模式专用：要求把「可并行子会话执行」的分组写进方案文档。

    plan-156-623：计划模式按能力矩阵不下发 create_subsession，旧实现因此整段跳过，
    计划文档里没有任何并行拆分信息，确认执行时只能串行。这里补一份**只读型**要求
    （只写文档、不派发），执行期再按它派发；其它模式返回空串，避免与完整提示重复。
    """
    if not _hint_enabled():
        return ""

    from app.orchestration.subagent_capabilities import MODE_PLAN, effective_mode

    if effective_mode(mode) != MODE_PLAN:
        return ""
    return PLAN_PARALLEL_SECTION_HINT


def execute_parallel_subsession_hint() -> str:
    """确认计划卡后的执行期提示：按文档里的并行分组派发子会话。

    与 `build_main_context` 的完整提示互补——确认执行路径的 instruction 是最贴近
    本轮目标的注入点，不依赖会话模式判定是否已切到 agent。
    """
    if not _hint_enabled():
        return ""
    return EXECUTE_PARALLEL_SUBSESSION_HINT


# plan-20261018-operator-boost：GUI 操作交给操控子会话。操作类工作每步都是一次模型往返，
# 思考越深越慢，却几乎不需要深度推理；交给最低思考档的子会话执行，主代理保留深度思考做设计与结论。
OPERATOR_SUBSESSION_HINT = (
    "## 界面操作交给操控子会话\n"
    "需要多步界面操作时（内置浏览器自测改动、导出文件、走一遍业务流程，或电脑操控打开软件核对），"
    "用 create_subsession 并设 purpose=operator 派给操控子会话：它以最低思考深度执行，速度快、不占你的上下文，"
    "你保留深度思考处理设计、编码与结论。\n"
    "- 需要它的结论才能继续（例如改完前端要确认效果）时同时设 background=false，结论会直接返回给你；\n"
    "- task 写清：地址或应用、要做的步骤、要核对的点、要带回的证据（关键现象、报错原文、下载文件路径）；\n"
    "- 一两步就能完成的简单操作（打开页面看一眼、点一个按钮）自己直接做，不必派发。"
)


def operator_subsession_hint(mode: str | None, *, gui_tools: bool, is_subsession: bool = False) -> str:
    """按设置、执行模式与 GUI 工具可用性返回「交给操控子会话」的提示；不应注入时返回空串。"""
    if not gui_tools or is_subsession:
        return ""
    try:
        from app.core.config import settings

        if getattr(settings, "operator_subsession_enabled", True) is False:
            return ""
    except Exception:  # noqa: BLE001 — 配置层不可用时不注入
        return ""
    from app.orchestration.subagent_capabilities import allows_subsession

    if not allows_subsession(mode):
        return ""
    return OPERATOR_SUBSESSION_HINT
