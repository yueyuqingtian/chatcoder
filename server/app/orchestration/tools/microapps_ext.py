"""外置微应用注册表（protocol 13-external-microapps）。

**为什么单独一层**：内置微应用的界面跑在 `app://microapp/` 里、能力经宿主桥回到桌面端；
外置微应用自带前后端、自己起本地 HTTP 服务，能力在它自己的进程里。两者「怎么发现」与
「怎么调用」完全不同，混在 `microapp.py` 里会让那个文件的每个判断都多一个分支。

**这里只做三件事**：认识库里的微应用、找到某一个、知道它的地址。
端口分配与进程拉起是 Electron 主进程的职责（协议 §5）——server 侧不做 spawn，
否则「谁拥有进程」会出现两个答案，退出时必留孤儿进程。

库根与目录结构（协议 §2，与 `microapp.py` 的库根取值同一套写法）：

    <root>/<microappId>/microapp.json    ← 声明的其余内容由微应用自己决定

变量缺失时回落 `~/.chatcoder/microapps-ext` 并记一条 warning：静默回落会让
「工具读到空库」与「用户根本没装」变得无从区分（与内置库根同一条理由）。
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

logger = logging.getLogger(__name__)

# 主进程拉后端时应注入；缺失时回落默认库根
MICROAPP_EXT_ROOT_ENV = "CHATCODER_MICROAPP_EXT_ROOT"
# 清单文件名（协议 §2）：与内置微应用的 manifest.json 区分开，避免两类库混读
MICROAPP_MANIFEST_NAME = "microapp.json"
# id 口径与内置微应用 appId 同一个正则（协议 §2）：两边都要求「目录名 = 声明 id」
MICROAPP_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{1,62}$")
# 版本号口径：协议只要求 ^\d+\.\d+\.\d+ 前缀，这里放宽到与内置一致的完整形态
MICROAPP_VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+")
# 后端协议前缀缺省值（协议 §3）：清单里没写 capabilities.path 时按它拼调用地址
DEFAULT_CAPABILITIES_PATH = "/__microapp"
DEFAULT_HEALTH_PATH = "/__microapp/health"
# 调用超时（协议 §6 的链路要经过一次本机 HTTP 往返）：30 秒足够慢方法跑完，
# 又不会让 AI 在没有响应时无限挂住——超时必须给出可读原因，而不是把 traceback 抛给模型
MICROAPP_EXT_CALL_TIMEOUT = 30.0
# health 探测给短超时：它只判断「端口通了没」，不该占住调用预算
MICROAPP_EXT_HEALTH_TIMEOUT = 5.0


def microapp_ext_root() -> Path:
    """外置微应用库根：环境变量优先，缺失回落默认目录并记一条 warning（协议 §2）。"""
    raw = (os.environ.get(MICROAPP_EXT_ROOT_ENV) or "").strip()
    if raw:
        return Path(raw).expanduser()
    fallback = Path.home() / ".chatcoder" / "microapps-ext"
    logger.warning(
        "[microapp-ext] 环境变量 %s 未设置，回落默认库根 %s"
        "（主进程拉后端时应注入，见 protocol 13-external-microapps §2）",
        MICROAPP_EXT_ROOT_ENV, fallback,
    )
    return fallback


def read_manifest(path: Path) -> dict[str, Any] | None:
    """读清单；坏 JSON / 非对象 / 读不到一律返回 None（调用方决定是跳过还是报问题）。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _method_names(manifest: dict[str, Any]) -> list[str]:
    """`capabilities.methods` 里声明的方法名（其余形态由校验逐条报问题）。

    兼容两种写法：`[{name, ...}]` 对象数组，以及历史字符串数组——只取有名字的项，
    结构问题不在这里报，交给 `validate_manifest()` 统一收集。
    """
    capabilities = manifest.get("capabilities")
    raw = capabilities.get("methods") if isinstance(capabilities, dict) else None
    if not isinstance(raw, list):
        return []
    names: list[str] = []
    for item in raw:
        if isinstance(item, str):
            if item:
                names.append(item)
        elif isinstance(item, dict):
            name = item.get("name")
            if isinstance(name, str) and name:
                names.append(name)
    return names


def capabilities_path(manifest: dict[str, Any]) -> str:
    """调用用的**前缀**：由 `capabilities.path` 推导，缺省 `/__microapp`（协议 §3）。

    这里有一处必须消掉的歧义：协议 §2 的示例把 `capabilities.path` 写成
    `/__microapp/capabilities`（那是「能力自述端点」的地址，§3.2），而调用端点是
    `POST /__microapp/call`（§3.3）。若直接按 `{capabilities.path}/call` 拼接，
    会打到 `/__microapp/capabilities/call`——一个协议里不存在的端点。
    因此：以 `/capabilities` 结尾时取它的父路径作为前缀，其余写法原样当前缀。
    两种读法（写 `/__microapp` 或写 `/__microapp/capabilities`）都得到 `/__microapp/call`。

    只接受以 `/` 开头的相对路径：`http://evil` 这类值会被拼成跨站地址，
    那是「微应用自己起服务」之外的另一个信任边界，不在本协议范围内。
    """
    capabilities = manifest.get("capabilities")
    raw = capabilities.get("path") if isinstance(capabilities, dict) else None
    if not (isinstance(raw, str) and raw.startswith("/")):
        return DEFAULT_CAPABILITIES_PATH
    base = raw.rstrip("/")
    if base.endswith("/capabilities"):
        base = base[: -len("/capabilities")]
    return base or DEFAULT_CAPABILITIES_PATH



def frontend_url(manifest: dict[str, Any]) -> str:
    """`frontend.url` 原文（含 `{port}` 占位符时的实际填充由主进程负责，协议 §2）。"""
    frontend = manifest.get("frontend")
    raw = frontend.get("url") if isinstance(frontend, dict) else None
    return raw if isinstance(raw, str) else ""


def origin_of(url: str) -> str:
    """从任意 URL 取 `scheme://host:port`；解析不出时返回空串。

    只保留 origin 是刻意为之：调用地址是 `origin + capabilities.path + "/call"`，
    若直接拼接 `frontend.url`（可能带 `/#/route` 或子路径），拼出来的地址经常打到前端路由上。
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if not parts.scheme or not parts.netloc:
        return ""
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def has_server(manifest: dict[str, Any]) -> bool:
    """清单里有 `server` 段即为「需要宿主拉起进程」的微应用（协议 §2）。"""
    return isinstance(manifest.get("server"), dict)


def autostart(manifest: dict[str, Any]) -> bool:
    """`server.autostart`，缺省 true（协议 §2 的字段规则）。"""
    server = manifest.get("server")
    if not isinstance(server, dict):
        return False
    return server.get("autostart") is not False


def health_path(manifest: dict[str, Any]) -> str:
    """`server.healthPath`，缺省 `/__microapp/health`（协议 §3.1）。"""
    server = manifest.get("server")
    raw = server.get("healthPath") if isinstance(server, dict) else None
    if isinstance(raw, str) and raw.startswith("/"):
        return raw
    return DEFAULT_HEALTH_PATH


def validate_manifest(manifest: dict[str, Any] | None, dir_name: str) -> list[str]:
    """逐条校验清单内容，返回问题列表（空 = 全过）。

    纯函数：不读盘、不看环境变量，这样单测可以拿任意字典直接跑
    （路由与工具两侧都复用它，避免第二份校验逻辑漂移）。

    校验范围严格贴协议 §2 的字段规则，**不做**内容审核：
    外置微应用的信任边界是「用户自己放进库目录」这一点（协议 §7 末条）。
    """
    problems: list[str] = []

    if manifest is None:
        return ["清单无法解析为 JSON 对象"]

    # id：与目录名一致才谈得上「找到它」；形状复用内置 appId 的正则
    declared_id = manifest.get("id")
    if not isinstance(declared_id, str) or not declared_id:
        problems.append("manifest.id 缺失或不是字符串")
    elif not MICROAPP_ID_PATTERN.match(declared_id):
        problems.append(
            f"manifest.id 不符合正则 {MICROAPP_ID_PATTERN.pattern}：{declared_id}"
        )
    elif declared_id != dir_name:
        problems.append(f"manifest.id（{declared_id}）与目录名（{dir_name}）不一致")

    declared_version = manifest.get("version")
    if not isinstance(declared_version, str) or not MICROAPP_VERSION_PATTERN.match(
        declared_version
    ):
        problems.append(
            f"manifest.version 不是合法版本号（期望 1.2.3 形式）：{declared_version!r}"
        )

    frontend = manifest.get("frontend")
    if not isinstance(frontend, dict):
        problems.append("manifest.frontend 缺失或不是对象")
    else:
        url = frontend.get("url")
        if not isinstance(url, str) or not url.strip():
            problems.append("manifest.frontend.url 缺失或不是字符串")
        elif not origin_of(url):
            # 探测不到 origin 时调用侧永远拼不出地址，必须在装库阶段就报出来
            problems.append(f"manifest.frontend.url 不是合法地址：{url!r}")
        dev_url = frontend.get("devUrl")
        if dev_url is not None and not isinstance(dev_url, str):
            problems.append("manifest.frontend.devUrl 必须是字符串")

    server = manifest.get("server")
    if server is not None:
        if not isinstance(server, dict):
            problems.append("manifest.server 必须是对象（没有该段就省略，不要给 null）")
        else:
            command = server.get("command")
            if not isinstance(command, str) or not command.strip():
                problems.append("manifest.server.command 缺失或不是字符串（有 server 段时必填）")

    capabilities = manifest.get("capabilities")
    if capabilities is not None:
        if not isinstance(capabilities, dict):
            problems.append("manifest.capabilities 必须是对象")
        else:
            path = capabilities.get("path")
            if path is not None and (not isinstance(path, str) or not path.startswith("/")):
                problems.append(f"manifest.capabilities.path 必须以 / 开头：{path!r}")
            methods = capabilities.get("methods")
            if methods is not None and not isinstance(methods, list):
                problems.append("manifest.capabilities.methods 必须是数组")
            elif isinstance(methods, list):
                for index, item in enumerate(methods):
                    if isinstance(item, str):
                        if not item.strip():
                            problems.append(f"capabilities.methods[{index}] 是空方法名")
                        continue
                    if not isinstance(item, dict):
                        problems.append(f"capabilities.methods[{index}] 必须是对象或字符串")
                        continue
                    name = item.get("name")
                    if not isinstance(name, str) or not name.strip():
                        problems.append(f"capabilities.methods[{index}].name 缺失或不是字符串")

    return problems


@dataclass
class ExternalMicroApp:
    """库内一个外置微应用（协议 §2：一个目录就是一个应用，无多版本目录层）。"""

    microapp_id: str
    name: str
    version: str
    description: str
    frontend_url: str
    has_server: bool
    autostart: bool
    methods: list[str]
    path: Path
    manifest: dict[str, Any] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """给工具面与 HTTP 路由共用的展示结构（字段名一次定好，两边不再各写一套）。"""
        return {
            "id": self.microapp_id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "frontendUrl": self.frontend_url,
            "hasServer": self.has_server,
            "autostart": self.autostart,
            "methods": list(self.methods),
            "path": str(self.path),
            "problems": list(self.problems),
        }


def _load_one(app_dir: Path) -> ExternalMicroApp | None:
    """读一个目录；无效时返回 None（调用方记日志后继续扫下一个）。"""
    manifest = read_manifest(app_dir / MICROAPP_MANIFEST_NAME)
    if manifest is None:
        return None
    problems = validate_manifest(manifest, app_dir.name)
    return ExternalMicroApp(
        microapp_id=app_dir.name,
        name=str(manifest.get("name") or ""),
        version=str(manifest.get("version") or ""),
        description=str(manifest.get("description") or ""),
        frontend_url=frontend_url(manifest),
        has_server=has_server(manifest),
        autostart=autostart(manifest),
        methods=_method_names(manifest),
        path=app_dir,
        manifest=manifest,
        problems=problems,
    )


def list_external_microapps(root: Path | None = None) -> list[ExternalMicroApp]:
    """扫描库根，返回**校验通过**的外置微应用（按 id 排序）。

    跳过点目录（`.tmp` 之类）与无效微应用：一个坏目录不能让它旁边的好微应用一起消失，
    也不能让 `microapp_list` 整个失败——所以坏的那个只记日志。
    详情与校验端点走 `get_external_microapp()` / `inspect_external_microapp()`，
    它们同样能读到「装了但没通过校验」的目录，否则用户改不了也就删不掉。
    """
    base = root if root is not None else microapp_ext_root()
    if not base.is_dir():
        return []
    try:
        dirs = sorted(base.iterdir())
    except OSError as exc:  # 库根不可读（权限 / 断链）不是「没有微应用」，交给上层报错
        raise OSError(f"库根不可读: {base}") from exc

    apps: list[ExternalMicroApp] = []
    for app_dir in dirs:
        if not app_dir.is_dir() or app_dir.name.startswith("."):
            continue
        try:
            app = _load_one(app_dir)
        except OSError as exc:
            logger.warning("[microapp-ext] 读取失败，跳过 %s：%s", app_dir, exc)
            continue
        if app is None:
            logger.warning(
                "[microapp-ext] 跳过 %s：缺少或无法解析 %s", app_dir, MICROAPP_MANIFEST_NAME,
            )
            continue
        if app.problems:
            logger.warning(
                "[microapp-ext] 跳过 %s：清单校验未通过（%s）", app_dir, "；".join(app.problems),
            )
            continue
        apps.append(app)
    return apps


def inspect_external_microapp(
    microapp_id: str, root: Path | None = None,
) -> ExternalMicroApp | None:
    """读一个外置微应用，**不**过滤校验问题（详情 / 校验端点要看得到问题本身）。

    目录名先过 id 正则：正则已排除 `/` 与 `..`，拼路径不会越出库根。
    """
    if not MICROAPP_ID_PATTERN.match(microapp_id or ""):
        return None
    base = root if root is not None else microapp_ext_root()
    app_dir = base / microapp_id
    if not app_dir.is_dir():
        return None
    try:
        return _load_one(app_dir)
    except OSError as exc:
        logger.warning("[microapp-ext] 读取失败 %s：%s", app_dir, exc)
        return None


def find_external_microapp(
    microapp_id: str, root: Path | None = None,
) -> ExternalMicroApp | None:
    """按 id 找**可用**的外置微应用；清单没通过校验的视为不可用（返回 None）。

    与 `inspect_external_microapp()` 的分工：调用路径只要「能用」的，
    声明坏了就该按未安装处理，而不是带着半份声明去发 HTTP 请求。
    """
    app = inspect_external_microapp(microapp_id, root)
    if app is None or app.problems:
        return None
    return app


def declared_methods(app: ExternalMicroApp) -> list[str]:
    """可调用方法白名单：清单 `capabilities.methods` 里声明过的方法（协议 §2 末条）。

    协议 §3.2 要求与微应用自己的 `/__microapp/capabilities` 取交集，那是**发起调用时**的
    二次确认；这里只回答静态声明，交集在调用侧实现（缺一次网络往返就先不校验）。
    """
    return list(app.methods)


# ── 调用通道（协议 §3.3 / §6）──────────────────────────────
#
# 外置微应用**没有**宿主桥：它的能力在它自己的进程里，chatcoder 只负责把调用
# 送到它自己起的本地 HTTP 服务上。因此这里不复用 `bridge.request("microappCall")`，
# 那条通道的语义是「在 guest webview 里执行方法名」，与本协议的回环 HTTP 是两回事。


class ExternalCallError(Exception):
    """外置微应用调用失败的统一错误；`code` 由工具侧翻成 `ToolResult.error_code`。"""

    code = "EXEC_FAILED"

    def __init__(self, message: str, *, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


class ExternalCallUnavailable(ExternalCallError):
    """连不上 / 超时：微应用进程没起来或端口还没就绪（EXEC_FAILED）。"""

    code = "EXEC_FAILED"


class ExternalCallMethodNotDeclared(ExternalCallError):
    """方法没在清单 `capabilities.methods` 里声明（协议 §2 末条 / §3.3）。"""

    code = "method-not-declared"


def call_url(app: ExternalMicroApp) -> str:
    """调用地址：`{frontend.url 的 origin}{capabilities.path 缺省 /__microapp}/call`。

    只取 origin 是刻意的：`frontend.url` 可能带前端路由（`/#/canvas`）或子路径，
    直接拼接会打到前端页面而不是后端方法入口。
    """
    return f"{origin_of(app.frontend_url)}{capabilities_path(app.manifest)}/call"


async def call_method(
    app: ExternalMicroApp,
    method: str,
    args: dict[str, Any] | None = None,
    *,
    timeout: float = MICROAPP_EXT_CALL_TIMEOUT,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """把一次方法调用 POST 到微应用自己的 `/call`，原样带回它的 `{ok, result, error, message}`。

    只在**传输层**失败时抛异常（连不上 / 超时 / 非 2xx / 响应不是 JSON 对象）——
    业务失败（`ok:false`）是正常回包，交给调用侧翻译，不能混成同一种错误。

    `transport` 只为单测留出注入口：默认建真实连接，测试用 `httpx.MockTransport` 免起进程。
    """
    # `{port}` 占位符是主进程在拉起微应用后回填的（协议 §2）；没回填就说明这台机器上
    # 还没人负责它的端口，此时发请求只会得到一句 Connection refused，不利于定位
    if "{port}" in app.frontend_url:
        raise ExternalCallUnavailable(
            f"外置微应用 {app.microapp_id} 的 frontend.url 仍未回填端口（{app.frontend_url}）",
            hint=(
                "动态端口需要已连接的桌面端代为启动和调用；"
                "请启动或重新连接桌面端后重试，无需先打开应用面板。"
            ),
        )

    url = call_url(app)
    payload = {"method": method, "args": dict(args or {})}
    client_kwargs: dict[str, Any] = {"timeout": timeout}
    if transport is not None:
        client_kwargs["transport"] = transport

    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            response = await client.post(url, json=payload)
    except httpx.TimeoutException as exc:
        raise ExternalCallUnavailable(
            f"调用外置微应用 {app.microapp_id}.{method} 超时（{timeout:.0f} 秒）",
            hint=(
                "微应用服务收到了请求但没在超时内回包：方法可能仍在执行，"
                "先确认微应用界面现状再决定是否重试，不要直接重复同一调用。"
            ),
        ) from exc
    except httpx.HTTPError as exc:
        # 连不上是最常见的失败：进程没起、端口没就绪、或插件已退出
        raise ExternalCallUnavailable(
            f"无法连接外置微应用 {app.microapp_id}（{url}）：{exc}",
            hint=(
                "微应用未启动或端口未就绪：让用户在右侧面板点开该微应用（或点「启动」）后再重试；"
                "若它不是由 chatcoder 拉起的纯前端微应用，确认它自己的服务已在运行。"
            ),
        ) from exc

    if response.status_code >= 500:
        # 协议 §3.3 明确：业务失败要用 ok:false 表达，5xx 一律视为传输故障
        raise ExternalCallUnavailable(
            f"外置微应用 {app.microapp_id} 返回 {response.status_code}（{url}）",
            hint=(
                "按协议，业务失败应回 200 + ok:false；5xx 说明微应用后端自己崩了或路由没接上。"
                "让用户看一眼微应用的日志尾部再决定重试。"
            ),
        )
    if response.status_code >= 400:
        raise ExternalCallUnavailable(
            f"外置微应用 {app.microapp_id} 拒绝了这次调用：HTTP {response.status_code}（{url}）",
            hint=(
                "确认方法名与参数形状符合微应用的声明（协议 §3.3），"
                "或该微应用是否要求额外的鉴权头。"
            ),
        )

    try:
        body = response.json()
    except ValueError as exc:
        raise ExternalCallUnavailable(
            f"外置微应用 {app.microapp_id} 的 /call 响应不是 JSON（{url}）",
            hint="微应用的 POST /call 必须回 JSON 对象 {ok, result, error, message}（协议 §3.3）。",
        ) from exc
    if not isinstance(body, dict):
        raise ExternalCallUnavailable(
            f"外置微应用 {app.microapp_id} 的 /call 响应不是 JSON 对象（{url}）",
            hint="微应用的 POST /call 必须回 JSON 对象 {ok, result, error, message}（协议 §3.3）。",
        )
    return body


async def probe_health(
    app: ExternalMicroApp,
    *,
    timeout: float = MICROAPP_EXT_HEALTH_TIMEOUT,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any] | None:
    """探一次 `server.healthPath`（协议 §3.1）：任一 2xx 即视为就绪。

    返回 health 响应体（可能为空字典）；连不上 / 非 2xx 返回 None。
    进程拉起与轮询等就绪是主进程的职责（协议 §5），这里只给「现在通不通」这一个事实。
    """
    if "{port}" in app.frontend_url:
        return None
    url = f"{origin_of(app.frontend_url)}{health_path(app.manifest)}"
    client_kwargs: dict[str, Any] = {"timeout": timeout}
    if transport is not None:
        client_kwargs["transport"] = transport
    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            response = await client.get(url)
    except httpx.HTTPError:
        return None
    if not (200 <= response.status_code < 300):
        return None
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}

