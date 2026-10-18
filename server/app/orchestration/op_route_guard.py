"""操作路线沉淀守卫（plan-20261018-operator-boost）。

【问题】
提示词要求「完成一条新的 GUI 操作路线后沉淀」，但这一步发生在任务已经做完、模型准备收尾的时刻，
实测最容易被跳过：下次同类操作又从零摸索，又慢又容易点错。

【做法】
与「规划模式未写计划文档 → 收尾前纠偏」同一机制：agent loop 每个工具结果都交给守卫登记，
模型准备结束 turn（本步无工具调用）时问一次 `reminder()`：
- 本轮成功执行的 GUI 写操作（点击 / 输入 / 按键…）达到阈值；
- 且没有成功保存过路线（desktop_recipe / browser_recipe action=save）；
- 且不是「命中已有路线、全程没有失败」的纯复用（复用成功说明路线已经够用）；
满足时返回一条系统提醒，注入后让模型再走一步。每轮最多提醒一次，模型可以回答「无新经验」直接结束。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.orchestration.route_hints import host_of

# 会改变界面状态的操作类工具（只读的截图 / 快照 / 读取不计）
_DESKTOP_ACTIONS = frozenset({
    "desktop_click", "desktop_type", "desktop_keys", "desktop_scroll",
})
_BROWSER_ACTIONS = frozenset({
    "browser_click", "browser_type", "browser_fill", "browser_press", "browser_drag",
    "browser_hover", "browser_scroll", "browser_batch",
})
_RECIPE_TOOLS = {"desktop_recipe": "desktop", "browser_recipe": "browser"}
# 少于这么多步的操作不值得沉淀（点一下按钮、填一个框）
MIN_ACTIONS = 3


@dataclass
class OpRouteGuard:
    enabled: bool = True
    actions: dict[str, int] = field(default_factory=lambda: {"desktop": 0, "browser": 0})
    failures: dict[str, int] = field(default_factory=lambda: {"desktop": 0, "browser": 0})
    targets: dict[str, list[str]] = field(default_factory=lambda: {"desktop": [], "browser": []})
    saved: set[str] = field(default_factory=set)
    reused: set[str] = field(default_factory=set)
    reminded: bool = False

    def _add_target(self, scope: str, name: str) -> None:
        name = (name or "").strip()
        if name and name not in self.targets[scope]:
            self.targets[scope].append(name[:60])

    def observe(self, tool_name: str, args: Any, ok: bool, data: Any = None) -> None:
        """登记一次工具调用结果（agent loop 每个工具结果调用一次）。"""
        if not self.enabled:
            return
        a = args if isinstance(args, dict) else {}
        d = data if isinstance(data, dict) else {}
        if tool_name in _RECIPE_TOOLS:
            scope = _RECIPE_TOOLS[tool_name]
            action = str(a.get("action") or "").lower()
            if ok and action == "save":
                self.saved.add(scope)
            elif ok and action == "recall" and int(d.get("count") or 0) > 0:
                self.reused.add(scope)
            return
        if d.get("route_hint"):
            # 工具自动附上了已有路线：视同一次召回
            self.reused.add("browser" if tool_name.startswith("browser_") else "desktop")
        if tool_name == "browser_navigate":
            self._add_target("browser", host_of(str(d.get("url") or a.get("url") or "")))
            return
        if tool_name in ("desktop_focus", "desktop_apps"):
            self._add_target("desktop", str(a.get("proc") or a.get("title") or a.get("keyword") or ""))
            return
        scope = "desktop" if tool_name in _DESKTOP_ACTIONS else "browser" if tool_name in _BROWSER_ACTIONS else ""
        if not scope:
            return
        if ok:
            steps = len(a.get("actions") or []) if tool_name == "browser_batch" else 1
            self.actions[scope] += max(1, steps)
        else:
            self.failures[scope] += 1
        if scope == "desktop":
            self._add_target("desktop", str(a.get("title") or ""))

    def _pending_scopes(self) -> list[str]:
        out = []
        for scope in ("browser", "desktop"):
            if self.actions[scope] < MIN_ACTIONS or scope in self.saved:
                continue
            if scope in self.reused and self.failures[scope] == 0:
                continue
            out.append(scope)
        return out

    def reminder(self) -> str | None:
        """turn 准备结束时调用：需要提醒沉淀时返回提醒文本（每轮最多一次），否则 None。"""
        if not self.enabled or self.reminded:
            return None
        scopes = self._pending_scopes()
        if not scopes:
            return None
        self.reminded = True
        parts = []
        for scope in scopes:
            tool = "browser_recipe" if scope == "browser" else "desktop_recipe"
            what = "内置浏览器" if scope == "browser" else "电脑操控"
            key = "site（站点 host）" if scope == "browser" else "app_name（进程名）"
            where = "、".join(self.targets[scope][:3]) or "目标界面"
            fails = f"，其间失败 {self.failures[scope]} 次" if self.failures[scope] else ""
            parts.append(f"- {what}：在「{where}」完成了 {self.actions[scope]} 步操作{fails} → 调用 {tool} action=save，"
                         f"填 {key}、一句话 intent、通用做法（怎么定位、按什么顺序、哪些步骤可合并）与踩过的坑")
        return (
            "[系统提醒] 本轮的界面操作还没有沉淀为操作路线，下次同类操作会重新摸索：\n"
            + "\n".join(parts)
            + "\n只记做法，不记坐标 / ref。若这次操作是一次性的、没有复用价值，或与已有路线相同且没有新经验，"
            "直接给出最终回复即可。不要复述本条提醒。"
        )
