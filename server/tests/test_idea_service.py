"""plan-308-1542 需求7-B：IDEA 双向断点通道测试。

覆盖：
  1) 读 IDEA 断点（.idea/workspace.xml 解析，$PROJECT_DIR$ 归一化）；
  2) 写 IDEA 断点（去重、备份 .bak、返回"需重启生效"警告）；
  3) 删除 IDEA 断点；
  4) JDWP 进程解析（纯函数）与 method_at_line 推导 class#method；
  5) 解析失败/文件缺失时优雅降级（不抛异常，不影响调试主流程）。
"""
from pathlib import Path

import pytest

from app.services import idea_service

_WS_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<project version="4">
  <component name="XDebuggerManager">
    <breakpoint-manager>
      <breakpoints>
{items}
      </breakpoints>
    </breakpoint-manager>
  </component>
</project>
"""


def _write_workspace(root: Path, items: str) -> Path:
    d = root / ".idea"
    d.mkdir(parents=True, exist_ok=True)
    ws = d / "workspace.xml"
    ws.write_text(_WS_TEMPLATE.format(items=items), encoding="utf-8")
    return ws


@pytest.fixture
def project(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    (p / "src/main/java/com/example").mkdir(parents=True)
    return p


def test_list_breakpoints_parses_project_dir_urls(project: Path):
    _write_workspace(project, (
        '        <line-breakpoint enabled="true" '
        'url="file://$PROJECT_DIR$/src/main/java/com/example/A.java" line="42" suspend="ALL" />\n'
        '        <line-breakpoint enabled="false" '
        'url="file://$PROJECT_DIR$/src/main/java/com/example/B.java" line="7" />'
    ))
    out = idea_service.list_breakpoints(str(project))
    assert out["ok"] is True and out["available"] is True
    rows = out["breakpoints"]
    assert len(rows) == 2
    a = next(r for r in rows if r["line"] == 42)
    assert a["file"] == "src/main/java/com/example/A.java", "$PROJECT_DIR$ 应归一化为相对路径"
    assert a["enabled"] is True and a["source"] == "idea"
    b = next(r for r in rows if r["line"] == 7)
    assert b["enabled"] is False


def test_list_breakpoints_missing_workspace_degrades(project: Path):
    """无 .idea/workspace.xml：降级为 available=False，不抛异常。"""
    out = idea_service.list_breakpoints(str(project))
    assert out["ok"] is True
    assert out["available"] is False
    assert out["breakpoints"] == []
    assert "workspace.xml" in out["reason"]


def test_list_breakpoints_broken_xml_degrades(project: Path):
    d = project / ".idea"
    d.mkdir(parents=True, exist_ok=True)
    (d / "workspace.xml").write_text("<project><broken", encoding="utf-8")
    out = idea_service.list_breakpoints(str(project))
    assert out["ok"] is True and out["available"] is False
    assert out["breakpoints"] == []


def test_add_breakpoint_writes_backup_and_warns(project: Path):
    """写入 IDEA 断点：必须生成 .bak 备份，并给出"需重启生效"警告。"""
    ws = _write_workspace(project, "")
    out = idea_service.add_breakpoint(str(project), "src/main/java/com/example/A.java", 15,
                                      idea_running=True)
    assert out["ok"] is True and out["added"] is True
    assert out["backup"] and Path(out["backup"]).is_file(), "写前必须备份 workspace.xml"
    assert "重启" in out["warning"], "必须提示需重启 IDEA 生效"
    assert "覆盖" in out["warning"], "IDEA 运行中时必须提示可能被覆盖"

    # 写回后能被重新读出
    rows = idea_service.list_breakpoints(str(project))["breakpoints"]
    assert any(r["line"] == 15 for r in rows)
    # 且文件仍是合法 XML（tree.write 不应破坏结构）
    assert "<line-breakpoint" in ws.read_text(encoding="utf-8")


def test_add_breakpoint_deduplicates(project: Path):
    _write_workspace(project, "")
    r1 = idea_service.add_breakpoint(str(project), "src/A.java", 9, idea_running=False)
    r2 = idea_service.add_breakpoint(str(project), "src/A.java", 9, idea_running=False)
    assert r1["added"] is True
    assert r2["ok"] is True and r2["duplicated"] is True and r2.get("added") is not True
    rows = idea_service.list_breakpoints(str(project))["breakpoints"]
    assert len([r for r in rows if r["line"] == 9]) == 1, "同 file+line 不应重复写入"


def test_remove_breakpoint(project: Path):
    _write_workspace(project, (
        '        <line-breakpoint enabled="true" '
        'url="file://$PROJECT_DIR$/src/A.java" line="11" />'
    ))
    out = idea_service.remove_breakpoint(str(project), "src/A.java", 11)
    assert out["ok"] is True and out["removed"] == 1
    assert idea_service.list_breakpoints(str(project))["breakpoints"] == []

    # 删除不存在的断点：不报错，返回 removed=0
    out2 = idea_service.remove_breakpoint(str(project), "src/A.java", 999)
    assert out2["ok"] is True and out2["removed"] == 0


def test_add_breakpoint_missing_workspace(project: Path):
    out = idea_service.add_breakpoint(str(project), "src/A.java", 1)
    assert out["ok"] is False and "workspace.xml" in out["error"]


# ── JDWP 进程解析（纯函数）──

def test_parse_jdwp_processes():
    sample = (
        "1234 com.example.Main -agentlib:jdwp=transport=dt_socket,server=y,suspend=n,address=*:5005\n"
        "5678 org.other.App -Xmx512m\n"
        "9012 com.example.Svc -javaagent:foo.jar -agentlib:jdwp=transport=dt_socket,address=127.0.0.1:6006\n"
        "jps JdkBuiltInTool JVMDetectedTool\n"
    )
    out = idea_service.parse_jdwp_processes(sample)
    assert len(out) == 2, "只应识别带 -agentlib:jdwp 的 JVM"
    assert out[0] == {"pid": 1234, "main_class": "com.example.Main",
                      "jdwp_port": 5005, "debugging": True}
    assert out[1]["jdwp_port"] == 6006


def test_parse_jdwp_processes_empty():
    assert idea_service.parse_jdwp_processes("") == []
    assert idea_service.parse_jdwp_processes("jps JdkBuiltInTool\n") == []


# ── method_at_line：文件:行 → class#method ──

_JAVA_SRC = """package com.example;

public class OrderService {
    private int count = 0;

    public void handle(String id) {
        int a = 1;
        int b = a + 1;
        System.out.println(id + b);
    }

    public String name() {
        return "svc";
    }
}
"""


def test_method_at_line_resolves_enclosing_method(project: Path):
    src = project / "src/main/java/com/example/OrderService.java"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(_JAVA_SRC, encoding="utf-8")
    # 第 8 行（int b = a + 1;）位于 handle() 内
    out = idea_service.method_at_line(str(project), "src/main/java/com/example/OrderService.java", 8)
    assert out["ok"] is True
    assert out["class"] == "com.example.OrderService"
    assert out["method"] == "handle"
    assert out["target"] == "com.example.OrderService#handle"

    # 第 13 行（return "svc";）位于 name() 内
    out2 = idea_service.method_at_line(str(project), "src/main/java/com/example/OrderService.java", 13)
    assert out2["method"] == "name"


def test_method_at_line_missing_file(project: Path):
    out = idea_service.method_at_line(str(project), "src/Nope.java", 1)
    assert out["ok"] is False and "不存在" in out["error"]


def test_method_at_line_out_of_range(project: Path):
    src = project / "src/A.java"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("package p;\nclass A {}\n", encoding="utf-8")
    out = idea_service.method_at_line(str(project), "src/A.java", 999)
    assert out["ok"] is False and "越界" in out["error"]


def test_detect_debug_session_shape(project: Path):
    """探测会话：结构完整，缺 jps 时也不抛异常（降级为空会话）。"""
    out = idea_service.detect_debug_session(str(project))
    assert out["ok"] is True
    assert isinstance(out["idea_running"], bool)
    assert isinstance(out["sessions"], list)
    assert isinstance(out["note"], str)
