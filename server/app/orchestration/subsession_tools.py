"""子会话工具 schema（plan-156-619 S4）。

【与 `subagent_tools.py` 的分工】
两套机制并存，职责不重叠：

| 机制 | 用途 | 存储 | 展示 |
|---|---|---|---|
| 只读子代理 | 探索 / 调研 | `agents(kind=sub)` + `messages.thread_id` | 右侧面板 |
| 子会话 | 执行 / 并行 | `sessions(side_parent_id)` | 左侧栏 + 主区（普通会话）|

- 探索阶段用 `spawn_subagent(explore=true)`，主代理拿到结论回到自己上下文；
- 执行阶段用 `create_subsession`，子会话有独立 turn 流水线、用户可直接接管。

因此两者**都保留**，但按执行模式分别下发（见 `subagent_capabilities.py`）。

【主会话侧 4 个工具】
`create_subsession` / `send_to_subsession` / `get_subsession_progress` / `interrupt_subsession`。
历史上分散的 `collect_results(wait=true)` / `subagent_inspect` / `resume_subagent` 语义
并入「进度查询」与「发消息」两项，避免主代理在 6 个工具里挑。

【子会话侧 2 个工具】
`message_main`（向主代理汇报 / 提问）、`message_peer`（与同父子会话通信）。
子会话不暴露 `create_subsession`，避免无限套娃。
"""
from __future__ import annotations

from app.orchestration.approval_policy import EXECUTION_MODES

# ── 工具名常量（各处引用同一份，避免裸字面量）──
TOOL_CREATE = "create_subsession"
TOOL_SEND = "send_to_subsession"
TOOL_PROGRESS = "get_subsession_progress"
TOOL_INTERRUPT = "interrupt_subsession"
TOOL_MESSAGE_MAIN = "message_main"
TOOL_MESSAGE_PEER = "message_peer"

CREATE_SUBSESSION_SCHEMA = {
    "type": "function",
    "function": {
        "name": TOOL_CREATE,
        "description": (
            "创建一个子会话，把写入范围独立、可独立验证的**执行**任务交给它。\n"
            "\n## 什么时候该用（开工前先想这一步）\n"
            "- 任务清单里有两项以上写入范围互不相交的工作（改动的文件集合不重叠），派出去并行推进。\n"
            "- 某一步会长时间占用上下文（大量文件读写、反复试错），交给子会话可以保住你的上下文。\n"
            "- 纯样式 / 纯文案、独立模块、补测试、更新日志与文档归类，都是典型可派对象。\n"
            "\n## 什么时候不要用\n"
            "- 只是查资料、读代码、做调研 —— 那是探索，用只读子代理（`spawn_subagent`），不要建子会话。\n"
            "- 步骤之间有强依赖（后一步需要前一步的中间产物）—— 串行自己做。\n"
            "- 单文件单处修改、纯问答、一两次工具调用就能完成的小事 —— 直接做。\n"
            "- 多个工作点共用同一个验证环境（同一浏览器标签 / 测试库 / 端口）—— "
            "仍应**串行派发**（先 A 后 B），不要因为只能逐个验证就全部自己做。\n"
            "\n## 写法要求\n"
            "- `task` 必须是自包含的：子会话看不到你的对话，把问题、要求、参考位置（文件 / 符号）都写清。\n"
            "- `context` 交代背景：你已经完成了什么、它不要重复做什么、有哪些约束。\n"
            "- `mode` 决定子会话的能力边界，默认 `agent`；纯调研用 `readonly`。\n"
            "- 多步界面操作（内置浏览器 / 电脑操控）设 `purpose=operator`：以最低思考深度快速执行；"
            "需要它的结论才能继续时同时设 `background=false`。\n"
            "\n## 之后\n"
            "- 子会话会出现在用户左侧栏，用户可自行接管或中断；你也可以用 "
            "`get_subsession_progress` 查进度、`send_to_subsession` 补充要求、"
            "`interrupt_subsession` 中断（谨慎使用）。\n"
            "- 派出去的工作**不要自己再做一遍**。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "子会话标题（不超过 40 字，动宾结构），会直接显示在左侧栏",
                },
                "task": {
                    "type": "string",
                    "description": (
                        "自包含的任务说明：要解决什么问题、具体要求、参考的文件或符号、"
                        "以及完成标准。使用当前对话语言。"
                    ),
                },
                "context": {
                    "type": "string",
                    "description": (
                        "背景上下文：你已经完成了哪些内容（避免它重复做）、"
                        "以及明确提醒它不要做什么。使用当前对话语言。"
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": list(EXECUTION_MODES),
                    "description": (
                        "子会话的执行模式：readonly=只能读；plan=只读+写计划；"
                        "agent=全量（默认）；workflow=以工作流方式完成。"
                    ),
                },
                "background": {
                    "type": "boolean",
                    "description": (
                        "默认 true：立即返回，子会话在后台跑，完成后结果自动回到你的上下文。"
                        "只有「下一步马上就要用到它的结果」时才设 false 阻塞等待。"
                    ),
                },
                "purpose": {
                    "type": "string",
                    "enum": ["", "operator"],
                    "description": (
                        "operator = 操控子会话：专门执行多步界面操作（内置浏览器测试 / 导出、电脑操控），"
                        "以最低思考深度运行，快且不占你的上下文；task 写清要打开的地址 / 应用、操作步骤、"
                        "要核对什么、要带回什么（看到的关键现象、报错原文、下载文件路径）。普通任务留空。"
                    ),
                },
            },
            "required": ["title", "task"],
        },
    },
}

SEND_TO_SUBSESSION_SCHEMA = {
    "type": "function",
    "function": {
        "name": TOOL_SEND,
        "description": (
            "向一个子会话发消息：运行中则把要求注入它下一次模型调用前（不打断当前步骤）；"
            "已中断/暂停则从检查点继续跑。"
            "用于补充约束、纠正方向、或让它继续没做完的工作。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "subsession_id": {
                    "type": "integer",
                    "description": "子会话 id（create_subsession 的返回值，或左侧栏会话 id）",
                },
                "message": {
                    "type": "string",
                    "description": "要传达的内容，需自包含（子会话看不到你与用户的对话）",
                },
            },
            "required": ["subsession_id", "message"],
        },
    },
}

GET_SUBSESSION_PROGRESS_SCHEMA = {
    "type": "function",
    "function": {
        "name": TOOL_PROGRESS,
        "description": (
            "查询子会话的进度与结果：状态、步骤进度、已改动文件，以及子会话最近一轮的**完整结论原文**。"
            "不传 subsession_id 时汇总本会话创建的全部子会话（每个结论给出开头部分并标明总字数）；"
            "传 subsession_id 时返回该子会话结论全文，过长时分段返回，"
            "按结果末尾提示的 report_offset 继续读取，直到读完——不要根据开头部分臆测结论。"
            "wait=true 时阻塞到它们全部结束（优先用它，而不是反复轮询）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "subsession_id": {
                    "type": "integer",
                    "description": "只看这一个子会话；省略则汇报全部",
                },
                "wait": {
                    "type": "boolean",
                    "description": "true = 阻塞到目标子会话结束再返回（默认 false）",
                },
                "report_offset": {
                    "type": "integer",
                    "description": "分段读取结论原文的起始字符位置（配合 subsession_id 使用，默认 0）",
                },
            },
            "required": [],
        },
    },
}

INTERRUPT_SUBSESSION_SCHEMA = {
    "type": "function",
    "function": {
        "name": TOOL_INTERRUPT,
        "description": (
            "中断一个仍在运行的子会话。**谨慎使用**：子会话已作为普通会话展示在用户左侧栏，"
            "用户可以自己中断它——只有在你确定这项派发已无意义（计划变了、范围被砍掉）时才调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "subsession_id": {"type": "integer", "description": "要中断的子会话 id"},
            },
            "required": ["subsession_id"],
        },
    },
}

MESSAGE_MAIN_SCHEMA = {
    "type": "function",
    "function": {
        "name": TOOL_MESSAGE_MAIN,
        "description": (
            "向主代理汇报进度或提问（仅子会话可用）。消息会进入主代理下一次模型调用的上下文，"
            "同时显示在你自己的会话里。kind=progress（默认）是异步状态更新；"
            "kind=question 用于标记「需要主代理决定」——同样是异步送达，"
            "不要在等待回复上空转，把未决问题写进自己的最终结论即可。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "要汇报或询问的内容"},
                "kind": {
                    "type": "string",
                    "description": "progress（默认）= 状态更新；question = 需要主代理决定",
                },
            },
            "required": ["message"],
        },
    },
}

MESSAGE_PEER_SCHEMA = {
    "type": "function",
    "function": {
        "name": TOOL_MESSAGE_PEER,
        "description": (
            "向同一主会话下的其他子会话发消息（仅子会话可用）。"
            "只能联系与自己同级的子会话——跨主会话的联系会被服务端拒绝。"
            "用于对齐接口约定、交接中间产物、避免重复改动同一处代码。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "subsession_id": {"type": "integer", "description": "同级子会话 id"},
                "message": {"type": "string", "description": "要传达的内容"},
            },
            "required": ["subsession_id", "message"],
        },
    },
}

# 主会话可见的四个工具（按能力矩阵下发）
SUBSESSION_TOOL_SCHEMAS: list[dict] = [
    CREATE_SUBSESSION_SCHEMA,
    SEND_TO_SUBSESSION_SCHEMA,
    GET_SUBSESSION_PROGRESS_SCHEMA,
    INTERRUPT_SUBSESSION_SCHEMA,
]

# 子会话自身可见的通信工具（主会话不可见）
SUBSESSION_OWN_TOOLS: list[dict] = [MESSAGE_MAIN_SCHEMA, MESSAGE_PEER_SCHEMA]

SUBSESSION_TOOL_NAMES: set[str] = (
    {s["function"]["name"] for s in SUBSESSION_TOOL_SCHEMAS}
    | {s["function"]["name"] for s in SUBSESSION_OWN_TOOLS}
)

# 供权限面板展示的风险级（与 SUBAGENT_TOOL_RISK 同口径）
SUBSESSION_TOOL_RISK: dict[str, str] = {
    TOOL_CREATE: "medium",
    TOOL_SEND: "low",
    TOOL_PROGRESS: "low",
    TOOL_INTERRUPT: "low",
    TOOL_MESSAGE_MAIN: "low",
    TOOL_MESSAGE_PEER: "low",
}


def append_subsession_tools(tool_schemas: list[dict], *, enabled: bool) -> list[dict]:
    """为主代理工具列表追加子会话工具组（幂等）。

    `enabled=False`（非智能体模式）时**先剔除**列表中已有的子会话工具再返回——
    只做「不追加」是不够的：白名单模式下 `_schema_whitelist` 可能已经把
    `create_subsession` 带进来，那样仍然会绕过能力矩阵。剔除保证口径唯一。
    """
    out = [
        s for s in tool_schemas
        if s.get("function", {}).get("name") not in SUBSESSION_TOOL_NAMES
    ]
    if not enabled:
        return out
    names = {s.get("function", {}).get("name") for s in out}
    for schema in SUBSESSION_TOOL_SCHEMAS:
        name = schema["function"]["name"]
        if name not in names:
            out.append(schema)
            names.add(name)
    return out


def append_own_tools(tool_schemas: list[dict]) -> list[dict]:
    """为**子会话自己**追加通信工具（message_main / message_peer）。"""
    names = {s.get("function", {}).get("name") for s in tool_schemas}
    out = list(tool_schemas)
    for schema in SUBSESSION_OWN_TOOLS:
        name = schema["function"]["name"]
        if name not in names:
            out.append(schema)
            names.add(name)
    return out
