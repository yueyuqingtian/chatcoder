"""plan-308-1542 需求7-A：断点枚举 / 删除 / 清空（右侧面板可视化后端）。

用假会话对象注入 debug_service._sessions，避免真实连接 CDP/JDWP——
本用例只验证"断点列表与删除逻辑"这一层（面板数据源）。
"""
import pytest

from app.services import debug_service


class _FakeWebSession:
    def __init__(self):
        self.breakpoints = {"https://a.com/app.js:10": "bp-1",
                            "https://a.com/app.js:20": "bp-2"}
        self.removed: list[tuple[str, int]] = []

    async def remove_breakpoint(self, url_regex: str, line: int) -> bool:
        self.removed.append((url_regex, line))
        self.breakpoints.pop(f"{url_regex}:{line}", None)
        return True


class _FakeJdwpSession:
    def __init__(self):
        self.cleared: list[int] = []

    def clear_breakpoint(self, request_id: int) -> None:
        self.cleared.append(request_id)


@pytest.fixture
def clean_sessions():
    """保存并清空全局会话表（避免污染其它用例）。"""
    saved = dict(debug_service._sessions)
    saved_meta = dict(debug_service._meta)
    debug_service._sessions.clear()
    debug_service._meta.clear()
    yield
    debug_service._sessions.clear()
    debug_service._meta.clear()
    debug_service._sessions.update(saved)
    debug_service._meta.update(saved_meta)


def test_web_breakpoint_list_and_status(clean_sessions):
    sid = 991
    sess = _FakeWebSession()
    debug_service._sessions[(sid, "web")] = sess
    debug_service._meta[(sid, "web")] = {"paused": False}

    st = debug_service.status(sid, "web")
    assert st["connected"] is True
    assert st["breakpoints"] == 2
    rows = st["breakpointList"]
    assert len(rows) == 2
    assert {r["id"] for r in rows} == {"bp-1", "bp-2"}
    assert all(r["target"] == "web" and r["source"] == "app" for r in rows)
    assert {r["line"] for r in rows} == {10, 20}


def test_web_remove_breakpoint_by_id(clean_sessions):
    sid = 992
    sess = _FakeWebSession()
    debug_service._sessions[(sid, "web")] = sess
    debug_service._meta[(sid, "web")] = {}

    import asyncio
    out = asyncio.run(debug_service.remove_breakpoint(sid, "web", "bp-1"))
    assert out["ok"] is True
    assert sess.removed == [("https://a.com/app.js", 10)]
    # 列表随之减少
    assert len(debug_service.list_breakpoints(sid, "web")["breakpoints"]) == 1

    # 不存在 id → 可读错误
    out2 = asyncio.run(debug_service.remove_breakpoint(sid, "web", "nope"))
    assert out2["ok"] is False and "不存在" in out2["error"]


def test_java_breakpoint_list_remove_clear(clean_sessions):
    sid = 993
    sess = _FakeJdwpSession()
    debug_service._sessions[(sid, "java")] = sess
    debug_service._meta[(sid, "java")] = {
        "breakpoints": {"com.example.A:42": 1001, "com.example.B:7": 1002},
        "bp_files": {"com.example.A:42": "src/A.java", "com.example.B:7": "src/B.java"},
    }

    rows = debug_service.list_breakpoints(sid, "java")["breakpoints"]
    assert {r["id"] for r in rows} == {"1001", "1002"}
    a = next(r for r in rows if r["id"] == "1001")
    assert a["class"] == "com.example.A" and a["line"] == 42
    assert a["file"] == "src/A.java"  # 反推源码路径（面板可点定位）

    import asyncio
    out = asyncio.run(debug_service.remove_breakpoint(sid, "java", "1001"))
    assert out["ok"] is True
    assert sess.cleared == [1001]
    assert "com.example.A:42" not in debug_service._meta[(sid, "java")]["breakpoints"]

    out2 = asyncio.run(debug_service.clear_breakpoints(sid, "java"))
    assert out2["ok"] is True
    assert sess.cleared == [1001, 1002]
    assert debug_service._meta[(sid, "java")]["breakpoints"] == {}


def test_breakpoints_when_not_connected(clean_sessions):
    """未连接会话：返回空列表而非报错（面板可正常渲染空态）。"""
    out = debug_service.list_breakpoints(994, "web")
    assert out["ok"] is True and out["connected"] is False and out["breakpoints"] == []
