"""微应用后端工具面（plan-693-2868 §6.1 / §6.4 / §7 P0 第 5 条）。

**为什么要有这一层**：微应用的能力面必须先以「工具」形态存在——ta3 伪装通道
（`mcp__workspace-tools__microapp_*`，见 `app/models/providers/ta3_tool_aliases.py` 的
`_EXTENSION_REAL_NAMES`）、只读子代理白名单（`subagent_tools.EXPLORE_TOOLS`，决策 D9
只放行列表）、以及后续 `/api/microapps` 路由（§6.5）都从注册表取同一份口径；
三处各写一套必然漂移。

**库根与目录结构**（§6.1，与 Electron 主进程 spawn 时注入的 `CHATCODER_MICROAPP_ROOT` 同源）：

    <libraryRoot>/<appId>/<version>/manifest.json    ← 入口为 manifest 的 entry 字段

变量缺失时回落 `~/.chatcoder/microapps` 并记日志：静默回落会让「工具读到空库」与
「用户根本没装」变得无从区分。

**界面方法调用**（§7 P0-4）：校验通过后经浏览器桥（`bridge.request`，op=`microappCall`）把调用
交给桌面端主进程，由它在微应用 guest webview 里执行 `window.__MICROAPP_METHODS__[method](args)`
并把返回值原样带回。这条通道复用浏览器宿主那条 WS 连接（本机回环 `/ws/global`，见
`app/orchestration/browser/bridge.py`），**不新增端口 / 令牌 / 协议版本**。

因此 `microapp_call` 的硬前提只剩「桌面端在运行」：界面没打开时，宿主会先在右侧面板把该微应用的
标签挂上（webview 一挂上就开始加载）再调用，**但不展开面板**——面板的打开权只在用户手上
（`docs/app1/README.md` 的四条界面约定）。界面挂不上 / 一直没就绪时明确报 `EXEC_FAILED` 并说清原因，
宁可让模型知道「界面没就绪」，也不要假装调用成功（后者会让模型以为界面已响应）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.orchestration.tools.base import Tool, ToolContext, ToolResult
# 外置微应用（protocol 13-external-microapps）：与内置微应用共存，只共用工具入口
from app.orchestration.tools.microapps_ext import (
    ExternalCallError,
    ExternalMicroApp,
    call_method,
    declared_methods,
    find_external_microapp,
    list_external_microapps,
    microapp_ext_root,
)

logger = logging.getLogger(__name__)

# 主进程 spawn 后端时注入（§6.1 末段）；缺失时按 D6 回落默认库根
MICROAPP_ROOT_ENV = "CHATCODER_MICROAPP_ROOT"
# D26：appId 形状由发布校验 / Release tag / 目录名三方共用，放宽即产生无法发布的目录
APP_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{1,62}$")
# 版本号：接受 1.2.3 / 1.2.3-beta.1 / 1.2.3+build；Release tag = <appId>-<version>
_VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
# 方法名按 ECMAScript 标识符收口：它最终要贴成页面里可被调用的桥接函数名
_METHOD_NAME_PATTERN = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]{0,63}$")

# 单次调用要等界面方法自己跑完（主进程侧上限 15 s）：后端留出余量，
# 让「界面方法超时」这条具体原因先到，而不是被桥的超时抢先替换成笼统的「宿主没回包」。
# 注意：桌面端在界面没打开时会先用最多 5 s 等 WebView 就绪（plan-693-2868 的自动挂载），
# 与 15 s 的方法上限叠加后会顶到本值。26 s = 5 s 就绪 + 15 s 方法 + 6 s IPC 往返余量，
# 保证「界面方法超时」这条具体原因先到，而不是被桥抢先替换成笼统的「宿主没回包」；
# 改本值必须同步下面「宿主 26 秒」的文案与测试断言。
MICROAPP_CALL_TIMEOUT = 26.0
# output 里内联返回值的最长字符数：结果要能让模型直接读懂，又不能被一张大表吃掉上下文
MICROAPP_RESULT_MAX_CHARS = 4000

# 主进程（electron/browser-agent.cjs 的 MICROAPP_ERR）用这些前缀回报失败原因。
# 前缀是两端的约定：任一侧改名字都要同步另一侧，否则错误会退化成「未归类失败」。
_BRIDGE_ERROR_HINTS: dict[str, str] = {
    "microapp-guest-missing": (
        "界面没能自动挂载：调用前已尝试在右侧面板为它加一个标签（webview 一挂上就开始加载），"
        "但仍没找到这个版本的界面。先确认桌面端在运行、该 appId + version 确实在库内"
        "（microapp_list 可核对版本）；仍失败就让用户在右侧面板手动打开该微应用后再试，"
        "不要反复重试同一调用。"
    ),
    "microapp-guest-loading": (
        "微应用界面正在加载（标签已挂上，但界面还没出桥）：宿主已等过 5 秒仍没就绪。"
        "让用户看一眼右侧面板该微应用的界面是否停在空白 / 报错，确认后再重试一次。"
    ),
    "microapp-bridge-missing": (
        "页面没有注册 window.__MICROAPP_METHODS__：确认 app.js 在顶层执行了 "
        "`window.__MICROAPP_METHODS__ = methods`，且没有被 CSP 拦下（见 microapp-create 技能）。"
    ),
    "microapp-method-missing": (
        "manifest 声明了该方法，但页面注册表里没有同名函数：补齐 app.js 的实现"
        "（方法名大小写要与 manifest 完全一致）后重新打包安装。"
    ),
    "microapp-method-invalid": (
        "方法名不是合法的 JavaScript 标识符，无法在页面里安全取用；改用 manifest 里声明的方法名。"
    ),
    "microapp-not-serializable": (
        "方法返回值需可 JSON 序列化：返回普通对象 / 数组 / 字符串 / 数字，"
        "不要返回 DOM 节点、函数或带循环引用的对象。"
    ),
    "microapp-threw": (
        "页面里的方法执行时抛错（原因见 error）。修正 app.js 的实现后重新打包安装；"
        "界面状态可能已被这次调用改了一半，重试前先看一眼界面。"
    ),
    "microapp-timeout": (
        "界面方法超时没返回：它可能仍在执行（方法里有死循环或未 resolve 的 Promise）。"
        "先确认界面现状再决定是否重试，不要直接重复同一调用。"
    ),
}


def _split_bridge_error(message: str) -> tuple[str, str]:
    """拆开主进程回的错误串 `microapp-xxx: 细节`，返回 (前缀, 细节)；无前缀时前缀为空串。"""
    for prefix in _BRIDGE_ERROR_HINTS:
        if message.startswith(prefix):
            return prefix, message[len(prefix):].lstrip("：: ").strip()
    return "", message


def microapp_root() -> Path:
    """微应用库根：环境变量优先，缺失回落默认目录并记一条 warning（§6.1）。"""
    raw = (os.environ.get(MICROAPP_ROOT_ENV) or "").strip()
    if raw:
        return Path(raw).expanduser()
    fallback = Path.home() / ".chatcoder" / "microapps"
    logger.warning(
        "[microapp] 环境变量 %s 未设置，回落默认库根 %s"
        "（主进程 spawn 后端时应注入，见 plan-693-2868 §6.1）",
        MICROAPP_ROOT_ENV, fallback,
    )
    return fallback


def _version_sort_key(name: str) -> tuple:
    """版本目录排序键：能解析的按数字段比大小，不能解析的排在最前（当作最旧）。

    返回元组形状保持一致（int, tuple, str），否则不同类型段之间比较会抛 TypeError。
    """
    if not _VERSION_PATTERN.match(name):
        return (0, (), "")
    core = name.partition("+")[0].partition("-")[0]
    return (1, tuple(int(part) for part in core.split(".")), name)


def _read_manifest(path: Path) -> dict[str, Any] | None:
    """读 manifest；坏 JSON / 非对象 / 读不到一律返回 None（调用方决定是跳过还是报问题）。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


@dataclass
class _Install:
    """库内一份已安装的微应用（同一 appId 只保留版本最高的一份）。"""

    app_id: str
    version: str
    version_dir: Path
    manifest: dict[str, Any]


def _scan_installs(root: Path) -> list[_Install]:
    """扫描库根，每个 appId 取版本号最大的那一份；坏目录跳过而不抛异常。

    为什么按 appId 去重取最新：同一应用可能装着多个版本（§6.1 的 `<appId>/<version>`），
    列表与调用必须指向唯一一份，否则「装了 v2 却调到 v1」这类问题无从解释。
    `.vendor` / `.community` 等点开头目录不是应用（§6.1 目录图），直接跳过。
    """
    if not root.is_dir():
        return []
    installs: list[_Install] = []
    try:
        app_dirs = sorted(root.iterdir())
    except OSError as exc:  # 库根不可读（权限 / 断链）不是「没有应用」，交给上层报错
        raise OSError(f"库根不可读: {root}") from exc

    for app_dir in app_dirs:
        if not app_dir.is_dir() or app_dir.name.startswith("."):
            continue
        if not APP_ID_PATTERN.match(app_dir.name):
            # 非法名的目录调用侧永远 ARG_INVALID，但静默隐藏会让人以为「装了没生效」
            logger.warning("[microapp] 跳过 appId 不合法的目录: %s", app_dir)
            continue
        best: _Install | None = None
        try:
            version_dirs = list(app_dir.iterdir())
        except OSError:
            continue
        for version_dir in version_dirs:
            if not version_dir.is_dir():
                continue
            manifest_file = version_dir / "manifest.json"
            if not manifest_file.is_file():
                continue
            manifest = _read_manifest(manifest_file)
            if manifest is None:
                continue
            candidate = _Install(app_dir.name, version_dir.name, version_dir, manifest)
            if best is None or (
                _version_sort_key(candidate.version) > _version_sort_key(best.version)
            ):
                best = candidate
        if best is not None:
            installs.append(best)
    return installs


def _find_install(root: Path, app_id: str) -> _Install | None:
    for install in _scan_installs(root):
        if install.app_id == app_id:
            return install
    return None


def _method_names(manifest: dict[str, Any]) -> list[str]:
    """manifest.methods 里所有合法的 name（其余形态由 microapp_validate 逐条报问题）。"""
    raw = manifest.get("methods")
    if not isinstance(raw, list):
        return []
    names: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            name = item.get("name")
            if isinstance(name, str) and name:
                names.append(name)
    return names


def _safe_entry(version_dir: Path, entry: str) -> Path | None:
    """入口必须落在应用自己的版本目录内——`../` 会把读取带到库外（§7 P0 第 3 条同一口径）。"""
    try:
        base = version_dir.resolve()
        target = (version_dir / entry).resolve()
    except OSError:
        return None
    return target if target.is_relative_to(base) else None


def _pick_version_dir(app_dir: Path) -> Path | None:
    """校验用：取版本号最大的子目录，**不要求** manifest 可解析（解析失败本身就是要报的问题）。"""
    if not app_dir.is_dir():
        return None
    try:
        candidates = [d for d in app_dir.iterdir() if d.is_dir()]
    except OSError:
        return None
    if not candidates:
        return None
    candidates.sort(key=lambda d: _version_sort_key(d.name))
    return candidates[-1]


def _validate_manifest(manifest: dict[str, Any], app_id: str, version_dir: Path) -> list[str]:
    """逐条校验 manifest 内容，返回问题列表（空 = 全过）。

    清单项来自 §7 P0 第 2 条的定稿 schema：appId / name / version / description / entry /
    methods / permissions / runtime.vendors。这里只做**结构**校验——内容审核明确不做（D14）。
    """
    problems: list[str] = []

    # appId：与目录名一致是安装与 Release tag 的前提（D26 / §6.4）
    declared_id = manifest.get("appId")
    if not isinstance(declared_id, str) or not declared_id:
        problems.append("manifest.appId 缺失或不是字符串")
    elif declared_id != app_id:
        problems.append(f"manifest.appId（{declared_id}）与安装目录名（{app_id}）不一致")
    elif not APP_ID_PATTERN.match(declared_id):
        problems.append(f"manifest.appId 不符合 D26 正则 {APP_ID_PATTERN.pattern}：{declared_id}")

    if not isinstance(manifest.get("name"), str) or not str(manifest.get("name") or "").strip():
        problems.append("manifest.name 缺失或为空")
    if not isinstance(manifest.get("description"), str):
        problems.append("manifest.description 缺失或不是字符串")

    declared_version = manifest.get("version")
    if not isinstance(declared_version, str) or not _VERSION_PATTERN.match(declared_version):
        problems.append(f"manifest.version 不是合法版本号（期望 1.2.3 形式）：{declared_version!r}")
    elif declared_version != version_dir.name:
        problems.append(
            f"manifest.version（{declared_version}）与版本目录名（{version_dir.name}）不一致"
        )

    entry = manifest.get("entry")
    if not isinstance(entry, str) or not entry.strip():
        problems.append("manifest.entry 缺失或为空")
    else:
        target = _safe_entry(version_dir, entry)
        if target is None:
            problems.append(f"manifest.entry 越出应用目录：{entry}")
        elif not target.is_file():
            problems.append(f"manifest.entry 指向的文件不存在：{entry}")

    methods = manifest.get("methods")
    if not isinstance(methods, list) or not methods:
        problems.append("manifest.methods 必须是非空数组")
    else:
        seen: set[str] = set()
        for index, item in enumerate(methods):
            if not isinstance(item, dict):
                problems.append(f"methods[{index}] 必须是对象")
                continue
            name = item.get("name")
            if not isinstance(name, str) or not _METHOD_NAME_PATTERN.match(name):
                problems.append(f"methods[{index}].name 不是合法方法名：{name!r}")
                continue
            if name in seen:
                problems.append(f"methods 存在重复方法名：{name}")
            seen.add(name)
            if not isinstance(item.get("description", ""), str):
                problems.append(f"methods[{index}].description 必须是字符串")

    # permissions：只校验结构（必须是字符串数组），**不做能力清单拦截**。
    # 维护者决策（plan-693-2868 · docs/app1/11-host-bridge-protocol.md §2）：能力完全放开，
    # 空 permissions 视为可执行（老包如 java-devtools 的 `permissions: []` 照常跑），
    # 不实施 `proc.exec:allowlist` 之类的白名单/分档确认。此字段目前是**提示性声明**，
    # 仅供界面与文档说明应用打算用什么能力；运行时是否放行由宿主桥统一决定并写审计日志。
    permissions = manifest.get("permissions")
    if not isinstance(permissions, list) or not all(isinstance(p, str) for p in permissions):
        problems.append("manifest.permissions 必须是字符串数组")

    runtime = manifest.get("runtime")
    if not isinstance(runtime, dict):
        problems.append("manifest.runtime 缺失或不是对象")
    else:
        vendors = runtime.get("vendors", [])
        if not isinstance(vendors, list):
            problems.append("runtime.vendors 必须是数组")
        else:
            for index, vendor in enumerate(vendors):
                if not isinstance(vendor, dict):
                    problems.append(f"runtime.vendors[{index}] 必须是对象")
                    continue
                name = vendor.get("name")
                version = vendor.get("version")
                if not isinstance(name, str) or not name.strip():
                    problems.append(f"runtime.vendors[{index}].name 缺失或不是字符串")
                if not isinstance(version, str) or not version.strip():
                    problems.append(f"runtime.vendors[{index}].version 缺失或不是字符串")

    return problems


def _external_summary(app: ExternalMicroApp) -> dict[str, Any]:
    """外置微应用在 `microapp_list` 里的条目形状（protocol 13-external-microapps §2）。

    用 `appId` 而不是 `id` 对外：模型只认 `microapp_call({appId})` 这一个入参名，
    列表若换个字段名，模型就得自己猜两者是同一个东西。
    `external: true` 是唯一的区分标记——内置条目**没有**这个字段，
    因此既有消费方（前端 / 模型提示词）对内置微应用的解读完全不变。
    """
    return {
        "appId": app.microapp_id,
        "name": app.name,
        "version": app.version,
        "description": app.description,
        "methods": declared_methods(app),
        "external": True,
        "frontendUrl": app.frontend_url,
        "hasServer": app.has_server,
        "autostart": app.autostart,
    }


async def _call_external(
    app: ExternalMicroApp, app_id: str, method: str, call_args: dict[str, Any],
    ctx: ToolContext,
) -> ToolResult:
    """经桌面主进程调用外置服务；无桌面宿主时仍支持直连固定地址。

    plan-20261116-review-fixes：外置服务的实际端口只由主进程掌握，专用宿主操作必须与
    内置页面的 microappCall 区分，不能通过重复请求或落盘改写清单来猜测运行地址。

    白名单在这里就拦住（协议 §2 末条）：未声明的方法发出去只会得到一句插件自述的失败，
    模型无法据此改正；本地直接拒绝才能给出「有哪些方法可用」。
    """
    available = declared_methods(app)
    if method not in available:
        listed = "、".join(available) if available else "（清单未声明任何方法）"
        return ToolResult(
            ok=False, output="",
            error=f"外置微应用 {app_id} 未声明方法 {method!r}",
            # 协议约定的错误标记；ERROR_CODES 是内置类目，故放进 error / data 供调用侧识别
            error_code="NOT_FOUND",
            hint=(
                f"method-not-declared：该微应用可用的方法：{listed}。"
                "只有 microapp.json 的 capabilities.methods 里声明过的方法才能调用。"
            ),
            data={
                "appId": app_id, "method": method,
                "external": True, "error": "method-not-declared",
                "availableMethods": available,
            },
        )

    try:
        from app.orchestration.browser.bridge import HostCallError, HostUnavailable, bridge
        if bridge.has_host():
            # plan-20261116-review-fixes：端口和进程属于主进程，不能从磁盘清单猜运行地址。
            server = app.manifest.get("server") or {}
            ready_seconds = max(1, min(120, float(server.get("readyTimeoutMs") or 20000) / 1000))
            body = await bridge.request(
                ctx.session_id, "microappExternalCall",
                {"appId": app_id, "method": method, "args": call_args},
                ready_seconds * 2 + 35, cancel_event=ctx.cancel_event,
            )
        else:
            body = await call_method(app, method, call_args)
    except ExternalCallError as exc:
        return ToolResult(
            ok=False, output="", error=str(exc), error_code=exc.code, hint=exc.hint,
            data={"appId": app_id, "method": method, "external": True},
        )
    except (HostCallError, HostUnavailable) as exc:
        return ToolResult(
            ok=False, output="", error=f"外置微应用 {app_id} 调用失败：{exc}",
            error_code="EXEC_FAILED", hint="确认桌面端仍连接；调用结果未知时先核对应用状态，避免重复执行。",
            data={"appId": app_id, "method": method, "external": True},
        )

    if body.get("ok") is not True:
        # 业务失败：把微应用自述的 error / message 原样交给模型，别改写成「调用失败」
        reason = str(body.get("message") or "").strip()
        code = str(body.get("error") or "").strip() or "plugin-error"
        return ToolResult(
            ok=False, output="",
            error=f"外置微应用 {app_id}.{method} 返回失败（{code}）：{reason or '未提供原因'}",
            error_code="EXEC_FAILED",
            hint=(
                "这是微应用自己报告的业务失败，不是传输故障："
                "按它给的原因调整参数后重试；无法解决时让用户看一眼该微应用的日志。"
            ),
            data={
                "appId": app_id, "method": method, "external": True,
                "error": code, "message": reason,
            },
        )

    result = body.get("result")
    try:
        rendered = json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError):
        return ToolResult(
            ok=False, output="",
            error=f"外置微应用 {app_id}.{method} 的返回值无法序列化",
            error_code="EXEC_FAILED",
            hint="协议 §3.3 要求 result 必须可 JSON 序列化：让微应用改成返回普通对象 / 数组 / 字符串 / 数字。",
            data={"appId": app_id, "method": method, "external": True},
        )

    if len(rendered) > MICROAPP_RESULT_MAX_CHARS:
        shown = (
            rendered[:MICROAPP_RESULT_MAX_CHARS]
            + f"…（返回值过长已截断，完整长度 {len(rendered)} 字符）"
        )
    else:
        shown = rendered
    return ToolResult(
        ok=True,
        output=f"已调用外置微应用 {app_id} v{app.version} 的方法 {method}，返回值：{shown}",
        data={
            "appId": app_id, "method": method, "result": result,
            "external": True, "version": app.version,
        },
    )


class MicroAppListTool(Tool):
    name = "microapp_list"
    concurrency_safe = True  # 纯读库目录，不写文件、不改库
    risk_level = "low"
    description = (
        "列出本机已安装的微应用（plan-693-2868）。\n"
        "无参数。返回库内每个微应用的 appId、名称、版本、描述与已声明的方法名；"
        "调用某方法前先用它确认 appId 与方法名。\n"
        "库不存在或为空时返回空列表（不是错误）。"
    )

    def function_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        root = microapp_root()
        try:
            installs = await asyncio.to_thread(_scan_installs, root)
        except OSError as exc:
            return ToolResult(
                ok=False, output="", error=f"扫描微应用库失败: {exc}",
                error_code="EXEC_FAILED",
                hint=(
                    f"检查库根 {root} 是否可读；必要时清除 {MICROAPP_ROOT_ENV} "
                    "让系统回落默认目录。"
                ),
            )

        # 外置微应用（protocol 13-external-microapps §2）：与内置并存，列表里一次性给全。
        # 扫描失败**不**让整个列表失败——外置库不可读不该连内置微应用也列不出来，
        # 因此这里降级成「本次没有外置条目」并记日志。
        ext_root = microapp_ext_root()
        ext_apps: list[ExternalMicroApp] = []
        ext_error = ""
        try:
            ext_apps = await asyncio.to_thread(list_external_microapps, ext_root)
        except OSError as exc:
            ext_error = f"扫描外置微应用库失败: {exc}"
            logger.warning("[microapp] %s（库根 %s），本次只列内置微应用", ext_error, ext_root)

        apps = [
            {
                "appId": install.app_id,
                "name": str(install.manifest.get("name") or ""),
                "version": install.version,
                "description": str(install.manifest.get("description") or ""),
                "methods": _method_names(install.manifest),
            }
            for install in installs
        ]
        # 内置条目一条字段都不改（向后兼容：history 里的消费方按原形状解析），
        # 外置条目靠 external: true 自证身份
        apps.extend(_external_summary(app) for app in ext_apps)

        data: dict[str, Any] = {
            # count 保持「内置微应用数量」的旧语义：既有消费方按它判断「装在不在」，
            # 混入外置数量会让「库里有 N 个」这句话在两个不同来源之间含混
            "root": str(root),
            "count": len(installs),
            "apps": apps,
            "externalCount": len(ext_apps),
            "totalCount": len(apps),
        }
        if ext_apps or ext_error or ext_root.is_dir():
            data["extRoot"] = str(ext_root)
        if ext_error:
            data["externalError"] = ext_error

        if not apps:
            message = f"微应用库（{root}）中没有已安装的微应用。"
            if ext_root.is_dir():
                message += f"外置微应用库（{ext_root}）中也没有有效条目。"
            return ToolResult(ok=True, output=message, data=data)

        lines = [f"微应用库: {root}（{len(installs)} 个内置）"]
        for app in apps[: len(installs)]:
            methods = app["methods"]
            method_text = "、".join(methods) if methods else "（未声明方法）"
            lines.append(
                f"- {app['appId']} v{app['version']} {app['name'] or '(未命名)'}："
                f"{app['description'] or '(无描述)'}；方法：{method_text}"
            )
        if ext_apps:
            lines.append(f"外置微应用（{ext_root}，{len(ext_apps)} 个）：")
            for app in ext_apps:
                methods = declared_methods(app)
                method_text = "、".join(methods) if methods else "（未声明方法）"
                origin = "自带服务" if app.has_server else "纯前端"
                lines.append(
                    f"- {app.microapp_id} v{app.version} {app.name or '(未命名)'}"
                    f"［外置·{origin}］：{app.description or '(无描述)'}；"
                    f"方法：{method_text}；地址：{app.frontend_url}"
                )
        elif ext_root.is_dir():
            lines.append(f"外置微应用库（{ext_root}）中没有有效条目。")
        if ext_error:
            lines.append(f"注意：{ext_error}")

        return ToolResult(ok=True, output="\n".join(lines), data=data)


class MicroAppCallTool(Tool):
    name = "microapp_call"
    concurrency_safe = False  # 独占：桥接落地后要驱动界面，不能与其他调用并行
    risk_level = "medium"
    description = (
        "调用已安装微应用声明的方法（plan-693-2868）。\n"
        "- appId: 微应用标识（先用 microapp_list 查，正则 ^[a-z0-9][a-z0-9._-]{1,62}$）\n"
        "- method: manifest.methods 里声明的方法名（未声明会返回 NOT_FOUND 并列出可用方法）\n"
        "- args: 传给该方法参数对象，可省略\n"
        "- autoOpen: 界面没打开时是否先在右侧面板挂载（默认 true）；挂载只加标签、不展开面板\n"
        "调用会驱动用户在右侧面板看到的微应用界面（在界面的方法注册表里执行），"
        "并把方法返回值原样带回；界面挂不上或桌面端未连接会明确报错，不会假装成功。\n"
        "外置微应用（microapp_list 里标了 external）不走界面桥：直接 POST 到它自己的"
        " /__microapp/call，此时 autoOpen 无效，且只能调它清单里声明过的方法。"
    )

    def function_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "appId": {
                            "type": "string",
                            "description": "微应用标识，如 demo-app（可用 microapp_list 查询）",
                        },
                        "method": {
                            "type": "string",
                            "description": "manifest.methods 中声明的方法名，如 drawRect",
                        },
                        "args": {
                            "type": "object",
                            "description": "传给该方法的参数对象（可选，键名由微应用自行约定）",
                        },
                        "autoOpen": {
                            "type": "boolean",
                            "description": (
                                "界面没打开时是否先在右侧面板挂载再调用（默认 true；"
                                "只加标签不展开面板，打开面板的权力仍在用户手上）。"
                                "仅在要求「界面必须本来就已经打开」时传 false。"
                            ),
                        },
                    },
                    "required": ["appId", "method"],
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        app_id = str(args.get("appId") or "").strip()
        method = str(args.get("method") or "").strip()
        raw_args = args.get("args")
        # 缺省即允许自动挂载（界面没打开时由宿主补标签）
        # 只有显式 false 才要求「界面本来就已经开着」
        auto_open = args.get("autoOpen") is not False

        if not APP_ID_PATTERN.match(app_id):
            return ToolResult(
                ok=False, output="",
                error=f"appId 非法: {app_id!r}（须匹配 {APP_ID_PATTERN.pattern}）",
                error_code="ARG_INVALID",
                hint="appId 以 manifest 与安装目录名为准，先用 microapp_list 取准确值再调用。",
            )
        if not method:
            return ToolResult(
                ok=False, output="", error="method 不能为空",
                error_code="ARG_INVALID",
                hint="传 manifest.methods 里声明的方法名；microapp_list 会列出每个应用的方法。",
            )
        # 方法名会被贴成页面里的取值表达式（window.__MICROAPP_METHODS__[method]），
        # 先在这里收口，避免把坏名字送到主进程再被白名单挡回来（报错文本对模型更不友好）
        if not _METHOD_NAME_PATTERN.match(method):
            return ToolResult(
                ok=False, output="",
                error=f"method 不是合法方法名: {method!r}",
                error_code="ARG_INVALID",
                hint=(
                    "方法名必须是 JavaScript 标识符（字母 / 下划线 / $ 开头，最长 64 字符）："
                    "用 microapp_list 取准确名字；manifest 里的坏名字由 microapp_validate 报出。"
                ),
            )
        if raw_args is None:
            call_args: dict[str, Any] = {}
        elif isinstance(raw_args, dict):
            call_args = raw_args
        else:
            return ToolResult(
                ok=False, output="",
                error=f"args 必须是对象（JSON object），收到 {type(raw_args).__name__}",
                error_code="ARG_INVALID",
                hint="把参数包成一个对象传入，例如 {\"x\": 10, \"y\": 20}；无参数可省略该字段。",
            )

        root = microapp_root()
        try:
            install = await asyncio.to_thread(_find_install, root, app_id)
        except OSError as exc:
            return ToolResult(
                ok=False, output="", error=f"扫描微应用库失败: {exc}",
                error_code="EXEC_FAILED",
                hint=(
                    f"检查库根 {root} 是否可读；必要时清除 {MICROAPP_ROOT_ENV} "
                    "让系统回落默认目录。"
                ),
            )
        if install is None:
            # 内置库没有 → 再查一次外置库（protocol 13-external-microapps §2）：
            # 一个 appId 只可能属于其中一边（两边都要求目录名合规），所以这里是「换库再找」，
            # 不是「两个来源合并」。命中外置就走它自己的 HTTP 入口，不复用界面桥。
            ext_app = await asyncio.to_thread(find_external_microapp, app_id)
            if ext_app is not None:
                return await _call_external(ext_app, app_id, method, call_args, ctx)
            ext_root = microapp_ext_root()
            return ToolResult(
                ok=False, output="",
                error=f"未安装微应用: {app_id}（库根 {root}；外置库根 {ext_root}）",
                error_code="NOT_FOUND",
                hint=(
                    "先确认该微应用已安装到库根：microapp_list 可列出库内全部 appId"
                    "（外置微应用标了 external）；安装 / 导入走库管理能力（§6.5 的 /api/microapps），"
                    "外置微应用则把含 microapp.json 的目录放进外置库根。不要凭记忆拼 appId。"
                ),
            )

        available = _method_names(install.manifest)
        if method not in available:
            listed = "、".join(available) if available else "（manifest 未声明任何方法）"
            return ToolResult(
                ok=False, output="",
                error=f"微应用 {app_id} 未声明方法 {method!r}",
                error_code="NOT_FOUND",
                hint=f"该应用可用的方法：{listed}。对照方法名后再调用一次。",
                data={"appId": app_id, "method": method, "availableMethods": available},
            )

        # plan-693-2868 §7 P0-4：真正把调用送到界面。
        # 通道沿用浏览器宿主那条 WS 连接（桌面端渲染层是客户端，后端在同一条连接上推
        # browser.request），只多一个 op：host.ts 的 dispatch 把 microappCall 转成 IPC 交给主进程，
        # 主进程在 persist:microapp 分区里找到该 app 的 guest webview 执行方法并把返回值带回。
        # 之所以复用而不是新开端口/令牌：桌面端从不监听，唯一常驻的上行连接就是这条。
        from app.orchestration.browser.bridge import HostCallError, HostUnavailable, bridge

        request_payload = {
            "appId": app_id,
            "version": install.version,
            "method": method,
            "args": call_args,
        }
        # 界面没打开时让宿主先挂标签（不展开面板）：microapp_call 不再要求用户先手动打开界面。
        # 只在调用方**显式**传了 autoOpen 时才出现在请求体里——宿主侧的缺省就是「允许自动挂载」
        # （host.ts: `a.autoOpen !== false`），这样既有请求体的形状保持不变。
        if "autoOpen" in args:
            request_payload["autoOpen"] = auto_open
        try:
            payload = await bridge.request(
                ctx.session_id, "microappCall", request_payload,
                MICROAPP_CALL_TIMEOUT, cancel_event=ctx.cancel_event,
            )
        except HostUnavailable as exc:
            # 没有登记中的宿主（桌面端未启动 / 已断线），或发送途中断开
            return ToolResult(
                ok=False, output="",
                error=f"microapp_call 无法送达界面（{app_id}.{method}）：{exc}",
                error_code="EXEC_FAILED",
                hint=(
                    "桌面端未连接：确认应用在桌面端运行（浏览器模式没有这条通道）。"
                    "界面不需要用户先手动打开——宿主会自动把该微应用的标签挂到右侧面板；"
                    "确认连接正常后再重试。"
                ),
                data={"appId": app_id, "version": install.version, "method": method},
            )
        except HostCallError as exc:
            parts = _split_bridge_error(str(exc))
            if parts[0]:
                prefix, detail = parts
                error = f"微应用 {app_id}.{method} 调用失败：{detail or prefix}"
                hint = _BRIDGE_ERROR_HINTS[prefix]
            elif "超时" in str(exc):
                # 桥自己判的超时（宿主 20 s 内没回包）：与「方法超时」分开说，
                # 因为这种情况更可能是桌面端整体卡住
                error = f"微应用 {app_id}.{method} 调用失败：宿主 26 秒内没有回包"
                hint = (
                    "桌面端可能卡住或界面已崩溃：确认应用窗口还在响应，"
                    "界面状态未知，重试前先看界面现状，不要直接重复同一调用。"
                )
            elif "未知的浏览器原语" in str(exc):
                # 桌面端渲染层还是旧版本：协议版本没升，老客户端仍会被登记成宿主，但不认识这个 op。
                # 不单独说的话，用户只会看到一句「未知的浏览器原语」，根本不知道是版本问题。
                error = f"微应用 {app_id}.{method} 调用失败：当前桌面端不支持微应用方法调用"
                hint = (
                    "桌面端版本过旧（渲染层不认识 microappCall）：升级 / 重启应用后再调用；"
                    f"原始错误：{exc}"
                )
            else:
                error = f"微应用 {app_id}.{method} 调用失败：{exc}"
                hint = (
                    "未归类的界面侧失败：先确认界面已打开并加载完成；仍失败就用 microapp_validate "
                    "复核 manifest，并检查 app.js 里该方法的实现。"
                )
            return ToolResult(
                ok=False, output="", error=error, error_code="EXEC_FAILED", hint=hint,
                data={"appId": app_id, "version": install.version, "method": method},
            )

        # 主进程固定回 {appId, version, method, result}；桥对非 dict 结果会包一层 {"value": ...}，
        # 这里兜住那种形态，避免把包装层当成返回值
        result = payload.get("result") if isinstance(payload, dict) else payload
        if isinstance(payload, dict) and "result" not in payload and "value" in payload:
            result = payload["value"]
        try:
            rendered = json.dumps(result, ensure_ascii=False)
        except (TypeError, ValueError):
            # 主进程已做 JSON 往返，走到这里说明桥/宿主两边对「可序列化」的口径不一致
            return ToolResult(
                ok=False, output="",
                error=f"微应用 {app_id}.{method} 的返回值无法序列化",
                error_code="EXEC_FAILED",
                hint=(
                    "方法返回值需可 JSON 序列化：返回普通对象 / 数组 / 字符串 / 数字，"
                    "不要返回 DOM 节点或函数。"
                ),
                data={"appId": app_id, "version": install.version, "method": method},
            )

        if len(rendered) > MICROAPP_RESULT_MAX_CHARS:
            shown = (
                rendered[:MICROAPP_RESULT_MAX_CHARS]
                + f"…（返回值过长已截断，完整长度 {len(rendered)} 字符）"
            )
        else:
            shown = rendered
        return ToolResult(
            ok=True,
            output=f"已调用微应用 {app_id} v{install.version} 的方法 {method}，返回值：{shown}",
            data={"appId": app_id, "method": method, "result": result},
        )


class MicroAppValidateTool(Tool):
    name = "microapp_validate"
    concurrency_safe = True  # 纯读 manifest 与入口文件，不写盘
    risk_level = "low"
    description = (
        "校验一个已安装微应用的 manifest 是否合规（plan-693-2868 §7 P0）。\n"
        "- appId: 微应用标识（先用 microapp_list 查）\n"
        "逐条检查：manifest 可解析、appId 与目录名一致、version 合法、entry 文件存在、"
        "methods 非空且方法名合法、permissions 是字符串数组、runtime.vendors 结构合法。\n"
        "全部通过返回 ok=true；否则按问题逐条列出。"
    )

    def function_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "appId": {
                            "type": "string",
                            "description": "微应用标识，如 demo-app（可用 microapp_list 查询）",
                        },
                    },
                    "required": ["appId"],
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        app_id = str(args.get("appId") or "").strip()
        if not APP_ID_PATTERN.match(app_id):
            return ToolResult(
                ok=False, output="",
                error=f"appId 非法: {app_id!r}（须匹配 {APP_ID_PATTERN.pattern}）",
                error_code="ARG_INVALID",
                hint="appId 以 manifest 与安装目录名为准，先用 microapp_list 取准确值再校验。",
            )

        root = microapp_root()
        # 正则已排除 `/` 与 `..`，拼路径不会越出库根
        version_dir = await asyncio.to_thread(_pick_version_dir, root / app_id)
        if version_dir is None:
            return ToolResult(
                ok=False, output="",
                error=f"未安装微应用: {app_id}（库根 {root}）",
                error_code="NOT_FOUND",
                hint="先用 microapp_list 确认 appId 已安装，再对该 appId 做校验。",
            )

        manifest_file = version_dir / "manifest.json"
        manifest = await asyncio.to_thread(_read_manifest, manifest_file)
        if not manifest_file.is_file():
            problems = [f"缺少 manifest.json：{manifest_file}"]
        elif manifest is None:
            problems = ["manifest.json 存在但无法解析为 JSON 对象"]
        else:
            problems = _validate_manifest(manifest, app_id, version_dir)

        base_data = {
            "appId": app_id,
            "version": version_dir.name,
            "path": str(version_dir),
            "problems": problems,
        }
        if problems:
            lines = [f"微应用 {app_id} v{version_dir.name} 校验未通过（{len(problems)} 项）："]
            lines.extend(f"{index}. {text}" for index, text in enumerate(problems, start=1))
            return ToolResult(
                ok=False,
                output="\n".join(lines),
                error="；".join(problems),
                error_code="EXEC_FAILED",
                hint=(
                    "按上面每一条修正 manifest.json（必要时重新打包安装）后再校验；"
                    "发布到社区前必须全过——共享仓库的 Actions 只做结构校验（D14）。"
                ),
                data=base_data,
            )

        return ToolResult(
            ok=True,
            output=(
                f"微应用 {app_id} v{version_dir.name} 校验通过：manifest 可解析、"
                f"appId 与版本和目录一致、入口 {manifest.get('entry')} 存在、"
                f"{len(_method_names(manifest))} 个方法、permissions 与 runtime.vendors 结构合法。"
            ),
            data=base_data,
        )


# 顺序即注册顺序：列表在前（先发现再用），调用居中，校验在后
ALL_MICROAPP_TOOLS = (
    MicroAppListTool,
    MicroAppCallTool,
    MicroAppValidateTool,
)
