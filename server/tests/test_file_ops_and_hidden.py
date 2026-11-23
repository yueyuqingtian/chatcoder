"""文件树隐藏开关统一与安全文件增删改接口测试（plan-771-3177 Group C）。"""
from pathlib import Path
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.gateway.routers import projects, upload
from app.persistence.database import run_write_locked
from app.persistence.models.message import Session
from app.persistence.models.project import Project
from app.services import preview_service
from app.services.preview_service import initial_tree, list_children


@pytest.fixture
async def project_env(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (root / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "pkg").mkdir()
    (root / "node_modules" / "pkg" / "index.js").write_text("module.exports = {}\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / ".hidden.ts").write_text("export const h = 1;\n", encoding="utf-8")
    (root / "src" / "main.ts").write_text("export const m = 1;\n", encoding="utf-8")

    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "wt_only.txt").write_text("worktree", encoding="utf-8")
    (worktree / ".wt_hidden.txt").write_text("hidden", encoding="utf-8")

    other_proj = tmp_path / "other"
    other_proj.mkdir()

    def seed(db):
        p = Project(name="repo", path=str(root))
        p2 = Project(name="other", path=str(other_proj))
        db.add_all([p, p2])
        db.flush()
        s_main = Session(project_id=p.id, title="主会话")
        s_wt = Session(project_id=p.id, title="工作树会话", worktree_path=str(worktree))
        s_other = Session(project_id=p2.id, title="其他会话")
        db.add_all([s_main, s_wt, s_other])
        db.flush()
        ids = (p.id, s_main.id, s_wt.id, s_other.id)
        db.commit()
        return ids

    project_id, main_session_id, wt_session_id, other_session_id = await run_write_locked(seed)

    app = FastAPI()
    app.include_router(projects.router, prefix="/api")
    app.include_router(upload.router, prefix="/api")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, root, worktree, project_id, main_session_id, wt_session_id, other_session_id


def test_list_children_and_initial_tree_consistent_hidden_policy(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".env").write_text("A=1", encoding="utf-8")
    (ws / ".git").mkdir()
    (ws / ".git" / "HEAD").write_text("ref: refs/heads/main", encoding="utf-8")
    (ws / "node_modules").mkdir()
    (ws / "node_modules" / "dep.js").write_text("", encoding="utf-8")
    (ws / "src").mkdir()
    (ws / "src" / ".secret").write_text("x", encoding="utf-8")
    (ws / "src" / "app.ts").write_text("x", encoding="utf-8")

    # show_hidden=False：list_children 与 initial_tree 一致，隐藏点文件与 node_modules/.git
    children_default = list_children(ws)
    tree_default = initial_tree(ws, depth=3)
    assert [n["name"] for n in children_default] == ["src"]
    assert [n["name"] for n in tree_default] == ["src"]
    assert [n["name"] for n in tree_default[0]["children"]] == ["app.ts"]

    # show_hidden=True：同时显示点文件与忽略目录（忽略目录本身保留且不无限下钻）
    children_all = list_children(ws, show_hidden=True)
    tree_all = initial_tree(ws, depth=3, show_hidden=True)
    assert {n["name"] for n in children_all} == {".git", "node_modules", "src", ".env"}
    assert {n["name"] for n in tree_all} == {".git", "node_modules", "src", ".env"}
    src_node = next(n for n in tree_all if n["name"] == "src")
    assert {n["name"] for n in src_node["children"]} == {".secret", "app.ts"}


def test_preview_tree_budget_keeps_complete_sibling_lists(tmp_path, monkeypatch):
    monkeypatch.setattr(preview_service, "INITIAL_TREE_NODE_BUDGET", 5)
    crowded = tmp_path / "ai"
    crowded.mkdir()
    for index in range(8):
        (crowded / f"note-{index}.md").touch()
    source = tmp_path / "src"
    source.mkdir()
    (source / "main.ts").touch()
    (tmp_path / "package.json").touch()

    tree = initial_tree(tmp_path, depth=8)
    assert [node["name"] for node in tree] == ["ai", "src", "package.json"]
    assert tree[0]["children"] is None
    assert len(list_children(crowded)) == 8
    assert [node["name"] for node in tree[1]["children"]] == ["main.ts"]

    monkeypatch.setattr(preview_service, "INITIAL_TREE_NODE_BUDGET", 1)
    assert initial_tree(tmp_path, depth=8) == list_children(tmp_path)


@pytest.mark.asyncio
async def test_preview_children_and_project_tree_search_show_hidden(project_env):
    client, root, worktree, pid, main_sid, wt_sid, _ = project_env

    # preview/children（主会话）默认隐藏，depth=1 与 depth=3 口径一致
    r = await client.get("/api/preview/children", params={
        "source": "project", "session_id": main_sid, "project_id": pid, "path": str(root), "depth": 1,
    })
    assert r.status_code == 200
    assert [n["name"] for n in r.json()["children"]] == ["src"]

    r_deep = await client.get("/api/preview/children", params={
        "source": "project", "session_id": main_sid, "project_id": pid, "path": str(root), "depth": 3,
    })
    assert r_deep.status_code == 200
    assert [n["name"] for n in r_deep.json()["children"]] == ["src"]
    assert [n["name"] for n in r_deep.json()["children"][0]["children"]] == ["main.ts"]

    r_show = await client.get("/api/preview/children", params={
        "source": "project", "session_id": main_sid, "project_id": pid, "path": str(root), "depth": 3, "show_hidden": True,
    })
    assert r_show.status_code == 200
    assert {n["name"] for n in r_show.json()["children"]} == {".git", "node_modules", "src", ".env"}

    # worktree 会话以 worktree_path 为根（传主仓库路径应 403，传 "" 返回 worktree 下可见项）
    r_wt = await client.get("/api/preview/children", params={
        "source": "project", "session_id": wt_sid, "project_id": pid, "path": "", "depth": 1,
    })
    assert r_wt.status_code == 200
    assert [n["name"] for n in r_wt.json()["children"]] == ["wt_only.txt"]
    r_wt_show = await client.get("/api/preview/children", params={
        "source": "project", "session_id": wt_sid, "project_id": pid, "path": "", "depth": 1, "show_hidden": True,
    })
    assert r_wt_show.status_code == 200
    assert {n["name"] for n in r_wt_show.json()["children"]} == {".wt_hidden.txt", "wt_only.txt"}

    # project tree 默认隐藏 & show_hidden=true
    t_def = await client.get(f"/api/projects/{pid}/tree", params={"depth": 4})
    assert t_def.status_code == 200
    assert [n["name"] for n in t_def.json()["children"]] == ["src"]

    t_all = await client.get(f"/api/projects/{pid}/tree", params={"depth": 4, "show_hidden": True})
    assert t_all.status_code == 200
    assert {n["name"] for n in t_all.json()["children"]} == {".git", "node_modules", "src", ".env"}

    # files/search 默认不包含 .env / .hidden.ts，开启 show_hidden=true 可搜出
    s_def = await client.get(f"/api/projects/{pid}/files/search", params={"q": "hidden"})
    assert s_def.status_code == 200 and s_def.json() == []
    s_all = await client.get(f"/api/projects/{pid}/files/search", params={"q": "hidden", "show_hidden": True})
    assert s_all.status_code == 200 and s_all.json() == ["src/.hidden.ts"]

    (worktree / "document.txt").write_text("唯一匹配内容", encoding="utf-16")
    (worktree / "document.md").write_text("唯一匹配内容", encoding="utf-8")
    found = await client.get(f"/api/projects/{pid}/files/search", params={
        "q": "唯一匹配", "mode": "content", "extension": ".txt", "session_id": wt_sid,
    })
    assert found.status_code == 200 and found.json() == ["document.txt"]


@pytest.mark.asyncio
async def test_file_ops_create_rename_duplicate_delete(project_env):
    client, root, worktree, pid, _, wt_sid, other_sid = project_env

    # 1. 创建多级文件 & 409 冲突
    r = await client.post(f"/api/projects/{pid}/files/create", json={
        "path": "src/nested/hello.ts", "type": "file", "content": "console.log(1);",
    })
    assert r.status_code == 200
    assert (root / "src/nested/hello.ts").read_text(encoding="utf-8") == "console.log(1);"
    r_dup = await client.post(f"/api/projects/{pid}/files/create", json={
        "path": "src/nested/hello.ts", "type": "file",
    })
    assert r_dup.status_code == 409

    # 创建目录
    r_dir = await client.post(f"/api/projects/{pid}/files/create", json={
        "path": "docs/api", "type": "dir",
    })
    assert r_dir.status_code == 200 and (root / "docs/api").is_dir()

    # 2. 重命名 & 冲突检测
    r_ren = await client.post(f"/api/projects/{pid}/files/rename", json={
        "src": "src/nested/hello.ts", "dst": "src/nested/world.ts",
    })
    assert r_ren.status_code == 200
    assert r_ren.json()["relative_path"] == "src/nested/world.ts"
    assert not (root / "src/nested/hello.ts").exists()
    assert (root / "src/nested/world.ts").is_file()

    r_ren_conflict = await client.post(f"/api/projects/{pid}/files/rename", json={
        "src": "src/nested/world.ts", "dst": "src/main.ts",
    })
    assert r_ren_conflict.status_code == 409

    # 3. 复制副本（自动命名 + 二次副本）
    r_copy1 = await client.post(f"/api/projects/{pid}/files/duplicate", json={
        "path": "src/nested/world.ts",
    })
    assert r_copy1.status_code == 200
    assert r_copy1.json()["name"] == "world_copy.ts"
    r_copy2 = await client.post(f"/api/projects/{pid}/files/duplicate", json={
        "path": "src/nested/world.ts",
    })
    assert r_copy2.status_code == 200
    assert r_copy2.json()["name"] == "world_copy_2.ts"

    # 4. 删除：非空目录未传 recursive 报错，传入 recursive=true 成功
    r_del_dir_fail = await client.post(f"/api/projects/{pid}/files/delete", json={
        "path": "src/nested", "permanent": True,
    })
    assert r_del_dir_fail.status_code == 400
    r_del_dir_ok = await client.post(f"/api/projects/{pid}/files/delete", json={
        "path": "src/nested", "recursive": True, "permanent": True,
    })
    assert r_del_dir_ok.status_code == 200
    assert not (root / "src/nested").exists()

    # 5. session_id 路由到 worktree_path，且拒绝跨项目 session
    r_wt_create = await client.post(f"/api/projects/{pid}/files/create", json={
        "path": "wt_new.txt", "type": "file", "content": "hi", "session_id": wt_sid,
    })
    assert r_wt_create.status_code == 200
    assert (worktree / "wt_new.txt").is_file()
    assert not (root / "wt_new.txt").exists()

    r_cross_sess = await client.post(f"/api/projects/{pid}/files/create", json={
        "path": "x.txt", "type": "file", "session_id": other_sid,
    })
    assert r_cross_sess.status_code == 403


@pytest.mark.asyncio
async def test_file_ops_security_boundaries(project_env):
    client, _, _, pid, _, _, _ = project_env

    # 路径穿越、空路径、根目录、.git 内部操作全部拒绝
    for bad in ("../outside.txt", "a/../../outside.txt", ".git/hooks/pre-commit", ".git/config"):
        r = await client.post(f"/api/projects/{pid}/files/create", json={
            "path": bad, "type": "file",
        })
        assert r.status_code == 403

    r_empty = await client.post(f"/api/projects/{pid}/files/create", json={
        "path": "   ", "type": "file",
    })
    assert r_empty.status_code == 400

    r_root_del = await client.post(f"/api/projects/{pid}/files/delete", json={
        "path": ".", "recursive": True, "permanent": True,
    })
    assert r_root_del.status_code == 403

    r_git_del = await client.post(f"/api/projects/{pid}/files/delete", json={
        "path": ".git", "recursive": True, "permanent": True,
    })
    assert r_git_del.status_code == 403
