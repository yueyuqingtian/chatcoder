"""任务复杂度评估、结构化拆分与能力校验。

该模块只负责规划，不执行文件或终端操作：
- 规则仅处理极端输入；
- 主要决策交给当前会话模型；
- 模型输出必须经过结构化校验；
- 拆分结果只保留能映射到当前工作区/工具能力的可执行小点。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from app.core.config import settings
from app.models.base import ModelProvider
from app.models.schemas import ChatMessage, ChatRequest

Decision = Literal["direct", "plan", "split"]


@dataclass
class ComplexityVerdict:
    decision: Decision
    complexity: str
    should_split: bool
    reasons: list[str] = field(default_factory=list)
    suggested_steps: list[str] = field(default_factory=list)
    source: str = "fallback"


@dataclass
class PlannedStep:
    title: str
    summary: str = ""
    acceptance: str = ""
    depends_on: list[int] = field(default_factory=list)
    estimate: int | None = None
    supported: bool = True
    unsupported_reason: str | None = None


_FORBIDDEN_CAPABILITY_PATTERNS = (
    re.compile(r"git\s*(commit|push)|commit\s+to\s+git|push\s+(the\s+)?code", re.I),
    re.compile(r"提交(?:到|代码到)?\s*(git|远程|仓库)|推送(?:代码|到远程)", re.I),
    re.compile(r"创建|新增|管理\s*(智能体|agent)\s*(团队|实例)?", re.I),
    re.compile(r"部署(?:服务|应用)|上线生产|发布到生产", re.I),
)


def _contains_explicit_list(text: str) -> bool:
    if re.search(r"(?:^|\n)\s*(?:[-*]|\d+[.)]|[一二三四五六七八九十]+[、.])\s+", text):
        return True
    # 中文常见的多交付物连接词，只作为预筛信号，不做复杂度分数。
    return len(re.findall(r"(?:实现|修复|新增|添加|支持|优化|改造|迁移)", text)) >= 3


def _parse_json(raw: str) -> dict[str, Any] | None:
    text = (raw or "").strip()
    if not text:
        return None
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            return None
        try:
            obj = json.loads(match.group(0))
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None


def _normalise_decision(complexity: str, should_split: bool) -> Decision:
    if should_split or complexity.lower() == "high":
        return "split"
    if complexity.lower() == "low":
        return "direct"
    return "plan"


async def evaluate_complexity(
    provider: ModelProvider | None,
    *,
    user_text: str,
    mode: str | None = None,
    workspace: str = "",
) -> ComplexityVerdict:
    """使用规则预筛 + LLM 语义判断，返回执行决策。"""
    text = user_text.strip()
    if mode == "plan":
        return ComplexityVerdict(
            decision="split", complexity="high", should_split=True,
            reasons=["/plan 模式完成方案文档后默认拆分"], source="plan_mode",
        )

    if len(text) <= settings.complexity_direct_max_chars and not _contains_explicit_list(text):
        return ComplexityVerdict(
            decision="direct", complexity="low", should_split=False,
            reasons=["请求极短且未发现多交付物列表"], source="rule_prefilter",
        )
    if _contains_explicit_list(text):
        return ComplexityVerdict(
            decision="split", complexity="high", should_split=True,
            reasons=["输入包含明确的多个交付物或步骤"], source="rule_prefilter",
        )

    if provider is None:
        return ComplexityVerdict(
            decision="plan", complexity="medium", should_split=False,
            reasons=["模型不可用，采用保守的直接规划回退"], source="fallback",
        )

    prompt = (
        "Assess whether the user's request should be split into executable subtasks. "
        "The input may be colloquial, incomplete, or contain implicit goals. "
        "Use semantic understanding, not keyword counting. Return JSON only: "
        '{"complexity":"low|medium|high","should_split":true|false,'
        '"reasons":["short reason"],"suggested_steps":["short title"]}. '
        "Use high/true when the request has multiple independently verifiable goals, "
        "crosses layers/files, or has meaningful dependencies. Use low/false for a small "
        "single outcome. Do not invent capabilities that are not available in a coding workspace.\n\n"
        f"Workspace: {workspace or '(unknown)'}\nUser request:\n{text[:12000]}"
    )
    try:
        response = await provider.chat(ChatRequest(
            messages=[
                ChatMessage(role="system", content="You are a task planning classifier. Output valid JSON only."),
                ChatMessage(role="user", content=prompt),
            ], model="", temperature=0, max_tokens=700, reasoning_effort="low",
        ))
        data = _parse_json(response.content or "")
        if data is None:
            raise ValueError("invalid complexity JSON")
        complexity = str(data.get("complexity", "medium")).lower()
        if complexity not in {"low", "medium", "high"}:
            complexity = "medium"
        should_split = bool(data.get("should_split", False))
        reasons = [str(x)[:180] for x in data.get("reasons", []) if str(x).strip()][:5]
        suggested = [str(x).strip()[:120] for x in data.get("suggested_steps", []) if str(x).strip()][:settings.max_subagents_per_turn]
        return ComplexityVerdict(
            decision=_normalise_decision(complexity, should_split),
            complexity=complexity,
            should_split=should_split,
            reasons=reasons or ["模型完成语义评估"],
            suggested_steps=suggested,
            source="llm",
        )
    except Exception:
        # 语义判断失败不能把普通请求误拆；/plan 已在上面确定性处理。
        return ComplexityVerdict(
            decision="plan", complexity="medium", should_split=False,
            reasons=["语义评估失败，采用保守回退"], source="fallback",
        )


async def decompose_request(
    provider: ModelProvider | None,
    *,
    source_text: str,
    suggested_steps: list[str] | None = None,
    plan_mode: bool = False,
) -> list[PlannedStep]:
    """将用户请求或 /plan 文档拆成可执行小点。失败时返回安全的单步回退。"""
    hints = "\n".join(f"- {s}" for s in (suggested_steps or [])[:settings.max_subagents_per_turn])
    prompt = (
        "Decompose the following coding task into a small list of independently verifiable "
        "executable steps. Return JSON only in the form "
        '{"subtasks":[{"title":"...","summary":"...","acceptance":"...",'
        '"depends_on":[0],"estimate":1}]}.'
        f" Maximum {settings.max_subagents_per_turn} subtasks. Each title must be a short human-readable "
        "verb-object title (under 32 characters); when a step clearly involves specific files, "
        "use the format 'file.py: what to change' so the task card reads like a file checklist. "
        "Each acceptance must describe how to verify the step (a concrete check, command, or expected behavior). "
        "Each step must have one concrete outcome. "
        "Do not create steps for Git commit/push, deployment, creating agents, or any capability "
        "not present in a normal coding workspace. Do not expose internal fields in titles.\n"
        + (f"Semantic hints:\n{hints}\n" if hints else "")
        + ("This is a /plan document; preserve the document's actionable order.\n" if plan_mode else "")
        + f"Source:\n{source_text[:18000]}"
    )
    raw = ""
    if provider is not None:
        try:
            response = await provider.chat(ChatRequest(
                messages=[
                    ChatMessage(role="system", content="You are a precise task decomposer. Output valid JSON only."),
                    ChatMessage(role="user", content=prompt),
                ], model="", temperature=0, max_tokens=2400, reasoning_effort="low",
            ))
            raw = response.content or ""
        except Exception:
            raw = ""

    data = _parse_json(raw)
    raw_items = data.get("subtasks", []) if data else []
    steps: list[PlannedStep] = []
    if isinstance(raw_items, list):
        for item in raw_items[:settings.max_subagents_per_turn]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "")).strip()
            if not title:
                continue
            deps = item.get("depends_on", [])
            if not isinstance(deps, list):
                deps = []
            clean_deps = sorted({int(x) for x in deps if isinstance(x, int) and 0 <= x < len(steps)})
            estimate = item.get("estimate")
            try:
                estimate = max(1, min(5, int(estimate))) if estimate is not None else None
            except (TypeError, ValueError):
                estimate = None
            step = PlannedStep(
                title=title[:200], summary=str(item.get("summary", "")).strip()[:4000],
                acceptance=str(item.get("acceptance", "")).strip()[:500],
                depends_on=clean_deps, estimate=estimate,
            )
            for pattern in _FORBIDDEN_CAPABILITY_PATTERNS:
                if pattern.search(f"{step.title} {step.summary}"):
                    step.supported = False
                    step.unsupported_reason = "当前系统没有对应的执行能力"
                    break
            steps.append(step)

    supported = [s for s in steps if s.supported]
    if supported:
        # 过滤 unsupported 后重建依赖索引，避免悬空引用。
        old_to_new = {old: new for new, old in enumerate(i for i, s in enumerate(steps) if s.supported)}
        for step in supported:
            step.depends_on = [old_to_new[d] for d in step.depends_on if d in old_to_new]
        return supported

    # 结构化调用不可用时仍然保证 /plan 有可确认的步骤，不直接伪造多个能力。
    fallback_titles = [x[:120] for x in (suggested_steps or []) if x.strip()][:settings.max_subagents_per_turn]
    if not fallback_titles:
        fallback_titles = ["执行用户请求" if not plan_mode else "按方案执行任务"]
    return [PlannedStep(title=title, summary=source_text[:4000], acceptance="完成该小点并验证结果") for title in fallback_titles]
