"""变更清单「净变更」与撤销严格校验测试（plan-20261019-toolcard-v2）。

用户反馈的缺陷：
  ① 本轮新建、收尾又被 AI 用 shell 删除的临时文件，仍显示在变更列表里（点「审查」报文件不存在）；
  ② 之前就存在、本轮被删除的文件不显示，更没有「已删除」标记；
  ③ 本轮改过的文件，之后用户又手动改过，撤销却把手动改动「合并」掉了，没有报错。

覆盖：删除类 shell 命令解析、净变更折算（建后又删不展示 / 删除标记）、撤销顺序锁只认有净变更的轮次、
撤销前的严格校验（手动改动 / 删除 / 重建 → 整体拒绝并给出原因）、删除后按备份字节恢复、轮次收尾对账。
"""
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.persistence.database import Base
from app.persistence.models.message import Message, Session
from app.persistence.models.rollback import TurnSnapshot
from app.persistence.models.turn import Turn
from app.services import rollback_service as rs


# ── 删除类 shell 命令解析（纯函数）────────────────────────────────────────────


def test_delete_paths_posix_rm():
    assert rs.extract_shell_delete_paths("rm tmp_verify_v083.js") == ["tmp_verify_v083.js"]
    assert rs.extract_shell_delete_paths("rm -rf build/ dist") == ["build/", "dist"]
    assert rs.extract_shell_delete_paths("/bin/rm -f a.txt b.txt") == ["a.txt", "b.txt"]
    assert rs.extract_shell_delete_paths('rm "my file.txt"') == ["my file.txt"]
    # 重定向残片（2>&1 被语句切分后会剩下 `2>`）不能当成路径
    assert rs.extract_shell_delete_paths("rm a.txt 2>&1") == ["a.txt"]
    assert rs.extract_shell_delete_paths("rm -- -weird.txt") == ["-weird.txt"]


def test_delete_paths_windows_commands():
    assert rs.extract_shell_delete_paths("del /f /q tmp.js") == ["tmp.js"]
    assert rs.extract_shell_delete_paths("erase tmp.js") == ["tmp.js"]
    assert rs.extract_shell_delete_paths("rd /s /q out") == ["out"]
    assert rs.extract_shell_delete_paths("rmdir /s /q out") == ["out"]


def test_delete_paths_powershell():
    assert rs.extract_shell_delete_paths("Remove-Item -Path .\\tmp.js -Force") == [".\\tmp.js"]
    assert rs.extract_shell_delete_paths("Remove-Item tmp.js") == ["tmp.js"]
    assert rs.extract_shell_delete_paths("Remove-Item a.txt, b.txt -Force") == ["a.txt", "b.txt"]
    assert rs.extract_shell_delete_paths("ri -Recurse -Force dist") == ["dist"]
    assert rs.extract_shell_delete_paths("Remove-Item -LiteralPath 'x y.txt'") == ["x y.txt"]
    # -Include 的值是通配过滤器，不是路径
    assert rs.extract_shell_delete_paths("Remove-Item .\\logs\\* -Include *.log") == [".\\logs\\*"]


def test_delete_paths_git_rm_and_chains():
    assert rs.extract_shell_delete_paths("git rm old.txt") == ["old.txt"]
    assert rs.extract_shell_delete_paths("git rm -r --cached keep.txt") == []  # 只移出索引，文件还在
    assert rs.extract_shell_delete_paths("cd sub && rm file.txt && echo ok > log.txt") == ["file.txt"]
    assert rs.extract_shell_delete_paths("node t.js; rm t.js") == ["t.js"]
    assert rs.extract_shell_delete_paths("echo hi") == []


def test_delete_paths_inline_scripts():
    assert rs.extract_shell_delete_paths("python -c \"import os; os.remove('x.txt')\"") == ["x.txt"]
    assert rs.extract_shell_delete_paths("python -c \"import shutil; shutil.rmtree('build')\"") == ["build"]
    assert rs.extract_shell_delete_paths("node -e \"require('fs').unlinkSync('a.js')\"") == ["a.js"]
    assert rs.extract_shell_delete_paths("node -e \"fs.unlinkSync('a.js')\"") == ["a.js"]
    assert rs.extract_shell_delete_paths("node -e \"fs.rmSync('dist', {recursive:true})\"") == ["dist"]


def test_move_removes_source_and_creates_destination():
    assert rs.extract_shell_delete_paths("mv a.txt b.txt") == ["a.txt"]
    assert "b.txt" in rs.extract_shell_move_dests("mv a.txt b.txt")
    assert rs.extract_shell_delete_paths("Move-Item -Path a.txt -Destination b.txt") == ["a.txt"]
    assert "b.txt" in rs.extract_shell_move_dests("Move-Item -Path a.txt -Destination b.txt")


def test_resolve_includes_deletes_cd_and_glob(workspace):
    (workspace / "sub").mkdir()
    (workspace / "sub" / "gone.txt").write_text("x", encoding="utf-8")
    (workspace / "a1.log").write_text("1", encoding="utf-8")
    (workspace / "a2.log").write_text("2", encoding="utf-8")
    paths = rs.resolve_command_write_paths(
        "terminal_exec", {"command": "cd sub && rm gone.txt"}, str(workspace))
    assert "sub/gone.txt" in paths  # cd 之后的相对路径也会被带上
    paths = rs.resolve_command_write_paths("terminal_exec", {"command": "rm *.log"}, str(workspace))
    assert paths == ["a1.log", "a2.log"]  # 通配符就地展开


def test_resolve_delete_outside_workspace_rejected(workspace):
    paths = rs.resolve_command_write_paths("terminal_exec", {"command": "rm ../outside.txt"}, str(workspace))
    assert paths == []


def test_expand_directory_targets(workspace):
    (workspace / "out" / "deep").mkdir(parents=True)
    (workspace / "out" / "a.txt").write_text("a", encoding="utf-8")
    (workspace / "out" / "deep" / "b.txt").write_text("b", encoding="utf-8")
    (workspace / "keep.txt").write_text("k", encoding="utf-8")
    got = rs.expand_directory_targets(str(workspace), ["out", "keep.txt", "missing.txt"])
    assert got == ["out/a.txt", "out/deep/b.txt", "keep.txt", "missing.txt"]


def test_expand_directory_skips_huge_and_excluded(workspace):
    (workspace / "big").mkdir()
    for i in range(5):
        (workspace / "big" / f"f{i}.txt").write_text("x", encoding="utf-8")
    assert rs.expand_directory_targets(str(workspace), ["big"], max_files=3) == []  # 过多：整体不记录
    (workspace / "node_modules" / "pkg").mkdir(parents=True)
    (workspace / "node_modules" / "pkg" / "i.js").write_text("x", encoding="utf-8")
    assert rs.expand_directory_targets(str(workspace), ["node_modules"]) == []  # 永不展开


# ── 数据库场景 ────────────────────────────────────────────────────────────────


@pytest.fixture
async def db(monkeypatch, tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path}/rb_net.db"
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("app.persistence.database.async_session_factory", factory)
    from app.persistence import write_engine as _we
    _we.configure(db_url, foreign_keys=False)
    async with factory() as s:
        yield s
    await engine.dispose()
    _we.configure(None)


async def _session(db, workspace) -> Session:
    sess = Session(project_id=None, worktree_path=str(workspace), title="net_test", status="active")
    db.add(sess)
    await db.flush()
    return sess


async def _turn(db, session, *, file_list=None, new_files=None) -> Turn:
    turn = Turn(session_id=session.id, status="completed")
    db.add(turn)
    await db.flush()
    msg = Message(session_id=session.id, turn_id=turn.id, sender_type="user", msg_type="text", content={"text": "x"})
    db.add(msg)
    await db.flush()
    db.add(TurnSnapshot(session_id=session.id, turn_id=turn.id, user_message_id=msg.id, git_head=None,
                        file_list=file_list or [], new_files=new_files or []))
    await db.flush()
    await db.commit()
    return turn


async def _write(db, sess, turn, ws, path, *, before, after, op, binary=False, tool="terminal_exec"):
    """登记一条带 op / 指纹的写盘记录（指纹取当前磁盘字节，调用方先把磁盘调成「写后」的样子）。"""
    await rs.record_turn_write(
        db, session_id=sess.id, turn_id=turn.id, tool=tool, path=path,
        before=None if binary else before, after=None if binary else after, binary=binary,
        op=op, after_hash=rs.file_digest(str(ws), path) if op != rs.OP_DELETE else None,
    )


@pytest.mark.asyncio
async def test_created_then_deleted_file_is_not_listed(db, tmp_path):
    """用户原始反馈：本轮新建、收尾又被删除的临时文件，不应出现在变更列表里。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    sess = await _session(db, ws)
    turn = await _turn(db, sess, new_files=["tmp_verify_v083.js"])
    (ws / "tmp_verify_v083.js").write_text("console.log(1)\n", encoding="utf-8")
    await _write(db, sess, turn, ws, "tmp_verify_v083.js", before=None, after="console.log(1)\n", op=rs.OP_CREATE)
    # 收尾：AI 用 shell 把它删了（磁盘上没有了），登记一条删除
    (ws / "tmp_verify_v083.js").unlink()
    await _write(db, sess, turn, ws, "tmp_verify_v083.js", before="console.log(1)\n", after=None, op=rs.OP_DELETE)

    changes = await rs.list_turn_changes(db, session_id=sess.id, turn_id=turn.id, workspace=str(ws))
    assert changes == []
    # 审查：该文件没有可展示的净变更
    assert await rs.get_file_diff(db, session_id=sess.id, turn_id=turn.id, workspace=str(ws),
                                  path="tmp_verify_v083.js") is None


@pytest.mark.asyncio
async def test_created_then_deleted_legacy_records_also_hidden(db, tmp_path):
    """旧记录（没有 op）也要按「首条写前状态 + 末条写后状态」折算：新建 → 删除 = 零净变更。
    旧实现用 any(old_content is not None) 判定「写前存在」，会把它误判成「之前存在、被删除」。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    sess = await _session(db, ws)
    turn = await _turn(db, sess)
    await rs.record_turn_write(db, session_id=sess.id, turn_id=turn.id, tool="fs_write",
                               path="t.js", before=None, after="x\n")
    await rs.record_turn_write(db, session_id=sess.id, turn_id=turn.id, tool="terminal_exec",
                               path="t.js", before="x\n", after=None)
    assert await rs.list_turn_changes(db, session_id=sess.id, turn_id=turn.id, workspace=str(ws)) == []


@pytest.mark.asyncio
async def test_existing_file_deleted_is_listed_as_deleted(db, tmp_path):
    """之前就存在、本轮被删除的文件：要在列表里，并标记「已删除」，行数统计为整篇删除。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    sess = await _session(db, ws)
    turn = await _turn(db, sess, file_list=[{"ckpt": "", "path": "old.txt"}])
    await _write(db, sess, turn, ws, "old.txt", before="l1\nl2\nl3\n", after=None, op=rs.OP_DELETE)

    changes = await rs.list_turn_changes(db, session_id=sess.id, turn_id=turn.id, workspace=str(ws))
    assert [(c["path"], c["action"], c["additions"], c["deletions"]) for c in changes] == [("old.txt", "deleted", 0, 3)]

    diff = await rs.get_file_diff(db, session_id=sess.id, turn_id=turn.id, workspace=str(ws), path="old.txt")
    assert diff is not None
    assert diff["action"] == "deleted"
    assert diff["before"] == "l1\nl2\nl3\n" and diff["after"] is None


@pytest.mark.asyncio
async def test_binary_create_and_delete_are_distinguished_by_op(db, tmp_path):
    """二进制不存全文，新建 / 删除只能靠 op 区分（旧实现一律当成「修改」）。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    sess = await _session(db, ws)
    turn = await _turn(db, sess)
    (ws / "new.png").write_bytes(b"\x89PNG\x00\x01")
    await _write(db, sess, turn, ws, "new.png", before=None, after=None, op=rs.OP_CREATE, binary=True)
    await _write(db, sess, turn, ws, "gone.png", before=None, after=None, op=rs.OP_DELETE, binary=True)
    changes = await rs.list_turn_changes(db, session_id=sess.id, turn_id=turn.id, workspace=str(ws))
    assert {c["path"]: c["action"] for c in changes} == {"new.png": "added", "gone.png": "deleted"}


@pytest.mark.asyncio
async def test_recreated_after_delete_is_modified(db, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    sess = await _session(db, ws)
    turn = await _turn(db, sess, file_list=[{"ckpt": "", "path": "f.txt"}])
    await _write(db, sess, turn, ws, "f.txt", before="old\n", after=None, op=rs.OP_DELETE)
    (ws / "f.txt").write_text("new\n", encoding="utf-8")
    await _write(db, sess, turn, ws, "f.txt", before=None, after="new\n", op=rs.OP_CREATE)
    changes = await rs.list_turn_changes(db, session_id=sess.id, turn_id=turn.id, workspace=str(ws))
    assert [(c["path"], c["action"]) for c in changes] == [("f.txt", "modified")]


@pytest.mark.asyncio
async def test_revert_lock_ignores_net_zero_turns(db, tmp_path):
    """只留下零净变更记录的轮次没有变更卡片，也不能拦住更早一轮的撤销。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    sess = await _session(db, ws)
    turn1 = await _turn(db, sess, file_list=[{"ckpt": "", "path": "a.txt"}])
    (ws / "a.txt").write_text("mod\n", encoding="utf-8")
    await _write(db, sess, turn1, ws, "a.txt", before="orig\n", after="mod\n", op=rs.OP_MODIFY)
    turn2 = await _turn(db, sess, new_files=["tmp.js"])
    await _write(db, sess, turn2, ws, "tmp.js", before=None, after="x\n", op=rs.OP_CREATE)
    await _write(db, sess, turn2, ws, "tmp.js", before="x\n", after=None, op=rs.OP_DELETE)

    lock = await rs.revert_lock(db, sess.id)
    assert lock == {"latest_turn_id": turn1.id, "pending_turn_ids": [turn1.id]}
    # 更晚的 turn2 没有净变更：turn1 可以直接撤销（要求 ckpt 缺失时文本记录退回写前文本）
    res = await rs.revert_turn_files(db, turn_id=turn1.id)
    assert res["ok"] is True, res
    assert (ws / "a.txt").read_text(encoding="utf-8") == "orig\n"


# ── 撤销：严格校验 ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_revert_refuses_when_manual_edit_after_turn(db, tmp_path):
    """用户要求：该轮改了文件、之后又有手动改动 → 撤销报错、拒绝，并说明原因，文件原样不动。
    即使手动改动与 AI 改动完全不重叠（此前会被三路合并静默吞掉）。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    sess = await _session(db, ws)
    turn = await _turn(db, sess, file_list=[{"ckpt": "", "path": "a.txt"}])
    (ws / "a.txt").write_text("top\nai line\nbottom\n", encoding="utf-8")
    await _write(db, sess, turn, ws, "a.txt", before="top\nbottom\n", after="top\nai line\nbottom\n", op=rs.OP_MODIFY)
    # 之后用户在文件末尾手动追加了一行（与 AI 改的行不重叠）
    (ws / "a.txt").write_text("top\nai line\nbottom\nmanual tail\n", encoding="utf-8")

    res = await rs.revert_turn_files(db, turn_id=turn.id)
    assert res["ok"] is False
    assert res["code"] == "conflict"
    assert [c["path"] for c in res["conflicts"]] == ["a.txt"]
    assert "修改过" in res["conflicts"][0]["reason"]
    assert "未修改任何文件" in res["reason"]
    assert (ws / "a.txt").read_text(encoding="utf-8") == "top\nai line\nbottom\nmanual tail\n"


@pytest.mark.asyncio
async def test_revert_refuses_manual_edit_legacy_records_without_hash(db, tmp_path):
    """旧记录没有指纹时退回文本比较，同样拒绝（此前非重叠改动会被合并）。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    sess = await _session(db, ws)
    turn = await _turn(db, sess, file_list=[{"ckpt": "", "path": "a.txt"}])
    await rs.record_turn_write(db, session_id=sess.id, turn_id=turn.id, tool="fs_write",
                               path="a.txt", before="top\nbottom\n", after="top\nai line\nbottom\n")
    (ws / "a.txt").write_text("top\nai line\nbottom\nmanual tail\n", encoding="utf-8")
    res = await rs.revert_turn_files(db, turn_id=turn.id)
    assert res["ok"] is False and res["code"] == "conflict"


@pytest.mark.asyncio
async def test_revert_refuses_when_file_deleted_after_turn(db, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    sess = await _session(db, ws)
    turn = await _turn(db, sess, file_list=[{"ckpt": "", "path": "a.txt"}])
    (ws / "a.txt").write_text("ai\n", encoding="utf-8")
    await _write(db, sess, turn, ws, "a.txt", before="orig\n", after="ai\n", op=rs.OP_MODIFY)
    (ws / "a.txt").unlink()  # 之后被手动删掉
    res = await rs.revert_turn_files(db, turn_id=turn.id)
    assert res["ok"] is False and res["code"] == "conflict"
    assert "已被删除" in res["conflicts"][0]["reason"]


@pytest.mark.asyncio
async def test_revert_refuses_when_deleted_file_was_recreated(db, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    sess = await _session(db, ws)
    turn = await _turn(db, sess, file_list=[{"ckpt": "", "path": "a.txt"}])
    await _write(db, sess, turn, ws, "a.txt", before="orig\n", after=None, op=rs.OP_DELETE)
    (ws / "a.txt").write_text("someone recreated\n", encoding="utf-8")
    res = await rs.revert_turn_files(db, turn_id=turn.id)
    assert res["ok"] is False and res["code"] == "conflict"
    assert "重新创建" in res["conflicts"][0]["reason"]
    assert (ws / "a.txt").read_text(encoding="utf-8") == "someone recreated\n"


@pytest.mark.asyncio
async def test_revert_created_file_deletes_it_and_manual_edit_blocks(db, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    sess = await _session(db, ws)
    turn = await _turn(db, sess, new_files=["new.txt"])
    (ws / "new.txt").write_text("ai\n", encoding="utf-8")
    await _write(db, sess, turn, ws, "new.txt", before=None, after="ai\n", op=rs.OP_CREATE)

    (ws / "new.txt").write_text("ai\nmanual\n", encoding="utf-8")
    res = await rs.revert_turn_files(db, turn_id=turn.id)
    assert res["ok"] is False and res["code"] == "conflict"
    assert (ws / "new.txt").exists()

    (ws / "new.txt").write_text("ai\n", encoding="utf-8")  # 手动改动撤回后，撤销可以执行
    res = await rs.revert_turn_files(db, turn_id=turn.id)
    assert res["ok"] is True
    assert res["deleted"] == 1
    assert not (ws / "new.txt").exists()


@pytest.mark.asyncio
async def test_revert_restores_deleted_file_from_checkpoint_bytes(db, tmp_path):
    """删除后撤销：按执行前的备份字节恢复（GBK / 二进制不经文本解码，不会被改坏）。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    original = "中文内容\n".encode("gb18030")
    ckpt = tmp_path / "ckpt_old.txt"
    ckpt.write_bytes(original)
    sess = await _session(db, ws)
    turn = await _turn(db, sess, file_list=[{"ckpt": str(ckpt), "path": "old.txt"}])
    # 文本记录的写前内容是有损解码的结果，恢复不能依赖它
    await _write(db, sess, turn, ws, "old.txt", before="��\n", after=None, op=rs.OP_DELETE)
    assert not (ws / "old.txt").exists()

    res = await rs.revert_turn_files(db, turn_id=turn.id)
    assert res["ok"] is True, res
    assert res["restored"] == 1
    assert (ws / "old.txt").read_bytes() == original


@pytest.mark.asyncio
async def test_revert_binary_delete_without_backup_is_refused(db, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    sess = await _session(db, ws)
    turn = await _turn(db, sess, file_list=[{"ckpt": "", "path": "a.png"}])
    await _write(db, sess, turn, ws, "a.png", before=None, after=None, op=rs.OP_DELETE, binary=True)
    res = await rs.revert_turn_files(db, turn_id=turn.id)
    assert res["ok"] is False and res["code"] == "conflict"
    assert "备份" in res["conflicts"][0]["reason"]


@pytest.mark.asyncio
async def test_multi_turn_gap_refuses_rollback_chain(db, tmp_path):
    """回滚多轮：两轮之间文件被手动改过 → 衔接校验不通过，整体拒绝。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    ck2 = tmp_path / "ck2.txt"
    sess = await _session(db, ws)
    # turn1：orig → t1
    t1 = await _turn(db, sess, file_list=[{"ckpt": "", "path": "a.txt"}])
    (ws / "a.txt").write_text("t1\n", encoding="utf-8")
    await _write(db, sess, t1, ws, "a.txt", before="orig\n", after="t1\n", op=rs.OP_MODIFY)
    # 两轮之间用户手动改了文件
    (ws / "a.txt").write_text("t1 + user edit\n", encoding="utf-8")
    # turn2：基于用户改过的内容再改（备份里是用户改过的样子）
    ck2.write_text("t1 + user edit\n", encoding="utf-8")
    t2 = await _turn(db, sess, file_list=[{"ckpt": str(ck2), "path": "a.txt"}])
    (ws / "a.txt").write_text("t2\n", encoding="utf-8")
    await _write(db, sess, t2, ws, "a.txt", before="t1 + user edit\n", after="t2\n", op=rs.OP_MODIFY)

    writes = await rs.list_turn_writes(db, sess.id, t1.id)
    plan = rs.build_plan(str(ws), writes, lambda rec: ck2.as_posix() if rec.turn_id == t2.id else None)
    assert [c["path"] for c in rs._conflict_list(plan)] == ["a.txt"]
    assert "相邻两轮" in rs._conflict_list(plan)[0]["reason"]
    assert plan.finals == {}


# ── 轮次收尾对账 ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reconcile_records_untracked_delete(db, tmp_path):
    """本轮新建的临时文件被脚本删掉（命令文本里解析不到）：收尾对账补记删除，清单随之不再显示它。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    sess = await _session(db, ws)
    turn = await _turn(db, sess, new_files=["tmp.js"])
    (ws / "tmp.js").write_text("x\n", encoding="utf-8")
    await _write(db, sess, turn, ws, "tmp.js", before=None, after="x\n", op=rs.OP_CREATE)
    assert len(await rs.list_turn_changes(db, session_id=sess.id, turn_id=turn.id, workspace=str(ws))) == 1

    (ws / "tmp.js").unlink()  # 被 `node cleanup.js` 之类删掉：没有任何记录
    added = await rs.reconcile_turn_writes(db, session_id=sess.id, turn_id=turn.id, workspace=str(ws))
    assert added == 1
    assert await rs.list_turn_changes(db, session_id=sess.id, turn_id=turn.id, workspace=str(ws)) == []


@pytest.mark.asyncio
async def test_reconcile_records_external_modification_and_skips_unchanged(db, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    sess = await _session(db, ws)
    turn = await _turn(db, sess, file_list=[{"ckpt": "", "path": "a.txt"}, {"ckpt": "", "path": "b.txt"}])
    (ws / "a.txt").write_text("ai a\n", encoding="utf-8")
    (ws / "b.txt").write_text("ai b\n", encoding="utf-8")
    await _write(db, sess, turn, ws, "a.txt", before="orig a\n", after="ai a\n", op=rs.OP_MODIFY)
    await _write(db, sess, turn, ws, "b.txt", before="orig b\n", after="ai b\n", op=rs.OP_MODIFY)
    (ws / "a.txt").write_text("ai a\nrewritten by a script\n", encoding="utf-8")

    added = await rs.reconcile_turn_writes(db, session_id=sess.id, turn_id=turn.id, workspace=str(ws))
    assert added == 1  # 只有被改写的 a.txt 补记，b.txt 与记录一致
    changes = await rs.list_turn_changes(db, session_id=sess.id, turn_id=turn.id, workspace=str(ws))
    by_path = {c["path"]: c for c in changes}
    # 清单口径：a.txt 的末态是脚本改写后的内容（相对首条写前的 orig a 为 2 行新增）
    assert by_path["a.txt"]["action"] == "modified"
    assert by_path["a.txt"]["additions"] == 2 and by_path["a.txt"]["deletions"] == 1


@pytest.mark.asyncio
async def test_reconcile_is_noop_without_records_or_workspace(db, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    sess = await _session(db, ws)
    turn = await _turn(db, sess)
    assert await rs.reconcile_turn_writes(db, session_id=sess.id, turn_id=turn.id, workspace=str(ws)) == 0
    assert await rs.reconcile_turn_writes(db, session_id=sess.id, turn_id=turn.id, workspace="") == 0


# ── 备份登记 ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_checkpoint_returns_whether_newly_registered(db, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    sess = await _session(db, ws)
    turn = await _turn(db, sess)
    assert await rs.record_checkpoint_for_turn(db, turn.id, "/c/one", "a.txt") is True
    assert await rs.record_checkpoint_for_turn(db, turn.id, "/c/two", "a.txt") is False  # 同文件本轮已备份：去重
    assert await rs.record_checkpoint_for_turn(db, turn.id, "", "n.txt", new_file="n.txt") is True
    assert await rs.record_checkpoint_for_turn(db, turn.id, "", "n.txt", new_file="n.txt") is False
    assert await rs.record_checkpoint_for_turn(db, turn.id, "", "x.txt") is False  # 无备份也无新文件：不登记


def test_checkpoint_skips_directories_and_discard_is_safe(workspace):
    (workspace / "d").mkdir()
    assert rs.checkpoint_file(str(workspace), "d") is None
    f = workspace / "f.txt"
    f.write_text("v1", encoding="utf-8")
    ck = rs.checkpoint_file(str(workspace), "f.txt", session_id=1, turn_id=2)
    assert ck is not None
    rs.discard_checkpoint(ck)
    rs.discard_checkpoint(ck)  # 重复删除不抛
    rs.discard_checkpoint(None)
    assert not __import__("pathlib").Path(ck).exists()
