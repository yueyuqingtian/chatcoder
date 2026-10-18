"""Arthas 观测作业的回归测试（不启动真实 JVM，Arthas 客户端以假实现替换）。

覆盖：
  1) 默认观测表达式使用 Arthas 的异常属性名 throwExp（写 throwable 会让 watch 立即失败，实测）；
  2) 表达式失败（status 消息 statusCode≠0）时，pull 把作业收口为失败并通知面板，不会留下空轮询的作业；
  3) 中断一个已被 Arthas 终止的作业时，本地作业表仍被清理。
"""
import asyncio

import pytest

from app.mcp_servers import arthas_client
from app.mcp_servers.debugger import _WATCH_DEFAULT_EXPRESS
from app.services import arthas_service as svc


class _FakeClient:
    results: list = []
    interrupt_error: str | None = None

    def __init__(self, *args, **kwargs):
        pass

    async def pull_results(self, **kwargs):
        return {"ok": True, "results": list(_FakeClient.results), "job_status": None}

    async def interrupt_job(self, job_id):
        if _FakeClient.interrupt_error:
            raise arthas_client.ArthasError(_FakeClient.interrupt_error)
        return {"ok": True}


@pytest.fixture
def session(monkeypatch):
    _FakeClient.results = []
    _FakeClient.interrupt_error = None
    sess = svc.ArthasSession(session_id=7, pid=1, http_port=8563, java="java", jar="boot.jar")
    sess.arthas_session_id = "s"
    sess.arthas_consumer_id = "c"
    monkeypatch.setattr(svc, "_sessions", {svc._key(7): sess})
    sent: list[dict] = []

    async def _capture(session_id, payload):
        sent.append(payload)

    monkeypatch.setattr(svc, "_broadcast", _capture)
    monkeypatch.setattr(arthas_client, "ArthasClient", _FakeClient)
    return sess, sent


def test_default_expression_uses_throw_exp():
    assert "throwExp" in _WATCH_DEFAULT_EXPRESS
    assert "throwable" not in _WATCH_DEFAULT_EXPRESS


def test_failed_expression_closes_job_and_notifies_panel(session):
    sess, sent = session
    sess.jobs["5"] = {"command": "watch a b '{params, returnObj, throwable}' -x 2", "created_at": 0.0}
    _FakeClient.results = [{
        "type": "status", "statusCode": -1, "jobId": 5,
        "message": "watch failed, ognl.NoSuchPropertyException: Advice.throwable",
    }]
    res = asyncio.run(svc.pull(7, "5", timeout_s=0.1))
    assert res["ok"] is True
    assert res["job_status"] == "FAILED"
    assert "5" not in sess.jobs
    failed = [p for p in sent if p.get("phase") == "job_failed"]
    assert failed and "NoSuchPropertyException" in failed[0]["summary"]


def test_interrupt_clears_local_job_even_when_arthas_already_ended(session):
    sess, _ = session
    sess.jobs["9"] = {"command": "watch a b '{params}' -x 2", "created_at": 0.0}
    _FakeClient.interrupt_error = "no foreground job is running"
    res = asyncio.run(svc.interrupt(7, "9"))
    assert res["ok"] is False
    assert "9" not in sess.jobs
