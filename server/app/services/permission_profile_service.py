"""权限模式配置服务（plan-230-1144 M2）。

改造前问题：工具白名单硬编码在 `engine._READONLY_TOOLS` / `_PLAN_TOOLS`，
模式提示词硬编码在 `_MODE_HINTS`，前端 `setMode` 只接受 3 个值——
"模式自定义、各种模式的权限分配不够自由"无从谈起。

本模块把"模式 → 工具白名单 + 提示词"外置为可配置数据：

- **内置 3 模式**（agent / readonly / plan，plan-75-332）：白名单沿用 engine 原定义，
  作为默认集，用户可覆盖其白名单与提示词；
- **自定义模式**：用户新建，`builtin=False`，白名单从全量工具中勾选，
  持久化到用户 config.json 的 `permission_profiles` 键；
- **安全兜底（新旧双跑取更严方）**：`resolve_tools` 返回的白名单只允许是
  全量注册工具的子集；未识别的自定义模式名在 engine 侧按 `agent` 处理。

模式语义（`kind` 字段，供 engine/executor 复用既有判定分支）：
- `full`：全量工具（agent 属此类）；
- `readonly`：仅只读工具；
- `plan`：只读 + 计划文档写。

plan-75-332 变更：原 `default`（完全访问）更名为 `agent`（智能体模式），原
`accept_edits`（计划执行）删除——两者能力完全相同（全量工具），保留两个等价
选项只会让用户在选择时困惑。存量旧名由 `_LEGACY_NAMES` 映射到 `agent`。

存储位置与 `skills_mcp.py` 的技能仓库配置同址（~/.chatcoder/config.json，
可经 CHATCODER_USER_CONFIG 覆盖），不新增表、不需要迁移。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

_CFG_PATH = Path(
    os.environ.get("CHATCODER_USER_CONFIG", str(Path.home() / ".chatcoder" / "config.json"))
)

# ── 内置模式定义（白名单与 engine 原硬编码保持一致，外置后仍是唯一事实源）──
# plan-75-332 R2（用户指定）：菜单顺序固定为「只读 → 计划 → 智能体」——
# 按能力由窄到宽递进，故内置列表按此顺序声明；list_profiles 按本列表顺序
# 返回，前端菜单直接消费（agent 定义已移至列表末尾）。

# plan-20261016-ux（问题 7）：只读 / 计划模式的默认白名单补齐——此前缺少审批策略本已允许的工具，
# 新用户开箱即「缺工具」：目标模式下无法 goal_complete 结束续跑、计划模式能后台跑命令却查不了状态、
# 浏览器只能打开不能读取、git 历史看不了、记忆写不进。补齐范围严格对齐 approval_policy 的执行模式边界：
# 只放行在只读 / 计划模式下会被裁决为「读」的工具（git 写子命令、网页点击输入、电脑键鼠操作仍由边界拒绝）。
_READ_CORE = [
    "fs_read", "fs_list", "fs_grep", "git_diff",
    "memory_search", "web_fetch", "web_search", "view_image",
    "read_attachment", "codebase_search",
    "ask_user_question", "todo_write",
    "skill_view", "compaction_index", "compaction_view",
    # plan-230-1144 M3: 符号检索/文件骨架属纯读探索能力
    "symbol_search", "outline",
    # plan-549-2281：功能地图查询与符号索引开启——两者都不改项目文件，
    # 且代码里的引导文案（symbol_search 索引未开时、主提示词的工具选择表）
    # 都要求模型直接调用它们；不进白名单会让只读 / 计划模式拿到「未知工具」。
    "feature_lookup", "symbol_index_enable",
    # git 只读子命令（log / blame / show / status / diff …）；写操作由执行模式边界拒绝
    "git",
    # 记忆写入与目标完成：两者都不改项目文件，审批策略按「读」裁决
    "memory_write", "goal_complete",
    # 下一步建议卡只在智能体 / 工作流模式提供（且需在设置 → 常规开启），
    # 只读 / 计划模式的白名单里不放 suggest_next_task
]
# 内置浏览器：打开与读取页面（点击 / 输入 / 执行脚本由执行模式边界拒绝，不放进白名单）
_BROWSER_READ = [
    "browser_navigate", "browser_tabs", "browser_snapshot", "browser_get_text",
    "browser_screenshot", "browser_scroll", "browser_hover", "browser_wait", "browser_logs",
]
# 电脑操控：读屏类（键鼠操作与切换窗口不放进白名单）
_DESKTOP_READ = [
    "desktop_windows", "desktop_snapshot", "desktop_screenshot",
]
# v36 (plan-321-1600 R1): 四模式全支持子代理——只读 / 计划模式仅暴露只读探索子代理（运行时强制 explore）
_SUBAGENT = [
    "spawn_subagent", "collect_results", "subagent_inspect", "cancel_subagent", "resume_subagent",
    "send_to_subagent",
]
# 工作流（设计 §5.8）：只读 / 计划模式可规划与查询；启动 / 续跑 / 取消在运行时拒绝
_WORKFLOW_READ = ["plan_workflow", "workflow_status"]

READONLY_TOOLS: list[str] = [*_READ_CORE, *_BROWSER_READ, *_DESKTOP_READ, *_SUBAGENT, *_WORKFLOW_READ]
# 计划模式：只读 + 写计划文档（fs_write，限 ai/*.md）+ 命令行（只读命令）+ 后台命令状态查询
PLAN_TOOLS: list[str] = [*READONLY_TOOLS, "fs_write", "terminal_exec", "terminal_bg_status"]

BUILTIN_PROFILES: list[dict] = [
    {
        "name": "readonly",
        "display_name": "只读模式",
        "kind": "readonly",
        "builtin": True,
        "description": "仅查看、检索、分析，禁止任何修改",
        "tools": [*READONLY_TOOLS],
        "hint": (
            "【审阅模式】当前处于只读审阅模式，你只能查看、检索、分析代码，"
            "严禁执行任何修改操作（禁止写入文件、运行命令、应用补丁）；"
            "可以用 git 工具查看提交历史与差异（log / blame / show / diff），可以打开与读取网页。"
            "请直接给出审阅意见、发现的问题与改进建议。"
        ),
    },
    {
        "name": "plan",
        "display_name": "计划模式",
        "kind": "plan",
        "builtin": True,
        "description": "先规划后执行：只读工具 + 计划文档写入 + 命令行",
        "tools": [*PLAN_TOOLS],
        # plan 的完整提示词较长且含 {session_id}/{turn_id} 占位符，
        # 仍由 engine._MODE_HINTS 维护（格式化处理在那边）；这里留空表示沿用。
        "hint": "",
    },
    {
        "name": "agent",
        "display_name": "智能体模式",
        "kind": "full",
        "builtin": True,
        # S13（plan-41-197）：系统模式说明补全——“全量工具”不是缺陷而是语义定位，
        # 把能力边界写清楚。plan-75-332：改名 agent，避免与权限模式的
        # 「完全访问」重名（前者说能力范围，后者说是否免询问）。
        "description": "全量工具；是否需审批由权限模式决定（日常开发默认）",
        "tools": [],  # 空列表 = 全量工具（不限制）
        "hint": "",
    },
    {
        # plan-workflow-collab：工作流模式——能力同智能体，任务默认以工作流（多步协同、评估返工）完成；
        # 模式提示词由 engine._MODE_HINTS 维护
        "name": "workflow",
        "display_name": "工作流模式",
        "kind": "full",
        "builtin": True,
        "description": "全量工具；复杂任务默认拆成工作流，多步协同、评估把关、确认后返工",
        "tools": [],
        "hint": "",
    },
]

_BY_NAME = {p["name"]: p for p in BUILTIN_PROFILES}

# plan-75-332: 旧模式名 → 新模式名。default（原「完全访问」）与 accept_edits（原「计划执行」）
# 的能力完全相同（全量工具、无白名单限制），统一并入 agent。存量会话与用户配置里
# 可能还留着旧名，在此映射；否则解析不到模式会让白名单校验静默失效。
_LEGACY_NAMES = {"default": "agent", "accept_edits": "agent"}


def _canonical(name: str) -> str:
    """模式名归一化：旧名映射到新名，其余原样返回。"""
    n = (name or "").strip()
    return _LEGACY_NAMES.get(n, n)


def _read_cfg() -> dict:
    try:
        if _CFG_PATH.exists():
            data = json.loads(_CFG_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        logger.debug("[permission_profile] 读取用户配置失败", exc_info=True)
    return {}


# ── 读改写互斥 ────────────────────────────────────────────────────────────
# 【为什么是线程锁而不是 asyncio.Lock】本模块的 upsert/delete 都是**同步函数**，
# 由 gateway 的 async 路由直接调用（`permission_profiles.py:32` 未走线程池）。同步函数里
# 不能 await，`asyncio.Lock` 在这个调用形态下拿不到；而 read→改动→整文件覆盖 的临界区
# 一旦交错，后写的进程会用自己读到的旧快照覆盖掉别人刚写进去的 profile（实测丢配置）。
# 因此用 `threading.Lock`：它能同时覆盖「事件循环线程里并发调用」与「线程池/worker 进程内
# 并发调用」两种形态（同进程内多个 asyncio task 顺序执行同步代码，本就不会交错，锁主要防
# 线程池；跨进程另见文件锁）。
_CFG_LOCK = threading.RLock()

# 跨进程互斥：`msvcrt.locking` 只在 Windows 可用、`fcntl` 只在 POSIX 可用，二者都是标准库，
# 不引入新依赖。锁加在**独立的 .lock 文件**上而不是配置文件本身——配置文件会被 os.replace
# 换掉（inode/句柄失效），锁文件本身永不替换，才能长期持有稳定的字节区间锁。
# 拿不到平台实现时（理论上不会）退化为「只有进程内锁」，并在日志里留痕。
_LOCK_SUFFIX = ".lock"
_LOCK_TIMEOUT_S = 5.0
# 写临时文件的后缀：与 feature_map_service.save_map 同思路（临时文件 + os.replace）
_TMP_SUFFIX = ".tmp"


def _lock_path() -> Path:
    return _CFG_PATH.with_name(_CFG_PATH.name + _LOCK_SUFFIX)


@contextmanager
def _file_lock():
    """跨进程互斥（阻塞式，带超时）。拿不到锁只记日志，**不阻断**本次写入。

    为什么允许降级：权限模式配置的写入是低频用户操作，一次锁竞争
    失败就抛错会让「保存模式」直接报 500；这里选择「尽力串行 + 留痕」，配合原子替换
    （读到的永远是完整的旧文件或完整的新文件）已经能保证文件不损坏。真正丢更新的窗口
    只在两个进程同一瞬间写同一键时出现，概率远低于此前的无锁实现。
    """
    path = _lock_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a+b")  # noqa: SIM115 —— 需跨上下文持有，不能 with 一起退出
    except OSError:
        logger.debug("[permission_profile] 打开锁文件失败，退化为仅进程内互斥", exc_info=True)
        yield
        return

    locked = False
    try:
        # 锁需要落在**已存在的字节区间**上：Windows 的 msvcrt.locking 对空文件的锁定请求
        # 会失败，那样每次写都要白等满 _LOCK_TIMEOUT_S 才退化——比不加锁还慢。
        # 锁文件只用于加锁，内容无意义，写一个占位字节即可。
        try:
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")
                handle.flush()
        except OSError:
            logger.debug("[permission_profile] 初始化锁文件失败（继续尝试加锁）", exc_info=True)

        deadline = time.monotonic() + _LOCK_TIMEOUT_S
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    # 非阻塞模式（LK_NBLCK）自旋重试：阻塞模式（LK_LOCK）超时行为在不同
                    # Windows 版本上不一致，且会卡死整个写路径。
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except (OSError, ImportError):
                if time.monotonic() >= deadline:
                    logger.debug(
                        "[permission_profile] 等待配置文件锁超时（%.1fs），继续写入", _LOCK_TIMEOUT_S,
                    )
                    break
                time.sleep(0.02)
        yield
    finally:
        if locked:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                logger.debug("[permission_profile] 释放配置文件锁失败", exc_info=True)
        try:
            handle.close()
        except OSError:
            pass


@contextmanager
def _cfg_transaction():
    """配置文件的「读 → 改 → 写」临界区：进程内锁 + 跨进程文件锁。"""
    with _CFG_LOCK, _file_lock():
        yield


def _write_cfg(data: dict) -> None:
    """整文件写回，改**原子替换**（临时文件 + os.replace）。

    为什么必须原子替换：此前直接 `write_text` 先截断再写，写到一半崩溃/被杀就留下半截 JSON，
    下次 `_read_cfg` 解析失败返回 `{}` —— 用户所有自定义模式一次性消失（且无任何报错）。
    临时文件与目标同目录（os.replace 跨盘会退化成复制，不原子），写完再替换。
    """
    _CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _CFG_PATH.with_name(_CFG_PATH.name + _TMP_SUFFIX)
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, _CFG_PATH)
    except OSError:
        logger.warning("[permission_profile] 写入用户配置失败 %s", _CFG_PATH, exc_info=True)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _load_custom() -> list[dict]:
    raw = _read_cfg().get("permission_profiles", [])
    if not isinstance(raw, list):
        return []
    out = []
    for p in raw:
        if isinstance(p, dict) and p.get("name"):
            raw_name = str(p["name"])
            # plan-75-332: 旧模式名的用户覆盖条目直接忽略（default / accept_edits
            # 已并入 agent）；若照原样返回，输入框模式菜单会多出语义重复的选项。
            if raw_name in _LEGACY_NAMES:
                continue
            out.append({
                "name": raw_name,
                "display_name": str(p.get("display_name") or p["name"]),
                "kind": str(p.get("kind") or "full"),
                "builtin": False,
                "description": str(p.get("description") or ""),
                "tools": [str(t) for t in (p.get("tools") or [])],
                "hint": str(p.get("hint") or ""),
            })
    return out


def list_profiles() -> list[dict]:
    """全部模式 = 内置（可能被用户覆盖）+ 自定义。"""
    overrides = {p["name"]: p for p in _load_custom() if p["name"] in _BY_NAME}
    merged = []
    for p in BUILTIN_PROFILES:
        merged.append(overrides.get(p["name"], p))
    merged.extend(p for p in _load_custom() if p["name"] not in _BY_NAME)
    return merged


def get_profile(name: str) -> dict | None:
    target = _canonical(name)
    for p in list_profiles():
        if p["name"] == target:
            return p
    return None


def resolve_tools(mode: str, all_tool_names: set[str]) -> list[str] | None:
    """把模式解析为工具白名单。

    - `agent` / 未识别模式 → None（全量，不限制）；
    - 内置只读/计划/自定义模式 → 白名单列表（过滤掉未注册的幽灵工具名）。

    安全口径：白名单只能从 `all_tool_names` 中取（deny-by-default），
    配置里写了不存在的工具名不会产生越权。
    """
    profile = get_profile(mode)
    if profile is None:
        return None
    tools = profile.get("tools") or []
    if not tools:
        return None  # 空 = 全量
    return [t for t in tools if t in all_tool_names]


def resolve_hint(mode: str) -> str:
    """自定义模式的提示词；内置模式返回空串（由 engine 既有逻辑处理）。"""
    profile = get_profile(mode)
    if profile and not profile.get("builtin"):
        return profile.get("hint") or ""
    return ""


def mcp_allowed_tools(mode: str) -> set[str] | None:
    """MCP 工具的注入白名单（None = 不限制）。

    MCP 工具名（`mcp_<server>_<tool>`）随用户配置动态变化，无法预先写进
    内置模式白名单，此前 `_inject_mcp_tools` 因此对它们"无条件注入"——
    设置页里把某个 MCP 工具排除在白名单外并不生效。现按模式类型分流：

    - 只读/计划类（kind=readonly/plan）：返回 None，低风险过滤由 engine 的
      `readonly_only` 规则承担（保持既有行为：规划/审阅时仍可用只读类 MCP 工具）；
    - 显式勾选了工具的模式（tools 非空）：只放行其中列出的 MCP 工具，
      未勾选即不注入——设置页勾选对 MCP 工具同样生效；
    - default / accept_edits 等空列表（= 全量工具）：返回 None（全量注入）。
    """
    profile = get_profile(mode)
    if profile is None:
        return None
    kind = profile.get("kind") or "full"
    if kind in ("readonly", "plan"):
        return None
    tools = profile.get("tools") or []
    if not tools:
        return None  # 空 = 全量
    return {str(t) for t in tools}


def is_readonly_like(mode: str) -> bool:
    """该模式是否属于只读类（MCP 注入只放行只读工具的依据）。"""
    profile = get_profile(mode)
    if profile is None:
        return False
    kind = profile.get("kind") or "full"
    return kind in ("readonly", "plan")


# ── 模式名 → 判定档（plan-560-2311 C1：模式归一的**单一事实源**）──
# 【为什么落在本模块】归一口径原先有两份半实现：`approval_policy.normalize_execution_mode`
# 只做旧名映射、对自定义模式名原样返回；`subagent_capabilities.effective_mode` 按 kind 归一
# 却没人调用。审批层若反向 import 后者会成环（subagent_capabilities 已 import approval_policy），
# 故下沉到本模块——它只依赖标准库与模式配置，编排层任何模块都能安全依赖。
#
# 【为什么未知模式名按 agent】未识别模式名（例如用户删掉的自定义模式，会话里还留着旧名）
# 在本模块的 `resolve_tools` / `is_readonly_like` 与 engine 的工具下发里都按「全量工具」处理；
# 若判定期把它当 plan，就会出现「工具面全量可写、写操作却一律被拒」的自相矛盾——那是倒退，
# 不是收紧。kind 取值域已由 upsert_profile 限定为 full/readonly/plan，读不到 kind 时才保守回落。
MODE_KIND_READONLY = "readonly"
MODE_KIND_PLAN = "plan"
MODE_KIND_AGENT = "agent"
MODE_KIND_WORKFLOW = "workflow"

_BUILTIN_MODE_KIND: dict[str, str] = {
    "readonly": MODE_KIND_READONLY,
    "plan": MODE_KIND_PLAN,
    "agent": MODE_KIND_AGENT,
    "workflow": MODE_KIND_WORKFLOW,
}
_KIND_TO_MODE_KIND: dict[str, str] = {
    "readonly": MODE_KIND_READONLY,
    "plan": MODE_KIND_PLAN,
    "full": MODE_KIND_AGENT,
}
_MODE_KIND_UNKNOWN = MODE_KIND_AGENT

# plan-562-2318 M4：判定档的进程内缓存（键为规范化后的模式名）。
# 为什么需要：`resolve_mode_kind` 是**每个工具调用**的必经点（审批层 `approval_policy.decide`
# 每次都调它判边界）。内置四档命中 `_BUILTIN_MODE_KIND` 就返回、零 IO；但自定义模式要走
# `get_profile → list_profiles → _load_custom → _read_cfg`，即两次同步 `read_text + json.loads`
# 落在事件循环线程里——工具步密集时逐次阻塞。这里缓存结果，profile 变更（upsert/delete）时清空。
_MODE_KIND_CACHE: dict[str, str] = {}

# 缓存只在「本进程自己写配置」时清空，别的进程（索引 worker / 另一实例）
# 改了配置文件，本进程永远读不到新 kind——用户改了模式类型，审批边界却还是旧的。
# 修法：记住**缓存建立时配置文件的状态指纹**，每次读缓存前比对，变了就整表作废重读。
# 指纹用 mtime_ns + size 而不是只 mtime：Windows 的 FAT/exFAT 与部分同步工具 mtime 精度只到
# 2 秒、甚至整秒，同一秒内两次写入会被当成「没变」；size 补上「内容长度变了」的常见情况。
# 这样每次调用只多一次 `stat()`（微秒级），比重新 `read_text + json.loads` 便宜一个量级。
_CACHE_STAMP: tuple[int, int] | None = None


def _cfg_stamp() -> tuple[int, int]:
    """配置文件的状态指纹（mtime_ns, size）；文件不存在返回 (0, -1)。"""
    try:
        st = _CFG_PATH.stat()
    except OSError:
        return (0, -1)
    return (st.st_mtime_ns, st.st_size)


def _clear_mode_kind_cache() -> None:
    """清空判定档缓存（profile 增删改后必须调用，否则新 kind 不生效）。"""
    global _CACHE_STAMP
    _MODE_KIND_CACHE.clear()
    # 清空后下一次写入缓存时重新取指纹；置 None 表示「尚无有效指纹」
    _CACHE_STAMP = None


def _sync_cache_stamp_if_changed() -> None:
    """配置文件指纹变了就整表作废（跨进程缓存失效）。"""
    global _CACHE_STAMP
    stamp = _cfg_stamp()
    if _CACHE_STAMP is None:
        _CACHE_STAMP = stamp
        return
    if stamp != _CACHE_STAMP:
        logger.debug(
            "[permission_profile] 配置文件已被其它进程修改，判定档缓存失效 %s -> %s",
            _CACHE_STAMP, stamp,
        )
        _MODE_KIND_CACHE.clear()
        _CACHE_STAMP = stamp


def resolve_mode_kind(mode: str | None) -> str:
    """把任意模式名（内置 / 自定义 / 未知）归一为判定档。

    返回值固定取自 readonly / plan / agent / workflow 四档：审批层的 `_mode_boundary`、
    子代理能力矩阵、`agent_loop` 的模式护栏、`context_manager` 的提示词选择都按它判定。

    plan-560-2311 C1：修的就是「自定义 kind=readonly/plan 模式绕过硬边界」——调用方原先
    拿模式名和内置字面量做等值比较，自定义名两者都不等，于是边界被整体跳过。

    口径：内置名直接命中（绝大多数调用零 IO）；自定义模式按 profile 的 kind 映射
    （full → agent）；空值按 agent（与 `approval_policy.normalize_execution_mode` 一致）。
    **只读判定、不写回会话值**——会话里存的自定义模式名必须原样保留（见 test_session_modes）。
    """
    name = _canonical(str(mode or "").strip())
    builtin = _BUILTIN_MODE_KIND.get(name.lower())
    if builtin is not None:
        return builtin
    # 读缓存前先比对配置文件指纹——别的进程（索引 worker / 另一实例）
    # 改了 profile，本进程的缓存必须失效，否则审批边界会一直按旧 kind 判定。
    _sync_cache_stamp_if_changed()
    cached = _MODE_KIND_CACHE.get(name.lower())
    if cached is not None:
        return cached
    profile = get_profile(name)
    kind = _MODE_KIND_UNKNOWN if profile is None else _KIND_TO_MODE_KIND.get(
        str(profile.get("kind") or ""), _MODE_KIND_UNKNOWN)
    _MODE_KIND_CACHE[name.lower()] = kind
    # 记录本次缓存建立时的指纹，供下次比对（若已在 _sync_cache_stamp_if_changed 里取过则复用）
    global _CACHE_STAMP
    if _CACHE_STAMP is None:
        _CACHE_STAMP = _cfg_stamp()
    return kind


def mcp_explicit_tools(mode: str) -> set[str]:
    """用户在权限面板显式勾选的 MCP 工具名（v36 plan-321-1600 R1）。

    只读/计划类模式默认只注入只读类（low 风险）MCP 工具；但用户在模式白名单里
    显式勾选的 MCP 工具应额外放行（用户明确授权）——否则「勾选了仍不可用」。
    返回勾选集合（可能为空集）。
    """
    profile = get_profile(mode)
    if not profile:
        return set()
    return {str(t) for t in (profile.get("tools") or []) if str(t).startswith("mcp_")}


def upsert_profile(profile: dict) -> dict:
    """新建/更新自定义模式，或覆盖内置模式的白名单/提示词。

    校验：name 只允许小写字母/数字/下划线；tools 过滤到已注册集合外仍保留
    （前端按全量工具勾选，此处不做注册校验——未注册工具名在
    resolve_tools 时会被过滤，不产生越权）。
    """
    import re
    name = _canonical(str(profile.get("name") or "").strip())
    if not re.fullmatch(r"[a-z0-9_]{1,40}", name):
        raise ValueError("模式名仅允许小写字母、数字、下划线（1-40 字符）")
    if profile.get("kind") not in (None, "full", "readonly", "plan"):
        raise ValueError("kind 必须为 full/readonly/plan 之一")

    entry = {
        "name": name,
        "display_name": str(profile.get("display_name") or name),
        "kind": str(profile.get("kind") or (
            _BY_NAME[name]["kind"] if name in _BY_NAME else "full"
        )),
        "description": str(profile.get("description") or ""),
        "tools": [str(t) for t in (profile.get("tools") or [])],
        "hint": str(profile.get("hint") or ""),
    }
    # 读 → 改 → 写整段进临界区（`_read_cfg` 必须在锁内）。
    # 此前三步各自独立，两个请求并发时后写的会用自己读到的旧快照覆盖掉对方刚写入的
    # profile（用户看到「刚保存了 A，B 不见了」）。entry 构造不依赖配置，可留在锁外。
    with _cfg_transaction():
        data = _read_cfg()
        profiles = data.get("permission_profiles", [])
        if not isinstance(profiles, list):
            profiles = []
        profiles = [p for p in profiles if not (isinstance(p, dict) and p.get("name") == name)]
        profiles.append(entry)
        data["permission_profiles"] = profiles
        _write_cfg(data)
    # plan-562-2318 M4：kind 可能变了，判定档缓存必须作废
    _clear_mode_kind_cache()
    return {**entry, "builtin": name in _BY_NAME}


def delete_profile(name: str) -> bool:
    """删除自定义模式；内置模式只能重置覆盖（删除覆盖条目）。"""
    # 与 upsert_profile 同一临界区口径，避免并发删除/保存互相覆盖。
    with _cfg_transaction():
        data = _read_cfg()
        profiles = data.get("permission_profiles", [])
        if not isinstance(profiles, list):
            return False
        kept = [p for p in profiles if not (isinstance(p, dict) and p.get("name") == name)]
        if len(kept) == len(profiles):
            return False
        data["permission_profiles"] = kept
        _write_cfg(data)
    # plan-562-2318 M4：同 upsert，删除后该名字的判定档缓存作废
    _clear_mode_kind_cache()
    return True
