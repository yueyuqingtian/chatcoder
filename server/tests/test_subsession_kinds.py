"""子会话种类与归属回归测试（plan-156-620）。

覆盖：
  1) 侧边对话（side）与执行子会话（sub）分离：右侧面板只列侧边对话，左栏索引只列执行子会话；
  2) 子会话工具只能操作本主会话名下的子会话（越权被拒）；
  3) 自动标题：前端占位截断与服务端截断长度不同时，仍识别为占位标题。
"""
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.enums import SideKind
from app.gateway.routers import sessions as sessions_router
from app.persistence.database import async_session_factory, run_write_locked
from app.persistence.models.message import Session as SessionRow
from app.persistence.models.project import Project as ProjectRow
from app.services import subsession_service
from app.services.session_service import _auto_title_fallback


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(sessions_router.router)
    return app


async def _seed() -> dict[str, int]:
    """两个主会话：A 下一个侧边对话 + 一个执行子会话；B 下一个执行子会话。"""
    def _op(s):
        proj = ProjectRow(name="种类测试", path=f"C:/tmp/subsession-kind-test-{uuid.uuid4().hex[:8]}")
        s.add(proj)
        s.flush()
        a = SessionRow(title="主会话A", project_id=proj.id)
        b = SessionRow(title="主会话B", project_id=proj.id)
        s.add_all([a, b])
        s.flush()
        side = SessionRow(title="侧边会话", project_id=proj.id, side_parent_id=a.id, side_kind=SideKind.SIDE.value)
        sub_a = SessionRow(title="子任务A", project_id=proj.id, side_parent_id=a.id, side_kind=SideKind.SUB.value)
        sub_b = SessionRow(title="子任务B", project_id=proj.id, side_parent_id=b.id, side_kind=SideKind.SUB.value)
        s.add_all([side, sub_a, sub_b])
        s.flush()
        ids = {"project": proj.id, "a": a.id, "b": b.id, "side": side.id, "sub_a": sub_a.id, "sub_b": sub_b.id}
        s.commit()
        return ids

    return await run_write_locked(_op, label="test.subsession.kinds")


async def test_side_panel_and_sidebar_index_are_separated():
    ids = await _seed()
    client = TestClient(_app())

    side_list = client.get(f"/sessions/{ids['a']}/side-sessions").json()
    assert [c["id"] for c in side_list] == [ids["side"]]

    index = client.get(f"/sessions/subsession-index?project_id={ids['project']}").json()
    got = {c["id"]: c["side_kind"] for c in index}
    assert got == {ids["sub_a"]: "sub", ids["sub_b"]: "sub"}


async def test_subsession_ownership_is_enforced():
    ids = await _seed()
    async with async_session_factory() as db:
        # 主会话与其子会话都能操作同组子会话
        await subsession_service.require_owned(db, ids["a"], ids["sub_a"])
        # 跨主会话、以及把侧边对话当子会话，都必须被拒
        with pytest.raises(subsession_service.SubsessionError):
            await subsession_service.require_owned(db, ids["a"], ids["sub_b"])
        with pytest.raises(subsession_service.SubsessionError):
            await subsession_service.require_owned(db, ids["a"], ids["side"])


def test_auto_title_accepts_shorter_frontend_placeholder():
    text = "请帮我把左侧会话列表的置顶按钮改成悬停显示并统一图标"
    placeholder = SimpleNamespace(title=text[:12] + "…", title_source="auto")
    assert _auto_title_fallback(placeholder, text) == text[:30]

    named = SimpleNamespace(title="正式标题", title_source="auto")
    assert _auto_title_fallback(named, text) is None

    manual = SimpleNamespace(title="", title_source="manual")
    assert _auto_title_fallback(manual, text) is None


async def _suggestion(status: str, decided_at: str | None = None) -> int:
    from app.persistence.models.suggestion import TaskSuggestion

    ids = await _seed()

    def _op(s):
        row = TaskSuggestion(session_id=ids["a"], turn_id=None, payload_json={"title": "t"},
                             status=status, decided_at=decided_at)
        s.add(row)
        s.flush()
        sid = row.id
        s.commit()
        return sid

    return await run_write_locked(_op, label="test.suggestion.seed")


async def test_suggestion_claim_is_atomic_and_recovers_stale():
    from app.services import suggestion_service as svc

    sid = await _suggestion(svc.STATUS_PENDING)
    assert await svc._claim(sid) is True
    # 第二次（双击 / 并发）必须失败，不能再建一个子会话
    assert await svc._claim(sid) is False

    stale = await _suggestion(svc.STATUS_ACCEPTING, decided_at="2000-01-01T00:00:00+00:00")
    assert await svc._claim(stale) is True


async def test_background_title_does_not_override_manual_rename():
    from app.services.session_service import _patch_title_if_auto

    ids = await _seed()

    def _rename(s):
        row = s.get(SessionRow, ids["a"])
        row.title = "用户改的名字"
        row.title_source = "manual"
        s.commit()

    await run_write_locked(_rename, label="test.rename")
    assert await _patch_title_if_auto(ids["a"], "AI 归纳的标题") is False
    async with async_session_factory() as db:
        assert (await db.get(SessionRow, ids["a"])).title == "用户改的名字"


def test_report_paging_never_drops_text():
    from app.orchestration.subsession_report import continuation_hint, page_of

    report = "结" * 25000
    seen = ""
    offset = 0
    while True:
        page = page_of(report, offset, 12000)
        seen += page.text
        hint = continuation_hint(page, 42)
        if page.complete:
            assert hint == ""
            break
        # 未读完必须给出下一段的准确起点
        assert f"report_offset={page.end}" in hint and "subsession_id=42" in hint
        offset = page.end
    assert seen == report


async def test_progress_returns_full_final_answer():
    from app.core.enums import MsgType, SenderType
    from app.orchestration.subsession_report import REPORT_PREVIEW_CHARS
    from app.orchestration.subsession_tool_runner import _format_brief
    from app.persistence.models.message import Message
    from app.persistence.models.turn import Turn

    ids = await _seed()
    long_answer = "结论" + "x" * 3000 + "END"

    def _op(s):
        turn = Turn(session_id=ids["sub_a"], status="completed")
        s.add(turn)
        s.flush()
        for content in ({"text": "过程说明"}, {"text": long_answer}, {"text": "思考", "thinking": True}):
            s.add(Message(session_id=ids["sub_a"], turn_id=turn.id, sender_type=SenderType.AGENT.value,
                          msg_type=MsgType.TEXT.value, content=content))
        s.commit()

    await run_write_locked(_op, label="test.subsession.answer")
    async with async_session_factory() as db:
        brief = await subsession_service.describe(db, ids["sub_a"])
    # 结论原文取最后一条非思考正文，且完整无截断
    assert brief.report == long_answer
    # 指定单个子会话：整段给全
    single = _format_brief(brief, report_offset=0)
    assert long_answer in single and "尚未读完" not in single
    # 汇总视图：只给开头，但必须明确提示如何续读
    summary = _format_brief(brief)
    assert long_answer[:REPORT_PREVIEW_CHARS] in summary
    assert f"report_offset={REPORT_PREVIEW_CHARS}" in summary
