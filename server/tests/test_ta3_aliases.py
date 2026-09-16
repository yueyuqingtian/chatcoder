"""ta3 工具名伪装映射单测：出站伪装 + 入站反伪装 + 参数适配往返。"""
import json

from app.models.providers.ta3_tool_aliases import (
    ARGS_FROM_TA3, ARGS_TO_TA3, FROM_TA3, TO_TA3, disguise_args, restore_args,
)
from app.models.providers.ta3_tool_schemas import TA3_NATIVE_SCHEMAS, disguise_tools


def test_direct_mappings():
    assert TO_TA3["fs_read"] == "Read"
    assert TO_TA3["fs_list"] == "List"
    assert TO_TA3["fs_grep"] == "Search"
    assert TO_TA3["git_diff"] == "Diff"
    assert TO_TA3["fs_write"] == "Write"
    assert TO_TA3["terminal_exec"] == "Bash"
    assert TO_TA3["web_search"] == "WebSearch"
    assert TO_TA3["todo_write"] == "TodoWrite"
    assert TO_TA3["memory_search"] == "get_project_memory"
    # plan-147-674: 附件/图片读取工具补映射（此前被伪装层剔除导致多模态图片只能读二进制）
    assert TO_TA3["read_attachment"] == "ReadAttachment"
    assert TO_TA3["view_image"] == "ViewImage"
    # v7: 通用提问工具映射（四种模式均可用；参数 questions 键名一致无需适配）
    assert TO_TA3["ask_user_question"] == "AskUser"
    # plan-238-1191: 多文件批量编辑补映射（此前缺映射被伪装层剔除，ta3 会话里
    # 模型失去批量编辑工具、历史调用被降级为"结果已略"文本导致工作流中断）
    assert TO_TA3["multi_file_edit"] == "MultiFileEdit"
    # plan-248-1258 M3.3: 符号索引工具补映射（此前缺映射被伪装层剔除，系统提示词
    # 却仍引导模型优先用 symbol_search/outline，引导落空、索引能力闲置）
    assert TO_TA3["symbol_search"] == "SymbolSearch"
    assert TO_TA3["outline"] == "get_file_outline"


def test_reverse_mapping_is_consistent():
    for real, alias in TO_TA3.items():
        assert FROM_TA3[alias] == real


def test_disguise_tools_drops_unmapped():
    schemas = [
        {"type": "function", "function": {"name": "fs_read", "parameters": {}}},
        {"type": "function", "function": {"name": "fs_write", "parameters": {}}},
        # v7: 有映射的通用提问工具 → 伪装保留
        {"type": "function", "function": {"name": "ask_user_question", "parameters": {}}},
        # 无映射工具 → 剔除
        {"type": "function", "function": {"name": "web_fetch", "parameters": {}}},
        {"type": "function", "function": {"name": "collect_results", "parameters": {}}},
    ]
    out = disguise_tools(schemas)
    names = [s["function"]["name"] for s in out]
    assert names == ["Read", "Write", "AskUser"]
    # 原生 schema 中文 description 完整还原
    assert out[0]["function"]["description"].startswith("读取工作区内指定文件内容")
    assert out[2]["function"]["description"].startswith("向用户发起结构化提问")


def test_disguise_tools_keeps_mcp_tools():
    """MCP 工具不再被剔除（plan-230-1144 M1.3 Phase 2）——改为 ta3 风格伪装名。

    此前 mcp_* 在本用例里属于"应被剔除"，ta3 会话因此完全看不到 MCP 工具；
    现按 ta3_mcp 改名为 PascalCase 伪装名并中文化描述，明细见 test_ta3_mcp.py。
    """
    schemas = [
        {"type": "function", "function": {"name": "mcp_something", "parameters": {}}},
        {"type": "function", "function": {"name": "mcp_codegraph_codegraph_explore",
                                          "description": "Explore.\n[MCP Server: codegraph, Tool: codegraph_explore]",
                                          "parameters": {"type": "object",
                                                         "properties": {"query": {"type": "string"}}}}},
    ]
    out = disguise_tools(schemas)
    assert [s["function"]["name"] for s in out] == [
        "McpSomething", "McpCodegraphCodegraphExplore",
    ]
    assert "codegraph" in out[1]["function"]["description"]


def test_disguise_tools_keeps_attachment_tools():
    """plan-147-674: read_attachment/view_image 有映射，伪装后保留（不再剔除）。"""
    schemas = [
        {"type": "function", "function": {"name": "read_attachment", "parameters": {}}},
        {"type": "function", "function": {"name": "view_image", "parameters": {}}},
    ]
    out = disguise_tools(schemas)
    names = [s["function"]["name"] for s in out]
    assert names == ["ReadAttachment", "ViewImage"]
    assert out[0]["function"]["description"].startswith("读取用户上传的附件文件内容")


def test_attachment_args_roundtrip():
    """plan-147-674: 附件工具参数名一致（path），伪装往返不变形。"""
    real_args = {"path": "C:/uploads/1a/image.png"}
    assert disguise_args("read_attachment", real_args) == real_args
    assert restore_args("ReadAttachment", real_args) == real_args
    assert disguise_args("view_image", real_args) == real_args
    assert restore_args("ViewImage", real_args) == real_args


def test_native_schemas_cover_aliases():
    """所有伪装目标名都应有原生 schema（除 generate_project_memory 等未映射目标）。"""
    for alias in set(TO_TA3.values()):
        assert alias in TA3_NATIVE_SCHEMAS, f"缺原生 schema: {alias}"
        assert TA3_NATIVE_SCHEMAS[alias]["function"]["name"] == alias


def test_edit_args_roundtrip():
    # 出站：真实 editor_apply_diff 参数 → ta3 Edit 参数（replace_all → replaceAll）
    real_args = {"path": "src/main.py", "old_text": "a", "new_text": "b", "replace_all": True}
    disguised = disguise_args("editor_apply_diff", real_args)
    assert disguised == {"filepath": "src/main.py", "oldString": "a", "newString": "b", "replaceAll": True}
    # 入站：反伪装（replaceAll → replace_all 原样恢复，不再丢弃）
    restored = restore_args("Edit", disguised)
    assert restored == real_args


def test_subagent_args_roundtrip():
    # 出站：真实 spawn_subagent → ta3 SubAgent（explore 丢弃，由入站强制补回）
    real_args = {"task_title": "调研", "task_description": "读 A 文件", "acceptance_criteria": "ok", "explore": False}
    disguised = disguise_args("spawn_subagent", real_args)
    assert disguised == {"description": "调研", "prompt": "读 A 文件"}
    # 入站：ta3 SubAgent → 真实参数（强制 explore=True 同步探索）
    restored = restore_args("SubAgent", {"prompt": "读 A 文件", "description": "调研", "subagent_type": "Explore"})
    assert restored["task_description"] == "读 A 文件"
    assert restored["task_title"] == "调研"
    assert restored["explore"] is True
    assert "subagent_type" not in restored


def test_restore_unknown_alias_keeps_name():
    """模型幻觉出 ta3 有但当前项目没有的工具（如 RevertFile）→ 保持原样由执行层报错。"""
    from app.models.providers.ta3 import Ta3Provider

    provider = Ta3Provider(api_key="llm-x", base_url="https://x", model="m")
    calls = provider._restore_tool_calls([
        {"id": "1", "name": "RevertFile", "arguments": {"filepath": "a.py"}},
        {"id": "2", "name": "Read", "arguments": {"filepath": "b.py"}},
    ])
    assert calls[0]["name"] == "RevertFile"
    assert calls[1]["name"] == "fs_read"
    assert calls[1]["arguments"] == {"path": "b.py"}


def test_message_disguise_tool_calls_json():
    """出站消息：assistant.tool_calls 伪装（名+参数 JSON 序列化）。"""
    from app.models.providers.ta3 import Ta3Provider
    from app.models.schemas import ChatMessage

    provider = Ta3Provider(api_key="llm-x", base_url="https://x", model="m")
    m = ChatMessage(
        role="assistant", content=None,
        tool_calls=[{"id": "c1", "name": "editor_apply_diff",
                     "arguments": {"path": "a.py", "old_text": "x", "new_text": "y"}}],
        reasoning_content="思考中",
    )
    out = provider._disguise_message(m)
    assert out["tool_calls"][0]["function"]["name"] == "Edit"
    args = json.loads(out["tool_calls"][0]["function"]["arguments"])
    assert args == {"filepath": "a.py", "oldString": "x", "newString": "y"}
    # 工具轮保留 reasoning_content
    assert out["reasoning_content"] == "思考中"


def test_message_disguise_plain_turn_strips_reasoning():
    """普通回复轮剥离 reasoning_content（对齐 applyPlainTurnReasoningPolicy）。"""
    from app.models.providers.ta3 import Ta3Provider
    from app.models.schemas import ChatMessage

    provider = Ta3Provider(api_key="llm-x", base_url="https://x", model="m")
    m = ChatMessage(role="assistant", content="好的", reasoning_content="老思考")
    out = provider._disguise_message(m)
    assert "reasoning_content" not in out


def test_common_tools_args_roundtrip():
    """核心工具的 ta3 参数与真实参数双向转换（Read/Search/Write/get_project_memory/List）。"""
    cases = [
        # (真实工具名, 真实参数, ta3 名, ta3 参数)
        ("fs_read", {"path": "src/main.py", "offset": 1, "limit": 200},
         "Read", {"filepath": "src/main.py", "offset": 1, "limit": 200}),
        ("fs_list", {"path": "docs", "recursive": True},
         "List", {"dirPath": "docs", "recursive": True}),
        ("fs_grep", {"pattern": "class User", "path": "src", "case_sensitive": True},
         "Search", {"query": "class User", "path": "src", "case_sensitive": True}),
        ("fs_write", {"path": "notes.md", "content": "hi"},
         "Write", {"filepath": "notes.md", "content": "hi"}),
        ("memory_search", {"keyword": "架构", "limit": 5},
         "get_project_memory", {"query": "架构", "limit": 5}),
    ]
    for real_name, real_args, ta3_name, ta3_args in cases:
        assert disguise_args(real_name, real_args) == ta3_args
        assert restore_args(ta3_name, ta3_args) == real_args


def test_unmapped_history_call_becomes_text():
    """历史中未映射的 tool_call（collect_results）→ 转普通文本，防协议断裂。"""
    from app.models.providers.ta3 import Ta3Provider
    from app.models.schemas import ChatMessage

    provider = Ta3Provider(api_key="llm-x", base_url="https://x", model="m")
    m = ChatMessage(role="assistant", content=None,
                    tool_calls=[{"id": "c1", "name": "collect_results", "arguments": {}}])
    out = provider._disguise_message(m)
    assert "tool_calls" not in out
    assert "不可用" in out["content"]
    # plan-238-1191: 提示语不得再写"结果已略"（结果会以文本形式紧随其后），
    # 且须显式要求模型不要复述本条、继续用可用工具推进
    assert "结果已略" not in out["content"]
    assert "请勿复述" in out["content"]


# ───────────────── plan-238-1191: multi_file_edit → MultiFileEdit 伪装 ─────────────────


def test_multi_file_edit_disguise_roundtrip():
    """multi_file_edit 出站伪装为 MultiFileEdit（edits 键名一致原样透传），
    历史调用不再被降级成文本提示。"""
    from app.models.providers.ta3 import Ta3Provider
    from app.models.schemas import ChatMessage

    args = {"edits": [{"path": "a.py", "old_text": "x", "new_text": "y"}]}
    assert disguise_args("multi_file_edit", args) == args
    assert restore_args("MultiFileEdit", args) == args

    provider = Ta3Provider(api_key="llm-x", base_url="https://x", model="m")
    m = ChatMessage(role="assistant", content=None,
                    tool_calls=[{"id": "c1", "name": "multi_file_edit", "arguments": args}])
    out = provider._disguise_message(m)
    assert out["tool_calls"][0]["function"]["name"] == "MultiFileEdit"
    assert json.loads(out["tool_calls"][0]["function"]["arguments"]) == args
    assert "不可用" not in (out.get("content") or "")


def test_disguise_tools_keeps_multi_file_edit():
    """multi_file_edit 有映射与原生 schema → 伪装后保留（不再被剔除）。"""
    schemas = [
        {"type": "function", "function": {"name": "multi_file_edit", "parameters": {}}},
    ]
    out = disguise_tools(schemas)
    names = [s["function"]["name"] for s in out]
    assert names == ["MultiFileEdit"]
    props = TA3_NATIVE_SCHEMAS["MultiFileEdit"]["function"]["parameters"]["properties"]
    assert "edits" in props


# ───────────────── plan-609: schema 参数可见性 + 参数名对齐 ─────────────────


def test_ta3_schemas_expose_real_params():
    """plan-609: TA3 伪装 schema 暴露真实工具关键参数，不隐藏、不声明无效参数。"""
    read_props = TA3_NATIVE_SCHEMAS["Read"]["function"]["parameters"]["properties"]
    assert {"offset", "limit"} <= set(read_props)  # plan-645: Read 缺 offset/limit 曾致 ta3 分页失效
    diff_props = TA3_NATIVE_SCHEMAS["Diff"]["function"]["parameters"]["properties"]
    assert {"repo", "tracked_only", "stat_only"} <= set(diff_props)
    search_props = TA3_NATIVE_SCHEMAS["Search"]["function"]["parameters"]["properties"]
    assert {"include", "case_sensitive", "context_lines"} <= set(search_props)
    list_props = TA3_NATIVE_SCHEMAS["List"]["function"]["parameters"]["properties"]
    assert "max_depth" in list_props
    ws_props = TA3_NATIVE_SCHEMAS["WebSearch"]["function"]["parameters"]["properties"]
    assert "timeRange" not in ws_props  # 真实工具无时间过滤能力，schema 不得声明
    assert {"query", "searchEngine", "maxResults"} <= set(ws_props)
    gpm_props = TA3_NATIVE_SCHEMAS["get_project_memory"]["function"]["parameters"]["properties"]
    assert "limit" in gpm_props
    # 描述与真实语义对齐：不再承诺「'all' 返回全部记忆」
    assert "全部记忆" not in TA3_NATIVE_SCHEMAS["get_project_memory"]["function"]["description"]
    # SubAgent 描述说明 subagent_type 被忽略、强制只读探索
    assert "只读" in TA3_NATIVE_SCHEMAS["SubAgent"]["function"]["description"]
    # plan-645: Bash 补 cwd（多 git 仓库必须指定，与真实 terminal_exec 对齐）
    bash_props = TA3_NATIVE_SCHEMAS["Bash"]["function"]["parameters"]["properties"]
    assert "cwd" in bash_props


def test_websearch_args_roundtrip():
    """plan-609: WebSearch searchEngine/maxResults ↔ web_search engine/max_results 双向转换。"""
    real_args = {"query": "q", "engine": "bing", "max_results": 8}
    disguised = disguise_args("web_search", real_args)
    assert disguised == {"query": "q", "searchEngine": "bing", "maxResults": 8}
    restored = restore_args("WebSearch", disguised)
    assert restored == real_args


def test_get_project_memory_limit_passthrough():
    """plan-609: get_project_memory 的 limit 透传到 memory_search。"""
    assert restore_args("get_project_memory", {"query": "架构", "limit": 5}) == {
        "keyword": "架构", "limit": 5,
    }


# ───────────────── plan-153-705: 后台进程工具伪装 + Bash timeout ─────────────────


def test_bg_tools_mappings():
    """terminal_bg_status/terminal_bg_kill → BashStatus/BashKill 伪装映射。"""
    assert TO_TA3["terminal_bg_status"] == "BashStatus"
    assert TO_TA3["terminal_bg_kill"] == "BashKill"
    assert FROM_TA3["BashStatus"] == "terminal_bg_status"
    assert FROM_TA3["BashKill"] == "terminal_bg_kill"


def test_bg_tools_args_roundtrip():
    """shell_id ↔ shellId 双向转换；offset 键名一致透传。"""
    # 出站：真实参数 → ta3 参数
    assert disguise_args("terminal_bg_status", {"shell_id": "bg_ab12", "offset": 100}) == {
        "shellId": "bg_ab12", "offset": 100,
    }
    assert disguise_args("terminal_bg_kill", {"shell_id": "bg_ab12"}) == {
        "shellId": "bg_ab12",
    }
    # 入站：ta3 参数 → 真实参数
    assert restore_args("BashStatus", {"shellId": "bg_ab12", "offset": 100}) == {
        "shell_id": "bg_ab12", "offset": 100,
    }
    assert restore_args("BashKill", {"shellId": "bg_ab12"}) == {
        "shell_id": "bg_ab12",
    }


def test_bash_waitforcompletion_passthrough():
    """Bash 的 waitForCompletion/timeout 键名两侧一致，原样透传（无映射条目）。"""
    real_args = {"command": "npm run dev", "waitForCompletion": False, "timeout": 300}
    assert disguise_args("terminal_exec", real_args) == real_args
    assert restore_args("Bash", real_args) == real_args


def test_bg_tools_schemas_exposed():
    """BashStatus/BashKill 有原生 schema 且暴露 shellId；Bash schema 含 timeout。"""
    status_props = TA3_NATIVE_SCHEMAS["BashStatus"]["function"]["parameters"]["properties"]
    assert "shellId" in status_props
    assert "offset" in status_props
    kill_props = TA3_NATIVE_SCHEMAS["BashKill"]["function"]["parameters"]["properties"]
    assert "shellId" in kill_props
    bash_props = TA3_NATIVE_SCHEMAS["Bash"]["function"]["parameters"]["properties"]
    assert {"waitForCompletion", "timeout", "cwd"} <= set(bash_props)


def test_disguise_tools_keeps_bg_tools():
    """伪装后 terminal_bg_status/terminal_bg_kill 保留（不再被剔除）。"""
    schemas = [
        {"type": "function", "function": {"name": "terminal_bg_status", "parameters": {}}},
        {"type": "function", "function": {"name": "terminal_bg_kill", "parameters": {}}},
    ]
    out = disguise_tools(schemas)
    names = [s["function"]["name"] for s in out]
    assert names == ["BashStatus", "BashKill"]


# ───────────────── plan-248-1258 M3.3: 符号索引工具伪装 ─────────────────


def test_symbol_tools_mappings():
    """symbol_search / outline → SymbolSearch / get_file_outline 伪装映射。"""
    assert TO_TA3["symbol_search"] == "SymbolSearch"
    assert TO_TA3["outline"] == "get_file_outline"
    assert FROM_TA3["SymbolSearch"] == "symbol_search"
    assert FROM_TA3["get_file_outline"] == "outline"


def test_symbol_tools_args_roundtrip():
    """symbol_search 参数键名一致透传；outline 的 path ↔ filepath 双向转换。"""
    search_args = {"query": "build_main", "kind": "function", "file_glob": "*.py", "limit": 10}
    assert disguise_args("symbol_search", search_args) == search_args
    assert restore_args("SymbolSearch", search_args) == search_args

    outline_args = {"filepath": "server/app/main.py"}
    assert disguise_args("outline", {"path": "server/app/main.py"}) == outline_args
    assert restore_args("get_file_outline", outline_args) == {"path": "server/app/main.py"}


def test_disguise_tools_keeps_symbol_tools():
    """两个符号索引工具伪装后保留（不再被伪装层剔除）。"""
    schemas = [
        {"type": "function", "function": {"name": "symbol_search", "parameters": {}}},
        {"type": "function", "function": {"name": "outline", "parameters": {}}},
    ]
    out = disguise_tools(schemas)
    assert [s["function"]["name"] for s in out] == ["SymbolSearch", "get_file_outline"]
    props = TA3_NATIVE_SCHEMAS["SymbolSearch"]["function"]["parameters"]["properties"]
    assert {"query", "kind", "file_glob", "limit"} <= set(props)
    outline_props = TA3_NATIVE_SCHEMAS["get_file_outline"]["function"]["parameters"]["properties"]
    assert "filepath" in outline_props
    # plan-609 同口径：描述不得超出真实能力——索引未开启时返开启指引，不自动建库
    assert "自动建索引" not in TA3_NATIVE_SCHEMAS["SymbolSearch"]["function"]["description"]
    # 骨架工具不产出 import，描述不得声明
    assert "import" not in TA3_NATIVE_SCHEMAS["get_file_outline"]["function"]["description"]


def test_symbol_tools_disguise_message_roundtrip():
    """出站伪装 + 入站还原：两个符号索引工具名与参数均正确转换。"""
    from app.models.providers.ta3 import Ta3Provider
    from app.models.schemas import ChatMessage

    provider = Ta3Provider(api_key="llm-x", base_url="https://x", model="m")
    m = ChatMessage(role="assistant", content=None, tool_calls=[
        {"id": "c1", "name": "symbol_search", "arguments": {"query": "SymbolSearchTool", "limit": 5}},
        {"id": "c2", "name": "outline", "arguments": {"path": "server/app/main.py"}},
    ])
    out = provider._disguise_message(m)
    assert out["tool_calls"][0]["function"]["name"] == "SymbolSearch"
    assert json.loads(out["tool_calls"][0]["function"]["arguments"]) == {"query": "SymbolSearchTool", "limit": 5}
    assert out["tool_calls"][1]["function"]["name"] == "get_file_outline"
    assert json.loads(out["tool_calls"][1]["function"]["arguments"]) == {"filepath": "server/app/main.py"}
    assert "不可用" not in (out.get("content") or "")

    calls = provider._restore_tool_calls([
        {"id": "1", "name": "SymbolSearch", "arguments": {"query": "outline", "limit": 5}},
        {"id": "2", "name": "get_file_outline", "arguments": {"filepath": "main.cjs"}},
    ])
    assert calls[0]["name"] == "symbol_search"
    assert calls[0]["arguments"] == {"query": "outline", "limit": 5}
    assert calls[1]["name"] == "outline"
    assert calls[1]["arguments"] == {"path": "main.cjs"}
