"""safe_path 防穿越单测。"""
from app.orchestration.tools.safe_path import safe_resolve


def test_normal_relative_path(tmp_path):
    p = safe_resolve(str(tmp_path), "src/main.py")
    assert p is not None
    assert p.name == "main.py"


def test_empty_path_rejected():
    assert safe_resolve("/tmp/ws", "") is None


def test_absolute_path_rejected():
    assert safe_resolve("/tmp/ws", "/etc/passwd") is None


def test_windows_absolute_rejected():
    assert safe_resolve("C:/ws", "C:/Windows/system32") is None


def test_parent_traversal_rejected():
    # workspace 外的相对路径应被拒
    assert safe_resolve("/tmp/ws", "../../../etc/passwd") is None


def test_dotdot_inside_workspace_ok():
    # workspace 内合法的 .. 应允许(如 a/../b.txt)
    import os
    p = safe_resolve(os.getcwd(), "sub/../file.txt")
    assert p is not None
    assert p.name == "file.txt"
