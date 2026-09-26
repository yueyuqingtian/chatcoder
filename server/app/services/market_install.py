"""市场内容安装（plan-41-225 S6）：详情接口 → 下载/提取 → 安全落盘 → 注册。

**协议（2026-09-25 实测固化）**：详情数据走可直连的 API（无需浏览器）：

    技能  GET /apphub/api/v1/marketplace/skills/<id>/detail
    插件  GET /apphub/api/v1/marketplace/plugins/<id>/detail
    连接器 GET /apphub/api/v1/marketplace/connectors/<id>/detail

三类详情分别给出安装所需的全部信息：

    · 技能：`download_url`（OSS 直链 zip）+ `skill_name` + `readme_content`（正文兜底）
    · 插件：`download_url`（OSS 直链 zip）+ `file_hash`（sha256）+ `skills[]`（贡献技能）
    · 连接器：`config.servers[]`（`name` / `url` / `protocol` / `qoder_url`）——无包可下，
      本质是写一条 MCP 服务器配置

**为什么经后端而非前端**：统一走 `build_http_client()` 以复用用户在设置里配的全局代理；
下载与落盘本来就在后端；且详情接口需要固定 UA/Referer（浏览器直连会被 CORS/UA 限制）。

**安全约束**（本机要执行/加载别人上传的包，必须假设包是敌意的）：
    · zip-slip 防护：逐条校验解压落点 realpath 必须在目标目录内，拒绝绝对路径与 `..`；
    · 条目数与解压后总体积上限（防 zip bomb）、下载体积上限；
    · 只写普通文件（不还原符号链接）；可选 sha256 校验（详情给了 `file_hash`）。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

HOST = "https://qoder.com.cn"


def _safe_market_icon(value) -> str:
    """只保存安全的 HTTPS 市场图标地址。"""
    url = str(value or "").strip()
    return url if url.startswith("https://") else ""


_DETAIL_PATH = {
    "skill": "/apphub/api/v1/marketplace/skills/{id}/detail",
    "plugin": "/apphub/api/v1/marketplace/plugins/{id}/detail",
    "connector": "/apphub/api/v1/marketplace/connectors/{id}/detail",
}
# 技能包地址回退规则（来自市场详情页的 CLI 安装命令；详情已给 download_url 时优先用它）
MARKET_CDN = "https://qoder-cn-mind.oss-accelerate.aliyuncs.com"
# 技能落盘根目录（沿用项目既有 ~/.chatcoder/ 约定与 CHATCODER_* 覆盖惯例）
SKILLS_ROOT = Path(
    os.environ.get("CHATCODER_SKILLS_DIR", str(Path.home() / ".chatcoder" / "skills"))
)

_TIMEOUT = 60.0
_DOWNLOAD_TIMEOUT = 180.0
_MAX_DOWNLOAD_BYTES = 80 * 1024 * 1024      # 单包 80MB（实测插件包 ~2MB，留足余量）
_MAX_ENTRIES = 4000                          # zip 条目数上限
_MAX_UNPACKED_BYTES = 300 * 1024 * 1024      # 解压后总体积上限
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://qoder.com.cn/marketplace",
}
# 连接器 protocol → 本机 transport（项目既有取值：stdio / sse / websocket / http）
_TRANSPORT_MAP = {
    "streamable_http": "http",
    "http": "http",
    "sse": "sse",
    "stdio": "stdio",
    "websocket": "websocket",
}


# ── 详情 ──────────────────────────────────────────────────────

async def fetch_detail(kind: str, ident: str) -> dict:
    """取市场详情（三类同构入口）。远端异常时抛出可读错误。"""
    path_tpl = _DETAIL_PATH.get(kind)
    if not path_tpl:
        raise ValueError("kind 必须为 plugin / skill / connector")
    ident = (ident or "").strip()
    if not ident:
        raise ValueError("缺少条目 id")

    from app.core.http_client import build_http_client

    url = HOST + path_tpl.format(id=ident)
    async with build_http_client(timeout=_TIMEOUT, follow_redirects=True, headers=_HEADERS) as client:
        resp = await client.get(url)
        if resp.status_code == 404:
            raise ValueError("该条目在市场上不存在或已下架")
        resp.raise_for_status()
        data = resp.json()
    if not isinstance(data, dict):
        raise ValueError("市场返回了非预期的数据格式")
    return data


# ── 下载与安全解压 ────────────────────────────────────────────

async def _download(url: str, dest: Path, *, expect_sha256: str = "") -> int:
    """流式下载（带体积与哈希校验）。超限/失败即清理半成品。"""
    from app.core.http_client import build_http_client

    written = 0
    digest = hashlib.sha256()
    try:
        async with build_http_client(
            timeout=_DOWNLOAD_TIMEOUT, follow_redirects=True, headers=_HEADERS
        ) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                declared = int(resp.headers.get("content-length") or 0)
                if declared and declared > _MAX_DOWNLOAD_BYTES:
                    raise ValueError(
                        f"包体积过大（{declared // 1024 // 1024}MB > "
                        f"{_MAX_DOWNLOAD_BYTES // 1024 // 1024}MB 上限）"
                    )
                with dest.open("wb") as fh:
                    async for chunk in resp.aiter_bytes():
                        written += len(chunk)
                        if written > _MAX_DOWNLOAD_BYTES:
                            raise ValueError("包体积超过上限，已中止下载")
                        digest.update(chunk)
                        fh.write(chunk)
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    if written == 0:
        dest.unlink(missing_ok=True)
        raise ValueError("下载内容为空")
    if expect_sha256 and digest.hexdigest() != expect_sha256.lower():
        dest.unlink(missing_ok=True)
        raise ValueError("包完整性校验失败（sha256 不匹配）")
    return written


def _extract_zip(zip_path: Path, dest: Path) -> None:
    """安全解压（同步实现，调用方放线程执行）。

    逐条校验：拒绝绝对路径与 `..`，并用 realpath 再次确认落点在 dest 内——
    只做字符串前缀判断会被 `a/../../b` 之类的构造绕过。
    """
    dest_resolved = dest.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()
        if len(infos) > _MAX_ENTRIES:
            raise ValueError(f"压缩包条目过多（{len(infos)} > {_MAX_ENTRIES}）")
        total = sum(int(i.file_size) for i in infos)
        if total > _MAX_UNPACKED_BYTES:
            raise ValueError("解压后体积超过上限，已中止")
        for info in infos:
            name = info.filename
            if not name or name.startswith(("/", "\\")) or ".." in Path(name).parts:
                raise ValueError(f"压缩包内存在非法路径：{name}")
            target = (dest / name).resolve()
            if os.path.commonpath([str(target), str(dest_resolved)]) != str(dest_resolved):
                raise ValueError(f"压缩包内路径越界：{name}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)


def _find_root(base: Path, markers: tuple[str, ...], max_depth: int = 2) -> Path:
    """在解压结果里定位内容根：优先自身命中，否则按深度广度找命中标记的目录。"""
    def hit(d: Path) -> bool:
        return any((d / m).exists() for m in markers)

    if hit(base):
        return base
    frontier = [base]
    for _ in range(max_depth):
        nxt: list[Path] = []
        for d in frontier:
            if not d.is_dir():
                continue
            for child in sorted(d.iterdir()):
                if child.is_dir():
                    if hit(child):
                        return child
                    nxt.append(child)
        frontier = nxt
    return base


# ── 三类安装 ──────────────────────────────────────────────────

async def install_skill(db, *, ident: str, display_name: str = "",
                        description: str = "") -> dict:
    """安装市场技能：下载技能包 → 安全解压 → 落盘 → 入库（source=qoder）。

    兜底顺序：`download_url` → 按名字推导规则地址；两条都失败但有 `readme_content`
    时直接以正文入库（技能本质是 SKILL.md，正文在手即可用）。
    """
    detail = await fetch_detail("skill", ident)
    name = str(detail.get("skill_name") or detail.get("display_name") or "").strip()
    if not name or "/" in name or "\\" in name or ".." in name:
        raise ValueError("市场返回的技能名非法，无法安装")
    display = display_name or str(detail.get("skill_name_cn") or detail.get("display_name_cn") or "")
    desc = description or str(detail.get("description_cn") or detail.get("description") or "")

    target_dir = SKILLS_ROOT / "qoder" / name
    package_url = str(detail.get("download_url") or "") or f"{MARKET_CDN}/skills/public/{name}/latest/{name}.zip"
    downloaded = 0
    try:
        with tempfile.TemporaryDirectory(prefix="chatcoder-skill-") as tmp:
            tmp_dir = Path(tmp)
            zip_path = tmp_dir / "package.zip"
            downloaded = await _download(package_url, zip_path)
            extract_dir = tmp_dir / "unpacked"
            extract_dir.mkdir(parents=True, exist_ok=True)
            # 解压是 CPU/磁盘密集的同步调用，放线程避免阻塞事件循环
            await asyncio.to_thread(_extract_zip, zip_path, extract_dir)
            root = _find_root(extract_dir, ("SKILL.md",) + tuple(
                str(f.get("name")) for f in ((detail.get("file_tree") or {}).get("files") or [])
                if isinstance(f, dict) and f.get("name")
            ))
            if not (root / "SKILL.md").is_file():
                raise ValueError("技能包内未找到 SKILL.md")
            if target_dir.exists():
                await asyncio.to_thread(shutil.rmtree, target_dir, True)
            target_dir.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(shutil.copytree, root, target_dir)
    except Exception as exc:  # noqa: BLE001 - 下载/解压失败时尝试正文兜底
        readme = str(detail.get("readme_content") or "")
        if not readme.strip():
            raise ValueError(f"技能包下载或解压失败：{exc}") from exc
        logger.warning("[market] 技能包不可用，改用详情正文入库 name=%s err=%s", name, exc)
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "SKILL.md").write_text(readme, encoding="utf-8")
        downloaded = len(readme.encode("utf-8"))

    row_id, updated = await _upsert_skill_row(
        db, name=name, display_name=display or name, description=desc,
        path=str(target_dir), extra={
            "market_id": ident, "package_url": package_url,
            "market_icon_url": _safe_market_icon(detail.get("icon_url")),
        },
    )
    logger.info("[market] 技能安装完成 name=%s bytes=%s dir=%s", name, downloaded, target_dir)
    return {
        "ok": True, "kind": "skill", "name": name, "path": str(target_dir),
        "skillId": row_id, "updated": updated, "bytes": downloaded,
    }


async def install_plugin(db, *, ident: str, display_name: str = "",
                         description: str = "") -> dict:
    """安装市场插件：下载插件包 → 安全解压 → 复用既有插件安装流程落盘并注册技能。

    复用 `plugin_service` 的三步（复制到 plugins 目录 / 写注册表 / 注册贡献技能），
    保证市场安装与本机目录安装**结果完全一致**，不产生第二套插件管理逻辑。
    """
    detail = await fetch_detail("plugin", ident)
    name = str(detail.get("plugin_name") or detail.get("display_name") or "").strip()
    if not name or "/" in name or "\\" in name or ".." in name:
        raise ValueError("市场返回的插件名非法，无法安装")
    package_url = str(detail.get("download_url") or "")
    if not package_url:
        raise ValueError("市场未提供该插件的下载地址，请改用「从本地目录 / Git 安装」")

    from app.services import plugin_service

    with tempfile.TemporaryDirectory(prefix="chatcoder-plugin-") as tmp:
        tmp_dir = Path(tmp)
        zip_path = tmp_dir / "package.zip"
        size = await _download(package_url, zip_path,
                               expect_sha256=str(detail.get("file_hash") or ""))
        extract_dir = tmp_dir / "unpacked"
        extract_dir.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(_extract_zip, zip_path, extract_dir)
        # 插件根：含 plugin.json（或其变体）或 skills/ 目录的层
        root = _find_root(extract_dir, (
            "plugin.json", ".qoder-plugin/plugin.json",
            ".chatcoder-plugin/plugin.json", ".claude-plugin/plugin.json", "skills",
        ))
        dest, manifest = await asyncio.to_thread(plugin_service._install_files, root, "qoder")
        market = str(manifest.get("marketplaceName") or "qoder")
        manifest["logo"] = _safe_market_icon(detail.get("icon_url"))
        manifest["marketId"] = ident
        plugin_service._record_install(str(manifest["name"]), market, dest, manifest,
                                      source="qoder-market")
        skills_n = await plugin_service._register_contributed_skills(
            db, str(manifest["name"]), dest, manifest
        )
        # plan-59-286：插件声明的 MCP 一并登记为连接器（默认未启用），
        # 与本机目录安装保持一致；否则"市场装的插件带连接器"在拓展页不可见。
        connectors_n = await plugin_service._register_contributed_mcp(
            db, str(manifest["name"]), dest, manifest
        )

    installed_name = str(manifest["name"])
    logger.info("[market] 插件安装完成 name=%s skills=%s connectors=%s bytes=%s",
                installed_name, skills_n, connectors_n, size)
    return {
        "ok": True, "kind": "plugin", "name": installed_name, "path": str(dest),
        "skills": skills_n, "connectorCount": connectors_n, "bytes": size,
        "displayName": display_name or str(detail.get("display_name_cn") or ""),
        "description": description or str(detail.get("description_cn") or ""),
    }


async def install_connector(db, *, ident: str) -> dict:
    """安装市场连接器：把详情里的 MCP 服务器配置写入本机（默认**不启用**）。

    连接器没有包可下载——它的"安装"就是把远端给出的 MCP 服务（url / transport）
    登记到本机，由用户在扩展管理里决定是否启用。默认不启用是安全选择：
    远端 MCP 会让请求发往第三方服务，应由用户显式开启。
    """
    detail = await fetch_detail("connector", ident)
    config = detail.get("config") if isinstance(detail.get("config"), dict) else {}
    servers = config.get("servers") if isinstance(config.get("servers"), list) else []
    if not servers:
        raise ValueError("市场未提供该连接器的 MCP 配置")

    added: list[str] = []
    from sqlalchemy import select

    from app.persistence.database import run_write_locked
    from app.persistence.models.skill import McpServer

    for raw in servers:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or detail.get("connector_name") or "").strip()
        url = str(raw.get("url") or "").strip()
        if not name or not url:
            continue
        protocol = str(raw.get("protocol") or "")
        transport = _TRANSPORT_MAP.get(protocol, "http")
        desc = str(detail.get("description_cn") or detail.get("description") or "")

        def patch(s, _name=name, _url=url, _transport=transport, _desc=desc,
                  _protocol=protocol, _raw=raw, _ident=ident,
                  _icon=_safe_market_icon(detail.get("icon_url"))):
            existing = s.execute(select(McpServer).where(McpServer.name == _name)).scalars().first()
            if existing is not None:
                # 幂等：已存在则刷新地址与配置，但**不动** is_active（尊重用户当前选择）
                existing.url = _url
                existing.transport = _transport
                existing.description = _desc or existing.description
                existing.meta = {**(existing.meta or {}), "market": "qoder",
                                 "market_id": _ident, "market_icon_url": _icon,
                                 "protocol": _protocol,
                                 "qoder_url": str(_raw.get("qoder_url") or "")}
            else:
                s.add(McpServer(
                    name=_name, display_name=_name, description=_desc,
                    source="qoder", transport=_transport, url=_url,
                    is_active=False,
                    meta={"market": "qoder", "market_id": _ident,
                          "market_icon_url": _icon, "protocol": _protocol,
                          "qoder_url": str(_raw.get("qoder_url") or "")},
                ))
            s.commit()
            return _name

        added.append(await run_write_locked(patch, label=f"market.connector.{name}"))

    if not added:
        raise ValueError("该连接器的配置缺少必要字段，无法安装")
    logger.info("[market] 连接器安装完成 connectors=%s", added)
    return {
        "ok": True, "kind": "connector", "name": added[0], "connectors": added,
        "transport": _TRANSPORT_MAP.get(str((servers[0] or {}).get("protocol") or ""), "http"),
        "enabled": False,   # 默认不启用，需用户到扩展管理开启
    }


async def _upsert_skill_row(db, *, name: str, display_name: str, description: str,
                            path: str, extra: dict) -> tuple[int, bool]:
    """写入/更新技能行。同名被他源占用时加 `qoder:` 前缀，避免覆盖用户已有技能。"""
    from sqlalchemy import select

    from app.persistence.database import run_write_locked
    from app.persistence.models.skill import Skill

    def patch(s):
        row_name = name
        existing = s.execute(select(Skill).where(Skill.name == row_name)).scalars().first()
        if existing is not None and (existing.source or "") != "qoder":
            row_name = f"qoder:{name}"
            existing = s.execute(select(Skill).where(Skill.name == row_name)).scalars().first()
        if existing is not None:
            existing.source = "qoder"
            existing.path = path
            existing.display_name = display_name or existing.display_name or row_name
            existing.description = description or existing.description
            existing.meta = {**(existing.meta or {}), **extra}
            row_id, updated = existing.id, True
        else:
            row = Skill(
                name=row_name, display_name=display_name or row_name,
                description=description, source="qoder", path=path,
                is_active=True, auto_load=True, meta=dict(extra),
            )
            s.add(row)
            s.flush()
            row_id, updated = row.id, False
        s.commit()
        return row_id, updated

    return await run_write_locked(patch, label=f"market.skill.{name}")


async def install_item(db, *, kind: str, ident: str, display_name: str = "",
                       description: str = "") -> dict:
    """按类型分派安装（供 market_service 调用）。"""
    if kind == "skill":
        return await install_skill(db, ident=ident, display_name=display_name,
                                   description=description)
    if kind == "plugin":
        return await install_plugin(db, ident=ident, display_name=display_name,
                                    description=description)
    if kind == "connector":
        return await install_connector(db, ident=ident)
    raise ValueError("kind 必须为 plugin / skill / connector")
