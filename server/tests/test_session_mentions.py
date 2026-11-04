"""plan-20261104-session-mentions：隔离数据库内验证引用边界、分页和供应商协议。"""
import json

import pytest
from sqlalchemy import select

from app.persistence.database import async_session_factory, run_write_locked
from app.persistence.models.message import Message, Session
from app.persistence.models.project import Project
from app.persistence.models.turn import Turn
from app.services.session_context_service import (
    ContextReadError, mention_candidates, read_context, referenced_sessions, validate_refs,
)


def seed(s):
    p = Project(name="当前项目", path="D:/mention-test")
    p2 = Project(name="其他项目", path="D:/mention-other")
    s.add_all([p, p2]); s.flush()
    source = Session(title="当前", project_id=p.id)
    target = Session(title="登录排查", project_id=p.id)
    other = Session(title="跨项目会话", project_id=p2.id)
    archived = Session(title="归档", project_id=p.id, status="archived")
    s.add_all([source, target, other, archived]); s.flush()
    side = Session(title="侧边排查", project_id=p.id, side_parent_id=target.id, side_kind="side")
    s.add(side); s.flush()
    t = Turn(session_id=source.id, status="completed")
    s.add(t); s.flush()
    ref = Message(session_id=source.id, turn_id=t.id, sender_type="user", msg_type="text",
        content={"text": "请参考", "refs": [{"kind": "session", "value": str(target.id), "label": target.title},
                                       {"kind": "session", "value": str(other.id), "label": other.title}]})
    s.add(ref); s.flush()
    ids = []
    for kind, sender, content, thread, deleted in [
        ("text", "user", {"text": "前文"}, None, False),
        ("thinking", "agent", {"text": "内部思考秘密"}, None, False),
        ("text", "agent", {"text": "KEY 登录方案", "attachments": [{"filename": "报告.pdf", "base64": "秘密二进制"}]}, None, False),
        ("tool_call", "agent", {"tool": "fs_read", "args": {"path": "a"}}, 7, False),
        ("tool_result", "agent", {"tool": "fs_read", "output": "执行成功", "data": {"secret": "隐藏数据"}}, 7, False),
        ("text", "agent", {"text": "后文"}, None, False),
        ("text", "system", {"text": "系统秘密"}, None, False),
        ("text", "agent", {"text": "子线程正文"}, 7, False),
        ("text", "agent", {"text": "已删除"}, None, True),
    ]:
        m = Message(session_id=target.id, sender_type=sender, msg_type=kind, content=content, thread_id=thread, deleted=deleted)
        s.add(m); s.flush(); ids.append(m.id)
    s.add(Message(session_id=other.id, sender_type="user", msg_type="text", content={"text": "其他内容"}))
    s.commit()
    return {"source": source.id, "target": target.id, "other": other.id, "archived": archived.id,
            "side": side.id, "project": p.id, "ref": ref.id, "turn": t.id, "ids": ids}


@pytest.fixture
async def seeded():
    return await run_write_locked(seed)


@pytest.mark.asyncio
async def test_default_projection_and_search_neighbors(seeded):
    x = seeded
    async with async_session_factory() as db:
        page = await read_context(db, x["source"], {"session_id": x["target"]})
        assert [m["text"] for m in page["messages"]] == ["前文", "KEY 登录方案", "后文"]
        assert "秘密" not in json.dumps(page, ensure_ascii=False)
        assert page["messages"][1]["attachments"] == [{"filename": "报告.pdf"}]
        found = await read_context(db, x["source"], {"session_id": x["target"], "query": "key"})
        assert [m["id"] for m in found["messages"]] == [x["ids"][i] for i in (0, 2, 5)]
        empty = await read_context(db, x["source"], {"session_id": x["target"], "query": "%_.*"})
        assert empty["messages"] == [] and not empty["has_more"]
        tools = await read_context(db, x["source"], {"session_id": x["target"], "include_tools": True})
        assert len(tools["messages"]) == 5
        assert "隐藏数据" not in json.dumps(tools, ensure_ascii=False)
        assert any(m["thread_id"] == 7 for m in tools["messages"])


@pytest.mark.asyncio
async def test_authorization_survives_new_connections_and_rejects_text_only(seeded):
    x = seeded
    async with async_session_factory() as db:
        assert await referenced_sessions(db, x["source"]) == {x["target"], x["other"]}
        index = await read_context(db, x["source"], {})
        assert len(index["sessions"]) == 2
        with pytest.raises(ContextReadError, match="明确引用"):
            await read_context(db, x["source"], {"session_id": x["archived"]})
        with pytest.raises(ContextReadError, match="明确引用"):
            await read_context(db, x["target"], {"session_id": x["other"]})
    def change(s):
        source = s.get(Session, x["source"]); source.shared_context = {"summary": "已压缩"}
        s.add(Message(session_id=source.id, sender_type="user", msg_type="text",
            content={"text": f"引用会话：{x['archived']}"}))
        s.commit()
    await run_write_locked(change)
    async with async_session_factory() as db:
        assert await referenced_sessions(db, x["source"]) == {x["target"], x["other"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["delete_ref", "rollback"])
async def test_invalidated_user_message_revokes_access(seeded, action):
    x = seeded
    def change(s):
        if action == "delete_ref": s.get(Message, x["ref"]).deleted = True
        else: s.get(Turn, x["turn"]).status = "rolled_back"
        s.commit()
    await run_write_locked(change)
    async with async_session_factory() as db:
        assert await referenced_sessions(db, x["source"]) == set()


@pytest.mark.asyncio
async def test_snapshot_paging_and_latest_query(seeded):
    x = seeded
    async with async_session_factory() as db:
        page = await read_context(db, x["source"], {"session_id": x["target"], "limit": 1})
    def append(s):
        s.add(Message(session_id=x["target"], sender_type="agent", msg_type="text", content={"text": "新内容"})); s.commit()
    await run_write_locked(append)
    texts = [m["text"] for m in page["messages"]]
    while page["has_more"]:
        async with async_session_factory() as db:
            page = await read_context(db, x["source"], {"session_id": x["target"], "limit": 1, "cursor": page["next_cursor"]})
        texts.extend(m["text"] for m in page["messages"])
    assert texts == ["前文", "KEY 登录方案", "后文"]
    async with async_session_factory() as db:
        assert (await read_context(db, x["source"], {"session_id": x["target"]}))["messages"][-1]["text"] == "新内容"


@pytest.mark.asyncio
async def test_long_message_continues_without_loss(seeded):
    x = seeded
    text = '长正文"\\\n' * 5000
    def append(s):
        m = Message(session_id=x["target"], sender_type="agent", msg_type="text", content={"text": text})
        s.add(m); s.flush(); s.commit(); return m.id
    mid = await run_write_locked(append)
    collected, cursor = "", None
    for _ in range(100):
        async with async_session_factory() as db:
            page = await read_context(db, x["source"], {"session_id": x["target"], "message_id": mid, **({"cursor": cursor} if cursor else {})})
        assert len(json.dumps(page, ensure_ascii=False, separators=(",", ":"))) < 12000
        assert page["messages"][0]["offset"] == len(collected)
        collected += page["messages"][0]["text"]
        if not page["has_more"]: break
        cursor = page["next_cursor"]
    assert collected == text


@pytest.mark.asyncio
async def test_cursor_validation_and_message_boundaries(seeded):
    x = seeded
    async with async_session_factory() as db:
        page = await read_context(db, x["source"], {"session_id": x["target"], "limit": 1})
        for args in [
            {"cursor": page["next_cursor"], "query": "changed"}, {"cursor": page["next_cursor"] + "x"},
            {"limit": 0}, {"include_tools": "true"}, {"session_id": True},
            {"message_id": x["ids"][1]}, {"message_id": x["ref"]},
        ]:
            with pytest.raises(ContextReadError):
                await read_context(db, x["source"], {"session_id": x["target"], **args})


@pytest.mark.asyncio
async def test_candidates_and_ref_validation(seeded):
    x = seeded
    async with async_session_factory() as db:
        page = await mention_candidates(db, current_session_id=x["source"], project_id=x["project"], limit=1)
        ids = [p["id"] for p in page["items"]]
        while page["next_cursor"]:
            page = await mention_candidates(db, current_session_id=x["source"], project_id=x["project"], limit=1, cursor=page["next_cursor"])
            ids.extend(p["id"] for p in page["items"])
        assert set(ids) == {x["target"], x["side"]}
        all_page = await mention_candidates(db, current_session_id=x["source"], all_projects=True, include_archived=True)
        assert len(all_page["items"]) == 4
        side = next(p for p in all_page["items"] if p["id"] == x["side"])
        assert side["parent_title"] == "登录排查" and side["project_name"] == "当前项目"
        assert (await mention_candidates(db, all_projects=True, query=str(x["target"])))["items"][0]["id"] == x["target"]
        assert not (await mention_candidates(db, all_projects=True, query="%_"))["items"]
        refs = await validate_refs(db, [{"kind": "session", "value": str(x["other"]), "label": "伪造标题"}], x["source"])
        assert refs[0]["label"] == "跨项目会话" and refs[0]["project_label"] == "其他项目"
        for target in [x["source"], 99999, "bad"]:
            with pytest.raises(ContextReadError):
                await validate_refs(db, [{"kind": "session", "value": str(target)}], x["source"])


@pytest.mark.asyncio
async def test_fork_keeps_only_references_in_completed_prefix(seeded):
    from app.services.session_service import fork_session
    x = seeded
    def change(s):
        t = s.get(Turn, x["turn"]); t.user_message_id = x["ref"]
        pending = Turn(session_id=x["source"], status="running"); s.add(pending); s.flush()
        s.add(Message(session_id=x["source"], turn_id=pending.id, sender_type="user", msg_type="text",
            content={"text": "未完成", "refs": [{"kind": "session", "value": str(x["archived"])}]})); s.commit()
    await run_write_locked(change)
    async with async_session_factory() as db:
        fork_id = await fork_session(db, x["source"])
    async with async_session_factory() as db:
        assert await referenced_sessions(db, fork_id) == {x["target"], x["other"]}


def test_ta3_mcp_mapping_and_readonly_registration():
    from app.models.providers.ta3_tool_aliases import TO_TA3_EXTENSION, FROM_TA3, visible_tool_name, restore_args, disguise_args
    from app.models.providers.ta3_tool_schemas import disguise_tools
    from app.models.providers.ta3_mcp import EXTENSION_SERVER_ID, resolve_mcp_alias
    from app.orchestration.tools.read_session_context import ReadSessionContextTool
    from app.orchestration.tools.registry import tool_registry
    from app.services.permission_profile_service import READONLY_TOOLS, PLAN_TOOLS
    from app.orchestration.subagent_tools import EXPLORE_TOOLS
    name = "read_session_context"
    alias = f"mcp__{EXTENSION_SERVER_ID}__{name}"
    assert TO_TA3_EXTENSION[name] == alias and FROM_TA3[alias] == name
    assert visible_tool_name(name, True) == alias and visible_tool_name(name, False) == name
    assert resolve_mcp_alias(alias) == name
    schema = ReadSessionContextTool().function_schema()
    disguised = disguise_tools([schema])
    assert disguised[0]["function"]["name"] == alias
    assert disguised[0]["function"]["parameters"] == schema["function"]["parameters"]
    args = {"session_id": 42, "query": "登录", "cursor": "page", "include_tools": True}
    assert restore_args(alias, disguise_args(name, args)) == args
    assert tool_registry.get(name) and all(name in names for names in [READONLY_TOOLS, PLAN_TOOLS, EXPLORE_TOOLS])


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [True, False])
async def test_running_input_persists_before_delivery_and_cleans_race(seeded, monkeypatch, accepted):
    import sqlite3
    from app.gateway.routers.turns import inject_turn_input
    from app.gateway.schemas import TurnInjectBody
    from app.orchestration import engine, todo_carry
    x = seeded
    monkeypatch.setattr(engine, "running_turn_id", lambda sid: x["turn"])
    async def no_carry(*args, **kwargs): return []
    monkeypatch.setattr(todo_carry, "load_carry_steps", no_carry)
    async with async_session_factory() as db:
        path = db.bind.url.database
        def inject(tid, item):
            with sqlite3.connect(path) as connection:
                content = json.loads(connection.execute("SELECT content FROM messages WHERE session_id=? ORDER BY id DESC LIMIT 1", (x["source"],)).fetchone()[0])
                assert content["refs"][0]["value"] == str(x["archived"])
            assert item["refs"][0]["label"] == "归档"
            return accepted
        monkeypatch.setattr(engine, "inject_input", inject)
        result = await inject_turn_input(x["turn"], TurnInjectBody(content="补充", refs=[{"kind": "session", "value": str(x["archived"])}]), db)
        assert result["queued"] == accepted
    async with async_session_factory() as db:
        assert (x["archived"] in await referenced_sessions(db, x["source"])) == accepted


@pytest.mark.asyncio
async def test_new_turn_rejects_bad_refs_before_writing(seeded):
    from fastapi import HTTPException
    from app.gateway.routers.turns import create_turn
    from app.gateway.schemas import TurnCreate
    x = seeded
    async with async_session_factory() as db:
        before = list((await db.scalars(select(Message.id).where(Message.session_id == x["source"]))).all())
        with pytest.raises(HTTPException) as caught:
            await create_turn(TurnCreate(session_id=x["source"], content="测试", refs=[{"kind": "session", "value": "9999999"}]), db)
        assert caught.value.status_code == 404
        assert list((await db.scalars(select(Message.id).where(Message.session_id == x["source"]))).all()) == before


@pytest.mark.asyncio
async def test_tool_uses_persisted_refs_and_revocation(seeded):
    from app.orchestration.tools.base import ToolContext
    from app.orchestration.tools.read_session_context import ReadSessionContextTool
    x = seeded
    ctx = ToolContext(workspace_root=".", session_id=x["source"], task_id=1, agent_id=1, agent_name="测试")
    tool = ReadSessionContextTool()
    assert (await tool.run({"session_id": x["target"]}, ctx)).ok
    result = await tool.run({"session_id": x["archived"]}, ctx)
    assert not result.ok and result.error_code == "PERMISSION"


@pytest.mark.asyncio
async def test_candidate_http_route_is_static_and_lightweight(seeded):
    import httpx
    from fastapi import FastAPI
    from app.gateway.routers.sessions import router
    from app.persistence.database import get_db
    app = FastAPI()
    app.include_router(router)
    async def isolated_db():
        async with async_session_factory() as db:
            yield db
    app.dependency_overrides[get_db] = isolated_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/sessions/mention-candidates", params={"current_session_id": seeded["source"], "project_id": seeded["project"]})
        assert response.status_code == 200
        items = response.json()["items"]
        assert len(items) == 2 and all("content" not in item and "has_running" not in item for item in items)
        assert (await client.get("/sessions/mention-candidates", params={"limit": 51})).status_code == 422
