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


def test_reverse_mapping_is_consistent():
    for real, alias in TO_TA3.items():
        assert FROM_TA3[alias] == real


def test_disguise_tools_drops_unmapped():
    schemas = [
        {"type": "function", "function": {"name": "fs_read", "parameters": {}}},
        {"type": "function", "function": {"name": "fs_write", "parameters": {}}},
        # 无映射工具 → 剔除
        {"type": "function", "function": {"name": "web_fetch", "parameters": {}}},
        {"type": "function", "function": {"name": "collect_results", "parameters": {}}},
        {"type": "function", "function": {"name": "ask_user_question", "parameters": {}}},
        {"type": "function", "function": {"name": "mcp_something", "parameters": {}}},
    ]
    out = disguise_tools(schemas)
    names = [s["function"]["name"] for s in out]
    assert names == ["Read", "Write"]
    # 原生 schema 中文 description 完整还原
    assert out[0]["function"]["description"].startswith("读取工作区内指定文件内容")


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
