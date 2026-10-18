"""长输出分段与落盘续读回归测试（plan-156-620）。

覆盖：
  1) 工具输出超出上下文预算时完整落盘，按提示用 read_attachment 分段读回的内容与原文一致；
  2) Ta3 会话下续读提示使用 Ta3 可见的工具名与参数键，非 Ta3 用真实名；
  3) 工作流交接文本：上游结论只给一份、不被其它上游挤没，超预算时写明总字数；
  4) 节点产出落库不再截断。
"""
import re
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.orchestration import output_paging
from app.orchestration.output_paging import present_output, set_ta3_visible, tool_ref
from app.orchestration.tools.base import ToolContext
from app.orchestration.tools.read_attachment import ReadAttachmentTool


@pytest.fixture()
def uploads(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "uploads_dir", str(tmp_path))
    set_ta3_visible(False)
    return tmp_path


def _ctx() -> ToolContext:
    return ToolContext(workspace_root=".", session_id=1, task_id=1, agent_id=1, agent_name="test")


async def test_spilled_output_reads_back_completely(uploads):
    original = "".join(f"第{i}行 输出内容\n" for i in range(6000))
    shown = present_output(original, 16000, session_id=9, tag="terminal")
    assert len(shown) < 16000 + 600
    assert f"输出共 {len(original)} 字" in shown

    m = re.search(r'read_attachment\(path="([^"]+)", offset=(\d+)\)', shown)
    assert m, shown[-300:]
    path, offset = m.group(1), int(m.group(2))
    collected = original[:offset]
    tool = ReadAttachmentTool()
    for _ in range(20):  # 上限防死循环
        res = await tool.run({"path": path, "offset": offset}, _ctx())
        assert res.ok, res.output
        assert res.data["total"] == len(original)  # 落盘内容与原文逐字一致（无换行转换）
        collected += res.data["text"]
        if res.data["offset"] + len(res.data["text"]) >= res.data["total"]:
            break
        nxt = re.findall(r"offset=(\d+)\)", res.output)
        assert nxt, res.output[-300:]
        offset = int(nxt[-1])
    assert collected == original


def test_short_output_untouched(uploads):
    assert present_output("ok", 16000, session_id=1, tag="t") == "ok"


def test_tool_ref_follows_ta3_aliases():
    set_ta3_visible(False)
    assert tool_ref("collect_results", agent_id=7, report_offset="{offset}") == \
        "collect_results(agent_id=7, report_offset={offset})"
    set_ta3_visible(True)
    try:
        ref = tool_ref("collect_results", agent_id=7, report_offset="{offset}")
        assert ref == "TaskQuery(taskId=7, reportOffset={offset})"
        assert tool_ref("read_attachment", path="a.txt").startswith("ReadAttachment(")
    finally:
        set_ta3_visible(False)


def test_workflow_handoff_keeps_each_upstream():
    from app.orchestration.workflow.executor_subagent import UPSTREAM_MIN_SHARE_CHARS, _handoff

    long_finding = "甲" * 50000
    req = SimpleNamespace(
        goal="目标", flow_key="f", node_key="n", kind="execute",
        upstream={
            "a": {"output": {"summary": long_finding, "findings": long_finding, "files_touched": ["x.py"]}},
            "b": {"output": {"summary": "乙的结论", "findings": "乙的结论"}},
        },
    )
    text = _handoff(req)
    # 第二个上游不会被第一个长结论挤掉
    assert "乙的结论" in text and "x.py" in text
    # 结论只出现一份（summary 与 findings 相同不重复），超出份额时写明总字数
    assert text.count("乙的结论") == 1
    assert f"结论共 {len(long_finding)} 字" in text
    assert text.count("甲") >= UPSTREAM_MIN_SHARE_CHARS // 2


def test_workflow_node_output_not_truncated():
    from app.orchestration.workflow.executor_subagent import _to_result

    findings = "结" * 20000
    handle = SimpleNamespace(status="done", summary=findings, findings=findings, files_touched=[],
                             risks=[], verification=[], acceptance=[], error=None)
    result = _to_result(handle, agent_id=3)
    assert result.output["findings"] == findings
    assert result.output["summary"] == findings


def test_spill_dir_is_readable_by_read_attachment(uploads):
    path = output_paging.spill_text("abc", session_id=2, tag="x")
    assert path is not None and str(path).startswith(str(uploads))


def test_csv_truncation_is_announced_and_full_read_available():
    from app.services.doc_parser import _parse_csv_like

    data = ("h1,h2\n" + "".join(f"{i},v{i}\n" for i in range(300))).encode("utf-8")
    preview = _parse_csv_like(data)
    assert "共 301 行" in preview  # 此前提示永远不出现（读取器已被耗尽）
    full = _parse_csv_like(data, max_rows=None)
    assert "| 299 | v299 |" in full and "仅显示" not in full
