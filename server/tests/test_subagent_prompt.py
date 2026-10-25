"""子代理提示词与编排层口径的守卫测试（plan-549-2281 复盘修复）。

这里守的都是「提示词/工具面/投递通道三方口径必须一致」类的约定——它们不会报错，
只会让模型按不存在的段落、不存在的参数去行动，所以只能靠断言挡住：

1. 子代理提示词不得指向上下文里根本不存在的 `## Current mode` 段
   （子代理走 `build_main_system_prompt` 时不传 execution_mode，主提示词的模式段不生成）；
2. 计划模式的模式说明不得提 `subagent_type`——spawn_subagent 的真实参数只有
   task_title / task_description / acceptance_criteria / mode / explore / purpose；
3. `_on_agent_report` 只处理进度上报，提问走 subagent_comm 的 P0 通道（死分支已删）。
"""
from __future__ import annotations

import inspect

from app.orchestration import engine
from app.orchestration.prompts import subagent as subagent_prompt


def test_subagent_prompt_has_no_dangling_current_mode_reference():
    """`## Current mode` 在子代理上下文里不存在，提示词不能指向它。"""
    text = subagent_prompt.SUBAGENT_SYSTEM_PROMPT
    assert "## Current mode" not in text, (
        "子代理提示词指向了不存在的 `## Current mode` 段：子代理走 build_main_system_prompt 时"
        "不传 execution_mode，该段不会生成，模型会去找一段不存在的说明。"
    )


def test_subagent_prompt_explains_mode_by_tool_surface():
    """模式改由「工具面」自述——这是子代理唯一能自行判断执行模式的可靠信号。"""
    text = subagent_prompt.SUBAGENT_SYSTEM_PROMPT
    assert "readonly" in text and "agent" in text
    assert "fs_write" in text  # 用真实工具名说明只读边界


def test_built_subagent_prompt_has_no_current_mode_section():
    """真实构建结果同样不得出现该段（防止别处的拼接把它带回来）。"""
    built = subagent_prompt.build_subagent_system_prompt("标题", "验收")
    assert "## Current mode" not in built


def test_plan_mode_note_uses_real_spawn_parameter():
    """计划模式的模式说明必须写真实参数 `mode=readonly`，不能写不存在的 subagent_type。"""
    note = engine._MODE_HINTS["plan"]
    assert "subagent_type" not in note, "spawn_subagent 没有 subagent_type 参数（ta3 侧才有）"
    assert "mode=readonly" in note or 'mode="readonly"' in note


def test_on_agent_report_only_publishes_progress_kind():
    """`_on_agent_report` 只剩进度上报一条路径；提问的 P0 通道在 subagent_comm。

    只看函数**代码体**——docstring 里会解释「旧实现为什么留着那条死分支」，
    断言源码全文会被这段说明本身命中。
    """
    src = inspect.getsource(engine._on_agent_report)
    body = src.split('"""')[-1] if '"""' in src else src
    assert "SUBAGENT_QUESTION" not in body, "提问不经此处，映射留在这是迁移死分支"
    assert "SUBAGENT_REPORT" in body
