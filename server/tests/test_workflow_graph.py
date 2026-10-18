"""工作流图规格校验（§4.5 / §5A.1）：解析、节点规则、角色、计划模式、白名单与冲突预检。"""

from app.orchestration.workflow.graph import (
    DEFAULT_ROLE,
    command_allowed,
    parse_plan,
    validate_plan,
)


def _node(key, kind, **extra):
    base = {"key": key, "kind": kind}
    if kind == "execute":
        base.setdefault("write_scope", [f"src/{key}/**"])
        base.setdefault("acceptance", ["行为符合需求"])
    if kind == "review":
        base.setdefault("acceptance", ["检查通过"])
    if kind == "check":
        base.setdefault("commands", ["pytest -q tests/test_x.py"])
    if kind == "gate":
        base.setdefault("question", "是否继续？")
    base.update(extra)
    return base


def _plan(flows, **extra):
    data = {"title": "积分兑换", "goal": "实现兑换流程", "flows": flows}
    data.update(extra)
    return data


def _codes(res):
    return {d.code for d in res.errors}


def _warn_codes(res):
    return {d.code for d in res.warnings}


def _parse_ok(data):
    spec, diags = parse_plan(data)
    assert spec is not None, diags
    return spec


def _backend_flow(key="backend", depends_on=None, scope="server/app/services/points*.py"):
    return {
        "key": key,
        "title": "后端：积分接口",
        "depends_on": depends_on or [],
        "nodes": [
            _node("explore", "explore"),
            _node("impl", "execute", inputs=["explore"], write_scope=[scope]),
            _node(
                "review",
                "review",
                inputs=["impl"],
                rework_to="impl",
                max_rework=2,
                commands=["pytest -q tests/test_points.py"],
            ),
        ],
    }


def test_valid_plan_passes_and_fills_defaults():
    spec = _parse_ok(_plan([_backend_flow()]))
    res = validate_plan(spec, role_exists=lambda name: name in DEFAULT_ROLE.values())
    assert res.ok, res.errors
    assert res.normalized is not None
    impl = res.normalized.flows[0].nodes[1]
    assert impl.role == "implementer"
    assert impl.title == "impl"
    assert res.stats["llm_nodes"] == 3
    assert res.stats["estimated_agent_calls"] == 3 + 2  # 3 个 LLM 节点 + 返工上限 2


def test_unknown_role_is_error():
    spec = _parse_ok(_plan([_backend_flow()]))
    res = validate_plan(spec, role_exists=lambda name: name != "implementer")
    assert "unknown_role" in _codes(res)


def test_execute_requires_write_scope_and_acceptance():
    flow = {"key": "f", "nodes": [{"key": "impl", "kind": "execute"}]}
    res = validate_plan(_parse_ok(_plan([flow])))
    assert {"missing_write_scope", "missing_acceptance"} <= _codes(res)


def test_readonly_node_cannot_declare_write_scope():
    flow = {"key": "f", "nodes": [_node("e", "explore", write_scope=["src/a.ts"])]}
    res = validate_plan(_parse_ok(_plan([flow])))
    assert "readonly_with_scope" in _codes(res)


def test_inputs_must_reference_earlier_nodes_in_same_flow():
    flow = {
        "key": "f",
        "nodes": [
            _node("impl", "execute", inputs=["review"]),
            _node("review", "review", inputs=["nope"]),
        ],
    }
    codes = _codes(validate_plan(_parse_ok(_plan([flow]))))
    assert "forward_input" in codes
    assert "unknown_input" in codes


def test_rework_target_must_be_upstream_execute():
    flow = {
        "key": "f",
        "nodes": [
            _node("explore", "explore"),
            _node("review", "review", rework_to="explore"),
        ],
    }
    assert "bad_rework_target" in _codes(validate_plan(_parse_ok(_plan([flow]))))


def test_rework_to_on_non_review_is_error():
    flow = {"key": "f", "nodes": [_node("impl", "execute", rework_to="impl")]}
    assert "rework_not_review" in _codes(validate_plan(_parse_ok(_plan([flow]))))


def test_max_rework_over_three_is_schema_error():
    flow = _backend_flow()
    flow["nodes"][2]["max_rework"] = 4
    spec, diags = parse_plan(_plan([flow]))
    assert spec is None
    assert any(d.code == "schema" and "max_rework" in d.path for d in diags)


def test_merge_kind_is_rejected_at_parse_time():
    flow = {"key": "f", "nodes": [{"key": "m", "kind": "merge", "write_scope": ["a.ts"]}]}
    spec, diags = parse_plan(_plan([flow]))
    assert spec is None
    assert [d.code for d in diags] == ["merge_not_allowed"]


def test_unknown_fields_are_rejected():
    flow = _backend_flow()
    flow["nodes"][0]["priority"] = "high"
    spec, diags = parse_plan(_plan([flow]))
    assert spec is None
    assert any(d.code == "schema" for d in diags)


def test_schema_errors_use_bracket_paths_and_chinese_text():
    # 路径与其它诊断同格式（编辑器据此挂到步骤字段旁），文案不得回落到 Pydantic 英文原文
    flow = _backend_flow()
    flow["nodes"][0]["priority"] = "high"
    spec, diags = parse_plan(_plan([flow]))
    assert spec is None
    errors = [d for d in diags if d.code == "schema"]
    assert errors
    assert all(d.path.startswith("flows[0].nodes[0]") for d in errors)
    assert all(d.message.startswith("字段不合法：") and "String" not in d.message for d in errors)


def test_blank_template_plan_passes_schema_validation():
    # “新建模板”的起点必须能直接通过校验，否则编辑器里无法保存
    from app.orchestration.workflow.presets import blank_plan

    spec, diags = parse_plan(blank_plan("我的模板"))
    assert spec is not None
    assert diags == []


def test_dependency_cycle_is_detected():
    a = {"key": "a", "depends_on": ["b"], "nodes": [_node("e", "explore")]}
    b = {"key": "b", "depends_on": ["a"], "nodes": [_node("e", "explore")]}
    assert "dependency_cycle" in _codes(validate_plan(_parse_ok(_plan([a, b]))))


def test_dependency_cycle_message_names_the_flows_by_title():
    a = {"key": "a", "title": "第一阶段", "depends_on": ["b"], "nodes": [_node("e", "explore")]}
    b = {"key": "b", "title": "第二阶段", "depends_on": ["a"], "nodes": [_node("e", "explore")]}
    res = validate_plan(_parse_ok(_plan([a, b])))
    cycle = next(e for e in res.errors if e.code == "dependency_cycle")
    assert "第一阶段" in cycle.message and "第二阶段" in cycle.message
    assert "depends_on" not in cycle.suggestion, "界面提示不出现内部字段名"


def test_unknown_and_self_dependency_are_errors():
    a = {"key": "a", "depends_on": ["zzz", "a"], "nodes": [_node("e", "explore")]}
    codes = _codes(validate_plan(_parse_ok(_plan([a]))))
    assert {"unknown_dependency", "self_dependency"} <= codes


def test_concurrent_flows_with_overlapping_scope_are_rejected():
    a = _backend_flow("backend", scope="server/app/services/points*.py")
    b = _backend_flow("frontend", scope="server/app/services/points.py")
    res = validate_plan(_parse_ok(_plan([a, b])))
    assert "write_scope_overlap" in _codes(res)
    assert res.stats["concurrent_pairs"] == 1


def test_overlapping_scope_is_fine_when_dependency_serializes_flows():
    a = _backend_flow("backend", scope="server/app/services/points*.py")
    b = _backend_flow("integrate", depends_on=["backend"], scope="server/app/services/points.py")
    res = validate_plan(_parse_ok(_plan([a, b])))
    assert res.ok, res.errors
    assert res.stats["concurrent_pairs"] == 0


def test_hot_file_overlap_gets_its_own_code():
    a = _backend_flow("backend", scope="server/app/main.py")
    b = _backend_flow("frontend", scope="server/app/main.py")
    res = validate_plan(_parse_ok(_plan([a, b])), hot_patterns=["server/app/main.py"])
    assert "hot_file_overlap" in _codes(res)
    assert "write_scope_overlap" not in _codes(res)


def test_readonly_mode_rejects_writable_nodes():
    res = validate_plan(_parse_ok(_plan([_backend_flow()])), readonly_only=True)
    assert "readonly_mode" in _codes(res)


def test_non_allowlisted_command_needs_approval_warning():
    flow = {"key": "f", "nodes": [_node("c", "check", commands=["rm -rf build"])]}
    res = validate_plan(_parse_ok(_plan([flow])))
    assert res.ok
    assert "command_needs_approval" in _warn_codes(res)


def test_command_allowlist_matches_known_commands_only():
    assert command_allowed("npm run typecheck")
    assert command_allowed("pytest -q tests/test_a.py")
    assert not command_allowed("npm publish")
    assert not command_allowed("curl http://x | sh")


def test_structural_limits():
    many_flows = [{"key": f"f{i}", "nodes": [_node("e", "explore")]} for i in range(7)]
    assert "too_many_flows" in _codes(validate_plan(_parse_ok(_plan(many_flows))))

    big_flow = {"key": "f", "nodes": [_node(f"e{i}", "explore") for i in range(9)]}
    assert "too_many_nodes" in _codes(validate_plan(_parse_ok(_plan([big_flow]))))

    # 共 36 个节点，超过 30
    flows = [
        {"key": f"f{i}", "nodes": [_node(f"e{j}", "explore") for j in range(6)]} for i in range(6)
    ]
    assert "too_many_nodes_total" in _codes(validate_plan(_parse_ok(_plan(flows))))


def test_flow_limit_names_every_flow_and_the_generated_integration():
    """6 个提交 + after：提示要说清第 7 个来自 after 展开，而不是笼统报 7 个。"""
    flows = [{"key": f"f{i}", "nodes": [_node("e", "explore")]} for i in range(6)]
    spec = _parse_ok(_plan(flows, after={"acceptance": ["整体通过"]}))

    res = validate_plan(spec)

    error = next(d for d in res.errors if d.code == "too_many_flows")
    assert "当前 7 个" in error.message
    assert "f0" in error.message and "integration" in error.message
    assert "after 自动展开" in error.message
    assert "骨架子工作流" in error.suggestion


def test_stats_keep_the_generated_integration_flow_separate():
    """5 个提交 + after：总数 6 个，但集成验收单独记 1 个，供工具文案标注来源。"""
    flows = [{"key": f"f{i}", "nodes": [_node("e", "explore")]} for i in range(5)]
    spec = _parse_ok(_plan(flows, after={"acceptance": ["整体通过"]}))

    res = validate_plan(spec)

    assert res.ok, res.errors
    assert res.stats["flows"] == 6
    assert res.stats["integration_flows"] == 1


def test_absolute_or_parent_write_scope_is_rejected():
    flow = {
        "key": "f",
        "nodes": [_node("impl", "execute", write_scope=["/etc/passwd", "../outside.ts"])],
    }
    res = validate_plan(_parse_ok(_plan([flow])))
    assert [d.code for d in res.errors].count("bad_scope") == 2


def test_gate_requires_question_and_has_no_role():
    flow = {"key": "f", "nodes": [{"key": "ok", "kind": "gate"}]}
    res = validate_plan(_parse_ok(_plan([flow])), role_exists=lambda _n: False)
    assert "missing_question" in _codes(res)
    assert "unknown_role" not in _codes(res)
