"""编辑工具链路测试（plan-20261016-ux 问题 3）。

覆盖：
- 读写对称：GBK + CRLF、UTF-8 BOM、LF 文件编辑后编码 / BOM / 换行保持不变；
- 宽松匹配：行尾空白、Tab/空格缩进（new_text 按文件缩进重排）、fs_read 行号前缀；
- 失败信息：多处命中列出行号、未命中给出最相近原文；
- 参数解析：字符串里的原始 Tab/换行、非法转义、代码围栏、截断 JSON；
- 执行器：参数解析失败时不进入工具、直接给出可操作的错误；
- multi_file_edit：同一文件的多处编辑按顺序叠加。
"""
import asyncio

from app.models.providers.tool_args import args_parse_error, parse_tool_args
from app.orchestration.tools.base import ToolContext
from app.orchestration.tools.editor import EditorApplyDiffTool
from app.orchestration.tools.fs_read import FsReadTool
from app.orchestration.tools.fs_write import FsWriteTool
from app.orchestration.tools.multi_edit import MultiFileEditTool
from app.orchestration.tools.text_edit import apply_replacement, decode_bytes


def _ctx(workspace) -> ToolContext:
    return ToolContext(
        workspace_root=str(workspace),
        session_id=1, task_id=1, agent_id=1, agent_name="tester",
    )


def _edit(tmp_path, args):
    return asyncio.run(EditorApplyDiffTool().run(args, _ctx(tmp_path)))


# ───────────────── 读写对称 ─────────────────

def test_gbk_crlf_roundtrip(tmp_path):
    f = tmp_path / "Clinic.java"
    src = "public class Clinic {\r\n    // 货位管理\r\n    int a = 1;\r\n}\r\n"
    f.write_bytes(src.encode("gbk"))
    r = _edit(tmp_path, {"path": "Clinic.java", "old_text": "    // 货位管理\n", "new_text": "    // 货位管理（已核对）\n"})
    assert r.ok, r.error
    raw = f.read_bytes()
    assert raw.decode("gbk") == src.replace("货位管理", "货位管理（已核对）")
    assert b"\r\n" in raw and raw.count(b"\r\n") == raw.count(b"\n")


def test_lf_file_stays_lf(tmp_path):
    f = tmp_path / "a.ts"
    f.write_bytes(b"const a = 1;\nconst b = 2;\n")
    r = _edit(tmp_path, {"path": "a.ts", "old_text": "const b = 2;", "new_text": "const b = 3;"})
    assert r.ok, r.error
    assert f.read_bytes() == b"const a = 1;\nconst b = 3;\n"


def test_utf8_bom_preserved(tmp_path):
    f = tmp_path / "b.tsx"
    f.write_bytes(b"\xef\xbb\xbfexport const x = 1;\r\n")
    r = _edit(tmp_path, {"path": "b.tsx", "old_text": "export const x = 1;", "new_text": "export const x = 2;"})
    assert r.ok, r.error
    assert f.read_bytes() == b"\xef\xbb\xbfexport const x = 2;\r\n"


def test_binary_file_refused(tmp_path):
    f = tmp_path / "x.bin"
    f.write_bytes(b"ab\x00cd")
    r = _edit(tmp_path, {"path": "x.bin", "old_text": "ab", "new_text": "zz"})
    assert not r.ok
    assert "二进制" in r.error
    assert f.read_bytes() == b"ab\x00cd"


def test_fs_read_decodes_gbk(tmp_path):
    f = tmp_path / "c.java"
    f.write_bytes("// 中文注释\r\nint a;\r\n".encode("gbk"))
    r = asyncio.run(FsReadTool().run({"path": "c.java"}, _ctx(tmp_path)))
    assert r.ok
    assert "中文注释" in r.output
    assert "GB18030" in r.output
    assert "共 2 行" in r.output


def test_fs_write_keeps_existing_style(tmp_path):
    f = tmp_path / "d.java"
    f.write_bytes("// 旧\r\n".encode("gbk"))
    r = asyncio.run(FsWriteTool().run({"path": "d.java", "content": "// 新内容\nint b;\n"}, _ctx(tmp_path)))
    assert r.ok, r.error
    assert f.read_bytes() == "// 新内容\r\nint b;\r\n".encode("gbk")


# ───────────────── 宽松匹配 ─────────────────

def test_trailing_whitespace_tolerated():
    content = "a = 1   \nb = 2\t\nc = 3\n"
    res = apply_replacement(content, "a = 1\nb = 2\n", "a = 10\nb = 20\n")
    assert res.ok and res.strategy == "trailing_ws"
    assert res.text == "a = 10\nb = 20\nc = 3\n"


def test_tab_indent_reindents_new_text():
    content = "class A {\n\tvoid f() {\n\t\treturn;\n\t}\n}\n"
    old = "    void f() {\n        return;\n    }"
    new = "    void f() {\n        log();\n        return;\n    }"
    res = apply_replacement(content, old, new)
    assert res.ok and res.strategy == "indent"
    assert res.text == "class A {\n\tvoid f() {\n\t\tlog();\n\t\treturn;\n\t}\n}\n"


def test_line_number_prefix_stripped():
    content = "def foo():\n    return 1\n\ndef bar():\n    pass\n"
    old = "  1: def foo():\n  2:     return 1"
    new = "  1: def foo():\n  2:     return 2"
    res = apply_replacement(content, old, new)
    assert res.ok and res.strategy == "line_numbers"
    assert res.text.startswith("def foo():\n    return 2\n")


def test_fuzzy_requires_unique():
    content = "x = 1  \ny = 2\nx = 1\t\ny = 2\n"
    res = apply_replacement(content, "x = 1\ny = 2", "z")
    assert not res.ok
    assert "匹配 2 处" in res.error and "1, 3" in res.error


def test_not_found_gives_nearest_snippet():
    content = "\n".join(f"line {i}" for i in range(1, 30)) + "\n\tvalue = compute(a, b)\n"
    res = apply_replacement(content, "    value = compute(a, c)", "x")
    assert not res.ok
    assert "最相近的原文" in res.error
    assert "value = compute(a, b)" in res.error
    assert "Tab" in res.error


def test_exact_multiple_lists_lines():
    res = apply_replacement("a\nb\na\n", "a", "c")
    assert not res.ok
    assert "匹配 2 处" in res.error and "第 1, 3 行" in res.error


def test_identical_old_new_rejected():
    res = apply_replacement("abc", "b", "b")
    assert not res.ok and "相同" in res.error


def test_decode_prefers_utf8():
    tf = decode_bytes("中文".encode("utf-8"))
    assert tf.encoding == "utf-8" and tf.text == "中文"


# ───────────────── 参数名容错 ─────────────────

def test_claude_style_arg_names(tmp_path):
    f = tmp_path / "e.py"
    f.write_text("x = 1\n", encoding="utf-8")
    r = _edit(tmp_path, {"file_path": "e.py", "old_string": "x = 1", "new_string": "x = 2"})
    assert r.ok, r.error
    assert f.read_text(encoding="utf-8") == "x = 2\n"


# ───────────────── multi_file_edit ─────────────────

def test_multi_edit_sequential_on_same_file(tmp_path):
    f = tmp_path / "m.py"
    f.write_bytes(b"a = 1\nb = 2\n")
    r = asyncio.run(MultiFileEditTool().run({"edits": [
        {"path": "m.py", "old_text": "a = 1", "new_text": "a = 10"},
        {"path": "m.py", "old_text": "a = 10\nb = 2", "new_text": "a = 10\nb = 20"},
    ]}, _ctx(tmp_path)))
    assert r.ok, r.error
    assert f.read_bytes() == b"a = 10\nb = 20\n"


def test_multi_edit_gbk_no_crash(tmp_path):
    f = tmp_path / "g.java"
    f.write_bytes("// 价格\r\n".encode("gbk"))
    r = asyncio.run(MultiFileEditTool().run({"edits": [
        {"path": "g.java", "old_text": "// 价格", "new_text": "// 零售价格"},
    ]}, _ctx(tmp_path)))
    assert r.ok, r.error
    assert f.read_bytes() == "// 零售价格\r\n".encode("gbk")


def test_multi_edit_failure_writes_nothing(tmp_path):
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    a.write_bytes(b"x = 1\n")
    b.write_bytes(b"y = 1\n")
    r = asyncio.run(MultiFileEditTool().run({"edits": [
        {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"},
        {"path": "b.py", "old_text": "nope", "new_text": "z"},
    ]}, _ctx(tmp_path)))
    assert not r.ok
    assert a.read_bytes() == b"x = 1\n"


# ───────────────── 参数解析 ─────────────────

def test_parse_raw_tab_and_newline_in_string():
    raw = '{"path": "a.java", "old_text": "\tint a;\n\tint b;", "new_text": "x"}'
    raw = raw.replace("\\t", "\t").replace("\\n", "\n")  # 模拟模型直接输出的原始控制字符
    args = parse_tool_args(raw)
    assert args_parse_error(args) is None
    assert args["old_text"] == "\tint a;\n\tint b;"


def test_parse_invalid_escape():
    args = parse_tool_args('{"text": "it\\\'s \\_ok"}')
    assert args_parse_error(args) is None
    assert args["text"] == "it\\'s \\_ok"


def test_parse_valid_json_unchanged():
    raw = '{"a": "x\\\\y", "b": "q\\"r"}'
    assert parse_tool_args(raw) == {"a": "x\\y", "b": 'q"r'}


def test_parse_fenced():
    args = parse_tool_args('```json\n{"a": 1}\n```')
    assert args == {"a": 1}


def test_parse_truncated_reports_reason():
    args = parse_tool_args('{"path": "a", "content": "abc')
    assert "不完整" in (args_parse_error(args) or "")


def test_executor_rejects_unparsed_args(tmp_path):
    from app.orchestration.tools.executor import ServerToolExecutor

    class _Agent:
        id = 1
        name = "tester"

    args = parse_tool_args('{"path": "a", "content": "abc')
    r = asyncio.run(ServerToolExecutor().execute(
        tool_name="fs_write", args=args, call_key="tc_x", agent=_Agent(), ctx=_ctx(tmp_path),
    ))
    assert not r.ok
    assert "未执行" in r.error and "JSON" in r.error
    assert not (tmp_path / "a").exists()


# ───────────────── 跨行 / 转义容错（plan-20261017） ─────────────────

def test_cross_line_whitespace_tolerated():
    """模型把两行合并成一行（或拆开）时，按空白归一化仍能唯一定位。"""
    content = (
        "/**\n"
        " * S1（plan-41-197）：`title` 不再落到原生属性，改由 ui/Tooltip 渲染统一圆角卡片浮层\n"
        " * （原生 title 是浏览器方形浮块且聚焦延迟明显）；`aria-label` 缺省时以 title 兜底。\n"
        " */\n"
        'import type { ButtonHTMLAttributes, ReactNode } from "react";\n'
    )
    old = (
        " * S1（plan-41-197）：`title` 不再落到原生属性，改由 ui/Tooltip 渲染统一圆角卡片浮层\n"
        " * （原生 title 是浏览器方形浮块且聚焦延迟明显）；`aria-label` 缺省时以 title 兜底。 */\n"
        'import type { ButtonHTMLAttributes, ReactNode } from "react";'
    )
    new = ' * S1 已更新\n */\nimport type { ReactNode } from "react";'
    res = apply_replacement(content, old, new)
    assert res.ok, res.error
    assert res.strategy == "whitespace"
    assert "S1 已更新" in res.text
    assert " * （原生 title" not in res.text


def test_cross_line_whitespace_requires_unique():
    content = "alpha beta\ngamma\n\nalpha beta\ngamma\n"
    res = apply_replacement(content, "alpha beta\ngamma", "x")
    assert not res.ok
    assert "匹配 2 处" in res.error


def test_partial_line_number_prefix_stripped():
    content = "def foo():\n    return 1\n\nx = 2\n"
    old = "  1: def foo():\n    return 1"
    res = apply_replacement(content, old, "def foo():\n    return 2")
    assert res.ok, res.error
    assert res.strategy == "line_numbers"
    assert res.text.startswith("def foo():\n    return 2\n")


def test_literal_escape_decoded():
    content = "a = 1\nb = 2\n"
    res = apply_replacement(content, "a = 1\\nb = 2", "a = 10\nb = 20")
    assert res.ok, res.error
    assert res.strategy == "literal_escapes"
    assert res.text == "a = 10\nb = 20\n"


def test_multi_edit_missing_path_reuses_sole_path(tmp_path):
    """同文件多处编辑时模型漏写某个 edit 的 path，复用本次唯一路径。"""
    f = tmp_path / "m.py"
    f.write_bytes(b"a = 1\nb = 2\n")
    r = asyncio.run(MultiFileEditTool().run({"edits": [
        {"old_text": "a = 1", "new_text": "a = 10"},
        {"path": "m.py", "old_text": "b = 2", "new_text": "b = 20"},
    ]}, _ctx(tmp_path)))
    assert r.ok, r.error
    assert f.read_bytes() == b"a = 10\nb = 20\n"


def test_multi_edit_accepts_json_string_edits(tmp_path):
    """部分模型把 edits 整个作为 JSON 字符串传入。"""
    import json

    f = tmp_path / "m.py"
    f.write_bytes(b"a = 1\n")
    r = asyncio.run(MultiFileEditTool().run({"edits": json.dumps([
        {"path": "m.py", "old_text": "a = 1", "new_text": "a = 2"},
    ])}, _ctx(tmp_path)))
    assert r.ok, r.error
    assert f.read_bytes() == b"a = 2\n"
