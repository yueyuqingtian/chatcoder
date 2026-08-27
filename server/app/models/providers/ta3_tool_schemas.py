"""ta3 原生工具 schema 移植（OpenAI function 格式，中文 description 原文保留）。

来源：参考项目 resources/app-extracted/src/tools/toolDefinitions/
- core.ts：Read/List/Search/Diff/ReadSkill/get_project_memory/generate_project_memory/
          read_file_range/get_file_outline
- edit.ts：Write/Edit/Bash/RevertFile/single_find_and_replace
- task.ts：TodoWrite/SubAgent/SubAgentAsync/TaskQuery/TaskCancel
- webSearch.ts：WebSearch

只取发给模型的 {type, function:{name, description, parameters}} 字段。
"""
from __future__ import annotations


def _f(name: str, description: str, parameters: dict) -> dict:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": parameters}}


# ── core ──
_CORE: list[dict] = [
    _f("Read", "读取工作区内指定文件内容。适合查看现有代码和配置文件。", {
        "type": "object", "required": ["filepath"],
        "properties": {"filepath": {"type": "string", "description": "文件路径，可以是相对工作区根目录的路径或工作区内绝对路径。"}},
    }),
    _f("List", "列出工作区内目录下的文件和文件夹。", {
        "type": "object",
        "properties": {
            "dirPath": {"type": "string", "description": "目录路径，默认为工作区根目录。"},
            "recursive": {"type": "boolean", "description": "是否递归列出。大型项目中请谨慎使用。"},
        },
    }),
    _f("Search", "使用 ripgrep 在工作区中搜索正则表达式，自动跳过常见构建和依赖目录。", {
        "type": "object", "required": ["query"],
        "properties": {
            "query": {"type": "string", "description": "要搜索的正则表达式。"},
            "path": {"type": "string", "description": "可选的搜索目录或文件路径。"},
        },
    }),
    _f("Diff", "查看当前工作区相对 Git HEAD 的未提交变更。", {
        "type": "object", "properties": {},
    }),
    _f("ReadSkill", "按技能名称读取完整技能说明。可用技能会出现在系统提示词的 <available_skills> 中。", {
        "type": "object", "required": ["skillName"],
        "properties": {"skillName": {"type": "string", "description": "技能名称，必须匹配 available_skills 中的 name。"}},
    }),
    _f("get_project_memory", "获取已保存的项目记忆。传入 'all' 返回所有记忆完整内容；传入关键词进行搜索匹配。", {
        "type": "object", "required": ["query"],
        "properties": {"query": {"type": "string", "description": "查询关键词，或 'all' 返回所有记忆。"}},
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
            "replaceAll": {"type": "boolean", "description": "是否替换所有匹配项。默认 false，只允许唯一匹配。"},
        },
    }),
    _f("Bash", "在工作区根目录运行终端命令。默认需要审批。启动 dev server、watch、后端服务等长驻进程时，将 waitForCompletion 设为 false，让命令进入后台运行并持续收集日志。不要用它直接修改文件，文件编辑应由专门的编辑工具完成。", {
        "type": "object", "required": ["command"],
        "properties": {
            "command": {"type": "string", "description": "要执行的命令。"},
            "waitForCompletion": {"type": "boolean", "description": "是否等待命令完成。默认 true；启动 dev server、watch、后端服务等长驻进程时必须设为 false，命令会进入后台运行。"},
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
    _f("SubAgent", "启动一个子代理来执行独立任务。适合把复杂任务拆成 2-3 个范围明确的子任务。", {
        "type": "object", "required": ["prompt"],
        "properties": {
            "subagent_type": {"type": "string", "enum": ["Explore", "Plan", "GeneralPurpose", "Verification"],
                              "description": "子代理类型。"},
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
    _f("WebSearch", "使用智谱 Web Search API 搜索互联网，获取最新网页、新闻、产品信息、文档和无法从当前代码库得知的外部事实。", {
        "type": "object", "required": ["query"],
        "properties": {
            "query": {"type": "string", "description": "要搜索的问题或关键词。智谱接口建议不超过 70 个字符。"},
            "searchEngine": {"type": "string", "enum": ["search_std", "search_pro", "search_pro_sogou", "search_pro_quark"],
                             "description": "智谱搜索引擎编码。"},
            "maxResults": {"type": "integer", "minimum": 1, "maximum": 50, "description": "最多返回多少条结果，范围 1-50。"},
            "timeRange": {"type": "string", "enum": ["day", "week", "month", "year", "d", "w", "m", "y", "oneDay", "oneWeek", "oneMonth", "oneYear", "noLimit"],
                          "description": "按发布时间过滤结果，例如 day/week/month/year；不填则不限时间。"},
        },
    }),
]

TA3_NATIVE_SCHEMAS: dict[str, dict] = {s["function"]["name"]: s for s in [*_CORE, *_EDIT, *_TASK, *_WEB_SEARCH]}


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
