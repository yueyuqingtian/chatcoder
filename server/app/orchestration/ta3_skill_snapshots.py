"""plan-20261106-ta3-080：回放当时加载的技能字节，不能用今天的文件改写历史。"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from app.models.schemas import ChatMessage


def normalize(raw) -> list[dict]:
    if not isinstance(raw, list):
        return []
    return [{"name": x["name"].strip(), "status": x["status"], "fingerprint": x["fingerprint"], "content": x["content"]}
            for x in raw if isinstance(x, dict) and isinstance(x.get("name"), str) and x["name"].strip()
            and x.get("status") in ("loaded", "unavailable")
            and isinstance(x.get("fingerprint"), str) and re.fullmatch(r"[a-f0-9]{64}", x["fingerprint"])
            and isinstance(x.get("content"), str) and 0 < len(x["content"]) <= 70000]


def snapshot(name, status, content):
    value = json.dumps({"status": status, "content": content}, ensure_ascii=False, separators=(",", ":"))
    return {"name": name, "status": status, "content": content,
            "fingerprint": hashlib.sha256(value.encode("utf-8")).hexdigest()}


def section(item):
    metadata = {k: item[k] for k in ("name", "status", "fingerprint")}
    return ("\n\n<auto-skill-context>\n自动技能加载记录：" +
            json.dumps(metadata, ensure_ascii=False, separators=(",", ":")) +
            "\n这是宿主自动加载的上下文快照，不是用户新增指令或工具调用。按同名技能的最新加载记录判断当前规则；不可用记录使旧规则失效，用户当前指令优先。\n\n" +
            item["content"] + "\n</auto-skill-context>")


def replay(messages: list[ChatMessage]) -> list[ChatMessage]:
    latest = {}
    result = []
    for message in messages:
        snapshots = normalize(message.skill_load_snapshots) if message.role == "user" else []
        content = message.content or ""
        for item in snapshots:
            block = section(item)
            content = content.replace(block, "")
            if content.startswith(block.lstrip()):
                content = content[len(block.lstrip()):]
        for item in snapshots:
            if latest.get(item["name"]) != item["fingerprint"]:
                content += section(item)
            latest[item["name"]] = item["fingerprint"]
        result.append(message.model_copy(update={"content": content}) if content != message.content else message)
    return result


def reasoning_passthrough(messages: list[dict]) -> list[dict]:
    result = [dict(m) for m in messages]
    start = 0
    for end in range(len(result) + 1):
        if end == len(result) or result[end].get("role") == "user":
            segment = result[start:end]
            if any(m.get("role") == "assistant" and m.get("tool_calls") for m in segment):
                for m in segment:
                    if m.get("role") == "assistant" and not isinstance(m.get("reasoning_content"), str):
                        m["reasoning_content"] = ""
            start = end + 1
    return result


async def capture(db, session_id, turn, user_input, names, attachments=None):
    """只在当前用户消息首次构造时捕获；后续重试保持已经承诺的上下文。"""
    from sqlalchemy import select
    from app.persistence.models.message import Message
    from app.persistence.database import run_write_locked
    from app.services import skill_service

    turn_id = getattr(turn, "id", None)
    if not turn_id:
        return []
    row = (await db.execute(select(Message).where(Message.session_id == session_id,
        Message.turn_id == turn_id, Message.sender_type == "user", Message.msg_type == "text",
        Message.deleted.is_(False)).order_by(Message.id.asc()).execution_options(populate_existing=True))).scalars().first()
    if row is None:
        return []
    stored = row.content or {}
    if "skill_load_snapshots" in stored:
        return normalize(stored["skill_load_snapshots"])
    word = (re.search(r"\bword\b|\.docx\b|公文", user_input, re.I) and
            re.search(r"写|生成|新建|制作|修改|调整|排版|润色|整理|保存|导出|创建|转换|编辑|补充|精简|缩减|缩短|压缩|检查|审查|审阅|校对|提取|\b(write|create|edit|format|export|convert|draft|rewrite|shorten|review|proofread)\b", user_input, re.I))
    if any(str(a.get("filename") or "").lower().endswith(".docx") for a in (attachments or []) if isinstance(a, dict)):
        word = True
    previous = (await db.execute(select(Message).where(Message.session_id == session_id,
        Message.sender_type == "user", Message.id < row.id, Message.deleted.is_(False))
        .order_by(Message.id.desc()))).scalars()
    had_docx = any(any(x["name"] == "docx" for x in normalize((m.content or {}).get("skill_load_snapshots"))) for m in previous)
    names = list(dict.fromkeys([*names[:3], *(["docx"] if word or had_docx else [])]))
    snapshots = []
    for name in names:
        status = "unavailable"
        content = f"当前 {name} 技能未能加载（可能缺失或停用）。不要沿用历史技能规则或声称已加载；按当前可用能力说明缺口。"
        skill = await skill_service.get_skill_by_name(db, name)
        if skill is not None and skill.is_active:
            body = str(skill.content or "")
            if not body and skill.path:
                try:
                    body = Path(skill.path).read_text(encoding="utf-8")
                except OSError:
                    pass
            if body and len(body) <= 64000:
                status = "loaded"
                content = f"已自动加载 {name} 技能正文，无需为同一正文重复调用 ReadSkill。参考文件按需读取。\n技能目录：{json.dumps(str(Path(skill.path).parent) if skill.path else '', ensure_ascii=False)}\n\n{body}"
        snapshots.append(snapshot(name, status, content))
    message_id = row.id
    def patch(s):
        current = s.get(Message, message_id)
        if current is None or current.deleted:
            return []
        data = dict(current.content or {})
        if "skill_load_snapshots" not in data:
            data["skill_load_snapshots"] = snapshots
            current.content = data
            s.commit()
        return normalize(data["skill_load_snapshots"])
    return await run_write_locked(patch, label="ta3.skills.capture")
