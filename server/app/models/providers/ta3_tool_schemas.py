"""ta3 原生工具 schema 移植（OpenAI function 格式，中文 description 原文保留）。

来源：参考项目 resources/app-extracted/src/tools/toolDefinitions/
- core.ts：Read/List/Search/Diff/ReadSkill/get_project_memory/generate_project_memory/
          read_file_range/get_file_outline
- edit.ts：Write/Edit/Bash/RevertFile/single_find_and_replace
- task.ts：TodoWrite/SubAgent/SubAgentAsync/TaskQuery/TaskCancel
- webSearch.ts：WebSearch
- attachment（plan-147-674）：ReadAttachment/ViewImage 为当前项目补充（参考项目
  无对应工具），用于附件与图片读取，伪装名对齐 ta3 PascalCase 风格

只取发给模型的 {type, function:{name, description, parameters}} 字段。
"""
from __future__ import annotations


def _f(name: str, description: str, parameters: dict) -> dict:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": parameters}}


# ── core ──
_CORE: list[dict] = [
    _f("Read", "读取工作区内指定文件内容。适合查看现有代码和配置文件。", {
        "type": "object", "required": ["filepath"],
        "properties": {
            "filepath": {"type": "string", "description": "文件路径，可以是相对工作区根目录的路径或工作区内绝对路径。"},
            "offset": {"type": "integer", "description": "从第几行开始读取(1-based, 默认1)"},
            "limit": {"type": "integer", "description": "最多读取多少行(默认200, 单次最多400行, 大文件可分段读取)"},
        },
    }),
    _f("List", "列出工作区内目录下的文件和文件夹。", {
        "type": "object",
        "properties": {
            "dirPath": {"type": "string", "description": "目录路径，默认为工作区根目录。"},
            "recursive": {"type": "boolean", "description": "是否递归列出。大型项目中请谨慎使用。"},
            "max_depth": {"type": "integer", "description": "递归最大深度（默认 2，仅 recursive=true 时有效）。"},
        },
    }),
    _f("Search", "使用 ripgrep 在工作区中搜索正则表达式，自动跳过常见构建和依赖目录。", {
        "type": "object", "required": ["query"],
        "properties": {
            "query": {"type": "string", "description": "要搜索的正则表达式。"},
            "path": {"type": "string", "description": "可选的搜索目录或文件路径。"},
            "include": {"type": "string", "description": "只搜索匹配此 glob 模式的文件（如 '*.py'、'*.java'、'*.ts'）。默认搜索所有文本文件。"},
            "case_sensitive": {"type": "boolean", "description": "是否区分大小写。默认 false。"},
            "context_lines": {"type": "integer", "description": "每个匹配项上下文行数（前后各显示多少行）。默认 0，最大 5。"},
        },
    }),
    _f("Diff", "查看 git 仓库中未提交的代码变更。多 git 仓库场景（工作根含多个独立仓库）必须用 repo 指定仓库目录。", {
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "git 仓库目录名（相对工作根，如 'clinic'）。多仓库场景必须指定。"},
            "tracked_only": {"type": "boolean", "description": "True=仅已跟踪文件的变更(默认);False=包含未跟踪新文件。"},
            "stat_only": {"type": "boolean", "description": "True=只返回文件统计(文件名+增删行数),不返回完整 diff(默认);False=返回完整 diff。"},
        },
    }),
    _f("ReadSkill", "按技能名称读取完整技能说明。可用技能会出现在系统提示词的 <available_skills> 中。", {
        "type": "object", "required": ["skillName"],
        "properties": {"skillName": {"type": "string", "description": "技能名称，必须匹配 available_skills 中的 name。"}},
    }),
    _f("get_project_memory", "在当前会话的历史消息中按关键词检索（包括已被摘要压缩的早期消息）。用于回忆之前讨论过的细节、决策、任务安排。", {
        "type": "object", "required": ["query"],
        "properties": {
            "query": {"type": "string", "description": "搜索关键词（如任务名、人名、技术名词）。"},
            "limit": {"type": "integer", "description": "最多返回条数（默认 10）。"},
        },
    }),
    _f("generate_project_memory", "将记忆保存为持久化文件。memories 参数直接传入结构化记忆数组；chatHistory 参数可触发自动提取。", {
        "type": "object",
        "properties": {
            "memories": {"type": "array", "description": "要保存的记忆列表。每项包含 name、description、type、content。"},
            "chatHistory": {"type": "array", "description": "聊天历史数组。仅在没有 memories 时用于自动提取。"},
        },
    }),
    _f("read_file_range", "读取工作区内指定文件的特定行范围。仅支持正行号（从1开始）。用于大文件的部分读取。", {
        "type": "object", "required": ["filepath", "startLine", "endLine"],
        "properties": {
            "filepath": {"type": "string", "description": "文件路径，可以是相对工作区根目录的路径或工作区内绝对路径。"},
            "startLine": {"type": "number", "description": "起始行号（从1开始的整数）。"},
            "endLine": {"type": "number", "description": "结束行号（从1开始的整数），必须大于或等于 startLine。"},
        },
    }),
    _f("get_file_outline", "提取文件的结构骨架，返回所有 import、class、function、method 的定义及其行号。适用于大文件的全局结构分析。", {
        "type": "object", "required": ["filepath"],
        "properties": {"filepath": {"type": "string", "description": "要分析的文件路径。可以是相对工作区根目录的路径或工作区内绝对路径。"}},
    }),
]

# ── edit ──
_EDIT: list[dict] = [
    _f("Write", "在工作区内创建或覆盖文件。此工具会修改文件，默认需要用户审批。", {
        "type": "object", "required": ["filepath", "content"],
        "properties": {
            "filepath": {"type": "string", "description": "要写入的文件路径，必须位于当前工作区内。"},
            "content": {"type": "string", "description": "完整文件内容。"},
        },
    }),
    _f("Edit", "在工作区内对文件做精确字符串替换。此工具会修改文件，默认需要用户审批。", {
        "type": "object", "required": ["filepath", "oldString", "newString"],
        "properties": {
            "filepath": {"type": "string", "description": "要编辑的文件路径，必须位于当前工作区内。"},
            "oldString": {"type": "string", "description": "需要被替换的原始字符串，必须和文件内容精确匹配。"},
            "newString": {"type": "string", "description": "替换后的字符串。"},
            "replaceAll": {"type": "boolean", "description": "是否替换所有匹配项。默认 false（oldString 必须唯一匹配，多匹配报错）；true=替换文件内所有匹配。"},
        },
    }),
    _f("Bash", "在工作区根目录运行终端命令。默认需要审批。启动 dev server、watch、后端服务等长驻进程时，将 waitForCompletion 设为 false，让命令进入后台运行并持续收集日志。不要用它直接修改文件，文件编辑应由专门的编辑工具完成。", {
        "type": "object", "required": ["command"],
        "properties": {
            "command": {"type": "string", "description": "要执行的命令。"},
            "cwd": {"type": "string", "description": "执行命令的工作目录，相对工作根的路径（如 'clinic'）。多 git 仓库时必须指定。"},
            "waitForCompletion": {"type": "boolean", "description": "是否等待命令完成。默认 true；启动 dev server、watch、后端服务等长驻进程时必须设为 false，命令会进入后台运行。"},
            "timeout": {"type": "integer", "description": "同步等待超时(秒)，默认 120。长命令(安装/构建/测试)按需调大。"},
        },
    }),
    _f("RevertFile", "撤销工作区内单个文件相对 Git HEAD 的未提交变更。此工具会修改文件，默认需要用户审批。", {
        "type": "object", "required": ["filepath"],
        "properties": {"filepath": {"type": "string", "description": "要撤销变更的文件路径，必须位于当前工作区内。"}},
    }),
    _f("single_find_and_replace", "在文件内执行精确的字符串查找和替换。此工具会修改文件，默认需要用户审批。适用于精确的小范围修改。", {
        "type": "object", "required": ["filepath", "old_string", "new_string"],
        "properties": {
            "filepath": {"type": "string", "description": "要编辑的文件路径，必须位于当前工作区内。"},
            "old_string": {"type": "string", "description": "需要被替换的原始字符串，必须和文件内容精确匹配（包括空格和缩进）。"},
            "new_string": {"type": "string", "description": "替换后的字符串。"},
            "replace_all": {"type": "boolean", "description": "是否替换所有匹配项。默认 false，只允许唯一匹配。用于重命名等跨文件替换场景。"},
        },
    }),
]

# ── task ──
_TASK: list[dict] = [
    _f("TodoWrite", "创建或更新当前会话的任务列表。处理需要跟踪多个步骤的复杂任务时使用。每次调用都会完整替换任务列表。", {
        "type": "object", "required": ["todos"],
        "properties": {
            "todos": {"type": "array", "description": "完整任务列表，按执行顺序排列。",
                      "items": {"type": "object", "required": ["content", "status"],
                                "properties": {
                                    "content": {"type": "string", "description": "任务简短描述。"},
                                    "activeForm": {"type": "string", "description": "进行中描述。status 为 in_progress 时建议提供；未提供时会自动使用 content。"},
                                    "status": {"type": "string", "enum": ["pending", "in_progress", "completed"], "description": "任务状态。"},
                                }}},
        },
    }),
    _f("SubAgent", "启动一个只读探索子代理来调研独立子任务，调用会阻塞直到子代理返回调研结论。subagent_type 参数被忽略（统一按只读探索执行）。", {
        "type": "object", "required": ["prompt"],
        "properties": {
            "subagent_type": {"type": "string", "enum": ["Explore", "Plan", "GeneralPurpose", "Verification"],
                              "description": "子代理类型（当前被忽略，统一按只读探索执行）。"},
            "prompt": {"type": "string", "description": "传递给子代理的任务描述。"},
            "description": {"type": "string", "description": "5 个词以内的任务摘要。"},
        },
    }),
    _f("SubAgentAsync", "异步启动一个子代理任务，立即返回 taskId。适合耗时较长、可并行推进的独立分析任务。任务完成后结果会自动推送回当前会话，不要主动调用 TaskQuery 轮询。", {
        "type": "object", "required": ["prompt"],
        "properties": {
            "subagent_type": {"type": "string", "enum": ["Explore", "Plan", "GeneralPurpose", "Verification"],
                              "description": "子代理类型。"},
            "prompt": {"type": "string", "description": "传递给子代理的任务描述。"},
            "description": {"type": "string", "description": "5 个词以内的任务摘要。"},
        },
    }),
    _f("TaskQuery", "查询异步子代理任务状态和结果。", {
        "type": "object", "required": ["taskId"],
        "properties": {"taskId": {"type": "string", "description": "SubAgentAsync 返回的 taskId。"}},
    }),
    _f("TaskCancel", "取消仍在运行的异步子代理任务。", {
        "type": "object", "required": ["taskId"],
        "properties": {"taskId": {"type": "string", "description": "要取消的 taskId。"}},
    }),
]

# ── webSearch ──
_WEB_SEARCH: list[dict] = [
    _f("WebSearch", "联网搜索获取实时信息，返回标题、链接与摘要。不支持按发布时间过滤。searchEngine 取值映射到实际搜索源：search_std=bing（直连可用）、search_pro=google、search_pro_sogou/search_pro_quark=duckduckgo（后两者可能需要配置 HTTP 代理）。", {
        "type": "object", "required": ["query"],
        "properties": {
            "query": {"type": "string", "description": "要搜索的问题或关键词。建议不超过 70 个字符。"},
            "searchEngine": {"type": "string", "enum": ["search_std", "search_pro", "search_pro_sogou", "search_pro_quark"],
                             "description": "搜索源编码：search_std=bing（默认，直连可用）；search_pro=google；search_pro_sogou/search_pro_quark=duckduckgo（后两者需配置 HTTP 代理）。"},
            "maxResults": {"type": "integer", "minimum": 1, "maximum": 50, "description": "最多返回多少条结果，范围 1-50。"},
        },
    }),
]

# ── attachment（plan-147-674：当前项目补充，非参考项目原生——附件/图片读取）──
_ATTACHMENT: list[dict] = [
    _f("ReadAttachment", "读取用户上传的附件文件内容。path 参数可直接使用用户消息附件中的"
        "服务器磁盘绝对路径（如 'C:/Users/xx/AppData/Local/chatcoder/uploads/1a2b3c/报告.docx'），"
        "也兼容相对路径（如 '1a2b3c/报告.docx'）。\n"
        "支持 docx / pdf / xlsx / csv / txt / md 等文本类（返回解析文本）"
        "以及 png/jpg/jpeg/gif/webp 图片（返回 base64 与元信息，多模态模型可直接理解）。\n"
        "path 取自用户消息中附件的 path 字段，或对话上下文「用户上传的附件」列表中的路径。", {
        "type": "object", "required": ["path"],
        "properties": {
            "path": {"type": "string", "description": "附件路径——优先使用消息中给出的服务器绝对路径（如 'C:/Users/xx/AppData/Local/chatcoder/uploads/1a2b3c/报告.docx'），相对路径（'1a2b3c/报告.docx'）亦可"},
        },
    }),
    _f("ViewImage", "查看图片文件，返回图片元信息（大小、格式、尺寸）与 base64（多模态模型可直接理解）。", {
        "type": "object", "required": ["path"],
        "properties": {
            "path": {"type": "string", "description": "图片文件路径（相对工作区根目录、绝对路径或用户附件路径均可）"},
        },
    }),
]

# ── background（plan-153-705：当前项目补充，非参考项目原生——后台进程管理）──
_BACKGROUND: list[dict] = [
    _f("BashStatus", "查询后台命令的运行状态与增量日志。shellId 来自 Bash 以 waitForCompletion=false "
        "启动命令时的返回。offset 传上次返回的 next_offset 可增量读取新日志；首次查询传 0（或不传）。", {
        "type": "object", "required": ["shellId"],
        "properties": {
            "shellId": {"type": "string", "description": "后台命令标识（bg_ 开头）"},
            "offset": {"type": "integer", "description": "日志起始字符偏移（默认 0，增量读取传上次 next_offset）"},
        },
    }),
    _f("BashKill", "终止一个后台命令（及其全部子进程）。shellId 来自 Bash 以 waitForCompletion=false "
        "启动命令时的返回。", {
        "type": "object", "required": ["shellId"],
        "properties": {
            "shellId": {"type": "string", "description": "后台命令标识（bg_ 开头）"},
        },
    }),
]

TA3_NATIVE_SCHEMAS: dict[str, dict] = {
    s["function"]["name"]: s for s in [
        *_CORE, *_EDIT, *_TASK, *_WEB_SEARCH, *_ATTACHMENT, *_BACKGROUND,
    ]
}


def disguise_tools(tool_schemas: list[dict]) -> list[dict]:
    """当前项目工具 schema → ta3 原生 schema（无映射的工具剔除）。"""
    from app.models.providers.ta3_tool_aliases import TO_TA3

    out: list[dict] = []
    for schema in tool_schemas:
        function = schema.get("function") or {}
        real_name = function.get("name") or ""
        alias = TO_TA3.get(real_name)
        if alias is None:
            continue
        native = TA3_NATIVE_SCHEMAS.get(alias)
        if native is None:
            continue
        out.append(native)
    return out
