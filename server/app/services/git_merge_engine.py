"""git 原生合并引擎（plan-308-1542 需求3-A）。

设计动机（用户诉求：\"应该优先使用 git 的工具来处理，就像我手动 git merge 一个分支到另一个分支\"）
--------------------------------------------------------------------------------------
改造前的实现是**手写三态比较**：base 取自 `git show <rev>:<path>`（保留仓库里的 CRLF），
ours/theirs 取自 Python `read_text()`（universal newlines 会把 CRLF 归一化成 LF）。
两者口径不一致 ⇒ `o == b` 恒为假 ⇒ 主工作区明明没改，也被判成\"两侧都改\"，
再交给 `git merge-file` 就必然产出冲突标记——这就是用户看到的\"假冲突\"。

本模块把\"判定 + 合并 + 冲突标记\"全部交给 git：
  * `detect()`      —— `git merge-tree --write-tree`（内存判定，不碰工作区/索引）
  * `merge_file()`  —— `git merge-file --diff3`，**返回码分级**（0 干净 / 1 真冲突 / >1 无法处理）
  * `snapshot_dirty()` —— 用**临时索引**把工作区（含未跟踪改动）固化成 commit，
                        使\"未提交改动\"也能参与 git 三方合并，且绝不改动用户索引
  * `merge_in_temp_worktree()` —— 在临时工作树里**真正执行 git merge**，
                        得到 git 原生的 diff3 标记文件与索引 stage1/2/3 三态

安全底线：本模块只创建**临时**索引与**临时**工作树（`.chatcoder/tmp-merge/`），
调用前后用户仓库的 HEAD 与工作区状态必须完全不变。
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

_TIMEOUT = 60
# 临时工作树根目录（相对仓库），与服务名同口径
_TMP_MERGE_DIR = ".chatcoder/tmp-merge"


class GitMergeUnavailable(RuntimeError):
    """git 不支持内存合并（版本过旧）或执行失败——调用方应降级到备用路径。"""


async def _git(cwd: str, *args: str, extra_env: dict | None = None) -> tuple[int, str, str]:
    """执行 git，返回 (returncode, stdout, stderr)。"""
    env = {**os.environ, **(extra_env or {})}
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=cwd, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT)
        return (proc.returncode,
                stdout.decode("utf-8", errors="surrogateescape"),
                stderr.decode("utf-8", errors="surrogateescape"))
    except asyncio.TimeoutError as e:
        raise GitMergeUnavailable(f"git {' '.join(args)} 超时") from e
    except FileNotFoundError as e:
        raise GitMergeUnavailable("系统未安装 git") from e


def is_binary_bytes(data: bytes) -> bool:
    """按 git 的口径判二进制：内容中含 NUL 字节。"""
    return b"\x00" in data[:8000]


def _looks_binary_text(text: str) -> bool:
    """文本里出现 surrogate（surrogateescape 解码二进制产物）时视为二进制。"""
    return "\udc00" <= (text or "")[:8000]


# ───────────────────────────────────────────────────────────────
# 1) 内存判定：git merge-tree
# ───────────────────────────────────────────────────────────────

async def detect(repo: str, ours_rev: str, theirs_rev: str) -> dict:
    """用 `git merge-tree --write-tree --name-only -z` 判定合并结果（不改动任何东西）。

    returncode：0 = 干净合并；1 = 有冲突；>1 = 不支持/失败 → 抛 GitMergeUnavailable。

    输出格式（-z，NUL 分隔）：
      干净：`<tree-oid>\0`
      冲突：`<tree-oid>\0<path>\0...\0` 随后是若干信息段（含 "CONFLICT" 描述）
    """
    rc, out, err = await _git(repo, "merge-tree", "--write-tree", "--name-only", "-z",
                              ours_rev, theirs_rev)
    if rc > 1:
        raise GitMergeUnavailable(f"git merge-tree 不可用（rc={rc}）: {(err or out)[:200]}")

    parts = [p for p in out.split("\0") if p]
    tree_oid = parts[0] if parts else ""
    conflicted = [p for p in parts[1:] if "/" in p or "." in p]
    # -z 输出里除首行 tree OID 外，冲突路径在最前面；后续信息段含空格描述（如 "CONFLICT (content)"）
    conflicted = [p.replace("\\", "/") for p in conflicted if not p.startswith("CONFLICT") and " " not in p]
    return {
        "clean": rc == 0,
        "exit_code": rc,
        "tree": tree_oid,
        "conflicted": sorted(set(conflicted)),
        "raw": out[:2000],
    }


async def diff_files(repo: str, ours_rev: str, theirs_rev: str) -> list[dict]:
    """两侧之间变更的文件清单（`git diff --name-status -z`）。"""
    rc, out, err = await _git(repo, "diff", "--name-status", "-z", ours_rev, theirs_rev)
    if rc != 0:
        raise GitMergeUnavailable(f"git diff 失败: {(err or out)[:200]}")
    toks = [t for t in out.split("\0") if t]
    files: list[dict] = []
    i = 0
    while i < len(toks):
        status = toks[i].strip()
        i += 1
        if i >= len(toks):
            break
        path = toks[i].replace("\\", "/")
        i += 1
        files.append({"path": path, "status": status[:1]})
    return files


async def show_file(repo: str, rev: str, rel: str) -> str | None:
    """取某 revision 下某文件的内容（不存在返回 None）。二进制按 surrogateescape 解码。"""
    rc, out, _ = await _git(repo, "show", f"{rev}:{rel}")
    return out if rc == 0 else None


def _status_of(letter: str) -> str:
    return {"A": "added", "D": "deleted", "M": "modified", "R": "renamed", "C": "copied"}.get(letter, "modified")


# ───────────────────────────────────────────────────────────────
# 2) 单文件三方合并：git merge-file（返回码分级）
# ───────────────────────────────────────────────────────────────

async def merge_file(ours: str | None, base: str | None, theirs: str | None) -> dict:
    """`git merge-file --diff3` 三方合并单文件内容。

    返回码语义（这是本模块的关键修复点）：
      0 → 干净合并（content 可直接采用）
      1 → 真冲突（content 是 git 生成的 diff3 标记）
      >1 → git 无法处理（如二进制/参数问题）→ **不阻断**：降级为采用来源侧并记 reason
    """
    if theirs is None:
        return {"clean": True, "content": None, "deleted": True, "returncode": 0, "reason": ""}
    if base is None or ours is None:
        # 新增/删除文件：git 的语义就是采用存在的一侧
        return {"clean": True, "content": theirs, "deleted": False, "returncode": 0, "reason": ""}

    td = Path(tempfile.mkdtemp(prefix="chatcoder-mergefile-"))
    try:
        fo, fb, ft = td / "ours.txt", td / "base.txt", td / "theirs.txt"
        # newline="" 保证字节级写入，不做任何换行转换（口径统一由 git 负责）
        for p, text in ((fo, ours), (fb, base), (ft, theirs)):
            p.write_bytes(text.encode("utf-8", errors="surrogateescape"))
        rc, out, err = await _git(
            str(td), "merge-file", "--diff3", "-p",
            "-L", "主工作区(ours)", "-L", "base", "-L", "工作树(theirs)",
            str(fo), str(fb), str(ft),
        )
    finally:
        shutil.rmtree(td, ignore_errors=True)

    if rc == 0:
        return {"clean": True, "content": out, "deleted": False, "returncode": 0, "reason": ""}
    if rc == 1:
        return {"clean": False, "content": out, "deleted": False, "returncode": 1, "reason": "git 判定为真冲突"}
    # rc > 1：git 自己处理不了（多为二进制）；不当作冲突阻断，采用来源侧并说明原因
    return {"clean": True, "content": theirs, "deleted": False, "returncode": rc,
            "reason": f"git merge-file 无法处理（rc={rc}），已自动采用来源侧: {(err or '')[:120]}"}


# ───────────────────────────────────────────────────────────────
# 3) 工作区快照（临时索引，含未提交改动，不碰用户索引）
# ───────────────────────────────────────────────────────────────

async def snapshot_dirty(repo: str) -> str | None:
    """把工作区当前内容（含未跟踪、尊重 .gitignore）固化成 commit OID；无改动返回 None。

    实现用**临时索引**（GIT_INDEX_FILE）：read-tree HEAD → add -A → write-tree → commit-tree。
    全程不写用户索引、不动工作区，因此调用前后 `git status` 与 HEAD 完全不变。
    """
    tmp_index = Path(tempfile.mkdtemp(prefix="chatcoder-idx-")) / "index"
    env = {"GIT_INDEX_FILE": str(tmp_index)}
    try:
        rc, out, err = await _git(repo, "read-tree", "HEAD", extra_env=env)
        if rc != 0:
            return None
        rc, out, err = await _git(repo, "add", "-A", extra_env=env)
        if rc != 0:
            return None
        rc, tree, err = await _git(repo, "write-tree", extra_env=env)
        if rc != 0 or not tree.strip():
            return None
        _rc2, head, _ = await _git(repo, "rev-parse", "HEAD")
        if head.strip() and tree.strip() == (await _head_tree(repo, head.strip())):
            return None  # 与 HEAD 一致 → 无改动
        rc, commit, err = await _git(
            repo, "-c", "user.name=chatcoder", "-c", "user.email=chatcoder@local",
            "commit-tree", tree.strip(), "-p", head.strip() or "HEAD",
            "-m", "chatcoder: merge snapshot", extra_env=env,
        )
        return commit.strip() if rc == 0 and commit.strip() else None
    except GitMergeUnavailable:
        return None
    finally:
        shutil.rmtree(tmp_index.parent, ignore_errors=True)


async def _head_tree(repo: str, rev: str) -> str:
    rc, out, _ = await _git(repo, "rev-parse", f"{rev}^{{tree}}")
    return out.strip() if rc == 0 else ""


async def has_commits(repo: str) -> bool:
    rc, out, _ = await _git(repo, "rev-parse", "--verify", "HEAD")
    return rc == 0 and bool(out.strip())


# ───────────────────────────────────────────────────────────────
# 4) 临时工作树里真正执行 git merge（拿 diff3 标记与索引三态）
# ───────────────────────────────────────────────────────────────

async def merge_in_temp_worktree(repo: str, ours_rev: str, theirs_rev: str,
                                 merge_id: str, *, keep: bool = False) -> dict:
    """在临时工作树里 `git merge --no-commit`，返回冲突清单与三态内容。

    keep=True 时保留临时目录（供后续读取），调用方负责清理。
    """
    base_dir = Path(repo) / _TMP_MERGE_DIR
    base_dir.mkdir(parents=True, exist_ok=True)
    wt_dir = base_dir / merge_id
    if wt_dir.exists():
        shutil.rmtree(wt_dir, ignore_errors=True)

    rc, out, err = await _git(repo, "worktree", "add", "--detach", "--force", str(wt_dir), ours_rev)
    if rc != 0:
        raise GitMergeUnavailable(f"创建临时工作树失败: {(err or out)[:200]}")

    result: dict = {"dir": str(wt_dir), "conflicted": [], "status_map": {}, "ok": False}
    try:
        rc, out, err = await _git(
            str(wt_dir), "-c", "merge.conflictStyle=diff3",
            "-c", "user.name=chatcoder", "-c", "user.email=chatcoder@local",
            "merge", "--no-commit", "--no-ff", theirs_rev,
        )
        result["ok"] = rc == 0
        result["exit_code"] = rc
        # 冲突清单与 stage 三态
        rc2, uout, _ = await _git(str(wt_dir), "ls-files", "-u")
        conflicted: list[str] = []
        for ln in (uout or "").splitlines():
            parts = ln.split("\t", 1)
            if len(parts) == 2:
                p = parts[1].strip().replace("\\", "/")
                if p and p not in conflicted:
                    conflicted.append(p)
        result["conflicted"] = conflicted
        # 变更状态（新增/修改/删除）
        rc3, nout, _ = await _git(str(wt_dir), "diff", "--name-status", "-z", ours_rev)
        if rc3 == 0 and nout:
            toks = [t for t in nout.split("\0") if t]
            i = 0
            while i + 1 < len(toks):
                st, p = toks[i].strip()[:1], toks[i + 1].replace("\\", "/")
                result["status_map"][p] = _status_of(st)
                i += 2
        return result
    finally:
        if not keep:
            await cleanup_temp_worktree(repo, merge_id)


async def stages_in_temp_worktree(wt_dir: str, rel: str) -> dict:
    """读临时工作树里某文件的 base/ours/theirs（索引 stage1/2/3）。"""
    out: dict = {"base": None, "ours": None, "theirs": None}
    for stage, key in ((1, "base"), (2, "ours"), (3, "theirs")):
        rc, txt, _ = await _git(wt_dir, "show", f":{stage}:{rel}")
        out[key] = txt if rc == 0 else None
    return out


async def conflict_content_in_temp(wt_dir: str, rel: str) -> str | None:
    """读临时工作树里冲突文件（即 git 生成的 diff3 标记内容）。"""
    p = Path(wt_dir) / rel
    try:
        if not p.is_file():
            return None
        return p.read_bytes().decode("utf-8", errors="surrogateescape")
    except OSError:
        return None


async def cleanup_temp_worktree(repo: str, merge_id: str) -> None:
    """清理临时工作树（先 abort 未完成的 merge，再 remove + prune）。"""
    wt_dir = Path(repo) / _TMP_MERGE_DIR / merge_id
    if wt_dir.exists():
        try:
            await _git(str(wt_dir), "merge", "--abort")
        except GitMergeUnavailable:
            pass
        await _git(repo, "worktree", "remove", "--force", "--force", str(wt_dir))
        if wt_dir.exists():
            shutil.rmtree(wt_dir, ignore_errors=True)
    await _git(repo, "worktree", "prune")


async def cleanup_all_temp_merges(repo: str) -> int:
    """清理所有遗留的临时合并工作树（服务启动/异常残留时调用）。返回清理数量。"""
    base = Path(repo) / _TMP_MERGE_DIR
    if not base.is_dir():
        return 0
    n = 0
    for d in list(base.iterdir()):
        if d.is_dir():
            await cleanup_temp_worktree(repo, d.name)
            n += 1
    shutil.rmtree(base, ignore_errors=True)
    return n
