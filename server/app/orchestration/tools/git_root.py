"""v0.9.1: 自动定位真实 git 仓库根。

解决:workspace_root 是 monorepo/项目根,但实际 git 仓库在子目录(如 server/)。
让 git.diff / terminal.exec 自动向上查找 .git,避免在错误目录执行导致任务跑偏。

v2.5: 多仓库场景(workspace 下含 clinic/ + clinicFrontEnd/)不再盲目选第一个,
而是返回所有仓库列表供上层选择;工具按 cwd 或命令上下文匹配。
"""
import logging
from pathlib import Path

_MAX_UP = 10
logger = logging.getLogger(__name__)


def find_git_root(start_path: str | None) -> str | None:
    """从 start_path 向上查找 .git 目录,返回真实仓库根。

    - 最多向上 _MAX_UP 层,防止越界
    - 必须存在 .git 目录或文件(submodule 情况是文件)
    - 找不到返回 None,调用方应回退到 start_path
    - v2.5: 如果 start_path 本身不是仓库,也检查一级子目录(monorepo 场景)
    """
    if not start_path:
        return None
    try:
        p = Path(start_path).resolve()
    except (OSError, ValueError):
        return None

    if not p.exists():
        return None

    # 先向上搜
    for _ in range(_MAX_UP):
        git_marker = p / ".git"
        if git_marker.exists():
            return str(p)
        if p.parent == p:
            break
        p = p.parent

    # 向上搜不到,向下搜一级子目录
    p = Path(start_path).resolve()
    repos = list_git_repos(str(p))
    if repos:
        # v2.5: 多仓库时只取第一个(按字母序),但日志提示
        if len(repos) > 1:
            logger.debug("工作目录下有 %d 个 git 仓库: %s,默认使用第一个", len(repos), repos)
        return repos[0]
    return None


def list_git_repos(workspace_root: str) -> list[str]:
    """列出工作目录下所有含 .git 的一级子目录的绝对路径,按字母序排列。

    v2.5: 供上下文注入和工具选择使用。
    """
    if not workspace_root:
        return []
    try:
        ws = Path(workspace_root).resolve()
        if not ws.is_dir():
            return []
        repos: list[str] = []
        for child in sorted(ws.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                if (child / ".git").exists():
                    repos.append(str(child))
        return repos
    except OSError:
        return []


def resolve_repo_root(workspace_root: str) -> str:
    """解析工具实际应使用的 cwd:优先 git 根,回退 workspace_root。"""
    return find_git_root(workspace_root) or workspace_root


def resolve_repo_for_cwd(workspace_root: str, cwd_hint: str | None = None) -> str:
    """v2.5: 根据 cwd 提示匹配正确的 git 仓库。

    - cwd_hint='clinic' → 匹配 workspace_root/clinic
    - cwd_hint=None 且只有一个仓库 → 返回该仓库
    - cwd_hint=None 且多个仓库 → 返回 workspace_root(让 agent 用 cwd 参数指定)
    """
    if cwd_hint:
        from pathlib import Path as _P
        hinted = _P(workspace_root) / cwd_hint
        if hinted.is_dir():
            return str(hinted.resolve())
        # 可能是绝对路径
        hinted_abs = _P(cwd_hint)
        if hinted_abs.is_absolute() and hinted_abs.is_dir():
            return str(hinted_abs.resolve())
        # fallthrough

    repos = list_git_repos(workspace_root)
    if len(repos) == 1:
        return repos[0]
    # 多仓库或无仓库,返回 workspace_root
    return workspace_root
