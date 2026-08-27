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


# ── v32 (plan-89): auto_approve 与 force_approval 冲突修复 ──


@pytest.mark.asyncio
async def test_auto_approve_skips_non_forced_tool(monkeypatch):
    """auto_approve=True 且工具不在强制审批列表/非高风险 → 直接批准。"""
    from app.core.config import settings
    monkeypatch.setattr(settings, "auto_approve_tools", True)
    monkeypatch.setattr(settings, "force_approval_tools", "terminal_exec,ci_run")
    mgr = ApprovalManager()
    approved = await mgr.request(detail={"tool": "fs_write", "risk_level": "medium"})
    assert approved is True


@pytest.mark.asyncio
async def test_auto_approve_still_asks_forced_tool(monkeypatch):
    """v32 (plan-89): 强制审批列表内的工具即使 auto_approve=True 仍走审批
    （恢复"始终需要审批的工具"字段本义，修复二者语义冲突）。"""
    from app.core.config import settings
    monkeypatch.setattr(settings, "auto_approve_tools", True)
    monkeypatch.setattr(settings, "force_approval_tools", "terminal_exec")
    mgr = ApprovalManager()
    aid = mgr.new_id()

    async def rejector():
        await asyncio.sleep(0.05)
        assert mgr.resolve(aid, False) is True

    asyncio.create_task(rejector())
    approved = await mgr.request(approval_id=aid, detail={"tool": "terminal_exec", "risk_level": "high"})
    assert approved is False  # 进入了审批流程（被拒绝），而非直接批准


@pytest.mark.asyncio
async def test_auto_approve_still_asks_high_risk(monkeypatch):
    """高风险工具即使 auto_approve=True 仍弹审批（v1.0 安全底线，v32 恢复）。"""
    from app.core.config import settings
    monkeypatch.setattr(settings, "auto_approve_tools", True)
    monkeypatch.setattr(settings, "force_approval_tools", "")
    mgr = ApprovalManager()
    aid = mgr.new_id()

    async def rejector():
        await asyncio.sleep(0.05)
        mgr.resolve(aid, False)

    asyncio.create_task(rejector())
    approved = await mgr.request(approval_id=aid, detail={"tool": "some.risk", "risk_level": "high"})
    assert approved is False
