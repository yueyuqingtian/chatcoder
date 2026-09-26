"""插件服务（plan-282-1441 #6 拓展中心）。

设计对齐参考项目的插件体系，但只依赖本地文件与内置目录（不接线上市场接口）：

- 目录布局：`~/.chatcoder/plugins/<market>/<name>/`
- 清单文件：`plugin.json`，字段与参考项目同构：
  `name / displayName / version / description / descriptionZh / author /
   keywords / category / tags / skills / logo / marketplaceName / defaultEnabled`
- 注册表：`~/.chatcoder/plugins/installed_plugins.json`
  `{ "plugins": { "<name>@<market>": [{ enabled, installPath, version, source,
     installedAt, displayName }] }, "version": 2 }`
- 插件贡献：
  - `skills/*.md`（或 `skills/<name>/SKILL.md`）→ 以 source="plugin" 注册进技能表，
    在「拓展 → 技能」中可见、可启停；
  - `mcp.json`（可选）→ 其 MCP 以 source="plugin" 出现在「拓展 → 连接器」，
    默认不启用，需用户手动开启。

安装来源：① 本地目录（校验含 plugin.json）；② Git 仓库（浅克隆后按其 plugin.json 安装）。
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import time
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

_PLUGINS_ROOT = Path.home() / ".chatcoder" / "plugins"
_REGISTRY_FILE = _PLUGINS_ROOT / "installed_plugins.json"
_GIT_TIMEOUT = 120
# plan-59-286：插件根目录下可能存在的 MCP 配置文件（与 plugin.json#mcpServers 二选一或并存）
_MCP_FILES = (".mcp.json", "mcp.json")
# 远端 MCP 的 type/protocol → 本机 transport（与 market_install 同口径）
_MCP_TRANSPORT = {
    "streamable_http": "http", "http": "http", "sse": "sse",
    "websocket": "websocket", "stdio": "stdio",
}


def _root() -> Path:
    _PLUGINS_ROOT.mkdir(parents=True, exist_ok=True)
    return _PLUGINS_ROOT


def _read_registry() -> dict:
    if not _REGISTRY_FILE.exists():
        return {"plugins": {}, "version": 2}
    try:
        data = json.loads(_REGISTRY_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"plugins": {}, "version": 2}
        data.setdefault("plugins", {})
        data["version"] = 2
        return data
    except (OSError, json.JSONDecodeError):
        logger.warning("[plugin] 注册表损坏，重建", exc_info=True)
        return {"plugins": {}, "version": 2}


def _write_registry(data: dict) -> None:
    _root()
    _REGISTRY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _key(name: str, market: str) -> str:
    return f"{name}@{market}"


def _read_manifest(dir_path: Path) -> dict | None:
    """读取并校验 plugin.json。缺失或非法返回 None。"""
    mf = dir_path / "plugin.json"
    if not mf.is_file():
        return None
    try:
        data = json.loads(mf.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not str(data.get("name") or "").strip():
        return None
    return data


def _manifest_to_out(manifest: dict, entry: dict | None, dir_path: Path) -> dict:
    author = manifest.get("author")
    if isinstance(author, dict):
        author = author.get("name")
    return {
        "name": str(manifest.get("name") or ""),
        "displayName": str(manifest.get("displayName") or manifest.get("name") or ""),
        "version": str(manifest.get("version") or ""),
        "description": str(manifest.get("description") or ""),
        "descriptionZh": str(manifest.get("descriptionZh") or ""),
        "author": str(author or ""),
        "category": str(manifest.get("category") or "其他"),
        "tags": [str(t) for t in (manifest.get("tags") or [])],
        "keywords": [str(t) for t in (manifest.get("keywords") or [])],
        "marketplaceName": str(manifest.get("marketplaceName") or "local"),
        # plan-284-1450：磁盘 manifest 不含市场图标与远端 id（只在安装时才知道），
        # 故优先取注册表记录，再回退 manifest —— 否则扩展管理画不出真实图标。
        "logo": str(manifest.get("logo") or (entry or {}).get("logo") or ""),
        "marketId": str(manifest.get("marketId") or (entry or {}).get("marketId") or ""),
        "skillsDir": str(manifest.get("skills") or ""),
        "path": str(dir_path),
        "installed": entry is not None,
        "enabled": bool(entry.get("enabled")) if entry else False,
        "installedAt": (entry or {}).get("installedAt"),
    }


def list_installed() -> list[dict]:
    """已安装插件列表（读注册表 + 各目录 plugin.json）。"""
    reg = _read_registry()
    out: list[dict] = []
    for key, entries in (reg.get("plugins") or {}).items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            ip = entry.get("installPath")
            if not ip:
                continue
            d = Path(ip)
            manifest = _read_manifest(d) or {"name": key.split("@")[0],
                                             "displayName": entry.get("displayName") or key}
            out.append(_manifest_to_out(manifest, entry, d))
    return out


def _catalog_path() -> Path:
    """定位内置插件目录文件（保留作为**离线兜底**，不再是市场主数据源）。

    plan-282-1441（#6 二次修复）：市场改为展示**从本机真实扫描到的插件**
    （参考项目的 qoder/claude/cursor/trae/codebuddy 等），内置目录只在本机
    一个插件都扫不到时兜底，避免市场空空如也。打包后模块进 PYZ 归档，
    `Path(__file__)` 不是真实磁盘路径，故按项目既有口径用 `sys._MEIPASS`。
    """
    import sys as _sys

    meipass = getattr(_sys, "_MEIPASS", None)
    if getattr(_sys, "frozen", False) and meipass:
        return Path(meipass) / "app" / "data" / "plugin_catalog.json"
    return Path(__file__).resolve().parent.parent / "data" / "plugin_catalog.json"


# ── 真实插件发现（plan-282-1441 #6 二次修复）──
#
# 本机各 AI 编程工具都把插件放在 `<home>/<tool>/plugins/cache/<market>/<plugin>/`，
# 清单文件位置有两种形态（实测参考项目）：
#   <plugin>/plugin.json            常见（better-harness / security-scan / qoder-create-plugin）
#   <plugin>/.qoder-plugin/plugin.json   部分插件（computer-use / qoder-context）
# 另有插件带**版本号子目录**（superpowers/6.3.0、qoder-context/1.0.61.xxx），
# 以及完全无清单的插件（superpowers）。技能统一是 `skills/<name>/SKILL.md`。

_PLUGIN_TOOLS = ("qoder", "claude", "cursor", "trae", "codebuddy", "chatcoder")
# 插件根下允许的清单文件名
_MANIFEST_CANDIDATES = ("plugin.json", ".qoder-plugin/plugin.json",
                        ".chatcoder-plugin/plugin.json", ".claude-plugin/plugin.json")
# 扫描深度上限（插件根 → 市场 → 插件 → 版本目录），避免深递归拖慢市场页
_MAX_SCAN_DEPTH = 3


def _read_manifest_in(dir_path: Path) -> dict | None:
    """在给定目录里按候选清单名查找并读取插件清单。"""
    for rel in _MANIFEST_CANDIDATES:
        mf = dir_path / rel
        if not mf.is_file():
            continue
        try:
            data = json.loads(mf.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and str(data.get("name") or "").strip():
            return data
    return None


def _skills_in(plugin_dir: Path, manifest: dict | None) -> list[dict]:
    """列出插件贡献的技能（`skills/<name>/SKILL.md` 与 `skills/*.md` 两种形态）。"""
    out: list[dict] = []
    rel = ""
    if manifest:
        raw = manifest.get("skills")
        if isinstance(raw, str):
            rel = raw
        elif isinstance(raw, list) and raw:
            rel = str(raw[0])
    candidates: list[Path] = []
    if rel:
        p = (plugin_dir / rel.lstrip("./")).resolve()
        candidates.append(p)
    candidates.append(plugin_dir / "skills")
    for sk_root in candidates:
        if not sk_root.is_dir():
            continue
        for entry in sorted(sk_root.iterdir()):
            if entry.is_dir():
                md = entry / "SKILL.md"
                if md.is_file():
                    out.append({"name": entry.name, "path": str(md)})
            elif entry.suffix.lower() == ".md":
                out.append({"name": entry.stem, "path": str(entry)})
        if out:
            break
    return out


def _looks_like_plugin_dir(d: Path) -> bool:
    """目录是否像一个插件（有清单，或有 skills/ 目录，或含 MCP 配置）。"""
    if _read_manifest_in(d) is not None:
        return True
    if (d / "skills").is_dir():
        return True
    return (d / ".mcp.json").is_file() or (d / "mcp.json").is_file()


def _find_plugin_dirs(root: Path) -> list[Path]:
    """在插件根下找出所有插件目录（跳过路径上的工具目录）。"""
    found: list[Path] = []
    if not root.is_dir():
        return found

    def _walk(d: Path, depth: int) -> None:
        if depth > _MAX_SCAN_DEPTH:
            return
        try:
            children = [c for c in d.iterdir() if c.is_dir() and not c.name.startswith(".qoder-plugin")]
        except OSError:
            return
        for child in children:
            if child.name in ("node_modules", "dist", "build", "__pycache__"):
                continue
            # 自身就是插件 → 记下并停止向下（避免把插件的子目录误当插件）
            if _looks_like_plugin_dir(child):
                found.append(child)
                continue
            _walk(child, depth + 1)

    _walk(root, 0)
    return found


def discover_external_plugins() -> list[dict]:
    """扫描本机各工具的插件目录，返回可安装的真实插件列表。

    返回项含：name/displayName/description/category/version/source(工具名)/path/skills。
    同名插件只保留一个（按工具优先级，参考项目 qoder 优先）。
    """
    seen: dict[str, dict] = {}
    home = Path.home()
    for tool in _PLUGIN_TOOLS:
        root = home / f".{tool}" / "plugins"
        for plugin_dir in _find_plugin_dirs(root):
            manifest = _read_manifest_in(plugin_dir)
            # 无清单的插件用目录名兜底（实测 superpowers 就没有清单）
            name = str((manifest or {}).get("name") or plugin_dir.name)
            if name in seen:
                continue
            skills = _skills_in(plugin_dir, manifest)
            if manifest is None and not skills:
                continue  # 既无清单又无技能 → 不是可用的插件
            author = (manifest or {}).get("author")
            if isinstance(author, dict):
                author = author.get("name")
            seen[name] = {
                "name": name,
                "displayName": str((manifest or {}).get("displayName") or name),
                "description": str((manifest or {}).get("description") or ""),
                "descriptionZh": str((manifest or {}).get("descriptionZh") or ""),
                "category": str((manifest or {}).get("category") or "本地插件"),
                "tags": [str(t) for t in ((manifest or {}).get("tags") or [])],
                "author": str(author or tool),
                "version": str((manifest or {}).get("version") or ""),
                # source 用于界面提示"来自哪个工具"，也用于安装时区分
                "source": tool,
                "path": str(plugin_dir),
                "skillCount": len(skills),
                "skills": [s["name"] for s in skills],
                "hasMcp": (plugin_dir / ".mcp.json").is_file() or (plugin_dir / "mcp.json").is_file(),
                "installed": False,
                "enabled": False,
            }
    return list(seen.values())


def read_catalog() -> list[dict]:
    """内置精选目录；文件缺失时返回空列表（界面仍可用本地/Git 安装）。"""
    p = _catalog_path()
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("[plugin] 内置目录解析失败", exc_info=True)
        return []
    items = data.get("plugins") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    for it in items:
        if isinstance(it, dict) and it.get("name"):
            out.append({
                "name": str(it.get("name")),
                "displayName": str(it.get("displayName") or it.get("name")),
                "description": str(it.get("description") or ""),
                "descriptionZh": str(it.get("descriptionZh") or ""),
                "category": str(it.get("category") or "其他"),
                "tags": [str(t) for t in (it.get("tags") or [])],
                "author": str(it.get("author") or ""),
                "version": str(it.get("version") or ""),
                "source": str(it.get("source") or ""),   # 目录 / git 地址
                "featured": bool(it.get("featured")),
            })
    return out


def list_marketplace() -> dict:
    """市场视图：**本机真实扫描到的插件** + 已安装标注。

    plan-282-1441（#6 二次修复）：此前市场直接渲染内置 `plugin_catalog.json` 的条目，
    那些条目在本机没有对应文件，点「安装」无从安装（用户反馈的"假插件"）。
    现在主数据源改为 `discover_external_plugins()`（扫描本机 qoder/claude/cursor 等
    工具的真实插件目录），每个条目都带 `path`，因此**详情可看、安装可真正生效**。
    内置目录仅在"本机一个插件都扫不到"时兜底，避免市场空白。
    """
    installed = {p["name"]: p for p in list_installed()}
    items: list[dict] = []
    seen: set[str] = set()

    def _merge(base: dict) -> dict:
        local = installed.get(base["name"])
        merged = dict(base)
        merged["installed"] = local is not None
        merged["enabled"] = bool(local.get("enabled")) if local else False
        return merged

    # ① 真实扫描到的插件（主数据源）
    for p in discover_external_plugins():
        if p["name"] in seen:
            continue
        seen.add(p["name"])
        items.append(_merge(p))

    # ② 已安装但本机已扫不到的（如从 Git 装来的）：也要列出，保证能卸载
    for name, p in installed.items():
        if name in seen:
            continue
        seen.add(name)
        items.append({
            "name": p["name"], "displayName": p["displayName"],
            "description": p["description"], "descriptionZh": p["descriptionZh"],
            "category": p["category"] or "已安装", "tags": p["tags"], "author": p["author"],
            "version": p["version"], "source": "installed", "path": p["path"],
            "featured": False, "installed": True, "enabled": p["enabled"],
        })

    # ③ 本机无任何真实插件时，用内置目录兜底（离线可浏览）
    if not items:
        for c in read_catalog():
            items.append(_merge({**c, "path": "", "skillCount": 0, "skills": []}))

    return {"ok": True, "items": items, "installedCount": len(installed)}


def _skill_md_meta(path: Path) -> dict:
    """从 SKILL.md 读 front matter 的 description/trigger/tags，缺失时取首个正文段落。

    plan-59-286：此前插件贡献技能的 description 一律写成空串，模型侧的技能清单里
    就只剩技能名，完全无法判断"这个技能什么时候该用"。这里补上有效描述，
    front matter 缺失时退到正文首个非标题段落（截 200 字）。
    """
    out: dict = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    lines = text.splitlines()
    body_start = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, min(len(lines), 80)):
            line = lines[i]
            if line.strip() == "---":
                body_start = i + 1
                break
            m = re.match(r"\s*(description|trigger|tags)\s*:\s*(.*)$", line)
            if m and m.group(2).strip():
                out[m.group(1)] = m.group(2).strip().strip("\"'")
    if not out.get("description"):
        for line in lines[body_start:]:
            s = line.strip()
            if not s or s.startswith("#") or s.startswith("---"):
                continue
            out["description"] = s[:200]
            break
    return out


def _mcp_specs(dir_path: Path, manifest: dict) -> list[tuple[str, dict, str]]:
    """收集插件声明的 MCP 服务器，返回 [(name, spec, 来源), ...]。

    兼容三种声明方式（真实插件三种都存在）：
      1. plugin.json 内联对象：`"mcpServers": { "<name>": {command|url, args, env} }`
      2. plugin.json 指向插件目录内的 json：`"mcpServers": "./.mcp.json"`
      3. 插件根目录直接放 `.mcp.json` / `mcp.json`
    """
    out: list[tuple[str, dict, str]] = []
    seen: set[str] = set()

    def add(specs, source: str) -> None:
        if not isinstance(specs, dict):
            return
        for raw_name, spec in specs.items():
            name = str(raw_name or "").strip()
            if not name or name in seen or not isinstance(spec, dict):
                continue
            seen.add(name)
            out.append((name, spec, source))

    def load(rel: str) -> dict | None:
        try:
            data = json.loads((dir_path / rel).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        inner = data.get("mcpServers")
        return inner if isinstance(inner, dict) else data

    decl = manifest.get("mcpServers")
    if isinstance(decl, dict):
        add(decl, "plugin.json#mcpServers")
    elif isinstance(decl, str) and decl.strip():
        data = load(decl.strip())
        if data is not None:
            add(data, decl.strip())
    for cand in _MCP_FILES:
        if (dir_path / cand).is_file():
            data = load(cand)
            if data is not None:
                add(data, cand)
    return out


async def _register_contributed_mcp(db: AsyncSession, plugin_name: str, dir_path: Path,
                                    manifest: dict) -> int:
    """把插件声明的 MCP 服务器登记为连接器（source="plugin"）。

    plan-59-286：文件头注释一直承诺"mcp.json → 其 MCP 出现在拓展-连接器"，
    但此前**没有任何登记实现**（`plugin.json#mcpServers` 全仓无读取点），
    于是装完插件后连接器页看不到任何东西。这里补齐这条链路。

    安全前提：插件来自第三方，其 MCP 会执行指定命令或访问指定地址，
    因此一律以 `is_active=False` 落库，由用户在「拓展 → 连接器」显式开启。
    名称加 `<插件>:` 前缀，避免与用户自建或其它来源的连接器撞名。
    """
    specs = _mcp_specs(dir_path, manifest)
    if not specs:
        return 0

    from sqlalchemy import select

    from app.persistence.database import run_write_locked
    from app.persistence.models.skill import McpServer

    market = str(manifest.get("marketplaceName") or "local")

    def patch(s) -> int:
        created = 0
        for name, spec, source_file in specs:
            row_name = f"{plugin_name}:{name}"
            url = str(spec.get("url") or "").strip()
            command = str(spec.get("command") or "").strip()
            if not url and not command:
                continue  # 既无命令也无地址的声明无法落地
            raw_type = str(spec.get("type") or spec.get("transport") or "").strip().lower()
            transport = _MCP_TRANSPORT.get(raw_type) or ("http" if url else "stdio")
            args = spec.get("args") if isinstance(spec.get("args"), list) else None
            env = spec.get("env") if isinstance(spec.get("env"), dict) else None
            meta = {
                "plugin": plugin_name, "plugin_dir": str(dir_path),
                "market": market, "source_file": source_file,
            }
            existing = s.execute(
                select(McpServer).where(McpServer.name == row_name)
            ).scalars().first()
            if existing is not None:
                # 幂等：刷新配置但**不动** is_active（尊重用户已做的开启/关闭选择）
                existing.transport = transport
                if url:
                    existing.url = url
                if command:
                    existing.command = command
                if args is not None:
                    existing.args = args
                if env is not None:
                    existing.env = env
                existing.meta = {**(existing.meta or {}), **meta}
                continue
            s.add(McpServer(
                name=row_name,
                display_name=name,
                description=f"由插件「{plugin_name}」提供，安装后默认未启用",
                source="plugin",
                transport=transport,
                command=command or None,
                args=args,
                env=env,
                url=url or None,
                is_active=False,
                meta=meta,
            ))
            created += 1
        s.commit()
        return created

    try:
        return await run_write_locked(patch, label=f"plugin.mcp.{plugin_name}")
    except Exception:  # noqa: BLE001
        logger.warning("[plugin] 登记插件连接器失败 plugin=%s", plugin_name, exc_info=True)
        return 0


async def _register_contributed_skills(db: AsyncSession, plugin_name: str, dir_path: Path,
                                       manifest: dict) -> int:
    """把插件 skills/ 下的技能注册进技能表（source="plugin"）。

    复用既有扫描器（它已支持 skills/*.md 与 skills/<name>/SKILL.md 两种形态），
    避免自己再写一套解析。失败不阻塞安装。
    """
    try:
        from app.services import skill_service

        # 用本模块自己的技能发现（已覆盖 skills/<name>/SKILL.md 与 skills/*.md，
        # 且兼容 manifest.skills 指向单目录的情况）
        found = _skills_in(dir_path, manifest)
        # plan-308-1542 需求2：插件技能的目录必须随技能一起登记，
        # 否则 AI 拿到技能正文也不知道 scripts/ 等配套资源在哪（"找不到技能目录"）。
        skills_root = str((dir_path / "skills").resolve())
        n = 0
        for item in found:
            skill_name = str(item.get("name") or "")
            if not skill_name:
                continue
            # 加插件前缀，避免与用户技能重名互相覆盖
            full_name = f"{plugin_name}:{skill_name}"
            src = str(item.get("path") or "")
            meta = {
                "plugin": plugin_name,
                "plugin_dir": str(dir_path),
                "skills_root": skills_root,
                "source_file": src,
            }
            md_meta = _skill_md_meta(Path(src)) if src else {}
            existing = await skill_service.get_skill_by_name(db, full_name)
            if existing is not None:
                # plan-308-1542 需求2：已存在也要补写 meta（旧版本注册的行 meta 为空），
                # 否则重新安装/升级插件后仍然定位不到技能目录。
                patch: dict = {}
                if not (existing.meta or {}).get("plugin_dir"):
                    patch["meta"] = meta
                # plan-59-286：旧版本注册的行 description 为空，这里补一次——
                # 空描述会让模型侧技能清单只剩技能名，无法判断何时该用。
                desc = str(md_meta.get("description") or "")
                if desc and not (existing.description or "").strip():
                    patch["description"] = desc[:200]
                if patch:
                    await skill_service.update_skill(db, existing.id, **patch)
                continue
            content = ""
            try:
                content = Path(src).read_text(
                    encoding="utf-8", errors="replace")[:20000]
            except OSError:
                content = ""
            await skill_service.create_skill(
                db, name=full_name, display_name=skill_name,
                description=str(md_meta.get("description") or "")[:200],
                content=content, source="plugin",
                path=src, is_active=True, auto_load=True,
                trigger=str(md_meta.get("trigger") or "") or None,
                meta=meta,
            )
            n += 1
        await db.commit()
        return n
    except Exception:  # noqa: BLE001
        logger.warning("[plugin] 注册插件技能失败 plugin=%s", plugin_name, exc_info=True)
        return 0


def _install_files(src_dir: Path, market: str) -> tuple[Path, dict]:
    """把插件目录复制到 `~/.chatcoder/plugins/<market>/<name>/` 并返回目标与清单。

    plan-282-1441（#6 二次修复）：真实插件不都有 `plugin.json`（参考项目的 superpowers
    就没有，computer-use 则把它放在 `.qoder-plugin/` 下）。因此这里改用多候选清单查找，
    完全没有清单但有 `skills/` 的目录也允许安装（按目录名注册）。
    """
    manifest = _read_manifest_in(src_dir)
    if manifest is None:
        # 无清单：只要确实带了技能，就按目录名当作插件安装
        if not (src_dir / "skills").is_dir():
            raise ValueError("该目录不是有效插件：既无 plugin.json，也没有 skills 目录")
        manifest = {"name": src_dir.name, "displayName": src_dir.name}
    name = str(manifest["name"]).strip()
    market = (manifest.get("marketplaceName") or market or "local").strip() or "local"
    dest = _root() / market / name
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src_dir, dest)
    return dest, manifest


def _record_install(name: str, market: str, dest: Path, manifest: dict, source: str) -> None:
    reg = _read_registry()
    plugins = reg.setdefault("plugins", {})
    plugins[_key(name, market)] = [{
        "enabled": bool(manifest.get("defaultEnabled", True)),
        "installPath": str(dest),
        "version": str(manifest.get("version") or ""),
        "source": source,
        "installedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "displayName": str(manifest.get("displayName") or name),
        # plan-284-1450：市场安装时记下图标与远端 id，扩展管理据此复用市场图标
        "logo": str(manifest.get("logo") or ""),
        "marketId": str(manifest.get("marketId") or ""),
    }]
    _write_registry(reg)


async def install_from_dir(db: AsyncSession, src: str) -> dict:
    """从本地目录安装。"""
    p = Path(src).expanduser().resolve()
    if not p.is_dir():
        raise ValueError("路径不存在或不是目录")
    dest, manifest = _install_files(p, "local")
    name = str(manifest["name"])
    market = str(manifest.get("marketplaceName") or "local")
    _record_install(name, market, dest, manifest, source=str(p))
    n = await _register_contributed_skills(db, name, dest, manifest)
    # plan-59-286：插件声明的 MCP 一并登记为连接器（默认未启用），
    # 否则"插件自带连接器"在拓展页完全不可见。
    m = await _register_contributed_mcp(db, name, dest, manifest)
    logger.info("[plugin] 安装 %s（技能 %d / 连接器 %d）", name, n, m)
    return {"ok": True, "name": name, "path": str(dest), "skills": n, "connectorCount": m}


async def install_from_git(db: AsyncSession, repo_url: str, *, market: str = "local") -> dict:
    """从 Git 仓库安装（浅克隆到临时目录后走本地安装流程）。"""
    tmp = _root() / ".tmp" / f"clone-{int(time.time())}"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            ["git", "clone", "--depth", "1", repo_url, str(tmp)],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT,
            encoding="utf-8", errors="replace",
        )
        if proc.returncode != 0:
            raise ValueError(f"克隆失败：{(proc.stderr or proc.stdout)[:200]}")
        return await install_from_dir(db, str(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def set_enabled(db: AsyncSession, name: str, enabled: bool) -> dict:
    """启用/停用插件：同步切换其贡献技能的 is_active。"""
    reg = _read_registry()
    hit_key = None
    for key, entries in (reg.get("plugins") or {}).items():
        for entry in entries or []:
            if isinstance(entry, dict) and (key.split("@")[0] == name
                                            or entry.get("displayName") == name):
                hit_key = key
                entry["enabled"] = enabled
                break
        if hit_key:
            break
    if hit_key is None:
        raise ValueError("插件未安装")
    _write_registry(reg)

    # 贡献技能与连接器跟随插件启停（名称都带 "<plugin>:" 前缀，可精确匹配）
    try:
        from sqlalchemy import select

        from app.persistence.models.skill import McpServer, Skill

        res = await db.execute(select(Skill).where(Skill.source == "plugin"))
        for sk in res.scalars().all():
            if str(sk.name).startswith(f"{name}:"):
                sk.is_active = enabled
        # plan-59-286：插件自带的连接器同样跟随启停——
        # 插件停用后其连接器仍在启用状态是不可预期的（第三方 MCP 不该继续被调用）。
        rows = await db.execute(select(McpServer).where(McpServer.source == "plugin"))
        for srv in rows.scalars().all():
            if str(srv.name).startswith(f"{name}:"):
                srv.is_active = enabled
        await db.commit()
    except Exception:  # noqa: BLE001
        logger.warning("[plugin] 同步技能/连接器启停失败 name=%s", name, exc_info=True)

    return {"ok": True, "name": name, "enabled": enabled}


async def uninstall(db: AsyncSession, name: str) -> dict:
    """卸载插件：移出目录、注销贡献技能、清注册表条目。"""
    reg = _read_registry()
    plugins = reg.get("plugins") or {}
    hit_key = None
    for key in list(plugins.keys()):
        if key.split("@")[0] == name:
            hit_key = key
            break
    if hit_key is None:
        raise ValueError("插件未安装")

    entries = plugins.pop(hit_key) or []
    for entry in entries:
        ip = (entry or {}).get("installPath")
        if ip:
            shutil.rmtree(ip, ignore_errors=True)
    _write_registry(reg)

    # 注销其贡献的技能与连接器（避免卸载后残留第三方 MCP 配置）
    try:
        from sqlalchemy import delete as _delete

        from app.persistence.models.skill import McpServer, Skill

        await db.execute(_delete(Skill).where(
            Skill.source == "plugin", Skill.name.like(f"{name}:%"),
        ))
        # plan-59-286：插件贡献的连接器同源清理（meta.plugin 与名称前缀双重对应）
        await db.execute(_delete(McpServer).where(
            McpServer.source == "plugin", McpServer.name.like(f"{name}:%"),
        ))
        await db.commit()
    except Exception:  # noqa: BLE001
        logger.warning("[plugin] 注销插件技能/连接器失败 name=%s", name, exc_info=True)

    logger.info("[plugin] 已卸载 %s", name)
    return {"ok": True, "name": name}
