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


def test_native_schemas_cover_aliases():
    """所有伪装目标名都应有原生 schema（除 generate_project_memory 等未映射目标）。"""
    for alias in set(TO_TA3.values()):
        assert alias in TA3_NATIVE_SCHEMAS, f"缺原生 schema: {alias}"
        assert TA3_NATIVE_SCHEMAS[alias]["function"]["name"] == alias


def test_edit_args_roundtrip():
    # 出站：真实 editor_apply_diff 参数 → ta3 Edit 参数
    real_args = {"path": "src/main.py", "old_text": "a", "new_text": "b"}
    disguised = disguise_args("editor_apply_diff", real_args)
    assert disguised == {"filepath": "src/main.py", "oldString": "a", "newString": "b"}
    # 入站：反伪装（replaceAll 丢弃）
    restored = restore_args("Edit", {**disguised, "replaceAll": False})
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
