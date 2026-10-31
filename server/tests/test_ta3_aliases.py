"""ta3 工具名伪装映射单测：出站伪装 + 入站反伪装 + 参数适配往返。

plan-598-2482 结构：伪装层拆成两条通道——
- 官方内置通道（`TO_TA3` → 官方 `function.name`，走 `TA3_NATIVE_SCHEMAS` 快照）；
- 拓展 MCP 通道（`TO_TA3_EXTENSION` → `mcp__workspace-tools__{真实名}`）。
本文件守两条通道的名字、参数与消息级转换；表结构层面的白名单校验见
`test_ta3_official_parity.py`。
"""
import json

from app.models.providers.ta3_mcp import EXTENSION_SERVER_ID, EXTENSION_SERVER_NAME, mcp_alias, ta3_mcp_name
from app.models.providers.ta3_tool_aliases import (
    ARGS_FROM_TA3, ARGS_TO_TA3, FROM_TA3, TO_TA3, TO_TA3_EXTENSION,
    disguise_args, restore_args,
)
from app.models.providers.ta3_tool_schemas import TA3_NATIVE_SCHEMAS, disguise_tools


def _ext(real_name: str) -> str:
    """拓展通道的出站名（与源码同一构造器，避免在测试里手写前缀）。"""
    return ta3_mcp_name(EXTENSION_SERVER_ID, real_name)


def test_direct_mappings():
    """官方内置通道：真实名 → 官方 function.name（plan-598-2482 逐字对齐）。"""
    assert TO_TA3["fs_read"] == "Read"
    assert TO_TA3["fs_list"] == "List"
    assert TO_TA3["fs_grep"] == "Search"
    assert TO_TA3["git_diff"] == "Diff"
    assert TO_TA3["fs_write"] == "Write"
    assert TO_TA3["terminal_exec"] == "Bash"
    assert TO_TA3["web_search"] == "WebSearch"
    assert TO_TA3["todo_write"] == "TodoWrite"
    assert TO_TA3["memory_search"] == "get_project_memory"
    assert TO_TA3["memory_write"] == "generate_project_memory"
    # v7: 通用提问工具映射（四种模式均可用；参数 questions 键名一致无需适配）
    assert TO_TA3["ask_user_question"] == "ask_user"
    # plan-248-1258 M3.3: 符号骨架工具映射官方 get_file_outline（snake_case 官方名）
    assert TO_TA3["outline"] == "get_file_outline"
    # 技能读取映射官方 ReadSkill（参数 name→skillName，见 ARGS 表）
    assert TO_TA3["skill_view"] == "ReadSkill"


def test_extension_mappings_use_official_mcp_shape():
    """拓展通道：本项目独有工具一律挂到 `mcp__workspace-tools__{真实名}`。

    plan-598-2482：官方普通会话只下发 27 项内置工具 + MCP 外部工具，因此浏览器/桌面/
    多文件编辑等工具改走 MCP 通道，不再自造 PascalCase 伪装名。
    """
    cases = (
        # plan-147-674: 附件/图片读取（此前被伪装层剔除，多模态图片只能读二进制）
        "read_attachment", "view_image",
        # plan-238-1191: 多文件批量编辑
        "multi_file_edit",
        # plan-248-1258 M3.3: 符号索引
        "symbol_search", "symbol_index_enable",
        # plan-548-2276: 功能地图
        "feature_lookup",
        # v43: 网络/命令/压缩/浏览器套件
        "web_fetch", "ci_run", "git", "codebase_search",
        "compaction_index", "compaction_view",
        "browser_navigate", "browser_screenshot", "browser_click", "browser_type",
        "browser_snapshot", "browser_evaluate",
        # plan-153-705: 后台进程管理
        "terminal_bg_status", "terminal_bg_kill",
        # v43: 子代理管理
        "collect_results", "cancel_subagent", "subagent_inspect",
        "send_to_subagent", "report_to_leader",
        # 工作流（plan-598-2482 一并改走 MCP 通道）
        "plan_workflow", "start_workflow", "workflow_status",
    )
    for real in cases:
        assert real not in TO_TA3, f"{real} 不应再走内置通道（官方无对应内置工具）"
        assert TO_TA3_EXTENSION[real] == _ext(real)
        assert TO_TA3_EXTENSION[real].startswith(f"mcp__{EXTENSION_SERVER_ID}__")
        assert FROM_TA3[TO_TA3_EXTENSION[real]] == real


def test_reverse_mapping_is_consistent():
    """两条通道的名字都能原路反查回真实执行名。"""
    for real, alias in TO_TA3.items():
        assert FROM_TA3[alias] == real
    for real, alias in TO_TA3_EXTENSION.items():
        assert FROM_TA3[alias] == real
    # 两张表的真实名互斥：同名工具不能同时出现在内置与拓展通道
    assert not (set(TO_TA3) & set(TO_TA3_EXTENSION))


def test_inbound_only_aliases_survive():
    """入站专用别名（官方有、本项目不出站）保留在 FROM_TA3 里。"""
    # 旧回放名：历史会话里可能残留，必须能还原执行
    assert FROM_TA3["SubAgentAsync"] == "spawn_subagent"
    assert FROM_TA3["TaskQuery"] == "collect_results"
    assert FROM_TA3["TaskCancel"] == "cancel_subagent"
    # 官方单文件替换语义等价于 editor_apply_diff；出站仍用 Edit，故不占 TO_TA3
    assert FROM_TA3["single_find_and_replace"] == "editor_apply_diff"
    for inbound_only in ("SubAgentAsync", "TaskQuery", "TaskCancel", "single_find_and_replace"):
        assert inbound_only not in TO_TA3.values(), f"{inbound_only} 不应作为出站名出现"


def test_disguise_tools_drops_unmapped():
    """三路分派：内置走快照 / 拓展走 MCP 通道 / 无映射剔除。"""
    schemas = [
        {"type": "function", "function": {"name": "fs_read", "parameters": {}}},
        {"type": "function", "function": {"name": "fs_write", "parameters": {}}},
        # v7: 有映射的通用提问工具 → 伪装保留（走官方快照）
        {"type": "function", "function": {"name": "ask_user_question",
                                          "description": "向用户发起结构化提问（用于需求澄清）。",
                                          "parameters": {}}},
        # 拓展通道：web_fetch / collect_results 不在官方内置清单里
        {"type": "function", "function": {"name": "web_fetch", "description": "抓取 URL。", "parameters": {}}},
        {"type": "function", "function": {"name": "collect_results", "parameters": {}}},
        # 真正无映射的工具（非 MCP 前缀）→ 仍剔除
        {"type": "function", "function": {"name": "mystery_tool", "parameters": {}}},
    ]
    out = disguise_tools(schemas)
    names = [s["function"]["name"] for s in out]
    # plan-601：内置按名字字典序、MCP 按 serverId+toolName 排在其后（官方 toolOrder.js）
    assert names == ["Read", "Write", "ask_user", _ext("collect_results"), _ext("web_fetch")]
    # 内置通道走官方快照：描述为快照原文，不沿用真实 schema
    assert out[0]["function"]["description"].startswith("读取工作区内指定文件内容")
    # 拓展通道按官方 MCP 模板挂服务显示名，描述沿用真实工具原文
    assert out[4]["function"]["description"].startswith(f"[MCP: {EXTENSION_SERVER_NAME}] 抓取 URL。")


def test_disguise_tools_keeps_mcp_tools():
    """真实 MCP 工具 → 官方形态 `mcp__{server}__{tool}`（plan-598-2482）。"""
    schemas = [
        {"type": "function", "function": {"name": "mcp_something", "parameters": {}}},
        {"type": "function", "function": {"name": "mcp_codegraph_codegraph_explore",
                                          "description": "Explore.\n[MCP Server: codegraph, Tool: codegraph_explore]",
                                          "parameters": {"type": "object",
                                                         "properties": {"query": {"type": "string"}}}}},
    ]
    out = disguise_tools(schemas)
    names = [s["function"]["name"] for s in out]
    assert set(names) == {mcp_alias("mcp_something"), mcp_alias("mcp_codegraph_codegraph_explore")}
    explore = next(s for s in out if s["function"]["name"] == mcp_alias("mcp_codegraph_codegraph_explore"))
    assert explore["function"]["description"].startswith("[MCP: codegraph] ")


def test_disguise_tools_keeps_attachment_tools():
    """plan-147-674: read_attachment/view_image 保留（改走拓展 MCP 通道）。

    plan-598-2482: 不再是自造伪装名，而是 `mcp__workspace-tools__{真实名}`，
    描述按官方 MCP 模板加服务前缀，参数原样沿用真实 schema。
    """
    schemas = [
        {"type": "function", "function": {
            "name": "read_attachment", "description": "读取用户上传的附件文件内容。",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}},
        {"type": "function", "function": {
            "name": "view_image", "description": "查看图片文件。",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}},
    ]
    out = disguise_tools(schemas)
    names = [s["function"]["name"] for s in out]
    assert names == [_ext("read_attachment"), _ext("view_image")]
    assert out[0]["function"]["description"].startswith(
        f"[MCP: {EXTENSION_SERVER_NAME}] 读取用户上传的附件文件内容")
    assert "path" in out[0]["function"]["parameters"]["properties"]


def test_attachment_args_roundtrip():
    """plan-147-674: 附件工具参数名一致（path），伪装往返不变形。"""
    real_args = {"path": "C:/uploads/1a/image.png"}
    assert disguise_args("read_attachment", real_args) == real_args
    assert restore_args(_ext("read_attachment"), real_args) == real_args
    assert disguise_args("view_image", real_args) == real_args
    assert restore_args(_ext("view_image"), real_args) == real_args


def test_native_schemas_cover_aliases():
    """每个内置通道的伪装名都必须有原生快照，且快照名与键一致。"""
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


def test_single_find_and_replace_restores_to_editor_apply_diff():
    """官方单文件替换工具（入站专用）→ editor_apply_diff 参数形态。"""
    restored = restore_args("single_find_and_replace", {
        "filepath": "a.py", "old_string": "x", "new_string": "y", "replace_all": True,
    })
    assert restored == {"path": "a.py", "old_text": "x", "new_text": "y", "replace_all": True}


def test_subagent_aliases_and_readonly_semantics():
    """plan-598-2482: 出站子代理只有官方内置名 `SubAgent`。

    `SubAgentAsync` 降级为入站专用（历史会话回放），两者入站还原都只看
    subagent_type 置 explore，不补任何调度参数。
    """
    assert TO_TA3["spawn_subagent"] == "SubAgent"
    assert FROM_TA3["SubAgent"] == "spawn_subagent"
    # 入站专用回放名：必须仍能还原执行
    assert FROM_TA3["SubAgentAsync"] == "spawn_subagent"
    assert "SubAgentAsync" not in TO_TA3.values()

    a = restore_args("SubAgentAsync", {"description": "T", "prompt": "P"})
    assert "background" not in a
    assert a["task_title"] == "T" and a["task_description"] == "P"

    # 只读语义透传：两个名字都只看 subagent_type，且都不带调度参数
    explore = restore_args("SubAgent", {"description": "T", "prompt": "P", "subagent_type": "Explore"})
    assert explore.get("explore") is True and "background" not in explore
    async_explore = restore_args(
        "SubAgentAsync", {"description": "T", "prompt": "P", "subagent_type": "Explore"}
    )
    assert async_explore.get("explore") is True and "background" not in async_explore
    general = restore_args(
        "SubAgent", {"description": "T", "prompt": "P", "subagent_type": "GeneralPurpose"}
    )
    assert "explore" not in general and "background" not in general

    # 查询/取消的键名适配（taskId → agent_id）
    assert restore_args("TaskQuery", {"taskId": 7})["agent_id"] == 7
    assert restore_args("TaskCancel", {"taskId": 8})["agent_id"] == 8


def test_disguise_dispatch_has_single_subagent_entry():
    """spawn_subagent 在 ta3 会话里只伪装成一个官方 `SubAgent`，模型拿不到第二入口。"""
    real = [{
        "type": "function",
        "function": {
            "name": "spawn_subagent",
            "description": "dispatch",
            "parameters": {"type": "object", "properties": {"task_title": {"type": "string"}}},
        },
    }]
    names = [s["function"]["name"] for s in disguise_tools(real)]
    assert names == ["SubAgent"]


def test_subagent_args_roundtrip():
    # 出站：真实 spawn_subagent → ta3 参数（explore 由 subagent_type 表达，验收标准并入 prompt）
    real_args = {"task_title": "调研", "task_description": "读 A 文件", "acceptance_criteria": "ok", "explore": False}
    disguised = disguise_args("spawn_subagent", real_args)
    assert disguised == {"description": "调研", "prompt": "读 A 文件\n\n验收标准：ok"}
    readonly = disguise_args("spawn_subagent", {"task_title": "t", "task_description": "p", "mode": "readonly"})
    assert readonly == {"description": "t", "prompt": "p", "subagent_type": "Explore"}
    # 入站：ta3 SubAgent → 真实参数（subagent_type=Explore 还原只读，不补调度参数）
    restored = restore_args("SubAgent", {"prompt": "读 A 文件", "description": "调研", "subagent_type": "Explore"})
    assert restored["task_description"] == "读 A 文件"
    assert restored["task_title"] == "调研"
    assert restored["explore"] is True
    assert "background" not in restored
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
    """核心工具的 ta3 参数与真实参数双向转换（Read/Search/Write/get_project_memory/List）。

    plan-601：出站只保留官方 schema 声明的键（Read 的 offset/limit 是分页续读依据，例外保留）。
    """
    cases = [
        # (真实工具名, 真实参数, ta3 名, ta3 参数)
        ("fs_read", {"path": "src/main.py", "offset": 1, "limit": 200},
         "Read", {"filepath": "src/main.py", "offset": 1, "limit": 200}),
        ("fs_list", {"path": "docs", "recursive": True},
         "List", {"dirPath": "docs", "recursive": True}),
        ("fs_grep", {"pattern": "class User", "path": "src"},
         "Search", {"query": "class User", "path": "src"}),
        ("fs_write", {"path": "notes.md", "content": "hi"},
         "Write", {"filepath": "notes.md", "content": "hi"}),
        ("memory_search", {"keyword": "架构"},
         "get_project_memory", {"query": "架构"}),
    ]
    for real_name, real_args, ta3_name, ta3_args in cases:
        assert disguise_args(real_name, real_args) == ta3_args
        assert restore_args(ta3_name, ta3_args) == real_args
    # 官方 schema 外的真实参数在历史回放时被收敛掉（与当轮 schema 一致）
    assert disguise_args("fs_grep", {"pattern": "x", "case_sensitive": True}) == {"query": "x"}


def test_unmapped_history_call_becomes_text():
    """历史中未映射的 tool_call → 转普通文本，防协议断裂。

    v43: 原用例用 collect_results 作样本，该工具已补映射（走拓展通道）；
    改用真正无映射的名字，保持对该降级分支的覆盖。
    """
    from app.models.providers.ta3 import Ta3Provider
    from app.models.schemas import ChatMessage

    provider = Ta3Provider(api_key="llm-x", base_url="https://x", model="m")
    m = ChatMessage(role="assistant", content=None,
                    tool_calls=[{"id": "c1", "name": "unknown_tool", "arguments": {}}])
    out = provider._disguise_message(m)
    assert "tool_calls" not in out
    assert "不可用" in out["content"]
    # plan-238-1191: 提示语不得再写"结果已略"（结果会以文本形式紧随其后），
    # 且须显式要求模型不要复述本条、继续用可用工具推进
    assert "结果已略" not in out["content"]
    assert "请勿复述" in out["content"]


# ───────────────── plan-238-1191: multi_file_edit 伪装（plan-598-2482 走 MCP 通道） ─────────────────


def test_multi_file_edit_disguise_roundtrip():
    """multi_file_edit 出站为 `mcp__workspace-tools__multi_file_edit`（edits 原样透传），
    历史调用不再被降级成文本提示。"""
    from app.models.providers.ta3 import Ta3Provider
    from app.models.schemas import ChatMessage

    args = {"edits": [{"path": "a.py", "old_text": "x", "new_text": "y"}]}
    alias = _ext("multi_file_edit")
    assert disguise_args("multi_file_edit", args) == args
    assert restore_args(alias, args) == args

    provider = Ta3Provider(api_key="llm-x", base_url="https://x", model="m")
    m = ChatMessage(role="assistant", content=None,
                    tool_calls=[{"id": "c1", "name": "multi_file_edit", "arguments": args}])
    out = provider._disguise_message(m)
    assert out["tool_calls"][0]["function"]["name"] == alias
    assert json.loads(out["tool_calls"][0]["function"]["arguments"]) == args
    assert "不可用" not in (out.get("content") or "")


def test_disguise_tools_keeps_multi_file_edit():
    """multi_file_edit 走拓展通道：参数从真实 schema 派生，描述挂官方 MCP 模板。"""
    schemas = [
        {"type": "function", "function": {
            "name": "multi_file_edit", "description": "多文件结构化编辑。",
            "parameters": {"type": "object", "properties": {"edits": {"type": "array"}}}}},
    ]
    out = disguise_tools(schemas)
    names = [s["function"]["name"] for s in out]
    assert names == [_ext("multi_file_edit")]
    props = out[0]["function"]["parameters"]["properties"]
    assert "edits" in props
    assert out[0]["function"]["description"] == f"[MCP: {EXTENSION_SERVER_NAME}] 多文件结构化编辑。"


# ───────────────── plan-609: schema 参数可见性 + 参数名对齐 ─────────────────


def test_ta3_schemas_match_official_param_surface():
    """plan-601：内置快照的参数面以官方为准（替代 plan-609/645 「暴露真实参数」的口径）。

    官方没有的能力（Read 分页、Diff 统计、Search 过滤、Bash cwd/timeout）不在 schema 里声明，
    由入站还原层补默认值；逐字一致性见 test_ta3_tools_parity_601。
    """
    def props(name):
        return set(TA3_NATIVE_SCHEMAS[name]["function"]["parameters"]["properties"])

    assert props("Read") == {"filepath", "document", "docx"}
    assert props("Diff") == set()
    assert props("Search") == {"query", "path"}
    assert props("List") == {"dirPath", "recursive"}
    assert {"query", "searchEngine", "maxResults", "timeRange"} <= props("WebSearch")
    assert props("get_project_memory") == {"query"}
    assert "'all'" in TA3_NATIVE_SCHEMAS["get_project_memory"]["function"]["description"]
    # plan-598-2482：SubAgent schema 已与官方客户端逐字对齐（描述 + 4 个参数 + required）。
    # 用 ASCII 片段断言，避免源码编码差异影响匹配；参数集与 required 是关键契约。
    _sub_fn = TA3_NATIVE_SCHEMAS["SubAgent"]["function"]
    assert "subagent_type" in _sub_fn["description"]
    assert "inherit_context" in _sub_fn["description"]
    assert _sub_fn["parameters"]["required"] == ["prompt", "description"]
    assert {"inherit_context", "subagent_type", "prompt", "description"} == set(
        _sub_fn["parameters"]["properties"]
    )
    # plan-601：Bash 以官方为准（purpose 必填，无 cwd/timeout）
    bash_fn = TA3_NATIVE_SCHEMAS["Bash"]["function"]
    assert set(bash_fn["parameters"]["properties"]) == {"purpose", "command", "waitForCompletion"}
    assert bash_fn["parameters"]["required"] == ["purpose", "command"]


def test_websearch_args_roundtrip():
    """plan-609: WebSearch searchEngine/maxResults ↔ web_search engine/max_results 双向转换。"""
    # plan-601：官方 searchEngine 是智谱档位 enum，双向按档位换算
    real_args = {"query": "q", "engine": "bing", "max_results": 8}
    disguised = disguise_args("web_search", real_args)
    assert disguised == {"query": "q", "searchEngine": "search_std", "maxResults": 8}
    assert restore_args("WebSearch", disguised) == {"query": "q", "engine": "workbuddy", "max_results": 8}
    assert restore_args("WebSearch", {"query": "q", "searchEngine": "search_pro_sogou"})["engine"] == "bing"


def test_get_project_memory_limit_passthrough():
    """plan-609: get_project_memory 的 limit 若被传入仍透传到 memory_search（容错）。"""
    assert restore_args("get_project_memory", {"query": "架构", "limit": 5}) == {
        "keyword": "架构", "limit": 5,
    }


# ───────────────── plan-153-705: 后台进程工具（plan-598-2482 走拓展通道） ─────────────────


def test_bg_tools_mappings():
    """terminal_bg_status/terminal_bg_kill 走拓展 MCP 通道（官方无内置对应）。"""
    for real in ("terminal_bg_status", "terminal_bg_kill"):
        alias = _ext(real)
        assert TO_TA3_EXTENSION[real] == alias
        assert FROM_TA3[alias] == real
        # 旧的自造伪装名必须消失
        assert real not in TO_TA3


def test_bg_tools_args_passthrough():
    """shell_id/offset 键名两侧一致，原样透传（官方 MCP 工具不做键名改写）。"""
    status_args = {"shell_id": "bg_ab12", "offset": 100}
    assert disguise_args("terminal_bg_status", status_args) == status_args
    assert restore_args(_ext("terminal_bg_status"), status_args) == status_args
    kill_args = {"shell_id": "bg_ab12"}
    assert disguise_args("terminal_bg_kill", kill_args) == kill_args
    assert restore_args(_ext("terminal_bg_kill"), kill_args) == kill_args


def test_bash_waitforcompletion_passthrough():
    """Bash：waitForCompletion 双向透传；入站丢 purpose，出站补 purpose 并收敛到官方参数面。"""
    restored = restore_args("Bash", {"purpose": "启动开发服务", "command": "npm run dev",
                                      "waitForCompletion": False})
    assert restored == {"command": "npm run dev", "waitForCompletion": False}
    real_args = {"command": "npm run dev", "waitForCompletion": False, "timeout": 300}
    disguised = disguise_args("terminal_exec", real_args)
    assert disguised["command"] == "npm run dev" and disguised["waitForCompletion"] is False
    assert disguised["purpose"]
    assert "timeout" not in disguised


def test_bg_tools_schemas_exposed():
    """拓展通道参数从真实 schema 派生：shell_id 与 offset 必须可见。"""
    from app.orchestration.tools.registry import tool_registry

    real = {s["function"]["name"]: s for s in tool_registry.all_schemas()}
    out = {s["function"]["name"]: s for s in disguise_tools(
        [real["terminal_bg_status"], real["terminal_bg_kill"]])}
    status_props = out[_ext("terminal_bg_status")]["function"]["parameters"]["properties"]
    assert "shell_id" in status_props and "offset" in status_props
    kill_props = out[_ext("terminal_bg_kill")]["function"]["parameters"]["properties"]
    assert "shell_id" in kill_props
    bash_props = TA3_NATIVE_SCHEMAS["Bash"]["function"]["parameters"]["properties"]
    assert "waitForCompletion" in bash_props


def test_disguise_tools_keeps_bg_tools():
    """伪装后 terminal_bg_status/terminal_bg_kill 保留（不再被剔除）。"""
    schemas = [
        {"type": "function", "function": {"name": "terminal_bg_status",
                                          "description": "读后台日志。", "parameters": {}}},
        {"type": "function", "function": {"name": "terminal_bg_kill",
                                          "description": "终止后台任务。", "parameters": {}}},
    ]
    out = disguise_tools(schemas)
    names = [s["function"]["name"] for s in out]
    assert names == [_ext("terminal_bg_kill"), _ext("terminal_bg_status")]


# ───────────────── plan-248-1258 M3.3: 符号索引工具伪装 ─────────────────


def test_symbol_tools_mappings():
    """symbol_search 走拓展通道；outline 走内置通道（官方 get_file_outline）。"""
    assert TO_TA3_EXTENSION["symbol_search"] == _ext("symbol_search")
    assert TO_TA3["outline"] == "get_file_outline"
    assert FROM_TA3[_ext("symbol_search")] == "symbol_search"
    assert FROM_TA3["get_file_outline"] == "outline"


def test_symbol_tools_args_roundtrip():
    """symbol_search 参数键名一致透传；outline 的 path ↔ filepath 双向转换。"""
    search_args = {"query": "build_main", "kind": "function", "file_glob": "*.py", "limit": 10}
    assert disguise_args("symbol_search", search_args) == search_args
    assert restore_args(_ext("symbol_search"), search_args) == search_args

    outline_args = {"filepath": "server/app/main.py"}
    assert disguise_args("outline", {"path": "server/app/main.py"}) == outline_args
    assert restore_args("get_file_outline", outline_args) == {"path": "server/app/main.py"}


def test_disguise_tools_keeps_symbol_tools():
    """两个符号索引工具伪装后保留（不再被伪装层剔除）。

    plan-598-2482: symbol_search 走拓展通道，参数/描述从真实 schema 派生；
    outline 走官方快照（参数键为官方 filepath）。
    """
    from app.orchestration.tools.registry import tool_registry

    real = {s["function"]["name"]: s for s in tool_registry.all_schemas()}
    out = disguise_tools([real["symbol_search"], real["outline"]])
    # 内置工具排在 MCP 工具之前（官方 tools/index.js:238）
    assert [s["function"]["name"] for s in out] == ["get_file_outline", _ext("symbol_search")]

    # 拓展通道：参数原样沿用真实 schema
    sym_props = out[1]["function"]["parameters"]["properties"]
    assert {"query", "kind", "file_glob", "limit"} <= set(sym_props)
    # plan-609 同口径：描述不得超出真实能力——索引未开启时返开启指引，不自动建库
    assert "自动建索引" not in out[1]["function"]["description"]

    outline_props = TA3_NATIVE_SCHEMAS["get_file_outline"]["function"]["parameters"]["properties"]
    assert "filepath" in outline_props
    # 描述必须说明骨架含哪些定义类别（真实 OutlineTool 输出类/函数/方法 + 行号）。
    # 注意：plan-598-2482 的官方快照描述还写有 "import"，但真实 OutlineTool
    # （server/app/orchestration/tools/symbol_search.py:188-275）只输出符号骨架、不产出
    # import 语句——该偏差属于源码侧待收敛项，此处不固化错误描述，只约束真实能力。
    outline_desc = TA3_NATIVE_SCHEMAS["get_file_outline"]["function"]["description"]
    for kind in ("class", "function", "method", "行号"):
        assert kind in outline_desc, f"get_file_outline 描述缺能力说明: {kind}"


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
    assert out["tool_calls"][0]["function"]["name"] == _ext("symbol_search")
    assert json.loads(out["tool_calls"][0]["function"]["arguments"]) == {"query": "SymbolSearchTool", "limit": 5}
    assert out["tool_calls"][1]["function"]["name"] == "get_file_outline"
    assert json.loads(out["tool_calls"][1]["function"]["arguments"]) == {"filepath": "server/app/main.py"}
    assert "不可用" not in (out.get("content") or "")

    calls = provider._restore_tool_calls([
        {"id": "1", "name": _ext("symbol_search"), "arguments": {"query": "outline", "limit": 5}},
        {"id": "2", "name": "get_file_outline", "arguments": {"filepath": "main.cjs"}},
    ])
    assert calls[0]["name"] == "symbol_search"
    assert calls[0]["arguments"] == {"query": "outline", "limit": 5}
    assert calls[1]["name"] == "outline"
    assert calls[1]["arguments"] == {"path": "main.cjs"}


# ───────────────── v43: ta3 全量工具覆盖 ─────────────────


def test_all_registry_tools_are_mapped():
    """全量守护：registry 内全部工具 + 子代理工具都必须落在某条伪装通道上。

    此前 web_fetch/ci_run/git/codebase_search/compaction_*/skill_view/memory_write/
    browser_* 等缺映射，被伪装层整体剔除——ta3 会话下模型完全看不到、也调不动。

    plan-598-2482: 通道有两个（内置 `TO_TA3` / 拓展 `TO_TA3_EXTENSION`），
    真实 MCP 工具（`mcp_` 前缀）由第三个分支派生；「必须有一个」的门槛不变——
    两者皆漏 = 工具静默消失。
    """
    from app.orchestration.subagent_tools import SUBAGENT_TOOL_NAMES
    from app.orchestration.tools.registry import tool_registry

    names = {t.name for t in tool_registry.all()} | SUBAGENT_TOOL_NAMES
    for n in sorted(names):
        if n.startswith("mcp_"):
            continue  # 真实 MCP 工具由 mcp_alias 分支派生
        assert (n in TO_TA3) or (n in TO_TA3_EXTENSION), f"缺 ta3 映射: {n}"
    # 覆盖不是单向的：伪装结果里不得出现被剔除的工具
    disguised = {s["function"]["name"] for s in disguise_tools(tool_registry.all_schemas())}
    for o in (_ext("browser_navigate"), _ext("multi_file_edit")):
        assert o in disguised, f"伪装结果缺 {o}"


def test_subagent_management_tools_mapped():
    """v43: 子代理管理/上报工具走拓展通道；参数键名两侧一致原样透传。"""
    assert TO_TA3_EXTENSION["collect_results"] == _ext("collect_results")
    assert TO_TA3_EXTENSION["cancel_subagent"] == _ext("cancel_subagent")
    assert TO_TA3_EXTENSION["subagent_inspect"] == _ext("subagent_inspect")
    assert TO_TA3_EXTENSION["send_to_subagent"] == _ext("send_to_subagent")
    assert TO_TA3_EXTENSION["report_to_leader"] == _ext("report_to_leader")
    # 出站：agent_id 原样透传（MCP 通道不做键名改写）
    assert disguise_args("collect_results", {"agent_id": 3, "wait": True}) == {
        "agent_id": 3, "wait": True}
    assert disguise_args("cancel_subagent", {"agent_id": 4}) == {"agent_id": 4}
    # 入站：官方回放名仍按 taskId → agent_id 适配
    assert restore_args("TaskQuery", {"taskId": 3}) == {"agent_id": 3}
    # 检视/指令参数名一致，往返不变形
    ins = {"agent_id": 1, "section": "result"}
    assert disguise_args("subagent_inspect", ins) == ins
    assert restore_args(_ext("subagent_inspect"), ins) == ins
    send = {"agent_id": 1, "message": "x"}
    assert disguise_args("send_to_subagent", send) == send
    assert restore_args(_ext("send_to_subagent"), send) == send


def test_feature_map_tools_mapped():
    """plan-548-2276：功能地图与符号索引开关必须有伪装通道。

    这两个工具缺映射时会被 `disguise_tools` 静默剔除：主提示词仍在引导
    「只知道功能时就先用 feature_lookup」，但 ta3 会话里工具根本不在列表里，
    模型只能退回全库 grep——提示词白写、能力白做，且没有任何报错。
    plan-598-2482: 两者都改走拓展 MCP 通道，参数键名与真实工具一致。
    """
    from app.orchestration.tools.registry import tool_registry

    assert TO_TA3_EXTENSION["feature_lookup"] == _ext("feature_lookup")
    assert TO_TA3_EXTENSION["symbol_index_enable"] == _ext("symbol_index_enable")
    # 入站可还原（否则模型调用后落到「未知工具」）
    assert FROM_TA3[_ext("feature_lookup")] == "feature_lookup"
    assert FROM_TA3[_ext("symbol_index_enable")] == "symbol_index_enable"
    # 出站不被剔除，且参数原样保留
    real = [tool_registry.get(n).function_schema()
            for n in ("feature_lookup", "symbol_index_enable")]
    out = {s["function"]["name"]: s for s in disguise_tools(real)}
    assert set(out) == {_ext("feature_lookup"), _ext("symbol_index_enable")}
    assert "query" in out[_ext("feature_lookup")]["function"]["parameters"]["properties"]


def test_toolkit_and_browser_tools_mapped():
    """v43: 新增工具（fetch/ci/git/检索/压缩/技能/浏览器）映射与参数透传。

    plan-598-2482: 除 skill_view（官方内置 ReadSkill）外全部走拓展 MCP 通道。
    """
    assert TO_TA3["skill_view"] == "ReadSkill"
    cases = (
        "web_fetch", "ci_run", "git", "codebase_search",
        "compaction_index", "compaction_view",
        "browser_navigate", "browser_screenshot", "browser_click", "browser_type",
        "browser_snapshot", "browser_evaluate",
    )
    for real in cases:
        assert TO_TA3_EXTENSION[real] == _ext(real)
        assert FROM_TA3[_ext(real)] == real
        # 参数键名两侧一致 → 原样透传（无 ARGS 适配）
        args = {"probe": 1}
        assert disguise_args(real, args) == args
        assert restore_args(_ext(real), args) == args
    # Git 参数多但键名一致
    git_args = {"command": "status", "cwd": "sub"}
    assert disguise_args("git", git_args) == git_args
    assert restore_args(_ext("git"), git_args) == git_args


def test_skill_view_args_roundtrip():
    """plan-598-2482: skill_view(name) ↔ 官方 ReadSkill(skillName)。"""
    assert disguise_args("skill_view", {"name": "brandkit"}) == {"skillName": "brandkit"}
    assert restore_args("ReadSkill", {"skillName": "brandkit"}) == {"name": "brandkit"}


def test_history_call_of_newly_mapped_tool_not_degraded():
    """v43: 补齐映射后，历史调用不再被降级成「不可用」文本。"""
    import json as _json

    from app.models.providers.ta3 import Ta3Provider
    from app.models.schemas import ChatMessage

    provider = Ta3Provider(api_key="llm-x", base_url="https://x", model="m")
    m = ChatMessage(role="assistant", content=None, tool_calls=[
        {"id": "c1", "name": "collect_results", "arguments": {"agent_id": 7}},
        {"id": "c2", "name": "skill_view", "arguments": {"name": "brandkit"}},
        {"id": "c3", "name": "browser_snapshot", "arguments": {"max_depth": 3}},
    ])
    out = provider._disguise_message(m)
    assert "不可用" not in (out.get("content") or "")
    assert [tc["function"]["name"] for tc in out["tool_calls"]] == [
        _ext("collect_results"), "ReadSkill", _ext("browser_snapshot"),
    ]
    assert _json.loads(out["tool_calls"][0]["function"]["arguments"]) == {"agent_id": 7}
    assert _json.loads(out["tool_calls"][1]["function"]["arguments"]) == {"skillName": "brandkit"}
