"""ta3 内置工具的 schema 快照 + 出站伪装分派（plan-598-2482 重写，plan-601 逐字对齐）。

分派规则（三条路径）：
1. **官方内置工具**（`TO_TA3` 命中）→ 用本文件的快照 `TA3_NATIVE_SCHEMAS` 下发。
   快照逐字取自官方客户端 Ta+3 牛码 0.7.5 的 `plugins/tools/*.js`（description、
   parameters 的 properties / required / enum / minimum / maximum / minItems 全部原样），
   由 tests/test_ta3_tools_parity_601.py 用官方定义的规范化哈希守护。
   参数键按官方形态（filepath / dirPath / oldString 等），由 `ta3_tool_aliases` 双向换算。
2. **本项目独有的拓展工具**（`TO_TA3_EXTENSION` 命中）→ 从真实 schema 派生，挂到拓展
   MCP 服务（见 `ta3_mcp.build_extension_tool`）。
3. **真实 MCP 工具**（registry 里 `mcp_` 开头的）→ 官方 MCP 形态（`ta3_mcp.disguise_schema`）。

输出顺序与序列化对齐官方 `chat/toolOrder.js` + `tools/index.js:225-238`：
内置工具按 function.name 字典序、MCP 工具按 `serverId\\0toolName` 排序且整体排在内置之后；
每个工具的 parameters 递归按键排序（只排 parameters，name/description 不动，数组元素顺序不动）。

plan-20261105-file-tools：官方快照保留为基线；Read/Search/Diff 的实际下发定义
由 execution_schema 补齐真实执行需要的分页、过滤及仓库参数。仅补默认值不能让模型
指定后续范围，工具描述与模型可见参数必须与执行能力一致。
"""
from __future__ import annotations

import copy
import sys


def _f(name: str, description: str, parameters: dict) -> dict:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": parameters}}


def sort_keys_deep(value):
    """对齐官方 `sortObjectKeysDeep`：对象键按码点序递归排序，数组保持元素顺序。"""
    if isinstance(value, list):
        return [sort_keys_deep(v) for v in value]
    if isinstance(value, dict):
        return {k: sort_keys_deep(value[k]) for k in sorted(value)}
    return value


def canonicalize_tool(tool: dict) -> dict:
    """对齐官方 `canonicalizeTool`：只对 function.parameters 做深度键排序。"""
    fn = dict(tool.get("function") or {})
    if fn.get("parameters"):
        fn["parameters"] = sort_keys_deep(fn["parameters"])
    return {**tool, "function": fn}


# ─────────────────────── 官方内置工具原文（15 项） ───────────────────────
# 以下字面量逐字取自官方 plugins/tools/*.js 的 function 定义；改动任何一个字符都会让
# test_ta3_tools_parity_601 的官方哈希断言失败。

# ── Read（plugins/tools/read.js）──
_READ_DESC = (
    "读取工作区内指定文件内容。DOCX 小文档默认完整读取，大文档返回概要；概要只确认版本，不代表全文已读。"
    "docx.mode=changes 由本地工具比较上次基线与最新正文、直接格式、样式定义及表格结构，返回变化摘要，"
    "无需 AI 全文比较；首次/重启无历史基线会明确说明。search 用 query 查找并读取匹配段落，"
    "paragraphs 读取目标段落，table 读取目标表格行，styles 读取样式，xml 读取原始 OOXML 部件。"
    "返回 nextRead 时用其中的 docx.cursor 继续同一范围，直到 requestedComplete=true；"
    "超长单段、表格行和 XML 也会分页。可直接读取目标段落或 XML 并取得版本，不必先单独读取概要。"
    "同一会话已读的完全相同字节副本会复用读取记录；修改前读取目标范围，不要求读完无关文档。"
    "保留用户已有样式。DOCUMENT_VERSION_CHANGED 表示分页/读取后版本已变，重新从概要和目标范围读取。"
)
_READ_PARAMS = {
    "type": "object",
    "required": ["filepath"],
    "properties": {
        "filepath": {
            "type": "string",
            "description": "文件路径，可以是相对工作区根目录的路径或工作区内绝对路径。",
        },
        "document": {
            "type": "object",
            "description": (
                "XLSX/PPTX/PDF 的版本化分页读取。默认 overview；XLSX/PPTX 用 xml 读取目标部件，"
                "PDF 用 pages 读取目标页；概要不代表全文已读。返回 nextRead 时原样续读，直至 requestedComplete=true。"
            ),
            "properties": {
                "mode": {"type": "string", "enum": ["overview", "xml", "part", "pages"]},
                "part": {"type": "string", "description": "包内 XML/.rels 路径，overview 列出可用部件。"},
                "start": {"type": "integer", "minimum": 1, "description": "PDF 页码或包目录起始项，从 1 开始。"},
                "count": {
                    "type": "integer", "minimum": 1, "maximum": 200,
                    "description": "PDF 最多 20 页，包目录最多 200 项。part 模式可读二进制部件的大小和摘要，不表示已查看图像。",
                },
                "documentVersion": {"type": "string"},
                "cursor": {"type": "string"},
            },
        },
        "docx": {
            "type": "object",
            "description": "仅 DOCX 使用；XLSX/PPTX/PDF 用 document 参数。start 从 1 开始；paragraphs 按段落、table 按行、styles 按样式。",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["auto", "overview", "changes", "search", "paragraphs", "table", "styles", "xml"],
                },
                "query": {
                    "type": "string",
                    "description": "search 模式按原文子串查找，返回匹配段落编号及内容；避免为定位某一句话读取整份文档。",
                },
                "part": {
                    "type": "string",
                    "description": (
                        "默认 word/document.xml；xml 模式还支持 [Content_Types].xml、_rels/.rels、"
                        "word/_rels/document.xml.rels 等包内 XML/关系部件，不允许路径跳转。"
                    ),
                },
                "start": {"type": "integer", "minimum": 1},
                "count": {"type": "integer", "minimum": 1, "maximum": 200},
                "tableIndex": {"type": "integer", "minimum": 1, "description": "table 模式的表格编号，来自概要。"},
                "styleIds": {
                    "type": "array", "items": {"type": "string"},
                    "description": "styles 模式指定样式编号；自动包含继承与关联样式。",
                },
                "documentVersion": {"type": "string", "description": "可提供概要版本，防止多次读取混用不同版本。"},
                "cursor": {"type": "string", "description": "原样传入上次 nextRead.docx.cursor；包含版本及分页位置。"},
            },
        },
    },
}

# ── List（plugins/tools/list.js）──
_LIST_DESC = "列出工作区内目录下的文件和文件夹。"
_LIST_PARAMS = {
    "type": "object",
    "properties": {
        "dirPath": {"type": "string", "description": "目录路径，默认为工作区根目录。"},
        "recursive": {"type": "boolean", "description": "是否递归列出。大型项目中请谨慎使用。"},
    },
}

# ── Search（plugins/tools/search.js）──
_SEARCH_DESC = "使用 ripgrep 在工作区中搜索正则表达式，自动跳过常见构建和依赖目录。"
_SEARCH_PARAMS = {
    "type": "object",
    "required": ["query"],
    "properties": {
        "query": {"type": "string", "description": "要搜索的正则表达式。"},
        "path": {"type": "string", "description": "可选的搜索目录或文件路径。"},
    },
}

# ── Diff（plugins/tools/diff.js）──
_DIFF_DESC = "查看当前工作区相对 Git HEAD 的未提交变更。"
_DIFF_PARAMS = {"type": "object", "properties": {}}

# ── ReadSkill（plugins/tools/readSkill.js）──
_READ_SKILL_DESC = "按技能名称读取完整技能说明。可用技能会出现在系统提示词的 <available_skills> 中。"
_READ_SKILL_PARAMS = {
    "type": "object",
    "required": ["skillName"],
    "properties": {
        "skillName": {"type": "string", "description": "技能名称，必须匹配 available_skills 中的 name。"},
    },
}

# ── Write（plugins/tools/write.js）──
_WRITE_DESC = (
    '在工作区内创建或覆盖文件。mode="append" 时改为追加到文件末尾（文件不存在则创建；'
    "追加前仍需先读取一次该文件；行尾会按文件现有风格适配）。此工具会修改文件，默认需要用户审批。"
)
_WRITE_PARAMS = {
    "type": "object",
    "required": ["filepath", "content"],
    "properties": {
        "filepath": {"type": "string", "description": "要写入的文件路径，必须位于当前工作区内。"},
        "content": {"type": "string", "description": '要写入的内容；mode="append" 时为要追加到文件末尾的内容。'},
        "mode": {
            "type": "string",
            "enum": ["overwrite", "append"],
            "description": "可选。默认 overwrite（整体覆盖）；append 追加到文件末尾，适合大文件追加场景，无需整文件读入。",
        },
    },
}

# ── Edit（plugins/tools/edit.js）──
_EDIT_DESC = "在工作区内对文件做精确字符串替换。此工具会修改文件，默认需要用户审批。"
_EDIT_PARAMS = {
    "type": "object",
    "required": ["filepath", "oldString", "newString"],
    "properties": {
        "filepath": {"type": "string", "description": "要编辑的文件路径，必须位于当前工作区内。"},
        "oldString": {"type": "string", "description": "需要被替换的原始字符串，必须和文件内容精确匹配。"},
        "newString": {"type": "string", "description": "替换后的字符串。"},
        "replaceAll": {"type": "boolean", "description": "是否替换所有匹配项。默认 false，只允许唯一匹配。"},
    },
}

# ── get_file_outline（plugins/tools/getFileOutline.js）──
_OUTLINE_DESC = "提取文件的结构骨架，返回所有 import、class、function、method 的定义及其行号。适用于大文件的全局结构分析。"
_OUTLINE_PARAMS = {
    "type": "object",
    "required": ["filepath"],
    "properties": {
        "filepath": {"type": "string", "description": "要分析的文件路径。可以是相对工作区根目录的路径或工作区内绝对路径。"},
    },
}

# ── get_project_memory（plugins/tools/getProjectMemory.js）──
_GET_MEMORY_DESC = "获取已保存的项目记忆。传入 'all' 返回所有记忆完整内容；传入关键词进行搜索匹配。"
_GET_MEMORY_PARAMS = {
    "type": "object",
    "required": ["query"],
    "properties": {
        "query": {"type": "string", "description": "查询关键词，或 'all' 返回所有记忆。"},
    },
}

# ── generate_project_memory（plugins/tools/generateProjectMemory.js）──
_GEN_MEMORY_DESC = "将记忆保存为持久化文件。memories 参数直接传入结构化记忆数组；chatHistory 参数可触发自动提取。"
_GEN_MEMORY_PARAMS = {
    "type": "object",
    "properties": {
        "memories": {
            "type": "array",
            "description": "要保存的记忆列表。每项包含 name、description、type、content。",
            "items": {
                "type": "object",
                "required": ["name", "type", "content"],
                "properties": {
                    "name": {"type": "string", "description": "记忆名称，例如 user_role、feedback_testing。"},
                    "description": {"type": "string", "description": "一行描述，用于未来判断相关性。"},
                    "type": {
                        "type": "string",
                        "enum": ["user", "feedback", "project", "reference"],
                        "description": "user/feedback 存全局，project/reference 存当前项目。",
                    },
                    "content": {"type": "string", "description": "记忆正文。feedback/project 建议包含 Why 和 How to apply。"},
                },
            },
        },
        "chatHistory": {
            "type": "array",
            "description": "聊天历史数组。仅在没有 memories 时用于自动提取。",
            "items": {"type": "object"},
        },
    },
}

# ── WebSearch（plugins/tools/webSearch.js）──
_WEB_SEARCH_DESC = "搜索互联网，获取最新网页、新闻、产品信息、文档和无法从当前代码库得知的外部事实。"
_WEB_SEARCH_PARAMS = {
    "type": "object",
    "required": ["query"],
    "properties": {
        "query": {"type": "string", "description": "要搜索的问题或关键词。智谱搜索建议不超过 70 个字符。"},
        "searchEngine": {
            "type": "string",
            "enum": ["search_std", "search_pro", "search_pro_sogou", "search_pro_quark"],
            "description": "仅智谱搜索生效：搜索引擎编码。search_std 为基础版，search_pro 为高阶版，search_pro_sogou/搜狗，search_pro_quark/夸克。",
        },
        "maxResults": {
            "type": "integer", "minimum": 1, "maximum": 50,
            "description": "仅智谱搜索生效：最多返回多少条结果，范围 1-50。",
        },
        "timeRange": {
            "type": "string",
            "enum": ["day", "week", "month", "year", "d", "w", "m", "y",
                     "oneDay", "oneWeek", "oneMonth", "oneYear", "noLimit"],
            "description": "仅智谱搜索生效：按发布时间过滤结果，例如 day/week/month/year；不填则不限时间。",
        },
        "domain": {"type": "string", "description": "仅智谱搜索生效：只搜索指定白名单域名，例如 www.example.com。"},
        "includeDomains": {
            "type": "array",
            "description": "仅智谱搜索生效：只搜索这些域名。智谱接口只支持一个白名单域名，工具会使用第一个。",
            "items": {"type": "string"},
        },
        "contentSize": {
            "type": "string", "enum": ["medium", "high"],
            "description": "仅智谱搜索生效：返回网页摘要长度。medium 适合常规问答，high 会返回更多细节。",
        },
        "searchIntent": {
            "type": "boolean",
            "description": "仅智谱搜索生效：是否启用搜索意图识别。默认关闭，开启后会先判断搜索意图再执行检索。",
        },
        "requestId": {"type": "string", "description": "仅智谱搜索生效：可选请求唯一标识，长度 6-64。"},
        "userId": {"type": "string", "description": "仅智谱搜索生效：可选终端用户唯一标识，长度 6-128。"},
    },
}

# ── Bash（plugins/tools/bash.js:14-29,46-63）──
# 基础描述与参数是平台无关的官方原文；运行环境提示由 `bash_description` 按平台追加
# （对齐 shellResolver.appendShellEnvironmentNote：`{描述}\n\n{提示}`）。
_BASH_BASE_DESC = (
    "在工作区根目录运行终端命令。默认需要审批。"
    "删除、移动等不可恢复操作前，必须单独调用一次只读命令列出目标，等结果返回并确认后再执行；"
    "不要在同一条命令里把列出和删除串起来——列出结果不会回到你的上下文，等于没有核对。"
    "启动 dev server、watch、后端服务等长驻进程时，将 waitForCompletion 设为 false。"
    "Word 表格样式用 EditWordDocument formatTable；DOCX/XLSX/PPTX/PDF 脚本修改优先用 EditDocument runScript"
    " 一次完成副本、执行、校验和保存；交互修改可 prepare/commit。不要覆盖原文档或重跑旧生成脚本。"
    "文件编辑应由专门的编辑工具完成。"
)
_BASH_PARAMS = {
    "type": "object",
    "required": ["purpose", "command"],
    "properties": {
        "purpose": {
            "type": "string",
            "description": (
                '用一句中文说明这条命令做什么、会改动哪些文件或目录，例如"删除 dist 下的全部构建产物"。'
                "必须与实际命令一致：声称的范围小于命令实际影响范围（例如说只删日志、命令却删除整个目录），"
                "或笼统到看不出会改动什么，审核都会被拒绝。"
            ),
        },
        "command": {"type": "string", "description": "要执行的命令。删除、移动类命令先用只读命令核对目标范围。"},
        "waitForCompletion": {
            "type": "boolean",
            "description": "是否等待命令完成。默认 true；启动 dev server、watch、后端服务等长驻进程时必须设为 false，命令会进入后台运行。",
        },
    },
}

# 官方 shellResolver.js:102-106 的三段原文
SHELL_ENVIRONMENT_NOTES: dict[str, str] = {
    "git-bash": (
        "当前 Windows 环境通过 bash（Git Bash 或同类）运行命令，支持 bash 语法：&&、管道、单引号、$VAR、"
        "grep/head/sed 等。不要使用 cmd 专有语法（如 cd /d、%VAR%、type、dir /b）。"
        '调用带 / 开头参数的 Windows 原生工具时，可加 MSYS_NO_PATHCONV=1 前缀，或用 cmd //c "..." 包一层。'
        '删除前单独用 ls -d <pattern> 或 find <目录> -name "<pattern>" 列出目标，不要和删除写在同一条命令里。'
    ),
    "cmd": (
        "当前 Windows 环境通过 cmd.exe 运行命令（不是 bash）：多命令串联用 &&；环境变量用 %VAR%；"
        "空设备用 NUL（不是 /dev/null）；不要使用单引号（会被当作普通字符、命令可能静默失效），代码参数请改用双引号；"
        "ls/cat/grep/head/sleep 等 UNIX 命令不可用（改用 dir/type/findstr/timeout）。"
        "如需 bash 语法可安装 Git Bash，安装后重启应用自动启用。"
        "删除前单独用 dir /b /s <pattern> 列出目标，不要和删除写在同一条命令里。"
    ),
    "posix": (
        "当前环境通过 POSIX shell（zsh/sh）运行命令，支持 bash 语法与 UNIX 工具。"
        '删除前单独用 ls -d <pattern> 或 find <目录> -name "<pattern>" 列出目标，不要和删除写在同一条命令里。'
    ),
}

# plan-601：官方没有 PowerShell 分支（Windows 上只区分 Git Bash / cmd），而本项目的终端
# 实际跑在 PowerShell 上。照搬官方探测会让描述写「bash 语法」或「cmd 方言」，与真实执行
# 环境不符、命令必然出错，因此按官方句式补两段 PowerShell 文案——这是已知的非原文差异点。
POWERSHELL_ENVIRONMENT_NOTES: dict[str, str] = {
    "pwsh": (
        "当前 Windows 环境通过 PowerShell 7（pwsh）运行命令（不是 bash，也不是 cmd.exe）：多命令串联用 ; 或 &&；"
        "环境变量用 $env:VAR；空设备用 $null（不是 /dev/null）；单引号字符串不展开变量，需要插值时用双引号；"
        "grep/sed/awk/head 等 UNIX 命令不可用（改用 Select-String/Get-Content/Get-ChildItem），ls -la 这类短横线参数不生效。"
        "删除前单独用 Get-ChildItem <pattern> 列出目标，不要和删除写在同一条命令里。"
    ),
    "powershell": (
        "当前 Windows 环境通过 Windows PowerShell 运行命令（不是 bash，也不是 cmd.exe）：多命令串联用 ;（不支持 &&）；"
        "环境变量用 $env:VAR；空设备用 $null（不是 /dev/null）；单引号字符串不展开变量，需要插值时用双引号；"
        "grep/sed/awk/head 等 UNIX 命令不可用（改用 Select-String/Get-Content/Get-ChildItem），ls -la 这类短横线参数不生效。"
        "删除前单独用 Get-ChildItem <pattern> 列出目标，不要和删除写在同一条命令里。"
    ),
}


def _current_shell_kind() -> str:
    """真实终端的 shell 类别（与 terminal_exec 同一事实源）；非 Windows 恒为 posix。"""
    if sys.platform != "win32":
        return "posix"
    try:
        from app.orchestration.tools.shell_env import shell_kind

        kind = shell_kind()
    except Exception:  # pragma: no cover - 探测失败时按 Windows 默认（PowerShell 7）
        return "pwsh"
    return "posix" if kind == "unix" else kind


def shell_environment_note(kind: str) -> str:
    return SHELL_ENVIRONMENT_NOTES.get(kind) or POWERSHELL_ENVIRONMENT_NOTES.get(kind) or ""


def bash_description(kind: str | None = None) -> str:
    note = shell_environment_note(kind or _current_shell_kind())
    return f"{_BASH_BASE_DESC}\n\n{note}" if note else _BASH_BASE_DESC


def strip_shell_environment_notes(text: str) -> str:
    """对齐官方 `stripShellEnvironmentNotes`：剥离任意一段运行环境提示（快照比对用）。"""
    out = str(text or "")
    for note in (*SHELL_ENVIRONMENT_NOTES.values(), *POWERSHELL_ENVIRONMENT_NOTES.values()):
        out = out.replace(f"\n\n{note}", "")
    return out


# ── TodoWrite（plugins/tools/todoWrite.js）──
_TODO_DESC = "创建或更新当前会话的任务列表。处理需要跟踪多个步骤的复杂任务时使用。每次调用都会完整替换任务列表。"
_TODO_PARAMS = {
    "type": "object",
    "required": ["todos"],
    "properties": {
        "todos": {
            "type": "array",
            "description": "完整任务列表，按执行顺序排列。",
            "items": {
                "type": "object",
                "required": ["content", "status"],
                "properties": {
                    "content": {"type": "string", "description": "任务简短描述。"},
                    "activeForm": {
                        "type": "string",
                        "description": "进行中描述。status 为 in_progress 时建议提供；未提供时会自动使用 content。",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed"],
                        "description": "任务状态。",
                    },
                },
            },
        },
    },
}

# ── ask_user（plugins/tools/askUser.js:158-199）──
_ASK_USER_DESC = (
    "向用户发起一次性问卷以获取确认或决策。适用于：需要用户在多个方案间选择、需要补充业务细节、"
    "执行有风险操作前需确认等场景。可一次问多个问题，每个问题可给选项（用户也可自由输入），用户可跳过任意问题。"
    "工具会阻塞等待用户作答，问卷以卡片形式出现在当前会话里；提交后返回每个问题的回答"
    "（选项或自定义文本；被跳过的问题标记为跳过）。不要用文本输出来询问用户，直接调用本工具。"
)
_ASK_USER_PARAMS = {
    "type": "object",
    "required": ["questions"],
    "properties": {
        "title": {"type": "string", "description": "问卷标题，简述这次提问的目的，例如「实现方案确认」"},
        "questions": {
            "type": "array",
            "minItems": 1,
            "description": "问题列表，按重要性排序",
            "items": {
                "type": "object",
                "required": ["question"],
                "properties": {
                    "id": {"type": "string", "description": "问题唯一标识，缺省为 q1/q2…"},
                    "question": {"type": "string", "description": "问题正文，用一句清晰的话提问"},
                    "description": {
                        "type": "string",
                        "description": (
                            "给用户看的背景说明（可选）：为什么要问、各选项差别或影响。"
                            "问题需要上下文才能答准时填写，写得越具体用户越答得准"
                        ),
                    },
                    "options": {
                        "type": "array",
                        "description": "候选选项（推荐提供 2-4 个）",
                        "items": {
                            "type": "object",
                            "required": ["label"],
                            "properties": {
                                "label": {"type": "string", "description": "选项展示文本"},
                                "value": {"type": "string", "description": "选项值，缺省等于 label"},
                                "description": {
                                    "type": "string",
                                    "description": (
                                        "给用户看的选项说明（可选）：选它会产生什么结果、与其它选项的差别；"
                                        "尽量每个选项都给或都不给，只有个别选项带说明会显得突兀"
                                    ),
                                },
                            },
                        },
                    },
                    "allowCustom": {"type": "boolean", "description": "是否允许自定义输入，默认 true"},
                    "multiple": {
                        "type": "boolean",
                        "description": "是否允许多选（默认单选）。选项之间互相独立、可以同时成立时设为 true",
                    },
                },
            },
        },
    },
}

# ── SubAgent（plugins/tools/subAgent.js）──
# 描述里的 send_message / interrupt_agent 是官方原文，有意保持一致（不替换为本项目工具名）。
_SUB_AGENT_DESC = (
    "把任务委派给独立会话里的子代理：立即返回子代理ID，本轮不阻塞，每一轮完成后结果自动推送回当前对话。\n"
    "适合拆出范围明确的独立工作（研究、限定范围的实现、分析），这样它不占用本对话的上下文；"
    "你拿到的是它的结果，不是中间步骤。\n"
    "默认（inherit_context=false）它只看得到 prompt 里的内容，"
    "需要它知道的前提、结论、约束都要写进 prompt。\n"
    "inherit_context=true 时它继承本对话已完成的轮次（截至最近一次完整回复，不含正在进行的这一轮），"
    '适合"接着这个结论继续分析""复审刚才的改动"这类离不开上下文的活；'
    "继承的是创建时的快照，之后本对话再推进它也看不到。\n"
    "subagent_type 只是角色提示词，不改变子代理的工具面与权限。\n"
    "需要追加指令用 send_message，打断它当前这一轮用 interrupt_agent；子代理本身会保留。"
)
_SUB_AGENT_PARAMS = {
    "type": "object",
    "required": ["prompt", "description"],
    "properties": {
        "inherit_context": {
            "type": "boolean",
            "description": "是否让子代理继承本对话已完成的轮次。默认 false（不继承，只看 prompt）。",
        },
        "subagent_type": {
            "type": "string",
            "enum": ["Explore", "Plan", "GeneralPurpose", "Verification"],
            "description": (
                "角色提示词：Explore（只读研究）、Plan（只读出计划）、Verification（审查挑错）、"
                "GeneralPurpose（默认，独立分析给结论）。不改变工具面与权限。"
            ),
        },
        "prompt": {"type": "string", "description": "传递给子代理的任务描述。"},
        "description": {"type": "string", "description": "5 个词以内的任务摘要，必填：子会话面板用它当标题。"},
    },
}

# 官方原文（未规范化、Bash 不含运行环境提示），供测试与官方定义逐字比对
TA3_OFFICIAL_DEFINITIONS: dict[str, dict] = {
    "Read": {"name": "Read", "description": _READ_DESC, "parameters": _READ_PARAMS},
    "List": {"name": "List", "description": _LIST_DESC, "parameters": _LIST_PARAMS},
    "Search": {"name": "Search", "description": _SEARCH_DESC, "parameters": _SEARCH_PARAMS},
    "Diff": {"name": "Diff", "description": _DIFF_DESC, "parameters": _DIFF_PARAMS},
    "ReadSkill": {"name": "ReadSkill", "description": _READ_SKILL_DESC, "parameters": _READ_SKILL_PARAMS},
    "Write": {"name": "Write", "description": _WRITE_DESC, "parameters": _WRITE_PARAMS},
    "Edit": {"name": "Edit", "description": _EDIT_DESC, "parameters": _EDIT_PARAMS},
    "get_file_outline": {"name": "get_file_outline", "description": _OUTLINE_DESC, "parameters": _OUTLINE_PARAMS},
    "get_project_memory": {"name": "get_project_memory", "description": _GET_MEMORY_DESC, "parameters": _GET_MEMORY_PARAMS},
    "generate_project_memory": {
        "name": "generate_project_memory", "description": _GEN_MEMORY_DESC, "parameters": _GEN_MEMORY_PARAMS,
    },
    "WebSearch": {"name": "WebSearch", "description": _WEB_SEARCH_DESC, "parameters": _WEB_SEARCH_PARAMS},
    "Bash": {"name": "Bash", "description": _BASH_BASE_DESC, "parameters": _BASH_PARAMS},
    "TodoWrite": {"name": "TodoWrite", "description": _TODO_DESC, "parameters": _TODO_PARAMS},
    "ask_user": {"name": "ask_user", "description": _ASK_USER_DESC, "parameters": _ASK_USER_PARAMS},
    "SubAgent": {"name": "SubAgent", "description": _SUB_AGENT_DESC, "parameters": _SUB_AGENT_PARAMS},
}


def _build_native(name: str, shell_kind: str | None = None) -> dict:
    d = TA3_OFFICIAL_DEFINITIONS[name]
    desc = bash_description(shell_kind) if name == "Bash" else d["description"]
    return canonicalize_tool(_f(d["name"], desc, d["parameters"]))


# 上行快照（已规范化）。同一快照对象跨请求复用：字节稳定，利于网关前缀缓存。
TA3_NATIVE_SCHEMAS: dict[str, dict] = {name: _build_native(name) for name in TA3_OFFICIAL_DEFINITIONS}
_BASH_BY_KIND: dict[str, dict] = {}


def native_schema(alias: str) -> dict | None:
    """内置快照；Bash 按当前 shell 类别取（用户切换终端后无需重启也能跟上）。"""
    if alias != "Bash":
        return TA3_NATIVE_SCHEMAS.get(alias)
    kind = _current_shell_kind()
    cached = _BASH_BY_KIND.get(kind)
    if cached is None:
        cached = _build_native("Bash", kind)
        _BASH_BY_KIND[kind] = cached
    return cached


def disguise_tools(tool_schemas: list[dict]) -> list[dict]:
    """当前项目工具 schema → ta3 上行 schema（未映射的工具剔除），按官方顺序输出。"""
    from app.models.providers.ta3_mcp import (
        EXTENSION_SERVER_ID, build_extension_tool, disguise_schema, is_mcp_tool, mcp_parts,
    )
    from app.models.providers.ta3_tool_aliases import TO_TA3, TO_TA3_EXTENSION

    builtins: list[dict] = []
    mcp: list[tuple[str, str, dict]] = []
    seen: set[str] = set()
    for schema in tool_schemas:
        function = schema.get("function") or {}
        real_name = function.get("name") or ""
        alias = TO_TA3.get(real_name)
        if alias is not None:
            native = execution_schema(alias)
            if alias == "SubAgent" and "mode" not in (function.get("parameters", {}).get("properties") or {}):
                # plan-20261105-subagent-tools：只读派发入口不能被供应商适配重新扩权。
                native = copy.deepcopy(native)
                fn = native["function"]
                fn["parameters"]["properties"].pop("mode", None)
                fn["parameters"]["properties"].pop("purpose", None)
                fn["parameters"]["properties"]["subagent_type"]["enum"] = ["Explore", "Plan"]
                fn["parameters"]["properties"]["subagent_type"]["description"] = "只读研究角色，运行时强制只读。"
                fn["description"] = "派发只读探索子代理，只能研究与查看，不能写入或操作界面；运行时强制只读。"
            if native is not None and alias not in seen:
                seen.add(alias)
                builtins.append(native)
            continue
        # 本项目独有工具 → 拓展 MCP 通道（形态与官方 MCP 工具一致）
        if real_name in TO_TA3_EXTENSION:
            mcp.append((EXTENSION_SERVER_ID, real_name, canonicalize_tool(build_extension_tool(schema))))
            continue
        # 真实 MCP 工具 → 官方 MCP 形态；排序键用 serverId + MCP 原始工具名（同官方 _mcpToolName）
        if is_mcp_tool(real_name):
            _alias, sid, tool, _server = mcp_parts(real_name, str(function.get("description") or ""))
            mcp.append((sid, tool, canonicalize_tool(disguise_schema(schema))))
            continue
        # 其余无映射：剔除（模型不可见，提示词也不会引导）
    builtins.sort(key=lambda s: s["function"]["name"])
    mcp.sort(key=lambda t: f"{t[0]}\u0000{t[1]}")
    return [*builtins, *(t[2] for t in mcp)]


def execution_schema(alias: str) -> dict | None:
    """plan-20261105-file-tools：分页和仓库定位必须在模型可见参数面中可用。"""
    native = native_schema(alias)
    if alias == "SubAgent" and native is not None:
        # plan-20261105-subagent-tools：保留官方快照，执行定义补足操控派发与明确的读写边界。
        from app.orchestration.subagent_tools import SPAWN_SUBAGENT_SCHEMA

        out = copy.deepcopy(native)
        fn = out["function"]
        props = fn["parameters"]["properties"]
        real_props = SPAWN_SUBAGENT_SCHEMA["function"]["parameters"]["properties"]
        props["purpose"] = copy.deepcopy(real_props["purpose"])
        props["mode"] = copy.deepcopy(real_props["mode"])
        fn["description"] = (
            "派发独立子代理，立即返回，完成结果自动推送。mode=readonly 仅只读调研，"
            "mode=agent 可写文件、执行命令，并可使用已启用的内置浏览器、电脑操控与拓展工具。"
            "Explore/Plan 在未指定 mode 时表示只读，其余类型默认可写。"
            "界面操作可设 purpose=operator 使用操控子代理档位；审批继承主会话。"
            "子代理不能再次派发子代理、直接问用户或使用工作流。"
        )
        props["subagent_type"]["description"] = "角色提示：Explore/Plan 默认只读，GeneralPurpose/Verification 默认可写；mode 优先。"
        return canonicalize_tool(out)
    if alias not in {"Read", "Search", "Diff"} or native is None:
        return native
    from app.models.providers.ta3_tool_aliases import ARGS_TO_TA3
    from app.orchestration.tools.fs_read import FsReadTool
    from app.orchestration.tools.git_diff import GitDiffTool
    from app.orchestration.tools.grep import GrepTool

    tool = {"Read": FsReadTool, "Search": GrepTool, "Diff": GitDiffTool}[alias]()
    real = tool.function_schema()["function"]
    out = copy.deepcopy(native)
    fn = out["function"]
    names = ARGS_TO_TA3.get(tool.name, {})
    for key, value in real["parameters"]["properties"].items():
        fn["parameters"]["properties"][names.get(key, key)] = copy.deepcopy(value)
    if alias == "Read":
        fn["description"] = (
            "读取文件文本，默认前 600 行，按输出页脚的 offset 续读，limit 控制窗口行数。"
            "同一路径的不同 offset 是独立范围；重复读取相同范围可能复用上文结果。"
        )
    elif alias == "Search":
        fn["description"] = real["description"] + "结果超过上限时用 offset 翻页，保留 query 和其它过滤参数。"
    else:
        fn["description"] += "多仓库工作区用 repo 指定仓库；stat_only=true 仅看统计。"
    return canonicalize_tool(out)
