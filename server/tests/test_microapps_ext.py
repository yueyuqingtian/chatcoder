"""外置微应用接入（protocol 13-external-microapps）：清单校验、列表、工具面分支。

不触碰真实 HOME：两个库根一律由环境变量指向 tmp_path，否则「空库」用例会读到
开发者本机的库目录，测试结果随机器而变。

调用链路一律用 `httpx.MockTransport` 拦截：本文件的任何用例都**不**起真实进程、
不监听端口——「进程生命周期归 Electron 主进程」（协议 §5）这条边界在测试里也要守住。
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.gateway.routers import microapps_ext as microapps_ext_router
from app.orchestration.tools import microapps_ext as registry
from app.orchestration.tools.base import ToolContext
from app.orchestration.tools.microapp import MicroAppCallTool, MicroAppListTool

APP_ID = "ext-demo"
VERSION = "1.0.0"


# ───────────────────────── 夹具 ─────────────────────────


@pytest.fixture(autouse=True)
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """两个库根都固定到临时目录（内置库不建目录：部分用例要验证「库不存在」）。"""
    builtin = tmp_path / "microapps"
    external = tmp_path / "microapps-ext"
    monkeypatch.setenv("CHATCODER_MICROAPP_ROOT", str(builtin))
    monkeypatch.setenv("CHATCODER_MICROAPP_EXT_ROOT", str(external))
    return builtin, external


@pytest.fixture
def ext_root(roots: tuple[Path, Path]) -> Path:
    return roots[1]


@pytest.fixture
def ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(
        workspace_root=str(tmp_path), session_id=1, task_id=1, agent_id=1, agent_name="测试代理",
    )


def _manifest(
    microapp_id: str = APP_ID,
    version: str = VERSION,
    *,
    with_server: bool = True,
    methods: list[str] | None = None,
    frontend_url: str = "http://127.0.0.1:5174/",
) -> dict:
    manifest: dict = {
        "id": microapp_id,
        "name": "外置示例",
        "version": version,
        "description": "自带后端的外置微应用",
        "frontend": {"url": frontend_url},
        "capabilities": {
            "methods": [
                {"name": name, "description": f"{name} 的描述"}
                for name in (methods or ["openProject", "listFiles"])
            ],
        },
    }
    if with_server:
        manifest["server"] = {
            "command": "node server.cjs", "autostart": True, "port": 0,
        }
    return manifest


def _write_ext(
    root: Path,
    *,
    dir_name: str = APP_ID,
    manifest: dict | None = None,
    raw_manifest: str | None = None,
) -> Path:
    """按协议 §2 落一个 `<root>/<microappId>/microapp.json`。"""
    app_dir = root / dir_name
    app_dir.mkdir(parents=True, exist_ok=True)
    if raw_manifest is not None:
        (app_dir / "microapp.json").write_text(raw_manifest, encoding="utf-8")
    else:
        payload = _manifest() if manifest is None else manifest
        (app_dir / "microapp.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8",
        )
    return app_dir


def _write_builtin(root: Path, app_id: str = "demo-app", version: str = VERSION) -> Path:
    """落一个最小内置微应用（`<root>/<appId>/<version>/manifest.json`），用于向后兼容断言。"""
    version_dir = root / app_id / version
    (version_dir / "app").mkdir(parents=True)
    (version_dir / "app" / "index.html").write_text("<!doctype html>", encoding="utf-8")
    (version_dir / "manifest.json").write_text(
        json.dumps({
            "appId": app_id,
            "name": "内置示例",
            "version": version,
            "description": "内置画布",
            "entry": "app/index.html",
            "methods": [{"name": "drawRect", "description": "画矩形"}],
            "permissions": [],
            "runtime": {"vendors": []},
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    return version_dir


# ───────────────────── 清单校验（纯函数）─────────────────────


def test_validate_accepts_well_formed_manifest():
    assert registry.validate_manifest(_manifest(), APP_ID) == []


def test_validate_accepts_pure_frontend_manifest_without_server():
    """没有 server 段 = 纯前端微应用（协议 §2），这是合法形态，不能报缺 command。"""
    assert registry.validate_manifest(_manifest(with_server=False), APP_ID) == []


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda m: m.pop("id"), "manifest.id 缺失"),
        (lambda m: m.update(id="Bad_ID"), "不符合正则"),
        (lambda m: m.pop("version"), "manifest.version"),
        (lambda m: m.update(version="v1"), "manifest.version"),
        (lambda m: m.update(frontend={}), "frontend.url 缺失"),
        (lambda m: m.update(frontend={"url": 123}), "frontend.url 缺失"),
        (lambda m: m.pop("frontend"), "manifest.frontend 缺失"),
        (lambda m: m.update(server={"autostart": True}), "server.command"),
        (lambda m: m.update(server="node server.cjs"), "manifest.server 必须是对象"),
    ],
)
def test_validate_collects_each_problem(mutate, expected):
    manifest = _manifest()
    mutate(manifest)
    problems = registry.validate_manifest(manifest, APP_ID)
    assert any(expected in item for item in problems), f"缺少 {expected}：{problems}"


def test_validate_reports_id_mismatch_with_directory():
    """id 与目录名不一致时「按 id 找得到」这件事就不成立，必须报出来。"""
    problems = registry.validate_manifest(_manifest(microapp_id="other-id"), APP_ID)
    assert any("与目录名" in item for item in problems), problems


def test_validate_reports_unparsable_manifest():
    problems = registry.validate_manifest(None, APP_ID)
    assert problems == ["清单无法解析为 JSON 对象"]


def test_validate_reports_every_problem_at_once():
    """一次报全：让用户改一轮就能过，而不是改一条、再校验、再发现下一条。"""
    broken = {
        "id": "other-id",
        "version": "abc",
        "frontend": {"url": "not-a-url"},
        "server": {},
        "capabilities": {"path": "no-slash", "methods": [{"description": "无名字"}]},
    }
    problems = registry.validate_manifest(broken, APP_ID)
    text = "；".join(problems)
    for expected in (
        "与目录名", "manifest.version", "frontend.url 不是合法地址",
        "server.command", "capabilities.path", "methods[0].name",
    ):
        assert expected in text, f"缺少问题项 {expected}：{text}"


# ───────────────────── 库扫描与查找 ─────────────────────


def test_root_env_is_honoured(ext_root: Path):
    assert registry.microapp_ext_root() == ext_root


def test_list_skips_invalid_and_dot_directories(ext_root: Path):
    """坏目录只记日志、不影响旁边的正常微应用；点目录不是微应用（协议 §2）。"""
    _write_ext(ext_root, dir_name=APP_ID)
    _write_ext(ext_root, dir_name="broken-app", manifest=_manifest("mismatch-id"))
    _write_ext(ext_root, dir_name="no-manifest")
    (ext_root / "no-manifest" / "microapp.json").unlink()
    _write_ext(ext_root, dir_name="bad-json", raw_manifest="{ 不是 JSON")
    (ext_root / ".tmp").mkdir()

    apps = registry.list_external_microapps(ext_root)
    assert [app.microapp_id for app in apps] == [APP_ID]


def test_list_missing_root_is_empty(ext_root: Path):
    assert not ext_root.exists()
    assert registry.list_external_microapps(ext_root) == []


def test_find_returns_valid_app_and_none_for_broken(ext_root: Path):
    _write_ext(ext_root)
    _write_ext(ext_root, dir_name="broken-app", manifest=_manifest("mismatch-id"))

    found = registry.find_external_microapp(APP_ID, ext_root)
    assert found is not None and found.microapp_id == APP_ID
    # 声明坏了就按未安装处理：不能带着半份声明去发 HTTP 请求
    assert registry.find_external_microapp("broken-app", ext_root) is None
    assert registry.find_external_microapp("no-such-app", ext_root) is None
    assert registry.find_external_microapp("..", ext_root) is None


def test_summary_exposes_protocol_fields(ext_root: Path):
    _write_ext(ext_root)
    app = registry.find_external_microapp(APP_ID, ext_root)
    assert app is not None
    summary = app.to_dict()
    assert summary["frontendUrl"] == "http://127.0.0.1:5174/"
    assert summary["methods"] == ["openProject", "listFiles"]
    assert summary["hasServer"] is True
    assert summary["autostart"] is True
    assert summary["problems"] == []


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://127.0.0.1:5174/", "http://127.0.0.1:5174"),
        ("http://127.0.0.1:5174/#/canvas", "http://127.0.0.1:5174"),
        ("https://example.com/app/index.html", "https://example.com"),
        ("not-a-url", ""),
    ],
)
def test_origin_of(url: str, expected: str):
    assert registry.origin_of(url) == expected


def test_call_url_uses_origin_and_capabilities_path(ext_root: Path):
    _write_ext(ext_root, manifest=_manifest(frontend_url="http://127.0.0.1:5174/#/canvas"))
    app = registry.find_external_microapp(APP_ID, ext_root)
    assert app is not None
    # 缺省前缀是 /__microapp（协议 §3），且不能把前端路由拼进调用地址
    assert registry.call_url(app) == "http://127.0.0.1:5174/__microapp/call"


def test_call_url_normalizes_capabilities_endpoint(ext_root: Path):
    """协议示例里 capabilities.path 写的是能力自述端点，调用端点要退回它的父前缀（§3.3）。"""
    manifest = _manifest()
    manifest["capabilities"]["path"] = "/__microapp/capabilities"
    _write_ext(ext_root, manifest=manifest)
    app = registry.find_external_microapp(APP_ID, ext_root)
    assert app is not None
    assert registry.call_url(app) == "http://127.0.0.1:5174/__microapp/call"


def test_call_url_honours_declared_prefix(ext_root: Path):
    """声明成前缀写法时原样使用。"""
    manifest = _manifest()
    manifest["capabilities"]["path"] = "/__microapp"
    _write_ext(ext_root, manifest=manifest)
    app = registry.find_external_microapp(APP_ID, ext_root)
    assert app is not None
    assert registry.call_url(app) == "http://127.0.0.1:5174/__microapp/call"


# ───────────────────── microapp_list 的外置条目 ─────────────────────


async def test_list_marks_external_entries(ext_root: Path, ctx: ToolContext):
    _write_ext(ext_root)
    result = await MicroAppListTool().run({}, ctx)
    assert result.ok is True
    assert result.data["count"] == 0          # 内置数量口径不变
    assert result.data["externalCount"] == 1
    entry = result.data["apps"][0]
    assert entry["external"] is True
    assert entry["appId"] == APP_ID
    assert entry["frontendUrl"] == "http://127.0.0.1:5174/"
    assert entry["methods"] == ["openProject", "listFiles"]
    assert "外置" in result.output


async def test_list_builtin_entries_keep_original_shape(roots: tuple[Path, Path], ctx: ToolContext):
    """内置条目的字段集合一个都不多：既有消费方按原形状解析（向后兼容）。"""
    builtin, ext_root = roots
    _write_builtin(builtin)
    _write_ext(ext_root)

    result = await MicroAppListTool().run({}, ctx)
    assert result.ok is True
    builtin_entry = next(item for item in result.data["apps"] if item["appId"] == "demo-app")
    assert set(builtin_entry) == {"appId", "name", "version", "description", "methods"}
    assert "external" not in builtin_entry
    assert "frontendUrl" not in builtin_entry
    # 外置条目在内置之后追加，且两个来源分别计数
    assert result.data["count"] == 1
    assert result.data["externalCount"] == 1
    assert result.data["totalCount"] == 2
    assert result.data["apps"][-1]["external"] is True


async def test_list_without_external_library_is_unchanged(roots: tuple[Path, Path], ctx: ToolContext):
    """外置库都不存在时，输出与改动前一致（只有内置那一段）。"""
    builtin, ext_root = roots
    _write_builtin(builtin)
    assert not ext_root.exists()

    result = await MicroAppListTool().run({}, ctx)
    assert result.ok is True
    assert result.data["apps"] == [{
        "appId": "demo-app", "name": "内置示例", "version": VERSION,
        "description": "内置画布", "methods": ["drawRect"],
    }]
    assert "外置" not in result.output


async def test_list_empty_libraries_still_ok(roots: tuple[Path, Path], ctx: ToolContext):
    """两个库都没有内容时仍是成功结果（首次使用必然如此）。"""
    result = await MicroAppListTool().run({}, ctx)
    assert result.ok is True
    assert result.data["apps"] == []
    assert result.data["count"] == 0
    assert result.data["externalCount"] == 0


# ───────────────────── microapp_call 的外置分支 ─────────────────────


async def test_call_rejects_undeclared_method(ext_root: Path, ctx: ToolContext):
    """协议 §2 末条：只允许调清单声明过的方法，未声明返回 method-not-declared。"""
    _write_ext(ext_root)
    result = await MicroAppCallTool().run(
        {"appId": APP_ID, "method": "deleteEverything"}, ctx,
    )
    assert result.ok is False
    assert result.data["error"] == "method-not-declared"
    assert "method-not-declared" in result.hint
    assert "openProject" in result.hint
    assert result.data["availableMethods"] == ["openProject", "listFiles"]
    assert result.data["external"] is True


async def test_call_external_posts_to_plugin_endpoint(
    ext_root: Path, ctx: ToolContext, monkeypatch: pytest.MonkeyPatch,
):
    """命中外置微应用时要 POST 到它自己的 /call，而不是走界面桥（协议 §3.3 / §6）。"""
    _write_ext(ext_root)
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({
            "url": str(request.url),
            "json": json.loads(request.content.decode("utf-8")),
            "method": request.method,
        })
        return httpx.Response(200, json={"ok": True, "result": {"opened": True}})

    real_call = registry.call_method

    async def patched(app, method, args=None, **kwargs):
        return await real_call(
            app, method, args, transport=httpx.MockTransport(handler), **kwargs,
        )

    monkeypatch.setattr(registry, "call_method", patched)
    # microapp.py 里按名字导入，需同步替换它那一侧的引用
    import app.orchestration.tools.microapp as microapp_module
    monkeypatch.setattr(microapp_module, "call_method", patched)

    result = await MicroAppCallTool().run(
        {"appId": APP_ID, "method": "openProject", "args": {"path": "D:\\proj"}}, ctx,
    )
    assert result.ok is True, result.error
    assert result.data["external"] is True
    assert result.data["result"] == {"opened": True}
    assert seen == [{
        "url": "http://127.0.0.1:5174/__microapp/call",
        "method": "POST",
        "json": {"method": "openProject", "args": {"path": "D:\\proj"}},
    }]


async def test_call_external_translates_business_failure(
    ext_root: Path, ctx: ToolContext, monkeypatch: pytest.MonkeyPatch,
):
    """插件的 {ok:false,error,message} 要原样翻译成工具失败，且不伪装成传输故障。"""
    _write_ext(ext_root)

    async def fake(app, method, args=None, **_kwargs):
        return {"ok": False, "error": "not-found", "message": "目录不存在"}

    import app.orchestration.tools.microapp as microapp_module
    monkeypatch.setattr(microapp_module, "call_method", fake)

    result = await MicroAppCallTool().run({"appId": APP_ID, "method": "openProject"}, ctx)
    assert result.ok is False
    assert result.error_code == "EXEC_FAILED"
    assert "not-found" in result.error and "目录不存在" in result.error
    assert result.data["error"] == "not-found"


async def test_call_external_unreachable_gives_readable_error(
    ext_root: Path, ctx: ToolContext, monkeypatch: pytest.MonkeyPatch,
):
    """连不上时给出「未启动或端口未就绪」这类可读原因，不要把 traceback 抛给模型。"""
    _write_ext(ext_root)
    real_call = registry.call_method

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    async def patched(app, method, args=None, **kwargs):
        return await real_call(
            app, method, args, transport=httpx.MockTransport(handler), **kwargs,
        )

    import app.orchestration.tools.microapp as microapp_module
    monkeypatch.setattr(microapp_module, "call_method", patched)

    result = await MicroAppCallTool().run({"appId": APP_ID, "method": "openProject"}, ctx)
    assert result.ok is False
    assert result.error_code == "EXEC_FAILED"
    assert "未启动或端口未就绪" in result.hint
    assert "Traceback" not in result.error


async def test_call_external_placeholder_port_is_reported(ext_root: Path, ctx: ToolContext):
    """frontend.url 还带 {port} 时说明端口没回填，本地直接报清楚，不发请求。"""
    _write_ext(ext_root, manifest=_manifest(frontend_url="http://127.0.0.1:{port}/"))
    result = await MicroAppCallTool().run({"appId": APP_ID, "method": "openProject"}, ctx)
    assert result.ok is False
    assert "{port}" in result.error
    assert "面板" in result.hint


async def test_dynamic_port_call_uses_desktop_owner(ext_root, ctx, monkeypatch):
    """plan-20261116-review-fixes：动态端口由主进程解析，后端不要求用户先开界面。"""
    from unittest.mock import AsyncMock
    from app.orchestration.browser.bridge import bridge
    from app.orchestration.tools import microapp as module

    _write_ext(ext_root, manifest=_manifest(frontend_url="http://127.0.0.1:{port}/"))
    request = AsyncMock(return_value={"ok": True, "result": {"opened": True}})
    direct = AsyncMock(side_effect=AssertionError("桌面宿主调用不得再补发 HTTP"))
    monkeypatch.setattr(bridge, "has_host", lambda: True)
    monkeypatch.setattr(bridge, "request", request)
    monkeypatch.setattr(module, "call_method", direct)
    result = await MicroAppCallTool().run({"appId": APP_ID, "method": "openProject", "args": {"path": "D:/proj"}}, ctx)
    assert result.ok is True
    assert result.data["result"] == {"opened": True}
    args, kwargs = request.await_args
    assert args[:3] == (ctx.session_id, "microappExternalCall", {"appId": APP_ID, "method": "openProject", "args": {"path": "D:/proj"}})
    assert args[3] >= 70
    assert kwargs["cancel_event"] is ctx.cancel_event
    direct.assert_not_awaited()


async def test_desktop_failure_does_not_repeat_external_side_effect(ext_root, ctx, monkeypatch):
    from unittest.mock import AsyncMock
    from app.orchestration.browser.bridge import HostCallError, bridge
    from app.orchestration.tools import microapp as module

    _write_ext(ext_root)
    request = AsyncMock(side_effect=HostCallError("调用结果未知"))
    direct = AsyncMock()
    monkeypatch.setattr(bridge, "has_host", lambda: True)
    monkeypatch.setattr(bridge, "request", request)
    monkeypatch.setattr(module, "call_method", direct)
    result = await MicroAppCallTool().run({"appId": APP_ID, "method": "openProject"}, ctx)
    assert not result.ok
    assert "结果未知" in result.error
    assert "避免重复执行" in result.hint
    direct.assert_not_awaited()


# ───────────────────── HTTP 路由 ─────────────────────


@pytest.fixture
def client(roots: tuple[Path, Path]) -> TestClient:
    app = FastAPI()
    app.include_router(microapps_ext_router.router, prefix="/api")
    return TestClient(app)


def test_router_lists_external_apps(ext_root: Path, client: TestClient):
    _write_ext(ext_root)
    _write_ext(ext_root, dir_name="broken-app", manifest=_manifest("mismatch-id"))

    response = client.get("/api/microapps-ext")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["apps"][0]["id"] == APP_ID
    assert body["apps"][0]["problems"] == []


def test_router_detail_includes_manifest_text(ext_root: Path, client: TestClient):
    _write_ext(ext_root)
    response = client.get(f"/api/microapps-ext/{APP_ID}")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["microappId"] == APP_ID
    assert json.loads(body["manifestText"])["id"] == APP_ID
    assert body["manifest"]["id"] == APP_ID


def test_router_detail_reports_problems_but_stays_200(ext_root: Path, client: TestClient):
    """声明不合格是「库里的现状」，不是请求失败：404 只留给「库里没有这个 id」。"""
    _write_ext(ext_root, manifest=_manifest("mismatch-id"))
    response = client.get(f"/api/microapps-ext/{APP_ID}")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert any("与目录名" in item for item in body["problems"])


def test_router_detail_unknown_id_is_404(ext_root: Path, client: TestClient):
    ext_root.mkdir(parents=True, exist_ok=True)
    assert client.get("/api/microapps-ext/no-such-app").status_code == 404


def test_router_detail_invalid_id_is_400(ext_root: Path, client: TestClient):
    ext_root.mkdir(parents=True, exist_ok=True)
    assert client.get("/api/microapps-ext/Bad_ID").status_code == 400


def test_router_validate_endpoint(ext_root: Path, client: TestClient):
    _write_ext(ext_root, manifest=_manifest("mismatch-id"))
    response = client.post(f"/api/microapps-ext/{APP_ID}/validate")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["issues"]
    assert any("与目录名" in item for item in body["issues"])


def test_router_validate_passes_for_valid_manifest(ext_root: Path, client: TestClient):
    _write_ext(ext_root)
    body = client.post(f"/api/microapps-ext/{APP_ID}/validate").json()
    assert body["ok"] is True
    assert body["issues"] == []


def test_router_validate_unknown_id_is_404(ext_root: Path, client: TestClient):
    ext_root.mkdir(parents=True, exist_ok=True)
    assert client.post("/api/microapps-ext/no-such-app/validate").status_code == 404


# ───────────────────── 健康探测 ─────────────────────


async def test_probe_health_ok(ext_root: Path):
    _write_ext(ext_root)
    app = registry.find_external_microapp(APP_ID, ext_root)
    assert app is not None
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"ok": True, "port": 5174})

    body = await registry.probe_health(app, transport=httpx.MockTransport(handler))
    assert body == {"ok": True, "port": 5174}
    assert seen == ["http://127.0.0.1:5174/__microapp/health"]


async def test_probe_health_unreachable_returns_none(ext_root: Path):
    _write_ext(ext_root)
    app = registry.find_external_microapp(APP_ID, ext_root)
    assert app is not None

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    assert await registry.probe_health(app, transport=httpx.MockTransport(handler)) is None
