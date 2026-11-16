"""微应用库管理路由（plan-693-2868 §6.5 / §7 P4 第 1 条）。

`/api/microapps` 只读写**本机库**，与 `/api/community`（远端索引 + 下载安装，§7 P5）分工：

- 库根一律取 `orchestration/tools/microapp.py:microapp_root()`
  （`CHATCODER_MICROAPP_ROOT` → 默认 `~/.chatcoder/microapps`，§6.1），与
  `microapp_list` / `microapp_call` / `microapp_validate` 三个工具同一份口径。
  路由自己拼路径必然与工具漂移——「工具说有、管理页说没有」这类问题无从解释。
- 目录结构 `<root>/<appId>/<version>/{manifest.json, app/, .origin.json}`（§6.1 目录图）；
  `.origin.json` 由导入 / 社区安装写入，缺失视为本地手写（回落 `{"source": "local"}`）。
- 发布与撤回**不在这里**：走 Electron IPC（`community:publish` / `community:withdraw`），
  用户 PAT 不出主进程（§6.5）。
- 每个 appId 的「当前版本」= 版本号最大的那份，与 `_scan_installs()` 同一口径：
  同一应用可能装着多个版本，列表与详情必须指向唯一一份。

端点（§6.5）：

    GET    /api/microapps                    列表（含各版本聚合；库不存在 = 空列表）
    GET    /api/microapps/{appId}            详情（manifest 原文 + 所有版本 + .origin.json）
    POST   /api/microapps/{appId}/validate   复用 `_validate_manifest` 逐条报问题
    DELETE /api/microapps/{appId}/{version}  删一个版本；`version=all` 删整个 appId
    POST   /api/microapps/import             multipart 上传 zip → 校验 → 原子落库

`export` / `state` 两个端点同在 §6.5 的清单里，但 P4 验收只覆盖上面五项（库管理 + 导入）；
等前端「导出 zip」与「注入状态」两处真正落地时再补，避免先造出没人调用的接口。

错误映射（`MicroAppLibraryError.code` → HTTP，写法照 `routers/community.py`）：

    ARG_INVALID → 400（appId / version 不合口径，或目标路径越出库根）
    NOT_FOUND   → 404（库内没有该 appId / 该版本）
    CONFLICT    → 409（导入时同名同版本已存在）
    INTEGRITY   → 422（不是合法 zip、manifest 不合规、解压越界 / 超量）
    TOO_LARGE   → 413（上传包超过 100MB，§6.4 体积表）
    EXEC_FAILED → 500（库根不可读 / 不可写等文件系统故障）

实现分层：`_xxx` 私有协程只抛域错误，路由函数是薄壳，统一在边界用 `_http_error()`
翻译成 HTTPException——转换点只有一个，不会出现「某条分支忘了翻」的 500。
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile

from app.orchestration.tools.microapp import (
    _VERSION_PATTERN,
    APP_ID_PATTERN,
    _find_install,
    _Install,
    _method_names,
    _pick_version_dir,
    _read_manifest,
    _scan_installs,
    _validate_manifest,
    _version_sort_key,
    microapp_root,
)
from app.services import market_install

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/microapps", tags=["microapps"])

# 版本号口径与 `microapp.py` / `community_service.py` 一致（Release tag = <appId>-<version>，
# §6.4）；直接复用同一份正则，避免第三处手写后漂移
VERSION_PATTERN = _VERSION_PATTERN

# 导入包体积上限 100MB（§6.4 体积表「上传包 100MB」；与社区包同一口径）
MAX_IMPORT_BYTES = 100 * 1024 * 1024
# 解压后总体积的下限口径，实际上限取 max(它, 包体积 × 4)（§6.4 体积表）
MIN_UNPACK_LIMIT = market_install._MAX_UNPACKED_BYTES
# 上传流式落盘的块大小：100MB 级包不能整包读进内存
_UPLOAD_CHUNK = 1024 * 1024
# 来源记录文件名（§6.1 目录图）
_ORIGIN_FILE = ".origin.json"
# 导入临时目录放在库根下：与目标同盘，最后的 rename 才是原子的；
# 点开头 → `_scan_installs()` 会跳过它，不会被当成一个微应用
_IMPORT_TMP_DIR = ".import-tmp"
# DELETE 的 version 通配：删整个 appId（目录名不会叫 all，与版本号口径不冲突）
_ALL_VERSIONS = "all"


# ── 错误模型（写法照 routers/community.py：服务侧给 code，路由翻 HTTP）────────


class MicroAppLibraryError(Exception):
    """库管理操作的统一错误基类；子类只改 `code`。

    `issues` 用于把「逐条问题」一并带给界面（manifest 校验、解压失败的多条原因），
    路由把非空 issues 放进 `detail`，与 main.py 的结构化错误响应（`error.detail`）对齐。
    """

    code = "EXEC_FAILED"

    def __init__(self, message: str, *, issues: list[str] | None = None) -> None:
        super().__init__(message)
        self.issues = list(issues or [])


class MicroAppArgumentInvalid(MicroAppLibraryError):
    """appId / version 不合口径，或目标路径越出库根（400）。"""

    code = "ARG_INVALID"


class MicroAppNotFound(MicroAppLibraryError):
    """库内没有该 appId / 该版本（404）。"""

    code = "NOT_FOUND"


class MicroAppConflict(MicroAppLibraryError):
    """导入时同名同版本已存在（409）。"""

    code = "CONFLICT"


class MicroAppIntegrity(MicroAppLibraryError):
    """包不合规：不是合法 zip、manifest 未通过校验、解压越界或超量（422）。"""

    code = "INTEGRITY"


class MicroAppTooLarge(MicroAppLibraryError):
    """上传包超过 100MB；不进库（413）。"""

    code = "TOO_LARGE"


_ERROR_STATUS = {
    "ARG_INVALID": 400,
    "NOT_FOUND": 404,
    "CONFLICT": 409,
    "INTEGRITY": 422,
    "TOO_LARGE": 413,
    "EXEC_FAILED": 500,
}


def _http_error(exc: MicroAppLibraryError) -> HTTPException:
    """把库错误翻成 HTTP 错误（detail 用可读中文，界面直接展示）。"""
    status = _ERROR_STATUS.get(exc.code, 500)
    detail: Any = {"message": str(exc), "issues": exc.issues} if exc.issues else str(exc)
    return HTTPException(status, detail=detail)


# ── 口径校验（与工具面 D26 同一份）───────────────────────────


def _check_app_id(app_id: str) -> str:
    """appId 校验：目录名 / Release tag / 正则三方共用同一口径（D26 / §6.4）。"""
    value = str(app_id or "").strip()
    if not APP_ID_PATTERN.match(value):
        raise MicroAppArgumentInvalid(
            f"appId 非法：{value!r}（须匹配 {APP_ID_PATTERN.pattern}）"
        )
    return value


def _check_version(version: str) -> str:
    """版本目录名校验：非法名既拼不出合法库路径，也会让 Release tag 不可用。"""
    value = str(version or "").strip()
    if not VERSION_PATTERN.match(value):
        raise MicroAppArgumentInvalid(f"版本号非法：{value!r}（期望 1.2.3 形式，§6.4）")
    return value


# ── 读库：列表项 / 版本 / 来源记录 ───────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dir_size(path: Path) -> int:
    """递归统计目录字节数；读不到的条目跳过（一处权限问题不该让整页列表失败）。"""
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def _version_dirs(app_dir: Path) -> list[Path]:
    """该 appId 下的版本目录（目录名符合版本号口径），按版本从旧到新排序。

    这里**不要求** manifest.json 可解析：库管理页要能看到「装了但坏了」的版本，
    并且允许把它删掉——只列健康版本会让坏目录永远清不掉。
    """
    try:
        candidates = [
            child for child in app_dir.iterdir()
            if child.is_dir() and VERSION_PATTERN.match(child.name)
        ]
    except OSError:
        return []
    candidates.sort(key=lambda child: _version_sort_key(child.name))
    return candidates


def _read_origin(version_dir: Path) -> dict[str, Any]:
    """读 `<appId>/<version>/.origin.json`；缺失或损坏时回落 `{"source": "local"}`。

    本地手写（或早期没有任何来源记录的）版本没有这个文件，回落到 local 而不是报错：
    界面只需要展示「来源是什么」，不存在的来源记录不该变成一条错误。
    """
    path = version_dir / _ORIGIN_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"source": "local"}
    if not isinstance(raw, dict):
        return {"source": "local"}
    raw.setdefault("source", "local")
    return raw


def _permissions(manifest: dict[str, Any]) -> list[str]:
    """manifest.permissions 里的字符串项（结构问题由 `_validate_manifest` 报）。"""
    raw = manifest.get("permissions")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, str)]


def _vendors(manifest: dict[str, Any]) -> list[dict[str, str]]:
    """manifest.runtime.vendors 收成 `{name, version}` 列表（§6.3 共享 vendor）。"""
    runtime = manifest.get("runtime")
    raw = runtime.get("vendors") if isinstance(runtime, dict) else None
    if not isinstance(raw, list):
        return []
    vendors: list[dict[str, str]] = []
    for item in raw:
        if isinstance(item, dict):
            vendors.append({
                "name": str(item.get("name") or ""),
                "version": str(item.get("version") or ""),
            })
    return vendors


def _version_detail(version_dir: Path) -> dict[str, Any]:
    """单个已装版本的展示信息（库管理页按版本列出与删除）。"""
    return {
        "version": version_dir.name,
        "path": str(version_dir),
        "sizeBytes": _dir_size(version_dir),
        "hasManifest": (version_dir / "manifest.json").is_file(),
        "origin": _read_origin(version_dir),
    }


def _app_summary(latest: _Install) -> dict[str, Any]:
    """一个 appId 的列表项：以版本最高那份的 manifest 作为「当前版本」的展示信息。

    `installedVersions` 列出全部已装版本；`sizeBytes` 是该 appId 所有版本合计的磁盘占用
    （库管理看的是总共占了多少）；`origin` 取当前版本（最高版本）的来源记录。
    """
    app_dir = latest.version_dir.parent
    versions = _version_dirs(app_dir)
    return {
        "appId": latest.app_id,
        "name": str(latest.manifest.get("name") or ""),
        "version": latest.version,
        "description": str(latest.manifest.get("description") or ""),
        "methods": _method_names(latest.manifest),
        "perms": _permissions(latest.manifest),
        "vendors": _vendors(latest.manifest),
        "installedVersions": [item.name for item in versions],
        "sizeBytes": sum(_dir_size(item) for item in versions),
        "origin": _read_origin(latest.version_dir),
        "path": str(latest.version_dir),
    }


def _matches(summary: dict[str, Any], needle: str) -> bool:
    """模糊过滤：name / appId / description 任一命中即可（大小写不敏感）。"""
    folded = str(needle or "").strip().lower()
    if not folded:
        return True
    for key in ("appId", "name", "description"):
        if folded in str(summary.get(key) or "").lower():
            return True
    return False


def _collect(root: Path, query: str) -> list[dict[str, Any]]:
    """扫描库根并构造列表项（同步实现，调用方放线程）。

    库根不存在时 `_scan_installs()` 返回空列表——「还没装过任何东西」不是错误（§6.5）。
    """
    summaries = [_app_summary(install) for install in _scan_installs(root)]
    summaries.sort(key=lambda item: item["appId"])
    return [item for item in summaries if _matches(item, query)]


def _read_manifest_text(version_dir: Path) -> str:
    """manifest.json 原文（详情页要能展示/对照原文，而不是只有解析结果）。"""
    try:
        return (version_dir / "manifest.json").read_text(encoding="utf-8")
    except OSError:
        return ""


# ── 写库：路径防护与原子落库 ─────────────────────────────────


def _guarded_target(root: Path, app_id: str, version: str) -> tuple[Path, Path]:
    """解析删除目标并做二次防护，返回 `(app 目录, 待删目录)`。

    appId 已经过 D26 正则（挡住了 `../x`），这里仍用 `resolve()` 再确认一次落点：
    正则挡不住「版本目录是指向库外的符号链接」——`rmtree` 会顺着链接把库外的东西删掉
    （与 §7 P0 第 3 条 app:// 协议「禁 `..`、禁绝对路径」同一口径）。

    - `version == "all"`：目标是整个 appId 目录，要求它**严格在**库根之内；
    - 否则：目标是 `<appId>/<version>`，要求它**严格在** appId 目录之内（不能等于它）。
    """
    root_resolved = root.resolve()
    app_dir = (root / app_id).resolve()
    if app_dir == root_resolved or not app_dir.is_relative_to(root_resolved):
        raise MicroAppArgumentInvalid(f"appId 越出微应用库根：{app_id!r}")
    if version == _ALL_VERSIONS:
        return app_dir, app_dir
    target = (app_dir / version).resolve()
    if target == app_dir or not target.is_relative_to(app_dir):
        raise MicroAppArgumentInvalid(f"版本路径越出应用目录：{version!r}")
    return app_dir, target


def _commit_stage(staged: Path, target: Path) -> None:
    """把校验通过的 staging 目录整体改名为 `<root>/<appId>/<version>`。

    同盘 rename 才是原子的（staging 建在库根的 `.import-tmp` 下，与目标同盘）：中途失败
    时库里不会留下半截版本目录，否则前端看到的是「导入了但打不开」的条目（§6.4 安装同写法）。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    staged.rename(target)


async def _save_upload(file: UploadFile, dest: Path) -> int:
    """把上传内容流式落盘并计数，返回字节数。

    上限 100MB（§6.4 体积表）：超限立刻中止并删掉半成品文件，不把内存 / 磁盘打满；
    空内容同样拒绝——0 字节的「包」只可能是传错了文件。
    """
    written = 0
    try:
        with dest.open("wb") as out:
            while True:
                chunk = await file.read(_UPLOAD_CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_IMPORT_BYTES:
                    raise MicroAppTooLarge(
                        f"上传包超过 {MAX_IMPORT_BYTES // 1024 // 1024}MB 上限"
                        f"（§6.4 体积表），已中止导入"
                    )
                out.write(chunk)
    except MicroAppLibraryError:
        dest.unlink(missing_ok=True)
        raise
    if written == 0:
        dest.unlink(missing_ok=True)
        raise MicroAppIntegrity("上传内容为空，未导入任何内容")
    return written


# ── 端点实现（只抛域错误，转换在下面的薄壳里）───────────────


async def _list_apps(root: Path, query: str) -> dict[str, Any]:
    try:
        apps = await asyncio.to_thread(_collect, root, query)
    except OSError as exc:  # 库根不可读（权限 / 断链）与「空库」必须区分开
        raise MicroAppLibraryError(f"扫描微应用库失败：{exc}") from exc
    return {"apps": apps, "count": len(apps), "root": str(root)}


async def _get_app(root: Path, raw_app_id: str) -> dict[str, Any]:
    app_id = _check_app_id(raw_app_id)
    try:
        install = await asyncio.to_thread(_find_install, root, app_id)
    except OSError as exc:
        raise MicroAppLibraryError(f"扫描微应用库失败：{exc}") from exc
    if install is None:
        raise MicroAppNotFound(f"未安装微应用：{app_id}（库根 {root}）")

    app_dir = install.version_dir.parent
    version_dirs = await asyncio.to_thread(_version_dirs, app_dir)
    versions = [await asyncio.to_thread(_version_detail, item) for item in version_dirs]
    summary = await asyncio.to_thread(_app_summary, install)
    manifest_text = await asyncio.to_thread(_read_manifest_text, install.version_dir)
    return {
        **summary,
        "appDir": str(app_dir),
        "versions": versions,
        # manifest 原文与解析结果都给：界面展示原文，程序化判断用解析结果
        "manifest": install.manifest,
        "manifestText": manifest_text,
    }


async def _validate_app(root: Path, raw_app_id: str) -> dict[str, Any]:
    """校验对象是该 appId 的**最高版本目录**（与 `microapp_validate` 工具同一口径）。"""
    app_id = _check_app_id(raw_app_id)
    # 正则已排除 `/` 与 `..`，拼路径不会越出库根
    version_dir = await asyncio.to_thread(_pick_version_dir, root / app_id)
    if version_dir is None:
        raise MicroAppNotFound(f"未安装微应用：{app_id}（库根 {root}）")

    manifest_file = version_dir / "manifest.json"
    manifest = await asyncio.to_thread(_read_manifest, manifest_file)
    if not manifest_file.is_file():
        issues = [f"缺少 manifest.json：{manifest_file}"]
    elif manifest is None:
        issues = ["manifest.json 存在但无法解析为 JSON 对象"]
    else:
        issues = await asyncio.to_thread(_validate_manifest, manifest, app_id, version_dir)

    return {
        "ok": not issues,
        "appId": app_id,
        "version": version_dir.name,
        "path": str(version_dir),
        "issues": issues,
    }


async def _delete_version(root: Path, raw_app_id: str, raw_version: str) -> dict[str, Any]:
    """删除一个版本；`all` 删整个 appId。

    路径防护在 `_guarded_target()`（resolve 后确认仍在 appId 目录内），删除用
    `shutil.rmtree`。单版本删完后若该 appId 已无任何版本目录，顺手清掉空壳目录：
    库里留下既列不出也无法校验的空目录只会让人以为「装了但坏了」。
    """
    app_id = _check_app_id(raw_app_id)
    version_name = str(raw_version or "").strip()
    if version_name != _ALL_VERSIONS:
        version_name = _check_version(version_name)

    app_dir, target = _guarded_target(root, app_id, version_name)
    if not target.is_dir():
        what = (
            f"{app_id}（整应用）" if version_name == _ALL_VERSIONS else f"{app_id}@{version_name}"
        )
        raise MicroAppNotFound(f"库内没有 {what}（库根 {root}）")

    try:
        await asyncio.to_thread(shutil.rmtree, target)
    except OSError as exc:
        raise MicroAppLibraryError(f"删除失败：{exc}") from exc

    app_removed = not app_dir.exists()
    remaining: list[str] = []
    if not app_removed:
        remaining = [item.name for item in await asyncio.to_thread(_version_dirs, app_dir)]
        if not remaining:
            # 版本已全部删完：清掉空壳 appId 目录（失败只是留个空目录，不影响本次删除结果）
            try:
                await asyncio.to_thread(app_dir.rmdir)
                app_removed = True
            except OSError:
                logger.debug("[microapps] 空目录清理失败（忽略）: %s", app_dir)

    logger.info("[microapps] 删除 %s@%s appRemoved=%s", app_id, version_name, app_removed)
    return {
        "ok": True,
        "appId": app_id,
        "version": version_name,
        "deleted": str(target),
        "appRemoved": app_removed,
        "remainingVersions": remaining,
    }


async def _import_app(root: Path, file: UploadFile) -> dict[str, Any]:
    """导入本地 zip 包：解压 → 校验 → 原子落库 → 写 `.origin.json`。

    链路与社区安装（`community_service.install_app`）保持同一套约束：

    1. 落盘上限 100MB（§6.4 体积表「上传包 100MB」）；
    2. 解压走 `market_install._extract_zip`（拒绝绝对路径 / `..` / realpath 越界 / 条目数与
       总量超限），解压上限 `max(300MB, 包体积 × 4)`（§6.4 同表）；
    3. 包内容根按 `manifest.json` 定位（`_find_root`，多套一层的包也能装）；
    4. manifest 的 appId / version 决定落库目录；`_validate_manifest` 全过才写库
       （它内部用 `_safe_entry` 保证 entry 不越出应用目录且文件存在）；
    5. 先解到 `<root>/.import-tmp/...` 的 staging（目录名必须是版本号，`_validate_manifest`
       要拿它和 manifest.version 对齐），再整体 rename 到 `<root>/<appId>/<version>`
       ——同盘 rename 才原子，失败时库里不会留半截目录；
    6. 同名同版本已存在直接 409，提示先删或升版本（不做静默覆盖：覆盖会丢掉用户已装的那份）。
    """
    tmp_base = root / _IMPORT_TMP_DIR
    try:
        await asyncio.to_thread(tmp_base.mkdir, parents=True, exist_ok=True)
    except OSError as exc:
        raise MicroAppLibraryError(f"微应用库根不可写：{root}（{exc}）") from exc

    with tempfile.TemporaryDirectory(prefix="import-", dir=tmp_base) as tmp:
        tmp_dir = Path(tmp)
        zip_path = tmp_dir / "package.zip"
        written = await _save_upload(file, zip_path)

        unpack_dir = tmp_dir / "unpacked"
        unpack_dir.mkdir()
        # §6.4 体积表：解压上限 max(300MB, 包体积 × 4)
        limit = max(MIN_UNPACK_LIMIT, written * 4)
        try:
            await asyncio.to_thread(
                market_install._extract_zip, zip_path, unpack_dir, max_unpacked_bytes=limit,
            )
        except (ValueError, zipfile.BadZipFile, OSError) as exc:
            raise MicroAppIntegrity(f"压缩包解压失败：{exc}") from exc

        package_root = await asyncio.to_thread(
            market_install._find_root, unpack_dir, ("manifest.json",),
        )
        manifest = await asyncio.to_thread(_read_manifest, package_root / "manifest.json")
        if manifest is None:
            raise MicroAppIntegrity("包内缺少 manifest.json，或它不是可解析的 JSON 对象")

        app_id = str(manifest.get("appId") or "").strip()
        if not APP_ID_PATTERN.match(app_id):
            raise MicroAppIntegrity(
                f"包内 manifest.appId 非法：{app_id!r}（须匹配 {APP_ID_PATTERN.pattern}，D26）"
            )
        version_name = str(manifest.get("version") or "").strip()
        if not VERSION_PATTERN.match(version_name):
            raise MicroAppIntegrity(
                f"包内 manifest.version 非法：{version_name!r}（期望 1.2.3 形式，§6.4）"
            )

        # 409：同名同版本已存在。覆盖会静默丢掉用户已装的那份，不如让用户显式选择
        app_dir, _ = _guarded_target(root, app_id, _ALL_VERSIONS)
        target = app_dir / version_name
        if target.exists():
            raise MicroAppConflict(
                f"{app_id}@{version_name} 已存在（{target}）：先删除该版本"
                f"（DELETE /api/microapps/{app_id}/{version_name}）或升版本后再导入"
            )

        # staging 目录名必须是版本号：`_validate_manifest` 用它和 manifest.version 对齐，
        # 也顺带保证 entry 的相对路径与落库后一致
        staged = tmp_dir / "stage" / version_name
        try:
            # symlinks=True：包内符号链接原样保留，不把链接指向的库外内容复制进库
            await asyncio.to_thread(shutil.copytree, package_root, staged, symlinks=True)
        except (OSError, shutil.Error) as exc:
            raise MicroAppIntegrity(f"包内容无法复制到落库 staging：{exc}") from exc

        issues = await asyncio.to_thread(_validate_manifest, manifest, app_id, staged)
        if issues:
            raise MicroAppIntegrity("包内 manifest 未通过校验，未写入微应用库", issues=issues)

        origin = {"source": "import", "importedAt": _now_iso()}
        (staged / _ORIGIN_FILE).write_text(
            json.dumps(origin, ensure_ascii=False, indent=2), encoding="utf-8",
        )

        try:
            await asyncio.to_thread(_commit_stage, staged, target)
        except OSError as exc:
            if target.exists():
                # 并发导入同一版本：另一个请求已落库，按冲突处理
                raise MicroAppConflict(
                    f"{app_id}@{version_name} 已存在（{target}），本次导入未写入"
                ) from exc
            raise MicroAppLibraryError(f"写入微应用库失败：{exc}") from exc

        unpacked_bytes = await asyncio.to_thread(_dir_size, target)

    logger.info(
        "[microapps] 导入完成 %s@%s 包=%s 解压=%s", app_id, version_name, written, unpacked_bytes,
    )
    return {
        "ok": True,
        "appId": app_id,
        "version": version_name,
        "path": str(target),
        "bytes": written,
        "unpackedBytes": unpacked_bytes,
        "origin": origin,
        # 默认不启用：导入不注入面板，是否注入由用户在前端决定（§6.4 同一语义）
        "enabled": False,
        "message": "已导入微应用库；默认不启用，需要到拓展页手动注入",
    }


# ── 端点（薄壳：唯一把域错误翻成 HTTP 的边界）────────────────


@router.get("", response_model=dict)
async def list_microapps(q: str = ""):
    """已安装微应用列表（§6.5 的 list）。

    - 库根不存在 / 空库返回 `{apps: [], count: 0}`，**不是**错误（首次使用必然如此）；
    - `q` 按 name / appId / description 模糊过滤（大小写不敏感）；
    - 每个 appId 只出一条，`installedVersions` 列出全部已装版本。
    """
    try:
        return await _list_apps(microapp_root(), q)
    except MicroAppLibraryError as exc:
        raise _http_error(exc) from exc


@router.get("/{appId}", response_model=dict)
async def get_microapp(appId: str):
    """单个微应用详情（§6.5 的 detail）：列表项 + manifest 原文 + 所有版本 + 来源记录。"""
    try:
        return await _get_app(microapp_root(), appId)
    except MicroAppLibraryError as exc:
        raise _http_error(exc) from exc


@router.post("/{appId}/validate", response_model=dict)
async def validate_microapp(appId: str):
    """校验一个已装微应用的 manifest（§6.5 的 validate，复用 `_validate_manifest`）。

    校验不通过仍返回 200 + `ok: false` + `issues[]`：这是「检查结果」而不是请求失败，
    界面要能把问题逐条列出来（4xx 会让前端走错误分支，反而看不到明细）。
    """
    try:
        return await _validate_app(microapp_root(), appId)
    except MicroAppLibraryError as exc:
        raise _http_error(exc) from exc


@router.delete("/{appId}/{version}", response_model=dict)
async def delete_microapp_version(appId: str, version: str):
    """删除一个已装版本；`version=all` 删整个 appId（§6.5 的 remove）。

    返回删除结果（`deleted` 路径、`appRemoved`）与剩余版本 `remainingVersions`。
    """
    try:
        return await _delete_version(microapp_root(), appId, version)
    except MicroAppLibraryError as exc:
        raise _http_error(exc) from exc


@router.post("/import", response_model=dict)
async def import_microapp(file: UploadFile = File(...)):
    """导入本地 zip 包（§6.5 的 import）；同名同版本已存在返回 409。"""
    try:
        return await _import_app(microapp_root(), file)
    except MicroAppLibraryError as exc:
        raise _http_error(exc) from exc
