"""工作流图规格：解析、校验、规范化与计划期冲突预检（纯逻辑，无数据库与运行时副作用）。

对应设计 §4.5（运行时自主编排）与 §5A.1 第 1 点（计划期预检）。主代理提交的 JSON 先经
parse_plan 解析（结构错误转为诊断，不抛异常），再由 validate_plan 逐条检查；诊断以结构化
形式返回，供模型据此修正（最多重试两次，次数由调用方控制）。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.config import settings
from app.orchestration.workflow.conflicts import normalize_scope, scopes_overlap

# merge 节点仅由运行时在冲突时创建，不在模型可写的 kind 之中
NodeKind = Literal["explore", "execute", "review", "check", "gate"]
FlowIsolation = Literal["shared", "worktree"]

READONLY_KINDS = frozenset({"explore", "review"})
# 独立工作树子工作流末尾的并入步骤（由运行时追加，计划中不得使用该标识）
INTEGRATE_NODE_KEY = "integrate"
WRITABLE_KINDS = frozenset({"execute"})
LLM_KINDS = frozenset({"explore", "execute", "review"})
DEFAULT_ROLE: dict[str, str] = {
    "explore": "explorer",
    "execute": "implementer",
    "review": "reviewer",
}
# 与设置项 workflow_default_max_rework 的默认值保持一致
DEFAULT_MAX_REWORK = 2

MAX_FLOWS = 6
MAX_NODES_PER_FLOW = 8
MAX_NODES_TOTAL = 30
MAX_REWORK = 3

_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
# 内置常用检查：任何权限模式下都直接执行；其余命令按会话的权限模式裁决（见 workflow/commands.py）
_COMMAND_ALLOWLIST = tuple(
    re.compile(p)
    for p in (
        r"npm run (typecheck|build|test|lint)\b",
        r"npx tsc --noEmit\b",
        r"npm test\b",
        r"pytest\b",
        r"python -m pytest\b",
        r"ruff check\b",
    )
)


# 程序级集成验收展开出的子工作流标识（由 after 生成，不能被模板替换）
INTEGRATION_FLOW_KEY = "integration"


def command_allowed(command: str) -> bool:
    cmd = command.strip()
    return any(p.match(cmd) for p in _COMMAND_ALLOWLIST)


# 规划期命令提示：返回 (诊断代码, 文案)；None 表示该命令无需提示
CommandNote = Callable[[str], "tuple[str, str] | None"]


def default_command_note(command: str) -> tuple[str, str] | None:
    """没有会话上下文时（模板编辑等）的口径。

    plan-20261014-fix4：是否需要审批取决于运行时会话的权限模式，规划期只能如实说明，
    不能断言「需要审批」——完全访问下这类提示曾误导模型反复改计划。有会话时由调用方按当前模式判定。
    """
    if command_allowed(command):
        return None
    return (
        "command_needs_approval",
        f"命令『{command}』不是内置检查，执行时按会话的权限模式决定是否需要审批",
    )


class NodeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=32)
    title: str = Field(default="", max_length=40)
    kind: NodeKind
    role: str | None = Field(default=None, max_length=64)
    task: str = Field(default="", max_length=4000)
    inputs: list[str] = Field(default_factory=list, max_length=8)
    acceptance: list[str] = Field(default_factory=list, max_length=12)
    write_scope: list[str] = Field(default_factory=list, max_length=20)
    commands: list[str] = Field(default_factory=list, max_length=8)
    rework_to: str | None = Field(default=None, max_length=32)
    max_rework: int | None = Field(default=None, ge=0, le=MAX_REWORK)
    question: str | None = Field(default=None, max_length=300)


class FlowSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=32)
    title: str = Field(default="", max_length=40)
    depends_on: list[str] = Field(default_factory=list, max_length=6)
    # 未声明时取设置中的默认隔离方式（见 effective_isolation）
    isolation: FlowIsolation | None = None
    nodes: list[NodeSpec] = Field(default_factory=list)


class AfterSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    acceptance: list[str] = Field(default_factory=list, max_length=12)
    commands: list[str] = Field(default_factory=list, max_length=8)


class PlanSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=40)
    goal: str = Field(min_length=1, max_length=2000)
    rationale: str = Field(default="", max_length=200)
    based_on: str | None = Field(default=None, max_length=80)
    flows: list[FlowSpec] = Field(default_factory=list)
    after: AfterSpec | None = None


@dataclass(frozen=True)
class Diagnostic:
    code: str
    path: str
    message: str
    suggestion: str | None = None


@dataclass
class ValidationResult:
    errors: list[Diagnostic] = field(default_factory=list)
    warnings: list[Diagnostic] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)
    normalized: PlanSpec | None = None

    @property
    def ok(self) -> bool:
        return not self.errors


def _error(
    res: ValidationResult, code: str, path: str, message: str, suggestion: str | None = None
) -> None:
    res.errors.append(Diagnostic(code, path, message, suggestion))


def _warning(
    res: ValidationResult, code: str, path: str, message: str, suggestion: str | None = None
) -> None:
    res.warnings.append(Diagnostic(code, path, message, suggestion))


# Pydantic 错误类型 → 界面文案（界面文案一律简体中文）；未登记的类型统一为「格式不正确」
_SCHEMA_TEXT: dict[str, str] = {
    "missing": "缺少必填项",
    "string_too_short": "不能为空",
    "string_too_long": "超过长度上限",
    "string_type": "应为文本",
    "string_unicode": "文本编码不正确",
    "int_type": "应为整数",
    "int_parsing": "应为整数",
    "bool_type": "应为是或否",
    "list_type": "应为列表",
    "dict_type": "应为对象",
    "literal_error": "取值不在可选范围内",
    "extra_forbidden": "包含不支持的字段",
    "too_short": "数量过少",
    "too_long": "数量超过上限",
}


def _schema_path(loc: tuple[object, ...]) -> str:
    """Pydantic 错误位置 (flows, 0, nodes, 1, title) → 诊断路径 flows[0].nodes[1].title。

    与其它诊断使用同一格式，编辑器据此把错误标到对应步骤与字段旁边。
    """
    path = ""
    for part in loc:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += f".{part}" if path else str(part)
    return path or "$"


def parse_plan(data: object) -> tuple[PlanSpec | None, list[Diagnostic]]:
    """解析模型提交的图规格。结构错误与 merge 节点以诊断返回，不抛异常。"""
    if not isinstance(data, Mapping):
        return None, [Diagnostic("schema", "$", "计划必须是对象")]
    diags: list[Diagnostic] = []
    flows = data.get("flows")
    if isinstance(flows, list):
        for fi, flow in enumerate(flows):
            nodes = flow.get("nodes") if isinstance(flow, Mapping) else None
            if not isinstance(nodes, list):
                continue
            for ni, node in enumerate(nodes):
                if isinstance(node, Mapping) and node.get("kind") == "merge":
                    diags.append(
                        Diagnostic(
                            "merge_not_allowed",
                            f"flows[{fi}].nodes[{ni}].kind",
                            "merge 节点仅由运行时在冲突时创建，不能由模型直接编写",
                            "改用 execute 节点，并声明 write_scope",
                        )
                    )
    if diags:
        return None, diags
    try:
        spec = PlanSpec.model_validate(data)
    except ValidationError as exc:
        for err in exc.errors():
            text = _SCHEMA_TEXT.get(err.get("type", ""), "格式不正确")
            diags.append(Diagnostic("schema", _schema_path(err["loc"]), f"字段不合法：{text}"))
        return None, diags
    return spec, []


def validate_plan(
    spec: PlanSpec,
    *,
    role_exists: Callable[[str], bool] = lambda _name: True,
    readonly_only: bool = False,
    hot_patterns: Iterable[str] = (),
    command_note: CommandNote = default_command_note,
) -> ValidationResult:
    """校验规格：节点类型规则、角色、计划模式权限天花板、命令提示、结构上限与计划期冲突预检。"""
    res = ValidationResult()
    original = spec
    # 程序级集成验收先展开为一个子工作流，之后与其它子工作流一样参与全部校验
    spec = _expand_after(spec)
    flows = spec.flows
    # plan-20261017-flow-limit：after 展开的集成验收也占子工作流名额，但模型提交时看不到它。
    # 超限提示必须说明这 N 个的来源，否则「提交 6 个却提示 7 个」会被误判成数组解析错误。
    integration_flows = len(flows) - len(original.flows)
    integration_nodes = sum(len(f.nodes) for f in flows[len(original.flows) :])
    if not flows:
        _error(res, "empty_program", "flows", "至少需要一个子工作流")
    if len(flows) > MAX_FLOWS:
        keys = "、".join(f.key for f in flows)
        auto = (
            f"；其中 {integration_flows} 个由 after 自动展开为集成验收"
            if integration_flows
            else ""
        )
        _error(
            res,
            "too_many_flows",
            "flows",
            f"子工作流最多 {MAX_FLOWS} 个，当前 {len(flows)} 个（{keys}{auto}）",
            "合并相近的子工作流；若使用了 based_on，未提交的骨架子工作流会保留并占用名额",
        )
    flow_keys = [f.key for f in flows]
    _report_duplicates(res, flow_keys, "flows", "duplicate_flow_key")

    total_nodes = 0
    for fi, flow in enumerate(flows):
        fp = f"flows[{fi}]"
        if not _KEY_RE.match(flow.key):
            _error(
                res,
                "bad_key",
                f"{fp}.key",
                f"子工作流标识『{flow.key}』不合法",
                "使用小写字母开头的 slug，如 backend",
            )
        if not flow.nodes:
            _error(res, "empty_flow", fp, f"子工作流『{flow.key}』没有节点")
        if len(flow.nodes) > MAX_NODES_PER_FLOW:
            _error(
                res,
                "too_many_nodes",
                f"{fp}.nodes",
                f"单个子工作流最多 {MAX_NODES_PER_FLOW} 个节点，当前 {len(flow.nodes)} 个",
                "拆成两个子工作流",
            )
        total_nodes += len(flow.nodes)
        for dep in flow.depends_on:
            if dep == flow.key:
                _error(res, "self_dependency", f"{fp}.depends_on", "子工作流不能依赖自己")
            elif dep not in flow_keys:
                _error(
                    res,
                    "unknown_dependency",
                    f"{fp}.depends_on",
                    f"依赖的子工作流『{dep}』不存在",
                )
        node_keys = [n.key for n in flow.nodes]
        if effective_isolation(flow) == "worktree" and INTEGRATE_NODE_KEY in node_keys:
            _error(
                res,
                "reserved_node_key",
                f"{fp}.nodes",
                f"节点标识『{INTEGRATE_NODE_KEY}』为独立工作树子工作流的并入步骤保留",
                "换一个节点标识",
            )
        _report_duplicates(res, node_keys, f"{fp}.nodes", "duplicate_node_key")
        index = {key: i for i, key in enumerate(node_keys)}
        for ni, node in enumerate(flow.nodes):
            _check_node(
                res,
                node,
                f"{fp}.nodes[{ni}]",
                ni,
                flow.nodes,
                index,
                role_exists,
                readonly_only,
                command_note,
            )

    if total_nodes > MAX_NODES_TOTAL:
        auto = (
            f"，其中集成验收展开占 {integration_nodes} 个节点"
            if integration_nodes
            else ""
        )
        _error(
            res,
            "too_many_nodes_total",
            "flows",
            f"整个程序最多 {MAX_NODES_TOTAL} 个节点，当前 {total_nodes} 个{auto}",
            "减少节点或分期运行",
        )
    cycle = _find_cycle({f.key: list(f.depends_on) for f in flows})
    if cycle:
        # 用子工作流的标题描述环路：界面上看到的是名称而不是内部标识
        titles = {f.key: f.title or f.key for f in flows}
        _error(
            res,
            "dependency_cycle",
            "flows",
            "子工作流依赖成环：" + " → ".join(titles.get(k, k) for k in cycle),
            "去掉其中一个子工作流的前置依赖，打破循环",
        )
    if original.after is not None:
        for cmd in original.after.commands:
            note = command_note(cmd)
            if note is not None:
                _warning(res, note[0], "after.commands", note[1])

    pairs = _check_concurrent_conflicts(res, spec, hot_patterns)
    res.stats = _stats(spec, total_nodes, pairs, integration_flows)
    if res.ok:
        res.normalized = _normalize(spec)
    return res


def _expand_after(spec: PlanSpec) -> PlanSpec:
    """程序级集成验收（after）展开为一个依赖全部子工作流的 integration 子工作流（设计 §4.5）。

    集成子工作流只含一个只读的评估节点：逐条核对程序级验收标准，并运行程序级检查命令，
    命令结果以实际输出为准。没有验收标准与命令、或没有任何子工作流时不展开（由结构校验报错）。
    """
    after = spec.after
    if after is None or not (after.acceptance or after.commands) or not spec.flows:
        return spec.model_copy(update={"after": None})
    taken = {f.key for f in spec.flows}
    key, n = INTEGRATION_FLOW_KEY, 1
    while key in taken:
        n += 1
        key = f"integration{n}"
    verify = NodeSpec(
        key="verify",
        title="集成验收",
        kind="review",
        task="对所有子工作流的整体改动做集成验收：逐条核对验收标准，检查命令以实际结果为准。",
        acceptance=list(after.acceptance) or ["集成检查命令全部通过"],
        commands=list(after.commands),
    )
    # model_construct：依赖列表可能已超过子工作流上限，由 validate_plan 统一报告而不是在这里抛异常
    flow = FlowSpec.model_construct(
        key=key,
        title="集成验收",
        depends_on=[f.key for f in spec.flows],
        isolation="shared",
        nodes=[verify],
    )
    return spec.model_copy(update={"flows": [*spec.flows, flow], "after": None})


def _report_duplicates(res: ValidationResult, keys: Sequence[str], path: str, code: str) -> None:
    seen: set[str] = set()
    for key in keys:
        if key in seen:
            _error(res, code, path, f"标识『{key}』重复", "同一范围内的标识必须唯一")
        seen.add(key)


def _check_node(
    res: ValidationResult,
    node: NodeSpec,
    path: str,
    ni: int,
    nodes: Sequence[NodeSpec],
    index: Mapping[str, int],
    role_exists: Callable[[str], bool],
    readonly_only: bool,
    command_note: CommandNote = default_command_note,
) -> None:
    if not _KEY_RE.match(node.key):
        _error(
            res,
            "bad_key",
            f"{path}.key",
            f"节点标识『{node.key}』不合法",
            "使用小写字母开头的 slug，如 explore",
        )
    kind = node.kind
    if readonly_only and kind in WRITABLE_KINDS:
        _error(
            res,
            "readonly_mode",
            path,
            f"计划模式下不能使用可写节点『{node.key}』",
            "改为 explore / review，或退出计划模式后再启动",
        )
    role = node.role or DEFAULT_ROLE.get(kind)
    if kind != "gate" and role is not None and not role_exists(role):
        _error(
            res,
            "unknown_role",
            f"{path}.role",
            f"角色『{role}』不存在或已停用",
            "使用启用的子代理类型名称，或内置角色 explorer / implementer / reviewer",
        )
    for inp in node.inputs:
        if inp not in index:
            _error(res, "unknown_input", f"{path}.inputs", f"输入节点『{inp}』不在同一子工作流内")
        elif index[inp] >= ni:
            _error(res, "forward_input", f"{path}.inputs", f"输入节点『{inp}』必须先于当前节点声明")
    if kind in READONLY_KINDS and node.write_scope:
        _error(
            res,
            "readonly_with_scope",
            f"{path}.write_scope",
            f"只读节点『{node.key}』不能声明写入范围",
            "删除 write_scope，或改为 execute",
        )
    if kind in WRITABLE_KINDS and not node.write_scope:
        _error(
            res,
            "missing_write_scope",
            f"{path}.write_scope",
            f"可写节点『{node.key}』必须声明写入范围",
            "例如 server/app/services/points*.py",
        )
    for scope in node.write_scope:
        if normalize_scope(scope) is None:
            _error(
                res,
                "bad_scope",
                f"{path}.write_scope",
                f"写入范围『{scope}』必须是工作区内的相对路径",
            )
    if kind in ("execute", "review") and not node.acceptance:
        _error(
            res,
            "missing_acceptance",
            f"{path}.acceptance",
            f"节点『{node.key}』缺少验收标准",
            "逐条写出可验证的标准",
        )
    if kind == "check" and not node.commands:
        _error(res, "missing_commands", f"{path}.commands", f"检查节点『{node.key}』必须给出命令")
    for cmd in node.commands:
        note = command_note(cmd)
        if note is not None:
            _warning(res, note[0], f"{path}.commands", note[1])
    if kind == "gate" and not node.question:
        _error(
            res,
            "missing_question",
            f"{path}.question",
            f"闸门节点『{node.key}』必须给出待决定的问题",
        )
    if node.rework_to is not None:
        target = index.get(node.rework_to)
        if kind != "review":
            _error(
                res,
                "rework_not_review",
                f"{path}.rework_to",
                "只有评估（review）节点可以声明返工目标",
            )
        elif target is None or target >= ni or nodes[target].kind != "execute":
            _error(
                res,
                "bad_rework_target",
                f"{path}.rework_to",
                f"返工目标『{node.rework_to}』必须是同一子工作流内、先于评估节点的 execute 节点",
                "指向前面的实现节点",
            )
    elif node.max_rework is not None:
        _warning(
            res, "max_rework_ignored", f"{path}.max_rework", "未声明 rework_to，max_rework 不生效"
        )


def effective_isolation(flow: FlowSpec) -> str:
    """子工作流实际采用的隔离方式：计划里声明的优先，否则取设置中的默认值。"""
    return flow.isolation or settings.workflow_isolation_default


def _check_concurrent_conflicts(
    res: ValidationResult, spec: PlanSpec, hot_patterns: Iterable[str]
) -> int:
    """计划期预检（§5A.1）：没有依赖路径的子工作流若写入范围重叠，则报错并给出修复建议。

    返回并发对数（用于统计）。
    """
    flows = spec.flows
    keys = [f.key for f in flows]
    closure = _dependency_closure({f.key: list(f.depends_on) for f in flows})
    isolated = [effective_isolation(f) == "worktree" for f in flows]
    hot = [h for h in (normalize_scope(p) for p in hot_patterns) if h]
    scopes: list[list[str]] = []
    for flow in flows:
        items: list[str] = []
        for node in flow.nodes:
            if node.kind in WRITABLE_KINDS:
                items.extend(n for n in (normalize_scope(s) for s in node.write_scope) if n)
        scopes.append(sorted(set(items)))

    pairs = 0
    reported: set[tuple[str, str]] = set()
    for i in range(len(flows)):
        for j in range(i + 1, len(flows)):
            if keys[j] in closure[keys[i]] or keys[i] in closure[keys[j]]:
                continue  # 存在依赖路径：串行执行，不构成冲突
            if isolated[i] or isolated[j]:
                continue  # 独立工作树中执行，改动在并入时由合并步骤处理
            pairs += 1
            a_title = flows[i].title or keys[i]
            b_title = flows[j].title or keys[j]
            for sa in scopes[i]:
                for sb in scopes[j]:
                    if not scopes_overlap(sa, sb) or (sa, sb) in reported:
                        continue
                    reported.add((sa, sb))
                    hot_hit = any(scopes_overlap(sa, h) or scopes_overlap(sb, h) for h in hot)
                    code = "hot_file_overlap" if hot_hit else "write_scope_overlap"
                    suggestion = (
                        "热点文件请归入最后的集成子工作流（让它排在其它子工作流之后）"
                        if hot_hit
                        else "为其中一方添加前置依赖以串行执行，或拆分写入范围"
                    )
                    _error(
                        res,
                        code,
                        f"flows[{i}] ↔ flows[{j}]",
                        f"并行的子工作流『{a_title}』与『{b_title}』都可能写入"
                        f" `{sa}` 与 `{sb}`（范围重叠）",
                        suggestion,
                    )
    return pairs


def _dependency_closure(graph: Mapping[str, Sequence[str]]) -> dict[str, set[str]]:
    """每个子工作流直接或间接依赖的全部子工作流。"""
    out: dict[str, set[str]] = {}
    for start in graph:
        seen: set[str] = set()
        stack = list(graph.get(start, ()))
        while stack:
            cur = stack.pop()
            if cur in seen or cur not in graph:
                continue
            seen.add(cur)
            stack.extend(graph[cur])
        out[start] = seen
    return out


def _find_cycle(graph: Mapping[str, Sequence[str]]) -> list[str] | None:
    state: dict[str, int] = {}  # 1 = 访问中，2 = 已完成
    path: list[str] = []

    def visit(node: str) -> list[str] | None:
        state[node] = 1
        path.append(node)
        for nxt in graph.get(node, ()):
            if nxt not in graph:
                continue
            if state.get(nxt) == 1:
                return path[path.index(nxt) :] + [nxt]
            if nxt not in state:
                found = visit(nxt)
                if found:
                    return found
        state[node] = 2
        path.pop()
        return None

    for key in graph:
        if key not in state:
            found = visit(key)
            if found:
                return found
    return None


def _stats(
    spec: PlanSpec, total_nodes: int, pairs: int, integration_flows: int = 0
) -> dict[str, int]:
    nodes = [n for flow in spec.flows for n in flow.nodes]
    llm = sum(1 for n in nodes if n.kind in LLM_KINDS)
    checks = sum(1 for n in nodes if n.kind == "check")
    rework_budget = sum(
        DEFAULT_MAX_REWORK if n.max_rework is None else n.max_rework
        for n in nodes
        if n.kind == "review" and n.rework_to
    )
    return {
        "flows": len(spec.flows),
        # 其中由 after 自动展开的集成验收数量：界面与工具文案据此区分“你提交的”与“自动生成的”
        "integration_flows": integration_flows,
        "nodes": total_nodes,
        "llm_nodes": llm,
        "check_nodes": checks,
        "concurrent_pairs": pairs,
        "estimated_agent_calls": llm + rework_budget,
    }


def _normalize(spec: PlanSpec) -> PlanSpec:
    """补全默认值（标题、角色、规范化写入范围）。只在校验通过后调用。"""
    flows: list[FlowSpec] = []
    for flow in spec.flows:
        nodes = [
            node.model_copy(
                update={
                    "title": node.title or node.key,
                    "role": None
                    if node.kind == "gate"
                    else (node.role or DEFAULT_ROLE.get(node.kind)),
                    "write_scope": [normalize_scope(s) or s for s in node.write_scope],
                }
            )
            for node in flow.nodes
        ]
        flows.append(flow.model_copy(update={"title": flow.title or flow.key, "nodes": nodes}))
    return spec.model_copy(update={"flows": flows})
