"""ApprovalManager 单测。

测试 request/resolve/超时/未匹配 resolve 四条路径。
注意:conftest 已把 APPROVAL_TIMEOUT_SEC 设为 2(秒),用于快速验证超时。
"""
import asyncio

import pytest

from app.orchestration.approval import ApprovalManager


@pytest.mark.asyncio
async def test_request_then_resolve_approved():
    mgr = ApprovalManager()
    aid = mgr.new_id()

    async def approver():
        await asyncio.sleep(0.05)
        assert mgr.resolve(aid, True) is True

    asyncio.create_task(approver())
    approved = await mgr.request(approval_id=aid, detail={"tool": "fs_write"})
    assert approved is True
    assert mgr.pending_count == 0  # 完成后已出 pending


@pytest.mark.asyncio
async def test_request_then_resolve_rejected():
    mgr = ApprovalManager()
    aid = mgr.new_id()

    async def rejector():
        await asyncio.sleep(0.05)
        mgr.resolve(aid, False)

    asyncio.create_task(rejector())
    approved = await mgr.request(approval_id=aid, detail={"tool": "terminal_exec"})
    assert approved is False


@pytest.mark.asyncio
async def test_request_timeout_auto_reject():
    """conftest 设 APPROVAL_TIMEOUT_SEC=2,不 resolve 应超时返 False。"""
    mgr = ApprovalManager()
    approved = await mgr.request(detail={"tool": "fs_write"})
    assert approved is False


@pytest.mark.asyncio
async def test_resolve_unknown_id_returns_false():
    mgr = ApprovalManager()
    assert mgr.resolve("nonexistent", True) is False


@pytest.mark.asyncio
async def test_on_request_callback_invoked():
    mgr = ApprovalManager()
    captured: list[tuple[str, dict]] = []

    async def cb(aid: str, detail: dict):
        captured.append((aid, detail))

    mgr.set_on_request(cb)
    aid = mgr.new_id()

    async def approver():
        await asyncio.sleep(0.05)
        mgr.resolve(aid, True)

    asyncio.create_task(approver())
    await mgr.request(approval_id=aid, detail={"tool": "fs_write"})
    assert len(captured) == 1
    assert captured[0][0] == aid
    assert captured[0][1]["tool"] == "fs_write"


def test_new_id_format():
    mgr = ApprovalManager()
    aid = mgr.new_id()
    assert aid.startswith("apr_")
    assert len(aid) > len("apr_")


# ── plan-75-332: 本模块不再自行放行 ──


@pytest.mark.asyncio
async def test_request_never_self_approves(monkeypatch):
    """走到 request 就意味着「需要询问用户」——无论旧配置怎么设，都不会自我放行。

    改造前会读 settings.auto_approve_tools 直接批准、再按 force_approval_tools /
    risk_level 兜底；现在是否放行统一由 approval_policy.decide() 在 executor 侧判定，
    本模块只负责挂起等待与超时。
    """
    from app.core.config import settings
    monkeypatch.setattr(settings, "auto_approve_tools", True, raising=False)
    monkeypatch.setattr(settings, "force_approval_tools", "", raising=False)
    mgr = ApprovalManager()
    aid = mgr.new_id()

    async def rejector():
        await asyncio.sleep(0.05)
        assert mgr.resolve(aid, False) is True

    asyncio.create_task(rejector())
    approved = await mgr.request(approval_id=aid, detail={"tool": "terminal_exec", "risk_level": "high"})
    assert approved is False  # 进入了审批流程（被拒绝），而非直接批准


@pytest.mark.asyncio
async def test_question_kind_has_no_timeout():
    """结构化提问（kind=question）不设超时——AI 保持暂停直到用户作答。"""
    mgr = ApprovalManager()
    aid = mgr.new_id()

    async def answerer():
        await asyncio.sleep(0.1)
        mgr.resolve(aid, True, answer={"step": "ok"})

    asyncio.create_task(answerer())
    # conftest 把审批超时设为 2s；提问路径不读该超时，作答后应正常返回
    approved = await mgr.request(approval_id=aid, detail={"kind": "question"})
    assert approved is True
