"""ta3 流式工具参数累积兼容（plan-20261017）。

覆盖：
- 标准增量、重复完整帧、累计式帧三种合并行为；
- OpenAI 协议多工具复用 index=0 时按 id 另开槽位；
- Anthropic content_block_start 直接下发完整 input 的网关形态；
- Anthropic partial_json 的重复/累计帧。

背景：部分 OpenAI/Anthropic 兼容网关在流式工具调用里重复发送完整
function.name / arguments，或所有工具都用 index=0。旧实现直接 `+=`
拼接，会把工具名变成 `EditEdit`、把参数拼成非法 JSON，最终表现为
「编辑工具时好时坏 / path 为空 / old_text 未找到」。
"""
import json

from app.models.providers.ta3 import Ta3Provider, _merge_stream_text


def _provider(**meta) -> Ta3Provider:
    return Ta3Provider(
        api_key="k", base_url="https://lc.example.com/newcoder",
        model="glm-4.6", meta=meta,
    )


def _frame(payload: dict) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False)


def _openai_tc(index: int, *, tid: str = "", name: str = "", args: str = "") -> dict:
    fn: dict = {}
    if name:
        fn["name"] = name
    if args:
        fn["arguments"] = args
    tc: dict = {"index": index, "function": fn}
    if tid:
        tc["id"] = tid
    return {"choices": [{"delta": {"tool_calls": [tc]}}]}


# ── 合并原语 ──

def test_merge_incremental():
    assert _merge_stream_text("", '{"pa') == '{"pa'
    assert _merge_stream_text('{"pa', 'th":"a"}') == '{"path":"a"}'


def test_merge_repeated_full_chunk():
    cur = '{"path":"a"}'
    assert _merge_stream_text(cur, cur) == cur


def test_merge_cumulative_chunk():
    assert _merge_stream_text('{"pa', '{"path":"a"}') == '{"path":"a"}'


def test_merge_keeps_legitimate_repeated_char():
    # 正常增量里出现与当前累积相同的字符（如 "a" 连续两次）不能被当成重复帧
    assert _merge_stream_text('{"v":"a', "a") == '{"v":"aa'


# ── OpenAI 协议 ──

def test_openai_repeated_name_frame_does_not_duplicate():
    p = _provider()
    monitor = p._new_monitor()
    p._parse_openai_frame(_frame(_openai_tc(0, tid="t1", name="Edit", args='{"filepath":"a.ts",')), monitor)
    p._parse_openai_frame(_frame(_openai_tc(0, name="Edit", args='"oldString":"a"')), monitor)
    p._parse_openai_frame(_frame(_openai_tc(0, name="Edit", args=',"newString":"b"}')), monitor)
    slot = monitor["tool_calls"][0]
    assert slot["name"] == "Edit"
    assert json.loads(slot["arguments"]) == {
        "filepath": "a.ts", "oldString": "a", "newString": "b",
    }


def test_openai_duplicate_full_arguments_frame():
    p = _provider()
    monitor = p._new_monitor()
    full = '{"filepath":"a.ts","oldString":"a","newString":"b"}'
    p._parse_openai_frame(_frame(_openai_tc(0, tid="t1", name="Edit", args=full)), monitor)
    p._parse_openai_frame(_frame(_openai_tc(0, name="Edit", args=full)), monitor)
    assert monitor["tool_calls"][0]["arguments"] == full


def test_openai_multi_tools_share_index_zero():
    """网关所有工具都发 index=0：按 id 变化另开槽位，参数不能互相拼接。"""
    p = _provider()
    monitor = p._new_monitor()
    p._parse_openai_frame(_frame(_openai_tc(0, tid="t1", name="Read", args='{"filepath":"a.ts"}')), monitor)
    p._parse_openai_frame(_frame(_openai_tc(0, tid="t2", name="Edit", args='{"filepath":"a.ts","oldString":"a","newString":"b"}')), monitor)
    slots = monitor["tool_calls"]
    assert len(slots) == 2
    names = {s["name"] for s in slots.values()}
    assert names == {"Read", "Edit"}
    for s in slots.values():
        json.loads(s["arguments"])  # 两个参数都必须是完整合法 JSON


# ── Anthropic 协议 ──

def test_anthropic_start_carries_full_input():
    """部分网关在 content_block_start 直接给完整 input，没有 input_json_delta。"""
    p = _provider(anthropic=True, provider="kimi")
    monitor = p._new_monitor()
    p._parse_anthropic_frame(_frame({
        "type": "content_block_start", "index": 0,
        "content_block": {
            "type": "tool_use", "id": "t1", "name": "Edit",
            "input": {"filepath": "a.ts", "oldString": "a", "newString": "b"},
        },
    }), monitor)
    slot = monitor["anthropic_tools"][0]
    assert slot["name"] == "Edit"
    assert json.loads(slot["arguments"])["oldString"] == "a"


def test_anthropic_partial_json_duplicate_and_cumulative():
    p = _provider(anthropic=True, provider="kimi")
    monitor = p._new_monitor()
    p._parse_anthropic_frame(_frame({
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "tool_use", "id": "t1", "name": "Edit", "input": {}},
    }), monitor)
    first = '{"filepath":"a.ts"'
    full = '{"filepath":"a.ts","oldString":"a","newString":"b"}'
    p._parse_anthropic_frame(_frame({
        "type": "content_block_delta", "index": 0,
        "delta": {"type": "input_json_delta", "partial_json": first},
    }), monitor)
    # 重复完整帧
    p._parse_anthropic_frame(_frame({
        "type": "content_block_delta", "index": 0,
        "delta": {"type": "input_json_delta", "partial_json": first},
    }), monitor)
    # 累计式帧
    p._parse_anthropic_frame(_frame({
        "type": "content_block_delta", "index": 0,
        "delta": {"type": "input_json_delta", "partial_json": full},
    }), monitor)
    assert monitor["anthropic_tools"][0]["arguments"] == full
