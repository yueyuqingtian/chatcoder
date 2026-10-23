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
    "desktop_click", "desktop_type", "desktop_keys", "desktop_scroll", "desktop_batch",
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
    # plan-537-2251：中途提醒过的 scope（与收尾兜底的 reminded 分开计数，每个 scope 只中途提醒一次）
    mid_prompted: set[str] = field(default_factory=set)

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
        # plan-537-2251：内核拒绝了输入（data.failed）的操作不能算作「成功走通一步」——
        # 否则会拿一次没生效的点击去催模型沉淀，把错误做法一起记进路线。
        if ok and d.get("failed"):
            self.failures[scope] += 1
            return
        if ok:
            steps = len(a.get("actions") or []) if tool_name in ("browser_batch", "desktop_batch") else 1
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

    def _describe(self, scope: str) -> str:
        tool = "browser_recipe" if scope == "browser" else "desktop_recipe"
        what = "内置浏览器" if scope == "browser" else "电脑操控"
        key = "site（站点 host）" if scope == "browser" else "app_name（进程名）"
        where = "、".join(self.targets[scope][:3]) or "目标界面"
        fails = f"，其间失败 {self.failures[scope]} 次" if self.failures[scope] else ""
        return (f"- {what}：在「{where}」完成了 {self.actions[scope]} 步操作{fails} → 调用 {tool} action=save，"
                f"填 {key}、一句话 intent、通用做法（怎么定位、按什么顺序、哪些步骤可合并）与踩过的坑")

    def mid_reminder(self) -> str | None:
        """任务进行中（本步仍有工具调用）调用：提示模型可以现在就把路线存下来。

        plan-537-2251：原来的提醒只在「模型本步不再调用工具、准备收尾」时发出，
        而那一刻最终汇报正文已经生成并落库（agent_loop 的收尾分支先写消息再提醒），
        结果沉淀必然出现在汇报之后。这里增加一个中途触发点：步数够、还没保存、
        且本 scope 还没被中途提示过时给一次轻量提醒，让它自然发生在汇报之前。

        与 reminder() 的区别：
        - 不设置 reminded（那是收尾兜底的「每轮一次」额度），只记在 mid_prompted；
        - 每个 scope 只提示一次，避免每步刷屏；
        - 文案更短，强调「现在可以存」，不催促结束任务。
        """
        if not self.enabled:
            return None
        scopes = [s for s in self._pending_scopes() if s not in self.mid_prompted]
        if not scopes:
            return None
        for s in scopes:
            self.mid_prompted.add(s)
        return (
            "[系统提醒] 你已经在界面上走通了一段多步操作，可以现在就把它存成操作路线（不必等到汇报时）：\n"
            + "\n".join(self._describe(s) for s in scopes)
            + "\n存完继续手头任务即可；只记做法，不记坐标 / ref。"
        )

    def has_pending_reminder(self) -> bool:
        """是否还有未发出的沉淀提醒（只读判断，不消耗「每轮最多一次」的额度）。"""
        return bool(self.enabled and not self.reminded and self._pending_scopes())

    def reminder(self) -> str | None:
        """turn 准备结束时调用：需要提醒沉淀时返回提醒文本（每轮最多一次），否则 None。"""
        if not self.enabled or self.reminded:
            return None
        scopes = self._pending_scopes()
        if not scopes:
            return None
        self.reminded = True
        parts = [self._describe(scope) for scope in scopes]
        return (
            "[系统提醒] 本轮的界面操作还没有沉淀为操作路线，下次同类操作会重新摸索：\n"
            + "\n".join(parts)
            + "\n只记做法，不记坐标 / ref。若这次操作是一次性的、没有复用价值，或与已有路线相同且没有新经验，"
            "说明原因后即可结束本轮（不要为了留痕再补一条）。不要复述本条提醒。"
        )
