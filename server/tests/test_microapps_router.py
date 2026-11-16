"""微应用库管理路由（plan-693-2868 §6.5 / §7 P4 第 1 条）。

覆盖 list / detail / validate / remove / import 五个端点。

不触碰真实 HOME：库根一律由 `CHATCODER_MICROAPP_ROOT` 指向 tmp_path（§6.1），否则
「空库」用例会读到开发者本机的 `~/.chatcoder/microapps`，测试结果随机器而变。

测试用最小 FastAPI 应用挂载路由（不加 main.py 的全局异常处理器），因此断言只看状态码；
错误文案与结构化 issues 的格式由路由自身的 `_ERROR_STATUS` 决定。
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.gateway.routers import microapps as microapps_router

APP_ID = "demo-app"
VERSION = "1.0.0"


@pytest.fixture(autouse=True)
def lib_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """库根固定到临时目录（目录本身先不建：list 用例要验证「库不存在」）。"""
    root = tmp_path / "microapps"
    monkeypatch.setenv("CHATCODER_MICROAPP_ROOT", str(root))
    return root


@pytest.fixture
def client(lib_root: Path) -> TestClient:
    app = FastAPI()
    app.include_router(microapps_router.router, prefix="/api")
    return TestClient(app)


def _valid_manifest(app_id: str = APP_ID, version: str = VERSION) -> dict:
    return {
        "appId": app_id,
        "name": "示例画布",
        "version": version,
        "description": "画流程图的最小示例",
        "entry": "app/index.html",
        "methods": [
            {"name": "drawRect", "description": "画一个矩形"},
            {"name": "readCanvas", "description": "读取当前画布"},
        ],
        "permissions": ["clipboard"],
        "runtime": {"vendors": [{"name": "antd", "version": "5.20.0"}]},
    }


def _write_app(
    lib: Path,
    *,
    app_id: str = APP_ID,
    version: str = VERSION,
    manifest: dict | None = None,
    origin: dict | None = None,
) -> Path:
    """按 §6.1 的 `<root>/<appId>/<version>/manifest.json` 落一个最小应用。"""
    version_dir = lib / app_id / version
    (version_dir / "app").mkdir(parents=True)
    (version_dir / "app" / "index.html").write_text(
        "<!doctype html><meta charset='utf-8'><title>demo</title>", encoding="utf-8",
    )
    payload = _valid_manifest(app_id, version) if manifest is None else manifest
    (version_dir / "manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8",
    )
    if origin is not None:
        (version_dir / ".origin.json").write_text(
            json.dumps(origin, ensure_ascii=False), encoding="utf-8",
        )
    return version_dir


def _zip_bytes(
    manifest: dict | None = None, *, wrap: str = "", extra: dict[str, bytes] | None = None,
) -> bytes:
    """构造导入用 zip；`wrap` 非空时把内容套一层目录（验证 `_find_root` 的容错）。"""
    payload = _valid_manifest() if manifest is None else manifest
    prefix = f"{wrap}/" if wrap else ""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(f"{prefix}manifest.json", json.dumps(payload, ensure_ascii=False))
        zf.writestr(f"{prefix}app/index.html", "<!doctype html><title>demo</title>")
        for name, data in (extra or {}).items():
            zf.writestr(f"{prefix}{name}", data)
    return buffer.getvalue()


def _import(client: TestClient, payload: bytes, *, filename: str = "demo.ccapp"):
    return client.post(
        "/api/microapps/import",
        files={"file": (filename, payload, "application/zip")},
    )


# ───────────────────────── list ─────────────────────────


def test_list_missing_library_is_empty_ok(client: TestClient, lib_root: Path):
    """库不存在 = 没有应用，不是错误（首次使用必然如此，§6.5）。"""
    assert not lib_root.exists()
    res = client.get("/api/microapps")
    assert res.status_code == 200
    assert res.json()["apps"] == []
    assert res.json()["count"] == 0


def test_list_reports_versions_and_origin(client: TestClient, lib_root: Path):
    _write_app(lib_root, version="1.0.0")
    _write_app(lib_root, version="1.2.0", origin={"source": "community", "repo": "o/r"})
    body = client.get("/api/microapps").json()
    assert body["count"] == 1
    app = body["apps"][0]
    assert app["appId"] == APP_ID
    # 同一 appId 只出一条，指向最高版本（与 _scan_installs 同口径）
    assert app["version"] == "1.2.0"
    assert app["installedVersions"] == ["1.0.0", "1.2.0"]
    assert app["name"] == "示例画布"
    assert app["methods"] == ["drawRect", "readCanvas"]
    assert app["perms"] == ["clipboard"]
    assert app["vendors"] == [{"name": "antd", "version": "5.20.0"}]
    assert app["sizeBytes"] > 0
    # origin 取当前版本（最高版本）的来源记录
    assert app["origin"]["source"] == "community"


def test_list_origin_defaults_to_local(client: TestClient, lib_root: Path):
    _write_app(lib_root)
    app = client.get("/api/microapps").json()["apps"][0]
    assert app["origin"] == {"source": "local"}


def test_list_filters_by_query(client: TestClient, lib_root: Path):
    _write_app(lib_root)
    other = _valid_manifest("other-app", VERSION)
    other.update({"name": "管线表单", "description": "填表用"})
    _write_app(lib_root, app_id="other-app", manifest=other)

    assert len(client.get("/api/microapps").json()["apps"]) == 2
    # name 命中
    hit = client.get("/api/microapps", params={"q": "画布"}).json()["apps"]
    assert [item["appId"] for item in hit] == [APP_ID]
    # appId 命中，且大小写不敏感
    hit = client.get("/api/microapps", params={"q": "OTHER"}).json()["apps"]
    assert [item["appId"] for item in hit] == ["other-app"]
    # description 命中
    hit = client.get("/api/microapps", params={"q": "填表"}).json()["apps"]
    assert [item["appId"] for item in hit] == ["other-app"]
    # 无命中 → 空列表仍是 200
    empty = client.get("/api/microapps", params={"q": "不存在的东西"})
    assert empty.status_code == 200 and empty.json()["apps"] == []


def test_list_skips_dot_directories(client: TestClient, lib_root: Path):
    """`.vendor` / `.import-tmp` 之类点开头目录不是应用（§6.1 目录图）。"""
    _write_app(lib_root)
    (lib_root / ".vendor" / "antd").mkdir(parents=True)
    assert [item["appId"] for item in client.get("/api/microapps").json()["apps"]] == [APP_ID]


# ───────────────────────── detail ─────────────────────────


def test_detail_returns_manifest_text_and_all_versions(client: TestClient, lib_root: Path):
    _write_app(lib_root, version="1.0.0")
    _write_app(lib_root, version="1.1.0", origin={"source": "import", "importedAt": "2026-01-01"})
    res = client.get(f"/api/microapps/{APP_ID}")
    assert res.status_code == 200
    body = res.json()
    assert body["appId"] == APP_ID
    assert body["version"] == "1.1.0"
    assert body["installedVersions"] == ["1.0.0", "1.1.0"]
    assert [item["version"] for item in body["versions"]] == ["1.0.0", "1.1.0"]
    assert body["versions"][0]["origin"] == {"source": "local"}
    assert body["versions"][1]["origin"]["source"] == "import"
    assert body["manifest"]["entry"] == "app/index.html"
    # manifest 原文要能展示（详情页对照）
    assert json.loads(body["manifestText"])["appId"] == APP_ID


def test_detail_unknown_app_is_404(client: TestClient, lib_root: Path):
    lib_root.mkdir(parents=True)
    res = client.get("/api/microapps/no-such-app")
    assert res.status_code == 404


@pytest.mark.parametrize("bad_app_id", ["A", "Demo-App", "demo app", "-demo", "d" * 64])
def test_invalid_app_id_is_4xx(client: TestClient, lib_root: Path, bad_app_id: str):
    _write_app(lib_root)
    for method, url in (
        ("get", f"/api/microapps/{bad_app_id}"),
        ("post", f"/api/microapps/{bad_app_id}/validate"),
        ("delete", f"/api/microapps/{bad_app_id}/{VERSION}"),
    ):
        res = getattr(client, method)(url)
        assert res.status_code == 400, f"{method.upper()} {url} → {res.status_code}"


# ───────────────────────── validate ─────────────────────────


def test_validate_ok_for_well_formed_app(client: TestClient, lib_root: Path):
    _write_app(lib_root)
    res = client.post(f"/api/microapps/{APP_ID}/validate")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["issues"] == []
    assert body["version"] == VERSION


def test_validate_reports_every_issue(client: TestClient, lib_root: Path):
    """校验不通过仍是 200 + ok:false + issues[]：界面要能逐条列出，而不是走错误分支。"""
    broken = {
        "appId": "other-app",          # 与目录名不一致
        "name": "",                    # 空名
        "version": "v1",               # 非法版本号
        "description": 123,            # 类型错
        "entry": "app/missing.html",   # 文件不存在
        "methods": [{"name": "draw rect"}],
        "permissions": "clipboard",    # 不是数组
        "runtime": {"vendors": [{"name": "antd"}]},
    }
    _write_app(lib_root, manifest=broken)
    res = client.post(f"/api/microapps/{APP_ID}/validate")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is False
    text = "；".join(body["issues"])
    for expected in (
        "与安装目录名", "manifest.name", "不是合法版本号", "指向的文件不存在", "permissions",
    ):
        assert expected in text, f"缺少问题项: {expected}；实际：{text}"


def test_validate_uninstalled_app_is_404(client: TestClient, lib_root: Path):
    lib_root.mkdir(parents=True)
    assert client.post("/api/microapps/no-such-app/validate").status_code == 404


# ───────────────────────── delete ─────────────────────────


def test_delete_single_version_keeps_others(client: TestClient, lib_root: Path):
    _write_app(lib_root, version="1.0.0")
    _write_app(lib_root, version="1.1.0")
    res = client.delete(f"/api/microapps/{APP_ID}/1.0.0")
    assert res.status_code == 200
    body = res.json()
    assert body["remainingVersions"] == ["1.1.0"]
    assert body["appRemoved"] is False
    assert not (lib_root / APP_ID / "1.0.0").exists()
    # 列表里版本减少
    listed = client.get("/api/microapps").json()["apps"][0]
    assert listed["installedVersions"] == ["1.1.0"]
    assert listed["version"] == "1.1.0"


def test_delete_all_removes_app_id(client: TestClient, lib_root: Path):
    _write_app(lib_root, version="1.0.0")
    _write_app(lib_root, version="1.1.0")
    res = client.delete(f"/api/microapps/{APP_ID}/all")
    assert res.status_code == 200
    body = res.json()
    assert body["appRemoved"] is True
    assert body["remainingVersions"] == []
    assert not (lib_root / APP_ID).exists()
    assert client.get("/api/microapps").json()["apps"] == []
    assert client.get(f"/api/microapps/{APP_ID}").status_code == 404


def test_delete_last_version_cleans_empty_app_dir(client: TestClient, lib_root: Path):
    """版本删完顺手清掉空壳 appId 目录：库里留下空目录只会像「装了但坏了」。"""
    _write_app(lib_root)
    body = client.delete(f"/api/microapps/{APP_ID}/{VERSION}").json()
    assert body["appRemoved"] is True
    assert not (lib_root / APP_ID).exists()


def test_delete_missing_version_is_404(client: TestClient, lib_root: Path):
    _write_app(lib_root)
    assert client.delete(f"/api/microapps/{APP_ID}/9.9.9").status_code == 404


def test_delete_bad_version_name_is_4xx(client: TestClient, lib_root: Path):
    _write_app(lib_root)
    res = client.delete(f"/api/microapps/{APP_ID}/v1")
    assert res.status_code == 400


def test_delete_escaping_path_is_rejected(client: TestClient, lib_root: Path):
    """越界删除：HTTP 层可能被客户端/路由规整成 404，服务层必须独立挡下（见下一个用例）。"""
    _write_app(lib_root)
    res = client.delete(f"/api/microapps/{APP_ID}/../../etc")
    assert res.status_code in (400, 404)
    # 库里那份必须还在
    assert (lib_root / APP_ID / VERSION / "manifest.json").is_file()


def test_guarded_target_rejects_escape(lib_root: Path):
    """`_guarded_target` 的二次防护：resolve 后必须仍落在 appId 目录 / 库根之内。"""
    _write_app(lib_root)
    # 正常路径：整应用与单版本
    app_dir, target = microapps_router._guarded_target(lib_root, APP_ID, "all")
    assert target == app_dir == (lib_root / APP_ID).resolve()
    _, version_target = microapps_router._guarded_target(lib_root, APP_ID, VERSION)
    assert version_target == (lib_root / APP_ID / VERSION).resolve()
    # 版本名越出应用目录
    with pytest.raises(microapps_router.MicroAppArgumentInvalid):
        microapps_router._guarded_target(lib_root, APP_ID, "../../sensitive")
    # appId 越出库根（正则之外的第二道）
    with pytest.raises(microapps_router.MicroAppArgumentInvalid):
        microapps_router._guarded_target(lib_root, "..", "all")


# ───────────────────────── import ─────────────────────────


def test_import_zip_lands_in_library(client: TestClient, lib_root: Path):
    payload = _zip_bytes(wrap="demo-app-1.0.0")  # 多套一层：`_find_root` 要能找到内容根
    res = _import(client, payload)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["appId"] == APP_ID
    assert body["version"] == VERSION
    assert body["bytes"] == len(payload)
    assert body["origin"]["source"] == "import"
    assert body["origin"]["importedAt"]
    assert body["enabled"] is False
    # 落库位置与 .origin.json
    version_dir = lib_root / APP_ID / VERSION
    assert (version_dir / "manifest.json").is_file()
    assert (version_dir / "app" / "index.html").is_file()
    origin = json.loads((version_dir / ".origin.json").read_text(encoding="utf-8"))
    assert origin["source"] == "import"
    # 能被 list 看到，且 origin 如实反映导入来源
    app = client.get("/api/microapps").json()["apps"][0]
    assert app["appId"] == APP_ID and app["origin"]["source"] == "import"
    # 导入不留临时目录（点开头目录也不该被当成应用）
    assert not list(lib_root.glob(".import-tmp/*"))


def test_import_same_version_twice_is_409(client: TestClient, lib_root: Path):
    payload = _zip_bytes()
    assert _import(client, payload).status_code == 200
    again = _import(client, payload)
    assert again.status_code == 409
    detail = again.json()["detail"]
    text = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
    assert "已存在" in text and "升版本" in text


def test_import_invalid_manifest_is_422_with_issues(client: TestClient, lib_root: Path):
    bad = _valid_manifest()
    bad["entry"] = "app/missing.html"
    res = _import(client, _zip_bytes(bad))
    assert res.status_code == 422
    detail = res.json()["detail"]
    assert isinstance(detail, dict)
    assert any("指向的文件不存在" in issue for issue in detail["issues"])
    # 校验没过就不能落库
    assert not (lib_root / APP_ID).exists()


def test_import_bad_app_id_in_manifest_is_422(client: TestClient, lib_root: Path):
    bad = _valid_manifest()
    bad["appId"] = "../evil"
    res = _import(client, _zip_bytes(bad))
    assert res.status_code == 422
    assert not (lib_root / "evil").exists()


def test_import_rejects_non_zip(client: TestClient, lib_root: Path):
    res = _import(client, b"this is not a zip")
    assert res.status_code == 422
    assert not (lib_root / APP_ID).exists()


def test_import_rejects_empty_file(client: TestClient, lib_root: Path):
    res = _import(client, b"")
    assert res.status_code == 422


def test_import_rejects_zip_without_manifest(client: TestClient, lib_root: Path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("app/index.html", "<!doctype html>")
    assert _import(client, buffer.getvalue()).status_code == 422


def test_import_rejects_path_traversal_entries(client: TestClient, lib_root: Path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("manifest.json", json.dumps(_valid_manifest()))
        zf.writestr("../escaped.txt", "x")
    res = _import(client, buffer.getvalue())
    assert res.status_code == 422
