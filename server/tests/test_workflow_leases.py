"""写入范围、进程级租约与读账本（§5A.1）：范围重叠判定、全部或无加锁、释放、CAS 与读集索引。"""

import pytest

from app.orchestration.workflow.conflicts import (
    SNAPSHOT_LIMIT_BYTES,
    LeaseOwner,
    ReadIndex,
    ReadLedger,
    WriteLeaseTable,
    normalize_scope,
    scopes_overlap,
)


def test_normalize_scope_accepts_relative_and_rejects_escapes():
    assert normalize_scope("./src//a\\b.ts") == "src/a/b.ts"
    assert normalize_scope("src/**") == "src/**"
    assert normalize_scope("/etc/passwd") is None
    assert normalize_scope("C:/x.ts") is None
    assert normalize_scope("../x.ts") is None
    assert normalize_scope("  ") is None


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("src/a.ts", "src/a.ts", True),  # 同一文件
        ("src", "src/a.ts", True),  # 目录包含文件
        ("src/a.ts", "src/b.ts", False),  # 同目录的两个文件
        ("src/*.ts", "src/a.ts", True),  # 通配符命中字面路径
        ("src/*.ts", "lib/*.ts", False),  # 目录互不相干
        ("server/app/services/points*.py", "client/src/x.ts", False),
        ("**", "anything/else.md", True),  # 根通配覆盖一切
        ("src/a/*.ts", "src/ab/*.ts", False),  # 段级比较，不会把 a 与 ab 混为一谈
        ("src/a/*.ts", "src/a/*.tsx", True),  # 同目录的不同通配：保守视为重叠
    ],
)
def test_scopes_overlap(a, b, expected):
    assert scopes_overlap(a, b) is expected
    assert scopes_overlap(b, a) is expected


def test_invalid_scope_is_conservatively_overlapping():
    assert scopes_overlap("../x", "src/a.ts") is True


def _owner(run_id, node, attempt=None):
    return LeaseOwner(run_id=run_id, node_key=node, attempt_id=attempt)


def test_disjoint_scopes_are_granted_together(tmp_path):
    table = WriteLeaseTable()
    ws = str(tmp_path)
    ok_a, _ = table.acquire(ws, _owner(1, "impl"), ["server/**"])
    ok_b, conflicts = table.acquire(ws, _owner(2, "impl"), ["client/**"])
    assert ok_a and ok_b and conflicts == []


def test_overlap_is_all_or_nothing(tmp_path):
    table = WriteLeaseTable()
    ws = str(tmp_path)
    assert table.acquire(ws, _owner(1, "impl"), ["src/shared.ts"])[0]
    ok, conflicts = table.acquire(ws, _owner(2, "impl"), ["src/own.ts", "src/shared.ts"])
    assert not ok
    assert [c.requested for c in conflicts] == ["src/shared.ts"]
    # 失败时不得残留 src/own.ts 的租约（不存在“持有一部分再等另一部分”）
    assert all(lease.pattern != "src/own.ts" for lease in table.holders(ws))


def test_same_node_reacquires_across_attempts_without_self_conflict(tmp_path):
    table = WriteLeaseTable()
    ws = str(tmp_path)
    assert table.acquire(ws, _owner(1, "impl", attempt=1), ["src/a.ts"])[0]
    assert table.acquire(ws, _owner(1, "impl", attempt=2), ["src/a.ts"])[0]
    assert len(table.holders(ws)) == 1


def test_release_frees_only_the_owner(tmp_path):
    table = WriteLeaseTable()
    ws = str(tmp_path)
    table.acquire(ws, _owner(1, "impl"), ["src/a.ts"])
    table.acquire(ws, _owner(2, "impl"), ["src/b.ts"])
    freed = table.release(ws, _owner(1, "impl"))
    assert [lease.pattern for lease in freed] == ["src/a.ts"]
    assert [lease.pattern for lease in table.holders(ws)] == ["src/b.ts"]


def test_release_run_clears_every_node_of_the_run(tmp_path):
    table = WriteLeaseTable()
    ws = str(tmp_path)
    table.acquire(ws, _owner(7, "a"), ["x/a.ts"])
    table.acquire(ws, _owner(7, "b"), ["x/b.ts"])
    table.acquire(ws, _owner(8, "c"), ["x/c.ts"])
    assert len(table.release_run(7)) == 2
    assert [lease.owner.run_id for lease in table.holders()] == [8]


def test_leases_are_scoped_per_workspace(tmp_path):
    table = WriteLeaseTable()
    ws_a = str(tmp_path / "a")
    ws_b = str(tmp_path / "b")
    assert table.acquire(ws_a, _owner(1, "impl"), ["src/a.ts"])[0]
    assert table.acquire(ws_b, _owner(2, "impl"), ["src/a.ts"])[0]


def test_invalid_pattern_raises(tmp_path):
    with pytest.raises(ValueError):
        WriteLeaseTable().acquire(str(tmp_path), _owner(1, "impl"), ["../escape.ts"])


def test_cas_detects_external_change_and_keeps_base_snapshot():
    ledger = ReadLedger()
    assert ledger.verify_write("src/a.ts", "x").reason == "no_prior_read"
    ledger.record_read("src/a.ts", "v1")
    unchanged = ledger.verify_write("src/a.ts", "v1")
    assert unchanged.ok and unchanged.reason == "unchanged"
    changed = ledger.verify_write("src/a.ts", "v2 (edited elsewhere)")
    assert not changed.ok and changed.reason == "changed"
    assert changed.base_snapshot == "v1"


def test_cas_reports_deleted_file():
    ledger = ReadLedger()
    ledger.record_read("src/a.ts", "v1")
    result = ledger.verify_write("src/a.ts", None)
    assert result.reason == "deleted"


def test_large_files_keep_only_hash_not_snapshot():
    ledger = ReadLedger()
    big = "x" * (SNAPSHOT_LIMIT_BYTES + 1)
    ledger.record_read("big.txt", big)
    result = ledger.verify_write("big.txt", big + "!")
    assert result.reason == "changed"
    assert result.base_snapshot is None  # 超过阈值：冲突时降级为重做（L2）


def test_mark_written_moves_baseline_for_subsequent_edits():
    ledger = ReadLedger()
    ledger.record_read("src/a.ts", "v1")
    ledger.mark_written("src/a.ts", "v2")
    assert ledger.verify_write("src/a.ts", "v2").ok


def test_read_index_reports_other_readers_for_invalidation():
    index = ReadIndex()
    index.add("ws", "src/a.ts", "1:explore:1")
    index.add("ws", "src/a.ts", "2:impl:1")
    assert index.readers_of("ws", "src/a.ts", exclude="2:impl:1") == ["1:explore:1"]
    index.remove_attempt("1:explore:1")
    assert index.readers_of("ws", "src/a.ts") == ["2:impl:1"]
