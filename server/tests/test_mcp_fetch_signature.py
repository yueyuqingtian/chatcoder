"""MCP 握手调用链签名与占位符替换回归测试（plan-234-1171 R1）。

修复前症状：
- 前端点「刷新工具」→ `fetch_mcp_tools() missing 2 required positional arguments:
  'args' and 'env'`（skills_mcp.py 以单参调用四参函数）；
- 打开启用开关后工具清单永远为空（skill_service.py 同样单参调用，异常被 logger.debug 吞掉）；
- codegraph 握手即便签名正确也失败——args 中的 `${workspaceFolder}` 未替换，
  且 rootUri 未传，server 无法定位项目。

本测试以 monkeypatch 替身断言调用形态正确（不启动真实子进程）。
"""
import pytest

from app.orchestration import skill_scanner as ss


def test_resolve_workspace_placeholder_replaces_and_drops():
    """${workspaceFolder} 被替换为实际路径；无工作区上下文时该参数被剔除。"""
    assert ss.resolve_workspace_placeholder("${workspaceFolder}", "D:/proj") == "D:/proj"
    assert ss.resolve_workspace_placeholder("serve", "D:/proj") == "serve"
    # 无工作区 → 返回空串，由 resolve_workspace_args 过滤，避免传空参数破坏命令行
    assert ss.resolve_workspace_placeholder("${workspaceFolder}", None) == ""


def test_resolve_workspace_args_filters_empty():
    """args 逐项替换；因无工作区而变空的项被剔除，其余参数原样保留。"""
    args = ["serve", "--mcp", "--path", "${workspaceFolder}"]
    assert ss.resolve_workspace_args(args, "D:/proj") == ["serve", "--mcp", "--path", "D:/proj"]
    assert ss.resolve_workspace_args(args, None) == ["serve", "--mcp", "--path"]
    assert ss.resolve_workspace_args(None, "D:/proj") == []


@pytest.mark.asyncio
async def test_fetch_mcp_tools_accepts_four_positional_args(monkeypatch):
    """fetch_mcp_tools 必须以 (command, args, env, root_path) 调用成功（签名契约）。

    这是 skills_mcp / skill_service 两处调用点的共同契约；此前按单参调用会抛
    TypeError，被包成 400「MCP 握手失败」或被 logger.debug 静默吞掉。
    """
    captured: dict = {}

    async def _fake_handshake(proc, root_path):
        return [{"name": "t1", "description": "d"}]

    class _FakeProc:
        returncode = 0
        stdin = stdout = stderr = None

    async def _fake_exec(command, *args, **kwargs):
        captured["command"] = command
        captured["args"] = list(args)
        captured["env"] = kwargs.get("env") or {}
        return _FakeProc()

    async def _fake_terminate(proc):
        return None

    monkeypatch.setattr(ss.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(ss, "_fetch_mcp_tools_handshake", _fake_handshake)
    monkeypatch.setattr(ss, "_terminate_process_tree", _fake_terminate)

    tools = await ss.fetch_mcp_tools(
        "codegraph",
        ["serve", "--mcp", "--path", "${workspaceFolder}"],
        {"FOO": "bar"},
        root_path="D:/myProject/chatcoder",
    )

    assert tools == [{"name": "t1", "description": "d"}]
    assert captured["command"] == "codegraph"
    # 关键断言：占位符在 spawn 前已替换（修复前子进程拿到字面量 ${workspaceFolder}）
    assert captured["args"] == ["serve", "--mcp", "--path", "D:/myProject/chatcoder"]
    assert "${workspaceFolder}" not in captured["args"]


@pytest.mark.asyncio
async def test_fetch_mcp_tools_empty_command_no_process(monkeypatch):
    """空 command 直接返回空列表，不启动子进程（sse 类型 server 走此分支）。"""
    called = False

    async def _fake_exec(*_a, **_k):
        nonlocal called
        called = True
        raise AssertionError("不应启动子进程")

    monkeypatch.setattr(ss.asyncio, "create_subprocess_exec", _fake_exec)
    assert await ss.fetch_mcp_tools("", [], {}) == []
    assert called is False


@pytest.mark.asyncio
async def test_refresh_endpoint_calls_fetch_with_expanded_args(monkeypatch):
    """刷新端点必须展开为多参调用（回归：单参调用会抛 TypeError 包成 400）。"""
    from app.gateway.routers import skills_mcp

    captured: dict = {}

    async def _fake_fetch(command, args, env, root_path=None):
        captured.update(command=command, args=args, env=env, root_path=root_path)
        return [{"name": "t1"}]

    class _Srv:
        id = 1
        name = "codegraph"
        display_name = "codegraph"
        description = ""
        source = "cursor"
        command = "codegraph"
        args = ["serve", "--mcp", "--path", "${workspaceFolder}"]
        env = {"K": "V"}
        url = None
        tools = None
        is_active = False
        transport = "stdio"

    async def _fake_get(_db, _sid):
        return _Srv()

    # 端点内部 `from app.orchestration.skill_scanner import fetch_mcp_tools`，
    # 故需 patch 源模块属性。
    monkeypatch.setattr(ss, "fetch_mcp_tools", _fake_fetch)
    monkeypatch.setattr(skills_mcp.skill_service, "get_mcp_server", _fake_get)

    written: dict = {}

    async def _fake_run_write_locked(fn, label=None):
        return fn(_FakeSession())

    class _FakeSession:
        def get(self, _model, _sid):
            return _Srv()

        def commit(self):
            return None

    monkeypatch.setattr(
        "app.persistence.database.run_write_locked", _fake_run_write_locked,
    )

    result = await skills_mcp.refresh_mcp_tools(
        server_id=1, workspace="D:/myProject/chatcoder", db=object(),
    )

    assert result["ok"] is True
    assert result["count"] == 1
    # 关键断言：workspace 透传为 root_path，供 rootUri + 占位符替换使用
    assert captured["command"] == "codegraph"
    assert captured["args"] == ["serve", "--mcp", "--path", "${workspaceFolder}"]
    assert captured["root_path"] == "D:/myProject/chatcoder"
    del written


@pytest.mark.asyncio
async def test_update_mcp_server_fetches_tools_with_four_args(monkeypatch):
    """启用开关路径：update_mcp_server 必须四参调用并接受 workspace 透传。"""
    from app.services import skill_service

    captured: dict = {}

    async def _fake_fetch(command, args, env, root_path=None):
        captured.update(command=command, args=args, root_path=root_path)
        return [{"name": "t1"}]

    class _Srv:
        id = 1
        name = "codegraph"
        command = "codegraph"
        args = ["serve"]
        env = {}
        tools = None
        is_active = True
        transport = "stdio"

    async def _fake_get(_db, _sid):
        return _Srv()

    class _FakeSession:
        def get(self, _model, _sid):
            return _Srv()

        def commit(self):
            return None

    async def _fake_run_write_locked(fn, label=None):
        return fn(_FakeSession())

    monkeypatch.setattr(ss, "fetch_mcp_tools", _fake_fetch)
    monkeypatch.setattr(skill_service, "get_mcp_server", _fake_get)
    monkeypatch.setattr(
        "app.persistence.database.run_write_locked", _fake_run_write_locked,
    )

    ok = await skill_service.update_mcp_server(
        object(), 1, is_active=True, workspace="D:/proj",
    )

    assert ok is True
    assert captured["command"] == "codegraph"
    # 修复前此处抛 TypeError（单参调用），异常被吞导致 tools 永远为空
    assert captured["root_path"] == "D:/proj"
