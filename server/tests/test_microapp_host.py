"""宿主桥 server 层单测（plan-693-2868 · docs/app1/11-host-bridge-protocol.md）。

只覆盖**纯逻辑**与**边界判定**：聚批窗口（§5）、审计字段组装（§2）、
fs 工作区边界（§4）、op 分派与回执配对（§4）、以及 §7 那句粗过滤真的放行了 microapp.*。

不起真实进程、不连数据库：`exec.start` 的进程能力复用 `bg_process_registry`，
它自己的用例在 `test_bg_task_lifecycle.py` / `test_terminal_web.py`，这里只验证
「页面请求 → 正确的 op 分派 → 正确的回执」这一层。

**例外**：协议 v3 的 `exec.stdin`（`docs/app1/12-capability-boundaries.md` §4.1）必须
用真进程测——它的全部意义就是「往进程 stdin 里写字节」，假进程测不出管道有没有保留。
那几个用例在文件末尾单独一段，进程用 `python <脚本>` 起，跑完都 kill 干净。
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from app.orchestration import microapp_host as host
from app.orchestration.tools.bg_process import bg_process_registry

# ───────────────────────── 聚批（契约 §5）─────────────────────────


def test_batcher_flushes_at_200_lines():
    """满 200 行立刻发一批——不必等 100ms（先到者赢）。"""
    b = host.LineBatcher()
    text = "".join(f"line{i}\n" for i in range(250))
    batches = b.feed(text, now=0.0)
    assert len(batches) == 1
    assert len(batches[0]) == 200
    assert batches[0][0] == "line0" and batches[0][-1] == "line199"
    # 余下 50 行仍在缓冲里，尚未到期
    assert b.feed("", now=0.01) == []


def test_batcher_flushes_after_100ms_with_few_lines():
    b = host.LineBatcher()
    assert b.feed("a\nb\n", now=0.0) == []           # 2 行 + 未到 100ms：先攒着
    batches = b.feed("c\n", now=0.11)                # 到点：三行一起发
    assert batches == [["a", "b", "c"]]


def test_batcher_keeps_partial_line_until_completed():
    """增量可能在某一行中间被切断：半行不能先发出去（页面会看到被劈开的行）。"""
    b = host.LineBatcher(interval_s=0.1)
    assert b.feed("partial", now=0.0) == []
    # 补齐后该行才成为「一整行」，聚批窗口从它到齐的那一刻开始算
    assert b.feed(" tail\n", now=0.2) == []
    assert b.feed("", now=0.31) == [["partial tail"]]


def test_batcher_strips_windows_carriage_return():
    """Windows 上命令输出是 CRLF：行尾 \\r 原样推给页面会渲染成多余回车。"""
    b = host.LineBatcher()
    b.feed("a\r\nb\r\n", now=0.0)
    assert b.flush() == ["a", "b"]


def test_batcher_flush_returns_trailing_partial_and_rest():
    b = host.LineBatcher()
    b.feed("a\nb", now=0.0)
    assert b.flush() == ["a", "b"]  # 进程退出：未完成的半行也要交出去，不能丢最后一行
    assert b.flush() == []


def test_batcher_empty_feed_still_triggers_time_flush():
    """进程安静下来时，攒着的行仍要按时发，不能等「下一行到来」才发。"""
    b = host.LineBatcher()
    b.feed("x\n", now=0.0)
    assert b.feed("", now=0.05) == []
    assert b.feed("", now=0.12) == [["x"]]


# ───────────────────────── 审计（契约 §2）─────────────────────────


def test_audit_record_has_contract_fields():
    rec = host.build_audit_record(
        app_id="java-devtools", version="1.0.0", uid="u-1", session_id=7,
        op="exec.start", args={"command": "java -version"}, ok=True, duration_ms=12,
    )
    assert set(rec) == {
        "ts", "appId", "version", "uid", "sessionId", "op",
        "argsDigest", "ok", "durationMs",
    }  # error 只在失败时出现
    assert rec["appId"] == "java-devtools" and rec["sessionId"] == 7
    assert rec["argsDigest"] == "java -version"
    assert rec["ok"] is True
    assert rec["ts"].startswith("20")  # ISO8601


def test_audit_digest_truncated_and_prefers_command_then_path():
    long_cmd = "x" * 500
    rec = host.build_audit_record(
        app_id="a", version="1", uid="u", session_id=1, op="exec.start",
        args={"command": long_cmd}, ok=True, duration_ms=1,
    )
    assert len(rec["argsDigest"]) == host.AUDIT_DIGEST_CHARS
    path_rec = host.build_audit_record(
        app_id="a", version="1", uid="u", session_id=1, op="fs.read",
        args={"path": "src/app.ts"}, ok=True, duration_ms=1,
    )
    assert path_rec["argsDigest"] == "src/app.ts"
    # 空参数（workspace.info）不硬塞无意义内容
    empty_rec = host.build_audit_record(
        app_id="a", version="1", uid="u", session_id=1, op="workspace.info",
        args={}, ok=True, duration_ms=1,
    )
    assert empty_rec["argsDigest"] == ""


def test_audit_record_carries_error_only_on_failure():
    rec = host.build_audit_record(
        app_id="a", version="1", uid="u", session_id=1, op="fs.read",
        args={"path": "x"}, ok=False, duration_ms=3, error=host.ERR_FS_OUTSIDE,
    )
    assert rec["error"] == "microapp-host-fs-outside-workspace"
    assert rec["ok"] is False


async def test_audit_is_appended_as_json_lines(tmp_path, monkeypatch):
    """落盘时机：请求处理完（成功/失败都写），JSON Lines 追加，路径可由环境变量改道。"""
    log = tmp_path / "microapp-host.log"
    monkeypatch.setenv(host.AUDIT_LOG_ENV, str(log))
    await host.write_audit(host.build_audit_record(
        app_id="a", version="1", uid="u", session_id=1, op="workspace.info",
        args={}, ok=True, duration_ms=1,
    ))
    await host.write_audit(host.build_audit_record(
        app_id="a", version="1", uid="u", session_id=1, op="fs.read",
        args={"path": "x"}, ok=False, duration_ms=2, error=host.ERR_FS_OUTSIDE,
    ))
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["op"] == "workspace.info"
    assert json.loads(lines[1])["error"] == "microapp-host-fs-outside-workspace"


async def test_audit_write_failure_does_not_raise(tmp_path, monkeypatch):
    """审计写不进去（目录非法）只告警——不能反过来挡住页面操作。"""
    monkeypatch.setenv(host.AUDIT_LOG_ENV, str(tmp_path / "file-not-dir" / "x.log"))
    (tmp_path / "file-not-dir").write_text("i am a file", encoding="utf-8")
    await host.write_audit({"op": "fs.read"})  # 不抛即通过


# ───────────────────────── fs 工作区边界（契约 §4）─────────────────────────


def test_fs_read_returns_content(tmp_path):
    (tmp_path / "a.txt").write_text("hello 世界", encoding="utf-8")
    out = host.fs_read(str(tmp_path), {"path": "a.txt"})
    assert out["content"] == "hello 世界"
    assert out["truncated"] is False


def test_fs_read_truncates_large_file(tmp_path):
    big = tmp_path / "big.txt"
    big.write_bytes(b"x" * (host.FS_READ_MAX_BYTES + 10))
    out = host.fs_read(str(tmp_path), {"path": "big.txt"})
    assert out["truncated"] is True
    assert len(out["content"]) == host.FS_READ_MAX_BYTES


@pytest.mark.parametrize("bad", ["../outside.txt", "..", "/etc/passwd", "sub/../../outside.txt"])
def test_fs_read_outside_workspace_is_rejected(tmp_path, bad):
    """越界必须拒绝且错误码是契约定的 microapp-host-fs-outside-workspace。"""
    (tmp_path / "sub").mkdir()
    with pytest.raises(host.HostError) as exc:
        host.fs_read(str(tmp_path), {"path": bad})
    assert exc.value.code == host.ERR_FS_OUTSIDE


def test_fs_read_missing_file_is_not_found(tmp_path):
    with pytest.raises(host.HostError) as exc:
        host.fs_read(str(tmp_path), {"path": "nope.txt"})
    assert exc.value.code == host.ERR_FS_NOT_FOUND


def test_fs_list_lists_entries_with_type_and_size(tmp_path):
    (tmp_path / "dir1").mkdir()
    (tmp_path / "b.txt").write_text("12345", encoding="utf-8")
    out = host.fs_list(str(tmp_path), {"path": "."})
    names = [e["name"] for e in out["entries"]]
    assert names == ["dir1", "b.txt"]  # 目录在前、同类按名字排（与文件面板同序）
    by_name = {e["name"]: e for e in out["entries"]}
    assert by_name["dir1"]["type"] == "dir"
    assert by_name["b.txt"]["type"] == "file"
    assert by_name["b.txt"]["size"] == 5
    assert out["truncated"] is False


def test_fs_list_outside_workspace_is_rejected(tmp_path):
    with pytest.raises(host.HostError) as exc:
        host.fs_list(str(tmp_path), {"path": ".."})
    assert exc.value.code == host.ERR_FS_OUTSIDE


def test_fs_list_missing_dir_is_not_found(tmp_path):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    with pytest.raises(host.HostError) as exc:
        host.fs_list(str(tmp_path), {"path": "a.txt"})
    assert exc.value.code == host.ERR_FS_NOT_FOUND


# ───────────────────────── op 分派与回执（契约 §4）─────────────────────────


def _sent() -> list[dict]:
    return []


async def _call(payload: dict, monkeypatch: pytest.MonkeyPatch, root: str) -> list[dict]:
    """跑一次 handle_request，返回它发出的事件（回执 + 可能的推送）。"""
    async def fake_workspace(_sid: int) -> str:
        return root

    monkeypatch.setattr(host, "session_workspace", fake_workspace)
    out: list[dict] = []
    await host.handle_request(payload, out.append)
    return out


async def test_workspace_info_returns_root_and_session(tmp_path, monkeypatch):
    (tmp_path / "w").mkdir()
    events = await _call(
        {"reqId": "req-1", "uid": "u1", "appId": "a", "version": "1.0.0",
         "sessionId": 42, "op": "workspace.info", "args": {}},
        monkeypatch, str(tmp_path / "w"),
    )
    assert events[0]["event"] == host.EVENT_RESPONSE
    payload = events[0]["payload"]
    assert payload["reqId"] == "req-1" and payload["ok"] is True
    assert payload["result"] == {"root": str(tmp_path / "w"), "sessionId": 42}


async def test_unknown_op_is_replying_not_silent(tmp_path, monkeypatch):
    """未知 op 也要回一条带 reqId 的失败——页面在等回包，静默丢弃会表现为「点了没反应」。"""
    events = await _call(
        {"reqId": "req-2", "uid": "u1", "appId": "a", "version": "1",
         "sessionId": 1, "op": "proc.nuke", "args": {}},
        monkeypatch, str(tmp_path),
    )
    payload = events[0]["payload"]
    assert payload["reqId"] == "req-2" and payload["ok"] is False
    assert payload["reason"] == host.ERR_UNKNOWN_OP
    assert payload["message"].startswith(host.ERR_UNKNOWN_OP)


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"uid": "u", "sessionId": 1, "op": "workspace.info", "args": {}}, host.ERR_BAD_REQUEST),
        ({"reqId": "r", "sessionId": 1, "args": {}}, host.ERR_BAD_REQUEST),
        ({"reqId": "r", "op": "workspace.info", "args": {}}, host.ERR_BAD_REQUEST),
    ],
)
async def test_malformed_request_replies_with_reason(payload, reason, tmp_path, monkeypatch):
    events = await _call(payload, monkeypatch, str(tmp_path))
    assert events[0]["payload"]["ok"] is False
    assert events[0]["payload"]["reason"] == reason


async def test_fs_read_via_dispatch_uses_session_workspace(tmp_path, monkeypatch):
    (tmp_path / "f.txt").write_text("ok", encoding="utf-8")
    events = await _call(
        {"reqId": "r", "uid": "u", "appId": "a", "version": "1",
         "sessionId": 9, "op": "fs.read", "args": {"path": "f.txt"}},
        monkeypatch, str(tmp_path),
    )
    assert events[0]["payload"]["result"] == {"content": "ok", "truncated": False}


async def test_exec_status_unknown_stream_replies_not_found(tmp_path, monkeypatch):
    events = await _call(
        {"reqId": "r", "uid": "u", "appId": "a", "version": "1",
         "sessionId": 9, "op": "exec.status", "args": {"streamId": "bg_nope"}},
        monkeypatch, str(tmp_path),
    )
    assert events[0]["payload"]["reason"] == host.ERR_STREAM_NOT_FOUND


# ───────────────────── 全局通道粗过滤（契约 §7）─────────────────────


def test_global_channel_filter_lets_microapp_through():
    """§7 的关键一行：`microapp.*` 上行必须过粗过滤，否则请求被静默丢弃且无日志。

    直接断言源码里的过滤条件——这条规则本身就是「少了半句就静默失效」，用行为测
    需要起一个真 WS 才能覆盖，而少了半句的症状恰恰是「没有任何日志可查」，
    所以这里宁可做一次源码守卫（改回 `if "browser." not in raw` 会立刻失败）。
    """
    from pathlib import Path

    src = Path(host.__file__).resolve().parents[1] / "gateway" / "ws.py"
    text = src.read_text(encoding="utf-8")
    assert '"browser." not in raw and "microapp." not in raw' in text
    # 且确实分派给了宿主桥
    assert "microapp_host.spawn_request(payload" in text


# ─────────────────── 协议 v3：exec.stdin / host.capabilities ───────────────────
# 设计文档 docs/app1/12-capability-boundaries.md §4、§9 验证清单。
# 与上面「不起真实进程」的分工不同：`exec.stdin` 的全部意义就是「往真进程里写字节」，
# 用假进程测等于没测。被测进程写成 python 脚本而不是 `cat` / `python -c`：Windows 上
# 命令要经 PowerShell 解析，`-c` 里的引号会被吃掉一层，脚本文件是最稳的写法。


def _payload(req_id: str, op: str, args: dict, session_id: int = 9) -> dict:
    """组装一条契约形状的请求（`{ reqId, uid, appId, version, sessionId, op, args }`）。"""
    return {"reqId": req_id, "uid": "u1", "appId": "a", "version": "1.0.0",
            "sessionId": session_id, "op": op, "args": args}


def _response(events: list[dict], req_id: str) -> dict:
    """按 reqId 取回执。

    `exec.start` 期间推流任务可能先发出 `stream.state running`，所以不能假设
    `events[0]` 就是回执（这正是真进程用例与纯逻辑用例的差别）。
    """
    for event in events:
        payload = event.get("payload") or {}
        if event.get("event") == host.EVENT_RESPONSE and payload.get("reqId") == req_id:
            return payload
    raise AssertionError(f"没有收到 reqId={req_id} 的回执：{events}")


def _write_echo_script(tmp_path) -> str:
    """被测控制器进程：读一行回一行，读到 EOF 后打印 eof 并正常退出（跨平台）。"""
    script = tmp_path / "echo_stdin.py"
    script.write_text(
        "import sys\n"
        "for line in iter(sys.stdin.readline, ''):\n"
        "    sys.stdout.write('got:' + line)\n"
        "    sys.stdout.flush()\n"
        "sys.stdout.write('eof\\n')\n"
        "sys.stdout.flush()\n",
        encoding="utf-8",
    )
    return script.name


async def _until(predicate, timeout: float = 20.0) -> bool:
    """轮询等待条件成立：真进程启动/退出有几百毫秒量级抖动，不能用固定 sleep。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(0.05)
    return False


async def _read_all_log(stream_id: str) -> str:
    """读该流当前的全量缓冲（页面收到的 stream.log 就来自这里）。"""
    snap = bg_process_registry.poll(stream_id, 0)
    return "" if snap is None else str(snap["log"])


async def _log_has(stream_id: str, needle: str) -> bool:
    return needle in await _read_all_log(stream_id)


async def _exited(stream_id: str) -> bool:
    info = bg_process_registry.status(stream_id, 0)
    return info is None or not info["running"]


async def _start_echo(tmp_path, monkeypatch, events: list[dict],
                      session_id: int = 9) -> str:
    """经 `exec.start` 起一个回显进程，返回 streamId。

    顺带断言 `result.stdin is True`：v3 要把能力点回报给页面，否则页面得先调一次
    `host.capabilities` 才知道能不能对这条流写 stdin。
    """
    async def fake_workspace(_sid: int) -> str:
        return str(tmp_path)

    monkeypatch.setattr(host, "session_workspace", fake_workspace)
    command = f"python {_write_echo_script(tmp_path)}"
    await host.handle_request(
        _payload("start", "exec.start", {"command": command}, session_id), events.append,
    )
    started = _response(events, "start")
    assert started["ok"] is True, started
    assert started["result"]["stdin"] is True
    return started["result"]["streamId"]


async def test_exec_stdin_writes_line_that_echoes_back(tmp_path, monkeypatch):
    """§9 第 1 条：`exec.stdin` 写进去的行，进程回显后能从流里读到。"""
    events: list[dict] = []
    stream_id = await _start_echo(tmp_path, monkeypatch, events)
    try:
        await host.handle_request(
            _payload("w1", "exec.stdin", {"streamId": stream_id, "data": "hello\n"}),
            events.append,
        )
        written = _response(events, "w1")
        assert written["ok"] is True, written
        # written 是字符数（不是字节数）：页面按它做进度提示时不必自己数
        assert written["result"] == {"ok": True, "written": 6}
        assert await _until(lambda: _log_has(stream_id, "got:hello")), \
            f"没读到回显，当前日志：{await _read_all_log(stream_id)!r}"

        # 连写两行都要按顺序到达（JSON 行协议就靠这个）
        await host.handle_request(
            _payload("w2", "exec.stdin", {"streamId": stream_id, "data": "second\n"}),
            events.append,
        )
        assert await _until(lambda: _log_has(stream_id, "got:second")), \
            f"第二行没读到，当前日志：{await _read_all_log(stream_id)!r}"
    finally:
        await bg_process_registry.kill(stream_id, by="system")


async def test_exec_stdin_eof_lets_process_exit_cleanly(tmp_path, monkeypatch):
    """§9 第 2 条：`eof: true` 关掉 stdin，进程读到 EOF 自行退出（退出码 0）。"""
    events: list[dict] = []
    stream_id = await _start_echo(tmp_path, monkeypatch, events)
    try:
        await host.handle_request(
            _payload("w1", "exec.stdin", {"streamId": stream_id, "data": "bye\n"}),
            events.append,
        )
        assert await _until(lambda: _log_has(stream_id, "got:bye"))

        await host.handle_request(
            _payload("eof", "exec.stdin", {"streamId": stream_id, "eof": True}),
            events.append,
        )
        closed = _response(events, "eof")
        assert closed["ok"] is True, closed
        assert closed["result"] == {"ok": True, "written": 0}  # 只关流、没数据

        assert await _until(lambda: _exited(stream_id)), "进程没有因为 EOF 退出"
        info = bg_process_registry.status(stream_id, 0)
        assert info is not None and info["returncode"] == 0, info
        # 走完了正常路径：脚本在 EOF 之后才打印 eof 退出
        assert "eof" in await _read_all_log(stream_id)
    finally:
        await bg_process_registry.kill(stream_id, by="system")


async def test_exec_stdin_after_exit_is_stdin_closed(tmp_path, monkeypatch):
    """§9 第 3 条：对已退出的 streamId 写 stdin → `microapp-host-stdin-closed`。"""
    events: list[dict] = []
    stream_id = await _start_echo(tmp_path, monkeypatch, events)
    # 先 EOF 让它干净退出，再往这条已死的流里写
    await host.handle_request(
        _payload("eof", "exec.stdin", {"streamId": stream_id, "eof": True}), events.append,
    )
    assert await _until(lambda: _exited(stream_id)), "进程没有退出"

    await host.handle_request(
        _payload("late", "exec.stdin", {"streamId": stream_id, "data": "too late\n"}),
        events.append,
    )
    late = _response(events, "late")
    assert late["ok"] is False
    assert late["reason"] == host.ERR_STDIN_CLOSED
    assert late["message"].startswith("microapp-host-stdin-closed")


async def test_exec_stdin_cross_session_is_stream_unknown(tmp_path, monkeypatch):
    """跨会话隔离：别的会话的 streamId 一律 `microapp-host-stream-unknown`。

    与 `exec.stop` / `exec.status` 复用同一套 `bg_process_registry.owner_session()` 校验；
    错误码按 v3 契约统一成 stream-unknown（「不存在」与「不是你的」对外不可区分）。
    """
    events: list[dict] = []
    stream_id = await _start_echo(tmp_path, monkeypatch, events, session_id=9)
    try:
        # 同一 streamId、换成会话 77 来写：必须被拒
        await host.handle_request(
            _payload("x", "exec.stdin", {"streamId": stream_id, "data": "hijack\n"},
                     session_id=77),
            events.append,
        )
        denied = _response(events, "x")
        assert denied["ok"] is False
        assert denied["reason"] == host.ERR_STREAM_UNKNOWN
        # 真的没写进去：进程侧不该出现 hijack
        assert "hijack" not in await _read_all_log(stream_id)
    finally:
        await bg_process_registry.kill(stream_id, by="system")

    # 完全不存在的 streamId 报同一个码
    await host.handle_request(
        _payload("y", "exec.stdin", {"streamId": "bg_deadbeef", "data": "x\n"}), events.append,
    )
    assert _response(events, "y")["reason"] == host.ERR_STREAM_UNKNOWN


async def test_host_capabilities_shape_and_ops_come_from_dispatch_table(tmp_path, monkeypatch):
    """§4.2：形状固定，且 `ops` 必须由分派表生成（写死清单迟早与实现漂移）。"""
    from app.orchestration.tools.microapp import MICROAPP_RESULT_MAX_CHARS

    events: list[dict] = []
    await host.handle_request(_payload("cap", "host.capabilities", {}), events.append)
    cap = _response(events, "cap")
    assert cap["ok"] is True, cap
    result = cap["result"]

    assert result["protocol"] == host.PROTOCOL_VERSION == 3
    # ops 与分派表等同（双向包含）：漏登记会「调不到」，这里也不会冒出幽灵 op
    assert sorted(result["ops"]) == sorted(host.OP_HANDLERS)
    # v3 新增的两个 op 必须在表里
    assert {"exec.stdin", "host.capabilities"} <= set(result["ops"])
    # 老 op 一个都不能少（v1/v2 的页面还在用）
    assert {"exec.start", "exec.stop", "exec.status",
            "fs.read", "fs.list", "workspace.info"} <= set(result["ops"])

    assert result["features"] == {"stdin": True, "env": False, "net": False}
    assert result["limits"]["maxConcurrentExec"] is None
    # 4000 是设计文档 §4.2 定死的数字，与 AI 侧 microapp_call 的内联上限同一口径
    assert result["limits"]["maxOutputCharsPerCall"] == 4000
    assert MICROAPP_RESULT_MAX_CHARS == 4000


async def test_v3_ops_are_audited_through_the_same_path(tmp_path, monkeypatch):
    """新 op 走 `handle_request` 原来的 finally 审计（不另开路径，失败也要留痕）。"""
    log = tmp_path / "microapp-host.log"
    monkeypatch.setenv(host.AUDIT_LOG_ENV, str(log))
    events: list[dict] = []
    await host.handle_request(_payload("a1", "host.capabilities", {}), events.append)
    # 失败的新 op 同样要留痕；stdin 的 data 不进摘要（只留 streamId）
    await host.handle_request(
        _payload("a2", "exec.stdin", {"streamId": "bg_deadbeef", "data": "secret\n"}),
        events.append,
    )
    text = log.read_text(encoding="utf-8")
    records = [json.loads(line) for line in text.strip().splitlines()]
    assert [r["op"] for r in records] == ["host.capabilities", "exec.stdin"]
    assert records[0]["ok"] is True
    assert records[1]["ok"] is False and records[1]["error"] == host.ERR_STREAM_UNKNOWN
    assert records[1]["argsDigest"] == "bg_deadbeef"
    assert "secret" not in text
