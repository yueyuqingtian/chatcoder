"""内置预设骨架与用户模板（设计 §4.5 第 1、8 点）。

预设只是起点：它们是完整、可通过校验的图规格，主代理通过 plan_workflow 的 based_on 引用，
本次提交的 flows 按 key 覆盖骨架中的同名子工作流，未提交的子工作流保留。
写入范围（write_scope）在预设中是占位，主代理应按任务收窄；确认计划时用户可以看到实际范围。
"""

from __future__ import annotations

import copy
from typing import Any

PRESET_PREFIX = ""
USER_PREFIX = "user:"

_PRESETS: dict[str, dict[str, Any]] = {
    "explore_build_review": {
        "name": "探索 · 实现 · 评估",
        "description": "先探索代码与约束，再实现，最后评估；未通过则交回实现返工（最多 2 轮）。",
        "plan": {
            "title": "探索 · 实现 · 评估",
            "goal": "完成一项需要先调研再实现、且需要质量把关的改动",
            "rationale": "探索结论先行，实现按结论执行，评估独立把关；评估未通过时回到实现返工。",
            "flows": [
                {
                    "key": "main",
                    "title": "主流程",
                    "nodes": [
                        {
                            "key": "explore",
                            "kind": "explore",
                            "task": (
                                "探索与任务相关的代码、数据与约束，给出可执行的实施计划与影响文件。"
                            ),
                        },
                        {
                            "key": "implement",
                            "kind": "execute",
                            "inputs": ["explore"],
                            "task": "严格按探索结论完成实现，只修改写入范围内的文件。",
                            "write_scope": ["src/**"],
                            "acceptance": ["实现覆盖任务的全部要求"],
                        },
                        {
                            "key": "review",
                            "kind": "review",
                            "inputs": ["implement"],
                            "rework_to": "implement",
                            "max_rework": 2,
                            "task": "独立评估实现是否满足要求，逐条给出结论与证据。",
                            "acceptance": ["满足任务目标", "没有明显回归"],
                        },
                    ],
                }
            ],
        },
    },
    "parallel_research": {
        "name": "并行调研 · 汇总",
        "description": "三路只读探索并行调研，最后由一个只读评估汇总结论，不占用写入范围。",
        "plan": {
            "title": "并行调研 · 汇总",
            "goal": "就一个问题并行调研多个方向，并给出汇总结论",
            "rationale": "调研彼此独立，可并行；汇总只读，不与写入类工作流争用文件。",
            "flows": [
                {
                    "key": "research_a",
                    "title": "方向 A",
                    "nodes": [
                        {
                            "key": "explore",
                            "kind": "explore",
                            "task": "调研方向 A，列出事实、依据与风险。",
                        },
                    ],
                },
                {
                    "key": "research_b",
                    "title": "方向 B",
                    "nodes": [
                        {
                            "key": "explore",
                            "kind": "explore",
                            "task": "调研方向 B，列出事实、依据与风险。",
                        },
                    ],
                },
                {
                    "key": "research_c",
                    "title": "方向 C",
                    "nodes": [
                        {
                            "key": "explore",
                            "kind": "explore",
                            "task": "调研方向 C，列出事实、依据与风险。",
                        },
                    ],
                },
                {
                    "key": "synthesis",
                    "title": "汇总",
                    "depends_on": ["research_a", "research_b", "research_c"],
                    "nodes": [
                        {
                            "key": "summarize",
                            "kind": "review",
                            "task": "汇总三路调研结论，比较取舍并给出推荐方案。",
                            "acceptance": ["结论有依据", "明确推荐方案与理由"],
                        },
                    ],
                },
            ],
        },
    },
    "fix_and_verify": {
        "name": "缺陷修复 · 验证",
        "description": "先复现与定位，再修复，最后用检查命令验证；适合有明确失败用例的缺陷。",
        "plan": {
            "title": "缺陷修复 · 验证",
            "goal": "修复一个可复现的缺陷，并用检查确认已修复",
            "rationale": "先定位根因再动手；修复后以确定性检查为准，不依赖主观判断。",
            "flows": [
                {
                    "key": "main",
                    "title": "修复",
                    "nodes": [
                        {
                            "key": "reproduce",
                            "kind": "explore",
                            "task": "复现缺陷，定位根因与受影响的文件。",
                        },
                        {
                            "key": "fix",
                            "kind": "execute",
                            "inputs": ["reproduce"],
                            "task": "修复根因，避免只改表象；保持无关行为不变。",
                            "write_scope": ["src/**"],
                            "acceptance": ["复现步骤不再出现问题"],
                        },
                        {
                            "key": "verify",
                            "kind": "check",
                            "inputs": ["fix"],
                            "commands": ["pytest -q"],
                        },
                    ],
                }
            ],
        },
    },
    # plan-156-619 S9：两个「最小骨架」——它们刻意只有 2~3 个节点、没有 review，
    # 用来告诉使用者「工作流可以是这么小的东西」，不需要凑齐「探索→实现→评估」三段。
    "parallel_only": {
        "name": "纯并行 · 各自收口",
        "description": "两个互不依赖的子工作流并行跑完即结束，不设汇总、不设评估；适合两处独立改动。",
        "plan": {
            "title": "纯并行 · 各自收口",
            "goal": "把两处互不依赖的改动并行推进，各自完成后直接结束",
            "rationale": "两处改动无交集、无依赖，并行是最短路径；额外加汇总节点只会拖长时间。",
            "flows": [
                {
                    "key": "part_a",
                    "title": "A 部分",
                    "nodes": [
                        {
                            "key": "do_a",
                            "kind": "execute",
                            "task": "完成 A 部分改动，只修改本流写入范围内的文件。",
                            "write_scope": ["src/a/**"],
                        },
                    ],
                },
                {
                    "key": "part_b",
                    "title": "B 部分",
                    "nodes": [
                        {
                            "key": "do_b",
                            "kind": "execute",
                            "task": "完成 B 部分改动，只修改本流写入范围内的文件。",
                            "write_scope": ["src/b/**"],
                        },
                    ],
                },
            ],
        },
    },
    "serial_chain": {
        "name": "串行链 · 一步接一步",
        "description": "三步严格串行（改 A → 改 B → 跑检查），每步依赖前一步的产出；最简的线性图。",
        "plan": {
            "title": "串行链 · 一步接一步",
            "goal": "按依赖顺序推进多个步骤，最后用检查命令收口",
            "rationale": "后续步骤需要前一步的中间产物，只能串行；用 check 节点客观验证，不额外加评估节点。",
            "flows": [
                {
                    "key": "chain",
                    "title": "串行链",
                    "nodes": [
                        {
                            "key": "step_a",
                            "kind": "execute",
                            "task": "第一步：完成基础改动，为后续步骤提供接口或数据结构。",
                            "write_scope": ["src/**"],
                        },
                        {
                            "key": "step_b",
                            "kind": "execute",
                            "inputs": ["step_a"],
                            "task": "第二步：基于第一步的产出继续实现，不重复第一步的工作。",
                            "write_scope": ["src/**"],
                        },
                        {
                            "key": "verify",
                            "kind": "check",
                            "inputs": ["step_b"],
                            "commands": ["pytest -q"],
                        },
                    ],
                },
            ],
        },
    },
}


def list_presets() -> list[dict[str, Any]]:
    return [
        {
            "key": key,
            "name": item["name"],
            "description": item["description"],
            "source": "preset",
            "enabled": True,
            "plan": copy.deepcopy(item["plan"]),
        }
        for key, item in _PRESETS.items()
    ]


def preset_plan(key: str) -> dict[str, Any] | None:
    item = _PRESETS.get(key)
    return copy.deepcopy(item["plan"]) if item is not None else None


def is_user_ref(ref: str) -> bool:
    return ref.startswith(USER_PREFIX)


def to_template_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """把已规范化的计划转为可保存的模板：末尾的集成验收子工作流还原为 after，避免再次展开时重复。"""
    flows = list(plan.get("flows") or [])
    result = copy.deepcopy(plan)
    if flows and flows[-1].get("title") == "集成验收" and len(flows[-1].get("nodes") or []) == 1:
        verify = flows[-1]["nodes"][0]
        if verify.get("key") == "verify" and verify.get("kind") == "review":
            result["after"] = {
                "acceptance": list(verify.get("acceptance") or []),
                "commands": list(verify.get("commands") or []),
            }
            result["flows"] = flows[:-1]
    result["after"] = result.get("after")
    return result


def merge_into_base(
    base: dict[str, Any],
    *,
    title: str,
    goal: str,
    rationale: str,
    flows: list[dict[str, Any]],
    after: dict[str, Any] | None,
) -> dict[str, Any]:
    """以骨架为底：本次提交的同 key 子工作流覆盖骨架。

    其它骨架子工作流保留，未给出的标题与目标沿用骨架。
    """
    by_key: dict[str, dict[str, Any]] = {f["key"]: copy.deepcopy(f) for f in base.get("flows", [])}
    for flow in flows:
        by_key[flow["key"]] = flow
    merged: dict[str, Any] = {
        "title": title or base.get("title", ""),
        "goal": goal or base.get("goal", ""),
        "rationale": rationale or base.get("rationale", ""),
        "flows": list(by_key.values()),
    }
    if after:
        merged["after"] = after
    elif base.get("after"):
        merged["after"] = copy.deepcopy(base["after"])
    return merged


def blank_plan(name: str) -> dict[str, Any]:
    """“新建模板”的起点：一个子工作流、一个探索步骤，可在模板编辑器中继续增删。"""
    return {
        "title": name,
        # 目标必填（PlanSpec.goal）；编辑器没有目标输入框，沿用模板名，否则无法保存
        "goal": name,
        "rationale": "",
        "flows": [
            {
                "key": "main",
                "title": "子工作流 1",
                "depends_on": [],
                "nodes": [
                    {
                        "key": "explore",
                        "title": "探索",
                        "kind": "explore",
                        "task": "探索需要改动的代码与约束",
                    }
                ],
            }
        ],
    }
