"""智能组队服务逻辑测试（纯逻辑验证）。

验证团队配置文件、角色推荐、Leader 设置等业务逻辑。
DB 集成测试留待后续引入 testcontainers/PG 时补充。
"""
from app.services.smart_team_service import EXTRA_TEMPLATES, _PROFILES


def test_profiles_contains_expected_types():
    assert "web_app" in _PROFILES
    assert "backend_service" in _PROFILES
    assert "frontend_only" in _PROFILES
    assert "fullstack_minimal" in _PROFILES
    assert "data_ai" in _PROFILES


def test_profile_structure():
    for key, profile in _PROFILES.items():
        assert "name" in profile
        assert "description" in profile
        assert "roles" in profile
        assert "leader_role" in profile
        assert "workflow" in profile
        assert isinstance(profile["roles"], list)
        assert len(profile["roles"]) > 0
        assert profile["leader_role"] in profile["roles"]
        assert isinstance(profile["workflow"], list)
        assert len(profile["workflow"]) > 0


def test_web_app_profile_has_full_team():
    profile = _PROFILES["web_app"]
    roles = set(profile["roles"])
    assert "leader" in roles
    assert "ui_designer" in roles
    assert "frontend" in roles
    assert "backend" in roles
    assert "qa" in roles
    assert "reviewer" in roles
    assert profile["leader_role"] == "leader"


def test_backend_profile_no_frontend():
    profile = _PROFILES["backend_service"]
    roles = set(profile["roles"])
    assert "leader" in roles
    assert "architect" in roles
    assert "backend" in roles
    assert "qa" in roles
    assert "reviewer" in roles
    assert "frontend" not in roles
    assert "ui_designer" not in roles


def test_minimal_profile_smallest_team():
    profile = _PROFILES["fullstack_minimal"]
    roles = profile["roles"]
    assert len(roles) <= 4
    assert "leader" in roles
    assert "fullstack" in roles
    assert "reviewer" in roles


def test_extra_templates_have_expected_roles():
    roles = {t["role"] for t in EXTRA_TEMPLATES}
    assert "ui_designer" in roles
    assert "fullstack" in roles
    assert "data_scientist" in roles
    assert "devops" in roles


def test_extra_templates_have_required_fields():
    for tpl in EXTRA_TEMPLATES:
        assert "key" in tpl
        assert "name" in tpl
        assert "role" in tpl
        assert "system_prompt" in tpl
        assert "tool_whitelist" in tpl
        assert "default_model_level" in tpl
        assert isinstance(tpl["tool_whitelist"], list)
        assert len(tpl["system_prompt"]) > 10


def test_ui_designer_has_design_tools():
    tpl = next(t for t in EXTRA_TEMPLATES if t["role"] == "ui_designer")
    assert "fs_read" in tpl["tool_whitelist"]
    assert "fs_write" in tpl["tool_whitelist"]
    assert "web_fetch" in tpl["tool_whitelist"]


def test_devops_has_ci_tool():
    tpl = next(t for t in EXTRA_TEMPLATES if t["role"] == "devops")
    assert "ci_run" in tpl["tool_whitelist"]
    assert "terminal_exec" in tpl["tool_whitelist"]


def test_data_scientist_has_exec_tools():
    tpl = next(t for t in EXTRA_TEMPLATES if t["role"] == "data_scientist")
    assert "fs_read" in tpl["tool_whitelist"]
    assert "fs_write" in tpl["tool_whitelist"]
    assert "terminal_exec" in tpl["tool_whitelist"]


def test_fullstack_has_broad_tool_access():
    tpl = next(t for t in EXTRA_TEMPLATES if t["role"] == "fullstack")
    tools = set(tpl["tool_whitelist"])
    assert "fs_read" in tools
    assert "fs_write" in tools
    assert "editor_apply_diff" in tools
    assert "terminal_exec" in tools
    assert "web_fetch" in tools


def test_workflow_steps_match_profile_complexity():
    """验证不同项目类型的工作流步骤数量符合预期。"""
    web_steps = len(_PROFILES["web_app"]["workflow"])
    minimal_steps = len(_PROFILES["fullstack_minimal"]["workflow"])
    assert web_steps >= minimal_steps
    assert web_steps >= 5
    assert minimal_steps >= 3


def test_profile_descriptions_are_unique():
    descriptions = [p["description"] for p in _PROFILES.values()]
    assert len(set(descriptions)) == len(descriptions)


def test_profile_names_are_unique():
    names = [p["name"] for p in _PROFILES.values()]
    assert len(set(names)) == len(names)
