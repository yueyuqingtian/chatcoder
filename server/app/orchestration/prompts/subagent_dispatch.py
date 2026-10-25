"""子代理派发提示词（plan-549-2281）。

【为什么重写】
子会话（subsession）下线后，`spawn_subagent` 成为唯一的并行执行通道，原本分散在
`parallel_subsession.py` 里的「派发要求」必须整体改写。旧提示词有四类结构性问题，
本版逐条修掉：

1. **判据是感受性的**（「彼此隔离、互不影响」）——无法证伪，任何理由都能否决它。
   改为两条**可判定**判据：写入路径集合是否相交、验证通道是否冲突。
2. **没有强制动作**——改为要求把任务清单逐项标注「自做 / 派发」，并给出分派不足的自检条件。
3. **边界条款过宽**——收紧为「单文件单处修改、纯问答、一两次工具调用」才算小事。
4. **缺反例清单**——模型会用专业口吻的搪塞理由说服自己串行，点名列出并给正确做法
   （验证可冲突时**串行派发**，而不是全部自做）。

另外补上旧提示词完全没有的一点：**执行模式的用法**。旧提示词只说「只读调研用子代理」，
加上系统提示词里那句「implementation is never delegated」，等于禁止把实现派出去；
而工具层的 `explore=false` 其实是可写的。现在明确：调研派 `mode=readonly`，
**执行派 `mode=agent`，实现可以且应该委派**（智能体模式会话）。

plan-549-2281 复盘修复：本文件从旧 `create_subsession` 提示词搬运时带进了三个已删除的
参数名（`task` / `context` / `background`）——`spawn_subagent` 没有这些参数，模型照做会
静默无效（该阻塞等结论时不去 `collect_results(wait=true)`）。所有参数话术已按
`SPAWN_SUBAGENT_SCHEMA` 的真实参数面（task_title / task_description /
acceptance_criteria / mode / explore / purpose）重写。

【注入时机】
1. 每轮 turn 开始（`build_main_context`）：完整提示进 developer 段；
2. 运行中注入的排队消息（agent_loop 每步 drain）：给该条用户消息附一行简短提醒。

【不注入的情况】
- 设置中心关闭「子代理」开关（`settings.subagent_enabled=False`）；
- **调用方本身是子代理**——它不能再派发下级，注入只会诱导它调用一个不存在的工具；
- **历史挂靠子会话**（`side_kind=sub`）——它没有派发工具，由 LEGACY_CHILD_SESSION_HINT 单独约束。

只读 / 计划模式**仍注入**（工具面下发 explore_only 的 spawn_subagent），但换成受限版：
只引导派只读调研子代理——运行时会强制只读（agent_loop 的模式语义护栏），完整版里
「实现派 mode=agent」的引导在这两类模式下是做不到的空话，必须同口径收窄。

plan-560-2311 C2（用户决策）：**workflow 等同 agent**——它的 kind 是 full，工具面与审批
边界都和 agent 一致，因此拿完整版派发提示与 operator 提示；早先「workflow 强制只读 explore」
的声明与实现不符，已随本次修复删除。工作流节点自身的写入仍受 `write_scope` 约束
（`workflow/guard.py`），与本文件无关。
"""
from __future__ import annotations

# 完整提示：放在 developer 段、紧随 Current Goal 之后（模型注意力最高的位置之一）
SUBAGENT_DISPATCH_HINT = (
    "## 派发子代理（默认策略，不是可选项）\n"
    "开始执行前，把任务拆成工作点，逐个过下面两条判据。**两条同时满足就派**，不需要再找别的理由：\n"
    "1. **写入范围零交集**：各自改动的文件/目录集合不相交。「改的是同一个模块」不算相交，"
    "只有真的写同一批文件才算。\n"
    "2. **验证通道不冲突**：不共用同一个内置浏览器标签、同一个测试库、同一个服务端口。\n"
    "- 若验证通道冲突但这项工作确实该派：**串行派发**——让 A 先跑完验证，再派 B。"
    "不要因为「只能一个个验证」就全部自己做。\n"
    "\n**派发时必须指定执行模式**（`mode` 是子代理能力边界的唯一开关，用 `spawn_subagent` 派发）：\n"
    "- 调研 / 审阅 / 分析 → `mode=\"readonly\"`：只读，不能写文件、不能跑命令；\n"
    "- 实现 / 改动 / 修 bug / 补测试 → `mode=\"agent\"`：可写可执行。"
    "**实现可以且应该委派**——写入范围互不相交的实现工作派出去比自己逐处敲更快。\n"
    "- 子代理的权限模式（要不要审批）始终继承本会话，不是派发参数。\n"
    "\n**强制动作**：任务清单必须逐项标注「自做」或「派发」。"
    "有两个以上满足判据的工作点却一个都没派，视为分派不足，必须说明为什么。\n"
    "\n**典型该派的工作**（写入零交集时一律派出去）：纯样式 / 纯文案调整、"
    "独立模块或独立文件组、补测试与回归用例、更新日志与文档归类、多步界面操作（purpose=operator）。\n"
    "\n**禁止用这些理由把工作全揽在自己手里**：\n"
    '- 「改动集中在少数文件」——只在写入集合真的相交时成立；两个不相交的文件就不是集中。\n'
    '- 「验证要共用同一个环境」——按上面的串行派发处理，不是取消派发。\n'
    '- 「需求之后可能还会变」——已确定的部分现在就派，新增的增量再自己接手。\n'
    '- 「自己写更快」——你的价值在设计、判断与整合；逐处敲代码会挤占这些所需的上下文。\n'
    "\n**派发与等待的口径**：派发后立即返回，结论不会塞在派发结果里——子代理完成后会自动推送回"
    "你的上下文；下一步马上要用它的结论时，调 `collect_results(wait=true)` 阻塞等它结束。不要轮询。\n"
    "task_description 必须自包含（它看不到你的对话）：写清背景、你已经完成了什么、明确它不要做什么；"
    "验收标准放 acceptance_criteria。\n"
    "派出去的工作不要自己再做一遍；强依赖的步骤留在自己手里串行做。\n"
    "\n**只有这几种情况自己直接做**：单文件单处修改、纯问答或解释、"
    "一两次工具调用就能完成的小事。\n"
    "\n**子代理提问的处理（必须遵守）**：你会收到形如"
    "「【子代理提问 subagent#N「标题」】…」的消息，它可能打断你正在等待的命令。\n"
    "1. 能自己回答 → 立刻用 send_to_subagent(agent_id, message) 答复，等待中的子代理会直接拿到这句继续执行；\n"
    "2. 答不了（涉及用户偏好 / 需求取舍 / 权限范围）→ 先用 ask_user_question 问用户，"
    "拿到回答后再用 send_to_subagent 转达；\n"
    "3. **不要忽略提问**——不答复的子代理会阻塞到超时，然后按自己的判断乱猜；\n"
    "4. 需要补充要求或纠正方向时，可以**多次**调用 send_to_subagent，最新一条优先。"
)

# 只读 / 计划模式的受限版：完整版里「实现派 mode=agent」在这两类模式下做不到
# （运行时强制 explore），必须同口径收窄，否则模型按提示派发却拿不到可写子代理。
SUBAGENT_DISPATCH_HINT_READONLY = (
    "## 派发子代理（本会话为只读 / 计划模式）\n"
    "本会话不产生业务写入，只允许派发**只读调研子代理**——运行时也会把派发强制为只读，"
    "设 `mode=\"agent\"` 不会生效。\n"
    "- 可派：并行检索 / 审阅 / 分析 / 读代码核对结论（`mode=\"readonly\"`，或省略 mode 默认只读）；\n"
    "- 不派：实现 / 改动 / 修 bug——需要改代码时，先在回复里向用户说明，并建议切换到智能体模式后执行；\n"
    "- 派发后立即返回，结论完成后自动推送回你的上下文；马上要用结论时调 `collect_results(wait=true)`。\n"
    "task_description 必须自包含（它看不到你的对话）。"
)

# 排队消息附带的简短提醒：完整提示在 turn 开始时已给过，这里只做「本轮仍适用」的复述，避免重复长文本
SUBAGENT_DISPATCH_REMINDER = (
    "（提醒：新出现的工作点若写入范围互不相交、验证通道不冲突，就派 spawn_subagent 出去——"
    "调研用 mode=readonly，实现用 mode=agent；验证冲突时串行派发，不要全部自己做。"
    "派出去的工作不要自己再做一遍。）"
)

SUBAGENT_DISPATCH_REMINDER_READONLY = (
    "（提醒：本会话为只读 / 计划模式，只可派只读调研子代理（mode=readonly）；"
    "实现类工作不要派发——需要改动时先向用户说明并建议切换智能体模式。）"
)

# 界面操作交给操控子代理（原 plan-20261018-operator-boost 的 operator 子会话，plan-549-2281 迁移）
# 操作类工作每步都是一次模型往返，思考越深越慢，却几乎不需要深度推理；交给最低思考档的子代理执行，
# 主代理保留深度思考做设计与结论。
OPERATOR_SUBAGENT_HINT = (
    "## 界面操作交给操控子代理\n"
    "需要多步界面操作时（内置浏览器自测改动、导出文件、走一遍业务流程，或电脑操控打开软件核对），"
    "用 `spawn_subagent` 并设 `purpose=operator` 派给操控子代理：它以最低思考深度执行，速度快、不占你的上下文，"
    "你保留深度思考处理设计、编码与结论。\n"
    "- 需要它的结论才能继续（例如改完前端要确认效果）时，派发后调 `collect_results(wait=true)` 等它结束；\n"
    "- task_description 写清：地址或应用、要做的步骤、要核对什么、要带回的证据（关键现象、报错原文、下载文件路径）；\n"
    "- 一两步就能完成的简单操作（打开页面看一眼、点一个按钮）自己直接做，不必派发。"
)

# 历史挂靠会话（plan-549-2281）：子会话创建能力已下线，但历史会话仍可打开并继续对话。
# 它没有子会话工具、也没有派发工具，需要一条轻量约束说明「不要再找派发入口」。
LEGACY_CHILD_SESSION_HINT = (
    "## 历史子会话（能力已下线）\n"
    "本会话是旧版本创建的子会话。子会话的创建与控制能力已经下线，"
    "你现在既不能创建子会话，也不能派发子代理。\n"
    "正常执行用户要求，需要澄清时先自己核实代码与文档；仍无法判断就直接向用户提问，"
    "并在最终回复里说明未决项。不要尝试调用不存在的工具。"
)


def _subagents_enabled() -> bool:
    """设置中心「子代理」总开关（显式 false 才关闭）。"""
    try:
        from app.core.config import settings

        # 用 `is not False` 而不是 `bool()`：配置项为 null / 属性缺失时都保持注入。
        # 开关关闭时工具已不下发，提示词必须同口径停止注入，否则模型会反复调用不存在的工具。
        return getattr(settings, "subagent_enabled", True) is not False
    except Exception:  # noqa: BLE001 — 配置层不可用时不注入，避免误伤
        return False


def _hint_variant(mode: str | None) -> tuple[str, str] | None:
    """按执行模式选（完整版, 简短提醒）或受限版；不该注入返回 None。"""
    from app.orchestration.subagent_capabilities import effective_mode

    em = effective_mode(mode)
    # plan-549-2281：只读 / 计划模式运行时会强制 explore（agent_loop 模式语义护栏），
    # 完整版里「实现派 mode=agent」是做不到的引导——必须收窄成只读版，两边口径一致。
    # plan-560-2311 C2：判定按归一档（effective_mode）而非模式名——workflow 等同 agent，
    # 拿得到完整版（含「实现/改动 → mode="agent"」）；自定义 kind=readonly/plan 模式
    # 也因归一面正确落到受限版，不再因名字不在白名单里而漏判。
    if em in ("readonly", "plan"):
        return SUBAGENT_DISPATCH_HINT_READONLY, SUBAGENT_DISPATCH_REMINDER_READONLY
    return SUBAGENT_DISPATCH_HINT, SUBAGENT_DISPATCH_REMINDER


def subagent_dispatch_hint(
    mode: str | None, *, full: bool = True, is_subagent: bool = False,
    is_legacy_child: bool = False,
) -> str:
    """按设置与执行模式返回派发提示词；不应注入时返回空串。

    `full=False` 返回排队消息用的简短提醒（同一套开关判断）。
    `is_subagent=True`（调用方本身是子代理）一律返回空串——它不能再派发下级。
    `is_legacy_child=True`（历史挂靠子会话）同上——它没有派发工具，
    由 legacy_child_session_hint 单独约束。
    """
    if is_subagent or is_legacy_child or not _subagents_enabled():
        return ""
    variant = _hint_variant(mode)
    if variant is None:
        return ""
    return variant[0] if full else variant[1]


def operator_subagent_hint(
    mode: str | None, *, gui_tools: bool, is_subagent: bool = False,
    is_legacy_child: bool = False,
) -> str:
    """按设置、执行模式与 GUI 工具可用性返回「交给操控子代理」的提示；不应注入时返回空串。"""
    if not gui_tools or is_subagent or is_legacy_child or not _subagents_enabled():
        return ""
    try:
        from app.core.config import settings

        # 操控子代理是子代理的一种形态：子代理总开关之外，它还跟随独立的视觉/操作开关
        if getattr(settings, "operator_subagent_enabled", True) is False:
            return ""
    except Exception:  # noqa: BLE001 — 配置层不可用时不注入
        return ""
    # plan-549-2281：操控子代理一定会写界面（点按、输入、下载），只读 / 计划模式既不能派
    # 可写子代理、也不该出现「交给操控子代理」的引导，否则等于让模型诱导一次必被拒的派发。
    # plan-560-2311 C2：判定按归一档而非 `== "agent"`——workflow 等同 agent（kind=full），
    # 旧写法让 workflow 下 operator 提示整体不注入，与「可派可写子代理」的口径矛盾。
    from app.orchestration.subagent_capabilities import effective_mode

    if effective_mode(mode) not in ("agent", "workflow"):
        return ""
    return OPERATOR_SUBAGENT_HINT


def legacy_child_session_hint(is_legacy_child: bool) -> str:
    """历史挂靠会话专用约束（plan-549-2281）。非历史子会话返回空串。"""
    return LEGACY_CHILD_SESSION_HINT if is_legacy_child else ""


# 确认计划卡后的执行期提示（原 plan-156-623 的 execute_parallel_subsession_hint）。
# 确认执行是本轮最贴近目标的注入点：方案文档里的「可并行」分组只有在这里被读到，
# 执行指令若仍写死「串行推进」等于让计划白拆。
# plan-549-2281 复盘修复：去掉旧参数 `background=true` 的话术（参数已删除），
# 等待语义改由 collect_results(wait=true) 表达；engine 侧注入前判「子代理」开关。
EXECUTE_DISPATCH_HINT = (
    "【执行要求】按方案文档里的「可并行」分组派发 spawn_subagent："
    "同一组内的独立步骤在一条消息里同时派发（派发后继续推进其余步骤，不要轮询），"
    "写入范围互斥的组才可以并行；研究/审阅用 mode=\"readonly\"，实现用 mode=\"agent\"。"
    "有依赖的步骤自己串行做，或等前一组结论回来再派发"
    "（需要结论才能继续时用 collect_results(wait=true)）。"
    "文档没有拆分、但你发现两个以上写入范围互不相交的工作点时，同样要派，不要因为文档没写就串行做完。"
    "验证通道冲突时串行派发（先 A 后 B），不要因此全部自己做。"
    "派出去的工作不要自己再做一遍；单文件单处修改、一两次工具调用的小事不必拆子代理。"
)
