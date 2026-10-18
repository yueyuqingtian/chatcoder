"""Git 常规操作服务（plan-20261017-goal12 项 5）。

面向右侧面板的「Git」标签页：仓库状态、变更差异、暂存 / 取消暂存 / 丢弃、提交、
拉取 / 推送 / 抓取、提交历史、分支（切换 / 新建 / 删除）、冲突处理（采用我方 / 对方 / 中止合并）。

安全约定：
  - 所有命令以非交互方式运行（GIT_TERMINAL_PROMPT=0、GIT_EDITOR=true），凭据缺失时直接失败而不是挂起；
  - 路径参数必须是仓库内的相对路径，拒绝 `..`、绝对路径与盘符；
  - 丢弃变更、删除分支等不可逆操作只在这里执行，前端必须先确认；
  - 输出做长度上限，超大 diff 截断并标注，避免阻塞界面。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

LOCAL_TIMEOUT = 30
NETWORK_TIMEOUT = 180
MAX_DIFF_BYTES = 400_000
MAX_NEW_FILE_BYTES = 256_000
_UNMERGED = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}


class GitError(Exception):
    """可直接展示给用户的错误（已是中文或 git 原文）。"""


async def _run(cwd: str, *args: str, timeout: int = LOCAL_TIMEOUT, stdin: bytes | None = None) -> tuple[int, str, str]:
    env = dict(os.environ)
    env.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "never",
        "GIT_EDITOR": "true",
        "GIT_PAGER": "cat",
        "LC_ALL": "C.UTF-8",
    })
    cmd = ["git", "-c", "core.quotepath=off", "-c", "color.ui=never", "--no-pager", *args]
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd, env=env,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout=timeout)
    except asyncio.TimeoutError:
        # 超时必须结束子进程：否则卡住的 git（如等待凭据）会常驻并持有 index.lock，阻塞之后所有操作
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
        raise GitError(f"git 命令超时（>{timeout}s），可能是网络或凭据问题") from None
    except FileNotFoundError:
        raise GitError("系统未安装 git，请先安装后再使用 Git 功能") from None
    except OSError as e:
        raise GitError(f"git 执行失败：{e}") from None
    return proc.returncode or 0, out.decode("utf-8", errors="replace"), err.decode("utf-8", errors="replace")


async def _must(cwd: str, *args: str, timeout: int = LOCAL_TIMEOUT, stdin: bytes | None = None) -> str:
    rc, out, err = await _run(cwd, *args, timeout=timeout, stdin=stdin)
    if rc != 0:
        raise GitError((err or out).strip() or f"git {args[0]} 失败（退出码 {rc}）")
    return out


def _safe_rel(path: str) -> str:
    """仓库内相对路径校验：拒绝空串、绝对路径、盘符与上级引用。"""
    norm = path.replace("\\", "/").strip()
    if not norm or norm.startswith("/") or re.match(r"^[A-Za-z]:", norm) or ".." in norm.split("/"):
        raise GitError(f"非法路径：{path}")
    return norm


def _safe_paths(paths: list[str]) -> list[str]:
    if not paths:
        raise GitError("请先选择文件")
    return [_safe_rel(p) for p in paths]


async def repo_root(path: str) -> str:
    """返回仓库根目录；不是 Git 仓库时给出中文提示。"""
    if not path or not Path(path).is_dir():
        raise GitError("项目目录不存在")
    rc, out, _ = await _run(path, "rev-parse", "--show-toplevel")
    if rc != 0 or not out.strip():
        raise GitError("当前项目不是 Git 仓库（可在终端执行 git init 后再使用）")
    return out.strip()


async def _has_head(root: str) -> bool:
    rc, _, _ = await _run(root, "rev-parse", "--verify", "-q", "HEAD")
    return rc == 0


# ── 状态 ─────────────────────────────────────────────────────────────────────

_STATUS_LETTER = {"M": "modified", "A": "added", "D": "deleted", "R": "renamed", "C": "copied", "T": "typechange", "U": "unmerged"}


def _entry(path: str, code: str, old: str | None = None) -> dict:
    return {"path": path, "code": code, "old_path": old}


async def status(path: str) -> dict:
    root = await repo_root(path)
    # plan-20261017-frontend-polish：normal 会把未跟踪的整个目录折叠成一条（尾部带 /），
    # 界面上就会把目录当成文件列出来；all 逐文件返回，只显示真正的文件。
    out = await _must(root, "status", "--porcelain=v2", "--branch", "--untracked-files=all")
    info: dict = {"branch": None, "detached": False, "upstream": None, "ahead": 0, "behind": 0, "oid": None}
    staged: list[dict] = []
    unstaged: list[dict] = []
    untracked: list[dict] = []
    conflicts: list[dict] = []
    for line in out.splitlines():
        if line.startswith("# branch.head "):
            head = line[len("# branch.head "):].strip()
            info["detached"] = head == "(detached)"
            info["branch"] = None if info["detached"] else head
        elif line.startswith("# branch.oid "):
            info["oid"] = line[len("# branch.oid "):].strip()[:12]
        elif line.startswith("# branch.upstream "):
            info["upstream"] = line[len("# branch.upstream "):].strip()
        elif line.startswith("# branch.ab "):
            m = re.match(r"# branch\.ab \+(\d+) -(\d+)", line)
            if m:
                info["ahead"], info["behind"] = int(m.group(1)), int(m.group(2))
        elif line.startswith("1 "):
            parts = line.split(" ", 8)
            xy, p = parts[1], parts[8]
            if xy[0] != ".":
                staged.append(_entry(p, xy[0]))
            if xy[1] != ".":
                unstaged.append(_entry(p, xy[1]))
        elif line.startswith("2 "):
            parts = line.split(" ", 9)
            xy = parts[1]
            p, _, orig = parts[9].partition("\t")
            if xy[0] != ".":
                staged.append(_entry(p, xy[0], orig or None))
            if xy[1] != ".":
                unstaged.append(_entry(p, xy[1], orig or None))
        elif line.startswith("u "):
            parts = line.split(" ", 10)
            conflicts.append(_entry(parts[10], parts[1]))
        elif line.startswith("? "):
            untracked.append(_entry(line[2:], "?"))
    state = "normal"
    git_dir = Path(root) / ".git"
    if (git_dir / "MERGE_HEAD").exists():
        state = "merging"
    elif (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists():
        state = "rebasing"
    return {
        "root": root, **info, "state": state,
        "staged": staged, "unstaged": unstaged, "untracked": untracked, "conflicts": conflicts,
        "clean": not (staged or unstaged or untracked or conflicts),
    }


# ── 差异 ─────────────────────────────────────────────────────────────────────

async def diff(path: str, file: str, staged: bool = False) -> dict:
    root = await repo_root(path)
    rel = _safe_rel(file)
    full = Path(root) / rel
    # 未跟踪的新文件没有 diff：整文件作为新增内容展示
    if not staged:
        rc, out, _ = await _run(root, "ls-files", "--error-unmatch", "--", rel)
        if rc != 0:
            if not full.is_file():
                return {"path": rel, "text": "", "truncated": False, "binary": False, "new": False}
            # 只读上限字节：大文件不整体载入内存
            with full.open("rb") as fh:
                raw = fh.read(MAX_NEW_FILE_BYTES + 1)
            if b"\x00" in raw[:8192]:
                return {"path": rel, "text": "", "truncated": False, "binary": True, "new": True}
            text = raw[:MAX_NEW_FILE_BYTES].decode("utf-8", errors="replace")
            return {"path": rel, "text": text, "truncated": len(raw) > MAX_NEW_FILE_BYTES, "binary": False, "new": True}
    args = ["diff", "--no-ext-diff", "-U3"]
    if staged:
        args.append("--cached")
    args += ["--", rel]
    out = await _must(root, *args)
    if "Binary files" in out[:200]:
        return {"path": rel, "text": "", "truncated": False, "binary": True, "new": False}
    truncated = len(out.encode("utf-8")) > MAX_DIFF_BYTES
    text = out.encode("utf-8")[:MAX_DIFF_BYTES].decode("utf-8", errors="ignore") if truncated else out
    return {"path": rel, "text": text, "truncated": truncated, "binary": False, "new": False}


# ── 暂存 / 提交 ──────────────────────────────────────────────────────────────

async def stage(path: str, paths: list[str]) -> None:
    root = await repo_root(path)
    await _must(root, "add", "-A", "--", *_safe_paths(paths))


async def unstage(path: str, paths: list[str]) -> None:
    root = await repo_root(path)
    rels = _safe_paths(paths)
    if await _has_head(root):
        await _must(root, "reset", "-q", "HEAD", "--", *rels)
    else:
        await _must(root, "rm", "-r", "--cached", "-q", "--", *rels)


async def discard(path: str, paths: list[str], untracked: list[str]) -> None:
    """丢弃工作区改动：已跟踪文件恢复到 HEAD（同时取消暂存），未跟踪文件直接删除。调用方须先确认。"""
    root = await repo_root(path)
    rels = _safe_paths(paths)
    untracked_set = set(_safe_paths(untracked)) if untracked else set()
    tracked = [p for p in rels if p not in untracked_set]
    if tracked:
        if await _has_head(root):
            # HEAD 中不存在的文件（新加入暂存区的文件）没有可检出的版本，checkout 会整批失败：
            # 这类文件取消暂存后直接删除，其余的检出 HEAD 版本
            in_head = set((await _must(root, "ls-tree", "-r", "--name-only", "HEAD", "--", *tracked)).splitlines())
            restore = [p for p in tracked if p in in_head]
            fresh = [p for p in tracked if p not in in_head]
            if restore:
                await _must(root, "checkout", "HEAD", "--", *restore)
            if fresh:
                await _must(root, "rm", "--cached", "-f", "-q", "--", *fresh)
                for rel in fresh:
                    target = Path(root) / rel
                    if target.is_file():
                        target.unlink()
        else:
            await _must(root, "rm", "-r", "--cached", "-q", "--", *tracked)
            for rel in tracked:
                target = Path(root) / rel
                if target.is_file():
                    target.unlink()
    rootp = Path(root).resolve()
    for rel in untracked_set:
        target = (rootp / rel).resolve()
        if rootp not in target.parents and target != rootp:
            raise GitError(f"拒绝删除仓库外的路径：{rel}")
        if target.is_dir():
            import shutil
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists():
            target.unlink()


async def commit(path: str, message: str, amend: bool = False) -> str:
    root = await repo_root(path)
    msg = message.strip()
    if not msg and not amend:
        raise GitError("提交信息不能为空")
    staged = await _run(root, "diff", "--cached", "--quiet")
    if staged[0] == 0 and not amend:
        raise GitError("没有已暂存的改动，请先暂存要提交的文件")
    if msg:
        args = ["commit", "-F", "-", *(["--amend"] if amend else [])]
        await _must(root, *args, stdin=msg.encode("utf-8"))
    else:
        await _must(root, "commit", "--amend", "--no-edit")
    sha = (await _must(root, "rev-parse", "--short", "HEAD")).strip()
    return sha


# ── 远端同步 ─────────────────────────────────────────────────────────────────

async def sync(path: str, action: Literal["fetch", "pull", "push"]) -> dict:
    """抓取 / 拉取 / 推送。

    plan-20261017-git-conflict：拉取产生冲突时不再以错误返回——git 已经把冲突写进工作区，
    界面需要拿到冲突文件清单直接进入处理流程，而不是只看到一句「拉取失败」。
    """
    root = await repo_root(path)
    st = await status(path)
    if action == "fetch":
        out = await _must(root, "fetch", "--prune", timeout=NETWORK_TIMEOUT)
    elif action == "pull":
        if st["detached"]:
            raise GitError("当前是游离 HEAD，无法拉取，请先切换到分支")
        if not st["upstream"]:
            raise GitError("当前分支没有设置上游分支，请在终端执行 git push -u 后再拉取")
        if st["conflicts"]:
            raise GitError("存在未解决的冲突，请先处理冲突")
        code, out, err = await _run(root, "pull", "--no-rebase", "--no-edit", timeout=NETWORK_TIMEOUT)
        if code != 0:
            # 冲突不是错误：git 已把冲突写进工作区，界面据此直接进入冲突处理
            after = await status(path)
            if after["conflicts"] or after["state"] in ("merging", "rebasing"):
                return {
                    "output": (out + "\n" + err).strip(),
                    "conflict": True,
                    "conflicts": [c["path"] for c in after["conflicts"]],
                    "state": after["state"],
                }
            raise GitError((err or out or "拉取失败").strip())
        return {"output": (out or "完成").strip(), "conflict": False, "conflicts": [], "state": "normal"}
    else:
        if st["detached"] or not st["branch"]:
            raise GitError("当前是游离 HEAD，无法推送")
        if st["upstream"]:
            out = await _must(root, "push", timeout=NETWORK_TIMEOUT)
        else:
            out = await _must(root, "push", "-u", "origin", st["branch"], timeout=NETWORK_TIMEOUT)
    return {"output": (out or "完成").strip(), "conflict": False, "conflicts": [], "state": st["state"]}


# ── 历史 ─────────────────────────────────────────────────────────────────────

_SHA_RE = re.compile(r"[0-9a-fA-F]{4,64}")


def _commit_sha(sha: str) -> str:
    """提交号只接受十六进制串，避免把用户输入当作 git 选项或revision 表达式拼进命令。"""
    s = (sha or "").strip()
    if not _SHA_RE.fullmatch(s):
        raise GitError("提交号不合法")
    return s


def _parse_refs(raw: str, remotes: set[str]) -> list[dict]:
    """%D 装饰（如 `HEAD -> main, origin/main, tag: v1`）→ 分类后的引用列表，供历史行上的标签展示。"""
    refs: list[dict] = []
    for item in (s.strip() for s in raw.split(",")):
        if not item:
            continue
        if item.startswith("HEAD -> "):
            refs.append({"name": "HEAD", "kind": "head"})
            refs.append({"name": item[len("HEAD -> "):], "kind": "branch"})
        elif item == "HEAD":
            refs.append({"name": "HEAD", "kind": "head"})
        elif item.startswith("tag: "):
            refs.append({"name": item[len("tag: "):], "kind": "tag"})
        elif "/" in item and item.split("/", 1)[0] in remotes:
            refs.append({"name": item, "kind": "remote"})
        else:
            refs.append({"name": item, "kind": "branch"})
    return refs


async def log(path: str, limit: int = 40, skip: int = 0) -> list[dict]:
    root = await repo_root(path)
    if not await _has_head(root):
        return []
    limit = max(1, min(limit, 200))
    skip = max(0, min(skip, 100_000))
    remotes = set((await _must(root, "remote")).split())
    out = await _must(root, "log", f"-n{limit}", f"--skip={skip}", "--date=iso-strict",
                      "--pretty=format:%H%x1f%h%x1f%an%x1f%ad%x1f%s%x1f%P%x1f%D%x1e")
    rows: list[dict] = []
    for rec in out.split("\x1e"):
        rec = rec.strip("\n")
        if not rec:
            continue
        parts = rec.split("\x1f")
        if len(parts) < 7:
            continue
        rows.append({
            "sha": parts[0], "short": parts[1], "author": parts[2], "date": parts[3], "subject": parts[4],
            "parents": parts[5].split(), "refs": _parse_refs(parts[6], remotes),
        })
    return rows


async def commit_detail(path: str, sha: str) -> dict:
    """单个提交的元信息、完整提交信息与变更文件（含增删行数）。

    merge 提交的 diff-tree 不输出文件（与 git log 的默认行为一致），这里如实标记 merge，由前端说明。
    """
    root = await repo_root(path)
    ref = _commit_sha(sha)
    meta = await _must(root, "show", "-s", "--date=iso-strict",
                       "--pretty=format:%H%x1f%an%x1f%ae%x1f%ad%x1f%P%x1f%B", ref)
    parts = meta.split("\x1f", 5)
    if len(parts) < 6:
        raise GitError("无法读取该提交")
    full, author, email, date, parents_raw, body = parts
    message = body.strip("\n")
    parents = parents_raw.split()
    base = ["diff-tree", "--root", "--no-commit-id", "-r", "--no-renames"]
    numstat = await _must(root, *base, "--numstat", full)
    names = await _must(root, *base, "--name-status", full)
    counts: dict[str, tuple[int | None, int | None]] = {}
    for line in numstat.splitlines():
        cols = line.split("\t", 2)
        if len(cols) == 3:
            a, d, p = cols
            counts[p] = (None if a == "-" else int(a), None if d == "-" else int(d))
    files: list[dict] = []
    for line in names.splitlines():
        cols = line.split("\t")
        if len(cols) < 2:
            continue
        p = cols[-1]
        add, dele = counts.get(p, (None, None))
        files.append({"path": p, "code": cols[0][:1], "additions": add, "deletions": dele})
    return {
        "sha": full, "short": full[:7], "author": author, "email": email, "date": date,
        "subject": message.split("\n", 1)[0].strip(), "message": message,
        "parents": parents, "merge": len(parents) > 1, "files": files,
        "additions": sum(f["additions"] or 0 for f in files),
        "deletions": sum(f["deletions"] or 0 for f in files),
    }


async def commit_diff(path: str, sha: str, file: str) -> dict:
    """某个提交里单个文件的差异（与 diff() 同一返回结构，便于前端复用渲染）。"""
    root = await repo_root(path)
    ref = _commit_sha(sha)
    rel = _safe_rel(file)
    out = await _must(root, "show", "--format=", "--no-ext-diff", "-U3", "--no-renames", ref, "--", rel)
    if "Binary files" in out[:200]:
        return {"path": rel, "text": "", "truncated": False, "binary": True, "new": False}
    truncated = len(out.encode("utf-8")) > MAX_DIFF_BYTES
    text = out.encode("utf-8")[:MAX_DIFF_BYTES].decode("utf-8", errors="ignore") if truncated else out
    return {"path": rel, "text": text, "truncated": truncated, "binary": False, "new": False}


# ── 分支 ─────────────────────────────────────────────────────────────────────

async def branches(path: str) -> dict:
    root = await repo_root(path)
    out = await _must(root, "for-each-ref", "--format=%(refname)%09%(refname:short)%09%(HEAD)%09%(upstream:short)%09%(committerdate:iso-strict)",
                      "refs/heads", "refs/remotes")
    local: list[dict] = []
    remote: list[dict] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        ref, short, head, upstream, date = parts[0], parts[1], parts[2], parts[3], parts[4]
        if ref.startswith("refs/remotes/"):
            if short.endswith("/HEAD"):
                continue
            remote.append({"name": short, "date": date})
        else:
            local.append({"name": short, "current": head == "*", "upstream": upstream or None, "date": date})
    local.sort(key=lambda b: (not b["current"], b["name"]))
    remote.sort(key=lambda b: b["name"])
    return {"local": local, "remote": remote}


async def branch_action(path: str, action: Literal["checkout", "create", "delete"], name: str,
                        base: str | None = None, force: bool = False,
                        on_dirty: Literal["stash", "discard"] | None = None) -> dict:
    """分支切换 / 创建 / 删除。

    plan-20261017-git-conflict：切换分支时若未提交改动会被覆盖，返回 switched=False 与文件清单，
    由界面让用户选择「暂存后切换」或「放弃改动后切换」；可恢复的情况不当作失败。
    """
    root = await repo_root(path)
    name = name.strip()
    if not name or not re.match(r"^[A-Za-z0-9._/-]+$", name) or name.startswith("-"):
        raise GitError("分支名格式不正确")
    if base is not None and (not re.match(r"^[A-Za-z0-9._/-]+$", base) or base.startswith("-")):
        raise GitError("基于的分支或提交格式不正确")
    if action == "checkout":
        before = await status(path)
        stashed = False
        if not before["clean"] and on_dirty == "stash":
            await _must(root, "stash", "push", "-u", "-m", "chatcoder: 切换分支前自动暂存")
            stashed = True
        elif not before["clean"] and on_dirty == "discard":
            # 「放弃改动再切换」须同时清掉已暂存的改动：先取消暂存，再丢弃工作区改动与新文件
            staged_paths = [e["path"] for e in before["staged"]]
            if staged_paths:
                await unstage(path, staged_paths)
            after_unstage = await status(path)
            await discard(path, [e["path"] for e in after_unstage["unstaged"]],
                          [e["path"] for e in after_unstage["untracked"]])
        # 远端分支：本地已有同名分支时直接切换到它，避免 --track 因重名失败
        remote_ref = "/" in name and (await _run(root, "show-ref", "--verify", "-q", f"refs/remotes/{name}"))[0] == 0
        # 用 switch 而不是 checkout：checkout 在找不到同名分支时会把名字当作路径，
        # 例如分支名填 README.md 会把该文件恢复成索引版本、静默丢弃本地改动；switch 只接受分支
        if remote_ref:
            local = name.split("/", 1)[1]
            has_local = (await _run(root, "show-ref", "--verify", "-q", f"refs/heads/{local}"))[0] == 0
            args = ["switch", local] if has_local else ["switch", "--track", name]
        else:
            args = ["switch", name]
        code, out, err = await _run(root, *args)
        if code != 0:
            if stashed:
                await _run(root, "stash", "pop")  # 切不过去就把改动还回去
            msg = (err or out).strip()
            now = await status(path)
            blocked = list(dict.fromkeys(e["path"] for e in now["staged"] + now["unstaged"] + now["untracked"]))
            # 只有 git 明确因「本地改动会被覆盖」拒绝时才交给界面让用户选择；
            # 分支不存在等其它失败照实报错，不能误弹「本地改动阻止切换」
            if blocked and "overwritten" in msg.lower():
                return {"switched": False, "reason": "dirty", "files": blocked, "output": msg}
            raise GitError(msg or "切换分支失败")
        if stashed:
            pcode, pout, perr = await _run(root, "stash", "pop")
            if pcode != 0:
                after = await status(path)
                return {
                    "switched": True, "stashed": True,
                    "conflict": bool(after["conflicts"]),
                    "conflicts": [c["path"] for c in after["conflicts"]],
                    "state": after["state"],
                    "output": (pout + "\n" + perr).strip() or "已切换分支，但暂存的改动未能自动恢复。",
                }
        return {"switched": True, "stashed": stashed, "conflict": False, "conflicts": [],
                "state": (await status(path))["state"], "output": (out or f"已切换到 {name}").strip()}
    if action == "create":
        args = ["checkout", "-b", name]
        if base:
            args.append(base)
        out = await _must(root, *args)
        return {"switched": True, "created": True, "output": (out or f"已创建并切换到 {name}").strip()}
    st = await status(path)
    if st["branch"] == name:
        raise GitError("不能删除当前分支，请先切换到其他分支")
    out = await _must(root, "branch", "-D" if force else "-d", name)
    return {"deleted": True, "output": (out or f"已删除 {name}").strip()}


# ── 冲突与合并 ───────────────────────────────────────────────────────────────

async def resolve_conflict(path: str, file: str, side: Literal["ours", "theirs"]) -> None:
    root = await repo_root(path)
    rel = _safe_rel(file)
    await _must(root, "checkout", f"--{side}", "--", rel)
    await _must(root, "add", "--", rel)


async def abort_merge(path: str) -> None:
    root = await repo_root(path)
    if not (Path(root) / ".git" / "MERGE_HEAD").exists():
        raise GitError("当前没有进行中的合并")
    await _must(root, "merge", "--abort")
