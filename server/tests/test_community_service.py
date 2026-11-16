"""社区读取与安装（plan-693-2868 §3.4 / §6.4 / §7 P5）：索引解析、拉黑、降级、身份、安装。

**不连网**：`_fetch_raw` / `_fetch_api` 与 `market_install._download` 全部被替换，
本机 HOME 也不碰——库根由 `CHATCODER_MICROAPP_ROOT` 指向 tmp_path（§6.1），
用户配置文件由 `CHATCODER_USER_CONFIG` 指向 tmp_path，避免读到开发机上的 community_repo。
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import time
import zipfile
from pathlib import Path

import httpx
import pytest

from app.orchestration.tools.base import ToolContext
from app.orchestration.tools.microapp import MicroAppListTool
from app.services import community_service, market_install
from app.services.community_service import (
    CommunityDisabled,
    CommunityIntegrityError,
    CommunityNetworkError,
    CommunityNotFound,
)

APP_ID = "pipeline-canvas"
VERSION = "1.0.0"
AUTHOR = "usr_7f3c1a2b9d0e"
OTHER_AUTHOR = "usr_1a2b3c4d5e6f"
OTHER_APP = "form-builder"
REPO = "chatcoder-owner/chatcoder-microapps"


@pytest.fixture(autouse=True)
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """库根 / 用户配置 / 社区仓库全部指向临时环境（用例之间互不影响）。"""
    root = tmp_path / "microapps"
    monkeypatch.setenv("CHATCODER_MICROAPP_ROOT", str(root))
    monkeypatch.setenv("CHATCODER_MICROAPP_EXT_ROOT", str(tmp_path / "microapps-ext"))
    monkeypatch.setenv("CHATCODER_USER_CONFIG", str(tmp_path / "user-config.json"))
    monkeypatch.setenv(community_service.COMMUNITY_REPO_ENV, REPO)
    return root


def _entry(
    app_id: str = APP_ID,
    version: str = VERSION,
    *,
    author_id: str = AUTHOR,
    sha256: str = "",
    size: int = 0,
    name: str = "管线画布",
    desc: str = "把管线协议画成时序图",
) -> dict:
    """一条符合 §6.4 / docs/community-template/apps.json 形状的索引条目。"""
    return {
        "appId": app_id,
        "name": name,
        "version": version,
        "authorId": author_id,
        "authorName": "示例作者",
        "desc": desc,
        "size": size,
        "sha256": sha256 or "a" * 64,
        "perms": ["read_workspace"],
        "methods": ["drawTimeline", "highlight"],
        "vendors": [],
        "downloadUrl": (
            f"https://github.com/{REPO}/releases/download/{app_id}-{version}/{app_id}-{version}.ccapp"
        ),
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-01T00:00:00Z",
    }


def _package(app_id: str = APP_ID, version: str = VERSION) -> bytes:
    """造一个最小的 .ccapp（zip：manifest.json + app/index.html）。"""
    manifest = {
        "appId": app_id,
        "name": "管线画布",
        "version": version,
        "description": "把管线协议画成时序图",
        "entry": "app/index.html",
        "methods": [{"name": "drawTimeline", "description": "画时序图"}],
        "permissions": ["read_workspace"],
        "runtime": {"vendors": []},
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        zf.writestr("app/index.html", "<!doctype html><title>pipeline</title>")
    return buffer.getvalue()


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _patch_index(monkeypatch: pytest.MonkeyPatch, entries: list[dict] | None,
                 *, fail: bool = False) -> None:
    """替换两条读链路：`fail=True` 时两级都连不上（用于验证缓存降级）。"""
    payload = json.dumps(
        {"schemaVersion": 1, "updatedAt": "2026-01-01T00:00:00Z", "apps": entries or []},
        ensure_ascii=False,
    ).encode("utf-8")

    async def ok(_identifier: str) -> bytes:
        return payload

    async def bad(_identifier: str) -> bytes:
        raise httpx.ConnectError("网络不可达（测试桩）")

    monkeypatch.setattr(community_service, "_fetch_raw", bad if fail else ok)
    monkeypatch.setattr(community_service, "_fetch_api", bad if fail else ok)


def _patch_download(monkeypatch: pytest.MonkeyPatch, payload: bytes,
                    *, declared_sha: str = "") -> dict:
    """替换 `market_install._download`：按真实实现的契约写盘并校验 sha256。

    返回值是可被用例断言的调用记录（URL / 期望哈希）。
    """
    calls: dict = {}

    async def fake_download(url: str, dest: Path, *, expect_sha256: str = "") -> int:
        calls.update(url=url, expect_sha256=expect_sha256)
        dest.write_bytes(payload)
        if expect_sha256 and _sha256(payload) != expect_sha256.lower():
            # 与 market_install._download 同语义：校验失败删半成品再抛 ValueError
            dest.unlink(missing_ok=True)
            raise ValueError("包完整性校验失败（sha256 不匹配）")
        return len(payload)

    monkeypatch.setattr(market_install, "_download", fake_download)
    return calls


# ───────────────────────── 索引与列表 ─────────────────────────


async def test_list_parses_index_and_drops_malformed_entries(
    env: Path, monkeypatch: pytest.MonkeyPatch,
):
    """正常条目入列表；缺 sha256 / appId 非法的条目被跳过（不让一条脏数据废掉整页）。"""
    broken_sha = _entry(app_id="no-sha", sha256="")
    broken_sha["sha256"] = ""
    broken_id = _entry(app_id="Bad App")
    _patch_index(monkeypatch, [_entry(), broken_sha, broken_id])

    result = await community_service.list_apps()

    assert result["enabled"] is True
    assert result["repo"] == REPO
    assert result["stale"] is False
    assert [app["appId"] for app in result["apps"]] == [APP_ID]
    assert result["apps"][0]["releaseTag"] == f"{APP_ID}-{VERSION}"


async def test_list_supports_keyword_query(env: Path, monkeypatch: pytest.MonkeyPatch):
    _patch_index(monkeypatch, [
        _entry(),
        _entry(app_id=OTHER_APP, name="表单构建器", desc="拖拽表单"),
    ])

    assert (await community_service.list_apps("表单"))["count"] == 1
    assert (await community_service.list_apps("pipeline"))["count"] == 1
    assert (await community_service.list_apps("时序图"))["count"] == 1
    assert (await community_service.list_apps("没有这个词"))["count"] == 0


async def test_list_filters_blocked_author(env: Path, monkeypatch: pytest.MonkeyPatch):
    """两档拉黑之一：按 authorId 屏蔽该作者的全部条目（§6.4 拉黑）。"""
    _patch_index(monkeypatch, [_entry(), _entry(app_id=OTHER_APP, author_id=OTHER_AUTHOR)])
    community_service.update_blocklist(users=[AUTHOR])

    result = await community_service.list_apps()

    assert [app["appId"] for app in result["apps"]] == [OTHER_APP]


async def test_list_filters_blocked_app(env: Path, monkeypatch: pytest.MonkeyPatch):
    """两档拉黑之二：按 appId 只屏蔽单条，同作者其它条目仍可见。"""
    _patch_index(monkeypatch, [_entry(), _entry(app_id=OTHER_APP, author_id=OTHER_AUTHOR)])
    community_service.update_blocklist(apps=[OTHER_APP])

    result = await community_service.list_apps()

    assert [app["appId"] for app in result["apps"]] == [APP_ID]


def test_update_blocklist_replaces_only_given_tier(env: Path):
    """PUT 语义：传了哪一档改哪一档，另一档保持；空数组 = 清空该档。"""
    community_service.update_blocklist(users=[AUTHOR])
    community_service.update_blocklist(apps=[APP_ID])
    assert community_service.get_blocklist() == {"users": [AUTHOR], "apps": [APP_ID]}

    community_service.update_blocklist(apps=["非法 appId", "", "ok-app", "ok-app"])
    # 非法 appId 被清洗掉，重复项去重——脏值会让过滤逻辑静默失效
    assert community_service.get_blocklist()["apps"] == ["ok-app"]
    assert community_service.get_blocklist()["users"] == [AUTHOR]

    community_service.update_blocklist(users=[])
    assert community_service.get_blocklist() == {"users": [], "apps": ["ok-app"]}


async def test_list_without_repo_is_disabled(env: Path, monkeypatch: pytest.MonkeyPatch):
    """社区未配置（repo 为空）：返回启用标记，不报错（§7 P5 第 8 条：入口隐藏）。"""
    monkeypatch.setenv(community_service.COMMUNITY_REPO_ENV, "")

    result = await community_service.list_apps()

    assert result["apps"] == []
    assert result["enabled"] is False
    assert (await community_service.list_apps("任意"))["count"] == 0


async def test_install_without_repo_reports_disabled(env: Path, monkeypatch: pytest.MonkeyPatch):
    """未配置社区时安装要给出明确原因，而不是 404「没有该条目」，也不该去打网络。"""
    monkeypatch.setenv(community_service.COMMUNITY_REPO_ENV, "")
    with pytest.raises(CommunityDisabled):
        await community_service.install_app(APP_ID, VERSION)


async def test_get_app_not_found_for_unknown_version(env: Path, monkeypatch: pytest.MonkeyPatch):
    _patch_index(monkeypatch, [_entry()])
    with pytest.raises(CommunityNotFound):
        await community_service.get_app(APP_ID, "9.9.9")


# ───────────────────────── 三级降级 ─────────────────────────


def _write_cache(apps: list[dict], *, age_seconds: float) -> None:
    community_service._write_json(
        community_service._cache_path(),
        {"schemaVersion": 1, "updatedAt": "2026-01-01T00:00:00Z", "apps": apps,
         "_cachedAt": time.time() - age_seconds},
    )


async def test_fetch_index_falls_back_to_stale_cache(env: Path, monkeypatch: pytest.MonkeyPatch):
    """① raw 与 ② API 都失败时用本地缓存，且标记 stale=True（过期也仍可用）。"""
    _write_cache([_entry()], age_seconds=3600)
    _patch_index(monkeypatch, None, fail=True)

    index = await community_service.fetch_index(force=True)

    assert index["stale"] is True
    assert index["source"] == "cache"
    assert [app["appId"] for app in index["apps"]] == [APP_ID]

    # list 把 stale 一路带到响应，界面据此提示「可能不是最新」
    listed = await community_service.list_apps()
    assert listed["stale"] is True


async def test_fetch_index_uses_fresh_cache_without_network(
    env: Path, monkeypatch: pytest.MonkeyPatch,
):
    """TTL 内不打远端（raw 有 5 分钟 CDN 缓存，本地缓存省一次往返）。"""
    _write_cache([_entry(app_id="cached-app")], age_seconds=10)
    _patch_index(monkeypatch, None, fail=True)  # 一旦联网就会失败，用例因此能证明没联网

    index = await community_service.fetch_index()

    assert index["stale"] is False
    assert [app["appId"] for app in index["apps"]] == ["cached-app"]


async def test_fetch_index_raises_when_all_tiers_fail(env: Path, monkeypatch: pytest.MonkeyPatch):
    """三级全失败才报错；错误类型是 NETWORK，路由会映射成 503「无法连接社区」。"""
    _patch_index(monkeypatch, None, fail=True)

    with pytest.raises(CommunityNetworkError) as err:
        await community_service.fetch_index(force=True)

    assert "无法连接社区" in str(err.value)


async def test_fetch_index_second_tier_decodes_base64(env: Path, monkeypatch: pytest.MonkeyPatch):
    """① 失败后走 ②（Contents API 的 base64 内容），成功后回写缓存。"""
    payload = json.dumps(
        {"schemaVersion": 1, "apps": [_entry()]}, ensure_ascii=False
    ).encode("utf-8")

    async def bad_raw(_identifier: str) -> bytes:
        raise httpx.ConnectError("raw 不通（测试桩）")

    async def api_ok(_identifier: str) -> bytes:
        return payload  # _fetch_api 内部负责 base64 解码，这里直接给解码后的字节

    monkeypatch.setattr(community_service, "_fetch_raw", bad_raw)
    monkeypatch.setattr(community_service, "_fetch_api", api_ok)

    index = await community_service.fetch_index(force=True)

    assert index["source"] == "api"
    assert index["stale"] is False
    assert community_service._cache_path().is_file()


# ───────────────────────── 身份 ─────────────────────────


def test_identity_is_idempotent(env: Path):
    """两次调用同一个 userId；形状为 usr_<12 位小写十六进制>（README「几条约定」）。"""
    first = community_service.identity()
    second = community_service.identity()

    assert first["userId"] == second["userId"]
    assert re.match(r"^usr_[0-9a-f]{12}$", first["userId"])
    assert community_service._identity_path().is_file()


# ───────────────────────── 安装 ─────────────────────────


async def test_install_lands_package_with_origin_and_stays_disabled(
    env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    """成功路径：落库到 <root>/<appId>/<version>/ + 写 .origin.json + 默认不启用。"""
    payload = _package()
    entry = _entry(sha256=_sha256(payload), size=len(payload))
    _patch_index(monkeypatch, [entry])
    calls = _patch_download(monkeypatch, payload)

    # 记录解压上限参数，验证 §6.4「max(300MB, 包体积 × 4)」的接线
    limits: list[int] = []
    real_extract = market_install._extract_zip

    def spy_extract(zip_path: Path, dest: Path, *, max_unpacked_bytes: int | None = None) -> None:
        limits.append(max_unpacked_bytes or 0)
        real_extract(zip_path, dest, max_unpacked_bytes=max_unpacked_bytes)

    monkeypatch.setattr(market_install, "_extract_zip", spy_extract)

    result = await community_service.install_app(APP_ID, VERSION)

    target = env / APP_ID / VERSION
    assert result["ok"] is True
    assert result["path"] == str(target)
    assert result["bytes"] == len(payload)
    # 「默认不启用」= 不做任何启用动作，返回值只是告诉前端没有自动注入
    assert result["enabled"] is False
    assert (target / "manifest.json").is_file()
    assert (target / "app" / "index.html").is_file()
    origin = json.loads((target / ".origin.json").read_text(encoding="utf-8"))
    assert origin["source"] == "community"
    assert origin["repo"] == REPO
    assert origin["sha256"] == entry["sha256"]
    assert origin["authorId"] == AUTHOR
    assert origin["releaseTag"] == f"{APP_ID}-{VERSION}"
    assert origin["installedAt"].endswith("Z")

    # 下载时把索引里的 sha256 交给 _download 校验
    assert calls["expect_sha256"] == entry["sha256"]
    # 解压上限 = max(300MB, 包体积 × 4)，小包因此落在 300MB 下限上
    assert limits == [max(300 * 1024 * 1024, len(payload) * 4)]

    # 装完即「可注入」：工具侧立刻能列出它（微应用没有 DB 行，装完就生效）
    listed = await MicroAppListTool().run(
        {}, ToolContext(workspace_root=str(tmp_path), session_id=1, task_id=1,
                        agent_id=1, agent_name="测试代理"),
    )
    assert [app["appId"] for app in listed.data["apps"]] == [APP_ID]

    # 除应用目录与 .community 外没有别的产物（没有 state/、没有注入或启用痕迹）
    assert {p.name for p in env.iterdir()} == {APP_ID, ".community"}


async def test_install_sha256_mismatch_is_integrity_and_leaves_no_dir(
    env: Path, monkeypatch: pytest.MonkeyPatch,
):
    """篡改包被 sha256 拒绝（§7 P5 验收）：报 INTEGRITY，库里不留半截目录。"""
    payload = _package()
    entry = _entry(sha256="b" * 64, size=len(payload))  # 索引声明的哈希与真实包不符
    _patch_index(monkeypatch, [entry])
    _patch_download(monkeypatch, payload)

    with pytest.raises(CommunityIntegrityError) as err:
        await community_service.install_app(APP_ID, VERSION)

    assert "sha256" in str(err.value)
    assert not (env / APP_ID).exists()          # 连 appId 目录都不该出现
    assert list((env / ".community" / "tmp").iterdir()) == []  # 临时目录已清理


async def test_install_size_mismatch_is_integrity(env: Path, monkeypatch: pytest.MonkeyPatch):
    """哈希对但体积与索引不符也要拒（防止索引/包错配）。"""
    payload = _package()
    entry = _entry(sha256=_sha256(payload), size=len(payload) + 1)
    _patch_index(monkeypatch, [entry])
    _patch_download(monkeypatch, payload)

    with pytest.raises(CommunityIntegrityError) as err:
        await community_service.install_app(APP_ID, VERSION)

    assert "体积" in str(err.value)
    assert not (env / APP_ID).exists()


async def test_install_rejects_package_with_other_app_id(
    env: Path, monkeypatch: pytest.MonkeyPatch,
):
    """索引说装 A、包里却是 B：落库后目录名与内容会错配，必须拒装。"""
    payload = _package(app_id="someone-else")
    entry = _entry(sha256=_sha256(payload), size=len(payload))
    _patch_index(monkeypatch, [entry])
    _patch_download(monkeypatch, payload)

    with pytest.raises(CommunityIntegrityError) as err:
        await community_service.install_app(APP_ID, VERSION)

    assert "不一致" in str(err.value)
    assert not (env / APP_ID).exists()


async def test_install_refuses_blocked_entry(env: Path, monkeypatch: pytest.MonkeyPatch):
    """拉黑条目不能再装（列表已隐藏，直接调接口也要挡住）。"""
    payload = _package()
    entry = _entry(sha256=_sha256(payload), size=len(payload))
    _patch_index(monkeypatch, [entry])
    _patch_download(monkeypatch, payload)
    community_service.update_blocklist(apps=[APP_ID])

    with pytest.raises(CommunityNotFound):
        await community_service.install_app(APP_ID, VERSION)
    assert not (env / APP_ID).exists()


@pytest.mark.parametrize("bad_version", ["v1", "1.0", "", "../1.0.0"])
async def test_install_rejects_illegal_version(
    env: Path, monkeypatch: pytest.MonkeyPatch, bad_version: str,
):
    _patch_index(monkeypatch, [_entry()])
    with pytest.raises(CommunityNotFound):
        await community_service.install_app(APP_ID, bad_version)


# ─────────────────── 体积上限（§6.4 体积表）───────────────────


def test_community_limits_are_raised():
    """客户端下载上限提到 105MB；解压默认上限仍是 300MB（既有调用方行为不变）。"""
    assert market_install._MAX_DOWNLOAD_BYTES == 105 * 1024 * 1024
    assert market_install._MAX_UNPACKED_BYTES == 300 * 1024 * 1024


def test_extract_zip_accepts_dynamic_limit(tmp_path: Path):
    """`_extract_zip` 的动态上限可用，且默认行为与原来一致（300MB）。"""
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as zf:
        zf.writestr("manifest.json", "x" * 2048)
    zip_path = tmp_path / "pkg.zip"
    zip_path.write_bytes(payload.getvalue())

    out = tmp_path / "out"
    out.mkdir()
    market_install._extract_zip(zip_path, out)          # 默认上限：正常解压
    assert (out / "manifest.json").is_file()

    tighter = tmp_path / "tighter"
    tighter.mkdir()
    with pytest.raises(ValueError, match="解压后体积超过上限"):
        market_install._extract_zip(zip_path, tighter, max_unpacked_bytes=1024)
