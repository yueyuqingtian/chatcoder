"""plan-20261105-file-tools：供应商参数面与真实大文件工具的端到端回归。"""
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.core.ripgrep_env import locate_ripgrep
from app.models.providers.ta3_tool_aliases import disguise_args, restore_args
from app.models.providers.ta3_tool_schemas import TA3_NATIVE_SCHEMAS, disguise_tools
from app.orchestration.tools import grep as grep_mod
from app.orchestration.tools.base import ToolContext
from app.orchestration.tools.fs_read import FsReadTool
from app.orchestration.tools.git_diff import GitDiffTool
from app.orchestration.tools.grep import GrepTool
from app.orchestration.tools.read_cache import ReadWindowCache


@pytest.fixture(autouse=True)
def _reset_shared_test_db():
    yield


def ctx(root, cache=None):
    return ToolContext(workspace_root=str(root), session_id=1, task_id=1,
                       agent_id=1, agent_name="tester", read_cache=cache)


def test_ta3_execution_parameters_match_real_tools_and_history():
    samples = [
        (FsReadTool(), "Read", {"path": "a.vue", "offset": 601, "limit": 200}),
        (GrepTool(), "Search", {"pattern": "needle", "path": "a.vue", "offset": 50,
                               "max_matches": 10, "include": "*.vue",
                               "context_lines": 2, "case_sensitive": True}),
        (GitDiffTool(), "Diff", {"repo": "frontend", "stat_only": True, "tracked_only": False}),
    ]
    for tool, alias, arguments in samples:
        fn = disguise_tools([tool.function_schema()])[0]["function"]
        outgoing = disguise_args(tool.name, arguments)
        assert set(outgoing) <= set(fn["parameters"]["properties"])
        assert restore_args(alias, outgoing) == arguments
    assert "offset" not in TA3_NATIVE_SCHEMAS["Read"]["function"]["parameters"]["properties"]


@pytest.mark.asyncio
async def test_ta3_can_read_after_600_with_visible_first_page(tmp_path):
    (tmp_path / "a.vue").write_text("\n".join(f"row {n}" for n in range(1, 1481)))
    cache = ReadWindowCache()
    messages = []
    cache.bind(lambda: messages)
    tool = FsReadTool()
    first = await tool.run(restore_args("Read", {"filepath": "a.vue"}), ctx(tmp_path, cache))
    messages.append(SimpleNamespace(role="tool", tool_call_id="first", content=first.output))
    cache.record(tuple(first.data["read_key"]), tuple(first.data["fingerprint"]), "first")
    repeated = await tool.run(restore_args("Read", {"filepath": "a.vue"}), ctx(tmp_path, cache))
    assert repeated.data["unchanged"]
    for start, end in [(601, 1200), (1201, 1480)]:
        args = disguise_args("fs_read", {"path": "a.vue", "offset": start})
        page = await tool.run(restore_args("Read", args), ctx(tmp_path, cache))
        assert page.ok and not page.data.get("unchanged")
        assert page.data["shown_lines"] == f"{start}-{end}"
        assert f"row {start}" in page.output and f"row {end}" in page.output


@pytest.fixture
def large_source(tmp_path):
    # 超过旧读取 2MB、搜索 512KB 上限，命中在文件后部。
    path = tmp_path / "large.vue"
    path.write_text(("x" * 160 + "\n") * 17596 + "needle-tail\n", encoding="utf-8")
    assert path.stat().st_size > 2 * 1024 * 1024
    return path


@pytest.mark.asyncio
async def test_read_large_source_tail(large_source):
    result = await FsReadTool().run(
        {"path": large_source.name, "offset": 17596, "limit": 2}, ctx(large_source.parent))
    assert result.ok and result.data["shown_lines"] == "17596-17597"
    assert "needle-tail" in result.output


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["file", "python-directory", "rg-directory"])
async def test_search_large_source_tail(large_source, monkeypatch, mode):
    monkeypatch.setattr(settings, "enhanced_search", True)
    if mode == "rg-directory" and locate_ripgrep() is None:
        pytest.skip("本机没有 ripgrep")
    if mode == "python-directory":
        monkeypatch.setattr(grep_mod, "locate_ripgrep", lambda: None)
    args = {"pattern": "needle-tail", "path": large_source.name if mode == "file" else "."}
    result = await GrepTool().run(args, ctx(large_source.parent))
    assert result.ok and result.data["returned"] == 1
    assert "17597" in result.output and "needle-tail" in result.output
    if mode == "rg-directory":
        assert "(rg)" in result.output


@pytest.mark.asyncio
async def test_large_source_search_pagination_and_context(large_source, monkeypatch):
    monkeypatch.setattr(grep_mod, "locate_ripgrep", lambda: None)
    with large_source.open("a", encoding="utf-8") as stream:
        stream.write("needle-next\nafter\n")
    arguments = {"pattern": "needle", "path": large_source.name, "max_matches": 1,
                 "context_lines": 1, "case_sensitive": True}
    first = await GrepTool().run(arguments, ctx(large_source.parent))
    assert first.data["has_more"]
    second_args = {**arguments, "offset": 1}
    page = await GrepTool().run(restore_args("Search", disguise_args("fs_grep", second_args)),
                              ctx(large_source.parent))
    assert not page.data["has_more"] and page.data["returned"] == 1
    assert ">> 17598: needle-next" in page.output
    assert "17599: after" in page.output
    assert ">> 17597:" not in page.output


def test_other_provider_converters_keep_paging_and_filters():
    from app.models.providers.anthropic import AnthropicProvider
    from app.models.providers.commandcode import CommandCodeProvider
    from app.models.providers.kiro import KiroProvider
    from app.models.providers.workbuddy import WorkBuddyProvider
    from app.models.schemas import ChatMessage, ChatRequest

    tools = [FsReadTool().function_schema(), GrepTool().function_schema()]
    request = ChatRequest(model="test", messages=[ChatMessage(role="user", content="读取")], tools=tools)
    common = dict(api_key="test", base_url="https://example.test", model="test")
    anthropic = AnthropicProvider(**common)._build_payload(request, stream=False)["tools"]
    command = CommandCodeProvider(**common)._convert_tools(tools)
    kiro = KiroProvider._convert_tools(tools)
    buddy = WorkBuddyProvider(**common)._build_body(request)["tools"]
    for index, tool in enumerate(tools):
        expected = tool["function"]["parameters"]
        assert anthropic[index]["input_schema"] == expected
        assert command[index]["input_schema"] == expected
        assert kiro[index]["toolSpecification"]["inputSchema"]["json"] == expected
        assert buddy[index]["function"]["parameters"] == expected


@pytest.mark.asyncio
async def test_openai_request_keeps_tool_parameters(monkeypatch):
    from app.models.providers.openai_compatible import OpenAICompatibleProvider
    from app.models.schemas import ChatMessage, ChatRequest

    tools = [FsReadTool().function_schema(), GrepTool().function_schema()]
    provider = OpenAICompatibleProvider(api_key="test", base_url="https://example.test", model="test")
    captured = {}

    async def empty_stream():
        if False:
            yield None

    async def fake_create(kwargs):
        captured.update(kwargs)
        return empty_stream()

    monkeypatch.setattr(provider, "_create_compat", fake_create)
    try:
        await provider.chat(ChatRequest(model="test", tools=tools,
                                        messages=[ChatMessage(role="user", content="读取")]))
        assert captured["tools"] == tools
    finally:
        await provider._client.close()


def test_ta3_both_protocols_keep_paging_in_schema_and_history():
    import json

    from app.models.providers.ta3 import Ta3Provider
    from app.models.schemas import ChatMessage

    provider = Ta3Provider(api_key="test", base_url="https://example.test", model="test")
    arguments = {"pattern": "needle", "path": "large.vue", "offset": 50,
                 "max_matches": 10, "context_lines": 2, "include": "*.vue"}
    message = ChatMessage(role="assistant", content=None, tool_calls=[
        {"id": "search-2", "name": "fs_grep", "arguments": arguments}])
    history = provider._disguise_message(message)
    outgoing = json.loads(history["tool_calls"][0]["function"]["arguments"])
    restored = provider._restore_tool_calls([
        {"id": "search-2", "name": "Search", "arguments": outgoing}])
    assert restored[0]["arguments"] == arguments
    schemas = disguise_tools([FsReadTool().function_schema(), GrepTool().function_schema()])
    converted = provider._convert_anthropic_tools(schemas)
    for original, anthropic in zip(schemas, converted):
        assert anthropic["input_schema"] == original["function"]["parameters"]
