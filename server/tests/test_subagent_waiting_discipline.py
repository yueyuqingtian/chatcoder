"""plan-64-291：子代理等待期防重复劳动单测。

覆盖点：
- running_snapshot：运行中子代理清单（agent_id / 标题 / 已运行秒数）、排除已结束、
  started_at 缺失时降级不报错（不编造时长）。
- 运行中提醒文案：含 #id、已运行时长与三条等待期纪律。
- 节流判定 _should_emit_running_reminder：无运行不发；集合变化立即发；集合不变每 3 步发一次。
- 文案层：spawn 工具返回 / spawn schema / Dispatch mode 提示词都写明等待期纪律，
  且不再出现「把重复劳动合法化」的示例。
- subagent_inspect：running 时追加纪律尾注，已结束时不追加。
"""
import inspect
import time
from pathlib import Path

from app.orchestration import agent_loop as agent_loop_mod
from app.orchestration.subagent import SubagentHandle, SubagentManager

_ORCH = Path(__file__).resolve().parent.parent / "app" / "orchestration"


# ── running_snapshot：运行中子代理清单 ──────────────────────

def test_running_snapshot_lists_running_with_elapsed():
    mgr = SubagentManager(session_id=1)
    mgr._handles[7] = SubagentHandle(
        agent_id=7, status="running", task_title="盘点后端", started_at=time.monotonic() - 90,
    )
    snap = mgr.running_snapshot()
    assert len(snap) == 1
    assert snap[0]["agent_id"] == 7
    assert snap[0]["title"] == "盘点后端"
    assert snap[0]["elapsed_s"] is not None and snap[0]["elapsed_s"] >= 89


def test_running_snapshot_excludes_finished():
    mgr = SubagentManager(session_id=2)
    mgr._handles[1] = SubagentHandle(agent_id=1, status="running", started_at=time.monotonic())
    mgr._handles[2] = SubagentHandle(agent_id=2, status="done", started_at=time.monotonic())
    mgr._handles[3] = SubagentHandle(agent_id=3, status="failed", started_at=time.monotonic())
    assert [r["agent_id"] for r in mgr.running_snapshot()] == [1]


def test_running_snapshot_degrades_without_started_at():
    """started_at 缺失（旧对象/未设置）→ elapsed_s 为 None，不报错、不编造时长。"""
    mgr = SubagentManager(session_id=3)
    mgr._handles[5] = SubagentHandle(agent_id=5, status="running")  # started_at 默认 None
    snap = mgr.running_snapshot()
    assert snap[0]["elapsed_s"] is None


def test_spawn_records_started_at():
    """spawn 必须记录 started_at，否则运行期提醒拿不到「已运行多久」。"""
    src = inspect.getsource(SubagentManager.spawn)
    assert "started_at=time.monotonic()" in src


# ── 提醒文案 ────────────────────────────────────────────

def test_build_running_reminder_has_facts_and_discipline():
    text = agent_loop_mod._build_subagent_running_reminder([
        {"agent_id": 28, "title": "盘点后端子代理阻塞路径", "elapsed_s": 180.0},
    ])
    assert "#28" in text and "盘点后端子代理阻塞路径" in text
    assert "3 分钟" in text
    # 三条纪律：别自己干 / 只做不重叠 / 没有就阻塞等待
    assert "不要自己执行它们的任务" in text
    assert "不重叠" in text
    assert "collect_results(wait=true)" in text


def test_format_elapsed_zh_units():
    fmt = agent_loop_mod._format_elapsed_zh
    assert fmt(45) == "45 秒"
    assert fmt(180) == "3 分钟"
    assert fmt(7200) == "2.0 小时"


# ── 节流判定 ────────────────────────────────────────────

def test_should_emit_false_without_running():
    fn = agent_loop_mod._should_emit_running_reminder
    assert fn(has_running=False, sig=(), last_sig=(), step=10, last_step=0) is False


def test_should_emit_immediately_on_set_change():
    fn = agent_loop_mod._should_emit_running_reminder
    # 首次出现运行中子代理（签名由空变非空）→ 立即注入
    assert fn(has_running=True, sig=(28, 29), last_sig=(), step=1, last_step=0) is True
    # 集合变化（新派发 #30）→ 立即注入
    assert fn(has_running=True, sig=(28, 29, 30), last_sig=(28, 29), step=2, last_step=1) is True


def test_should_emit_throttled_within_gap():
    fn = agent_loop_mod._should_emit_running_reminder
    gap = agent_loop_mod._SUBAGENT_REMINDER_STEP_GAP
    # 集合不变、间隔未达 → 不重复注入
    assert fn(has_running=True, sig=(1,), last_sig=(1,), step=2, last_step=1) is False
    assert fn(has_running=True, sig=(1,), last_sig=(1,), step=gap, last_step=1) is False
    # 达到间隔 → 注入
    assert fn(has_running=True, sig=(1,), last_sig=(1,), step=1 + gap, last_step=1) is True


# ── 文案层：工具返回 / schema / 提示词 ─────────────────────

def test_spawn_tool_result_documents_waiting_discipline():
    src = (_ORCH / "agent_loop.py").read_text(encoding="utf-8")
    assert "Waiting discipline" in src
    assert "do NOT do its task yourself" in src
    assert "NON-overlapping" in src
    assert "collect_results(wait=true)" in src


def test_spawn_schema_states_waiting_discipline():
    src = (_ORCH / "subagent_tools.py").read_text(encoding="utf-8")
    assert "waiting discipline" in src
    assert "NEVER do the delegated task yourself" in src


def test_cancel_schema_drops_duplication_legitimation():
    """取消工具不得再把「我已经自己找到答案了」写成正当理由（重复劳动的合法化暗示）。"""
    src = (_ORCH / "subagent_tools.py").read_text(encoding="utf-8")
    assert "you already found the answer" not in src
    assert "never cancel after duplicating" in src


def test_dispatch_mode_prompt_has_waiting_discipline():
    """提示词不再有「不要空等」的歧义表述，改为明确的等待期纪律。"""
    src = (_ORCH / "prompts" / "main.py").read_text(encoding="utf-8")
    assert "Never sit idle waiting for it" not in src
    assert "NON-overlapping" in src
    assert "collect_results(wait=true)" in src


# ── subagent_inspect 触点纪律 ────────────────────────────

async def test_inspect_running_appends_discipline_note():
    """running 状态必须追加纪律尾注——上一轮实测正是在此触点开始重复劳动。"""
    mgr = SubagentManager(session_id=11)
    mgr._handles[7] = SubagentHandle(agent_id=7, status="running", task_title="T")
    out = await agent_loop_mod._run_subagent_tool(
        None, tool_name="subagent_inspect", args={"agent_id": 7},
        session_id=1, turn_id=2, agent=None, workspace=".",
        subagent_context={"manager": mgr},
    )
    assert "[等待期纪律]" in out
    assert "collect_results(wait=true)" in out


async def test_inspect_finished_has_no_discipline_note():
    mgr = SubagentManager(session_id=12)
    mgr._handles[7] = SubagentHandle(agent_id=7, status="done", task_title="T", findings="done")
    out = await agent_loop_mod._run_subagent_tool(
        None, tool_name="subagent_inspect", args={"agent_id": 7},
        session_id=1, turn_id=2, agent=None, workspace=".",
        subagent_context={"manager": mgr},
    )
    assert "[等待期纪律]" not in out
