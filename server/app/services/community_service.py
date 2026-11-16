"""社区微应用：索引读取、身份、拉黑与安装（plan-693-2868 §3.4 / §5.4 / §6.4 / §7 P5）。

**职责边界**：本模块只做「读索引 + 下载安装」。发布与撤回走 Electron 主进程（用户 PAT 不出
主进程，§6.5），因此这里没有任何写 GitHub 的代码——社区仓库的写权限只属于 owner 的 Actions
（§3.3 已明确排除「内置 owner 写 token」）。

**读链路三级降级**（§3.4「读（所有用户，无 token）」/ §6.4）：

    ① raw.githubusercontent.com/<owner>/<repo>/HEAD/apps.json?t=<epoch>
       —— `?t=` 击穿 GitHub raw 的 `max-age=300`（§5.4），否则刚发布的条目 5 分钟内搜不到；
    ② GET api.github.com/repos/<owner>/<repo>/contents/apps.json（内容 base64）
       —— 匿名 60 次/时，只在 ① 失败时兜底，正常浏览不走 API（§5.4）；
    ③ 本地缓存 `<libraryRoot>/.community/cache.json`（TTL 600s，过期仍可用并标 `stale=True`）；
    三级全失败才抛 NETWORK，由路由映射成 503「无法连接社区」，本地库不受影响（§3.4 第 4 条）。

**安装**（§6.4「读取与安装」/ §7 P5 第 3 条）：下载 → 校验 `sha256` + `size` → 解压
（上限 `max(300MB, size×4)`）→ 落库 `<libraryRoot>/<appId>/<version>/` → 写 `.origin.json`
→ **默认不启用**。微应用没有数据库行，「启用」= 前端是否把它注入面板，装完即可注入，
由用户在前端决定；因此本模块不做任何"启用"动作。

**错误模型**：所有失败都转成 `CommunityError` 子类，`code` 供路由映射状态码
（NOT_FOUND→404 / NETWORK→503 / INTEGRITY→422 / DISABLED→503），界面据此给可读文案。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from app.orchestration.tools.microapp import APP_ID_PATTERN, microapp_root
from app.services import market_install

logger = logging.getLogger(__name__)

# 社区仓库地址的显式覆盖（本地排障与测试用；常规来源是设置项 community_repo）
COMMUNITY_REPO_ENV = "CHATCODER_COMMUNITY_REPO"
# 仓库地址形如 owner/repo（GitHub 口径），校验后再拼 URL——它会被拼进请求地址，不能放行怪字符
REPO_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")
# 版本号口径与 microapp.py 一致（Release tag = <appId>-<version>，§6.4）
_VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_USER_ID_PATTERN = re.compile(r"^usr_[0-9a-f]{12}$")

# 索引缓存 TTL：600s（§3.4 第 3 条 / §6.4）
CACHE_TTL = 600.0
# 社区包上限 100MB（§5.4 仓库单文件硬限，也是上传包上限）
MAX_PACKAGE_BYTES = 100 * 1024 * 1024
# 解压后总体积的下限口径；实际上限取 max(它, 包体积 × 4)（§6.4 体积表）
MIN_UNPACK_LIMIT = market_install._MAX_UNPACKED_BYTES
# 索引本身只有几十 KB，限死读取量避免被异常响应拖垮内存
_MAX_INDEX_BYTES = 8 * 1024 * 1024
_MAX_INDEX_ENTRIES = 5000
_INDEX_TIMEOUT = 20.0
_INDEX_HEADERS = {
    "User-Agent": "ChatCoder-Community/1.0 (+https://github.com/chatcoder)",
    "Accept": "application/json, text/plain, */*",
}


# ── 错误模型 ──────────────────────────────────────────────────


class CommunityError(Exception):
    """社区操作的统一错误基类。

    子类只改 `code`：路由按 code 映射 HTTP 状态码，界面按 code 决定文案与是否提示重试。
    """

    code = "NETWORK"


class CommunityNotFound(CommunityError):
    """索引里没有该条目 / 已被拉黑（404）。"""

    code = "NOT_FOUND"


class CommunityNetworkError(CommunityError):
    """三级读链路全失败或下载失败（503）。"""

    code = "NETWORK"


class CommunityIntegrityError(CommunityError):
    """包不合规：sha256 / size 不符、解压失败、包内 manifest 与索引不一致（422）。"""

    code = "INTEGRITY"


class CommunityDisabled(CommunityError):
    """社区未配置（repo 为空）。列目录不报错，其余操作给出明确原因。"""

    code = "DISABLED"


# ── 配置：仓库地址 ────────────────────────────────────────────


def _user_config_path() -> Path:
    """设置页同一份持久化配置（`gateway/routers/settings.py` 的 `_USER_CONFIG_PATH`）。

    服务层不反向 import 路由，故这里按同一规则解析（含 `CHATCODER_USER_CONFIG` 覆盖），
    保证「设置页写入的 community_repo」与「服务端读到的」永远同一个文件。
    """
    raw = (os.environ.get("CHATCODER_USER_CONFIG") or "").strip()
    if raw:
        return Path(raw)
    return Path.home() / ".chatcoder" / "config.json"


def _normalize_repo(value: Any) -> str:
    """把用户填的仓库地址收成 `owner/repo`；非法值一律当「未配置」（关闭社区）。

    允许直接粘贴 `https://github.com/owner/repo` 或带 `.git` 后缀——设置页是自由输入框，
    让人把地址原样粘进来就能用，比报一个正则错误更友好。
    """
    text = str(value or "").strip().strip("/")
    for prefix in ("https://github.com/", "http://github.com/", "github.com/"):
        if text.lower().startswith(prefix):
            text = text[len(prefix):]
            break
    if text.lower().endswith(".git"):
        text = text[:-4]
    text = text.strip("/")
    if not text:
        return ""
    if not REPO_PATTERN.match(text):
        logger.warning("[community] 仓库地址不合法，按未配置处理: %r", value)
        return ""
    return text


def repo() -> str:
    """当前社区仓库（`owner/repo`）；空字符串 = 社区关闭（§7 P5 第 8 条）。

    来源优先级：环境变量 `CHATCODER_COMMUNITY_REPO` → 运行时 settings（若已加该字段）→
    设置页落盘的 `community_repo`。环境变量存在时即使为空也以其为准，便于显式关闭/测试。
    """
    raw_env = os.environ.get(COMMUNITY_REPO_ENV)
    if raw_env is not None:
        return _normalize_repo(raw_env)
    runtime = _normalize_repo(getattr(_settings(), "community_repo", ""))
    if runtime:
        return runtime
    data = _read_json(_user_config_path(), {})
    return _normalize_repo(data.get("community_repo") if isinstance(data, dict) else "")


def _settings():
    """延迟取运行时 settings（避免与配置模块的初始化顺序耦合）。"""
    from app.core.config import settings

    return settings


def index_url() -> str:
    """社区索引的 raw 地址（§3.4；`?t=` 由 `_fetch_raw` 追加）。"""
    identifier = repo()
    if not identifier:
        raise CommunityDisabled("社区未配置仓库地址（设置 → 拓展 → 社区仓库留空即关闭社区）")
    return f"https://raw.githubusercontent.com/{identifier}/HEAD/apps.json"


# ── 本地文件：.community/ 下的缓存、身份、拉黑 ────────────────


def community_dir() -> Path:
    """`<libraryRoot>/.community/`（§6.1 目录图）。"""
    return microapp_root() / ".community"


def _cache_path() -> Path:
    return community_dir() / "cache.json"


def _identity_path() -> Path:
    return community_dir() / "identity.json"


def _blocklist_path() -> Path:
    return community_dir() / "blocklist.json"


def _read_json(path: Path, default: Any) -> Any:
    """读 JSON；不存在 / 坏内容一律返回 default（缓存与拉黑坏掉不该让功能整体不可用）。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, payload: Any) -> None:
    """原子写 JSON（tmp + replace）：写一半被杀掉不会留下无法解析的文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex[:8]}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_cache(*, allow_stale: bool) -> dict | None:
    """读索引缓存；`allow_stale=False` 时超过 TTL 视为没有缓存（§3.4 第 3 条）。"""
    data = _read_json(_cache_path(), None)
    if not isinstance(data, dict):
        return None
    apps = data.get("apps")
    if not isinstance(apps, list):
        return None
    try:
        cached_at = float(data.get("_cachedAt") or 0)
    except (TypeError, ValueError):
        cached_at = 0.0
    age = max(0.0, time.time() - cached_at)
    if not allow_stale and age > CACHE_TTL:
        return None
    return {
        "schemaVersion": int(data.get("schemaVersion") or 1),
        "updatedAt": str(data.get("updatedAt") or ""),
        "apps": apps,
        "cachedAt": cached_at,
        "ageSeconds": int(age),
    }


def _write_cache(index: dict) -> None:
    try:
        _write_json(_cache_path(), {
            "schemaVersion": int(index.get("schemaVersion") or 1),
            "updatedAt": str(index.get("updatedAt") or ""),
            "apps": index.get("apps") or [],
            "_cachedAt": time.time(),
        })
    except OSError:
        logger.debug("[community] 索引缓存写入失败（不影响本次响应）", exc_info=True)


# ── 索引解析 ──────────────────────────────────────────────────


def _normalize_entry(raw: Any) -> dict | None:
    """清洗一条索引条目；返回 None 表示该条不可用（缺关键字段 / 形状非法）。

    为什么要挑拣而不是整体失败：索引由 owner 的 Actions 写入，出现一条脏数据时
    「整页空白 + 无法解释」比「少一条 + 日志可查」难排查得多（D14 不做内容审核）。
    """
    if not isinstance(raw, dict):
        return None
    app_id = str(raw.get("appId") or "").strip()
    version = str(raw.get("version") or "").strip()
    sha256 = str(raw.get("sha256") or "").strip().lower()
    download_url = str(raw.get("downloadUrl") or "").strip()
    if not APP_ID_PATTERN.match(app_id) or not _VERSION_PATTERN.match(version):
        return None
    if not _SHA256_PATTERN.match(sha256):
        return None
    if not download_url.startswith(("https://", "http://")):
        return None
    try:
        size = int(raw.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    if size < 0 or size > MAX_PACKAGE_BYTES:
        # 超过 GitHub 单文件硬限的包本来就取不到（§5.4），提前挡掉比下载到一半失败好
        return None
    return {
        "appId": app_id,
        "name": str(raw.get("name") or ""),
        "version": version,
        "authorId": str(raw.get("authorId") or ""),
        "authorName": str(raw.get("authorName") or ""),
        "desc": str(raw.get("desc") or ""),
        "size": size,
        "sha256": sha256,
        "perms": [str(p) for p in raw.get("perms") or [] if isinstance(p, str)],
        "methods": [str(m) for m in raw.get("methods") or [] if isinstance(m, str)],
        "vendors": [v for v in raw.get("vendors") or [] if isinstance(v, dict)],
        "downloadUrl": download_url,
        # Release tag 固定为 <appId>-<version>（§6.4）；索引没写时按同口径推导
        "releaseTag": str(raw.get("releaseTag") or f"{app_id}-{version}"),
        "createdAt": str(raw.get("createdAt") or ""),
        "updatedAt": str(raw.get("updatedAt") or ""),
    }


def _parse_index(payload: bytes | str | dict) -> dict:
    """解析 apps.json（schema 见 §6.4 与 docs/community-template/apps.json）。"""
    if isinstance(payload, dict):
        data = payload
    else:
        text = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else str(payload)
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise CommunityIntegrityError(f"社区索引不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise CommunityIntegrityError("社区索引格式不正确（顶层应为对象）")
    raw_apps = data.get("apps")
    if not isinstance(raw_apps, list):
        raise CommunityIntegrityError("社区索引缺少 apps 数组")
    apps: list[dict] = []
    for raw in raw_apps[:_MAX_INDEX_ENTRIES]:
        entry = _normalize_entry(raw)
        if entry is not None:
            apps.append(entry)
    if len(apps) < len(raw_apps):
        logger.debug("[community] 索引中有 %s 条条目形状不合法已跳过", len(raw_apps) - len(apps))
    return {
        "schemaVersion": int(data.get("schemaVersion") or 1),
        "updatedAt": str(data.get("updatedAt") or ""),
        "apps": apps,
    }


# ── 网络：三级读链路 ──────────────────────────────────────────


async def _fetch_raw(identifier: str) -> bytes:
    """① raw CDN 读取（带 `?t=<epoch>` 击穿 `max-age=300`，§5.4）。"""
    from app.core.http_client import build_http_client

    url = f"https://raw.githubusercontent.com/{identifier}/HEAD/apps.json?t={int(time.time())}"
    async with build_http_client(
        timeout=_INDEX_TIMEOUT, follow_redirects=True, headers=_INDEX_HEADERS,
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return _guard_index_size(resp.content)


async def _fetch_api(identifier: str) -> bytes:
    """② GitHub Contents API 读取（内容 base64；匿名 60 次/时，§5.4）。"""
    from app.core.http_client import build_http_client

    url = f"https://api.github.com/repos/{identifier}/contents/apps.json"
    async with build_http_client(
        timeout=_INDEX_TIMEOUT, follow_redirects=True, headers=_INDEX_HEADERS,
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()
    if not isinstance(data, dict):
        raise CommunityNetworkError("Contents API 返回了非预期的数据格式")
    encoding = str(data.get("encoding") or "")
    content = str(data.get("content") or "")
    if encoding != "base64" or not content:
        raise CommunityNetworkError("Contents API 未返回可解码的 apps.json 内容")
    try:
        # GitHub 返回的 base64 带换行；b64decode 默认忽略非字母表字符
        return _guard_index_size(base64.b64decode(content))
    except (ValueError, TypeError) as exc:
        raise CommunityNetworkError(f"apps.json base64 解码失败：{exc}") from exc


def _guard_index_size(payload: bytes) -> bytes:
    if not payload:
        raise CommunityNetworkError("社区索引内容为空")
    if len(payload) > _MAX_INDEX_BYTES:
        raise CommunityNetworkError(
            f"社区索引过大（{len(payload) // 1024}KB > {_MAX_INDEX_BYTES // 1024}KB 上限）"
        )
    return payload


async def fetch_index(*, force: bool = False) -> dict:
    """取索引：TTL 内直接用缓存，否则按 ①→②→③ 降级（§3.4）。

    返回 `{schemaVersion, updatedAt, apps, stale, source, repo, fetchedAt}`；
    `stale=True` 表示数据来自过期缓存（界面要提示「可能不是最新」）。
    `force=True` 跳过 TTL 直接走网络（安装前刷新、或用户点「刷新」时用）。
    """
    identifier = repo()
    if not identifier:
        raise CommunityDisabled("社区未配置仓库地址（设置 → 拓展 → 社区仓库留空即关闭社区）")

    if not force:
        cached = _read_cache(allow_stale=False)
        if cached is not None:
            return {**cached, "stale": False, "source": "cache", "repo": identifier,
                    "fetchedAt": cached["cachedAt"]}

    failures: list[str] = []
    for source, fetcher in (("raw", _fetch_raw), ("api", _fetch_api)):
        try:
            payload = await fetcher(identifier)
            index = _parse_index(payload)
        except CommunityIntegrityError as exc:
            # 索引内容坏掉了也继续降级：owner 手滑推送坏 JSON 时，用户应看到上一次好数据
            failures.append(f"{source}: {exc}")
        except Exception as exc:  # noqa: BLE001 - 读链路任何失败都必须降级，不能漏给界面
            failures.append(f"{source}: {exc}")
        else:
            _write_cache(index)
            logger.info("[community] 索引已更新 source=%s apps=%s", source, len(index["apps"]))
            return {**index, "stale": False, "source": source, "repo": identifier,
                    "fetchedAt": time.time()}

    stale = _read_cache(allow_stale=True)
    if stale is not None:
        logger.warning("[community] 远端索引不可用，降级用本地缓存（%s）；失败原因：%s",
                       f"{stale['ageSeconds']}s 前的缓存", "; ".join(failures))
        return {**stale, "stale": True, "source": "cache", "repo": identifier,
                "fetchedAt": stale["cachedAt"]}

    raise CommunityNetworkError("无法连接社区：" + "; ".join(failures))


# ── 身份与拉黑（§6.4 / §7 P5 第 6 条）────────────────────────


def identity() -> dict:
    """本机社区身份 `<libraryRoot>/.community/identity.json`：不存在则生成 `usr_<12 hex>`。

    作者标识不可验证（README「几条约定」），它的作用是让用户能按作者拉黑，而不是鉴权。
    """
    data = _read_json(_identity_path(), {})
    user_id = str(data.get("userId") or "") if isinstance(data, dict) else ""
    if _USER_ID_PATTERN.match(user_id):
        return {"userId": user_id, "createdAt": str(data.get("createdAt") or "")}
    created = _now_iso()
    user_id = f"usr_{uuid.uuid4().hex[:12]}"
    _write_json(_identity_path(), {"userId": user_id, "createdAt": created})
    logger.info("[community] 已生成本机社区身份 %s", user_id)
    return {"userId": user_id, "createdAt": created}


def _clean_users(values: Any) -> list[str]:
    """作者黑名单清洗：去空白、去重、保序；畸形项丢弃。"""
    out: list[str] = []
    for raw in values or []:
        text = str(raw or "").strip()
        if text and text not in out and len(text) <= 64:
            out.append(text)
    return out


def _clean_apps(values: Any) -> list[str]:
    """条目黑名单清洗：只留合法 appId（脏值会让过滤逻辑静默失效）。"""
    out: list[str] = []
    for raw in values or []:
        text = str(raw or "").strip()
        if APP_ID_PATTERN.match(text) and text not in out:
            out.append(text)
    return out


def get_blocklist() -> dict:
    """读拉黑表：`users`（按 authorId 整作者屏蔽）、`apps`（按 appId 屏蔽单条）。"""
    data = _read_json(_blocklist_path(), {})
    if not isinstance(data, dict):
        data = {}
    return {"users": _clean_users(data.get("users")), "apps": _clean_apps(data.get("apps"))}


def update_blocklist(*, users: Any = None, apps: Any = None) -> dict:
    """写拉黑表（PUT 语义）：传了哪一档就整档替换，没传的档保持原样。

    `users=[]` / `apps=[]` 是「清空该档」的显式表达，与「不传」（保持）区分开——
    设置页勾选/取消都是一次全量提交，两者混起来会让清除操作看起来"没生效"。
    拉黑只影响社区列表与安装，不影响已装本地副本（§6.4 拉黑一节）。
    """
    current = get_blocklist()
    merged = {
        "users": current["users"] if users is None else _clean_users(users),
        "apps": current["apps"] if apps is None else _clean_apps(apps),
    }
    _write_json(_blocklist_path(), merged)
    return merged


def _is_blocked(entry: dict, blocklist: dict) -> bool:
    """两档拉黑判定：命中作者整作者屏蔽，命中 appId 只屏蔽该条目（§6.4）。"""
    author = str(entry.get("authorId") or "")
    by_author = bool(author) and author in blocklist["users"]
    by_app = entry.get("appId") in blocklist["apps"]
    return by_author or by_app


def _visible_apps(apps: list[dict]) -> list[dict]:
    """过滤拉黑条目（两档，§6.4）。"""
    blocklist = get_blocklist()
    if not blocklist["users"] and not blocklist["apps"]:
        return list(apps)
    return [entry for entry in apps if not _is_blocked(entry, blocklist)]


# ── 列表 / 详情 ───────────────────────────────────────────────


def _entry_dict(entry: dict) -> dict:
    """对外的条目形状（前端直接用；字段与 §6.4 索引条目一致）。"""
    return dict(entry)


async def list_apps(query: str = "", *, force: bool = False) -> dict:
    """社区列表：读索引 → 过滤拉黑 → name/appId/desc 模糊匹配（§6.4）。

    社区未配置时返回 `{apps: [], enabled: False}` 而不是报错——入口隐藏由前端按
    `enabled` 决定（§7 P5 第 8 条）。
    """
    identifier = repo()
    if not identifier:
        return {"apps": [], "enabled": False, "stale": False, "repo": "", "query": query.strip(),
                "count": 0}

    index = await fetch_index(force=force)
    apps = _visible_apps(index["apps"])
    keyword = query.strip().lower()
    if keyword:
        apps = [
            entry for entry in apps
            if keyword in entry["appId"].lower()
            or keyword in entry["name"].lower()
            or keyword in entry["desc"].lower()
        ]
    return {
        "apps": [_entry_dict(entry) for entry in apps],
        "enabled": True,
        "stale": bool(index["stale"]),
        "source": index["source"],
        "repo": identifier,
        "updatedAt": index.get("updatedAt", ""),
        "staleSeconds": index.get("ageSeconds", 0),
        "query": query.strip(),
        "count": len(apps),
    }


def _find_entry(apps: list[dict], app_id: str, version: str) -> dict | None:
    for entry in apps:
        if entry["appId"] == app_id and entry["version"] == version:
            return entry
    return None


def _check_app_id(app_id: str) -> str:
    """appId 校验：目录名 / Release tag / 正则三方共用同一口径（D26）。"""
    value = str(app_id or "").strip()
    if not APP_ID_PATTERN.match(value):
        raise CommunityNotFound(f"appId 非法：{value!r}（须匹配 {APP_ID_PATTERN.pattern}）")
    return value


def _check_version(version: str) -> str:
    value = str(version or "").strip()
    if not _VERSION_PATTERN.match(value):
        raise CommunityNotFound(f"版本号非法：{value!r}（期望 1.2.3 形式，§6.4）")
    return value


async def get_app(app_id: str, version: str) -> dict:
    """详情：索引条目 + 本机是否已装（不代表已注入/已启用）。"""
    identifier = repo()
    if not identifier:
        raise CommunityDisabled("社区未配置仓库地址（设置 → 拓展 → 社区仓库留空即关闭社区）")
    app_id = _check_app_id(app_id)
    version = _check_version(version)

    index = await fetch_index()
    entry = _find_entry(index["apps"], app_id, version)
    if entry is None:
        raise CommunityNotFound(f"社区里没有 {app_id}@{version}（可能已撤回，稍后刷新再看）")
    if _is_blocked(entry, get_blocklist()):
        raise CommunityNotFound(f"{app_id}@{version} 已被拉黑，先在设置里解除拉黑再查看")

    install_dir = microapp_root() / app_id / version
    return {
        "app": _entry_dict(entry),
        "enabled": True,
        "stale": bool(index["stale"]),
        "repo": identifier,
        "installed": install_dir.is_dir(),
        "installPath": str(install_dir) if install_dir.is_dir() else "",
    }


# ── 安装（§6.4 / §7 P5 第 3 条）──────────────────────────────


async def _download_package(entry: dict, dest: Path) -> int:
    """下载社区包到临时文件，返回字节数。

    复用 `market_install._download`：流式写盘 + 体积上限（105MB，§6.4 体积表）+
    sha256 校验 + 失败自动清理半成品。异常在这里收口成结构化错误：
    网络问题 → NETWORK，包不合规（哈希/体积/空内容）→ INTEGRITY。
    """
    url = entry["downloadUrl"]
    try:
        return await market_install._download(url, dest, expect_sha256=entry["sha256"])
    except ValueError as exc:
        # _download 内部已 unlink 半成品，库里不会留下坏包
        raise CommunityIntegrityError(f"社区包校验失败：{exc}") from exc
    except httpx.HTTPError as exc:
        raise CommunityNetworkError(f"下载社区包失败：{exc}") from exc


def _find_package_root(base: Path) -> Path:
    """定位包内容根：manifest.json 所在层（多套一层的包也能装）。"""
    return market_install._find_root(base, ("manifest.json",))


def _read_package_manifest(root: Path, app_id: str, version: str) -> dict:
    """校验包内 manifest 与索引条目一致。

    这一步是防投毒的关键：索引说装 A，而包里装的是 B（或另一个 appId）时，
    落库后的目录名与内容会对不上，`microapp_call` 的 appId 校验会形同虚设。
    """
    manifest_file = root / "manifest.json"
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CommunityIntegrityError(f"社区包内 manifest.json 缺失或无法解析：{exc}") from exc
    if not isinstance(manifest, dict):
        raise CommunityIntegrityError("社区包内 manifest.json 不是 JSON 对象")
    declared_id = str(manifest.get("appId") or "")
    declared_version = str(manifest.get("version") or "")
    if declared_id != app_id:
        raise CommunityIntegrityError(
            f"社区包与索引不一致：索引 appId={app_id}，包内 appId={declared_id or '(缺失)'}"
        )
    if declared_version != version:
        raise CommunityIntegrityError(
            f"社区包与索引不一致：索引 version={version}，"
            f"包内 version={declared_version or '(缺失)'}"
        )
    return manifest


def _commit_install(stage: Path, target: Path) -> None:
    """把解压好的目录原子改名为 `<appId>/<version>`（同盘 rename）。

    plan-693-2868 §6.4：先解到临时目录再改名——中途失败时库里不会留下半截版本目录，
    否则前端看到的是「装了但打不开」的条目。同版本重装时把旧目录挪开再换入，
    任一 rename 失败都回滚，避免把用户已装的那份弄丢。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    retired: Path | None = None
    if target.exists():
        retired = target.with_name(f".{target.name}.old-{uuid.uuid4().hex[:8]}")
        target.rename(retired)
    try:
        stage.rename(target)
    except OSError:
        if retired is not None:
            retired.rename(target)
        raise
    if retired is not None:
        shutil.rmtree(retired, ignore_errors=True)


async def install_app(app_id: str, version: str) -> dict:
    """从社区安装一个微应用：下载 → 校验 → 解压 → 落库 → 写 `.origin.json`。

    **不做任何"启用"动作**：微应用没有数据库行，「装完即可注入」，是否注入由用户在前端
    决定（§6.4「默认不启用」）。返回值里的 `enabled: False` 就是这个语义，不是失败。
    """
    identifier = repo()
    if not identifier:
        raise CommunityDisabled("社区未配置仓库地址（设置 → 拓展 → 社区仓库留空即关闭社区）")
    app_id = _check_app_id(app_id)
    version = _check_version(version)

    index = await fetch_index()
    entry = _find_entry(index["apps"], app_id, version)
    if entry is None:
        raise CommunityNotFound(f"社区里没有 {app_id}@{version}（可能已撤回，稍后刷新再看）")
    if _is_blocked(entry, get_blocklist()):
        raise CommunityNotFound(f"{app_id}@{version} 已被拉黑，先在设置里解除拉黑再安装")

    target = microapp_root() / app_id / version
    tmp_base = community_dir() / "tmp"
    tmp_base.mkdir(parents=True, exist_ok=True)

    # 临时目录放在库根内（.community/tmp）：与目标同盘，最后的 rename 才是原子的
    with tempfile.TemporaryDirectory(prefix=f"{app_id}-", dir=tmp_base) as tmp:
        tmp_dir = Path(tmp)
        zip_path = tmp_dir / "package.ccapp"
        written = await _download_package(entry, zip_path)

        declared_size = int(entry.get("size") or 0)
        if declared_size and written != declared_size:
            raise CommunityIntegrityError(
                f"包体积与索引不符（实际 {written} 字节，索引声明 {declared_size} 字节）"
            )

        unpack_dir = tmp_dir / "unpacked"
        unpack_dir.mkdir()
        # §6.4 体积表：解压上限 max(300MB, 包体积 × 4)
        limit = max(MIN_UNPACK_LIMIT, written * 4)
        try:
            await asyncio.to_thread(
                market_install._extract_zip, zip_path, unpack_dir, max_unpacked_bytes=limit,
            )
        except ValueError as exc:
            raise CommunityIntegrityError(f"社区包解压失败：{exc}") from exc

        root = await asyncio.to_thread(_find_package_root, unpack_dir)
        await asyncio.to_thread(_read_package_manifest, root, app_id, version)

        origin = {
            "source": "community",
            "repo": identifier,
            "releaseTag": entry.get("releaseTag") or f"{app_id}-{version}",
            "sha256": entry["sha256"],
            "size": declared_size or written,
            "authorId": entry.get("authorId") or "",
            "installedAt": _now_iso(),
        }
        # .origin.json 先写进 staging 再整体改名：库里的版本目录要么完整带来源记录，要么不存在
        (root / ".origin.json").write_text(
            json.dumps(origin, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        await asyncio.to_thread(_commit_install, root, target)

    logger.info("[community] 安装完成 %s@%s bytes=%s repo=%s",
                app_id, version, written, identifier)
    return {
        "ok": True,
        "appId": app_id,
        "version": version,
        "path": str(target),
        "bytes": written,
        "sha256": entry["sha256"],
        "repo": identifier,
        "origin": origin,
        # 默认不启用：装完不注入，等用户在前端选择（§6.4）
        "enabled": False,
        "message": "已安装到微应用库；默认不启用，需要到拓展页手动注入",
    }
