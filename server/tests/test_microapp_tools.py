"""微应用后端工具面（plan-693-2868 §7 P0 第 5 条）：schema、列表、调用、校验。

不触碰真实 HOME：库根一律由 `CHATCODER_MICROAPP_ROOT` 指向 tmp_path（§6.1），
否则「空库」用例会读到开发者本机的 `~/.chatcoder/microapps`，测试结果随机器而变。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.orchestration.tools.base import ToolContext
from app.orchestration.tools.microapp import (
    ALL_MICROAPP_TOOLS,
    MICROAPP_RESULT_MAX_CHARS,
    MicroAppCallTool,
    MicroAppListTool,
    MicroAppValidateTool,
    microapp_root,
)

APP_ID = "demo-app"
VERSION = "1.0.0"


@pytest.fixture(autouse=True)
def lib_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """库根固定到临时目录（目录本身先不建：list 用例要验证「库不存在」）。"""
    root = tmp_path / "microapps"
    monkeypatch.setenv("CHATCODER_MICROAPP_ROOT", str(root))
    monkeypatch.setenv("CHATCODER_MICROAPP_EXT_ROOT", str(tmp_path / "microapps-ext"))
    return root


@pytest.fixture
def ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(
        workspace_root=str(tmp_path), session_id=1, task_id=1, agent_id=1, agent_name="测试代理",
    )


@pytest.fixture
def bridge_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """把浏览器桥的 request 换成「记录器 + 固定成功回包」。

    记录每次调用的入参，用例据此断言「调用真的经桥发到了桌面端」——只断言返回文案的话，
    措辞一改或调用被短路都发现不了。需要失败形态的用例自行 monkeypatch 覆盖。
    """
    from app.orchestration.browser.bridge import bridge

    calls: list[dict] = []

    async def fake_request(session_id, op, args, timeout, cancel_event=None):
        calls.append({
            "session_id": session_id, "op": op, "args": args,
            "timeout": timeout, "cancel_event": cancel_event,
        })
        return {
            "appId": args["appId"], "version": args["version"], "method": args["method"],
            "result": {"ok": True, "drawn": 1},
        }

    monkeypatch.setattr(bridge, "request", fake_request)
    return calls


def _valid_manifest(app_id: str = APP_ID, version: str = VERSION) -> dict:
    return {
        "appId": app_id,
        "name": "示例画布",
        "version": version,
        "description": "画流程图的最小示例",
        "entry": "app/index.html",
        "methods": [
            {"name": "drawRect", "description": "画一个矩形", "params": {"x": "number"}},
            {"name": "readCanvas", "description": "读取当前画布"},
        ],
        "permissions": ["clipboard"],
        "runtime": {"vendors": [{"name": "antd", "version": "5.20.0"}]},
    }


def _write_app(
    lib: Path,
    *,
    app_id: str = APP_ID,
    version: str = VERSION,
    manifest: dict | None = None,
    raw_manifest: str | None = None,
    create_entry: bool = True,
) -> Path:
    """按 §6.1 的 `<root>/<appId>/<version>/manifest.json` 落一个最小应用。"""
    version_dir = lib / app_id / version
    (version_dir / "app").mkdir(parents=True)
    if create_entry:
        (version_dir / "app" / "index.html").write_text(
            "<!doctype html><meta charset='utf-8'><title>demo</title>", encoding="utf-8",
        )
    if raw_manifest is not None:
        (version_dir / "manifest.json").write_text(raw_manifest, encoding="utf-8")
    else:
        payload = _valid_manifest(app_id, version) if manifest is None else manifest
        (version_dir / "manifest.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8",
        )
    return version_dir


# ───────────────────────── 契约与注册 ─────────────────────────


@pytest.mark.parametrize("tool_cls", ALL_MICROAPP_TOOLS)
def test_schema_is_openai_function_shaped(tool_cls):
    tool = tool_cls()
    schema = tool.function_schema()
    assert schema["type"] == "function"
    fn = schema["function"]
    assert fn["name"] == tool.name
    assert isinstance(fn["description"], str) and fn["description"].strip()
    params = fn["parameters"]
    assert params["type"] == "object"
    assert isinstance(params["properties"], dict)
    # required 只能引用已声明的属性，否则模型会照着不存在的键传参
    for key in params["required"]:
        assert key in params["properties"], f"{tool.name} 的 required 引用了未声明参数: {key}"


def test_declared_names_risk_and_concurrency():
    assert [cls().name for cls in ALL_MICROAPP_TOOLS] == [
        "microapp_list", "microapp_call", "microapp_validate",
    ]
    assert MicroAppListTool.risk_level == "low"
    assert MicroAppListTool.concurrency_safe is True
    assert MicroAppCallTool.risk_level == "medium"
    assert MicroAppCallTool.concurrency_safe is False
    assert MicroAppValidateTool.risk_level == "low"
    assert MicroAppValidateTool.concurrency_safe is True


def test_tools_are_registered():
    from app.orchestration.tools.registry import tool_registry

    for name in ("microapp_list", "microapp_call", "microapp_validate"):
        assert tool_registry.get(name) is not None, f"{name} 未注册：ta3 与子代理都取不到"


def test_readonly_subagent_gets_list_only():
    """D9：只读子代理只允许列表，call / validate 不进白名单。"""
    from app.orchestration.subagent_tools import EXPLORE_TOOLS

    assert "microapp_list" in EXPLORE_TOOLS
    assert "microapp_call" not in EXPLORE_TOOLS
    assert "microapp_validate" not in EXPLORE_TOOLS


def test_ta3_extension_aliases_exist():
    """§7 P0 第 1 条：三名缺任一，ta3 会话下工具会被 disguise_tools 静默剔除。"""
    from app.models.providers.ta3_mcp import EXTENSION_SERVER_ID, ta3_mcp_name
    from app.models.providers.ta3_tool_aliases import FROM_TA3, TO_TA3_EXTENSION

    for name in ("microapp_list", "microapp_call", "microapp_validate"):
        alias = ta3_mcp_name(EXTENSION_SERVER_ID, name)
        assert TO_TA3_EXTENSION[name] == alias
        assert alias.startswith("mcp__") and alias.endswith(f"__{name}")
        assert FROM_TA3[alias] == name


def test_root_env_is_honoured(lib_root: Path):
    assert microapp_root() == lib_root


# ───────────────────────── microapp_list ─────────────────────────


async def test_list_missing_library_is_ok_and_empty(lib_root: Path, ctx: ToolContext):
    """库不存在 = 没有应用，不是错误（首次使用必然如此）。"""
    assert not lib_root.exists()
    result = await MicroAppListTool().run({}, ctx)
    assert result.ok is True
    assert result.data["apps"] == []
    assert result.data["count"] == 0


async def test_list_returns_summary_of_installed_app(lib_root: Path, ctx: ToolContext):
    _write_app(lib_root)
    result = await MicroAppListTool().run({}, ctx)
    assert result.ok is True
    assert result.data["count"] == 1
    app = result.data["apps"][0]
    assert app["appId"] == APP_ID
    assert app["version"] == VERSION
    assert app["name"] == "示例画布"
    assert app["methods"] == ["drawRect", "readCanvas"]
    assert "drawRect" in result.output


async def test_list_keeps_highest_version_per_app(lib_root: Path, ctx: ToolContext):
    """同一 appId 装多版本时只报一份，且是最新那份（否则「装了 v2 调到 v1」无法解释）。"""
    _write_app(lib_root, version="1.0.0")
    _write_app(lib_root, version="1.2.0")
    result = await MicroAppListTool().run({}, ctx)
    assert result.data["count"] == 1
    assert result.data["apps"][0]["version"] == "1.2.0"


# ───────────────────────── microapp_call ─────────────────────────


@pytest.mark.parametrize(
    "bad_app_id",
    ["", "A", "Demo-App", "demo app", "..", "-demo", "d" * 64],
)
async def test_call_invalid_app_id_is_arg_invalid(bad_app_id, lib_root: Path, ctx: ToolContext):
    result = await MicroAppCallTool().run({"appId": bad_app_id, "method": "drawRect"}, ctx)
    assert result.ok is False
    assert result.error_code == "ARG_INVALID"


async def test_call_uninstalled_app_is_not_found(lib_root: Path, ctx: ToolContext):
    lib_root.mkdir(parents=True)
    result = await MicroAppCallTool().run({"appId": APP_ID, "method": "drawRect"}, ctx)
    assert result.ok is False
    assert result.error_code == "NOT_FOUND"


async def test_call_unknown_method_is_not_found_with_available_names(
    lib_root: Path, ctx: ToolContext,
):
    _write_app(lib_root)
    result = await MicroAppCallTool().run({"appId": APP_ID, "method": "eraseAll"}, ctx)
    assert result.ok is False
    assert result.error_code == "NOT_FOUND"
    assert "drawRect" in result.hint and "readCanvas" in result.hint
    assert result.data["availableMethods"] == ["drawRect", "readCanvas"]


async def test_call_args_must_be_object(lib_root: Path, ctx: ToolContext):
    _write_app(lib_root)
    result = await MicroAppCallTool().run(
        {"appId": APP_ID, "method": "drawRect", "args": [1, 2]}, ctx,
    )
    assert result.ok is False
    assert result.error_code == "ARG_INVALID"


@pytest.mark.parametrize("bad_method", ["draw rect", "drawRect()", "1draw", "a" * 65])
async def test_call_illegal_method_name_is_arg_invalid(
    bad_method: str, lib_root: Path, ctx: ToolContext, bridge_calls: list[dict],
):
    """方法名会被贴成页面里的取值表达式：非法名在本地就拒掉，不要送去主进程再被白名单挡回来。"""
    _write_app(lib_root)
    result = await MicroAppCallTool().run({"appId": APP_ID, "method": bad_method}, ctx)
    assert result.ok is False
    assert result.error_code == "ARG_INVALID"
    assert bridge_calls == []


async def test_call_valid_payload_executes_through_bridge(
    lib_root: Path, ctx: ToolContext, bridge_calls: list[dict],
):
    """合法调用要真的经桥发出去并把返回值带回来（plan-693-2868 P0-4）。

    判据是「桥收到的请求体」+「工具返回的 data.result」，而不是文案——
    后者改措辞就会误报通过，前者才代表调用真的抵达了桌面端。
    """
    _write_app(lib_root)
    result = await MicroAppCallTool().run(
        {"appId": APP_ID, "method": "drawRect", "args": {"x": 10, "y": 20}}, ctx,
    )
    assert result.ok is True, result.error
    assert result.data == {
        "appId": APP_ID, "method": "drawRect", "result": {"ok": True, "drawn": 1},
    }
    assert len(bridge_calls) == 1
    call = bridge_calls[0]
    assert call["op"] == "microappCall"
    assert call["session_id"] == ctx.session_id
    assert call["args"] == {
        "appId": APP_ID,
        "version": VERSION,          # 版本取自磁盘上实际安装的那份，不能靠入参猜
        "method": "drawRect",
        "args": {"x": 10, "y": 20},
    }
    # 桥的超时必须比主进程的方法上限（15 s）更宽松，
    # 否则「界面方法超时」永远被桥抢先覆盖成「宿主没回包」
    assert call["timeout"] >= 15
    assert "drawRect" in result.output and "drawn" in result.output


async def test_call_without_args_sends_empty_object(
    lib_root: Path, ctx: ToolContext, bridge_calls: list[dict],
):
    _write_app(lib_root)
    result = await MicroAppCallTool().run({"appId": APP_ID, "method": "readCanvas"}, ctx)
    assert result.ok is True, result.error
    assert bridge_calls[0]["args"]["args"] == {}


async def test_call_reports_host_unavailable(
    lib_root: Path, ctx: ToolContext, monkeypatch: pytest.MonkeyPatch,
):
    """桌面端没连上（或发送途中断开）：EXEC_FAILED，且 hint 要说清是「没连」而非「方法写错」。"""
    from app.orchestration.browser.bridge import HostUnavailable, bridge

    _write_app(lib_root)

    async def boom(*_a, **_k):
        raise HostUnavailable("没有已连接的桌面端浏览器")

    monkeypatch.setattr(bridge, "request", boom)
    result = await MicroAppCallTool().run({"appId": APP_ID, "method": "drawRect"}, ctx)
    assert result.ok is False
    assert result.error_code == "EXEC_FAILED"
    assert "桌面端" in result.hint and "界面" in result.hint
    assert result.data == {"appId": APP_ID, "version": VERSION, "method": "drawRect"}


@pytest.mark.parametrize(
    ("prefix", "detail", "hint_keyword"),
    [
        ("microapp-guest-missing", "面板里没打开", "右侧面板"),
        ("microapp-guest-loading", "还是空白页", "正在加载"),
        ("microapp-bridge-missing", "", "__MICROAPP_METHODS__"),
        ("microapp-method-missing", "readCanvas", "app.js"),
        ("microapp-method-invalid", "bad name", "标识符"),
        ("microapp-not-serializable", "Converting circular", "JSON"),
        ("microapp-threw", "Cannot read properties of null", "抛错"),
        ("microapp-timeout", "15 秒内没有返回", "超时"),
    ],
)
async def test_call_maps_bridge_error_to_actionable_hint(
    lib_root: Path, ctx: ToolContext, monkeypatch: pytest.MonkeyPatch,
    prefix: str, detail: str, hint_keyword: str,
):
    """主进程用固定前缀回报失败原因，后端必须据此给出**可执行**的下一步，而不是笼统「调用失败」。"""
    from app.orchestration.browser.bridge import HostCallError, bridge

    _write_app(lib_root)

    async def boom(*_a, **_k):
        raise HostCallError(f"{prefix}: {detail}" if detail else prefix)

    monkeypatch.setattr(bridge, "request", boom)
    result = await MicroAppCallTool().run({"appId": APP_ID, "method": "drawRect"}, ctx)
    assert result.ok is False
    assert result.error_code == "EXEC_FAILED"
    assert hint_keyword in result.hint, f"hint 没覆盖 {prefix}：{result.hint}"
    # 出错也要能看出是哪个应用的哪个方法，否则用户无从复现
    assert APP_ID in result.error and "drawRect" in result.error


async def test_call_surfaces_method_exception_message(
    lib_root: Path, ctx: ToolContext, monkeypatch: pytest.MonkeyPatch,
):
    """页面方法抛错时把 error.message 透出来——模型据此能改 app.js，而不是只知道「失败了」。"""
    from app.orchestration.browser.bridge import HostCallError, bridge

    _write_app(lib_root)

    async def boom(*_a, **_k):
        raise HostCallError("microapp-threw: Cannot read properties of null (reading 'width')")

    monkeypatch.setattr(bridge, "request", boom)
    result = await MicroAppCallTool().run({"appId": APP_ID, "method": "drawRect"}, ctx)
    assert result.ok is False
    assert result.error_code == "EXEC_FAILED"
    assert "Cannot read properties of null" in result.error


async def test_call_bridge_timeout_is_explained(
    lib_root: Path, ctx: ToolContext, monkeypatch: pytest.MonkeyPatch,
):
    """桥自己判的超时（宿主 20 s 没回包）与「界面方法超时」是两回事：前者可能整端卡住。"""
    from app.orchestration.browser.bridge import HostCallError, bridge

    _write_app(lib_root)

    async def boom(*_a, **_k):
        raise HostCallError("浏览器操作超时（microappCall）：宿主可能已经执行了这次操作")

    monkeypatch.setattr(bridge, "request", boom)
    result = await MicroAppCallTool().run({"appId": APP_ID, "method": "drawRect"}, ctx)
    assert result.ok is False
    assert result.error_code == "EXEC_FAILED"
    assert "26 秒" in result.error
    assert "不要直接重复" in result.hint


async def test_call_old_desktop_build_is_reported_as_version_problem(
    lib_root: Path, ctx: ToolContext, monkeypatch: pytest.MonkeyPatch,
):
    """协议版本没升，老客户端仍会被登记成宿主但不认识 microappCall：必须点明是版本问题。

    否则用户只看到「未知的浏览器原语」，会以为是 manifest 或方法名写错了。
    """
    from app.orchestration.browser.bridge import HostCallError, bridge

    _write_app(lib_root)

    async def boom(*_a, **_k):
        raise HostCallError("未知的浏览器原语：microappCall")

    monkeypatch.setattr(bridge, "request", boom)
    result = await MicroAppCallTool().run({"appId": APP_ID, "method": "drawRect"}, ctx)
    assert result.ok is False
    assert result.error_code == "EXEC_FAILED"
    assert "不支持微应用方法调用" in result.error
    assert "升级" in result.hint


async def test_call_truncates_oversized_result(
    lib_root: Path, ctx: ToolContext, monkeypatch: pytest.MonkeyPatch,
):
    """返回值过长时 output 截断（护住上下文），但 data.result 必须是完好的。"""
    from app.orchestration.browser.bridge import bridge

    _write_app(lib_root)
    big = {"rows": ["x" * 100 for _ in range(120)]}

    async def fake(_session_id, _op, args, _timeout, cancel_event=None):
        return {
            "appId": args["appId"], "version": args["version"],
            "method": args["method"], "result": big,
        }

    monkeypatch.setattr(bridge, "request", fake)
    result = await MicroAppCallTool().run({"appId": APP_ID, "method": "drawRect"}, ctx)
    assert result.ok is True
    assert result.data["result"] == big
    assert "已截断" in result.output
    assert len(result.output) < MICROAPP_RESULT_MAX_CHARS + 200


async def test_call_rejects_unserializable_result(
    lib_root: Path, ctx: ToolContext, monkeypatch: pytest.MonkeyPatch,
):
    """返回值不可 JSON 序列化时报 EXEC_FAILED（不能把 set / 自定义对象当成功回给模型）。"""
    from app.orchestration.browser.bridge import bridge

    _write_app(lib_root)

    async def fake(_session_id, _op, args, _timeout, cancel_event=None):
        return {
            "appId": args["appId"], "version": args["version"],
            "method": args["method"], "result": {"s": {1, 2}},
        }

    monkeypatch.setattr(bridge, "request", fake)
    result = await MicroAppCallTool().run({"appId": APP_ID, "method": "drawRect"}, ctx)
    assert result.ok is False
    assert result.error_code == "EXEC_FAILED"
    assert "JSON" in result.hint


# ─────────────────────── microapp_validate ───────────────────────


async def test_validate_accepts_well_formed_manifest(lib_root: Path, ctx: ToolContext):
    _write_app(lib_root)
    result = await MicroAppValidateTool().run({"appId": APP_ID}, ctx)
    assert result.ok is True, result.output
    assert result.data["problems"] == []
    assert result.data["version"] == VERSION


async def test_validate_reports_unparsable_manifest(lib_root: Path, ctx: ToolContext):
    _write_app(lib_root, raw_manifest="{ 这不是 JSON")
    result = await MicroAppValidateTool().run({"appId": APP_ID}, ctx)
    assert result.ok is False
    assert any("无法解析" in p for p in result.data["problems"])


async def test_validate_reports_missing_manifest(lib_root: Path, ctx: ToolContext):
    version_dir = lib_root / APP_ID / VERSION
    version_dir.mkdir(parents=True)
    result = await MicroAppValidateTool().run({"appId": APP_ID}, ctx)
    assert result.ok is False
    assert any("缺少 manifest.json" in p for p in result.data["problems"])


async def test_validate_lists_every_problem(lib_root: Path, ctx: ToolContext):
    """一次把所有问题都报出来，而不是让用户改一条、再校验、再发现下一条。"""
    broken = {
        "appId": "other-app",           # 与目录名不一致
        "name": "",                     # 空名
        "version": "v1",                # 非法版本号
        "description": 123,             # 类型错
        "entry": "app/missing.html",    # 文件不存在
        "methods": [{"name": "draw rect"}, {"name": "drawRect"}, {"name": "drawRect"}],
        "permissions": "clipboard",     # 不是数组
        "runtime": {"vendors": [{"name": "antd"}]},  # vendor 缺 version
    }
    _write_app(lib_root, manifest=broken, create_entry=False)
    result = await MicroAppValidateTool().run({"appId": APP_ID}, ctx)
    assert result.ok is False
    text = "；".join(result.data["problems"])
    for expected in (
        "与安装目录名", "manifest.name", "不是合法版本号", "manifest.description",
        "指向的文件不存在", "不是合法方法名", "重复方法名",
        "permissions", "runtime.vendors[0].version",
    ):
        assert expected in text, f"缺少问题项: {expected}；实际：{text}"
    # 问题逐条进 output，模型不用再读 data 才能看到细节
    assert result.output.count("\n") >= len(result.data["problems"])


async def test_validate_rejects_entry_escaping_app_dir(lib_root: Path, ctx: ToolContext):
    outside = lib_root / "secret.html"
    manifest = _valid_manifest()
    manifest["entry"] = "../secret.html"
    _write_app(lib_root, manifest=manifest, create_entry=False)
    outside.write_text("x", encoding="utf-8")
    result = await MicroAppValidateTool().run({"appId": APP_ID}, ctx)
    assert result.ok is False
    assert any("越出应用目录" in p for p in result.data["problems"])


async def test_validate_invalid_app_id_and_uninstalled(lib_root: Path, ctx: ToolContext):
    bad = await MicroAppValidateTool().run({"appId": "Demo App"}, ctx)
    assert bad.ok is False and bad.error_code == "ARG_INVALID"
    missing = await MicroAppValidateTool().run({"appId": "no-such-app"}, ctx)
    assert missing.ok is False and missing.error_code == "NOT_FOUND"
