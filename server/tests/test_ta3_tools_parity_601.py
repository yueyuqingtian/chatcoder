"""ta3 工具面与官方客户端逐字对齐 + 入站参数还原（plan-601）。

守护四件事：
1. 15 个官方内置工具的 {name, description, parameters} 与官方 Ta+3 牛码 0.7.5
   `plugins/tools/*.js` 逐字一致——用官方定义（node 导出、键深度排序后紧凑 JSON）的 sha256 固定，
   Bash 比对剥离运行环境提示后的描述（同官方 check-tool-surface 的 stripShellEnvironmentNotes）；
   若本机存在官方解包目录，还会直接用 node 导出做一次实时比对；
2. 出站顺序与序列化：内置按名字字典序、MCP 按 serverId\\0toolName、parameters 深度键排序；
3. MCP 命名：serverId 形态稳定、可逆、与拓展通道不撞名；
4. 入站还原：模型按官方 schema 传参时，真实工具能拿到可执行的参数；出站历史可换算回官方形态。
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.models.providers.ta3_mcp import (
    EXTENSION_SERVER_ID,
    EXTENSION_SERVER_NAME,
    build_extension_tool,
    clean_extension_text,
    describe,
    make_server_id,
    mcp_alias,
    resolve_mcp_alias,
    safe_tool_name,
    server_id_for,
)
from app.models.providers.ta3_tool_aliases import (
    TO_TA3,
    TO_TA3_EXTENSION,
    disguise_args,
    restore_args,
)
from app.models.providers.ta3_tool_schemas import (
    POWERSHELL_ENVIRONMENT_NOTES,
    SHELL_ENVIRONMENT_NOTES,
    TA3_NATIVE_SCHEMAS,
    TA3_OFFICIAL_DEFINITIONS,
    bash_description,
    disguise_tools,
    sort_keys_deep,
    strip_shell_environment_notes,
)

# 官方定义的规范化哈希：sha256(json.dumps(sort_keys_deep({name,description,parameters}),
# ensure_ascii=False, separators=(",", ":")))。来源：node 直接加载官方 plugins/tools/*.js 导出。
_OFFICIAL_SHA256 = {
    "Bash": "5b81127cd5c1ea5c90d1ef1a175033fe4f9374b9bb9c8337d4841b3aae937ac4",
    "Diff": "4d948c0e648a993373d4643d8bcb3fbb3d1557855938006e4bf4e38f4b8f7b3b",
    "Edit": "8e76ea986a9f20e5dadba81eadf210ddcf5461694018542377266e23323170a5",
    "List": "7268b011e93af379e624e99da7430c2a65abd89d296936c42b93a6e7f7d65935",
    "Read": "c356ed9e9e9603b108ef740f7b2972712d8a818a9811704e89594b93fd34aa40",
    "ReadSkill": "7f74798567ba3cc4e8b83a585c1e20807c2ca02547ff5544283b48833acb892a",
    "Search": "d3dfd07a8b20a7ecbd05eb233079d43c49dfab59582868c0dcab3dbd513a8d1f",
    "SubAgent": "f1f80c870ef46b8d2758b74990e196518f9a2a9da4c2be952b00fa8a2021a58d",
    "TodoWrite": "1d2e239da952efdf8214080c7fe9e14e8ab4a996c32d1acf49f8dbb66694c2b1",
    "WebSearch": "38cb2b80c03f236859bc29d4197b3e1881932cef168753c7e4625b80046b7445",
    "Write": "ece239a78fe8ee7d359e575219067a61473c714ad2d72277c0409079eb58706f",
    "ask_user": "b231aab13b2fb9a0b18cbce0231fd19133f6cd0a00ef62ee0ec877a35ee5b038",
    "generate_project_memory": "e13da9aaaf43f07ed2f85d419b6fc2bcc473f2ae0d3cca883b51520240a50530",
    "get_file_outline": "78c0c58f71eb90bbc725d850a6cb8014b6c9624eb9279e1730e2d591f575a74b",
    "get_project_memory": "dc07a132e1af07542961070204c0791845805afb46fc309e8579741ab54a2f03",
}


def _sha(fn: dict) -> str:
    blob = json.dumps(sort_keys_deep(fn), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _uplink_fn(name: str) -> dict:
    """上行快照的 {name, description, parameters}，Bash 描述剥离运行环境提示。"""
    fn = dict(TA3_NATIVE_SCHEMAS[name]["function"])
    if name == "Bash":
        fn["description"] = strip_shell_environment_notes(fn["description"])
    return {"name": fn["name"], "description": fn["description"], "parameters": fn["parameters"]}


# ───────────────────────── 一、15 项 schema 逐字一致 ─────────────────────────


def test_official_definitions_cover_exactly_15_builtins():
    assert set(TA3_OFFICIAL_DEFINITIONS) == set(_OFFICIAL_SHA256)
    assert set(TA3_NATIVE_SCHEMAS) == set(_OFFICIAL_SHA256)
    assert set(TO_TA3.values()) == set(_OFFICIAL_SHA256)


@pytest.mark.parametrize("name", sorted(_OFFICIAL_SHA256))
def test_builtin_schema_matches_official_hash(name):
    """原文定义与上行快照都必须命中官方哈希（任何一个字符的偏差都会失败）。"""
    assert _sha(TA3_OFFICIAL_DEFINITIONS[name]) == _OFFICIAL_SHA256[name], f"{name} 原文与官方不一致"
    assert _sha(_uplink_fn(name)) == _OFFICIAL_SHA256[name], f"{name} 上行快照与官方不一致"


def _official_root() -> Path | None:
    root = Path(__file__).resolve().parents[2] / "tmp_ta3_ref" / "dist" / "main"
    return root if (root / "plugins" / "tools" / "read.js").is_file() else None


@pytest.mark.skipif(_official_root() is None or shutil.which("node") is None,
                    reason="本机没有官方解包产物或 node")
def test_builtin_schema_matches_live_official_export(tmp_path):
    """官方解包在本机时，直接用 node 加载官方插件导出定义做逐字比对。"""
    root = _official_root()
    files = ["read", "list", "search", "diff", "readSkill", "write", "edit", "getFileOutline",
             "getProjectMemory", "generateProjectMemory", "webSearch", "bash", "todoWrite",
             "askUser", "subAgent"]
    script = tmp_path / "dump.mjs"
    script.write_text(
        "import { pathToFileURL } from 'url';\n"
        f"const root = {json.dumps(str(root))};\n"
        f"const files = {json.dumps(files)};\n"
        "const out = {};\n"
        "for (const f of files) {\n"
        "  const mod = await import(pathToFileURL(`${root}/plugins/tools/${f}.js`).href);\n"
        "  Object.values(mod)[0].apply({ tools: { register: (d) => { out[d.function.name] = d.function; } }, skills: {}, memory: {} });\n"
        "}\n"
        "const sr = await import(pathToFileURL(`${root}/tools/shellResolver.js`).href);\n"
        "out.Bash.description = sr.stripShellEnvironmentNotes(out.Bash.description);\n"
        "process.stdout.write(JSON.stringify({ tools: out, notes: { 'git-bash': sr.buildShellEnvironmentNote('git-bash'), "
        "cmd: sr.buildShellEnvironmentNote('cmd'), posix: sr.buildShellEnvironmentNote('posix') } }));\n",
        encoding="utf-8",
    )
    proc = subprocess.run(["node", str(script)], capture_output=True, timeout=60)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    data = json.loads(proc.stdout.decode("utf-8"))
    for name, fn in data["tools"].items():
        assert fn == TA3_OFFICIAL_DEFINITIONS[name], f"{name} 与官方实时导出不一致"
        assert _sha(fn) == _OFFICIAL_SHA256[name]
    assert data["notes"] == SHELL_ENVIRONMENT_NOTES


def test_bash_description_appends_shell_note_per_platform():
    """Bash 描述 = 官方基础描述 + `\\n\\n{运行环境提示}`；三段官方原文逐字、PowerShell 为补充文案。"""
    base = TA3_OFFICIAL_DEFINITIONS["Bash"]["description"]
    for kind, note in {**SHELL_ENVIRONMENT_NOTES, **POWERSHELL_ENVIRONMENT_NOTES}.items():
        desc = bash_description(kind)
        assert desc == f"{base}\n\n{note}"
        assert strip_shell_environment_notes(desc) == base
    # 补充文案不得出现产品名
    for note in POWERSHELL_ENVIRONMENT_NOTES.values():
        assert "chatcoder" not in note.lower()


# ───────────────────────── 二、顺序与序列化 ─────────────────────────


def _fn(name, desc="", params=None):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": params or {"type": "object", "properties": {}},
    }}


def test_disguise_tools_order_matches_official_tool_order():
    """内置按 function.name 字典序（码点序：大写在前、snake_case 在后），MCP 整体排在内置之后。"""
    out = disguise_tools([
        _fn("web_fetch", "抓取。"),
        _fn("todo_write"), _fn("fs_read"), _fn("memory_search"), _fn("ask_user_question"),
        _fn("terminal_exec"), _fn("spawn_subagent"),
        _fn("mcp_codegraph_codegraph_explore", "Explore."),
        _fn("browser_click", "点击。"),
    ])
    names = [s["function"]["name"] for s in out]
    builtins = [n for n in names if not n.startswith("mcp__")]
    assert builtins == sorted(builtins)
    assert builtins == ["Bash", "Read", "SubAgent", "TodoWrite", "ask_user", "get_project_memory"]
    mcp = names[len(builtins):]
    assert all(n.startswith("mcp__") for n in mcp)
    # MCP 按 serverId\0toolName：codegraph-… < workspace-tools-…；同服务器内按工具名
    assert mcp == [mcp_alias("mcp_codegraph_codegraph_explore"),
                   TO_TA3_EXTENSION["browser_click"], TO_TA3_EXTENSION["web_fetch"]]


def test_parameters_are_deep_key_sorted():
    """只排 parameters 的对象键（递归），数组元素顺序不变（required / enum 保持官方顺序）。"""
    def assert_sorted(v):
        if isinstance(v, dict):
            assert list(v) == sorted(v)
            for x in v.values():
                assert_sorted(x)
        elif isinstance(v, list):
            for x in v:
                assert_sorted(x)

    out = disguise_tools([
        _fn("fs_write"), _fn("spawn_subagent"),
        _fn("browser_click", "x", {"type": "object", "required": ["z", "a"],
                                   "properties": {"z": {"type": "string"}, "a": {"type": "integer"}}}),
    ])
    for s in out:
        assert_sorted(s["function"]["parameters"])
    write = next(s for s in out if s["function"]["name"] == "Write")
    assert write["function"]["parameters"]["required"] == ["filepath", "content"]
    assert write["function"]["parameters"]["properties"]["mode"]["enum"] == ["overwrite", "append"]
    ext = next(s for s in out if s["function"]["name"].endswith("__browser_click"))
    assert ext["function"]["parameters"]["required"] == ["z", "a"]


def test_disguise_tools_is_byte_stable():
    """同一输入两次输出逐字节相同（前缀缓存的前提）。"""
    schemas = [_fn("fs_read"), _fn("terminal_exec"), _fn("browser_click", "点击。"),
               _fn("mcp_codegraph_codegraph_explore", "Explore.")]
    a = json.dumps(disguise_tools(schemas), ensure_ascii=False)
    b = json.dumps(disguise_tools(schemas), ensure_ascii=False)
    assert a == b


# ───────────────────────── 三、MCP 命名 ─────────────────────────


def test_server_id_shape_and_stability():
    """官方 store.js 形态：非法字符换 -、小写、`-{base36 毫秒时间戳}`，同名恒定、落在 2025 年。"""
    sid = make_server_id("My Server.v2")
    assert sid == make_server_id("My Server.v2")
    prefix, _, ts = sid.rpartition("-")
    assert prefix == "my-server-v2"
    ms = int(ts, 36)
    assert 1735689600000 <= ms < 1735689600000 + 366 * 24 * 3600 * 1000
    assert EXTENSION_SERVER_ID.startswith("workspace-tools-")
    assert EXTENSION_SERVER_ID == make_server_id("workspace-tools")


def test_real_mcp_server_never_collides_with_extension_channel():
    """真 MCP 服务器恰好也叫 workspace-tools 时必须换 serverId，不能与拓展通道撞名。"""
    assert server_id_for("workspace-tools") != EXTENSION_SERVER_ID
    assert server_id_for("workspace-tools").startswith("workspace-tools-")


class _FakeMcpTool:
    """模拟 McpToolWrapper：真实名有损（.- 换 _），原始名保存在私有字段。"""

    def __init__(self, server: str, tool: str, desc: str = ""):
        self._server_name = server
        self._tool_name = tool
        self.name = f"mcp_{server.replace('.', '_').replace('-', '_')}_{tool.replace('.', '_').replace('-', '_')}"
        self.description = (desc or f"MCP 工具: {tool} (来自 {server})") + f"\n[MCP Server: {server}, Tool: {tool}]"


@pytest.fixture
def fake_mcp_registry(monkeypatch):
    from app.orchestration.tools.registry import tool_registry

    tools = [
        _FakeMcpTool("my-db.prod", "query.run", "Run a query."),
        _FakeMcpTool("my_db", "prod_run"),  # 空描述 → 只留前缀
        _FakeMcpTool("workspace-tools", "browser_click", "伪装同名服务器"),
    ]
    added = []
    for t in tools:
        if t.name not in tool_registry._tools:
            tool_registry._tools[t.name] = t
            added.append(t.name)
    yield tools
    for n in added:
        tool_registry._tools.pop(n, None)


def test_mcp_names_from_wrapper_fields_are_reversible(fake_mcp_registry):
    """服务器名含 -_.、工具名含 . 时：出站名合法、可逆，且描述用显示名、空描述只留前缀。"""
    db = fake_mcp_registry[0]
    alias = mcp_alias(db.name)
    assert alias == f"mcp__{server_id_for('my-db.prod')}__{safe_tool_name('query.run')}"
    assert alias.startswith("mcp__my-db-prod-") and alias.endswith("__query_run")
    assert all(c.isalnum() or c in "_-" for c in alias)
    assert resolve_mcp_alias(alias) == db.name

    out = {s["function"]["name"]: s for s in disguise_tools([
        {"type": "function", "function": {"name": t.name, "description": t.description,
                                          "parameters": {"type": "object", "properties": {}}}}
        for t in fake_mcp_registry
    ])}
    assert out[alias]["function"]["description"] == "[MCP: my-db.prod] Run a query."
    empty = fake_mcp_registry[1]
    assert out[mcp_alias(empty.name)]["function"]["description"] == "[MCP: my_db]"
    # 同为「my」开头的两个服务器不会被切分混淆：各自还原
    assert resolve_mcp_alias(mcp_alias(empty.name)) == empty.name
    ws = fake_mcp_registry[2]
    ws_alias = mcp_alias(ws.name)
    assert ws_alias != TO_TA3_EXTENSION["browser_click"]
    assert resolve_mcp_alias(ws_alias) == ws.name
    assert resolve_mcp_alias(TO_TA3_EXTENSION["browser_click"]) == "browser_click"


def test_empty_mcp_description_keeps_only_prefix():
    """McpToolWrapper 的中文空描述模板按官方空描述处理（只留 `[MCP: 名]`）。"""
    raw = "MCP 工具: run (来自 srv)\n[MCP Server: srv, Tool: run]"
    assert describe("srv", raw) == "[MCP: srv]"


def test_history_replay_uses_same_mcp_name_as_schema(fake_mcp_registry):
    """历史回放与当轮 schema 用同一生成函数：出站历史名 == 当轮 tools 里的名字。"""
    from app.models.providers.ta3 import Ta3Provider
    from app.models.schemas import ChatMessage

    db = fake_mcp_registry[0]
    provider = Ta3Provider(api_key="llm-x", base_url="https://x", model="m")
    m = ChatMessage(role="assistant", content=None,
                    tool_calls=[{"id": "c1", "name": db.name, "arguments": {"sql": "select 1"}}])
    out = provider._disguise_message(m)
    assert out["tool_calls"][0]["function"]["name"] == mcp_alias(db.name)
    restored = provider._restore_tool_calls([
        {"id": "1", "name": mcp_alias(db.name), "arguments": {"sql": "select 1"}},
    ])
    assert restored[0]["name"] == db.name


# ───────────────────────── 四、拓展工具描述清洗 ─────────────────────────


def test_extension_description_rewrites_internal_tool_names():
    text = "先用 fs_read 读取，再用 editor_apply_diff 修改；需要时调用 terminal_bg_status 查看日志。"
    cleaned = clean_extension_text(text)
    assert "fs_read" not in cleaned and "editor_apply_diff" not in cleaned
    assert "Read" in cleaned and "Edit" in cleaned
    assert TO_TA3_EXTENSION["terminal_bg_status"] in cleaned
    # 子串不误伤：fs_read_extra 不是工具名
    assert clean_extension_text("fs_read_extra") == "fs_read_extra"


def test_extension_description_strips_product_fingerprints():
    raw = "路径如 'C:/Users/xx/AppData/Local/chatcoder/uploads/1a/报告.docx'；ChatCoder 内置。"
    out = build_extension_tool(_fn("read_attachment", raw, {
        "type": "object", "properties": {"path": {"type": "string", "description": raw}},
    }))
    blob = json.dumps(out, ensure_ascii=False).lower()
    assert "chatcoder" not in blob
    assert "appdata" not in blob
    assert out["function"]["description"].startswith(f"[MCP: {EXTENSION_SERVER_NAME}] ")


def test_registry_extension_tools_have_no_product_fingerprints():
    from app.orchestration.tools.registry import tool_registry

    out = disguise_tools(tool_registry.all_schemas())
    ext = [s for s in out if s["function"]["name"].startswith(f"mcp__{EXTENSION_SERVER_ID}__")]
    assert len(ext) >= 30
    blob = json.dumps(ext, ensure_ascii=False)
    assert "chatcoder" not in blob.lower()
    for internal in ("fs_read", "fs_write", "fs_grep", "terminal_exec", "editor_apply_diff",
                     "spawn_subagent", "todo_write", "ask_user_question", "memory_write", "memory_search"):
        assert f"{internal}" not in _strip_names(blob), f"拓展工具描述仍引用内部工具名 {internal}"


def _strip_names(blob: str) -> str:
    """去掉 mcp__… 名字本身（它们合法地包含真实工具名作为 toolName 段）。"""
    import re

    return re.sub(r"mcp__[A-Za-z0-9_-]+", "", blob)


# ───────────────────────── 五、入站还原 ─────────────────────────


def test_restore_read_drops_document_params_and_keeps_paging():
    assert restore_args("Read", {"filepath": "a.docx", "docx": {"mode": "overview"}}) == {"path": "a.docx"}
    assert restore_args("Read", {"filepath": "a.py", "document": {"mode": "pages"}}) == {"path": "a.py"}
    assert restore_args("Read", {"filepath": "a.py", "offset": 601}) == {"path": "a.py", "offset": 601}


def test_restore_bash_drops_purpose():
    out = restore_args("Bash", {"purpose": "列出文件", "command": "dir", "waitForCompletion": True})
    assert out == {"command": "dir", "waitForCompletion": True}


def test_restore_diff_requests_full_diff():
    assert restore_args("Diff", {}) == {"stat_only": False, "tracked_only": True}


def test_restore_write_mode():
    assert restore_args("Write", {"filepath": "a", "content": "x"}) == {"path": "a", "content": "x"}
    assert restore_args("Write", {"filepath": "a", "content": "x", "mode": "overwrite"}) == {"path": "a", "content": "x"}
    assert restore_args("Write", {"filepath": "a", "content": "x", "mode": "Append"}) == {
        "path": "a", "content": "x", "mode": "append"}


def test_restore_ask_user_object_options():
    out = restore_args("ask_user", {
        "title": "方案确认",
        "questions": [{
            "id": "q1", "question": "用哪种存储？", "description": "影响部署",
            "options": [{"label": "SQLite", "value": "sqlite", "description": "零依赖"}, {"label": "PG"}],
            "allowCustom": False, "multiple": True,
        }],
    })
    assert out == {"questions": [{
        "question": "用哪种存储？\n影响部署",
        "options": ["SQLite — 零依赖", "PG"],
        "allow_custom": False,
    }]}
    # allowCustom 缺省时不写（工具侧默认允许自定义）
    assert "allow_custom" not in restore_args("ask_user", {"questions": [{"question": "q"}]})["questions"][0]


def test_restore_ask_user_output_accepted_by_real_tool():
    from app.orchestration.tools.ask_user import _normalize_questions

    out = restore_args("ask_user", {"questions": [{"question": "q", "options": [{"label": "A"}]}]})
    assert _normalize_questions(out["questions"]) == [{"question": "q", "options": ["A"]}]


def test_restore_web_search_engines_and_domain():
    assert restore_args("WebSearch", {"query": "q", "searchEngine": "search_std"})["engine"] == "workbuddy"
    assert restore_args("WebSearch", {"query": "q", "searchEngine": "search_pro_quark"})["engine"] == "bing"
    out = restore_args("WebSearch", {"query": "q", "maxResults": 30, "timeRange": "week",
                                     "includeDomains": ["docs.python.org"], "contentSize": "high"})
    assert out == {"query": "site:docs.python.org q", "max_results": 30}


def test_restore_generate_project_memory():
    out = restore_args("generate_project_memory", {"memories": [
        {"name": "user_role", "description": "用户身份", "type": "user", "content": "后端工程师"},
        {"name": "feedback_tests", "type": "feedback", "content": "改动后必须跑测试"},
        {"name": "deploy", "type": "project", "content": "部署走 GitHub Actions"},
        {"name": "ref", "type": "reference", "content": "接口文档在 docs/api.md"},
    ], "chatHistory": []})
    assert out == {"memories": [
        {"text": "user_role：用户身份\n后端工程师", "kind": "fact", "scope": "global"},
        {"text": "feedback_tests\n改动后必须跑测试", "kind": "convention", "scope": "global"},
        {"text": "deploy\n部署走 GitHub Actions", "kind": "decision", "scope": "project"},
        {"text": "ref\n接口文档在 docs/api.md", "kind": "fact", "scope": "project"},
    ]}
    # 只给 chatHistory：本项目无自动提取能力，交给工具报错
    assert restore_args("generate_project_memory", {"chatHistory": [{}]}) == {}


def test_restore_get_project_memory_all():
    assert restore_args("get_project_memory", {"query": "all"}) == {
        "keyword": "all", "list_all": True, "limit": 50}
    assert restore_args("get_project_memory", {"query": "部署"}) == {"keyword": "部署"}


@pytest.mark.parametrize("raw, expected", [("false", False), ("true", True), ("False", False), (True, True)])
def test_restore_subagent_inherit_context_bool(raw, expected):
    out = restore_args("SubAgent", {"prompt": "p", "description": "d", "inherit_context": raw})
    assert out["inherit_context"] is expected


@pytest.mark.parametrize("stype, readonly", [
    ("Explore", True), ("Plan", True), ("Verification", False), ("GeneralPurpose", False), (None, False),
])
def test_restore_subagent_type_readonly(stype, readonly):
    args = {"prompt": "p", "description": "d"}
    if stype:
        args["subagent_type"] = stype
    out = restore_args("SubAgent", args)
    assert out["task_description"] == "p" and out["task_title"] == "d"
    assert "subagent_type" not in out
    assert (out.get("mode") == "readonly") is readonly
    assert bool(out.get("explore")) is readonly


# ───────────────────────── 六、出站历史换算 ─────────────────────────


def test_disguise_memory_write_history_to_official_shape():
    out = disguise_args("memory_write", {"text": "部署走 CI", "kind": "decision", "scope": "project",
                                         "keywords": ["部署", "CI"]})
    assert out == {"memories": [{"name": "部署", "type": "project", "content": "部署走 CI"}]}
    out = disguise_args("memory_write", {"text": "回复用中文", "kind": "convention", "scope": "global"})
    assert out["memories"][0]["type"] == "feedback"
    # 往返：官方形态 → 真实形态可再次写入
    back = restore_args("generate_project_memory", out)
    assert back["memories"][0]["scope"] == "global"


def test_disguise_history_args_stay_within_official_schema():
    """内置通道的历史参数只含官方 schema 声明的键（Read 的 offset/limit 例外）。"""
    samples = {
        "fs_read": {"path": "a", "offset": 3, "limit": 10},
        "fs_list": {"path": "d", "recursive": True, "max_depth": 3},
        "fs_grep": {"pattern": "x", "include": "*.py", "context_lines": 2},
        "git_diff": {"stat_only": True, "repo": "r"},
        "terminal_exec": {"command": "ls", "cwd": "sub", "timeout": 30},
        "web_search": {"query": "q", "engine": "google", "max_results": 3},
        "ask_user_question": {"questions": [{"question": "q", "options": ["A"], "allow_custom": False}]},
        "spawn_subagent": {"task_title": "t", "task_description": "p", "explore": True, "inherit_context": "true"},
        "todo_write": {"todos": [{"content": "c", "status": "pending", "owner": 2}]},
    }
    for real, args in samples.items():
        alias = TO_TA3[real]
        allowed = set(TA3_OFFICIAL_DEFINITIONS[alias]["parameters"]["properties"]) | (
            {"offset", "limit"} if alias == "Read" else set())
        out = disguise_args(real, args)
        assert set(out) <= allowed, f"{real} 历史参数越出官方 schema: {set(out) - allowed}"
    assert disguise_args("git_diff", {"stat_only": True}) == {}
    assert disguise_args("ask_user_question", samples["ask_user_question"]) == {
        "questions": [{"question": "q", "options": [{"label": "A"}], "allowCustom": False}]}
    assert disguise_args("spawn_subagent", samples["spawn_subagent"]) == {
        "description": "t", "prompt": "p", "subagent_type": "Explore", "inherit_context": True}
    assert disguise_args("web_search", samples["web_search"])["searchEngine"] == "search_pro"


def test_disguise_args_does_not_mutate_input():
    args = {"questions": [{"question": "q", "options": ["A"], "allow_custom": True}]}
    before = json.dumps(args, ensure_ascii=False)
    disguise_args("ask_user_question", args)
    assert json.dumps(args, ensure_ascii=False) == before


# ───────────────────────── 七、真实工具执行（还原结果可执行） ─────────────────────────


def _ctx(workspace):
    from app.orchestration.tools.base import ToolContext

    return ToolContext(workspace_root=str(workspace), session_id=1, task_id=1, agent_id=1, agent_name="t")


@pytest.mark.asyncio
async def test_fs_write_append_mode(tmp_path):
    from app.orchestration.tools.fs_write import FsWriteTool

    tool = FsWriteTool()
    # 文件不存在：append 等同创建
    r = await tool.run(restore_args("Write", {"filepath": "log.txt", "content": "a\n", "mode": "append"}),
                       _ctx(tmp_path))
    assert r.ok, r.error
    # 新建文件沿用 fs_write 既有的创建行为（文本模式写入，换行按平台）
    assert (tmp_path / "log.txt").read_text(encoding="utf-8") == "a\n"
    # 已存在：追加到末尾，行尾随文件现有风格（CRLF）
    (tmp_path / "crlf.txt").write_bytes(b"x\r\ny\r\n")
    r = await tool.run({"path": "crlf.txt", "content": "z\n", "mode": "append"}, _ctx(tmp_path))
    assert r.ok, r.error
    assert (tmp_path / "crlf.txt").read_bytes() == b"x\r\ny\r\nz\r\n"
    assert "追加" in r.output
    # 非法 mode 报错且不写盘
    r = await tool.run({"path": "n.txt", "content": "x", "mode": "insert"}, _ctx(tmp_path))
    assert r.ok is False and not (tmp_path / "n.txt").exists()


async def _seed_session_row() -> int:
    from app.persistence.database import async_session_factory
    from app.persistence.models.message import Session

    async with async_session_factory() as db:
        row = Session(title="ta3-601")
        db.add(row)
        await db.commit()
        return int(row.id)


@pytest.mark.asyncio
async def test_generate_project_memory_batch_writes_each_item(tmp_path):
    """官方 memories[] 经还原后由 memory_write 逐条写入；只传 text 的旧调用不受影响。"""
    from sqlalchemy import select

    from app.orchestration.tools.memory_write import MemoryWriteTool
    from app.persistence.database import async_session_factory
    from app.persistence.models.memory import MemoryEntry

    sid = await _seed_session_row()
    ctx = _ctx(tmp_path)
    ctx.session_id = sid
    args = restore_args("generate_project_memory", {"memories": [
        {"name": "user_lang", "type": "user", "content": "用户偏好简体中文回复"},
        {"name": "feedback_tests", "type": "feedback", "content": "每次改动后都要跑相关测试"},
    ]})
    r = await MemoryWriteTool().run(args, ctx)
    assert r.ok, r.error
    assert r.data["count"] == 2
    async with async_session_factory() as db:
        rows = list((await db.execute(select(MemoryEntry))).scalars().all())
    assert {row.scope for row in rows} == {"global"}
    assert {row.kind for row in rows} == {"fact", "convention"}
    # 旧调用：单条 text
    r = await MemoryWriteTool().run({"text": "单条写入路径保持不变的记忆", "scope": "global"}, ctx)
    assert r.ok and r.data["count"] == 1
    # 空 memories：明确报错
    r = await MemoryWriteTool().run({"memories": []}, ctx)
    assert r.ok is False


@pytest.mark.asyncio
async def test_get_project_memory_all_lists_every_entry(tmp_path):
    """query='all' 还原为列表模式：不按关键词过滤，返回全部可见记忆。"""
    from app.orchestration.tools.memory_search import MemorySearchTool
    from app.orchestration.tools.memory_write import MemoryWriteTool

    sid = await _seed_session_row()
    ctx = _ctx(tmp_path)
    ctx.session_id = sid
    for text in ("第一条记忆：部署走 CI 流水线", "第二条记忆：数据库位置固定在本地"):
        assert (await MemoryWriteTool().run({"text": text, "scope": "global"}, ctx)).ok
    r = await MemorySearchTool().run(restore_args("get_project_memory", {"query": "all"}), ctx)
    assert r.ok, r.error
    assert r.data["memory_total"] == 2 and r.data["list_all"] is True
    assert "部署走 CI" in r.output and "数据库位置" in r.output
    # 普通关键词检索不受影响（'all' 只在还原层触发列表模式）
    plain = await MemorySearchTool().run({"keyword": "all", "sources": "memory"}, ctx)
    assert plain.ok and not plain.data.get("list_all")


@pytest.mark.asyncio
async def test_fs_write_default_overwrite_unchanged(tmp_path):
    from app.orchestration.tools.fs_write import FsWriteTool

    (tmp_path / "a.txt").write_text("old", encoding="utf-8")
    r = await FsWriteTool().run({"path": "a.txt", "content": "new"}, _ctx(tmp_path))
    assert r.ok and (tmp_path / "a.txt").read_text(encoding="utf-8") == "new"
    assert r.output.startswith("已写入")
    # 非 ta3 schema 不暴露 mode
    props = FsWriteTool().function_schema()["function"]["parameters"]["properties"]
    assert "mode" not in props
