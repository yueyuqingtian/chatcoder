"""符号索引服务回归测试（重开索引后搜索失效 / 性能优化 / 无文件数上限）。

背景（用户反馈 2026-09）：
1. 部分扫描（上限截断/遍历报错）时"删除差集"把仍存在文件的符号误删
   → 重新开启索引后 symbol_search 搜不到函数；
2. 每轮增量（含自动巡检）都把全部文件读一遍算 sha1 → 性能差；
3. 扫描中关闭索引 → worker 被 terminate（Windows 退出码 1）→
   manager 把状态误报成 error "worker exited with code 1"；
4. 文件数硬上限（8000/20000）导致大项目永远只索引一部分 → 已改为默认不限制；
5. 通用正则灾难性回溯：SQL 迁移文件（含 '(' 但无 ';' 收尾）让 java/c# 方法正则
   指数级回溯，worker 空转 CPU 却永远停在 21%（py-spy 抓栈定位到 _extract_generic）。
"""
import time
from pathlib import Path

from app.services import symbol_index_service as sis


def _mk_ws(tmp_path: Path, n: int = 6) -> Path:
    ws = tmp_path / "proj"
    ws.mkdir()
    for i in range(n):
        (ws / f"mod{i}.py").write_text(f"def func_{i}():\n    return {i}\n", encoding="utf-8")
    return ws


def test_partial_scan_does_not_delete_existing_files(tmp_path, monkeypatch):
    """部分扫描（未覆盖全部文件）时，差集中"仍存在"的文件绝不能被误删。

    旧逻辑：known - current 直接删 → 扫描未覆盖到的文件（上限截断、
    遍历报错、窗口偏移）符号从索引里消失 → 重新开启索引后搜索不到。
    这里直接把收集结果裁一半来模拟"未扫全"。
    """
    ws = _mk_ws(tmp_path, n=6)
    full = sis.index_workspace(ws)
    assert full["files_scanned"] == 6
    assert sis.search_symbols(ws, "func_5")

    original = sis._collect_source_files

    def _half(workspace, cancel_file=None, on_progress=None):
        return original(workspace, cancel_file=cancel_file, on_progress=on_progress)[:3]

    monkeypatch.setattr(sis, "_collect_source_files", _half)
    partial = sis.index_workspace(ws)
    assert partial["files_scanned"] == 3  # 本轮只扫到 3 个
    assert partial["removed_files"] == 0  # 其余文件仍在磁盘 → 不得清删
    for i in range(6):
        assert sis.search_symbols(ws, f"func_{i}"), f"func_{i} 被误删"


def test_no_file_count_cap_by_default(tmp_path):
    """默认不设文件数上限：超过旧上限（数万级仓库）的量级也能全量建立。"""
    ws = _mk_ws(tmp_path, n=150)
    stats = sis.index_workspace(ws)
    assert sis._MAX_FILES == 0  # 默认不限制
    assert stats["files_total"] == 150
    assert stats["files_updated"] == 150
    assert stats["symbols"] == 150
    assert sis.search_symbols(ws, "func_149")


def test_deleted_file_symbols_are_removed(tmp_path):
    """真被删除的文件仍要正常清理（误删守卫不能放过真删除）。"""
    ws = _mk_ws(tmp_path, n=3)
    sis.index_workspace(ws)
    (ws / "mod1.py").unlink()
    stats = sis.index_workspace(ws)
    assert stats["removed_files"] == 1
    assert sis.search_symbols(ws, "func_1") == []
    assert len(sis.search_symbols(ws, "func_0")) == 1


def test_mtime_unchanged_skips_content_read(tmp_path, monkeypatch):
    """mtime+size 未变：跳过重解析且不读文件内容（零 IO 短路）。"""
    ws = _mk_ws(tmp_path, n=2)
    assert sis.index_workspace(ws)["files_updated"] == 2

    def _boom(self, *args, **kwargs):
        raise AssertionError("unchanged file must not be read")

    monkeypatch.setattr(Path, "read_bytes", _boom)
    stats = sis.index_workspace(ws)
    assert stats["files_scanned"] == 2
    assert stats["files_updated"] == 0


def test_generic_extraction_is_linear_on_poison_input(tmp_path):
    """回归：SQL 类输入不得触发正则灾难性回溯（曾让 worker 卡死 >600s）。

    旧 java/c# 方法正则含 (?:kw|\\s)* 与跨行的 [^;]*，对含大量 '(' 且无 ';'
    收尾的 SQL 文本指数级回溯（py-spy 抓栈：_extract_generic → finditer）。
    修复后用 [ \\t] 行内锚定 + 参数段排除换行并限长，必须秒级完成。
    """
    # 构造病态输入：大量空白/标识符 + 括号，且无 ';' 收尾（模拟 SQL 迁移文件）
    poison = ("create table t (\n" + "  col_a int, col_b varchar(20),\n" * 300
              + "  ); -- no semicolon terminator inside parens\n") * 20
    t0 = time.perf_counter()
    sis._extract_generic(poison, "migration.sql")
    elapsed = time.perf_counter() - t0
    assert elapsed < 2.0, f"病态输入提取耗时 {elapsed:.2f}s，疑似正则回溯"


def test_generic_extraction_keeps_all_languages(tmp_path):
    """回归：收紧正则后各语言提取能力不回退。"""
    cases = {
        "a.ts": ("export async function loadUser(id) {\n"
                 "export const fetchData = async (url) =>\n"
                 "export interface Foo { a: number }\n"
                 "export type Bar = string;\nclass Widget {\n", "loadUser", "Foo", "Bar", "fetchData", "Widget"),
        "a.go": ("func (s *Svc) Handle(req) error {\nfunc NewSvc() *Svc {\n", "Handle", "NewSvc"),
        "a.rs": ("pub async fn run() {\npub struct Cfg {\npub enum Mode {\n", "run", "Cfg", "Mode"),
        "a.java": ("public class UserService {\n  public String findById(Long id) {\n"
                   "  private static void log(String m) {\n", "UserService", "findById", "log"),
        "a.cs": ("internal class Repo {\n  public async Task<List<Item>> ListAsync(int page) {\n",
                 "Repo", "ListAsync"),
        "a.php": ("public function index() {\nprivate static function helper() {\n", "index", "helper"),
        "a.rb": ("def compute(x)\ndef valid?\n", "compute", "valid?"),
    }
    for fname, (src, *expected) in cases.items():
        names = {s["name"] for s in sis._extract_generic(src, fname)}
        for want in expected:
            assert want in names, f"{fname} 丢失符号 {want}，实际 {sorted(names)}"


def test_poison_sql_file_indexes_quickly(tmp_path):
    """端到端：含病态 SQL 的工作区必须能索引完成（而非卡死）。"""
    ws = tmp_path / "proj"
    ws.mkdir()
    (ws / "good.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    (ws / "migration.sql").write_text(
        ("create table t (\n" + "  col_a int, col_b varchar(20),\n" * 200 + "  );\n") * 15,
        encoding="utf-8",
    )
    t0 = time.perf_counter()
    stats = sis.index_workspace(ws)
    elapsed = time.perf_counter() - t0
    assert "error" not in stats
    assert elapsed < 5.0, f"含病态 SQL 的工作区索引耗时 {elapsed:.2f}s"
    assert sis.search_symbols(ws, "alpha"), "正常文件的符号仍应建立"


def test_mtime_touch_with_same_content_keeps_symbols(tmp_path):
    """touch（mtime 变、内容不变）：只刷新登记，不重解析、不丢符号。"""
    import os

    ws = _mk_ws(tmp_path, n=2)
    sis.index_workspace(ws)
    for p in ws.glob("mod*.py"):
        st = p.stat()
        os.utime(p, ns=(st.st_atime_ns + 1_000_000, st.st_mtime_ns + 1_000_000))
    stats = sis.index_workspace(ws)
    assert stats["files_updated"] == 0
    assert len(sis.search_symbols(ws, "func_0")) == 1
