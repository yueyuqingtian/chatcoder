"""plan-20261121-build-artifact-filter：打包 / 编译产物不计入本轮变更。

背景：turn 3198 在 test1 执行 electron 打包后，1163 个 win-unpacked 下的文件被登记成
写盘记录，变更清单接口要跑 1171 次行级 diff，把事件循环占满、连带其它请求超时。
本文件固定「产物路径判定」「构建命令判定」「写入侧拦截」「读取侧兜底」四层口径。
"""
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.persistence.database import Base
from app.persistence.models.rollback import RollbackWrite
from app.services import rollback_service as rs


@pytest.fixture
async def db(tmp_path, monkeypatch):
    url = f"sqlite+aiosqlite:///{tmp_path}/artifact.db"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("app.persistence.database.async_session_factory", factory)
    from app.persistence import write_engine
    write_engine.configure(url, foreign_keys=False)
    async with factory() as session:
        yield session
    await engine.dispose()
    write_engine.configure(None)


@pytest.mark.parametrize("path", [
    # 本次事故的实际路径
    "test1/win-unpacked/resources/app.asar",
    "test1/win-unpacked/LICENSE.electron.txt",
    "test1/chatcoder-Setup-0.10.6.exe.blockmap",
    "test1/chatcoder-Setup-0.10.6.exe",
    "test1/latest.yml",
    "test1/.icon-ico/icon.ico",
    "server/vendor/desktop-core/desktop-core.exe",
    "server/vendor/ms-playwright/.links/b4f569d223ce10511e31c1a7887936194acd6093",
    "node_modules/react/index.js",
    # 通用前后端构建目录
    "dist/index.js",
    "client/dist/assets/a.css",
    "build/out.js",
    "output/app.bin",
    ".next/server/x.js",
    # 编译产物目录
    "target/classes/A.class",
    "obj/Debug/x.dll",
    "pkg/name.egg-info/PKG-INFO",
    "dist/pkg-1.0.0-py3-none-any.whl",
    # electron-builder 的 arch 变体与发布目录
    "mac-arm64/App",
    "foo-unpacked/app",
    "release/latest-linux.yml",
])
def test_is_artifact_path_true(path):
    assert rs.is_artifact_path(path)


@pytest.mark.parametrize("path", [
    "package.json",
    "CHANGELOG.md",
    "docker-compose.yml",
    "client/src/main.tsx",
    "server/app/services/rollback_service.py",
    "electron/resources/icon.png",
    "electron/resources/icon.ico",
    "ai/chatcoder-plan-20261121-build-artifact-filter.md",
])
def test_is_artifact_path_false(path):
    assert not rs.is_artifact_path(path)


@pytest.mark.parametrize("cmd", [
    "pwsh -File .\\build-release.ps1",
    "powershell -File D:\\myProject\\chatcoder\\build.ps1",
    "npm run build",
    "npm run build:main",
    "pnpm run build",
    "npx electron-builder --win",
    "vite build",
    "./gradlew build",
    "mvn package -DskipTests",
    "dotnet publish -c Release",
    "pyinstaller -F app.py",
    "cargo build --release",
    "docker build -t x .",
])
def test_is_build_command_true(cmd):
    assert rs.is_build_command(cmd)


@pytest.mark.parametrize("cmd", [
    "npm run test",
    "npm run dev",
    "npm install",
    "git status",
    "python app.py",
    "node scripts/dev.js",
    "mvn test",
])
def test_is_build_command_false(cmd):
    assert not rs.is_build_command(cmd)


@pytest.mark.asyncio
async def test_record_turn_write_skips_artifact(db):
    """写入侧第一道：显式写盘 / 命令扫描 / 后台回填都走 record_turn_write。"""
    await rs.record_turn_write(
        db, session_id=1, turn_id=1, tool="terminal_exec",
        path="test1/win-unpacked/resources/app.asar", before=None, after="payload", op="create",
    )
    rows = (await db.execute(select(RollbackWrite))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_list_turn_changes_drops_artifacts(db, tmp_path):
    """读取侧兜底：历史已经登记进库的产物记录不再出现在变更清单里。"""
    workspace = tmp_path / "build" / "workspace"
    workspace.mkdir(parents=True)
    db.add_all([
        RollbackWrite(session_id=1, turn_id=1, tool="terminal_exec",
                      path="test1/win-unpacked/resources/app.asar", old_content=None,
                      new_content="x" * 10, op="create", binary=False),
        RollbackWrite(session_id=1, turn_id=1, tool="fs_write", path=str(workspace / "a.py"),
                      old_content="v1", new_content="v2", op="modify", binary=False,
                      diff_before="v1", diff_after="v2"),
    ])
    await db.commit()

    changes = await rs.list_turn_changes(db, session_id=1, turn_id=1, workspace=str(workspace))

    assert [c["path"] for c in changes] == ["a.py"]
    assert changes[0]["additions"] == 1
    assert changes[0]["deletions"] == 1


@pytest.mark.asyncio
async def test_binary_change_does_not_read_file_as_text(db, tmp_path, monkeypatch):
    path = "recording.bin"
    (tmp_path / path).write_bytes(b"\x00\n\x01\n")
    db.add(RollbackWrite(session_id=1, turn_id=1, tool="terminal_exec",
                         path=path, op="create", binary=True))
    await db.commit()

    def reject_text_read(*args, **kwargs):
        pytest.fail("二进制变更不应读取磁盘全文")

    monkeypatch.setattr(rs, "_read_file_text", reject_text_read)
    changes = await rs.list_turn_changes(db, session_id=1, turn_id=1, workspace=str(tmp_path))
    assert changes == [{"path": path, "action": "added", "additions": 0, "deletions": 0, "reviewed": False}]
    diff = await rs.get_file_diff(db, session_id=1, turn_id=1, workspace=str(tmp_path), path=path)
    assert diff["action"] == "added"
    assert diff["additions"] == diff["deletions"] == 0


@pytest.mark.asyncio
async def test_record_detected_file_changes_filters_artifacts(db, tmp_path):
    """非写盘工具分支：产物候选在进入磁盘核对前就被剔除。"""
    detected, add, dele = await rs.record_detected_file_changes(
        db, session_id=1, turn_id=1, workspace=str(tmp_path), tool_name="terminal_exec",
        candidates=["test1/win-unpacked/resources/app.asar", "dist/index.js"],
    )
    assert (detected, add, dele) == ([], 0, 0)
