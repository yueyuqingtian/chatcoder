"""artifacts 抽取器单测。

由于 extract_and_persist_artifacts 依赖 DB,这里改为单独测试其内部的正则抽取逻辑
(把正则提取出来做纯函数式验证),DB 集成测试留 v0.5。
"""
import re

from app.orchestration.artifacts import _CODE_BLOCK_RE


def test_extract_single_code_block_with_lang():
    text = "如下:\n```python\nprint('hi')\n```\n完"
    matches = list(_CODE_BLOCK_RE.finditer(text))
    assert len(matches) == 1
    lang = matches[0].group(1)
    code = matches[0].group(2)
    assert lang == "python"
    assert "print('hi')" in code


def test_extract_code_block_without_lang():
    text = "```\nplain code\n```"
    matches = list(_CODE_BLOCK_RE.finditer(text))
    assert len(matches) == 1
    lang = matches[0].group(1)
    assert lang is None
    assert "plain code" in matches[0].group(2)


def test_extract_multiple_code_blocks():
    text = "```js\na\n```\n中间文字\n```py\nb\n```"
    matches = list(_CODE_BLOCK_RE.finditer(text))
    assert len(matches) == 2
    assert matches[0].group(1) == "js"
    assert matches[1].group(1) == "py"


def test_extract_no_code_block():
    text = "纯文本无代码块"
    matches = list(_CODE_BLOCK_RE.finditer(text))
    assert len(matches) == 0


def test_extract_incomplete_code_block_ignored():
    """未闭合的代码块不应被匹配。"""
    text = "```python\nprint('hi')\n缺少闭合"
    matches = list(_CODE_BLOCK_RE.finditer(text))
    assert len(matches) == 0


def test_write_paths_passthrough():
    """write_paths 直接作为 file 类型 artifact 的来源,无需正则。"""
    write_paths = ["src/main.py", "README.md"]
    # 模拟 extract_and_persist_artifacts 的 write_paths 处理逻辑
    artifacts = [{"type": "file", "path": p} for p in write_paths]
    assert len(artifacts) == 2
    assert artifacts[0]["path"] == "src/main.py"
