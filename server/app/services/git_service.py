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
import subprocess
import sys
from pathlib import Path
from typing import Awaitable, Callable, Literal

from app.core.config import settings

logger = logging.getLogger(__name__)

LOCAL_TIMEOUT = 30
NETWORK_TIMEOUT = 180
# plan-522-2209：连接阶段短超时——只探一次 ls-remote，失败就立刻报错，
# 不必等满 NETWORK_TIMEOUT 才把「连不上」告诉用户。
CONNECT_TIMEOUT = 20
MAX_DIFF_BYTES = 400_000
MAX_NEW_FILE_BYTES = 256_000
_UNMERGED = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}


class GitError(Exception):
    """可直接展示给用户的错误（已是中文或 git 原文）。"""


ProgressCb = Callable[[str], Awaitable[None]]

# plan-522-2209：网络类 git 命令的 SSL 后端。
#
# 根因：Windows 上 git 默认用 schannel，经本机 HTTP 代理（127.0.0.1:7897）访问 GitHub 会在
# TLS 握手阶段挂死——同一仓库 git pull 实测 357.7 秒才退出，加 `-c http.sslBackend=openssl`
# 后 ls-remote 秒回。这里按配置给联网命令显式指定后端；本地命令不插入，避免无谓改变行为。
_PROXY_ENV = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
# git 配置里 http.proxy 的探测结果缓存：进程生命周期内极少变动，不必每次联网都跑一遍
_git_http_proxy: bool | None = None


def _git_config_has_proxy() -> bool:
    """git 配置里是否已有 http.proxy（有代理但没设环境变量时，这是「要走代理」的唯一线索）。"""
    global _git_http_proxy
    if _git_http_proxy is None:
        try:
            r = subprocess.run(["git", "config", "--get", "http.proxy"],
                               capture_output=True, timeout=5, text=True)
            _git_http_proxy = r.returncode == 0 and bool(r.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            _git_http_proxy = False
    return _git_http_proxy


async def _ssl_backend() -> str | None:
    """按配置决定 `-c http.sslBackend` 的取值；None 表示不插入（走 git 默认）。

    plan-522-2209：auto = 「Windows 且检测到代理」时用 openssl，其余平台/场景用 git 默认。
    除环境变量外，项目自身的 http_proxy 配置与 git 的 http.proxy 同样算作「要走代理」——
    桌面版常常只在设置中心填了代理，环境变量里并没有。
    """
    raw = (settings.git_ssl_backend or "auto").strip().lower()
    if raw in ("default", "none", "off", "0"):
        return None
    if raw in ("schannel", "openssl"):
        return raw
    # auto（含无法识别的取值，按 auto 处理）
    if sys.platform != "win32":
        return None
    if any(os.environ.get(k) for k in _PROXY_ENV) or (settings.http_proxy or "").strip():
        return "openssl"
    if await asyncio.to_thread(_git_config_has_proxy):
        return "openssl"
    return None


async def _kill(proc: asyncio.subprocess.Process | None) -> None:
    """结束子进程：超时后必须杀掉，否则卡住的 git 会常驻并持有 index.lock，阻塞之后所有操作。"""
    if proc is None or proc.returncode is not None:
        return
    try:
        proc.kill()
        await proc.wait()
    except ProcessLookupError:
        pass


async def _emit_progress(cb: ProgressCb, raw: bytes) -> None:
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return
    try:
        await cb(text)
    except Exception:  # 进度回调失败不能打断 git 本身
        logger.debug("[git] 进度回调异常", exc_info=True)


async def _pump(proc: asyncio.subprocess.Process, stdin: bytes | None, on_progress: ProgressCb) -> tuple[bytes, bytes]:
    """流式版本：stdout 分块读完、stderr 逐行回调，最后等进程退出（communicate 做不到这件事）。

    返回的 stderr 仍是原始字节（与 communicate 的行为一致，不改变错误文案）；
    \r 只是额外拆出进度行喂给回调。
    """
    out_chunks: list[bytes] = []
    err_chunks: list[bytes] = []
    progress = b""

    async def feed_stdin() -> None:
        if proc.stdin is None:
            return
        try:
            if stdin:
                proc.stdin.write(stdin)
                await proc.stdin.drain()
            proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError):
            pass

    async def read_stdout() -> None:
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                return
            out_chunks.append(chunk)

    async def read_stderr() -> None:
        nonlocal progress
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                break
            err_chunks.append(chunk)
            # git 的进度行用 \r 原地覆盖，按 \n 分行会把整段进度攒到最后才吐出
            progress += chunk.replace(b"\r", b"\n")
            *lines, progress = progress.split(b"\n")
            for line in lines:
                await _emit_progress(on_progress, line)
        if progress:
            await _emit_progress(on_progress, progress)
            progress = b""

    await asyncio.gather(feed_stdin(), read_stdout(), read_stderr())
    await proc.wait()
    return b"".join(out_chunks), b"".join(err_chunks)


async def _run(
    cwd: str, *args: str, timeout: int = LOCAL_TIMEOUT, stdin: bytes | None = None,
    network: bool = False, on_progress: ProgressCb | None = None,
) -> tuple[int, str, str]:
    """执行 git 命令。

    plan-522-2209：network=True 时（fetch / pull / push / ls-remote / clone）按配置在 `git` 与
    子命令之间插入 `-c http.sslBackend=<backend>`；传 on_progress 时改为流式读取，
    逐行回调 stderr，调用方能拿到「Receiving objects: 45%」这类进度。
    """
    env = dict(os.environ)
    env.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "never",
        "GIT_EDITOR": "true",
        "GIT_PAGER": "cat",
        "LC_ALL": "C.UTF-8",
    })
    cmd = ["git", "-c", "core.quotepath=off", "-c", "color.ui=never", "--no-pager"]
    if network:
        backend = await _ssl_backend()
        if backend:
            cmd += ["-c", f"http.sslBackend={backend}"]
    cmd += list(args)
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd, env=env,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        if on_progress is None:
            out, err = await asyncio.wait_for(proc.communicate(stdin), timeout=timeout)
        else:
            out, err = await asyncio.wait_for(_pump(proc, stdin, on_progress), timeout=timeout)
    except asyncio.TimeoutError:
        await _kill(proc)
        raise GitError(f"git 命令超时（>{timeout}s），可能是网络或凭据问题") from None
    except FileNotFoundError:
        raise GitError("系统未安装 git，请先安装后再使用 Git 功能") from None
    except OSError as e:
        raise GitError(f"git 执行失败：{e}") from None
    return proc.returncode or 0, out.decode("utf-8", errors="replace"), err.decode("utf-8", errors="replace")


async def _must(
    cwd: str, *args: str, timeout: int = LOCAL_TIMEOUT, stdin: bytes | None = None,
    network: bool = False, on_progress: ProgressCb | None = None,
) -> str:
    rc, out, err = await _run(cwd, *args, timeout=timeout, stdin=stdin, network=network,
                              on_progress=on_progress)
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


# ── 多仓库识别（plan-20261019-git-multirepo）────────────────────────────────
#
# 同一个工作目录下常有多个独立仓库（如 frontend/ 与 backend/，根目录本身不是仓库），
# 此前 Git 面板只认根目录，这种布局直接报「不是 Git 仓库」。这里统一识别项目内的仓库，
# 面板与接口按「相对项目根的子目录」（repo 参数）指定操作哪个仓库。

# 扫描时跳过的目录：依赖、构建产物、虚拟环境（与 worktree_service 的候选扫描一致）
_SCAN_SKIP = {
    "node_modules", "dist", "build", "target", "out", "venv", ".venv", "env",
    "__pycache__", "vendor", "bin", "obj",
}
# 向下扫描的层数：覆盖 frontend/、backend/ 与 apps/web、services/api 这两类常见布局
REPO_SCAN_DEPTH = 2


def has_git(path: Path) -> bool:
    """目录下有 .git（普通仓库是目录，子模块与工作树是文件）即视为仓库根。"""
    try:
        return (path / ".git").exists()
    except OSError:
        return False


def find_nested_repos(root: Path, depth: int = REPO_SCAN_DEPTH) -> list[Path]:
    """root 之下（不含 root）的仓库根目录：逐层扫描，找到仓库就不再深入它。"""
    found: list[Path] = []
    level = [root]
    for _ in range(max(0, depth)):
        nxt: list[Path] = []
        for d in level:
            try:
                children = sorted((p for p in d.iterdir() if p.is_dir()), key=lambda p: p.name.lower())
            except OSError:
                continue
            for child in children:
                if child.name.startswith(".") or child.name in _SCAN_SKIP:
                    continue
                if has_git(child):
                    found.append(child)
                else:
                    nxt.append(child)
        level = nxt
    # 按相对路径排序：下拉列表与工作树候选的顺序稳定、可预期（apps/web 排在 backend 前）
    return sorted(found, key=lambda p: p.relative_to(root).as_posix().lower())


def _rel_of(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


async def repo_dirs(root_path: str) -> list[tuple[str, str]]:
    """项目内的仓库列表 [(相对路径, 绝对路径)]，根仓库的相对路径为空串、排在最前。

    项目目录本身位于某个仓库内部（打开的是仓库的子目录）时按单仓库处理，返回 [("", 项目目录)]。
    """
    root = Path(root_path)
    if not root_path or not root.is_dir():
        raise GitError("项目目录不存在")
    out: list[tuple[str, str]] = []
    if has_git(root):
        out.append(("", str(root)))
    else:
        rc, top, _ = await _run(str(root), "rev-parse", "--show-toplevel")
        if rc == 0 and top.strip():
            return [("", str(root))]
    out.extend((_rel_of(root, p), str(p)) for p in find_nested_repos(root))
    return out


def repo_dir(project_path: str, repo: str | None) -> str:
    """接口里的 repo（相对项目根的子目录）→ 仓库目录；空值表示项目根（单仓库项目即仓库本身）。"""
    if not repo or not str(repo).strip() or str(repo).strip() in (".", "/"):
        return project_path
    rel = _safe_rel(str(repo).strip().strip("/"))
    path = Path(project_path) / rel
    if not path.is_dir() or not has_git(path):
        raise GitError(f"仓库不存在：{repo}")
    return str(path)


async def _repo_summary(rel: str, path: str) -> dict | None:
    """仓库下拉列表的一行：名称、分支、领先 / 落后、改动数（一次 git status 取齐）。"""
    rc, out, _ = await _run(path, "status", "--porcelain=v2", "--branch", "--untracked-files=normal")
    if rc != 0:
        return None
    branch, detached, ahead, behind, changes = None, False, 0, 0, 0
    for line in out.splitlines():
        if line.startswith("# branch.head "):
            head = line[len("# branch.head "):].strip()
            detached = head == "(detached)"
            branch = None if detached else head
        elif line.startswith("# branch.ab "):
            m = re.match(r"# branch\.ab \+(\d+) -(\d+)", line)
            if m:
                ahead, behind = int(m.group(1)), int(m.group(2))
        elif line and not line.startswith("#"):
            changes += 1
    return {
        "rel": rel, "name": Path(path).name, "path": path.replace("\\", "/"),
        "is_root": rel == "", "branch": branch, "detached": detached,
        "ahead": ahead, "behind": behind, "changes": changes,
    }


async def discover_repos(root_path: str) -> list[dict]:
    """识别项目内的全部仓库（根仓库在前），每个带分支与改动数，供面板的仓库切换列表使用。"""
    dirs = await repo_dirs(root_path)
    rows = await asyncio.gather(*(_repo_summary(rel, p) for rel, p in dirs))
    return [r for r in rows if r]


async def status_all(root_path: str) -> dict:
    """汇总项目内全部仓库的状态：路径带仓库前缀（相对项目根），与会话改动记录口径一致。

    给工具卡等「按项目看改动」的地方用：多仓库项目此前直接报「不是 Git 仓库」，工具卡里看不到改动。
    """
    dirs = await repo_dirs(root_path)
    results = await asyncio.gather(*(status(p) for _rel, p in dirs), return_exceptions=True)
    merged: dict = {"repos": [], "staged": [], "unstaged": [], "untracked": [], "conflicts": []}
    first: dict | None = None
    for (rel, _p), st in zip(dirs, results):
        if isinstance(st, BaseException):
            continue
        first = first or st
        prefix = f"{rel}/" if rel else ""
        merged["repos"].append({"rel": rel, "branch": st.get("branch"), "clean": st.get("clean")})
        for key in ("staged", "unstaged", "untracked", "conflicts"):
            for e in st.get(key) or []:
                old = e.get("old_path")
                merged[key].append(_entry(prefix + e["path"], e["code"], (prefix + old) if old else None))
    if first is None:
        raise GitError("当前项目不是 Git 仓库（可在终端执行 git init 后再使用）")
    single = len(merged["repos"]) == 1
    merged.update({
        "root": root_path, "branch": first.get("branch") if single else None,
        "detached": bool(first.get("detached")) if single else False,
        "upstream": first.get("upstream") if single else None,
        "ahead": sum(int(r.get("ahead") or 0) for r in results if isinstance(r, dict)),
        "behind": sum(int(r.get("behind") or 0) for r in results if isinstance(r, dict)),
        "oid": first.get("oid") if single else None, "state": "normal",
        "clean": not any(merged[k] for k in ("staged", "unstaged", "untracked", "conflicts")),
    })
    return merged


# ── 未提交改动的增删行数（plan-20261019-toolcard-v2）─────────────────────────────
#
# 工具卡 Git 栏的「更改 +N −M」：当前分支上所有未提交改动的行数合计——已暂存 + 未暂存相对 HEAD，
# 再加上未跟踪的新文本文件（整文件算新增，与「提交时会新增多少行」的直觉一致）。

# 统计未跟踪文件时的上限：文件数、单文件大小（超过的不数，避免被构建产物 / 日志拖慢）
_STAT_UNTRACKED_MAX_FILES = 300
_STAT_UNTRACKED_MAX_BYTES = 1_000_000


def _parse_numstat(out: str) -> tuple[int, int]:
    """git diff --numstat 输出 → (新增行, 删除行)。二进制文件（- -）不计行数。"""
    add = dele = 0
    for line in out.splitlines():
        cols = line.split("\t", 2)
        if len(cols) < 3:
            continue
        a, d = cols[0], cols[1]
        if a.isdigit():
            add += int(a)
        if d.isdigit():
            dele += int(d)
    return add, dele


def _count_untracked_lines(root: str, files: list[str]) -> int:
    """未跟踪文本文件的总行数（同步，放线程里跑）。二进制 / 过大 / 读不到的文件跳过。"""
    total = 0
    for rel in files[:_STAT_UNTRACKED_MAX_FILES]:
        p = Path(root) / rel
        try:
            if not p.is_file() or p.stat().st_size > _STAT_UNTRACKED_MAX_BYTES:
                continue
            raw = p.read_bytes()
        except OSError:
            continue
        if not raw or b"\x00" in raw[:8192]:
            continue
        total += raw.count(b"\n") + (0 if raw.endswith(b"\n") else 1)
    return total


async def _repo_diff_stat(rel: str, path: str) -> dict:
    """一个仓库的卡片摘要：分支 / 领先落后 + 改动文件数 + 增删行数（两条 git 命令取齐）。"""
    st = await status(path)  # porcelain v2 + 逐个未跟踪文件：分支、领先落后、各类改动文件一次到手
    root = st["root"]
    if await _has_head(root):
        out = await _must(root, "diff", "HEAD", "--numstat", "--no-renames", "--no-ext-diff")
        add, dele = _parse_numstat(out)
    else:
        # 还没有任何提交：已暂存（相对空树）与未暂存各算一次
        a1, d1 = _parse_numstat(await _must(root, "diff", "--cached", "--numstat", "--no-renames", "--no-ext-diff"))
        a2, d2 = _parse_numstat(await _must(root, "diff", "--numstat", "--no-renames", "--no-ext-diff"))
        add, dele = a1 + a2, d1 + d2
    # 末尾带 / 的未跟踪条目是内嵌的子仓库目录（多仓库项目里它们单独成仓，这里不重复计入）
    untracked = [e["path"] for e in st["untracked"] if not e["path"].endswith("/")]
    if untracked:
        add += await asyncio.to_thread(_count_untracked_lines, root, untracked)
    paths = {e["path"] for key in ("staged", "unstaged", "conflicts") for e in st[key]} | set(untracked)
    files = len(paths)
    return {
        "rel": rel, "name": Path(path).name,
        "branch": st["branch"], "detached": st["detached"], "oid": st["oid"],
        "ahead": st["ahead"], "behind": st["behind"],
        "files": files, "additions": add, "deletions": dele,
    }


async def diff_stat(root_path: str) -> dict:
    """项目内全部仓库的卡片摘要（并列出各仓库，供多仓库项目分别展示）与改动合计。"""
    dirs = await repo_dirs(root_path)
    results = await asyncio.gather(*(_repo_diff_stat(rel, p) for rel, p in dirs), return_exceptions=True)
    repos = [r for r in results if not isinstance(r, BaseException)]
    if not repos:
        raise GitError("当前项目不是 Git 仓库（可在终端执行 git init 后再使用）")
    return {
        "additions": sum(r["additions"] for r in repos),
        "deletions": sum(r["deletions"] for r in repos),
        "files": sum(r["files"] for r in repos),
        "repos": repos,
    }


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

# plan-522-2209：连接类错误的特征词。只有命中这些才判定「连不上远端」；
# 认证失败、仓库不存在等交给真正的命令去报原文，不要替换成误导性的「请检查网络」。
_CONNECT_ERROR_HINTS = (
    "could not resolve host", "failed to connect", "connection timed out", "connection reset",
    "operation timed out", "connection refused", "unable to access", "ssl", "tls",
    "proxy", "schannel", "recv failure", "send failure", "empty reply from server",
)

# 连接失败时给用户的文案：不提 git 原文，直接说清「查网络或代理」
_CONNECT_FAIL_TEXT = "无法连接远端仓库，请检查网络或代理设置后重试"


async def _ensure_reachable(root: str, upstream: str | None) -> None:
    """联网操作前先探一次连通性，连不上就立刻失败。

    plan-522-2209：此前只有 NETWORK_TIMEOUT=180 兜底，连接阶段就挂死也要等满 180 秒，
    用户全程只看到按钮变灰。这里用 ls-remote 做 20 秒短探测，把「连不上」提前暴露。
    没有上游分支的仓库（如刚 init 的本地仓库）不探测——那不是网络问题，交给 sync 的分支处理。
    """
    if not upstream:
        return
    remote = upstream.split("/", 1)[0] or "origin"
    try:
        code, _out, err = await _run(root, "ls-remote", "--exit-code", "--heads", remote,
                                     timeout=CONNECT_TIMEOUT, network=True)
    except GitError as e:
        # plan-522-2209：只有探测超时才归为连接问题；「未安装 git」「git 执行失败」等
        # 属于环境故障，原样抛出，不要包装成「请检查网络或代理」把用户带偏。
        if "未安装 git" in str(e) or "执行失败" in str(e):
            raise
        raise GitError(_CONNECT_FAIL_TEXT) from None
    if code == 0:
        return
    if any(h in (err or "").lower() for h in _CONNECT_ERROR_HINTS):
        raise GitError(_CONNECT_FAIL_TEXT)


async def sync(path: str, action: Literal["fetch", "pull", "push"],
               on_progress: ProgressCb | None = None) -> dict:
    """抓取 / 拉取 / 推送。

    plan-20261017-git-conflict：拉取产生冲突时不再以错误返回——git 已经把冲突写进工作区，
    界面需要拿到冲突文件清单直接进入处理流程，而不是只看到一句「拉取失败」。

    plan-522-2209：联网命令一律 network=True（按配置走 openssl 后端）、带 --progress，
    并支持 on_progress 回调逐行接收进度；本地前置检查仍先做，避免为一个必然失败的拉取白等探测。
    """
    root = await repo_root(path)
    st = await status(path)
    if action == "fetch":
        await _ensure_reachable(root, st["upstream"])
        out = await _must(root, "fetch", "--progress", "--prune", timeout=NETWORK_TIMEOUT,
                          network=True, on_progress=on_progress)
    elif action == "pull":
        if st["detached"]:
            raise GitError("当前是游离 HEAD，无法拉取，请先切换到分支")
        if not st["upstream"]:
            raise GitError("当前分支没有设置上游分支，请在终端执行 git push -u 后再拉取")
        if st["conflicts"]:
            raise GitError("存在未解决的冲突，请先处理冲突")
        await _ensure_reachable(root, st["upstream"])
        code, out, err = await _run(root, "pull", "--progress", "--no-rebase", "--no-edit",
                                    timeout=NETWORK_TIMEOUT, network=True, on_progress=on_progress)
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
        await _ensure_reachable(root, st["upstream"])
        if st["upstream"]:
            out = await _must(root, "push", "--progress", timeout=NETWORK_TIMEOUT,
                              network=True, on_progress=on_progress)
        else:
            out = await _must(root, "push", "--progress", "-u", "origin", st["branch"],
                              timeout=NETWORK_TIMEOUT, network=True, on_progress=on_progress)
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
