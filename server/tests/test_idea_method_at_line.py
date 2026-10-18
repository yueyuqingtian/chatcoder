"""IDEA 断点 → Arthas 方法定位（idea_service.method_at_line）回归测试。

原实现只向上找最近的「像方法签名」的行，`throw new X(...)`、`return new Y(...)` 会被误判为方法名，
导致 Arthas watch 找不到目标。这里覆盖：语句行、嵌套类、构造函数、多行签名、注释与字符串中的花括号、
字段初始化（应明确失败）。
"""
from app.services import idea_service

JAVA = """package com.demo.svc;

import java.util.ArrayList;
import java.util.List;

/** 说明里的 { 花括号 不应影响计数 */
public class OrderService {
    private final String name = "a}b{c";
    private final int limit;

    public OrderService(int limit) {
        this.limit = limit;
    }

    public List<String> load(String key) throws Exception {
        if (key == null) {
            throw new IllegalArgumentException("key");
        }
        return new ArrayList<>();
    }

    public void save(
            String item,
            int size) {
        log(item);
    }

    static class Helper {
        int twice(int x) {
            return x * 2;
        }
    }

    private static final String TABLE = build();
}
"""


def _resolve(tmp_path, line: int):
    src = tmp_path / "src" / "OrderService.java"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(JAVA, encoding="utf-8")
    return idea_service.method_at_line(str(tmp_path), "src/OrderService.java", line)


def _line_of(fragment: str) -> int:
    for i, text in enumerate(JAVA.splitlines(), start=1):
        if fragment in text:
            return i
    raise AssertionError(fragment)


def test_throw_new_inside_method_resolves_to_enclosing_method(tmp_path):
    r = _resolve(tmp_path, _line_of('throw new IllegalArgumentException'))
    assert r["ok"] is True
    assert r["target"] == "com.demo.svc.OrderService#load"


def test_return_new_inside_method_resolves_to_enclosing_method(tmp_path):
    r = _resolve(tmp_path, _line_of("return new ArrayList"))
    assert r["target"] == "com.demo.svc.OrderService#load"


def test_constructor_body_is_init(tmp_path):
    r = _resolve(tmp_path, _line_of("this.limit = limit;"))
    assert r["target"] == "com.demo.svc.OrderService#<init>"


def test_multiline_signature_method(tmp_path):
    r = _resolve(tmp_path, _line_of("log(item);"))
    assert r["target"] == "com.demo.svc.OrderService#save"


def test_nested_class_uses_arthas_dollar_name(tmp_path):
    r = _resolve(tmp_path, _line_of("return x * 2;"))
    assert r["ok"] is True
    assert r["target"] == "com.demo.svc.OrderService$Helper#twice"


def test_field_initializer_is_rejected_with_hint(tmp_path):
    r = _resolve(tmp_path, _line_of("private static final String TABLE"))
    assert r["ok"] is False
    assert r["target"] is None
    assert "方法体内" in r["error"]


def test_braces_inside_strings_and_comments_do_not_break_scope(tmp_path):
    # "a}b{c" 在字段里，load 方法仍应被正确识别
    r = _resolve(tmp_path, _line_of("if (key == null)"))
    assert r["target"] == "com.demo.svc.OrderService#load"


def test_missing_file_reports_error(tmp_path):
    r = idea_service.method_at_line(str(tmp_path), "nope/Missing.java", 3)
    assert r["ok"] is False
    assert "不存在" in r["error"]
