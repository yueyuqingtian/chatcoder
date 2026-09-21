"""IntelliJ IDEA 联动服务（plan-308-1542 需求7-B）。

用户诉求：*"增强 java 项目在 IDEA 以调试运行的情况下与当前软件的交互，
例如 IDEA 里面打了断点，到达断点当前软件也可捕获；当前软件 AI 打了断点，
IDEA 里也可以看到，看看可不可以和 IDEA 搭建通道"*。

可行的通道（用户已确认"双向写入"）
------------------------------------
1. **读 IDEA 断点**：IDEA 把断点存在工程内的 `.idea/workspace.xml`
   （`XDebuggerManager → breakpoint-manager → breakpoints → line-breakpoint`）。
   读它是纯文件解析，零风险。
2. **写 IDEA 断点**：把 AI 下的断点按 IDEA 的 XML 结构写回 `workspace.xml`，
   使 IDEA 打开工程后能看到这些断点。**注意边界（必须在 UI 明示）**：
   - 需要**重启/重新加载 IDEA** 才生效（IDEA 在内存中持有断点表，运行中写文件会被忽略甚至覆盖）；
   - IDEA 运行期间可能**覆盖**我们的写入。
   因此写前一律备份 `workspace.xml.bak`。
3. **捕获 IDEA 命中**：IDEA 调试会独占 JDWP 通道（本软件连不上），但 Arthas 走
   Attach API **可与 IDEA 调试并存**。因此"IDEA 断点命中本软件可见"的实现路径是：
   为 IDEA 断点**建立 Arthas 方法级观测**——命中时 Arthas 上报，本软件实时展示。
   本模块提供 `method_at_line` 把"文件:行"推导为 `class#method`，供建立观测使用。
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

logger = logging.getLogger(__name__)

_WORKSPACE = ".idea/workspace.xml"
_IDEA_DISPLAY = "IntelliJ IDEA"


# ───────────────────────────────────────────────────────────────
# 1) 读 IDEA 断点
# ───────────────────────────────────────────────────────────────

def workspace_path(project_path: str) -> Path:
    return Path(project_path) / _WORKSPACE


def _norm_rel(project_path: str, url: str) -> str:
    """把 IDEA 的 `file://$PROJECT_DIR$/src/A.java` 归一化为工程相对路径。"""
    u = (url or "").replace("\\", "/")
    u = u.replace("file://$PROJECT_DIR$/", "").replace("file://$PROJECT_DIR$", "")
    u = u.replace("$PROJECT_DIR$/", "").replace("$PROJECT_DIR$", "")
    if u.startswith("file:///"):
        # 绝对 file URL → 尝试相对工程根
        try:
            p = Path(u[len("file:///"):])
            return str(p.relative_to(Path(project_path))).replace("\\", "/")
        except (ValueError, OSError):
            return u
    return u.lstrip("/")


def list_breakpoints(project_path: str) -> dict:
    """解析 IDEA 工程断点。解析失败一律降级为空列表（不得影响调试主流程）。"""
    ws = workspace_path(project_path)
    if not ws.is_file():
        return {"ok": True, "available": False, "reason": f"未找到 {_WORKSPACE}",
                "breakpoints": []}
    try:
        tree = ET.parse(str(ws))
    except (ET.ParseError, OSError) as e:
        logger.debug("[idea] 解析 workspace.xml 失败: %s", e)
        return {"ok": True, "available": False, "reason": f"无法解析 {_WORKSPACE}: {e}",
                "breakpoints": []}

    root = tree.getroot()
    out: list[dict] = []
    # 结构：component[name=XDebuggerManager] → breakpoint-manager → breakpoints → line-breakpoint
    for comp in root.findall("component"):
        if comp.get("name") != "XDebuggerManager":
            continue
        for bp_mgr in comp.findall("breakpoint-manager"):
            for bps in bp_mgr.findall("breakpoints"):
                for bp in bps:
                    if bp.tag not in ("line-breakpoint",):
                        continue
                    url = bp.get("url") or ""
                    rel = _norm_rel(project_path, url)
                    try:
                        line = int(bp.get("line") or 0)
                    except ValueError:
                        line = 0
                    out.append({
                        "id": f"idea:{rel}:{line}",
                        "file": rel,
                        "line": line or None,
                        "enabled": (bp.get("enabled") or "true") != "false",
                        "suspend": bp.get("suspend") or "ALL",
                        "source": "idea",
                    })
    return {"ok": True, "available": True, "breakpoints": out,
            "path": str(ws), "count": len(out)}


# ───────────────────────────────────────────────────────────────
# 2) 写 IDEA 断点（双向写入；需重启 IDEA 生效）
# ───────────────────────────────────────────────────────────────

def _backup(ws: Path) -> str | None:
    try:
        bak = ws.with_suffix(".xml.bak")
        shutil.copy2(ws, bak)
        return str(bak)
    except OSError:
        logger.debug("[idea] 备份 workspace.xml 失败(非阻塞)", exc_info=True)
        return None


def _ensure_breakpoint_container(root: ET.Element) -> ET.Element:
    """取/建 XDebuggerManager → breakpoint-manager → breakpoints 节点。"""
    comp = None
    for c in root.findall("component"):
        if c.get("name") == "XDebuggerManager":
            comp = c
            break
    if comp is None:
        comp = ET.SubElement(root, "component", {"name": "XDebuggerManager"})
    mgr = comp.find("breakpoint-manager")
    if mgr is None:
        mgr = ET.SubElement(comp, "breakpoint-manager")
    bps = mgr.find("breakpoints")
    if bps is None:
        bps = ET.SubElement(mgr, "breakpoints")
    return bps


def add_breakpoint(project_path: str, file: str, line: int, *,
                   suspend: str = "ALL", idea_running: bool | None = None) -> dict:
    """把断点写入 IDEA 的 workspace.xml（写前备份）。

    plan-308-1542 需求7-B（用户决策 D：双向写入）：
    返回值中带 `warning`，前端/工具必须把它展示给用户——
    "需重启 IDEA 生效，且可能被 IDEA 覆盖"。
    """
    ws = workspace_path(project_path)
    if not ws.is_file():
        return {"ok": False, "error": f"未找到 {_WORKSPACE}（请先在 IDEA 中打开过该工程）"}
    rel = (file or "").replace("\\", "/").lstrip("/")
    if not rel:
        return {"ok": False, "error": "缺少 file（工程相对路径）"}
    if idea_running is None:
        idea_running = is_idea_running()

    try:
        tree = ET.parse(str(ws))
    except (ET.ParseError, OSError) as e:
        return {"ok": False, "error": f"无法解析 {_WORKSPACE}: {e}"}

    root = tree.getroot()
    bps = _ensure_breakpoint_container(root)
    # 去重：同 file + line 已存在则不重复写
    for bp in bps.findall("line-breakpoint"):
        if _norm_rel(project_path, bp.get("url") or "") == rel and \
                int(bp.get("line") or 0) == int(line):
            return {"ok": True, "added": False, "duplicated": True,
                    "breakpoint": {"file": rel, "line": int(line)},
                    "warning": _write_warning(idea_running)}

    backup = _backup(ws)
    el = ET.SubElement(bps, "line-breakpoint", {
        "enabled": "true",
        "url": f"file://$PROJECT_DIR$/{rel}",
        "line": str(int(line)),
        "suspend": suspend,
    })
    ET.SubElement(el, "option", {"name": "logMessage", "value": "true"})
    try:
        tree.write(str(ws), encoding="UTF-8", xml_declaration=True)
    except OSError as e:
        return {"ok": False, "error": f"写入 {_WORKSPACE} 失败: {e}"}

    logger.info("[idea] 已写入断点 %s:%s（备份=%s）", rel, line, backup)
    return {"ok": True, "added": True, "backup": backup,
            "breakpoint": {"file": rel, "line": int(line)},
            "warning": _write_warning(idea_running)}


def _write_warning(idea_running: bool) -> str:
    base = "断点已写入 IDEA 配置，需重启（或重新加载工程）IDEA 后生效"
    if idea_running:
        base += "；检测到 IDEA 正在运行，运行期写入可能被 IDEA 覆盖"
    return base


def remove_breakpoint(project_path: str, file: str, line: int) -> dict:
    """按 file+line 删除 IDEA 断点（同样先备份）。"""
    ws = workspace_path(project_path)
    if not ws.is_file():
        return {"ok": False, "error": f"未找到 {_WORKSPACE}"}
    rel = (file or "").replace("\\", "/").lstrip("/")
    try:
        tree = ET.parse(str(ws))
    except (ET.ParseError, OSError) as e:
        return {"ok": False, "error": f"无法解析 {_WORKSPACE}: {e}"}

    root = tree.getroot()
    removed = 0
    for comp in root.findall("component"):
        if comp.get("name") != "XDebuggerManager":
            continue
        for bp_mgr in comp.findall("breakpoint-manager"):
            for bps in bp_mgr.findall("breakpoints"):
                for bp in list(bps.findall("line-breakpoint")):
                    if _norm_rel(project_path, bp.get("url") or "") == rel and \
                            int(bp.get("line") or 0) == int(line):
                        bps.remove(bp)
                        removed += 1
    if removed == 0:
        return {"ok": True, "removed": 0, "message": "IDEA 中未找到该断点"}
    backup = _backup(ws)
    try:
        tree.write(str(ws), encoding="UTF-8", xml_declaration=True)
    except OSError as e:
        return {"ok": False, "error": f"写入失败: {e}"}
    return {"ok": True, "removed": removed, "backup": backup,
            "warning": "已从 IDEA 配置移除，需重启 IDEA 后生效"}


# ───────────────────────────────────────────────────────────────
# 3) IDEA 调试会话探测（JDWP 占用 → 只能走 Arthas）
# ───────────────────────────────────────────────────────────────

def is_idea_running() -> bool:
    """粗判 IDEA 是否在本机运行（只看进程名，不关心具体工程）。"""
    if not _IS_WIN:
        try:
            out = subprocess.run(["pgrep", "-f", "idea"], capture_output=True,
                                 text=True, timeout=5).stdout
            return bool(out.strip())
        except Exception:  # noqa: BLE001
            return False
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq idea64.exe", "/NH"],
                             capture_output=True, text=True, timeout=8,
                             encoding="utf-8", errors="replace").stdout
        return "idea64.exe" in (out or "")
    except Exception:  # noqa: BLE001
        return False


_IS_WIN = __import__("sys").platform == "win32"

# JDWP 参数形如：
#   address=*:5005 / address=5005 / address=127.0.0.1:6006
# 端口总在**最后一个冒号之后**——正则必须锚定行尾或空白，否则 `127.0.0.1:6006`
# 会被错配成 `127`（这正是本用例守住的坑）。
_JDWP_RE = re.compile(r"-agentlib:jdwp=[^\s]*?address=(?:[^:\s]*:)?(?P<port>\d+)(?=\s|$)")


def parse_jdwp_processes(jps_output: str) -> list[dict]:
    """从 `jps -lvm` 输出里筛出开启了 JDWP 的 JVM（纯函数，便于单测）。

    IDEA 调试中的 JVM 一定带 `-agentlib:jdwp=...`（IDEA 用它建立连接），
    据此可判断"当前有 IDEA 调试会话"，并取出 JDWP 端口。
    """
    out: list[dict] = []
    for ln in (jps_output or "").splitlines():
        parts = ln.split()
        if len(parts) < 2:
            continue
        pid, main = parts[0], parts[1]
        if not pid.isdigit():
            continue
        m = _JDWP_RE.search(ln)
        if not m:
            continue
        out.append({"pid": int(pid), "main_class": main,
                    "jdwp_port": int(m.group("port")), "debugging": True})
    return out


def detect_debug_session(project_path: str) -> dict:
    """探测本机 JDWP 调试会话（含 IDEA 调试中的 JVM）。"""
    jps_out = _run_jps()
    sessions = parse_jdwp_processes(jps_out)
    idea_running = is_idea_running()
    return {
        "ok": True,
        "idea_running": idea_running,
        "sessions": sessions,
        "note": ("检测到 JDWP 调试会话：该通道通常被 IDEA 独占，本软件的真断点连不上，"
                 "请用 Arthas 观测（可与 IDEA 调试并存）") if sessions else
                "未检测到 JDWP 调试会话",
    }


def _run_jps() -> str:
    """跑 jps -lvm（找不到 jps 时返回空串，不报错）。

    java_home 解析复用 arthas_service.resolve_java_home（返回 (Path|None, 来源)）。
    """
    home = None
    try:
        from app.services.arthas_service import resolve_java_home
        home, _src = resolve_java_home(None)
    except Exception:  # noqa: BLE001
        home = None
    exe = "jps"
    try:
        if home is not None:
            cand = Path(home) / "bin" / ("jps.exe" if _IS_WIN else "jps")
            if cand.is_file():
                exe = str(cand)
        return subprocess.run([exe, "-lvm"], capture_output=True, text=True, timeout=8,
                              encoding="utf-8", errors="replace").stdout
    except Exception:  # noqa: BLE001
        return ""


# ───────────────────────────────────────────────────────────────
# 4) 源码定位：文件:行 → class#method（供建立 Arthas 观测）
# ───────────────────────────────────────────────────────────────

_METHOD_RE = re.compile(
    r"^\s*(?:public|private|protected|static|final|synchronized|native|abstract|"
    r"default|strictfp|\s)*[\w<>\[\],.\s?]+\s+(?P<name>[\w$]+)\s*\(")


def method_at_line(project_path: str, file: str, line: int) -> dict:
    """由 Java 源文件 + 行号推导 `class#method`（用于 Arthas 方法级观测）。

    实现是轻量正则自该行向上找最近的方法签名——足以覆盖绝大多数常规 Java 代码；
    失败时返回 ok=False 并提示手填，不阻塞流程。
    """
    rel = (file or "").replace("\\", "/").lstrip("/")
    p = Path(project_path) / rel
    if not p.is_file():
        return {"ok": False, "error": f"源文件不存在：{rel}"}
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        return {"ok": False, "error": str(e)}
    if line < 1 or line > len(lines):
        return {"ok": False, "error": f"行号越界（文件共 {len(lines)} 行）"}

    # 包名
    pkg = ""
    for ln in lines[:60]:
        m = re.match(r"^\s*package\s+([\w.]+)\s*;", ln)
        if m:
            pkg = m.group(1)
            break
    # 类名
    cls = ""
    for ln in lines[:200]:
        m = re.match(r"^\s*(?:public|final|abstract|\s)*(?:class|interface|enum|record)\s+([\w$]+)", ln)
        if m:
            cls = m.group(1)
            break
    if not cls:
        cls = p.stem
    # 自该行向上找方法签名
    method = ""
    for i in range(min(line, len(lines)) - 1, -1, -1):
        m = _METHOD_RE.match(lines[i])
        if m:
            name = m.group("name")
            if name in ("if", "for", "while", "switch", "catch", "return", "new", "synchronized"):
                continue
            method = name
            break

    fqcn = f"{pkg}.{cls}" if pkg else cls
    return {
        "ok": bool(method),
        "class": fqcn,
        "method": method or None,
        "target": f"{fqcn}#{method}" if method else None,
        "line": int(line),
        "error": None if method else "未能从该行向上识别方法签名，请手动指定 class#method",
    }
