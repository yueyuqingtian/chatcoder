"""外置微应用路由（protocol 13-external-microapps §2）。

`/api/microapps-ext` 是**只读**入口：外置微应用由用户自己放进库目录，server 侧不认识
「安装 / 删除」这类动作（那是文件系统里的事），这里只回答「库里有什么、声明对不对」。

与 `/api/microapps`（内置微应用库，`routers/microapps.py`）的分工：

- 库根一律取 `orchestration/tools/microapps_ext.py:microapp_ext_root()`
  （`CHATCODER_MICROAPP_EXT_ROOT` → 默认 `~/.chatcoder/microapps-ext`），
  与 `microapp_list` / `microapp_call` 同一份口径。路由自己拼路径必然与工具漂移。
- 目录结构 `<root>/<microappId>/microapp.json`（协议 §2），没有内置那层版本目录。
- 进程的生命周期（拉起 / 停止 / 崩溃恢复）归 Electron 主进程（协议 §5），
  所以这里没有 start / stop 端点：server 侧强行 spawn 会让「谁拥有进程」出现两个答案。

端点：

    GET  /api/microapps-ext                 列表（库不存在 = 空列表，不是错误）
    GET  /api/microapps-ext/{microappId}    详情（清单原文 + 校验结果）
    POST /api/microapps-ext/{microappId}/validate  复用 `validate_manifest` 逐条报问题

错误映射（`MicroAppExtError.code` → HTTP，写法照 `routers/microapps.py` 的 `_http_error`）：

    ARG_INVALID → 400（microappId 不合口径）
    NOT_FOUND   → 404（库内没有该 microappId）
    EXEC_FAILED → 500（库根不可读等文件系统故障）

实现分层与 `routers/microapps.py` 一致：`_xxx` 私有协程只抛域错误，路由函数是薄壳，
统一在边界用 `_http_error()` 翻译成 HTTPException——转换点只有一个，不会出现
「某条分支忘了翻」的 500。
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from app.orchestration.tools.microapps_ext import (
    MICROAPP_ID_PATTERN,
    MICROAPP_MANIFEST_NAME,
    ExternalMicroApp,
    inspect_external_microapp,
    list_external_microapps,
    microapp_ext_root,
    validate_manifest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/microapps-ext", tags=["microapps-ext"])


# ── 错误模型（写法照 routers/microapps.py：服务侧给 code，路由翻 HTTP）────────


class MicroAppExtError(Exception):
    """外置微应用操作的统一错误基类；子类只改 `code`。

    `issues` 用于把「逐条问题」一并带给界面（清单校验的多条原因），
    路由把非空 issues 放进 `detail`，与 main.py 的结构化错误响应（`error.detail`）对齐。
    """

    code = "EXEC_FAILED"

    def __init__(self, message: str, *, issues: list[str] | None = None) -> None:
        super().__init__(message)
        self.issues = list(issues or [])


class MicroAppExtArgumentInvalid(MicroAppExtError):
    """microappId 不合口径（400）。"""

    code = "ARG_INVALID"


class MicroAppExtNotFound(MicroAppExtError):
    """库内没有该 microappId（404）。"""

    code = "NOT_FOUND"


_ERROR_STATUS = {
    "ARG_INVALID": 400,
    "NOT_FOUND": 404,
    "EXEC_FAILED": 500,
}


def _http_error(exc: MicroAppExtError) -> HTTPException:
    """把库错误翻成 HTTP 错误（detail 用可读中文，界面直接展示）。"""
    status = _ERROR_STATUS.get(exc.code, 500)
    detail: Any = {"message": str(exc), "issues": exc.issues} if exc.issues else str(exc)
    return HTTPException(status, detail=detail)


def _check_id(microapp_id: str) -> str:
    """id 校验：目录名 / 清单 id / 工具入参三方共用同一口径（协议 §2）。"""
    value = str(microapp_id or "").strip()
    if not MICROAPP_ID_PATTERN.match(value):
        raise MicroAppExtArgumentInvalid(
            f"microappId 非法：{value!r}（须匹配 {MICROAPP_ID_PATTERN.pattern}）"
        )
    return value


# ── 读库：列表项与详情 ──────────────────────────────────────


def _read_manifest_text(app_dir: Path) -> str:
    """清单原文（详情页要能展示 / 对照原文，而不是只有解析结果）。"""
    try:
        return (app_dir / MICROAPP_MANIFEST_NAME).read_text(encoding="utf-8")
    except OSError:
        return ""


def _collect(root: Path) -> list[dict[str, Any]]:
    """扫描库根构造列表项（同步实现，调用方放线程）。

    列表只列**校验通过**的：混进坏条目会让「能用」这件事变得不可知；
    坏在哪一步由详情 / 校验端点回答（那里读得到 problems）。
    """
    return [app.to_dict() for app in list_external_microapps(root)]


# ── 端点实现（只抛域错误，转换在下面的薄壳里）───────────────


async def _list_apps(root: Path) -> dict[str, Any]:
    try:
        apps = await asyncio.to_thread(_collect, root)
    except OSError as exc:  # 库根不可读（权限 / 断链）与「空库」必须区分开
        raise MicroAppExtError(f"扫描外置微应用库失败：{exc}") from exc
    return {"apps": apps, "count": len(apps), "root": str(root)}


async def _get_app(root: Path, raw_id: str) -> dict[str, Any]:
    """详情：清单原文 + 校验结果 + 展平后的展示字段。"""
    microapp_id = _check_id(raw_id)
    app: ExternalMicroApp | None = await asyncio.to_thread(
        inspect_external_microapp, microapp_id, root,
    )
    if app is None:
        raise MicroAppExtNotFound(f"未安装外置微应用：{microapp_id}（库根 {root}）")
    manifest_text = await asyncio.to_thread(_read_manifest_text, app.path)
    return {
        **app.to_dict(),
        "microappId": app.microapp_id,
        # 清单原文与解析结果都给：界面展示原文，程序化判断用解析结果
        "manifest": app.manifest,
        "manifestText": manifest_text,
        "ok": not app.problems,
    }


async def _validate_app(root: Path, raw_id: str) -> dict[str, Any]:
    microapp_id = _check_id(raw_id)
    app: ExternalMicroApp | None = await asyncio.to_thread(
        inspect_external_microapp, microapp_id, root,
    )
    if app is None:
        raise MicroAppExtNotFound(f"未安装外置微应用：{microapp_id}（库根 {root}）")

    # 详情那条路径已经做过校验；这里再跑一遍纯函数而不是复用 problems，
    # 是为了让「校验端点」的语义独立于「列表 / 详情曾经怎么算」——纯函数便宜，不会漂移
    issues = validate_manifest(app.manifest, app.path.name)
    return {
        "ok": not issues,
        "microappId": microapp_id,
        "version": app.version,
        "path": str(app.path),
        "issues": issues,
    }


# ── 端点（薄壳：唯一把域错误翻成 HTTP 的边界）────────────────


@router.get("", response_model=dict)
async def list_external_apps():
    """外置微应用列表。

    - 库根不存在 / 空库返回 `{apps: [], count: 0}`，**不是**错误（首次使用必然如此）；
    - 每项带 `problems`（正常情况下为空数组），与工具面 `microapp_list` 同一份数据结构。
    """
    try:
        return await _list_apps(microapp_ext_root())
    except MicroAppExtError as exc:
        raise _http_error(exc) from exc


@router.get("/{microappId}", response_model=dict)
async def get_external_app(microappId: str):
    """单个外置微应用详情：清单原文 + 校验结果 + 展示字段。

    声明不合格时仍返回 200：这是「库里的现状」，不是请求失败——
    404 只表示**这个 id 在库里根本没有**。
    """
    try:
        return await _get_app(microapp_ext_root(), microappId)
    except MicroAppExtError as exc:
        raise _http_error(exc) from exc


@router.post("/{microappId}/validate", response_model=dict)
async def validate_external_app(microappId: str):
    """校验一个外置微应用的清单（复用 `validate_manifest`）。

    校验不通过仍返回 200 + `ok: false` + `issues[]`：这是「检查结果」而不是请求失败，
    界面要能把问题逐条列出来（4xx 会让前端走错误分支，反而看不到明细）。
    """
    try:
        return await _validate_app(microapp_ext_root(), microappId)
    except MicroAppExtError as exc:
        raise _http_error(exc) from exc
