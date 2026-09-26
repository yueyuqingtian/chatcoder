"""市场目录服务（plan-41-198）。

背景：本应用原有插件/技能/连接器三条链路各自独立（插件有本机扫描，技能有导入/
技能仓库/扫描，连接器有导入 JSON/扫描），但**没有统一的「市场」视图**——
用户无法在一处浏览可安装内容并一键安装，设置页也只列已装项。

本模块提供两个只读聚合端点所需的逻辑：

1. `list_catalog(db, kind)`：市场条目 = 内置精选目录（按 kind 分组）+ 本机真实项叠加。
   - 本机真实项（插件扫描结果 / 已入库技能 / 已登记连接器）标记为 `installed` 或
     `scan`（本机扫描到、尚未安装，可直接一键安装）；
   - 内置目录项无本地来源，标记为 `market`（提供「前往市场」引导，不伪造仓库地址）。
2. `installed_summary(db)`：已安装聚合计数（插件 / 技能 / 连接器 / 智能体），
   供设置页「扩展管理」的计数标签使用。

设计约束：不引入任何第三方依赖。

plan-41-225 起本模块**接入远端 qoder 市场**（见文件末「远端市场接入」段）：
市场视图由远端分页数据驱动（技能 43,710 / 插件 109 / 连接器 278 条均可访问）；
`list_catalog` 退化为**离线兜底**（远端不可达时展示本机项与内置精选目录）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# 市场站点（用户指定的内容来源；「前往市场」按钮跳转目标）
MARKET_URL = "https://qoder.com.cn/marketplace"

# ── 内置精选目录 ──────────────────────────────────────────────
# 说明：条目按「分类 + 中文描述」整理，用于在无本机可安装内容时也能呈现完整
# 市场观感，并为用户提供发现入口。这些条目**不含伪造的仓库地址**，其安装路径是
# 「前往市场获取后用导入/安装通道接入」。

_PLUGIN_CATALOG: list[dict] = [
    {"name": "ppt-studio", "displayName": "PPT", "descriptionZh": "创建与编辑演示文稿，支持内容组织、幻灯片排版与演示文件产出。", "category": "内容创作", "tags": ["演示", "文稿"], "author": "Qoder", "downloads": 1629},
    {"name": "design-review", "displayName": "Design Review", "descriptionZh": "结合 Qoder 技能与 Design.md 检查器，审查界面设计一致性。", "category": "设计", "tags": ["设计审查"], "author": "Qoder", "downloads": 1120},
    {"name": "java-fullstack", "displayName": "全栈 Java 专家", "descriptionZh": "Java Spring Boot + Vue 3 全栈开发专家，覆盖接口、数据层与前端联调。", "category": "代码开发", "tags": ["Java", "全栈"], "author": "Qoder", "downloads": 980},
    {"name": "arch-visual", "displayName": "架构可视化", "descriptionZh": "把架构梳理成可读图表，辅助理解模块边界、调用链与依赖关系。", "category": "代码开发", "tags": ["架构", "图表"], "author": "Qoder", "downloads": 764},
    {"name": "chrome-devtools", "displayName": "Chrome DevTools", "descriptionZh": "通过 MCP 使用 Chrome DevTools 与 Puppeteer，抓取页面结构与运行时状态。", "category": "开发者工具", "tags": ["浏览器", "调试"], "author": "Qoder", "downloads": 611},
    {"name": "postman", "displayName": "Postman", "descriptionZh": "完整的 API 生命周期协作：同步 Collection、生成请求与测试脚本。", "category": "开发者工具", "tags": ["API"], "author": "Qoder", "downloads": 547},
    {"name": "polardb-memory", "displayName": "PolarDB-PG 记忆管理", "descriptionZh": "为开发过程提供长期记忆能力，自动记录并召回项目上下文。", "category": "数据库与分析", "tags": ["记忆", "数据库"], "author": "Qoder", "downloads": 430},
    {"name": "redis-tools", "displayName": "Redis", "descriptionZh": "Redis 开发运维实践，覆盖数据结构、查询语法与性能诊断。", "category": "数据库与分析", "tags": ["Redis"], "author": "Qoder", "downloads": 388},
    {"name": "qoder-cloud-agents", "displayName": "Qoder Cloud Agents", "descriptionZh": "通过云端协议连接并创建云端智能体，支持会话与任务协同。", "category": "Agent 自进化", "tags": ["云端", "智能体"], "author": "Qoder", "downloads": 352},
    {"name": "mongodb", "displayName": "MongoDB", "descriptionZh": "MongoDB 官方连接器，支持集合管理、聚合查询与索引优化建议。", "category": "数据库与分析", "tags": ["MongoDB"], "author": "MongoDB", "downloads": 316},
    {"name": "code-graph", "displayName": "代码图谱可视化", "descriptionZh": "从代码生成结构图谱，直观呈现模块依赖与关键调用路径。", "category": "代码开发", "tags": ["图谱"], "author": "Qoder", "downloads": 274},
    {"name": "superpowers", "displayName": "Superpowers", "descriptionZh": "核心方法论技能集：控制上下文、约束实现范围并规范交付流程。", "category": "工作流", "tags": ["方法论"], "author": "Qoder", "downloads": 240},
    {"name": "context7", "displayName": "Context7", "descriptionZh": "为代码库补齐最新文档与代码示例，减少过时用法带来的返工。", "category": "知识研究", "tags": ["文档"], "author": "Context7", "downloads": 226},
    {"name": "tencent-cloudbase", "displayName": "Tencent CloudBase", "descriptionZh": "腾讯云 CloudBase 工具包，为应用提供后端云能力与部署支持。", "category": "运维部署", "tags": ["云服务"], "author": "Tencent", "downloads": 198},
    {"name": "oceanbase", "displayName": "OceanBase", "descriptionZh": "OceanBase AI 智能体技能包，覆盖集群管理、租户与容量运维。", "category": "数据库与分析", "tags": ["OceanBase"], "author": "OceanBase", "downloads": 165},
]

_SKILL_CATALOG: list[dict] = [
    {"name": "deep-research", "displayName": "深入研究", "descriptionZh": "通过来源核查与交叉验证，输出可追溯的结论与调研报告。", "category": "知识研究", "tags": ["调研"], "author": "Qoder", "downloads": 1408},
    {"name": "data-analysis", "displayName": "分析数据分析", "descriptionZh": "使用 Python、Jupyter 与现代数据分析工具处理并解释数据。", "category": "数据库与分析", "tags": ["数据分析"], "author": "Qoder", "downloads": 1222},
    {"name": "content-writing", "displayName": "内容研究撰写", "descriptionZh": "先研究再写作：补齐资料、组织结构、迭代大纲并成稿。", "category": "内容创作", "tags": ["写作"], "author": "Qoder", "downloads": 1084},
    {"name": "diagram-builder", "displayName": "图像编辑器", "descriptionZh": "通过图形语法描述并生成示意图、流程图与结构图。", "category": "设计", "tags": ["绘图"], "author": "Qoder", "downloads": 902},
    {"name": "code-analysis", "displayName": "代码分析", "descriptionZh": "使用提问式分析进行深度代码分析，识别技术债务、风险与改进点。", "category": "代码开发", "tags": ["分析"], "author": "Qoder", "downloads": 847},
    {"name": "java-dev", "displayName": "Java 开发", "descriptionZh": "聚焦 Java 21+，包括虚拟线程、模式匹配与集合框架实践。", "category": "代码开发", "tags": ["Java"], "author": "Qoder", "downloads": 803},
    {"name": "test-first", "displayName": "测试测试", "descriptionZh": "从读写任何路径、测试失败或意外行为出发，定位根因并补齐回归测试。", "category": "安全与测试", "tags": ["测试"], "author": "Qoder", "downloads": 766},
    {"name": "project-onboarding", "displayName": "项目开发", "descriptionZh": "理解项目架构与模块组织，在需要时按既有约定开展改动。", "category": "代码开发", "tags": ["上手"], "author": "Qoder", "downloads": 712},
    {"name": "brainstorm", "displayName": "头脑风暴", "descriptionZh": "通过结构化方式约束并收敛发散想法，形成可执行方案。", "category": "工作流", "tags": ["发散"], "author": "Qoder", "downloads": 668},
    {"name": "python-dev", "displayName": "Python 开发", "descriptionZh": "Python 开发实践：类型标注、惯用法、异步与测试。", "category": "代码开发", "tags": ["Python"], "author": "Qoder", "downloads": 624},
    {"name": "ui-design", "displayName": "UI 设计", "descriptionZh": "从参考 UI 图提取设计系统并生成可实现的界面方案。", "category": "设计", "tags": ["UI"], "author": "Qoder", "downloads": 580},
    {"name": "frontend-expert", "displayName": "前端开发专家", "descriptionZh": "专注于现代 Web 应用开发的资深前端开发工程师工作流。", "category": "代码开发", "tags": ["前端"], "author": "Qoder", "downloads": 536},
    {"name": "market-research", "displayName": "市场研究报告", "descriptionZh": "以咨询公司（麦肯锡、BCG、Gartner）的标准风格生成研究结论。", "category": "知识研究", "tags": ["研究"], "author": "Qoder", "downloads": 492},
    {"name": "plan-builder", "displayName": "创建计划", "descriptionZh": "把复杂请求拆成有序可核对的任务，明确依赖关系与验收标准。", "category": "工作流", "tags": ["规划"], "author": "Qoder", "downloads": 448},
    {"name": "prompt-optimize", "displayName": "LLM 提示词优化", "descriptionZh": "改进提示词结构，明确角色、约束与输出格式，提升稳定性。", "category": "工作流", "tags": ["提示词"], "author": "Qoder", "downloads": 402},
    {"name": "sql-optimize", "displayName": "SQL 数据库优化", "descriptionZh": "面向 SQL 专家级优化的索引、执行计划与慢查询治理实践。", "category": "数据库与分析", "tags": ["SQL"], "author": "Qoder", "downloads": 358},
    {"name": "code-audit", "displayName": "代码审计", "descriptionZh": "理解代码库、追踪调用路径并识别潜在缺陷与安全隐患。", "category": "安全与测试", "tags": ["审计"], "author": "Qoder", "downloads": 314},
    {"name": "mini-program", "displayName": "微信小程序开发辅助", "descriptionZh": "提供微信小程序开发的代码模板、API 示例与常见问题排查思路。", "category": "代码开发", "tags": ["小程序"], "author": "Qoder", "downloads": 270},
]

_CONNECTOR_CATALOG: list[dict] = [
    {"name": "github", "displayName": "GitHub", "descriptionZh": "通过 MCP 连接 GitHub，检索代码库并处理 Issue / Pull Request。", "category": "开发者工具", "tags": ["Git"], "author": "GitHub", "downloads": 1533},
    {"name": "amap", "displayName": "高德地图", "descriptionZh": "提供地图数据、商业洞察、路径规划与地理查询服务。", "category": "知识研究", "tags": ["地图"], "author": "高德", "downloads": 1210},
    {"name": "cloudflare", "displayName": "Cloudflare", "descriptionZh": "提供网络性能与安全服务，包括 CDN、DNS、边缘计算与防护能力。", "category": "运维部署", "tags": ["CDN"], "author": "Cloudflare", "downloads": 1076},
    {"name": "gitee", "displayName": "Gitee", "descriptionZh": "访问已授权用户的 Gitee 资料与代码仓库内容。", "category": "开发者工具", "tags": ["Git"], "author": "Gitee", "downloads": 932},
    {"name": "figma", "displayName": "Figma", "descriptionZh": "访问设计文件、团队项目与设计协作评论，用于实现还原。", "category": "设计", "tags": ["设计稿"], "author": "Figma", "downloads": 874},
    {"name": "notion", "displayName": "Notion", "descriptionZh": "提供事件文档、知识库、数据库与协作工作区访问。", "category": "工作流", "tags": ["文档"], "author": "Notion", "downloads": 802},
    {"name": "supabase", "displayName": "Supabase", "descriptionZh": "通过 Supabase MCP 管理项目数据、函数与存储。", "category": "数据库与分析", "tags": ["Postgres"], "author": "Supabase", "downloads": 741},
    {"name": "grafana", "displayName": "Grafana", "descriptionZh": "提供可观测性平台连接，用于查询指标、日志与链路数据。", "category": "运维部署", "tags": ["监控"], "author": "Grafana", "downloads": 668},
    {"name": "clickhouse", "displayName": "ClickHouse", "descriptionZh": "提供高性能列式分析数据库连接，用于大规模实时数据分析。", "category": "数据库与分析", "tags": ["OLAP"], "author": "ClickHouse", "downloads": 594},
    {"name": "linear", "displayName": "Linear", "descriptionZh": "管理 Linear 问题、项目与团队协作流程。", "category": "工作流", "tags": ["项目管理"], "author": "Linear", "downloads": 530},
    {"name": "firecrawl", "displayName": "Firecrawl", "descriptionZh": "通过 Firecrawl 搜索、抓取和提取网页内容，并返回结构化数据。", "category": "知识研究", "tags": ["爬取"], "author": "Firecrawl", "downloads": 476},
    {"name": "exa", "displayName": "Exa", "descriptionZh": "通过 Exa 搜索引擎获取结构化内容，适合围绕问题做主题调研。", "category": "知识研究", "tags": ["搜索"], "author": "Exa", "downloads": 412},
    {"name": "todoist", "displayName": "Todoist", "descriptionZh": "管理 Todoist 任务与待办事项清单。", "category": "工作流", "tags": ["待办"], "author": "Todoist", "downloads": 356},
    {"name": "sanity", "displayName": "Sanity", "descriptionZh": "为内容与数字资产提供结构化内容管理能力。", "category": "内容创作", "tags": ["CMS"], "author": "Sanity", "downloads": 298},
]

_BUNDLED: dict[str, list[dict]] = {
    "plugin": _PLUGIN_CATALOG,
    "skill": _SKILL_CATALOG,
    "connector": _CONNECTOR_CATALOG,
}

# 市场视图的分类顺序（与参考设计一致；实际展示会按条目存在性过滤）
CATEGORY_ORDER: list[str] = [
    "全部", "精选", "办公效率", "内容创作", "市场营销", "电商零售", "金融财务",
    "法律", "代码开发", "代码评审", "安全与测试", "数据库与分析", "运维部署",
    "开发者工具", "设计", "产品管理", "知识研究", "工作流", "Agent 自进化", "通讯",
]


def _entry(kind: str, raw: dict) -> dict:
    """把内置目录条目补全为统一市场条目结构。"""
    return {
        "id": f"{kind}:{raw['name']}",
        "kind": kind,
        "name": raw["name"],
        "displayName": raw.get("displayName") or raw["name"],
        "descriptionZh": raw.get("descriptionZh") or "",
        "category": raw.get("category") or "全部",
        "tags": list(raw.get("tags") or []),
        "author": raw.get("author") or "",
        "downloads": raw.get("downloads"),
        "featured": bool(raw.get("featured")),
        "installKind": "market",
        "installed": False,
        "enabled": False,
        "source": "",
    }


def _local_plugin_items(items: list[dict]) -> list[dict]:
    """本机扫描到的插件 → 市场条目（可直接安装 / 已安装）。"""
    out: list[dict] = []
    for it in items:
        out.append({
            "id": f"plugin:{it.get('name')}",
            "kind": "plugin",
            "name": str(it.get("name") or ""),
            "displayName": it.get("displayName") or it.get("name") or "",
            "descriptionZh": it.get("descriptionZh") or "",
            "description": it.get("description") or "",
            "category": it.get("category") or "开发者工具",
            "tags": list(it.get("tags") or []),
            "author": it.get("author") or "",
            "downloads": None,
            "version": it.get("version"),
            "featured": False,
            # 本机扫到但未安装 → 可直接从本地目录安装；已安装 → 进入管理
            "installKind": "installed" if it.get("installed") else "scan",
            "installed": bool(it.get("installed")),
            "enabled": bool(it.get("enabled")),
            "source": it.get("source") or "",
            "path": it.get("path") or "",
            "skills": list(it.get("skills") or []),
            "hasMcp": bool(it.get("hasMcp")),
        })
    return out


async def _installed_names(db: AsyncSession, kind: str) -> dict[str, dict]:
    """已安装项索引：name → {enabled, source}。插件读注册表，技能/连接器读库表。"""
    index: dict[str, dict] = {}
    try:
        if kind == "plugin":
            from app.services import plugin_service
            # 复用 plugin_service.list_installed()（已封装「读注册表 + 各目录 manifest」
            # 并输出标准字段），避免在服务层重复解析 name@market 注册表结构
            for it in plugin_service.list_installed():
                name = str(it.get("name") or "")
                if name:
                    entry = {
                        "name": name,
                        "enabled": bool(it.get("enabled")),
                        "source": str(it.get("marketplaceName") or "installed"),
                        "iconUrl": str(it.get("logo") or ""),
                    }
                    index[name] = entry
                    market_id = str(it.get("marketId") or "")
                    if market_id:
                        index[market_id] = entry
        elif kind == "skill":
            from app.persistence.models.skill import Skill
            rows = (await db.execute(
                select(Skill.id, Skill.name, Skill.is_active, Skill.source, Skill.meta)
            )).all()
            for sid, name, active, src, meta in rows:
                entry = {
                    "name": str(name),
                    "enabled": bool(active), "source": str(src or ""),
                    "iconUrl": str((meta or {}).get("market_icon_url") or ""),
                    # plan-284-1451：市场行内的启停/删除要落到本机记录上，故带上库内 id
                    "localId": int(sid),
                }
                index[str(name)] = entry
                market_id = str((meta or {}).get("market_id") or "")
                if market_id:
                    index[market_id] = entry
        elif kind == "connector":
            from app.persistence.models.skill import McpServer
            rows = (await db.execute(
                select(McpServer.id, McpServer.name, McpServer.is_active, McpServer.source, McpServer.meta)
            )).all()
            for mid, name, active, src, meta in rows:
                entry = {
                    "name": str(name),
                    "enabled": bool(active), "source": str(src or ""),
                    "iconUrl": str((meta or {}).get("market_icon_url") or ""),
                    "localId": int(mid),
                }
                index[str(name)] = entry
                market_id = str((meta or {}).get("market_id") or "")
                if market_id:
                    index[market_id] = entry
    except Exception:
        logger.debug("[market] 读取已安装索引失败（按未安装处理）", exc_info=True)
    return index


async def _local_group_items(db: AsyncSession, kind: str,
                             remote_names: set[str], limit: int = 40) -> list[dict]:
    """本机项（内置 / 已安装，且不在当前远端结果里），供市场页分组展示与启停。

    用户反馈：连接器页看不到系统内置的两个（数据库连接 / 开发调试），技能页已安装的
    技能也没有启停入口 —— 它们都不在远端市场列表里，而市场页此前只渲染远端结果。
    这里把它们组装成与远面条目同构的结构，并标 `group` 供前端分组：
      · builtin  应用内置（连接器的 database / debugger）
      · local    本机已导入 / 已安装

    category 留空：本机项不应污染分类条（分类聚合只看市场条目）。
    """
    out: list[dict] = []
    try:
        if kind == "connector":
            from app.persistence.models.skill import McpServer
            rows = (await db.execute(
                select(McpServer).order_by(McpServer.source, McpServer.name)
            )).scalars().all()
            for m in rows:
                name = str(m.name or "")
                if not name or name in remote_names:
                    continue
                src = str(m.source or "")
                out.append({
                    "id": f"connector:local:{m.id}",
                    "kind": kind,
                    "name": name,
                    "displayName": str(m.display_name or name),
                    "description": str(m.description or ""),
                    "category": "",
                    "tags": [],
                    "featured": False,
                    "installKind": "installed",
                    "installed": True,
                    "enabled": bool(m.is_active),
                    "localId": int(m.id),
                    "source": src or "local",
                    "group": "builtin" if src == "builtin" else "local",
                    "iconUrl": str((m.meta or {}).get("market_icon_url") or ""),
                })
        elif kind == "skill":
            from app.persistence.models.skill import Skill
            rows = (await db.execute(select(Skill))).scalars().all()
            # 市场安装的排前面（用户更可能在这里管理它们），其余按名称
            rows = sorted(rows, key=lambda s: (
                0 if (s.meta or {}).get("market_id") else 1, str(s.name)))
            for s in rows:
                name = str(s.name or "")
                if not name or name in remote_names:
                    continue
                out.append({
                    "id": f"skill:local:{s.id}",
                    "kind": kind,
                    "name": name,
                    "displayName": str(s.display_name or name),
                    "description": str(s.description or ""),
                    "category": "",
                    "tags": [],
                    "featured": False,
                    "installKind": "installed",
                    "installed": True,
                    "enabled": bool(s.is_active),
                    "localId": int(s.id),
                    "source": str(s.source or "local"),
                    "group": "local",
                    "iconUrl": str((s.meta or {}).get("market_icon_url") or ""),
                })
        elif kind == "plugin":
            from app.services import plugin_service
            for p in plugin_service.list_installed():
                name = str(p.get("name") or "")
                if not name or name in remote_names:
                    continue
                out.append({
                    "id": f"plugin:local:{name}",
                    "kind": kind,
                    "name": name,
                    "displayName": str(p.get("displayName") or name),
                    "description": str(p.get("descriptionZh") or p.get("description") or ""),
                    "category": "",
                    "tags": [],
                    "featured": False,
                    "installKind": "installed",
                    "installed": True,
                    "enabled": bool(p.get("enabled")),
                    "source": str(p.get("marketplaceName") or "local"),
                    "path": str(p.get("path") or ""),
                    "group": "local",
                    "iconUrl": str(p.get("logo") or ""),
                })
    except Exception:
        logger.debug("[market] 组装本机项失败（不影响远端列表）", exc_info=True)
    return out[:limit]


async def _with_local_items(db: AsyncSession, kind: str, items: list[dict],
                            *, page: int, keyword: str, category: str) -> list[dict]:
    """首页（且无搜索 / 分类筛选）时把本机项排到最前：内置与已安装条目也要能启停。

    只在首页合并：翻页追加的是纯远端内容，本机项不会重复出现；
    搜索/筛选时保持纯市场结果，避免"搜不到却混进本机项"的困惑。
    """
    if page != 1 or keyword or category:
        return items
    names = {str(i.get("name") or "") for i in items}
    local = await _local_group_items(db, kind, names)
    return (local + items) if local else items


async def list_catalog(db: AsyncSession, kind: str) -> dict:
    """市场目录：内置精选 + 本机真实项（已安装 / 可安装）叠加。"""
    if kind not in _BUNDLED:
        raise ValueError("kind 必须为 plugin / skill / connector")

    installed = await _installed_names(db, kind)
    items: list[dict] = []
    seen: set[str] = set()

    # 1) 本机真实项优先（插件才有本机扫描概念；技能/连接器的「本机项」即已入库项）
    local_names: set[str] = set()
    if kind == "plugin":
        try:
            from app.services import plugin_service
            scanned = (await plugin_service.list_marketplace()).get("items") or []
            for it in _local_plugin_items(scanned):
                items.append(it)
                local_names.add(it["name"])
                seen.add(it["name"])
        except Exception:
            logger.warning("[market] 本机插件扫描失败，仅展示内置目录", exc_info=True)

    # 2) 内置精选目录（跳过与本机项同名的条目）
    for raw in _BUNDLED[kind]:
        if raw["name"] in seen:
            continue
        entry = _entry(kind, raw)
        hit = installed.get(raw["name"])
        if hit:
            entry["installed"] = True
            entry["enabled"] = hit["enabled"]
            entry["installKind"] = "installed"
            entry["localId"] = hit.get("localId")
        items.append(entry)

    # 3) 已安装但不在内置目录、也不在本机扫描结果中的项（避免「装了却看不见」）
    emitted: set[str] = set()
    for key, meta in installed.items():
        real_name = str(meta.get("name") or key)
        # 索引里同一项存了 name 与远端 id 两个键；别名键直接跳过，否则会输出两份
        if key != real_name or real_name in seen or real_name in emitted:
            continue
        emitted.add(real_name)
        items.append({
            "id": f"{kind}:local:{real_name}",
            "kind": kind,
            "name": real_name,
            "displayName": real_name,
            "descriptionZh": "",
            "category": "",
            "tags": [],
            "author": "",
            "downloads": None,
            "featured": False,
            "installKind": "installed",
            "installed": True,
            "enabled": meta["enabled"],
            "localId": meta.get("localId"),
            "source": meta["source"],
            "group": "local",
        })

    present = {str(i.get("category") or "") for i in items}
    categories = [c for c in CATEGORY_ORDER if c in ("全部", "精选") or c in present]
    for c in sorted(present):
        if c and c not in categories:
            categories.append(c)

    installed_count = sum(1 for i in items if i.get("installed"))
    return {
        "ok": True,
        "kind": kind,
        "marketUrl": MARKET_URL,
        "items": items,
        "categories": categories,
        "installedCount": installed_count,
        "localCount": len(local_names),
    }


async def installed_summary(db: AsyncSession) -> dict:
    """已安装聚合：插件 / 技能 / 连接器 / 智能体 计数（设置页计数标签用）。"""
    counts = {"plugin": 0, "skill": 0, "connector": 0, "agent": 0}
    try:
        plugin_index = await _installed_names(db, "plugin")
        counts["plugin"] = len(plugin_index)
    except Exception:
        logger.debug("[market] 插件计数失败", exc_info=True)
    try:
        from app.persistence.models.skill import McpServer, Skill
        counts["skill"] = len((await db.execute(select(Skill.id))).all())
        counts["connector"] = len((await db.execute(select(McpServer.id))).all())
    except Exception:
        logger.debug("[market] 技能/连接器计数失败", exc_info=True)
    try:
        from app.persistence.models.agent import Agent
        counts["agent"] = len((await db.execute(select(Agent.id))).all())
    except Exception:
        logger.debug("[market] 智能体计数失败", exc_info=True)
    return {"ok": True, "counts": counts}


# ══════════════════════════════════════════════════════════════
# 远端市场接入（plan-41-225）
#
# 为什么经后端代理而非前端直连：
#  ① 用户在设置里配的 http_proxy 只对后端生效（build_http_client 已闭环），
#     前端直连会绕过代理，国内网络下大概率失败；
#  ② 规避浏览器 CORS 与 UA 限制；
#  ③ 安装落盘本来就在后端，数据源与安装动作同源，避免两套地址；
#  ④ 后端可落盘缓存，断网时降级展示上次目录。
#
# 协议（2026-09-25 实测固化；市场改版时以实测为准）：
#   列表 GET /apphub/api/v1/marketplace/catalog/extensions
#        ?extension_types=skill|plugin|connector
#        &sort=hot|new&category=<code>&keyword=<词>&include_facets=true
#        &pagination.current_page=<n>&pagination.page_size=<n>
#   响应 {"<kind>s": {items: [...], facets: {...}, pages: {...}}}
#   条目 <kind>_id / <kind>_name / display_name_cn / description_cn /
#        icon_url / author_name / install_count / category / industries
#   pages  current_page / last_page / page_size / total_size
#   facets.categories [{code, label, count}]
#   实测总量：skill 43710 / plugin 109 / connector 278
# ══════════════════════════════════════════════════════════════

MARKET_API = "https://qoder.com.cn/apphub/api/v1/marketplace/catalog/extensions"

_CATEGORY_ZH = {
    "Productivity": "办公效率", "Content Creation": "内容创作",
    "Marketing & Sales": "市场与销售", "E-commerce & Retail": "电商零售",
    "Finance": "金融财务", "Legal": "法律", "HR & Recruiting": "人力资源",
    "Coding": "代码开发", "Code Review": "代码评审",
    "Security & Testing": "安全与测试", "Database & Analytics": "数据库与分析",
    "DevOps": "运维部署", "Developer Tools": "开发者工具", "Design": "设计",
    "Product Management": "产品管理", "Knowledge": "知识研究",
    "Workflow": "工作流", "Agent Evolution": "智能体进化",
    "IM": "即时通讯", "Other": "其他",
}


def _category_label(value) -> str:
    label = str(value or "")
    return _CATEGORY_ZH.get(label, label)
_MARKET_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": MARKET_URL,
}
# 缓存目录遵循项目既有约定（~/.chatcoder/ 下、支持 CHATCODER_* 环境变量覆盖）
_MARKET_CACHE_DIR = Path(
    os.environ.get("CHATCODER_MARKET_DIR", str(Path.home() / ".chatcoder" / "market"))
)
# 同一分页 10 分钟内复用缓存：来回切标签/退详情重进不再重复请求
_MARKET_CACHE_TTL = 600.0
_MARKET_TIMEOUT = 20.0
# 远端实测支持 page_size=48；上限设 60 防止滥用（前端固定用 40）
_MARKET_PAGE_SIZE_MAX = 60


def _market_cache_path(kind: str, page: int, page_size: int,
                       keyword: str, category: str, sort: str) -> Path:
    """缓存键 = 类型 + 全部分页/筛选参数（哈希后作文件名，避免路径非法字符）。"""
    key = f"{kind}|{page}|{page_size}|{keyword}|{category}|{sort}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return _MARKET_CACHE_DIR / f"{kind}-{digest}.json"


def _read_market_cache(path: Path, *, allow_stale: bool = False) -> dict | None:
    """读缓存。allow_stale=True 时忽略 TTL（供远端不可达时降级展示）。"""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if not allow_stale and (time.time() - float(data.get("_cached_at") or 0)) > _MARKET_CACHE_TTL:
        return None
    data.pop("_cached_at", None)
    return data


def _write_market_cache(path: Path, payload: dict) -> None:
    try:
        _MARKET_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_payload = dict(payload)
        cache_payload["items"] = []
        for item in payload.get("items") or []:
            # 本机项（内置 / 已安装）每次实时组装：写进缓存会在卸载后仍显示，故跳过
            if item.get("group"):
                continue
            cached_item = dict(item)
            # 安装状态随本机数据变化，不能随着市场目录缓存过期。
            cached_item.pop("installed", None)
            cached_item.pop("enabled", None)
            cached_item.pop("localId", None)
            if cached_item.get("installKind") == "installed":
                cached_item["installKind"] = "remote"
            cache_payload["items"].append(cached_item)
        path.write_text(
            json.dumps({**cache_payload, "_cached_at": time.time()}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        logger.debug("[market] 缓存写入失败（不影响响应）", exc_info=True)


def _refresh_cached_install_state(payload: dict, installed: dict[str, dict]) -> dict:
    """缓存只保留市场静态信息；每次响应都按当前本机安装记录重算状态。"""
    result = dict(payload)
    items: list[dict] = []
    for item in payload.get("items") or []:
        current = dict(item)
        name = str(current.get("name") or "")
        item_id = str(current.get("id") or "")
        remote_id = item_id.split(":", 1)[1] if ":" in item_id else ""
        hit = installed.get(name) or installed.get(remote_id)
        current["installed"] = hit is not None
        current["enabled"] = bool(hit and hit.get("enabled"))
        current["installKind"] = "installed" if hit else "remote"
        # plan-284-1451：本机记录 id 随响应带出（前端行内启停/删除直接用它，无需二次查询）
        current["localId"] = (hit or {}).get("localId")
        if hit and not current.get("iconUrl"):
            current["iconUrl"] = str(hit.get("iconUrl") or "")
        items.append(current)
    result["items"] = items
    result["installedCount"] = sum(1 for item in items if item.get("installed"))
    # plan-59-286：分类标签统一过一次中文映射。
    # 缓存文件里可能存着加映射之前写入的英文 label（旧缓存），
    # 若照原样返回，就会出现"热门（命中缓存）显示英文、最新（重新拉取）显示中文、
    # 切回去又变英文"的来回跳变——用户反馈的正是这个现象。
    result["categories"] = [
        {**c, "label": _category_label(c.get("label") or c.get("code") or "")}
        for c in (payload.get("categories") or []) if isinstance(c, dict)
    ]
    result["cached"] = True
    return result


def _safe_icon_url(raw_url) -> str:
    """图标地址安全校验：只接受 https 绝对地址（防 javascript:/data: 注入）。"""
    url = str(raw_url or "").strip()
    return url if url.startswith("https://") else ""


def _normalize_remote_item(kind: str, raw: dict, installed: dict[str, dict]) -> dict:
    """远端条目 → 前端统一结构（屏蔽三类字段名差异）。"""
    name = str(raw.get(f"{kind}_name") or raw.get("name") or "")
    remote_id = str(raw.get(f"{kind}_id") or raw.get("id") or name)
    display = str(
        raw.get("display_name_cn") or raw.get(f"{kind}_name_cn")
        or raw.get("display_name") or name
    )
    hit = installed.get(name) or installed.get(remote_id)
    industries = raw.get("industries") if isinstance(raw.get("industries"), list) else []
    return {
        "id": f"{kind}:{remote_id}",
        "marketId": remote_id,
        "kind": kind,
        "name": name,
        "displayName": display,
        "description": str(raw.get("description_cn") or raw.get("description") or ""),
        "iconUrl": _safe_icon_url(raw.get("icon_url")) or str((hit or {}).get("iconUrl") or ""),
        "author": str(raw.get("author_name") or raw.get("author") or ""),
        "installCount": int(raw.get("install_count") or 0),
        "downloads": int(raw.get("install_count") or 0),
        "version": str(raw.get("version") or ""),
        "categoryCode": str(raw.get("category") or (industries[0] if industries else "")),
        "category": _category_label(raw.get("category") or (industries[0] if industries else "")),
        "tags": [str(t) for t in industries[:4]],
        "featured": False,
        # 已装状态由后端比对得出（单一事实源），前端只据此渲染「安装 / 已安装」
        "installKind": "installed" if hit else "remote",
        "installed": bool(hit),
        "enabled": bool(hit and hit.get("enabled")),
        # plan-284-1451：本机记录 id（技能/连接器才有）——行内开关与卸载用它定位记录
        "localId": (hit or {}).get("localId"),
        "source": "qoder",
        "location": str(raw.get("location") or ""),
        "updatedAt": str(raw.get("content_updated_at") or ""),
    }


async def _fetch_market_page(kind: str, *, page: int, page_size: int,
                             keyword: str, category: str, sort: str) -> dict:
    """拉取远端一页并提取 items / 分页 / 分类维度（不做已装比对）。"""
    params: dict = {
        "extension_types": kind,
        "sort": sort,
        "include_facets": "true",
        "pagination.current_page": page,
        "pagination.page_size": page_size,
    }
    if keyword:
        params["keyword"] = keyword
    if category:
        params["category"] = category

    from app.core.http_client import build_http_client

    async with build_http_client(
        timeout=_MARKET_TIMEOUT, follow_redirects=True, headers=_MARKET_HEADERS
    ) as client:
        resp = await client.get(MARKET_API, params=params)
        resp.raise_for_status()
        data = resp.json()

    block = data.get(f"{kind}s") if isinstance(data, dict) else None
    block = block if isinstance(block, dict) else {}
    pages = block.get("pages") if isinstance(block.get("pages"), dict) else {}
    facets = block.get("facets") if isinstance(block.get("facets"), dict) else {}
    cats = facets.get("categories") if isinstance(facets.get("categories"), list) else []
    return {
        "items": block.get("items") if isinstance(block.get("items"), list) else [],
        "page": int(pages.get("current_page") or page),
        "lastPage": int(pages.get("last_page") or 1),
        "total": int(pages.get("total_size") or 0),
        "categories": [
            {"code": str(c.get("code") or ""), "label": _category_label(c.get("label") or c.get("code") or ""),
             "count": int(c.get("count") or 0)}
            for c in cats if isinstance(c, dict)
        ],
    }


async def browse(db: AsyncSession, kind: str, *, page: int = 1, page_size: int = 40,
                 keyword: str = "", category: str = "", sort: str = "hot") -> dict:
    """市场浏览（远端分页 + 归一化 + 已装比对 + 缓存降级）。

    调用链：缓存（10min） → 远端拉取 → 失败时用过期缓存/本机聚合兜底。
    无论成败都返回同构结构，前端统一按 items/page/lastPage/total 渲染。
    """
    if kind not in _BUNDLED:
        raise ValueError("kind 必须为 plugin / skill / connector")
    page = max(1, int(page or 1))
    page_size = max(1, min(_MARKET_PAGE_SIZE_MAX, int(page_size or 40)))
    sort = sort if sort in ("hot", "new") else "hot"
    keyword = (keyword or "").strip()
    category = (category or "").strip()
    # 「全部」不是远端分类 code，传空表示不筛选
    if category in ("全部", "精选"):
        category = ""

    cache_path = _market_cache_path(kind, page, page_size, keyword, category, sort)
    cached = _read_market_cache(cache_path)
    if cached:
        installed = await _installed_names(db, kind)
        result = _refresh_cached_install_state(cached, installed)
        result["items"] = await _with_local_items(
            db, kind, result["items"], page=page, keyword=keyword, category=category)
        return result

    # plan-284-1450：远端排序只认 hot/latest —— 实测传 `new` 会返回 400 BadRequest，
    # 随后被降级成「离线缓存」并丢掉分类条（用户反馈：点「最新」筛选消失、页面抖动）。
    # 本地语义仍用 hot/new，仅在此映射为远端取值。
    remote_sort = "latest" if sort == "new" else "hot"

    try:
        payload = await _fetch_market_page(
            kind, page=page, page_size=page_size,
            keyword=keyword, category=category, sort=remote_sort,
        )
    except Exception as exc:  # noqa: BLE001 - 远端不可控，任何失败都要降级而非报错
        logger.warning("[market] 远端拉取失败（降级展示）：%s", exc)
        stale = _read_market_cache(cache_path, allow_stale=True)
        if stale:
            installed = await _installed_names(db, kind)
            result = _refresh_cached_install_state(stale, installed)
            result["degraded"] = True
            result["items"] = await _with_local_items(
                db, kind, result["items"], page=page, keyword=keyword, category=category)
            return result
        fallback = await list_catalog(db, kind)
        fallback["degraded"] = True
        fallback["page"] = 1
        fallback["pageSize"] = page_size
        fallback["lastPage"] = 1
        # 本机项（内置 / 已安装）排在最前，保证离线时也能启停
        fallback["items"] = await _with_local_items(
            db, kind, fallback.get("items") or [], page=page, keyword=keyword, category=category)
        fallback["total"] = len(fallback.get("items") or [])
        fallback["installedCount"] = sum(1 for i in fallback["items"] if i.get("installed"))
        # 分类条按 {code,label,count} 渲染；兜底路径此前直接返回字符串数组，
        # 前端渲染出的分类项既无 key 也无文案 —— 表现为「分类整条消失」。
        fallback["categories"] = [
            {"code": c, "label": c, "count": 0}
            for c in (fallback.get("categories") or [])
            if c not in ("全部", "精选")
        ]
        return fallback

    installed = await _installed_names(db, kind)
    items = [_normalize_remote_item(kind, raw, installed)
             for raw in (payload.get("items") or []) if isinstance(raw, dict)]
    # 首页把本机项（内置 / 已安装）排到最前：它们不在远端名录里，但仍需可启停
    items = await _with_local_items(
        db, kind, items, page=page, keyword=keyword, category=category)
    result = {
        "ok": True,
        "kind": kind,
        "items": items,
        "page": payload.get("page", page),
        "pageSize": page_size,
        "lastPage": payload.get("lastPage", 1),
        "total": payload.get("total", len(items)),
        "categories": payload.get("categories") or [],
        "installedCount": sum(1 for i in items if i.get("installed")),
        "marketUrl": MARKET_URL,
        "cached": False,
    }
    _write_market_cache(cache_path, result)
    return result


async def install_item(db, *, kind: str, ident: str, display_name: str = "",
                       description: str = "") -> dict:
    """把市场条目安装到本机（plan-41-225 S6）。

    三类均走「详情接口 → 下载/提取 → 安全落盘 → 注册」同一条链路：
      · 技能：详情的 download_url（OSS 直链 zip）→ 解压到 ~/.chatcoder/skills/qoder/<name>/
              → 写 skills 表（source=qoder）；包不可用时回退详情正文入库；
      · 插件：详情的 download_url → 解压 → 复用插件安装流程（plugins 目录 + 注册表
              + 贡献技能注册），与本机目录安装在结果上完全一致；
      · 连接器：详情 config.servers[] → 写 mcp_servers（默认**不启用**，
              由用户在扩展管理里显式开启——远端 MCP 会把请求发往第三方服务）。
    """
    from app.services import market_install

    return await market_install.install_item(
        db, kind=kind, ident=ident,
        display_name=display_name, description=description,
    )
