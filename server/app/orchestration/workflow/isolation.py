"""工作流的 git 工作树隔离（设计 §5A.2）：子工作流在独立的工作树与分支中执行，结束时并入主工作区。

- prepare：以主工作区当前的状态为起点创建工作树与分支。
  主工作区干净时以 HEAD 为起点；有未提交的改动（含未跟踪的新文件）时，先把当前状态保存为一个快照提交
  （父提交为 HEAD，只借助临时索引，主工作区与它自己的索引都不会被改动），工作树从快照出发，
  子工作流看到的就是用户现在的改动。快照提交的编号记录在分支的配置里，工作树被清理后仍然可查。
  路径由运行编号与子工作流键决定，进程重启后可以找回。
- integrate：
  - 没有快照基线（主工作区干净时创建）：在工作树里提交本次改动，把主分支并入工作树分支，
    冲突留在工作树中交给合并步骤解决；最后在主工作区做快进合并，主工作区只接受无冲突的快进。
  - 有快照基线：只把子工作流的改动（相对快照）应用到主工作区，不提交、不快进；
    与用户的改动重叠时整体拒绝，用户的改动保持原样，主工作区不做部分写入。
- remove：成功并入后删除工作树与分支；放弃时只删除工作树，分支保留以便查看。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

WORKTREE_DIR = Path(".chatcoder") / "worktrees"
# 应用自己在项目里写的运行时文件：不计入用户的改动，也不进入快照与同步
_APP_OWNED_PATHS = (
    f"{WORKTREE_DIR.as_posix()}/",
    ".chatcoder/index_state.db",
    ".chatcoder/index_state.db-journal",
    ".chatcoder/index_state.db-wal",
    ".chatcoder/index_state.db-shm",
)
_CONFLICT_MARKER = re.compile(r"^(<{7}|={7}|>{7})(\s|$)", re.MULTILINE)
_BASELINE_NOTE = "chatcoder: 子工作流基线（主工作区快照）"
_LOCKS: dict[str, asyncio.Lock] = {}


@dataclass(frozen=True)
class Worktree:
    path: str  # 子工作流的工作区（工作树根 + 主工作区在仓库中的相对路径）
    root: str  # 工作树根目录
    branch: str
    baseline: str | None = None  # 快照基线（以主工作区的未提交改动为起点时才有）


@dataclass(frozen=True)
class Integration:
    status: str  # merged / conflict / blocked
    files: tuple[str, ...] = ()  # conflict / blocked：涉及的文件（相对仓库根）
    message: str = ""
    committed: bool = True  # merged：改动是否已提交到主分支（快照路径下为未提交的工作区改动）


class WorktreeUnavailable(RuntimeError):
    """无法为子工作流准备独立工作树（附具体原因）：调用方据此回落为共享工作区，并把原因写进运行日志。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def branch_name(run_id: int, flow_key: str) -> str:
    return f"chatcoder/wf-{run_id}-{flow_key}"


def _baseline_key(branch: str) -> str:
    return f"branch.{branch}.chatcoderBaseline"


def _origin_key(branch: str) -> str:
    return f"branch.{branch}.chatcoderOrigin"


def _lock_for(repo: str) -> asyncio.Lock:
    key = os.path.normcase(os.path.realpath(repo))
    lock = _LOCKS.get(key)
    if lock is None:
        lock = _LOCKS[key] = asyncio.Lock()
    return lock


async def _git(
    cwd: str, *args: str, strip: bool = True, env: dict[str, str] | None = None
) -> tuple[int, str]:
    rc, out = await _git_bytes(cwd, *args, env=env)
    text = out.decode("utf-8", "replace")
    return rc, text.strip() if strip else text.rstrip("\n")


async def _git_bytes(cwd: str, *args: str, env: dict[str, str] | None = None) -> tuple[int, bytes]:
    full_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})}
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            cwd,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=full_env,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
    except (OSError, asyncio.TimeoutError) as exc:
        return 1, f"{type(exc).__name__}: {exc}".encode()
    return proc.returncode or 0, out


async def _identity_args(cwd: str) -> list[str]:
    """仓库没有配置提交身份时，为本次提交临时补上（不写入用户的 git 配置）。"""
    args: list[str] = []
    if (await _git(cwd, "config", "user.email"))[0] != 0:
        args += ["-c", "user.email=chatcoder@localhost"]
    if (await _git(cwd, "config", "user.name"))[0] != 0:
        args += ["-c", "user.name=chatcoder"]
    return args


def _ensure_excluded(top: str, git_path_info: str) -> None:
    """把应用自己的运行时文件加入 .git/info/exclude：工作树目录与代码索引库不属于用户的改动。"""
    exclude = Path(git_path_info) if os.path.isabs(git_path_info) else Path(top) / git_path_info
    try:
        current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        missing = [line for line in _APP_OWNED_PATHS if line not in current.splitlines()]
        if missing:
            exclude.parent.mkdir(parents=True, exist_ok=True)
            prefix = "" if not current or current.endswith("\n") else "\n"
            with exclude.open("a", encoding="utf-8") as handle:
                handle.write(prefix + "".join(line + "\n" for line in missing))
    except OSError:
        pass  # 排除规则只影响主工作区的状态展示，失败不影响隔离本身


def _registered_worktrees(listing: str) -> set[str]:
    out: set[str] = set()
    for line in listing.splitlines():
        if line.startswith("worktree "):
            out.add(os.path.normcase(os.path.realpath(line[len("worktree ") :].strip())))
    return out


async def baseline_of(main: str, branch: str) -> str | None:
    """分支记录的快照基线；没有记录（以干净的 HEAD 创建）时返回 None。"""
    rc, out = await _git(main, "config", "--get", _baseline_key(branch))
    return out if rc == 0 and out else None


async def _snapshot_commit(main: str, head: str) -> str | None:
    """把主工作区当前的状态（含未提交与未跟踪的文件）保存为一个提交，父提交为 HEAD。

    借助临时索引完成：主工作区与它自己的索引都不会被改动；已忽略的文件不纳入。
    """
    ident = await _identity_args(main)
    with tempfile.TemporaryDirectory(prefix="chatcoder-snapshot-") as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        for args in (("read-tree", head), ("add", "-A")):
            rc, _ = await _git(main, *args, env=env)
            if rc != 0:
                return None
        rc, tree = await _git(main, "write-tree", env=env)
        if rc != 0 or not tree:
            return None
        rc, commit = await _git(
            main, *ident, "commit-tree", tree, "-p", head, "-m", _BASELINE_NOTE, env=env
        )
    return commit if rc == 0 and commit else None


async def prepare(main: str, *, run_id: int, flow_key: str) -> Worktree:
    """为子工作流准备工作树与分支。无法准备时抛出 WorktreeUnavailable（附原因）。"""
    async with _lock_for(main):
        rc, top = await _git(main, "rev-parse", "--show-toplevel")
        if rc != 0 or not top:
            raise WorktreeUnavailable("工作区不是 git 仓库")
        rc, prefix = await _git(main, "rev-parse", "--show-prefix")
        if rc != 0:
            raise WorktreeUnavailable("工作区不是 git 仓库")
        rc, head = await _git(main, "rev-parse", "--verify", "HEAD")
        if rc != 0:
            raise WorktreeUnavailable("仓库还没有任何提交")
        rc, info = await _git(main, "rev-parse", "--git-path", "info/exclude")
        if rc == 0 and info:
            _ensure_excluded(top, info)
        branch = branch_name(run_id, flow_key)
        root = Path(top) / WORKTREE_DIR / f"{run_id}-{flow_key}"
        _, listing = await _git(main, "worktree", "list", "--porcelain")
        registered = os.path.normcase(os.path.realpath(root)) in _registered_worktrees(listing)
        if registered and root.exists():
            pass  # 已挂载（例如进程重启后续跑）：直接复用
        else:
            if registered:
                await _git(main, "worktree", "prune")
            if root.exists():
                shutil.rmtree(root, ignore_errors=True)
            root.parent.mkdir(parents=True, exist_ok=True)
            rc, _ = await _git(main, "rev-parse", "--verify", f"refs/heads/{branch}")
            if rc == 0:
                rc, out = await _git(main, "worktree", "add", str(root), branch)
            else:
                base, snapshot = head, None
                if await has_local_changes(main):
                    snapshot = await _snapshot_commit(main, head)
                    if snapshot is None:
                        raise WorktreeUnavailable("无法为主工作区的当前改动创建快照")
                    base = snapshot
                    # 基线写在分支的配置里：工作树被清理后，分支仍能判断如何并入
                    await _git(main, "config", _baseline_key(branch), snapshot)
                # 起点记录在分支配置里：之后每个节点开始前，据此把主工作区的新改动同步进来
                await _git(main, "config", _origin_key(branch), head)
                rc, out = await _git(main, "worktree", "add", "-b", branch, str(root), base)
                if rc != 0:
                    await _git(main, "config", "--unset", _origin_key(branch))
                    if snapshot is not None:
                        await _git(main, "config", "--unset", _baseline_key(branch))
            if rc != 0:
                raise WorktreeUnavailable(f"无法创建独立工作树：{out[:160]}")
    rel = prefix.strip().strip("/")
    path = str(root / rel) if rel else str(root)
    return Worktree(
        path=path, root=str(root), branch=branch, baseline=await baseline_of(main, branch)
    )


async def origin_of(main: str, branch: str) -> str | None:
    """分支创建时所基于的提交（干净的 HEAD，或快照提交的父提交）；没有记录时返回 None。"""
    rc, out = await _git(main, "config", "--get", _origin_key(branch))
    return out if rc == 0 and out else None


async def sync_main_changes(main: str, worktree: str, *, branch: str) -> Integration:
    """子工作流的节点开始前调用：把主工作区里新出现的改动同步进工作树（§5A）。

    以上一次的基线为准（快照基线，或干净的起点）：取主工作区当前状态相对它的差异，应用到工作树，
    再把基线推进到当前状态。差异与工作树里已有的修改重叠时整体拒绝，工作树与基线都保持不变。
    """
    async with _lock_for(main):
        base = await baseline_of(main, branch) or await origin_of(main, branch)
        if base is None:
            return Integration("merged", message="没有需要同步的基线")
        rc, head = await _git(main, "rev-parse", "--verify", "HEAD")
        if rc != 0:
            return Integration("merged", message="主工作区还没有提交，无需同步")
        current = await _snapshot_commit(main, head)
        if current is None:
            return Integration("blocked", message="无法读取主工作区的当前状态")
        _, base_tree = await _git(main, "rev-parse", f"{base}^{{tree}}")
        _, current_tree = await _git(main, "rev-parse", f"{current}^{{tree}}")
        if base_tree == current_tree:
            return Integration("merged", message="主工作区没有新的改动")
        _, names = await _git(main, "diff", "--name-only", base, current)
        files = tuple(line for line in names.splitlines() if line.strip())
        rc, patch = await _git_bytes(main, "diff", "--binary", "--full-index", base, current)
        if rc != 0:
            detail = patch.decode("utf-8", "replace")[:200]
            return Integration("blocked", files=files, message=f"生成主工作区的差异失败：{detail}")
        fd, patch_path = tempfile.mkstemp(prefix="chatcoder-sync-", suffix=".patch")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(patch)
            rc, out = await _git(worktree, "apply", "--check", patch_path)
            if rc != 0:
                return Integration(
                    "blocked",
                    files=files,
                    message=f"主工作区的新改动与子工作流已有的修改重叠，无法同步：{out}",
                )
            rc, out = await _git(worktree, "apply", patch_path)
            if rc != 0:
                return Integration("blocked", files=files, message=f"同步主工作区的改动失败：{out}")
        finally:
            with contextlib.suppress(OSError):
                os.unlink(patch_path)
        await _git(main, "config", _baseline_key(branch), current)
        return Integration("merged", files=files, message="已同步主工作区的新改动", committed=False)


async def local_changes(main: str) -> list[str]:
    """主工作区未提交的改动路径（最多 50 个）。开始独立工作树时它们会以快照的形式带入。"""
    rc_top, top = await _git(main, "rev-parse", "--show-toplevel")
    rc_info, info = await _git(main, "rev-parse", "--git-path", "info/exclude")
    if rc_top == 0 and rc_info == 0 and top and info:
        _ensure_excluded(top, info)
    # 不去掉行首空白：状态码的第一列可能是空格（例如 " M path"）
    rc, out = await _git(main, "status", "--porcelain", strip=False)
    if rc != 0:
        return []
    paths = []
    for line in out.splitlines():
        if len(line) > 3:
            paths.append(line[3:].strip().strip('"'))
    return paths[:50]


async def has_local_changes(main: str) -> bool:
    return bool(await local_changes(main))


async def _merge_in_progress(worktree: str) -> bool:
    return (await _git(worktree, "rev-parse", "--verify", "-q", "MERGE_HEAD"))[0] == 0


async def _unmerged(worktree: str) -> list[str]:
    _, out = await _git(worktree, "diff", "--name-only", "--diff-filter=U")
    return [line for line in out.splitlines() if line.strip()]


def _has_markers(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return bool(_CONFLICT_MARKER.search(text))


async def _relative_to_workspace(worktree: str, repo_paths: list[str]) -> list[str] | None:
    """仓库根相对路径 → 子工作流工作区内的相对路径；有文件不在工作区内时返回 None。"""
    _, prefix = await _git(worktree, "rev-parse", "--show-prefix")
    prefix = prefix.strip()
    out: list[str] = []
    for path in repo_paths:
        if prefix and not path.startswith(prefix):
            return None
        out.append(path[len(prefix) :] if prefix else path)
    return out


async def integrate(main: str, worktree: str, *, branch: str, message: str) -> Integration:
    """把子工作流的改动并入主工作区（见模块说明）。冲突时返回冲突文件，工作树保持在合并中的状态。"""
    async with _lock_for(main):
        ident = await _identity_args(worktree)
        rc, top = await _git(worktree, "rev-parse", "--show-toplevel")
        if rc != 0:
            return Integration("blocked", message=f"工作树不存在：{top}")
        baseline = await baseline_of(main, branch)
        if baseline is not None:
            return await _integrate_from_snapshot(
                main, worktree, baseline=baseline, ident=ident, message=message
            )
        if await _merge_in_progress(worktree):
            # 上一轮的冲突已由合并步骤处理：仍有冲突标记的文件要继续解决
            unresolved = await _unmerged(worktree)
            marked = [p for p in unresolved if _has_markers(Path(top) / p)]
            if marked:
                files = await _relative_to_workspace(worktree, marked)
                if files is None:
                    return Integration("blocked", message="冲突文件不在本子工作流的工作区内")
                return Integration("conflict", files=tuple(files))
            await _git(worktree, "add", "-A")
            rc, out = await _git(worktree, *ident, "commit", "--no-edit")
            if rc != 0:
                return Integration("blocked", message=f"提交合并结果失败：{out}")
        else:
            await _git(worktree, "add", "-A")
            if (await _git(worktree, "diff", "--cached", "--quiet"))[0] != 0:
                rc, out = await _git(worktree, *ident, "commit", "-m", message)
                if rc != 0:
                    return Integration("blocked", message=f"提交子工作流改动失败：{out}")
        # 主分支的最新提交并入工作树分支；冲突留在工作树里交给合并步骤
        _, head_ref = await _git(main, "rev-parse", "--abbrev-ref", "HEAD")
        main_ref = (
            head_ref
            if head_ref and head_ref != "HEAD"
            else (await _git(main, "rev-parse", "HEAD"))[1]
        )
        rc, out = await _git(worktree, *ident, "merge", "--no-edit", main_ref)
        if rc != 0:
            unresolved = await _unmerged(worktree)
            if unresolved:
                files = await _relative_to_workspace(worktree, unresolved)
                if files is None:
                    return Integration("blocked", message="冲突文件不在本子工作流的工作区内")
                return Integration("conflict", files=tuple(files))
            return Integration("blocked", message=f"并入主分支失败：{out}")
        rc, out = await _git(main, "merge", "--ff-only", branch)
        if rc != 0:
            return Integration(
                "blocked", message=f"主工作区无法快进合并（请先提交或暂存本地改动）：{out}"
            )
        return Integration("merged")


async def _integrate_from_snapshot(
    main: str, worktree: str, *, baseline: str, ident: list[str], message: str
) -> Integration:
    """快照基线的并入：子工作流相对快照的差异应用到主工作区，不提交、不快进主分支。"""
    await _git(worktree, "add", "-A")
    if (await _git(worktree, "diff", "--cached", "--quiet"))[0] != 0:
        rc, out = await _git(worktree, *ident, "commit", "-m", message)
        if rc != 0:
            return Integration("blocked", message=f"提交子工作流改动失败：{out}")
    rc, head = await _git(worktree, "rev-parse", "HEAD")
    if rc != 0:
        return Integration("blocked", message=f"读取子工作流的提交失败：{head}")
    if head == baseline:
        return Integration("merged", message="子工作流没有产生需要并入的改动")
    _, names = await _git(worktree, "diff", "--name-only", baseline, head)
    files = tuple(line for line in names.splitlines() if line.strip())
    rc, patch = await _git_bytes(worktree, "diff", "--binary", "--full-index", baseline, head)
    if rc != 0:
        detail = patch.decode("utf-8", "replace")[:200]
        return Integration("blocked", files=files, message=f"生成子工作流的差异失败：{detail}")
    fd, patch_path = tempfile.mkstemp(prefix="chatcoder-apply-", suffix=".patch")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(patch)
        rc, out = await _git(main, "apply", "--check", patch_path)
        if rc != 0:
            return Integration(
                "blocked",
                files=files,
                message=f"主工作区的改动与子工作流的修改重叠，无法直接写入：{out}",
            )
        rc, out = await _git(main, "apply", patch_path)
        if rc != 0:
            return Integration("blocked", files=files, message=f"写入主工作区失败：{out}")
    finally:
        with contextlib.suppress(OSError):
            os.unlink(patch_path)
    return Integration(
        "merged", files=files, message="改动已写入主工作区（未提交）", committed=False
    )


async def remove(main: str, *, worktree: str, branch: str, keep_branch: bool) -> None:
    """删除子工作流的工作树；成功并入后同时删除分支，放弃时保留分支。"""
    async with _lock_for(main):
        _, top = await _git(worktree, "rev-parse", "--show-toplevel")
        if top and os.path.isdir(top):
            await _git(main, "worktree", "remove", "--force", top)
        await _git(main, "worktree", "prune")
        if not keep_branch:
            await _git(main, "branch", "-D", branch)


async def head_of(workspace: str) -> str | None:
    """工作区当前 HEAD 的短提交号；不是 Git 仓库或尚无提交时返回 None。"""
    rc, out = await _git(workspace, "rev-parse", "--short", "HEAD")
    return out if rc == 0 and out else None
