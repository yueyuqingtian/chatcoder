"""plan-20261103-fork：事务内构造完整前缀快照，历史实体永不进入执行注册表。"""
from copy import deepcopy
from datetime import datetime, timezone
import logging
from uuid import uuid4

from sqlalchemy import select

from app.persistence.models.message import Message, Session
from app.persistence.models.turn import Turn

logger = logging.getLogger(__name__)


class ForkSourceMissing(ValueError):
    pass


def pair_calls(rows):
    """按线程与调用出现位置配对；重复键只在当前待完成集合内唯一时可恢复。"""
    pending = {}
    pairs = {}
    for m in rows:
        c = m.content or {}
        if m.msg_type == "tool_call":
            pending.setdefault(m.thread_id, []).append(m)
        elif m.msg_type == "tool_result":
            candidates = pending.get(m.thread_id, [])
            key = c.get("call_key")
            matches = [x for x in candidates if (x.content or {}).get("call_key") == key]
            if not key:
                matches = [x for x in candidates if not (x.content or {}).get("call_key")
                           and (x.content or {}).get("tool") == c.get("tool")]
            if len(matches) != 1:
                return None
            call = matches[0]
            if c.get("tool") and (call.content or {}).get("tool") != c.get("tool"):
                return None
            candidates.remove(call)
            pairs[call.id] = m.id
    return None if any(pending.values()) else pairs


def complete_prefix(rows, turns):
    """首个不完整轮次截断；旧版无轮次记录必须有下一条用户消息作为结束证据。"""
    groups = []
    by_turn = {}
    for m in rows:
        if m.turn_id:
            t = turns.get(m.turn_id)
            if t and t.status == "rolled_back":
                continue
            if m.turn_id not in by_turn:
                group = {"turn": t, "rows": [], "legacy": False}
                by_turn[m.turn_id] = group
                groups.append(group)
            by_turn[m.turn_id]["rows"].append(m)
        elif m.sender_type == "user" and m.thread_id is None:
            groups.append({"turn": None, "rows": [m], "legacy": True})
        elif groups:
            groups[-1]["rows"].append(m)
        else:
            groups.append({"turn": None, "rows": [m], "legacy": False})
    blocking_ids = [t.id for t in turns.values() if t.status not in ("completed", "rolled_back")]
    first_blocked = min(blocking_ids, default=None)
    accepted, paired, accepted_turns = [], {}, []
    reason = None
    for i, g in enumerate(groups):
        t, batch = g["turn"], g["rows"]
        if t is not None and first_blocked is not None and t.id >= first_blocked:
            reason = "当前未完成轮次未包含在分支中"
            break
        if t is None and not g["legacy"]:
            if any(m.sender_type != "system" for m in batch):
                reason = "部分历史缺少轮次归属，已截取此前完整内容"
                break
        elif t is not None:
            if t.status != "completed":
                reason = "当前未完成轮次未包含在分支中"
                break
        elif i == len(groups) - 1 or not any(
                m.sender_type == "agent" and m.msg_type in ("text", "tool_result") for m in batch):
            reason = "旧版历史末轮的结束边界无法确认，已排除该轮"
            break
        pairs = pair_calls(batch)
        if pairs is None:
            reason = "部分历史工具记录不完整，已截取此前完整轮次"
            break
        accepted.extend(batch)
        paired.update(pairs)
        if t is not None or g["legacy"]:
            accepted_turns.append(g)
    if reason is None and first_blocked is not None:
        reason = "当前未完成轮次未包含在分支中"
    # plan-20261103-fork：交错轮次不能越过已确定的截取位置。
    if reason:
        rejected = {m.id for g in groups for m in g["rows"]} - {m.id for m in accepted}
        if rejected:
            floor = min(rejected)
            safe = [g for g in accepted_turns if max(m.id for m in g["rows"]) < floor]
            safe_ids = {m.id for g in safe for m in g["rows"]}
            accepted = [m for m in accepted if m.id < floor and (m.turn_id is None or m.id in safe_ids)]
            accepted_turns = safe
    return sorted(accepted, key=lambda m: m.id), accepted_turns, paired, reason


def _record(row):
    return {col.name: deepcopy(getattr(row, col.name)) for col in row.__table__.columns}


def _remap(value, maps):
    """仅转换声明过的结构引用，工具参数中的普通数字和文本保持原值。"""
    if isinstance(value, list):
        return [_remap(v, maps) for v in value]
    if not isinstance(value, dict):
        return value
    out = {}
    for key, v in value.items():
        mapping = maps.get(key)
        if mapping is not None:
            out[key] = mapping.get(v) if not isinstance(v, (list, dict)) else v
        elif key in ("shadowed_ids", "compacted_ids", "compacted_exceptions", "summarized_ids"):
            out[key] = [maps["message_id"][x] for x in (v or []) if x in maps["message_id"]]
        else:
            out[key] = _remap(v, maps)
    return out


def copy_snapshot(s, session_id, title=None, request_id=None):
    from app.orchestration.approval_policy import normalize_approval_mode, normalize_execution_mode
    from app.persistence.models.agent import Agent
    from app.persistence.models.task import Task
    from app.persistence.models.rollback import RollbackWrite
    from app.persistence.models.workflow import WorkflowRun, WorkflowNode

    src = s.get(Session, session_id)
    if src is None:
        raise ForkSourceMissing("来源会话不存在")
    if request_id:
        existing = s.scalar(select(Session.id).where(Session.fork_parent_id == session_id,
                                                      Session.fork_request_id == request_id))
        if existing is not None:
            return existing
    rows = list(s.scalars(select(Message).where(Message.session_id == session_id,
                      Message.deleted.is_(False)).order_by(Message.id)))
    turns = {t.id: t for t in s.scalars(select(Turn).where(Turn.session_id == session_id))}
    rows, groups, pairs, reason = complete_prefix(rows, turns)
    included = {m.id for m in rows}
    # 摘要跨越截取边界时保留原文，不能注入来自未来的结论。
    ctx = src.shared_context or {}
    compactions = [c for c in ctx.get("compactions", [])
                   if c.get("summary_message_id") in included
                   and set(c.get("shadowed_ids") or []).issubset(included)]
    valid_summaries = {c["summary_message_id"] for c in compactions}
    rows = [m for m in rows if not ((m.content or {}).get("checkpoint")
                                     and m.id not in valid_summaries)]
    child = Session(project_id=src.project_id, model_id=src.model_id,
        title=(title or src.title or "会话")[:160], title_source="named",
        fork_parent_id=session_id, fork_request_id=request_id, status="active",
        worktree_path=src.worktree_path,
        permission_mode=normalize_execution_mode(src.permission_mode),
        approval_mode=normalize_approval_mode(src.approval_mode), reasoning_effort=src.reasoning_effort)
    s.add(child)
    s.flush()
    tmap, mmap = {}, {}
    for g in groups:
        t, batch = g["turn"], g["rows"]
        new = Turn(session_id=child.id, status="completed", inherited=True,
            summary=t.summary if t else None, token_usage=t.token_usage if t else 0,
            started_at=t.started_at if t else batch[0].created_at,
            completed_at=t.completed_at if t else batch[-1].created_at)
        s.add(new)
        s.flush()
        g["new"] = new
        if t:
            tmap[t.id] = new.id
    old_snapshot = src.fork_snapshot or {}
    threads = sorted({m.thread_id for m in rows if m.thread_id is not None})
    amap = {aid: -(i + 1) for i, aid in enumerate(threads)}
    task_rows = list(s.scalars(select(Task).where(Task.session_id == session_id)))
    tasks = [_record(t) for t in task_rows if t.turn_id in tmap]
    tasks += [deepcopy(t) for t in old_snapshot.get("tasks", []) if t.get("turn_id") in tmap]
    taskmap = {t["id"]: -(i + 1) for i, t in enumerate(tasks)}
    runs = [_record(r) for r in s.scalars(select(WorkflowRun).where(WorkflowRun.session_id == session_id))
            if r.origin_turn_id in tmap and r.last_turn_id in (*tmap, None)]
    runs += [deepcopy(r) for r in old_snapshot.get("workflows", []) if r.get("origin_turn_id") in tmap]
    runmap = {r["id"]: -(i + 1) for i, r in enumerate(runs)}
    nodes = [_record(n) for n in s.scalars(select(WorkflowNode).where(WorkflowNode.run_id.in_(list(runmap))))] if runmap else []
    nodes += [deepcopy(n) for n in old_snapshot.get("nodes", []) if n.get("run_id") in runmap]
    nodemap = {n["id"]: -(i + 1) for i, n in enumerate(nodes)}
    callmap = {}
    reverse_pairs = {v: k for k, v in pairs.items()}
    call_ids = {m.id for m in rows if m.msg_type == "tool_call"}
    for call_id in call_ids:
        callmap[call_id] = "fork_" + uuid4().hex
    legacy_groups = {m.id: g for g in groups for m in g["rows"] if m.turn_id is None}
    copies = []
    for m in rows:
        g = legacy_groups.get(m.id)
        new = Message(session_id=child.id, turn_id=tmap.get(m.turn_id) or (g["new"].id if g else None),
            thread_id=amap.get(m.thread_id), sender_type=m.sender_type,
            sender_id=amap.get(m.sender_id), msg_type=m.msg_type,
            content={}, token_usage=m.token_usage, created_at=m.created_at)
        s.add(new)
        copies.append(new)
    s.flush()
    mmap = {m.id: new.id for m, new in zip(rows, copies)}
    copied_by_source = dict(zip((m.id for m in rows), copies))
    maps = {"turn_id": tmap, "origin_turn_id": tmap, "last_turn_id": tmap,
            "message_id": mmap, "user_message_id": mmap, "summary_message_id": mmap,
            "spawn_message_id": mmap, "agent_id": amap, "thread_id": amap, "sender_id": amap,
            "parent_agent_id": amap, "task_id": taskmap, "parent_task_id": taskmap,
            "program_id": runmap, "run_id": runmap, "parent_run_id": runmap,
            "node_id": nodemap, "workflow_node_id": nodemap, "session_id": {session_id: child.id},
            "subsession_id": {}, "subsession": {}, "sub_session_id": {}}
    key_candidates = {}
    keymap = {}
    for m in rows:
        new = copied_by_source[m.id]
        content = _remap(deepcopy(m.content or {}), maps)
        content["fork_history"] = True
        if m.msg_type in ("tool_call", "tool_result"):
            call_id = m.id if m.msg_type == "tool_call" else reverse_pairs.get(m.id)
            content["call_key"] = callmap[call_id]
            if m.msg_type == "tool_call":
                key_candidates.setdefault((m.turn_id, (m.content or {}).get("call_key")), []).append(callmap[call_id])
        new.content = content
    for g in groups:
        t = g["turn"]
        user_id = t.user_message_id if t else next((m.id for m in g["rows"] if m.sender_type == "user"), None)
        g["new"].user_message_id = mmap.get(user_id)
    agents = []
    for aid, target in amap.items():
        source = s.get(Agent, aid) if aid > 0 else None
        old = next((a for a in old_snapshot.get("agents", []) if a["agent_id"] == aid), {})
        tid = source.turn_id if source else old.get("turn_id")
        status = source.status if source else old.get("status", "done")
        agents.append({"agent_id": target, "name": source.name if source else old.get("name", "历史子代理"),
            "turn_id": tmap.get(tid), "turn_status": "completed", "status": status if status in ("done", "failed", "terminated") else "terminated",
            "task_id": None, "task_title": None, "workflow": None, "token_usage": 0,
            "message_count": sum(m.thread_id == aid for m in rows), "files_count": 0,
            "created_at": source.created_at if source else old.get("created_at"), "updated_at": None,
            "inherited": True})
    keymap = {key: values[0] for key, values in key_candidates.items() if len(values) == 1}
    writes = [_record(w) for w in s.scalars(select(RollbackWrite).where(RollbackWrite.session_id == session_id).order_by(RollbackWrite.id)) if w.turn_id in tmap]
    writes += [deepcopy(w) for w in old_snapshot.get("writes", []) if w.get("turn_id") in tmap]
    for w in writes:
        w["call_key"] = keymap.get((w["turn_id"], w.get("call_key")))
    for kind, records, mapping in (("tasks", tasks, taskmap), ("workflows", runs, runmap), ("nodes", nodes, nodemap)):
        for record in records:
            record["id"] = mapping[record["id"]]
    child.fork_snapshot = {"agents": agents, "tasks": _remap(tasks, maps),
        "workflows": _remap(runs, maps), "nodes": _remap(nodes, maps), "writes": _remap(writes, maps)}
    mapped_compactions = _remap(deepcopy(compactions), maps)
    shadowed = set()
    for c in mapped_compactions:
        if not c.get("restored"):
            shadowed.update(c.get("shadowed_ids") or [])
    child.shared_context = {"compactions": mapped_compactions, "compacted_ids": sorted(shadowed),
                            "compacted_floor": 0, "compacted_exceptions": []}
    now = datetime.now(timezone.utc).isoformat()
    warnings = [reason] if reason else []
    if not groups:
        warnings.append("暂无完整轮次，已创建空白分支")
    child.fork_info = {"version": 1, "source_session_id": session_id, "source_created_at": str(src.created_at),
        "source_turn_id": groups[-1]["turn"].id if groups and groups[-1]["turn"] else None,
        "copied_turns": len(groups), "created_at": now, "excluded_incomplete": bool(reason),
        "warnings": warnings, "output_sources": sorted(set((src.fork_info or {}).get("output_sources", []) + [session_id])), "last_message_id": max(mmap.values(), default=0),
        "message_watermark": max((m.id for m in rows), default=0)}
    # plan-20261103-fork-ui：来源信息只留内部元数据，普通消息流不插入分支标识。
    s.commit()
    logger.info("[fork] source=%s target=%s watermark=%s turns=%s excluded=%s", session_id,
                child.id, child.fork_info["message_watermark"], len(groups), bool(reason))
    return child.id
