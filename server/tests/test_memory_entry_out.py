"""记忆列表响应模型的空值兼容（回归）。

**为什么需要这个文件**：`memory_entries.session_id` 自 plan-537-2256 起允许为空
（来源会话被删除时外键 SET NULL，项目/全局记忆继续保留），但 `MemoryEntryOut`
曾把它声明为必填 `int`。设置中心「记忆」页打开时 `GET /api/memories` 会对整批
记录做响应校验，只要库里存在一条来源会话已删除的记忆就整体 500
（现象：页面顶部一块 `4 validation errors ... ('response', 0, 'session_id')`）。

这里同时锁住两侧：DB 列可空 + 响应模型接受 None，避免以后再次漂移。
"""
from pydantic import TypeAdapter

from app.gateway.schemas import MemoryEntryOut
from app.persistence.models import MemoryEntry


def test_session_id_column_is_nullable():
    """DB 列可空是前提（外键 SET NULL 才能生效），漂移成 NOT NULL 会先在这里失败。"""
    columns = MemoryEntry.__table__.c
    assert columns["session_id"].nullable is True


def test_accepts_null_session_id_in_list():
    """来源会话已删除的项目/全局记忆（session_id=None）必须能通过列表响应校验。"""
    rows = [
        {
            "id": 1,
            "session_id": None,
            "text": "项目约定：提交前跑 typecheck",
            "kind": "convention",
            "scope": "project",
            "project_id": 3,
        },
        {
            "id": 2,
            "session_id": 42,
            "text": "会话事实：本机代理在 7897",
            "kind": "fact",
            "scope": "session",
        },
    ]
    out = TypeAdapter(list[MemoryEntryOut]).validate_python(rows)
    assert out[0].session_id is None
    assert out[0].project_id == 3
    assert out[1].session_id == 42
