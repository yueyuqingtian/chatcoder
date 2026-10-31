# -*- coding: utf-8 -*-
"""ask_user_question 提问数量上限与超限告知（plan-598-2517）。

背景（用户实测 bug）：模型一次提交 5 个问题，`run()` 里 `questions[:_MAX_QUESTIONS]`
在 `_MAX_QUESTIONS = 4` 下静默切片——用户只看到问题 1~4，第 5 个从未展示；而模型
以为问了 5 个，后续还会基于「用户已回答第 5 题」继续推理（实测原话：
「清理/还原那项没作答」、而那条问题根本没出现过）。

本文件锁死三条不可回退的约定：
1. 上限为 8（不再因 5 题这种常见数量丢题）；
2. 超限时**必须**把「第 N 题未展示给用户」写回 tool result，让模型知情；
3. schema 的 `maxItems` 与实际上限同源（模型事先就知道硬上限）。
"""
import asyncio

import pytest

from app.orchestration.tools.ask_user import (
    _MAX_QUESTIONS,
    AskUserQuestionTool,
)


def _questions(n: int) -> list[dict]:
    return [{"question": f"问题{i}", "options": [f"选项{i}A", f"选项{i}B"]} for i in range(1, n + 1)]


class _Ctx:
    """最小 ToolContext 桩：只需 run() 读取的几个属性。"""

    session_id = 1
    agent_name = "主代理"
    is_subagent = False
    is_subsession = False


def _run_with_stubbed_approval(monkeypatch, args: dict, answer: dict):
    """跑 run()，把审批通道替换成直接回填答案的桩，并返回 (result, 实际下发的 questions)。"""
    from app.orchestration.tools import ask_user as mod

    captured: dict = {}

    def _new_id() -> str:
        return "approval-test"

    async def _request(*, approval_id, detail):
        # 记录真正下发到前端的问题列表（即用户能看到的）
        captured["questions"] = list(detail.get("questions") or [])
        detail["answer"] = answer
        return True

    monkeypatch.setattr(mod.approval_manager, "new_id", _new_id)
    monkeypatch.setattr(mod.approval_manager, "request", _request)

    tool = AskUserQuestionTool()
    result = asyncio.run(tool.run(args, _Ctx()))
    return result, captured.get("questions", [])


def test_max_questions_is_eight():
    """上限为 8：覆盖「一次问 5~8 个」的常见场景，不再丢题。"""
    assert _MAX_QUESTIONS == 8


def test_five_questions_are_all_delivered(monkeypatch):
    """回归本 bug：5 个问题必须全部展示给用户（旧实现会丢第 5 个）。"""
    result, delivered = _run_with_stubbed_approval(
        monkeypatch, {"questions": _questions(5)}, {"0": "A", "1": "B", "2": "A", "3": "B", "4": "A"},
    )
    assert result.ok is True
    assert len(delivered) == 5, f"5 个问题必须全部下发，实际 {len(delivered)} 个"
    # 未超限时不出现「已忽略」告知
    assert "未被展示给用户" not in result.output
    assert result.data.get("dropped_questions") == 0


def test_over_limit_drops_extra_and_tells_the_model(monkeypatch):
    """超过上限：只下发前 8 个，且必须明确告知模型后几个问题未展示。"""
    result, delivered = _run_with_stubbed_approval(
        monkeypatch, {"questions": _questions(10)}, {str(i): "A" for i in range(8)},
    )
    assert result.ok is True
    assert len(delivered) == _MAX_QUESTIONS
    # 用户能看到的是 1~8，与告知文案口径一致
    assert delivered[0]["question"] == "问题1"
    assert delivered[-1]["question"] == f"问题{_MAX_QUESTIONS}"
    # 关键：必须告知，且说清区间，避免模型误以为问过
    assert "未被展示给用户" in result.output
    assert f"第 {_MAX_QUESTIONS + 1}~10 个问题" in result.output
    assert result.data.get("dropped_questions") == 2


def test_schema_max_items_matches_limit():
    """schema 的 maxItems 与真实上限同源——模型事先知道硬上限，不会盲发 20 个。"""
    schema = AskUserQuestionTool().function_schema()
    q = schema["function"]["parameters"]["properties"]["questions"]
    assert q.get("maxItems") == _MAX_QUESTIONS
    assert f"1-{_MAX_QUESTIONS}" in q["description"]


def test_description_mentions_limit():
    """工具描述里的数量也要与上限一致（提示词与实现不能两套口径）。"""
    assert f"最多 {_MAX_QUESTIONS} 个" in AskUserQuestionTool.description


def test_empty_questions_still_rejected(monkeypatch):
    """空列表维持原有拒绝行为，不受本次改动影响。"""
    tool = AskUserQuestionTool()
    result = asyncio.run(tool.run({"questions": []}, _Ctx()))
    assert result.ok is False
    assert "不能为空" in (result.error or "")


@pytest.mark.parametrize("count", [1, 4, 8])
def test_within_limit_never_warns(monkeypatch, count):
    """未超限的任意数量都不应出现「已忽略」提示。"""
    result, delivered = _run_with_stubbed_approval(
        monkeypatch, {"questions": _questions(count)}, {str(i): "A" for i in range(count)},
    )
    assert result.ok is True
    assert len(delivered) == count
    assert "未被展示给用户" not in result.output
