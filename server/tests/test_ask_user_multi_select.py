# -*- coding: utf-8 -*-
"""ask_user_question 多选题型（multiple）与「无选项」提问的兼容约定。

用户实测两条 bug：
1. ta3 会话里模型按官方 ask_user schema 传 `multiple: true` 问多选题，前端只渲染成单选
   （询问卡片没有题型标注，也只能选一个）——`_restore_ask_user` 当时把 multiple 丢了；
2. 模型只提问不给 options（官方 schema 里 options 可选）时，前端选项区整块不渲染，
   连自定义输入行也没有，卡片只剩「忽略」可用。

本文件锁死：
- multiple / multiSelect 等各种写法都归一成布尔 multiple，且只在模型确实传了时才写；
- 选项保持可选（缺省不伪造空数组）；
- ta3 伪装层入站保留 multiple、出站历史回放也保留（与官方 schema 同形）；
- 真实工具 schema 暴露 multiple，让非 ta3 供应商也能问多选题。
"""
import asyncio

from app.models.providers.ta3_tool_aliases import disguise_args, restore_args
from app.orchestration.tools.ask_user import AskUserQuestionTool, _normalize_questions


class _Ctx:
    session_id = 1
    agent_name = "主代理"
    is_subagent = False
    is_subsession = False


def test_normalize_keeps_multiple_as_bool():
    out = _normalize_questions([{"question": "q", "options": ["A", "B"], "multiple": True}])
    assert out[0]["multiple"] is True
    # 没传就不写：前端据此按单选渲染
    assert "multiple" not in _normalize_questions([{"question": "q"}])[0]


def test_normalize_accepts_alias_keys():
    """模型常照搬 Claude 的 multiSelect / 官方驼峰 allowCustom。"""
    out = _normalize_questions([{
        "question": "q", "options": ["A"], "multiSelect": "true", "allowCustom": False,
    }])
    assert out[0]["multiple"] is True
    assert out[0]["allow_custom"] is False
    assert "multiSelect" not in out[0]
    assert "allowCustom" not in out[0]


def test_normalize_options_are_optional():
    """只提问不给选项（官方 schema 允许）不能报错、也不能被伪造成空数组。"""
    out = _normalize_questions([{"question": "q"}, {"question": "q2", "options": []}])
    assert out[0] == {"question": "q"}
    assert out[1]["options"] == []


def test_schema_exposes_multiple():
    props = AskUserQuestionTool().function_schema()["function"]["parameters"]["properties"]
    item_props = props["questions"]["items"]["properties"]
    assert "multiple" in item_props
    assert props["questions"]["items"].get("required") == ["question"]


def test_run_delivers_multiple_to_the_card(monkeypatch):
    """下发给前端的问题必须带上 multiple（否则多选被渲染成单选）。"""
    from app.orchestration.tools import ask_user as mod

    captured: dict = {}

    async def _request(*, approval_id, detail):
        captured["questions"] = list(detail.get("questions") or [])
        detail["answer"] = {"0": ["A", "B"]}
        return True

    monkeypatch.setattr(mod.approval_manager, "new_id", lambda: "approval-test")
    monkeypatch.setattr(mod.approval_manager, "request", _request)

    result = asyncio.run(AskUserQuestionTool().run(
        {"questions": [{"question": "选哪些？", "options": ["A", "B"], "multiple": True}]}, _Ctx(),
    ))
    assert result.ok is True
    assert captured["questions"][0]["multiple"] is True
    # 多选答案是数组，tool result 原样回给模型
    assert result.data["answer"] == {"0": ["A", "B"]}
    assert '"A"' in result.output and '"B"' in result.output


def test_ta3_restore_keeps_multiple():
    out = restore_args("ask_user", {"questions": [{"question": "q", "options": [{"label": "A"}], "multiple": True}]})
    assert out["questions"][0]["multiple"] is True


def test_ta3_history_replay_keeps_multiple():
    """历史回放的调用参数要与当轮下发的官方 schema 一致（multiple 是官方参数）。"""
    out = disguise_args("ask_user_question", {"questions": [{"question": "q", "multiple": True}]})
    assert out["questions"][0]["multiple"] is True

def test_normalize_accepts_option_aliases():
    """模型偶尔把选项写进 choices / answers —— 不认识就会让卡片整块没有可点选项。"""
    out = _normalize_questions([
        {"question": "q1", "choices": ["A", "B"]},
        {"question": "q2", "answers": [{"label": "X"}]},
    ])
    assert out[0] == {"question": "q1", "options": ["A", "B"]}
    assert out[1]["options"] == ["X"]
    # 别名键归一后不留残影
    for item in out:
        assert "choices" not in item and "answers" not in item


def test_ta3_restore_accepts_option_aliases():
    """ta3 入站同样要认出别名键，否则前端收不到 options。"""
    out = restore_args("ask_user", {"questions": [{"question": "q", "choices": [{"label": "A"}]}]})
    assert out["questions"][0]["options"] == ["A"]
