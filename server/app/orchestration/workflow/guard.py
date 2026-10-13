"""工作流节点的工具层约束（设计 §5A.1 第 5–10 点）。

只作用于带 workflow_node_id 的子代理（主代理与普通子代理不受影响），在工具执行前后各做一次检查：

- 写入范围（第 5 点）：写工具的目标路径必须落在节点的 write_scope 内，越界直接返回工具错误；
- 读写一致性（第 7 点，CAS）：写前比对本节点读取时的内容哈希。
  未变化直接写入；已被其它节点改动时：整文件写入尝试三方合并（L1，干净合并则写入合并结果），
  锚点式编辑（editor_apply_diff / multi_file_edit / apply_patch）由工具自身的唯一锚点校验承担 L1；
  无法合并则返回结构化冲突（L2），模型重读后基于最新内容重做；同一节点连续冲突达到上限，
  转入用户决定（L4，闸门携带基线 / 当前 / 待写入三栏数据）；
- 读集失效（第 10 点）：写入成功后，通知其它在途节点“该文件已被修改”；
- 仓库状态（第 9 点）：工作流节点不得执行改变仓库状态的 git 命令（检出、提交、暂存、合并等）；
- 申请扩展写入范围（request_write_scope）：只有未被其它节点占用的路径会被授予，并纳入节点范围。

状态（读账本、读集索引、连续冲突计数、待决定闸门）保存在进程内；账本随节点结束清理，
数据库中的事实（写入范围、冲突载荷）以节点记录为准。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.orchestration.tools.base import ToolResult
from app.orchestration.workflow import store
from app.orchestration.workflow.conflicts import (
    LeaseOwner,
    ReadIndex,
    ReadLedger,
    get_lease_table,
    normalize_scope,
)

logger = logging.getLogger(__name__)

REQUEST_SCOPE_TOOL = "request_write_scope"
# 同一节点连续出现这么多次写入冲突，即不再让模型自行反复重做，转入用户决定（L4）
CONFLICT_GATE_AFTER = 3
# 超过该大小的文件不做内容比对（只能靠其它手段兜底）
CAS_MAX_BYTES = 2 * 1024 * 1024
# 改变仓库状态的 git 子命令：工作流节点共享工作区，这些命令会影响其它节点的改动
GIT_STATE_SUBCOMMANDS = frozenset(
    {
        "checkout",
        "switch",
        "restore",
        "stash",
        "reset",
        "merge",
        "rebase",
        "commit",
        "add",
        "rm",
        "mv",
        "clean",
        "worktree",
        "cherry-pick",
        "revert",
        "am",
        "apply",
        "pull",
        "push",
        "tag",
    }
)
_GIT_IN_SHELL = re.compile(
    r"\bgit\s+(" + "|".join(sorted(GIT_STATE_SUBCOMMANDS, key=len, reverse=True)) + r")\b"
)
_PATCH_FILE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+?)\s*$", re.MULTILINE)
# 会改变工作区文件内容的工具（受写入范围与一致性约束）
_WRITE_TOOLS_ALL = frozenset({"fs_write", "editor_apply_diff", "multi_file_edit", "apply_patch"})

REQUEST_SCOPE_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": REQUEST_SCOPE_TOOL,
        "description": (
            "执行中需要修改写入范围之外的文件时，申请扩展写入范围。"
            "只有未被其它节点占用的路径会被授予；被占用时返回持有者，请等待或缩小申请范围。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "工作区内的相对路径或通配符，如 server/app/services/points*.py",
                },
            },
            "required": ["paths"],
        },
    },
}

# ── 进程内状态 ─────────────────────────────────────────────
_LEDGERS: dict[int, ReadLedger] = {}  # agent_id → 读账本（CAS 依据）
_READ_INDEX = ReadIndex()  # 文件 → 读过它的 agent（失效通知）
_CONFLICTS: dict[int, int] = {}  # node_id → 连续冲突次数
_GATES: dict[int, dict[str, Any]] = {}  # node_id → 待用户决定的载荷（运行时结算时取走）
_SHELL_BEFORE: dict[int, dict[str, str] | None] = {}  # agent_id → 命令执行前的 git 状态（越界检测）


def pop_gate(node_id: int) -> dict[str, Any] | None:
    """取走节点待用户决定的载荷（冲突 / 越界）；没有则返回 None。"""
    return _GATES.pop(node_id, None)


def forget_agent(agent_id: int) -> None:
    """节点进入终态后清理该子代理的读账本与读集索引。"""
    _LEDGERS.pop(agent_id, None)
    _SHELL_BEFORE.pop(agent_id, None)
    _READ_INDEX.remove_attempt(str(agent_id))


def _ledger(agent_id: int) -> ReadLedger:
    ledger = _LEDGERS.get(agent_id)
    if ledger is None:
        ledger = _LEDGERS[agent_id] = ReadLedger()
    return ledger


# ── 路径与范围 ─────────────────────────────────────────────


def _resolve(workspace: str, raw: str) -> tuple[str, Path] | None:
    """工作区内的相对路径与绝对路径；越出工作区返回 None。"""
    root = Path(workspace).resolve()
    candidate = Path(raw)
    full = (candidate if candidate.is_absolute() else root / candidate).resolve()
    try:
        return full.relative_to(root).as_posix(), full
    except ValueError:
        return None


def _pattern_regex(pattern: str) -> re.Pattern[str]:
    """写入范围通配符 → 正则：* 不跨目录，** 跨目录，? 匹配单个字符。"""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            if i < len(pattern) and pattern[i] == "/":
                i += 1  # "**/" 可匹配零层目录
            continue
        char = pattern[i]
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(char))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def in_scope(rel: str, scope: list[str]) -> bool:
    """相对路径是否落在写入范围内（目录写法 dir/ 等价于 dir/**）。"""
    for raw in scope:
        pattern = normalize_scope(raw) or ""
        if not pattern:
            continue
        if pattern.endswith("/"):
            pattern = pattern + "**"
        if _pattern_regex(pattern).match(rel):
            return True
    return False


# ── 文件内容 ───────────────────────────────────────────────


def _snapshot_text(full: Path) -> str | None:
    """文本文件的内容（用于哈希与三方合并基线）；二进制、超大或不可读返回 None（不做 CAS）。"""
    try:
        if full.stat().st_size > CAS_MAX_BYTES:
            return None
        data = full.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def _write_targets(tool_name: str, args: dict[str, Any]) -> list[str] | None:
    """写工具涉及的路径；无法识别时返回 None（由调用方拒绝执行）。"""
    if tool_name in ("fs_write", "editor_apply_diff"):
        path = args.get("path") or args.get("file_path") or args.get("filepath")
        return [str(path)] if path else None
    if tool_name == "multi_file_edit":
        paths = [
            str(edit.get("path", ""))
            for edit in (args.get("edits") or [])
            if isinstance(edit, dict)
        ]
        paths = [p for p in paths if p]
        return paths or None
    if tool_name == "apply_patch":
        found = [
            m.strip()
            for m in _PATCH_FILE.findall(str(args.get("patch") or args.get("input") or ""))
        ]
        return found or None
    return None


# ── 工具调用前 ─────────────────────────────────────────────


def _git_state_blocked(tool_name: str, args: dict[str, Any]) -> str | None:
    command = str(args.get("command") or "").strip()
    if not command:
        return None
    if tool_name == "git":
        sub = command.split()[0]
        if sub in GIT_STATE_SUBCOMMANDS:
            return sub
        return None
    if tool_name == "terminal_exec":
        match = _GIT_IN_SHELL.search(command)
        return match.group(1) if match else None
    return None


def _error(message: str, **data: Any) -> ToolResult:
    return ToolResult(ok=False, output="", error=message, data=data)


async def before_tool(
    tool_name: str, args: dict[str, Any], *, agent: Any, ctx: Any
) -> ToolResult | None:
    """工具执行前的检查：返回 ToolResult 表示已拦截（不执行工具）；返回 None 表示放行。

    放行时可能就地修改 args（三方合并成功时写入合并后的内容）。
    约束检查自身异常时按拦截处理（失败即关闭）：宁可让模型重试，也不放过可能越界的写入。
    """
    try:
        return await _before_tool(tool_name, args, agent=agent, ctx=ctx)
    except Exception:
        logger.warning("[workflow] 工具约束检查异常，按拦截处理 tool=%s", tool_name, exc_info=True)
        return _error("工作流约束检查异常，本次操作已取消，请重试")


async def _before_tool(
    tool_name: str, args: dict[str, Any], *, agent: Any, ctx: Any
) -> ToolResult | None:
    node_id = getattr(agent, "workflow_node_id", None)
    if tool_name == REQUEST_SCOPE_TOOL:
        if node_id is None:
            return _error("只有工作流节点可以申请扩展写入范围")
        return await _request_scope(int(node_id), args)
    if node_id is None:
        return None
    if int(node_id) in _GATES and (tool_name in _WRITE_TOOLS_ALL or tool_name == "terminal_exec"):
        return _error("本步骤已转交用户决定，请停止修改文件，直接说明情况后结束本次工作。")

    blocked = _git_state_blocked(tool_name, args)
    if blocked is not None:
        return _error(
            f"工作流节点不能执行改变仓库状态的 git 命令（{blocked}），其它节点共享同一工作区。"
            "请只读取状态（git status / git diff / git log）。"
        )

    targets = _write_targets(tool_name, args) if tool_name in _WRITE_TOOLS_ALL else None
    if tool_name in _WRITE_TOOLS_ALL and targets is None:
        return _error(
            "无法识别本次写入涉及的文件，请改用 fs_write 或 editor_apply_diff 并给出 path"
        )
    if not targets and tool_name != "terminal_exec":
        return None

    node_ctx = await store.load_node_context(int(node_id))
    if node_ctx is None:
        return None
    scope = list(node_ctx["node"]["write_scope"] or [])
    workspace = node_ctx["workspace"] or ctx.workspace_root
    if tool_name == "terminal_exec":
        # 命令行写入：按命令解析出的写入目标预先校验范围（重定向、Set-Content、Out-File 等）
        from app.services.rollback_service import resolve_command_write_paths

        for raw in resolve_command_write_paths(tool_name, args, workspace) or []:
            resolved = _resolve(workspace, raw)
            if resolved is None:
                continue  # 工作区之外的路径由既有的执行策略与审批处理
            rel, _full = resolved
            if not in_scope(rel, scope):
                return _error(
                    f"该命令将写入本节点范围之外的文件『{rel}』。"
                    f"请改用 fs_write / editor_apply_diff，"
                    f"或先调用 {REQUEST_SCOPE_TOOL} 申请扩展范围。",
                    out_of_scope=rel,
                )
        # 命令执行后再核对工作区改动（见 _check_shell_scope）；执行前的状态在这里记下
        _SHELL_BEFORE[int(agent.id)] = await _git_status(workspace)
        return None
    ledger = _ledger(agent.id)
    for raw in targets:
        resolved = _resolve(workspace, raw)
        if resolved is None:
            return _error(f"路径『{raw}』不在工作区内，禁止写入")
        rel, full = resolved
        if not in_scope(rel, scope):
            return _error(
                f"路径『{rel}』不在本节点的写入范围内（{'、'.join(scope) or '无'}）。"
                f"如确需修改，请先调用 {REQUEST_SCOPE_TOOL} 申请扩展范围。",
                out_of_scope=rel,
            )
        exists = full.exists()
        current = _snapshot_text(full) if exists else None
        if exists and current is None:
            continue  # 二进制 / 超大文件：不做内容比对
        verdict = ledger.verify_write(rel, current if exists else None)
        if verdict.reason in ("unchanged", "no_prior_read"):
            continue
        if (
            tool_name == "fs_write"
            and settings.workflow_auto_merge
            and verdict.base_snapshot is not None
            and current is not None
        ):
            merged = await _try_merge(current, verdict.base_snapshot, args)
            if merged is not None:
                args["content"] = merged
                logger.info(
                    "[workflow] 写入冲突已自动合并（三方合并）node=%s path=%s", node_id, rel
                )
                continue
        if tool_name in ("editor_apply_diff", "multi_file_edit", "apply_patch"):
            # 锚点式编辑：工具按原文唯一锚点替换，锚点已失效时工具自身报错（L1 锚点重放）
            continue
        return await _conflict(int(node_id), rel, verdict, current, args)
    return None


async def _try_merge(current: str, base: str, args: dict[str, Any]) -> str | None:
    """整文件写入的三方合并：基线（读取时）、当前（磁盘）、待写入（本次内容）。干净合并返回合并结果。"""
    pending = args.get("content")
    if not isinstance(pending, str):
        return None
    # 换行口径对齐基线：模型通常输出 LF，而 CRLF 文件若不转换，整文件都会被判为冲突
    if "\r\n" in base and "\r\n" not in pending:
        pending = pending.replace("\n", "\r\n")
    from app.services.git_merge_engine import merge_file

    result = await merge_file(current, base, pending)
    # 只接受 git 明确判定的干净合并（返回码 0）。返回码 >1 时 merge_file 会采用待写入侧，
    # 那会覆盖其它节点的改动，因此在这里视为无法合并。
    if (
        result.get("returncode") == 0
        and result.get("clean")
        and isinstance(result.get("content"), str)
    ):
        return str(result["content"])
    return None


async def _conflict(
    node_id: int, rel: str, verdict: Any, current: str | None, args: dict[str, Any]
) -> ToolResult:
    count = _CONFLICTS.get(node_id, 0) + 1
    _CONFLICTS[node_id] = count
    # ask：每次真冲突都交给用户决定；queue：先由模型基于最新内容重做，连续失败再进入决定
    threshold = 1 if settings.workflow_conflict_policy == "ask" else CONFLICT_GATE_AFTER
    if count >= threshold:
        payload = {
            "kind": "conflict",
            "path": rel,
            "attempts": count,
            "base": (verdict.base_snapshot or "")[:4000],
            "current": (current or "")[:4000],
            "pending": str(args.get("content") or "")[:4000],
            "reason": f"『{rel}』多次被其它节点改动，无法自动合并",
        }
        _GATES[node_id] = payload
        return _error(
            f"『{rel}』多次与其它节点的改动冲突，已转交用户决定。请停止修改该文件，直接说明情况后结束本次工作。",
            conflict=True,
            gated=True,
        )
    return _error(
        f"『{rel}』在你读取之后被其它节点改动了。请先重新读取该文件，基于最新内容重新完成修改。",
        conflict=True,
        path=rel,
        attempt=count,
    )


async def _request_scope(node_id: int, args: dict[str, Any]) -> ToolResult:
    raw_paths = args.get("paths") or []
    if not isinstance(raw_paths, list) or not raw_paths:
        return _error("paths 不能为空")
    patterns = [normalize_scope(str(p)) for p in raw_paths]
    if any(p is None for p in patterns):
        return _error("每个路径必须是工作区内的相对路径或通配符")
    node_ctx = await store.load_node_context(node_id)
    if node_ctx is None:
        return _error("节点上下文缺失，无法申请写入范围")
    owner = LeaseOwner(
        run_id=int(node_ctx["flow_run_id"]),
        node_key=str(node_ctx["node"]["key"]),
        session_id=int(node_ctx["session_id"]),
    )
    granted, conflicts = get_lease_table().acquire(
        node_ctx["workspace"], owner, tuple(p for p in patterns if p)
    )
    if not granted:
        holders = "、".join(f"『{c.holder.node_key}』正在使用 {c.requested}" for c in conflicts[:3])
        return _error(f"这些路径被其它节点占用：{holders}。请等待释放，或缩小申请范围。")
    merged = await store.add_node_scope(node_id, [p for p in patterns if p])
    return ToolResult(
        ok=True,
        output=(
            f"已获得写入范围：{'、'.join(p for p in patterns if p)}"
            f"（当前范围：{'、'.join(merged)}）"
        ),
        data={"granted": [p for p in patterns if p], "write_scope": merged},
    )


# ── 工具调用后 ─────────────────────────────────────────────


async def after_tool(
    tool_name: str, args: dict[str, Any], *, agent: Any, ctx: Any, result: ToolResult
) -> None:
    """工具成功执行后：记录读取内容、更新写入后的基线，并通知读过该文件的其它在途节点。

    记账失败只记录日志：工具本身已经成功，不能因为约束记账而把结果改成失败。
    """
    try:
        await _after_tool(tool_name, args, agent=agent, ctx=ctx, result=result)
    except Exception:
        logger.warning("[workflow] 工具执行后的约束记账异常 tool=%s", tool_name, exc_info=True)


async def _after_tool(
    tool_name: str, args: dict[str, Any], *, agent: Any, ctx: Any, result: ToolResult
) -> None:
    node_id = getattr(agent, "workflow_node_id", None)
    if tool_name == "terminal_exec":
        # 失败的命令也可能已经写入了文件，因此不以 result.ok 为前提核对
        await _check_shell_scope(agent, ctx, args)
    if node_id is None or not result.ok:
        return
    workspace = ctx.workspace_root
    if tool_name == "fs_read":
        resolved = _resolve(workspace, str(args.get("path") or ""))
        if resolved is None:
            return
        rel, full = resolved
        text = _snapshot_text(full) if full.is_file() else None
        if text is not None:
            _ledger(agent.id).record_read(rel, text)
            _READ_INDEX.add(workspace, rel, str(agent.id))
        return
    if tool_name in _WRITE_TOOLS_ALL:
        targets = _write_targets(tool_name, args) or []
        _CONFLICTS[int(node_id)] = 0
        for raw in targets:
            resolved = _resolve(workspace, raw)
            if resolved is None:
                continue
            rel, full = resolved
            text = _snapshot_text(full) if full.is_file() else None
            if text is not None:
                _ledger(agent.id).mark_written(rel, text)
            await _notify_readers(int(ctx.session_id), workspace, rel, str(agent.id), node_id)


async def _notify_readers(
    session_id: int, workspace: str, rel: str, writer: str, node_id: int
) -> None:
    readers = _READ_INDEX.readers_of(workspace, rel, exclude=writer)
    if not readers:
        return
    from app.orchestration.subagent import get_subagent_manager

    manager = get_subagent_manager(session_id)
    node_ctx = await store.load_node_context(node_id)
    writer_name = node_ctx["node"]["key"] if node_ctx else str(writer)
    message = (
        f"[工作流通知] 文件『{rel}』已被节点『{writer_name}』修改。"
        "若你的方案依赖它的旧内容，请先重新读取该文件，再继续。"
    )
    for reader in readers:
        try:
            manager.push_agent_input(int(reader), message)
        except (TypeError, ValueError):
            continue


# ── 命令行写入的事后核对 ───────────────────────────────────


async def _git_status(workspace: str) -> dict[str, str] | None:
    """工作区内各路径（相对工作区）的 git 状态码，含未跟踪文件。

    非 git 仓库或 git 不可用时返回 None：无法判断命令是否越界，此时不做事后核对。
    """
    try:
        prefix_proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            workspace,
            "rev-parse",
            "--show-prefix",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        prefix_out, _ = await asyncio.wait_for(prefix_proc.communicate(), timeout=10)
        if prefix_proc.returncode != 0:
            return None
        status_proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            workspace,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        status_out, _ = await asyncio.wait_for(status_proc.communicate(), timeout=20)
        if status_proc.returncode != 0:
            return None
    except (OSError, asyncio.TimeoutError):
        return None
    prefix = prefix_out.decode("utf-8", "replace").strip()
    entries = status_out.decode("utf-8", "replace").split("\0")
    out: dict[str, str] = {}
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        if code[0] in "RC":  # 重命名 / 复制：紧随其后的一项是原路径
            index += 1
        if prefix:
            if not path.startswith(prefix):
                continue  # 工作区之外的改动不属于本次检测范围
            path = path[len(prefix) :]
        out[path] = code
    return out


async def _check_shell_scope(agent: Any, ctx: Any, args: dict[str, Any]) -> None:
    """命令执行后对比工作区改动：范围之外、且没有被其它节点持有租约的改动记为越界，节点转入用户决定。"""
    before = _SHELL_BEFORE.pop(int(agent.id), None)
    node_id = getattr(agent, "workflow_node_id", None)
    if before is None or node_id is None:
        return
    after = await _git_status(ctx.workspace_root)
    if after is None:
        return
    changed = {p for p in set(before) | set(after) if before.get(p) != after.get(p)}
    node_ctx = await store.load_node_context(int(node_id))
    if not changed or node_ctx is None:
        return
    scope = list(node_ctx["node"]["write_scope"] or [])
    me = (int(node_ctx["flow_run_id"]), str(node_ctx["node"]["key"]))
    others = [
        lease.pattern
        for lease in get_lease_table().holders(ctx.workspace_root)
        if (lease.owner.run_id, lease.owner.node_key) != me
    ]
    outside = sorted(
        p for p in changed if not in_scope(p, scope) and not any(in_scope(p, [o]) for o in others)
    )
    if not outside:
        return
    _GATES[int(node_id)] = {
        "kind": "scope_violation",
        "paths": outside[:50],
        "command": str(args.get("command") or "")[:500],
        "reason": "命令改动了本步骤范围之外的文件："
        + "、".join(outside[:3])
        + ("等" if len(outside) > 3 else ""),
    }
    logger.warning("[workflow] 命令改动了范围之外的文件 node=%s paths=%s", node_id, outside[:5])


# ── 工具调用台账（§6.5）────────────────────────────────────


def _args_preview(args: dict[str, Any]) -> str:
    try:
        text = json.dumps(args, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(args)
    return text[:300]


async def ledger_begin(
    agent: Any, tool_name: str, args: Any, llm_call_id: str | None, workspace: str = ""
) -> int | None:
    """工作流节点的工具调用开始时落台账；台账写入失败只记录日志，不影响工具执行。

    写入类操作在执行前记录目标文件的哈希，续跑对账时据此判断结果未知的写入是否已经生效。
    """
    from app.orchestration.workflow.effects import shell_effect, write_effect

    node_id = getattr(agent, "workflow_node_id", None)
    attempt_id = getattr(agent, "workflow_attempt_id", None)
    if node_id is None or attempt_id is None:
        return None
    call_args = args if isinstance(args, dict) else {}
    try:
        effect = write_effect(tool_name, call_args, workspace)
        if effect is None:
            effect = await shell_effect(tool_name, workspace)
        return await store.begin_tool_call(
            int(attempt_id),
            int(node_id),
            str(llm_call_id or ""),
            tool_name,
            _args_preview(call_args),
            effect,
        )
    except Exception:
        logger.warning("[workflow] 工具台账写入失败 tool=%s", tool_name, exc_info=True)
        return None


async def ledger_end(ticket: int | None, *, ok: bool | None) -> None:
    """工具调用有结果时补写台账；ok 为 None（中断、取消）时保持“已发出、结果未知”。"""
    if ticket is None or ok is None:
        return
    try:
        await store.end_tool_call(ticket, "done" if ok else "failed")
    except Exception:
        logger.warning("[workflow] 工具台账结果写入失败 ticket=%s", ticket, exc_info=True)


# ── 申请扩展写入范围 ───────────────────────────────────────

__all__ = [
    "REQUEST_SCOPE_SCHEMA",
    "REQUEST_SCOPE_TOOL",
    "after_tool",
    "before_tool",
    "forget_agent",
    "in_scope",
    "pop_gate",
]
