"""skill_view 工具（plan-230-1144 M1.2）。

改造前的问题：`Skill.content`（技能正文）从未送达模型——上下文只注入
`- 名称: 描述`（截 200 字、最多 10 条）的列表文本，全仓库不存在任何
"读取技能内容"的工具（grep `use_skill|load_skill|skill_view` 零匹配）。
AI 看得见技能名字却拿不到正文，自然不会、也无法主动使用技能。

本工具补齐：
- `skill_view`：按技能名返回完整正文（content），附触发条件与依赖工具；
  name 留空时返回全部已启用技能的索引（名称/描述/触发条件），供模型
  在不确定该用哪个技能时先浏览再定点加载。
- low risk 免审批（纯读操作，无副作用），四种权限模式全部可用。
"""
from typing import Any

from app.orchestration.tools.base import Tool, ToolContext, ToolResult
from app.persistence.database import async_session_factory

# 单条技能正文注入上限（超长技能截断，防止单个技能挤爆上下文）
_CONTENT_LIMIT = 12000
# 索引模式最多列出的技能数
_INDEX_LIMIT = 30
# plan-308-1542 需求2：未命中时的相近技能候选上限
_SUGGEST_LIMIT = 10
# plan-308-1542 需求2：技能同目录资源清单上限
_SIBLING_LIMIT = 50


class SkillViewTool(Tool):
    name = "skill_view"
    risk_level = "low"
    description = (
        "查看技能内容。技能是可复用的领域知识包/工作流指令，系统提示词的 "
        "Available Skills 段列出了可用技能。当任务与某技能的描述或触发条件"
        "匹配时，调用本工具（name=技能名）加载该技能的完整指令并遵照执行。"
        "不传 name 时返回全部已启用技能的索引（名称/描述/触发条件）。"
    )

    def function_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "技能名（Available Skills 中列出的名称）。留空返回全部技能索引。",
                        },
                    },
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        from sqlalchemy import select

        from app.persistence.models.skill import Skill

        name = str(args.get("name") or "").strip()

        async with async_session_factory() as db:
            if not name:
                # 索引模式：列出全部已启用技能供模型挑选
                res = await db.execute(
                    select(Skill).where(Skill.is_active.is_(True))
                    .order_by(Skill.source, Skill.name).limit(_INDEX_LIMIT)
                )
                skills = list(res.scalars().all())
                if not skills:
                    return ToolResult(ok=True, output="当前没有已启用的技能。", data={"count": 0})
                lines = [f"共 {len(skills)} 个已启用技能（用 skill_view(name=...) 加载完整指令）:"]
                for s in skills:
                    desc = (s.description or "").strip().replace("\n", " ")[:160]
                    trig = (s.trigger or "").strip().replace("\n", " ")[:100]
                    line = f"- {s.name}: {desc}" if desc else f"- {s.name}"
                    if trig:
                        line += f" [触发: {trig}]"
                    lines.append(line)
                return ToolResult(ok=True, output="\n".join(lines), data={"count": len(skills)})

            # 定点加载：按名称取技能正文
            res = await db.execute(select(Skill).where(Skill.name == name))
            skill = res.scalars().first()
            if skill is None:
                # 大小写/显示名兜底
                res = await db.execute(
                    select(Skill).where(Skill.display_name == name)
                )
                skill = res.scalars().first()
            if skill is None and ":" not in name:
                # plan-308-1542 需求2：插件技能注册名形如 "{plugin}:{skill}"，
                # AI 常直接用技能短名调用——按后缀 ":{name}" 反查，避免"找不到技能"。
                res = await db.execute(
                    select(Skill).where(Skill.name.like(f"%:{name}"))
                )
                skill = res.scalars().first()
            if skill is None:
                # plan-308-1542 需求2：未命中时给出候选（包含匹配），而不是让 AI 直接放弃。
                res = await db.execute(
                    select(Skill).where(Skill.name.like(f"%{name}%"))
                    .order_by(Skill.source, Skill.name).limit(_SUGGEST_LIMIT)
                )
                near = [s for s in res.scalars().all()]
                if near:
                    lines = [f"技能 '{name}' 不存在；相近的技能（用 skill_view(name=...) 加载）："]
                    for s in near:
                        lines.append(f"- {s.name}: {(s.description or '')[:120]}")
                    return ToolResult(ok=True, output="\n".join(lines), data={"count": len(near)})
                return ToolResult(ok=False, output="", error=f"技能 '{name}' 不存在（用不带参数的 skill_view 查看全部技能）")
            if not skill.is_active:
                return ToolResult(ok=False, output="", error=f"技能 '{name}' 已停用，请在设置 → 技能中启用")

            content = (skill.content or "").strip()
            # 扫描来源的技能正文在源文件里，按 path 兜底读取
            if not content and skill.path:
                try:
                    from pathlib import Path
                    p = Path(skill.path)
                    if p.is_file():
                        content = p.read_text(encoding="utf-8", errors="replace").strip()
                except Exception:  # noqa: BLE001
                    pass
            if not content:
                return ToolResult(
                    ok=False, output="",
                    error=f"技能 '{name}' 没有正文内容（content 为空且无可读源文件）",
                )

            truncated = len(content) > _CONTENT_LIMIT
            if truncated:
                content = content[:_CONTENT_LIMIT]

            # plan-308-1542 需求2：返回**路径与同目录资源清单**——
            # 技能常带 scripts/、references/ 等配套文件，AI 拿到目录才能读它们
            # （用户反馈"AI 说找不到技能目录"）。
            skill_path = (skill.path or "").strip()
            skill_dir = ""
            siblings: list[str] = []
            if skill_path:
                try:
                    from pathlib import Path as _P
                    p = _P(skill_path)
                    skill_dir = str(p.parent)
                    siblings = _list_siblings(p)
                except Exception:  # noqa: BLE001
                    skill_dir, siblings = "", []

            lines = [f"# 技能: {skill.display_name or skill.name}"]
            if skill.description:
                lines.append(f"描述: {skill.description.strip()}")
            if skill.trigger:
                lines.append(f"触发条件: {skill.trigger.strip()}")
            if skill.tools:
                lines.append(f"依赖工具: {', '.join(str(t) for t in skill.tools)}")
            if skill_path:
                lines.append(f"技能文件: {skill_path}")
            if skill_dir:
                lines.append(f"技能目录: {skill_dir}")
            if siblings:
                lines.append(f"同目录资源（可直接用 fs_read/terminal 读取）: {', '.join(siblings)}")
            lines.append("")
            lines.append(content)
            if truncated:
                lines.append(f"\n[正文超长已截断至 {_CONTENT_LIMIT} 字符]")
            return ToolResult(
                ok=True, output="\n".join(lines),
                data={"name": skill.name, "truncated": truncated,
                      "path": skill_path or None, "dir": skill_dir or None,
                      "siblings": siblings},
            )


def _list_siblings(skill_file) -> list[str]:
    """列出技能目录下的同级资源文件（含 scripts/references/assets 子目录，限 _SIBLING_LIMIT 条）。

    plan-308-1542 需求2：技能自带的脚本/参考文件必须可被 AI 发现，
    否则"技能能用"只停留在提示词层面，配套资源形同不存在。
    """
    out: list[str] = []
    try:
        base = skill_file.parent
        for sub in ("", "scripts", "references", "assets", "resources"):
            d = base / sub if sub else base
            if not d.is_dir():
                continue
            for f in sorted(d.iterdir()):
                if f.is_file() and f.name.lower() != "skill.md":
                    out.append(f.name if not sub else f"{sub}/{f.name}")
                if len(out) >= _SIBLING_LIMIT:
                    return out
    except OSError:
        return out
    return out
