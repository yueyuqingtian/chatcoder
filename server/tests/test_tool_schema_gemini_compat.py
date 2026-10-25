"""Gemini 工具声明兼容性回归：enum 里不能出现空值。

Gemini 原生 function declaration 遇到 `enum: ["", "x"]` 会把整轮请求打成
400（`parameters.properties[purpose].enum[0]: cannot be empty`），
这类写法在「可选参数」上用起来很自然，很容易被重新写回去，
因此用手写常量与内置工具做全量断言。
"""
from app.orchestration.subagent_tools import (
    CANCEL_SUBAGENT_SCHEMA,
    COLLECT_RESULTS_SCHEMA,
    SPAWN_SUBAGENT_SCHEMA,
    SUBAGENT_INSPECT_SCHEMA,
)
from app.orchestration.tools.mcp_wrapper import _sanitize_input_schema
from app.orchestration.tools.registry import tool_registry

_SCHEMAS = tool_registry.all_schemas() + [
    SPAWN_SUBAGENT_SCHEMA,
    COLLECT_RESULTS_SCHEMA,
    SUBAGENT_INSPECT_SCHEMA,
    CANCEL_SUBAGENT_SCHEMA,
]


def _iter_enums(node):
    """深度遍历 schema，产出所有 enum 列表。"""
    if isinstance(node, dict):
        enum = node.get("enum")
        if isinstance(enum, list):
            yield enum
        for value in node.values():
            yield from _iter_enums(value)
    elif isinstance(node, list):
        for value in node:
            yield from _iter_enums(value)


def test_no_empty_enum_in_tool_schemas():
    offenders = []
    for schema in _SCHEMAS:
        name = schema.get("function", {}).get("name", "")
        for enum in _iter_enums(schema):
            assert enum, f"{name} 存在空 enum（Gemini 会拒绝整轮请求）"
            for value in enum:
                if isinstance(value, str) and value.strip() == "":
                    offenders.append(name)
    assert not offenders, f"以下工具的 enum 含空字符串：{sorted(set(offenders))}"


def test_spawn_subagent_purpose_enum_excludes_empty():
    props = SPAWN_SUBAGENT_SCHEMA["function"]["parameters"]["properties"]
    assert props["purpose"]["enum"] == ["operator"]
    # 省略 purpose 等价于普通子代理，运行时按空串判定
    assert "" not in props["purpose"]["enum"]


def test_sanitize_input_schema_drops_empty_enum_values():
    schema = {
        "type": "object",
        "properties": {
            "purpose": {"type": "string", "enum": ["", "operator"]},
            "only_empty": {"type": "string", "enum": [""]},
            "normal": {"type": "string", "enum": ["a", "b"]},
            "nested": {
                "type": "array",
                "items": {"type": "object", "properties": {"k": {"enum": ["", "v"]}}},
            },
        },
    }
    cleaned = _sanitize_input_schema(schema)
    props = cleaned["properties"]
    assert props["purpose"]["enum"] == ["operator"]
    assert "enum" not in props["only_empty"]
    assert props["normal"]["enum"] == ["a", "b"]
    assert props["nested"]["items"]["properties"]["k"]["enum"] == ["v"]
    # 不能就地改写调用方传入的 schema（MCP 清单会被缓存复用）
    assert schema["properties"]["purpose"]["enum"] == ["", "operator"]


if __name__ == "__main__":
    test_no_empty_enum_in_tool_schemas()
    test_spawn_subagent_purpose_enum_excludes_empty()
    test_sanitize_input_schema_drops_empty_enum_values()
    print("Gemini 工具声明兼容性测试通过")
