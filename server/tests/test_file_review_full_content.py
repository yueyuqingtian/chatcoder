"""plan-20261105-file-review：完整审查保持真实行号和未修改上下文。"""
from app.services.rollback_service import _build_diff_payload, _MAX_DIFF_WINDOW_CHARS
import pytest


def test_full_review_keeps_unchanged_head_tail_and_newline():
    before = "".join(f"value_{i} = {i}\n" for i in range(3000))
    after = before.replace("value_1500 = 1500", "value_1500 = 9999")
    data = _build_diff_payload("sample.py", before, after, single_edit=False, full_file=True)
    assert data["before"] == before
    assert data["after"] == after
    assert data["additions"] == data["deletions"] == 1
    assert not data["truncated"]
    legacy = _build_diff_payload("sample.py", before, after, single_edit=False)
    assert "value_0 = 0" not in legacy["before"]


def test_full_review_large_response_is_bounded():
    before = "header\n" + "x" * (_MAX_DIFF_WINDOW_CHARS + 100) + "\ntail"
    after = before + "\nnew"
    data = _build_diff_payload("large.py", before, after, single_edit=False, full_file=True)
    assert data["truncated"]
    assert len(data["before"]) < _MAX_DIFF_WINDOW_CHARS + 100
    assert len(data["after"]) < _MAX_DIFF_WINDOW_CHARS + 100


def test_added_and_deleted_full_file_preserve_missing_side():
    added = _build_diff_payload("new.py", None, "a\nb\n", single_edit=False, full_file=True)
    deleted = _build_diff_payload("old.py", "a\nb\n", None, single_edit=False, full_file=True)
    assert added["before"] is None and added["after"] == "a\nb\n"
    assert deleted["after"] is None and deleted["before"] == "a\nb\n"


@pytest.mark.asyncio
async def test_review_route_forwards_full_file_only_when_requested(monkeypatch):
    from app.gateway.routers import turns

    async def no_history(*args):
        return None

    async def workspace(*args):
        return 1, "D:/fixture"

    async def extra(*args):
        return []

    requested = []

    async def diff(*args, **kwargs):
        requested.append(kwargs["full_file"])
        return {"path": "sample.py", "before": "before", "after": "after", "truncated": False}

    monkeypatch.setattr(turns.turn_service, "get_turn", no_history)
    monkeypatch.setattr(turns.rollback_service, "resolve_turn_workspace", workspace)
    monkeypatch.setattr(turns, "_round_extra_turn_ids", extra)
    monkeypatch.setattr(turns.rollback_service, "get_file_diff", diff)
    await turns.get_change_diff(7, "sample.py", full_file=True, turn_ids=None, db=object())
    await turns.get_change_diff(7, "sample.py", turn_ids=None, db=object())
    assert requested == [True, False]
