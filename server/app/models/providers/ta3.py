"""Ta3Provider — Ta+3 牛码（银海）模型供应商实现。

严格复刻参考项目 ta3-new-coder 的 LLM 请求契约（风控核心），不走 OpenAI SDK
（SDK 自带 User-Agent: OpenAI/Python、x-stainless-* 头，指纹明显），改用
httpx 裸请求完全控制头与体：

- 请求头：X-Call-Source: APP / Bearer llm- + api-key（OpenAI）/ x-api-key +
  anthropic-version（Anthropic）/ Electron 同族 UA
- 请求体：temperature 默认 0.1、prune 空字段、thinking 按模型系别
  （qwen→enable_thinking / 其他→thinking-object / kimi→output_config.effort）、
  zai 加 stream_options + tool_stream
- 工具名伪装：出站 schema/历史 tool_calls → ta3 原生名，入站反伪装回真实执行名
- 流式解析：SSE data: 帧手动解析（OpenAI / Anthropic 双协议）

上下文管理、压缩、审批、编排全部沿用当前项目（本类只负责"请求怎么发"）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator
from pathlib import Path

import httpx

from app.core.config import settings
from app.models.base import ModelProvider
from app.models.providers.tool_args import parse_tool_args
from app.models.providers.ta3_tool_aliases import FROM_TA3, TO_TA3, disguise_args, restore_args
from app.models.providers.ta3_mcp import (
    is_mcp_alias, is_mcp_tool, mcp_alias, resolve_mcp_alias,
)
from app.models.providers.ta3_tool_schemas import disguise_tools
from app.models.schemas import ChatMessage, ChatRequest, ChatResponse, Usage

logger = logging.getLogger(__name__)

_DEFAULT_TEMPERATURE = 0.1  # 官方 openAiAdapter.js:107 / anthropicAdapter.js:297 的 toNumber 兜底
_DEFAULT_MAX_TOKENS = 2048  # 官方 anthropicAdapter.js:287 的 max_tokens 兜底
_DEFAULT_ANTHROPIC_VERSION = "2023-06-01"  # 官方 modelClient.js:228

# Electron 同族 UA（参考项目为 Electron 打包应用；可经 TA3_USER_AGENT 覆盖）
_DEFAULT_TA3_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "ta3-new-coder-desktop/1.0.0 Chrome/126.0.0.0 Electron/31.0.0 Safari/537.36"
)

def _send_user_agent() -> bool:
    """是否下发伪装 UA（plan-598-2482 / plan-601-2495）。

    官方 build*Headers 不设置 User-Agent（modelClient.js:186-232），由主进程的 undici
    补默认值 `node`（undici/lib/web/fetch/index.js:76-78、1468-1470）。开关关闭（默认）时
    发 `node`；打开时发旧的 Electron 伪装 UA，仅供应急使用。
    """
    try:
        from app.core.config import settings

        return bool(getattr(settings, "ta3_send_user_agent", False))
    except Exception:  # pragma: no cover - 配置层异常时按官方行为（不下发）
        return False


# 官方 openAiAdapter.getThinkingControlMode 的 qwen 系判定是 identity **子串**匹配
# （openAiAdapter.js:89），这里保持子串口径，不改成词边界。
_QWEN_IDENT = ("qwen", "dashscope", "tongyi", "通义")

# plan-601-2495：kimi 特判（_KIMI_EFFORT_MAP / 每轮强制回传 thinking / 按名称判定
# supports_thinking）已整体移除——官方 0.7.5 的 anthropicAdapter / openAiAdapter
# 对 kimi 没有任何特殊分支，档位与 thinking 回传都走通用口径。

# ─────────────────── 官方数值/空值语义（plan-601-2495） ───────────────────
# 官方请求体大量使用 `a ?? b` 与 `toNumber(v, fallback)`（modelClient.js:171-174）。
# JS 里 null 与 undefined 是两种「空」：`??` 两者都跳过，但 `Number(null) === 0`、
# `Number(undefined) === NaN`。Python 的 dict.get 把两者都变成 None，直接用 `or`
# 还会把合法的 0 当成空值吞掉（此前温度/max_tokens 都有这个问题）。这里用哨兵区分。
_UNDEF = object()


def _opt(source: dict, *keys):
    """`source.k1 ?? source.k2 ?? …`：缺键视为 undefined，显式 null 视为 null。"""
    vals = [source.get(k, _UNDEF) if isinstance(source, dict) else _UNDEF for k in keys]
    return _nullish(*vals)


def _nullish(*vals):
    """JS `??` 链：返回首个非空值；全为空时返回最后一个操作数（保留 null/undefined 区别）。"""
    for v in vals:
        if v is not _UNDEF and v is not None:
            return v
    return vals[-1] if vals else _UNDEF


def _to_number(value, fallback):
    """对齐官方 toNumber：Number(value) 有限则取之，否则回落 fallback。"""
    if value is _UNDEF:
        return fallback
    if value is None:
        return 0  # Number(null) === 0
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        num = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return 0  # Number('') === 0
        try:
            num = float(text)
        except ValueError:
            return fallback
    else:
        return fallback
    if isinstance(num, float):
        if num != num or num in (float("inf"), float("-inf")):
            return fallback
        # JSON.stringify(1.0) === "1"：整数值浮点数按整数序列化，避免出站多出 ".0"
        if num.is_integer():
            return int(num)
    return num


def _prune(body: dict) -> dict:
    """官方 pruneRequestBody：只删 undefined / null / ''（不删 {}、False、0、[]）。"""
    for key in list(body.keys()):
        if body[key] is _UNDEF or body[key] is None or body[key] == "":
            del body[key]
    return body


# 请求 Accept（官方 providers.js:16-17）：流式聊天用 SSE 优先，文本请求用 JSON 优先
_ACCEPT_SSE = "text/event-stream, application/json"
_ACCEPT_JSON = "application/json, text/event-stream"

# 403 拦截判定（plan-601-2495）：官方 platformHttpError.js:25 的内容安全拦截固定文案，
# 加上网关路由拦截 / 风控常见字样。只有命中这些字样的 403 才值得脱敏重发。
_BLOCK_MARKERS = (
    "api 异常，无法访问", "routing_error", "风控", "拦截", "安全规则",
    "unauthorized cli", "blocked",
)


def _is_block_response(status: int, text: str) -> bool:
    if status != 403:
        return False
    low = (text or "").lower()
    return any(m in low for m in _BLOCK_MARKERS)


# undici 默认头（node_modules/undici/lib/web/fetch/index.js:498-503、1462-1529；
# util.js:230-253）：官方客户端在主进程用 Node 内置 fetch 发请求，build*Headers 之外
# 这几项由 undici 追加——我们必须显式补齐，否则请求头集合与官方不同。
_UNDICI_USER_AGENT = "node"


def _brotli_available() -> bool:
    """httpx 解 br 需要 brotli / brotlicffi；缺失时不能宣告 br，否则响应无法解码。"""
    try:
        import brotli  # noqa: F401
        return True
    except ImportError:
        try:
            import brotlicffi  # noqa: F401
            return True
        except ImportError:
            return False


_BROTLI_OK = _brotli_available()


def _undici_accept_encoding(url: str) -> str:
    """对齐 undici：https 发 'br, gzip, deflate'，http 发 'gzip, deflate'。"""
    if url.lower().startswith("https:") and _BROTLI_OK:
        return "br, gzip, deflate"
    return "gzip, deflate"


def _has_header(headers: dict, name: str) -> bool:
    lname = name.lower()
    return any(str(k).lower() == lname for k in headers)


def _wire_headers(headers: dict, url: str, content_length: int) -> dict:
    """把官方 build*Headers 的结果按 undici 的追加顺序补成线上实际发送的头。

    线上顺序（client-h1.js:1250-1264、1467 + fetch/index.js）：
    host → connection → 业务头（原样大小写与顺序）→ accept-language → sec-fetch-mode
    → user-agent → accept-encoding → content-length。undici 自己追加的头一律小写。
    httpx 只在缺失时才补 Host / Content-Length，这里显式给出即可保持小写与位置。
    """
    from urllib.parse import urlsplit

    netloc = urlsplit(url).netloc.rsplit("@", 1)[-1]
    out: dict = {"host": netloc, "connection": "keep-alive"}
    for k, v in headers.items():
        out[k] = v
    if not _has_header(out, "accept-language"):
        out["accept-language"] = "*"
    # appendFetchMetadata 用的是 set（覆盖），请求模式恒为 cors
    for k in [k for k in out if str(k).lower() == "sec-fetch-mode"]:
        del out[k]
    out["sec-fetch-mode"] = "cors"
    if not _has_header(out, "user-agent"):
        out["user-agent"] = _UNDICI_USER_AGENT
    if not _has_header(out, "accept-encoding"):
        out["accept-encoding"] = _undici_accept_encoding(url)
    out["content-length"] = str(content_length)
    return out


def _js_json(body) -> bytes:
    """等价 JSON.stringify：紧凑分隔符、非 ASCII 原样输出。"""
    try:
        return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except UnicodeEncodeError:
        # 孤立代理项无法 UTF-8 编码；JSON.stringify 会把它转义成 \uXXXX
        return json.dumps(body, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


# ─────────────────── 中途 system/developer 消息重定位（plan-601-2495） ───────────────────
# 官方 anthropicAdapter.convertMessages 只取第一条 system（:227-229），其余 system 整条丢弃；
# 官方装配本身也只产出开头一条 system。本项目则会在历史中途插入 system/developer（语言/规则
# 提醒、计划状态、压缩摘要与边界标记），直接丢弃会丢信息，原样上行又与官方结构不同。
# 因此在转换前统一改写成官方能产生的形态：摘要 → assistant 的 <conversation_summary>
# （contextCompaction.js:129-136），边界标记 → 丢弃，其余提醒 → <system-reminder> 文本块
# 并入最近的 user 消息（memory/promptBuilder.js:110 同款标签）。
_COMPACT_BOUNDARY_MARK = "[compact-boundary"
_EMERGENCY_SUMMARY_HEADS = ("以下是之前对话的摘要：", "Summary of earlier conversation:")
_READSTATE_HEADS = (
    "以下文件在已压缩的历史中曾被读取",
    "The following files were read earlier in the compacted history",
)


def _summary_prefixes() -> tuple[str, ...]:
    """压缩摘要前言（compaction.py 用 get_summary_prefix，agent_loop 用 checkpoint 前言）。"""
    try:
        from app.orchestration.prompts.summary import (
            CHECKPOINT_PREAMBLE, CHECKPOINT_PREAMBLE_ZH, SUMMARY_PREFIX, SUMMARY_PREFIX_ZH,
        )

        return (SUMMARY_PREFIX, SUMMARY_PREFIX_ZH, CHECKPOINT_PREAMBLE, CHECKPOINT_PREAMBLE_ZH)
    except Exception:  # pragma: no cover - 提示词模块不可用时只按 emergency 前缀识别
        return ()


def _extract_summary(text: str) -> str | None:
    """内容是压缩摘要时返回摘要正文，否则 None。"""
    stripped = (text or "").lstrip()
    for prefix in _summary_prefixes():
        if prefix and stripped.startswith(prefix.strip()):
            body = stripped[len(prefix.strip()):].strip()
            # checkpoint 帧把正文包在 <compacted-summary> 里，取内层正文
            m = re.search(r"<compacted-summary>\s*(.*?)\s*</compacted-summary>", body, re.S)
            if m:
                tail = body[m.end():].strip()
                body = m.group(1).strip() + (f"\n\n{tail}" if tail else "")
            return body
    for head in _EMERGENCY_SUMMARY_HEADS:
        if stripped.startswith(head):
            return stripped[len(head):].strip()
    return None


def _is_transient_system(text: str) -> bool:
    """压缩派生的临时 system（摘要/边界/ReadState），不能被选作主 system。"""
    stripped = (text or "").lstrip()
    return (_COMPACT_BOUNDARY_MARK in stripped
            or _extract_summary(stripped) is not None
            or stripped.startswith(_READSTATE_HEADS))


def _ta3_relocate_inline_system(messages: list[ChatMessage]) -> list[ChatMessage]:
    """把 ChatMessage 列表改写成「开头至多一条 system」的官方形态（纯函数，不改入参）。

    - 主 system：第一条 role=system 且非压缩派生的消息，移到最前（官方 take-first）；
    - 压缩摘要 → assistant `<conversation_summary>\\n正文\\n</conversation_summary>`，留在原位；
    - `[compact-boundary …]` 边界标记 → 丢弃；
    - 其余 system/developer → `<system-reminder>\\n内容\\n</system-reminder>`，追加到其后最近
      的 user 消息（tool 结果也算 user：插到该段连续 tool 之后，Anthropic 侧会与 tool_result
      合并为同一条 user，文本块排在 tool_result 之后）；之后没有 user 时追加到之前最近的 user；
      全程没有 user 时新起一条 user 放在末尾。
    """
    primary_idx = None
    for i, m in enumerate(messages):
        if m.role == "system" and (m.content or "").strip() and not _is_transient_system(m.content or ""):
            primary_idx = i
            break
    if primary_idx is None:
        for i, m in enumerate(messages):
            if m.role == "system" and (m.content or "").strip() and _extract_summary(m.content or "") is None \
                    and _COMPACT_BOUNDARY_MARK not in (m.content or ""):
                primary_idx = i
                break

    # 第一遍：识别每条消息的去向；提醒先挂在原位置，第二遍再落到目标 user 上
    items: list[tuple[str, object]] = []  # ("msg", ChatMessage) | ("reminder", str)
    for i, m in enumerate(messages):
        if i == primary_idx:
            continue
        if m.role in ("system", "developer"):
            text = m.content or ""
            if not text.strip():
                continue
            if _COMPACT_BOUNDARY_MARK in text:
                continue
            summary = _extract_summary(text)
            if summary is not None:
                items.append(("msg", ChatMessage(
                    role="assistant",
                    content=f"<conversation_summary>\n{summary}\n</conversation_summary>",
                )))
                continue
            items.append(("reminder", f"<system-reminder>\n{text.strip()}\n</system-reminder>"))
            continue
        items.append(("msg", m))

    out: list[ChatMessage] = []
    pending: list[str] = []
    attach_plan: list[tuple[int, list[str]]] = []  # (out 中的目标下标, 提醒) 延后统一处理

    def _is_userish(msg: ChatMessage) -> bool:
        return msg.role in ("user", "tool")

    # 找「之后最近的 user」：提醒先积压，遇到 user/tool 时绑定
    for kind, val in items:
        if kind == "reminder":
            pending.append(val)  # type: ignore[arg-type]
            continue
        msg = val  # type: ignore[assignment]
        out.append(msg)
        if pending and _is_userish(msg):
            attach_plan.append((len(out) - 1, pending))
            pending = []
    if pending:
        # 之后没有 user：回退到之前最近的 user/tool
        target = next((i for i in range(len(out) - 1, -1, -1) if _is_userish(out[i])), None)
        if target is None:
            out.append(ChatMessage(role="user", content="\n\n".join(pending)))
        else:
            attach_plan.append((target, pending))

    # 从后往前落位，插入新消息时不影响前面的下标
    for target, reminders in sorted(attach_plan, key=lambda x: x[0], reverse=True):
        msg = out[target]
        if msg.role == "user":
            blocks = list(msg.content_blocks or [])
            blocks.extend({"type": "text", "text": r} for r in reminders)
            out[target] = msg.model_copy(update={"content_blocks": blocks})
        else:
            # tool 结果：插到这段连续 tool 之后，避免打断 assistant(tool_calls)↔tool 配对
            end = target
            while end + 1 < len(out) and out[end + 1].role == "tool":
                end += 1
            out.insert(end + 1, ChatMessage(role="user", content="\n\n".join(reminders)))

    if primary_idx is not None:
        out.insert(0, messages[primary_idx])
    return out


def _ensure_user_lead(messages: list[ChatMessage]) -> list[ChatMessage]:
    """确保「首条非 system 消息」是 user（plan-598-2517）。

    网关（Anthropic / DeepSeek / OpenAI）都要求对话以 user 开场；以 assistant 或
    tool 开场会被直接判为非法请求，返回与内容无关的通用错误（ta3 侧表现为
    「AI服务异常，请稍后重试」，客户端只能看到这一句，重试必然再次失败）。

    真实事故：会话 605 turn=2520 step=46 压缩后消息序列变成
    `system → (summary→assistant) → assistant`，一条 user 也没有，请求被拒；
    用户重发「继续」后新 turn 重新注入 user 才恢复正常。

    这里不做内容改写，只补一条最小的 user 承接消息，让结构合法：
    - 已有 user 打头 → 原样返回；
    - 首条非 system 是 assistant/tool → 在前面补一条 user。
    """
    if not messages:
        return messages
    lead = next((i for i, m in enumerate(messages) if m.role not in ("system", "developer")), None)
    if lead is None:
        # 全是 system（极端情况）：补一条 user 让请求可成立
        return list(messages) + [ChatMessage(role="user", content=_USER_LEAD_PLACEHOLDER)]
    if messages[lead].role == "user":
        return messages
    out = list(messages)
    out.insert(lead, ChatMessage(role="user", content=_USER_LEAD_PLACEHOLDER))
    return out


# 补位 user 的文案：明确说明这是压缩后的续接，避免模型把它当成新的用户诉求
_USER_LEAD_PLACEHOLDER = "（上下文已压缩，请接着上面的进度继续当前任务。）"


def _merge_stream_text(current: str, chunk: str) -> str:
    """合并流式工具名/参数增量，兼容三种常见网关行为。

    - 标准增量：直接追加；
    - 累计式：每帧给的是「截至目前的完整内容」，chunk.startswith(current) 时替换；
    - 重复帧：部分网关在首个 delta 后重复发送完整内容，chunk == current 时跳过。

    不能简单用 endswith 判重——参数里出现重复字符（如 ``"a"`` 连续两次）是合法的；
    只有「完全相同」或「包含已累积前缀」才视为非增量。
    """
    if not chunk:
        return current
    if not current:
        return chunk
    if chunk == current:
        return current
    if len(chunk) > len(current) and chunk.startswith(current):
        return chunk
    return current + chunk

# ─────────────── 工作区指纹（x-ws-id，plan-270-1358）───────────────
# 对齐 Ta+3 v0.4.6 gitWorkspaceService.getWorkspaceFingerprint：
# git 远端 URL 归一化（host/path 小写，四端契约逐字节一致）→ sha256 前 16 位小写 hex。
# 平台侧用于用量统计；非仓库/无远端/格式不识别时不发送该头。结果按目录缓存（含 None）。
_LOCAL_ORG_ID = "personal"  # 本地组织：appId 不透传（对齐参考 resolveAppId）
_WS_FP_CACHE: dict[str, str | None] = {}

_WS_SSH_RE = re.compile(r"^git@([^:]+):(.+?)(?:\.git)?$")
_WS_URL_RE = re.compile(r"^(?:https?|ssh)://(?:[^@]+@)?([^/]+)/(.+?)(?:\.git)?$")
_WS_REMOTE_SECTION_RE = re.compile(r'^\s*\[\s*remote\s+"?([^"\]\s]+)"?\s*\]')
_WS_REMOTE_URL_RE = re.compile(r"^\s*url\s*=\s*(.+)$")


def _is_local_host(host: str) -> bool:
    host_wo_port = (host or "").split(":")[0]
    return host_wo_port == "localhost" or bool(
        re.match(r"^127\.\d{1,3}\.\d{1,3}\.\d{1,3}$", host_wo_port))


def normalize_workspace_key(url: str) -> str | None:
    """远端 URL 归一化为 host/path 小写（与平台端契约逐字节一致）。"""
    trimmed = (url or "").strip()
    if not trimmed:
        return None
    m = _WS_SSH_RE.match(trimmed)
    if m and m.group(1) and m.group(2):
        return f"{m.group(1)}/{m.group(2)}".lower()
    m = _WS_URL_RE.match(trimmed)
    if m and m.group(1) and m.group(2):
        host, remote_path = m.group(1), m.group(2)
        # 本地 git 代理：旧式 .../git/owner/repo（默认 github.com）；GHE 主机编码在 path
        if _is_local_host(host) and remote_path.startswith("git/"):
            proxy_path = remote_path[4:]
            segments = proxy_path.split("/")
            if len(segments) >= 3 and "." in segments[0]:
                return proxy_path.lower()
            return f"github.com/{proxy_path}".lower()
        return f"{host}/{remote_path}".lower()
    return None


def _parse_config_remote_urls(config_text: str) -> list[tuple[str, str]]:
    """从 .git/config 文本解析远端 URL 列表（[remote "origin"] 优先，其次首个 remote）。"""
    remotes: list[tuple[str, str]] = []
    current = ""
    for line in re.split(r"\r?\n", config_text or ""):
        section = _WS_REMOTE_SECTION_RE.match(line)
        if section:
            current = section.group(1)
            continue
        if not current:
            continue
        kv = _WS_REMOTE_URL_RE.match(line)
        if kv:
            remotes.append((current, kv.group(1).strip()))
    return remotes


def _resolve_git_config_path(directory: Path) -> Path | None:
    """定位 .git/config；worktree 的 .git 文件（gitdir: …/worktrees/<name>）回归公共 .git。"""
    dot_git = directory / ".git"
    if dot_git.is_dir():
        return dot_git / "config"
    if dot_git.is_file():
        try:
            content = dot_git.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None
        m = re.match(r"^gitdir:\s*(.+)$", content)
        if not m:
            return None
        gitdir = m.group(1).strip()
        idx = gitdir.find("/worktrees/")
        if idx != -1:
            gitdir = gitdir[:idx]
        target = Path(gitdir)
        if not target.is_absolute():
            target = directory / target
        return target / "config"
    return None


def get_workspace_fingerprint(directory: str | None) -> str | None:
    """工作区指纹（16 位小写 hex）。None = 无指纹（不发送 x-ws-id 头）。"""
    if not directory or not str(directory).strip():
        return None
    try:
        d = Path(str(directory).strip()).resolve()
    except OSError:
        return None
    key = str(d)
    if key == os.sep:
        return None
    if key in _WS_FP_CACHE:
        return _WS_FP_CACHE[key]
    fingerprint: str | None = None
    config_path = _resolve_git_config_path(d)
    if config_path is not None:
        try:
            remotes = _parse_config_remote_urls(
                config_path.read_text(encoding="utf-8", errors="replace"))
            ordered = ([r for r in remotes if r[0] == "origin"]
                       + [r for r in remotes if r[0] != "origin"])
            for _name, url in ordered:
                normalized = normalize_workspace_key(url)
                if normalized:
                    fingerprint = hashlib.sha256(
                        normalized.encode("utf-8")).hexdigest()[:16]
                    break
        except OSError:
            fingerprint = None
    _WS_FP_CACHE[key] = fingerprint
    return fingerprint

# ─────────────────── 出站风控提示词脱敏 ───────────────────
# 部分第三方/网关在检测到竞品 CLI 官方系统提示词签名时会以 403 routing_error
# 直接拦截整条请求（如 "you are claude code" 会被判定为未经授权的 CLI 流量）。
# 当工具执行（如 grep 源码、读取依赖）将该字符串读入历史消息后，后续所有请求
# 都会因携带该特征而永久 403。在此处出站时静默脱敏，保留语义但消除触发特征。
_OUTBOUND_RISK_PATTERNS = (
    # 不区分大小写匹配 "you are claude code" 及其空白/连字符变体 → 统一替换为 "you are ta+3 agent"
    (re.compile(r"\b(you\s+are|i\s+am)\s+claude[\s_-]*code\b", re.IGNORECASE), "you are ta+3 agent"),
)


def _sanitize_enabled() -> bool:
    """出站脱敏开关（plan-598-2482）。

    内置拦截策略：**默认开启**。理由——`you are claude code` 这类竞品 CLI 签名一旦
    被工具执行（grep 源码、读依赖）读进历史消息，后续每条请求都会带着它，网关会直接
    403 拦掉整条链路；这种情况下让用户自己去设置里翻开关是不合理的（现场表现只是
    「模型请求失败 403」，用户无从判断原因）。因此默认就做替换，语义保留、特征消除。

    仍保留关闭开关：官方客户端对消息内容不做任何替换（`dist/main/chat/` 下无内容级
    sanitize），若需要与官方逐字节一致的调试场景可置 False。
    """
    try:
        from app.core.config import settings

        return bool(getattr(settings, "ta3_sanitize_outbound", True))
    except Exception:  # pragma: no cover - 配置层异常时按内置拦截（开启）处理
        return True


def sanitize_outbound_text(text: str) -> str:
    """脱敏出站文本中的高危网关拦截特征（默认开启，见 `_sanitize_enabled`）。"""
    if not text or not isinstance(text, str):
        return text
    if not _sanitize_enabled():
        return text
    return _force_sanitize_text(text)


def _force_sanitize_text(text: str) -> str:
    """无条件执行脱敏替换（供「因特征被拦后自动重试」使用，绕开开关）。"""
    if not text or not isinstance(text, str):
        return text
    out = text
    for pat, repl in _OUTBOUND_RISK_PATTERNS:
        out = pat.sub(repl, out)
    return out


def _force_sanitize_structure(val):
    """无条件递归脱敏（供自动重试用）。"""
    if isinstance(val, str):
        return _force_sanitize_text(val)
    if isinstance(val, dict):
        return {k: _force_sanitize_structure(v) for k, v in val.items()}
    if isinstance(val, list):
        return [_force_sanitize_structure(x) for x in val]
    return val


def _has_risk_signature(payload) -> bool:
    """请求体里是否含竞品 CLI 签名特征（决定被拦后是否值得自动重试一次）。"""
    try:
        blob = json.dumps(payload, ensure_ascii=False) if not isinstance(payload, str) else payload
    except Exception:
        return False
    for pat, _repl in _OUTBOUND_RISK_PATTERNS:
        if pat.search(blob):
            return True
    return False


def sanitize_outbound_structure(val):
    """递归脱敏字典、列表或字符串结构体（用于 tool 参数与 content_blocks）。"""
    if isinstance(val, str):
        return sanitize_outbound_text(val)
    if isinstance(val, dict):
        return {k: sanitize_outbound_structure(v) for k, v in val.items()}
    if isinstance(val, list):
        return [sanitize_outbound_structure(x) for x in val]
    return val


def _first_text(*values) -> str:
    for v in values:
        if v:
            return str(v).strip()
    return ""


def _model_record(model_name: str, meta: dict) -> dict:
    """把本地模型行还原成官方 rawModel 的字段形态（协议判定 / 端点 / identity 共用）。

    目录同步会把 identity 各字段与显式端点存进 ta3_meta（plan-601-2495）；旧数据没有
    这些字段时退化为 provider + 请求名 + 标题。
    """
    record: dict = dict(meta.get("identity") or {})
    record.setdefault("provider", meta.get("provider") or "")
    record.setdefault("model", model_name)
    record.setdefault("title", meta.get("title") or "")
    for k in ("protocol", "apiProtocol"):
        if meta.get(k):
            record[k] = meta[k]
    record.update(meta.get("endpoints") or {})
    return record


def _identity(model_name: str, meta: dict) -> str:
    """官方 getModelIdentity 口径（modelClient.js:44-60）。"""
    from app.auth.ta3.catalog import model_identity

    return model_identity(_model_record(model_name, meta))


# ─────────────────── Anthropic 前后缀缓存打点（plan-598-2482） ───────────────────
# 官方客户端在三个「末尾块」位置打 cache_control:{type:'ephemeral'}：
#   system 唯一文本块、历史最后一条消息的最后一个 content block、tools 末项
# （dist/main/chat/anthropicAdapter.js:261-282、:288-290、:336-339）。
# 前缀缓存要求标记位置稳定，所以只打末尾，不在中间或每条消息上打。
_CACHE_CONTROL = {"type": "ephemeral"}


def _mark_tail_with_cache_control(entries: list) -> list:
    """给列表最后一项打 cache_control（浅拷贝，不改原对象）。"""
    if not isinstance(entries, list) or not entries:
        return entries
    marked = [dict(e) if isinstance(e, dict) else e for e in entries]
    last = marked[-1]
    if isinstance(last, dict) and not last.get("cache_control"):
        last["cache_control"] = dict(_CACHE_CONTROL)
    return marked


def _mark_message_tail(messages: list) -> list:
    """给最后一条消息的最后一个 content block 打 cache_control。

    plan-598-2482：官方 anthropicAdapter 的 contentToBlocks() 会把**字符串 content
    一律包成 [{type:'text',text}]**，于是末块永远是可打点的 block；此前这里只处理
    content 已是 list 的情况，字符串 content 被静默跳过，导致消息侧 cache_control
    完全缺失（与官方少一处显式前缀缓存标记）。
    """
    if not isinstance(messages, list) or not messages:
        return messages
    out = list(messages)
    last = out[-1]
    if not isinstance(last, dict):
        return messages
    content = last.get("content")
    # 字符串 content → 包成单个 text block（与官方 contentToBlocks 同口径）
    if isinstance(content, str):
        if not content:
            return messages
        patched = dict(last)
        patched["content"] = [{"type": "text", "text": content}]
        out[-1] = patched
        content = patched["content"]
    if isinstance(content, list) and content:
        patched = dict(out[-1])
        patched["content"] = _mark_tail_with_cache_control(content)
        out[-1] = patched
    return out


def _resolve_alias_for_outbound(real_name: str) -> str | None:
    """真实执行名 → 上行名（官方内置名 / 拓展 MCP 名 / 官方 MCP 名）；无映射返 None。

    统一出口：历史消息里的工具调用也要按同一套规则改名，否则多轮会话中
    模型会看不到自己上一轮用过的工具。
    """
    from app.models.providers.ta3_mcp import is_mcp_tool, mcp_alias
    from app.models.providers.ta3_tool_aliases import TO_TA3, TO_TA3_EXTENSION

    alias = TO_TA3.get(real_name)
    if alias is not None:
        return alias
    alias = TO_TA3_EXTENSION.get(real_name)
    if alias is not None:
        return alias
    if is_mcp_tool(real_name):
        return mcp_alias(real_name)
    return None


class Ta3Provider(ModelProvider):
    name = "ta3"

    def __init__(self, *, api_key: str, base_url: str, model: str, meta: dict | None = None,
                 client_kwargs: dict | None = None):
        # plan-110-438 M3: 供应商代理配置（provider_client_kwargs）注入每次请求的客户端；
        # client 每次请求后关闭并按需重建，故存实例属性、由 _make_client 统一注入。
        self._client_kwargs = dict(client_kwargs or {})
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model_name = model
        self._meta = meta or {}
        self._record = _model_record(model, self._meta)
        # plan-601-2495：协议分派对齐官方 builtInProviders 顺序（providers.js:122-131）——
        # DeepSeek 先匹配；目录未写 deepseek 字段的旧数据按 identity 现算。
        from app.auth.ta3.catalog import is_deepseek_model

        self._deepseek = (bool(self._meta["deepseek"]) if "deepseek" in self._meta
                          else is_deepseek_model(self._record))
        self._anthropic = bool(self._meta.get("anthropic")) and not self._deepseek
        self._completion_opts = self._meta.get("completionOptions") or {}
        self._request_headers = self._meta.get("requestHeaders") or {}
        self._ua = getattr(settings, "ta3_user_agent", "") or _DEFAULT_TA3_UA
        # v28: SSE 空闲超时改读配置——kimi-k3/grok-4.6 长思考时 30s 硬编码会误杀流
        self._stream_idle_timeout = float(getattr(settings, "ta3_stream_idle_timeout", 180) or 180)
        self._client = self._make_client()

    def _make_client(self) -> httpx.AsyncClient:
        """新建本会话的 httpx 客户端。

        plan-601-2495：httpx 默认会注入 `User-Agent: python-httpx/x.y`、`Accept: */*`、
        `Connection`、`Accept-Encoding` 四个客户端级默认头，其中 UA 是最明显的非官方指纹。
        这里全部清空，所有头由 `_wire_headers` 按 undici 的顺序与取值显式给出。
        """
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=10.0, write=10.0, read=self._stream_idle_timeout, pool=10.0,
            ),
            follow_redirects=False,
            **self._client_kwargs,
        )
        for key in list(client.headers.keys()):
            del client.headers[key]
        return client

    def _ensure_client(self) -> httpx.AsyncClient:
        """取本次请求的 httpx 客户端；已关闭则按需重建（provider 实例可能被复用）。"""
        client = self._client
        if client is None or getattr(client, "is_closed", False):
            client = self._make_client()
            self._client = client
        return client

    async def _close_client(self) -> None:
        """关闭本次请求的 httpx 客户端（连同其 keep-alive 连接池）。

        会话隔离要点：每个会话各自持有一条到网关的长连接，若请求结束不关闭，这些
        keep-alive 连接会一直挂着（此前只关响应、不关 client）。远端按账号维度限制
        并发流时，残留连接会占住槽位——表现为「另一会话的新流拿到 200 却长时间收不到
        任何 chunk」，直到某个会话结束或连接被远端掐断才恢复（2026-09-15 实测现场：
        turn=1312 的流空转 2 分钟后报 RemoteProtocolError，随后降级非流式才恢复）。

        注意：这里不把 self._client 置 None——属性保留指向已关闭对象（便于诊断与
        测试回写），下一次取用由 _ensure_client 按 is_closed 重建。关闭失败不阻塞。
        """
        client = self._client
        if client is None:
            return
        try:
            await client.aclose()
        except Exception:
            logger.debug("[ta3] 关闭 httpx 客户端失败(非阻塞)", exc_info=True)

    # ─────────────────────────── 请求构造 ───────────────────────────

    def _base_headers(self, accept: str = _ACCEPT_SSE,
                      workspace_dir: str | None = None, *, compact: bool = False) -> dict:
        """官方 build*Headers 的结果（键名大小写与顺序逐项对齐 modelClient.js:186-232）。

        这里只产出业务头；undici 追加的 host/connection/accept-language/sec-fetch-mode/
        user-agent/accept-encoding/content-length 由 `_wire_headers` 在发送前补齐。
        """
        headers: dict = {
            "Content-Type": "application/json",
            "Accept": accept,
            "X-Call-Source": "APP",
        }
        # plan-270-1358: appId 供后端按应用计费统计（resolveAppId：本地组织 personal 不透传）
        app_id = str(self._meta.get("orgId") or "").strip()
        if app_id and app_id != _LOCAL_ORG_ID:
            headers["lcappid"] = app_id
            headers["x-app-id"] = app_id
        # x-ws-id：工作区指纹（git 远端归一化 sha256[:16]），用于平台侧用量统计
        ws_fp = get_workspace_fingerprint(workspace_dir)
        if ws_fp:
            headers["x-ws-id"] = ws_fp
        if self._anthropic:
            # plan-601-2495：取目录 anthropicVersion（modelClient.js:228），缺省 2023-06-01
            headers["anthropic-version"] = str(
                self._meta.get("anthropicVersion") or self._meta.get("anthropic_version")
                or _DEFAULT_ANTHROPIC_VERSION)
            if self._api_key:
                headers[self._anthropic_auth_header()] = self._api_key
        elif self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
            headers["api-key"] = self._api_key
        for k, v in (self._request_headers or {}).items():
            if k and v is not None:
                headers[str(k)] = str(v)
        # 官方 buildDeepSeekHeaders 在 build 结果之后追加压缩归属头（deepSeekAdapter.js:53-58）
        if compact and self._deepseek:
            headers["x-ta3-compact"] = "1"
        # plan-601-2495：开关打开时才用旧的 Electron 伪装 UA；关闭时由 _wire_headers 补 `node`
        if _send_user_agent():
            headers["User-Agent"] = self._ua
        return headers

    def _anthropic_auth_header(self) -> str:
        """Azure cognitiveservices 端点用 api-key，其余 x-api-key（modelClient.js:202-221）。"""
        from urllib.parse import urlsplit

        try:
            host = (urlsplit(self._base_url).hostname or "").lower()
        except ValueError:
            host = ""
        is_azure = host.endswith(".services.ai.azure.com") or host.endswith(".cognitiveservices.azure.com")
        if is_azure and "cognitiveservices.azure.com" in self._base_url.lower():
            return "api-key"
        return "x-api-key"

    def _endpoint(self) -> str:
        """请求端点（plan-601-2495，对齐 modelClient.js:96-163）。

        - 目录显式端点优先（OpenAI 面任意显式端点；Anthropic 面只认以 /messages 结尾的）；
        - 基址已以 /chat/completions|/responses|/completions（OpenAI）或 /messages（Anthropic）
          结尾时原样使用；
        - Anthropic 基址已带 /v1 时只拼 messages，否则拼 v1/messages。
        """
        from urllib.parse import urlsplit

        endpoints = self._meta.get("endpoints") or {}
        base = self._base_url.rstrip("/")
        if self._anthropic:
            for k in ("anthropicMessagesEndpoint", "messagesEndpoint", "messageEndpoint",
                      "chatCompletionsEndpoint", "chatCompletionEndpoint", "apiEndpoint",
                      "apiUrl", "endpoint"):
                v = str(endpoints.get(k) or "").strip().rstrip("/")
                if v:
                    if re.search(r"/messages$", v, re.I):
                        return v
                    break  # 官方只取首个非空显式端点，不以 /messages 结尾就回落基址
            if re.search(r"/messages$", base, re.I):
                return base
            try:
                path = urlsplit(base).path.rstrip("/")
            except ValueError:
                path = ""
            return f"{base}/messages" if re.search(r"(^|/)v1$", path, re.I) else f"{base}/v1/messages"
        for k in ("chatCompletionsEndpoint", "chatCompletionEndpoint", "completionEndpoint",
                  "apiEndpoint", "apiUrl", "endpoint"):
            v = str(endpoints.get(k) or "").strip().rstrip("/")
            if v:
                return v
        if re.search(r"/(chat/completions|responses|completions)$", base, re.I):
            return base
        return f"{base}/chat/completions"

    def _thinking_intensity(self, request: ChatRequest) -> str:
        """思考档位：request.reasoning_effort 视为用户设置（会话级/全局都是用户可调）。

        官方取 chatSettings.thinkingIntensity，渲染层缺省 `?? "none"`；空值与 none 一律按
        官方 'none' 口径处理（normalizeThinkingIntensity，openAiAdapter.js:13-16）。
        """
        value = str(request.reasoning_effort or "").strip()
        return "none" if not value or value.lower() == "none" else value

    def _thinking_enabled(self, request: ChatRequest | None = None) -> bool:
        """目录是否开启思考：官方只看 completionOptions.thinkingEnabled === true。"""
        return self._completion_opts.get("thinkingEnabled") is True

    def supports_thinking(self) -> bool:
        """是否支持 thinking 参数——只看目录 thinkingEnabled（plan-601-2495：删除 kimi 名称特判，
        官方 anthropicAdapter.js:305 / openAiAdapter.js:67 都只认目录开关）。"""
        return self._thinking_enabled()

    def _thinking_control_mode(self) -> str | None:
        """官方 getThinkingControlMode（openAiAdapter.js:65-93）。"""
        if not self._thinking_enabled():
            return None
        configured = str(self._completion_opts.get("thinkingControl") or "").strip()
        if configured in ("thinking-object", "enable-thinking", "reasoning-effort"):
            return configured
        identity = _identity(self._model_name, self._meta)
        if any(kw in identity for kw in _QWEN_IDENT):
            return "enable-thinking"
        return "thinking-object"

    def _apply_thinking_openai(self, body: dict, request: ChatRequest) -> None:
        """思考字段（官方 openAiAdapter.js:114-131）：按目录 thinkingControl 三种模式下发。"""
        mode = self._thinking_control_mode()
        effort = self._thinking_intensity(request)
        if mode == "enable-thinking":
            body["enable_thinking"] = effort != "none"
            if effort != "none":
                body["reasoning_effort"] = effort
        elif mode == "thinking-object":
            if effort == "none":
                body["thinking"] = {"type": "disabled"}
            else:
                body["thinking"] = {"type": "enabled"}
                body["reasoning_effort"] = effort
        elif mode == "reasoning-effort" and effort != "none":
            body["reasoning_effort"] = effort

    # ─────────────────────────── 消息转换 ───────────────────────────

    def _disguise_message(self, m: ChatMessage) -> dict:
        """出站：ChatMessage → OpenAI dict，工具名/参数伪装 + reasoning 策略 + 出站脱敏。

        对齐参考项目 applyPlainTurnReasoningPolicy：工具调用轮次回传
        reasoning_content，普通回复轮次剥离。出站内容经 sanitize_outbound_text
        脱敏，消除触发网关 403 风控的竞品签名特征。
        """
        raw_content = sanitize_outbound_text(m.content or "")
        out: dict = {"role": m.role, "content": raw_content}
        if m.role in ("system", "developer"):
            out["role"] = "system"
        if m.role == "assistant":
            has_tool_calls = bool(m.tool_calls)
            if m.reasoning_content and has_tool_calls:
                out["reasoning_content"] = sanitize_outbound_text(m.reasoning_content)
            if has_tool_calls:
                tc_list = []
                for tc in m.tool_calls or []:
                    name = str(tc.get("name") or "")
                    args = tc.get("arguments") or {}
                    if isinstance(args, str):
                        args = parse_tool_args(args)
                    alias = _resolve_alias_for_outbound(name)
                    if alias is None:
                        # 未映射的历史调用 → 转普通文本，避免协议断裂。
                        # plan-598-2482: 措辞保持中性（不提及内部工具体系），
                        # 并明确要求不要复述本条，避免模型把提示原样抄进回复。
                        out.pop("tool_calls", None)
                        out["content"] = raw_content + (
                            f"\n\n（历史工具调用 {name} 在当前模型下不可用："
                            f"调用与结果已转为文本记录，请勿复述本条提示，继续使用当前可用工具推进任务。）"
                        )
                        return out
                    clean_args = sanitize_outbound_structure(disguise_args(name, args))
                    tc_list.append({
                        "id": str(tc.get("id") or f"call_{len(tc_list):02d}"),
                        "type": "function",
                        "function": {
                            "name": alias,
                            "arguments": json.dumps(clean_args, ensure_ascii=False),
                        },
                    })
                out["tool_calls"] = tc_list
                out["content"] = raw_content  # 纯 tool_call 回合 content 发空串（部分网关拒绝 null）
        elif m.role == "tool":
            out["role"] = "tool"
            out["tool_call_id"] = m.tool_call_id or ""
            out["content"] = raw_content
        elif m.content_blocks:
            parts = []
            if raw_content:
                parts.append({"type": "text", "text": raw_content})
            for b in m.content_blocks or []:
                parts.append(sanitize_outbound_structure(b))
            out["content"] = parts
        return out

    def _restore_tool_calls(self, tool_calls: list[dict]) -> list[dict]:
        """入站：模型返回的 ta3 工具调用 → 真实执行名+参数。未知名保持原样（由执行层报错）。"""
        out = []
        for tc in tool_calls or []:
            name = str(tc.get("name") or "")
            args = tc.get("arguments") or {}
            if isinstance(args, str):
                args = parse_tool_args(args)
            if not isinstance(args, dict):
                args = {"_raw": str(args)}
            real = FROM_TA3.get(name)
            if real is not None:
                args = restore_args(name, args)
            elif is_mcp_alias(name):
                # MCP 伪装名 → 真实 mcp_<server>_<tool>（按 registry 反查，与 executor
                # 查找源一致）；未命中则保持原样，由执行层报"未知工具"。
                real = resolve_mcp_alias(name)
            out.append({
                "id": str(tc.get("id") or ""),
                "name": real or name,
                "arguments": args,
            })
        return out

    # ─────────────────────────── OpenAI 协议 ───────────────────────────

    @staticmethod
    def _sanitize_openai_tool_pairing(messages: list[dict]) -> list[dict]:
        """确保 OpenAI 协议下的 tool_calls 与 tool 角色严格配对。

        OpenAI 规范要求：
        1. 每一个携带 tool_calls 的 assistant 消息，后面必须紧随对应的 role='tool' 消息；
        2. 每一个 role='tool' 消息的前一条，必须是包含该 tool_call_id 的 assistant 消息；
        若发生未映射工具降级、异常中断丢失 tool_result、或上下文预算截断，
        会导致出现孤儿 tool 消息或悬空 tool_calls，网关会直接返回 API 异常。

        plan-599-2520：旧实现按「紧随其后的连续 tool」整体匹配，只要匹配到**任意一条**
        就原样保留 assistant 的全部 tool_calls——并行调用里只要缺一条结果（压缩把某条
        tool 结果划进摘要区就会这样），就留下悬空 tool_calls，上游直接判非法（ta3 表现为
        「AI服务异常，请稍后重试」，客户端只能看到这一句）。实测事故：会话 630 turn=2596，
        新一轮第一条消息触发压缩后立即请求失败，压缩产物 assistant 67 / tool 66。

        现在逐个 id 对齐：
        - 缺少结果的 tool_call 从 assistant 上剔除（一个都不剩时整条降级为纯文本）；
        - 结果按 tool_calls 的顺序紧跟在 assistant 之后；
        - 没有前置 assistant 的孤儿 tool 消息降级为 user 文本。
        """
        if not messages:
            return []
        cleaned: list[dict] = []
        i = 0
        n = len(messages)
        while i < n:
            m = messages[i]
            role = m.get("role")
            if role == "assistant" and m.get("tool_calls"):
                expected_ids = [tc.get("id") for tc in m["tool_calls"] if tc.get("id")]
                # 收集紧随其后所有 tool 消息的结果（按 id 建索引，不依赖顺序）
                results: dict[str, dict] = {}
                j = i + 1
                while j < n and messages[j].get("role") == "tool":
                    t_id = messages[j].get("tool_call_id")
                    if t_id in expected_ids and t_id not in results:
                        results[t_id] = messages[j]
                    j += 1
                matched = [tid for tid in expected_ids if tid in results]
                if not matched:
                    # 悬空 tool_calls（后续完全没有 tool 结果，如命令执行中被强杀）
                    # 剥离 tool_calls，保留文字，避免协议破坏
                    m_copy = dict(m)
                    m_copy.pop("tool_calls", None)
                    if not m_copy.get("content"):
                        m_copy["content"] = "（工具调用未完成）"
                    cleaned.append(m_copy)
                else:
                    # 只保留有结果的 tool_calls，避免部分缺失造成的悬空
                    if len(matched) != len(m["tool_calls"]):
                        m = dict(m)
                        m["tool_calls"] = [tc for tc in m["tool_calls"]
                                           if not tc.get("id") or tc.get("id") in results]
                    cleaned.append(m)
                    cleaned.extend(results[tid] for tid in matched)
                i = j
            elif role == "tool":
                # 没有前置匹配 assistant 的孤儿 tool 消息 → 降级为 user 提示文本
                content = m.get("content") or ""
                cleaned.append({
                    "role": "user",
                    "content": f"（历史工具调用结果：{content}）" if content else "（历史工具调用完成）",
                })
                i += 1
            else:
                cleaned.append(m)
                i += 1
        return cleaned

    def _runtime_max_tokens(self, request: ChatRequest):
        """运行时 max_tokens（官方 runtimeOptions.maxTokens）：只认用户显式配置的正数。"""
        return request.max_tokens if request.max_tokens else _UNDEF

    def _is_zai(self) -> bool:
        # 官方判定是 `String(model.provider).toLowerCase() === 'zai'`（openAiAdapter.js:137、170），
        # 不是 identity 子串匹配
        return str(self._meta.get("provider") or "").lower() == "zai"

    def _build_openai_body(self, request: ChatRequest, disguised: list[dict], *,
                           text: bool = False) -> dict:
        """OpenAI / DeepSeek 请求体（plan-601-2495）。

        流式聊天对齐 openAiAdapter.buildRequestBody（:98-144），文本请求（text=True）对齐
        buildTextRequestBody（:149-176）；DeepSeek 再叠加 applyDeepSeekBodySemantics
        （deepSeekAdapter.js:32-52）。键顺序、取值来源、prune 时机均与官方一致：
        - 温度只取目录 completionOptions.temperature，缺省 0.1，思考开启也照发（OpenAI 面从不删温度）；
          不再使用本项目的 0.3/0.7 设置温度，也没有 claude 名称特判；
        - max_tokens = 运行时 ?? 目录值，不做 min 截断；
        - top_p / frequency_penalty / presence_penalty / stop 取自目录。
        """
        opts = self._completion_opts
        # plan-598-2517：压缩后可能一条 user 也没有（保留区从 assistant 起切）——
        # 网关要求以 user 开场，否则直接判非法（ta3 表现为「AI服务异常，请稍后重试」）。
        msgs = _ensure_user_lead(_ta3_relocate_inline_system(list(request.messages)))
        from app.orchestration.history_sequence import prepare_history, validate_openai_wire
        msgs = prepare_history(msgs)
        raw_msgs = [self._disguise_message(m) for m in msgs]
        runtime_max = self._runtime_max_tokens(request)
        body: dict = {
            "model": request.model or self._model_name,
            "messages": validate_openai_wire(self._sanitize_openai_tool_pairing(raw_msgs)),
            "stream": not text,
            "temperature": _to_number(_opt(opts, "temperature"), _DEFAULT_TEMPERATURE),
            "top_p": _opt(opts, "topP", "top_p"),
            "frequency_penalty": _opt(opts, "frequencyPenalty", "frequency_penalty"),
            "presence_penalty": _opt(opts, "presencePenalty", "presence_penalty"),
            "max_tokens": _to_number(_nullish(runtime_max, _opt(opts, "maxTokens", "max_tokens")), _UNDEF),
            "stop": _opt(opts, "stop"),
        }
        if not text:
            # 官方文本请求不带思考字段；流式聊天按目录 thinkingControl 下发
            self._apply_thinking_openai(body, request)
        if disguised:
            body["tools"] = disguised
            body["tool_choice"] = "auto"
        _prune(body)
        if text:
            if self._is_zai():
                body["thinking"] = {"type": "disabled"}
        elif self._is_zai():
            body["stream_options"] = {"include_usage": True}
            body["tool_stream"] = True
        if self._deepseek:
            # DeepSeek 只在 stream=true 时接受 stream_options.include_usage
            if body.get("stream") is False:
                body.pop("stream_options", None)
            else:
                body["stream_options"] = {"include_usage": True}
            body.pop("cache_control", None)
            body["messages"] = [{k: v for k, v in m.items() if k != "cache_control"}
                                for m in body.get("messages") or []]
        return body

    def _parse_openai_frame(self, line: str, monitor) -> bool:
        """解析单条 SSE data 帧；返回是否终止。"""
        if not line.startswith("data:"):
            return False
        data = line[5:].strip()
        if not data:
            return False
        if data == "[DONE]":
            monitor["terminal"] = True
            return True
        try:
            payload = json.loads(data)
        except ValueError:
            return False
        monitor["frames"] += 1
        # 网关下发错误帧（如 HTTP 200 下返回 payload.error）
        if isinstance(payload, dict) and payload.get("error"):
            err_obj = payload.get("error")
            err_msg = err_obj.get("message") if isinstance(err_obj, dict) else str(err_obj)
            monitor["error"] = err_msg or "网关返回错误帧"
            monitor["terminal"] = True
            logger.warning("[ta3] OpenAI协议收到网关错误帧: %s", err_msg)
            return True
        # usage（含 reasoning/cached 明细）
        usage = payload.get("usage")
        if isinstance(usage, dict):
            details = usage.get("completion_tokens_details") or {}
            prompt_details = usage.get("prompt_tokens_details") or {}
            monitor["usage"] = Usage(
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                total_tokens=int(usage.get("total_tokens") or 0),
                cached_input_tokens=int(prompt_details.get("cached_tokens") or 0),
                reasoning_tokens=int(details.get("reasoning_tokens") or 0),
            )
        choices = payload.get("choices") or []
        if not choices:
            return False
        choice = choices[0]
        delta = choice.get("delta") or {}
        reasoning = (delta.get("reasoning_content")
                     or delta.get("reasoning")
                     or delta.get("thinking") or "")
        if reasoning:
            monitor["thinking_parts"].append(reasoning)
        text = delta.get("content")
        if text:
            monitor["content_parts"].append(text)
        if delta.get("tool_calls"):
            for tc in delta["tool_calls"]:
                idx = int(tc.get("index") or 0)
                slot = monitor["tool_calls"].get(idx)
                new_id = str(tc.get("id") or "")
                # 部分网关（少数 OpenAI 兼容层）所有工具都发 index=0：
                # id 变化说明是新的工具调用，另开槽位，避免多个工具的参数互相拼接。
                if slot is not None and new_id and slot.get("id") and new_id != slot["id"]:
                    idx = max(monitor["tool_calls"].keys(), default=-1) + 1
                    slot = None
                if slot is None:
                    slot = {"id": "", "name": "", "arguments": ""}
                    monitor["tool_calls"][idx] = slot
                if new_id:
                    slot["id"] = new_id
                fn = tc.get("function") or {}
                name = fn.get("name")
                if isinstance(name, str) and name:
                    slot["name"] = _merge_stream_text(slot["name"], name)
                arg = fn.get("arguments")
                if isinstance(arg, str) and arg:
                    slot["arguments"] = _merge_stream_text(slot["arguments"], arg)
                elif isinstance(arg, dict):
                    # 非标准网关可能直接给对象，统一转成 JSON 文本交给 finalize 解析
                    slot["arguments"] = json.dumps(arg, ensure_ascii=False)
        finish_reason = choice.get("finish_reason") or choice.get("finishReason")
        if finish_reason:
            monitor["finish_reason"] = str(finish_reason)
            monitor["terminal"] = True
            # 不在此 break：网关常在 finish_reason 后补发 usage 帧与 [DONE]，
            # 提前终止会丢失 usage（由 [DONE] 或流结束兜底）
        return False

    # ─────────────────────────── Anthropic 协议 ───────────────────────────

    @staticmethod
    def _push_anthropic(converted: list[dict], role: str, blocks: list[dict]) -> None:
        """官方 pushAnthropicMessage（anthropicAdapter.js:211-221）：空内容不入列，相邻同角色合并。"""
        if not blocks:
            return
        if converted and converted[-1]["role"] == role and isinstance(converted[-1]["content"], list):
            converted[-1]["content"].extend(blocks)
            return
        converted.append({"role": role, "content": blocks})

    def _convert_anthropic_messages(self, messages: list[ChatMessage]) -> tuple[str, list[dict]]:
        """ChatMessage → Anthropic messages（plan-601-2495，对齐 anthropicAdapter.js:179-238）。

        - 先经 `_ta3_relocate_inline_system` 把中途 system/developer 改写为官方形态，
          之后只剩开头一条 system，按官方 take-first 取用；
        - content 一律转为块数组（contentToBlocks），空消息不入列，相邻同角色合并；
        - assistant 只回传带 signature 的 thinking / redacted_thinking 块并放在最前
          （normalizeAnthropicThinkingBlocks + unshift，:159-175、:201）；本项目只存
          reasoning_content 文本、没有 signature，因此不回传（官方对 kimi 无特判）。
        """
        converted: list[dict] = []
        system = ""
        # plan-598-2517：重定位后可能没有任何 user（压缩摘要被改写成 assistant，
        # 边界标记被丢弃），必须以 user 开场，否则 Anthropic 面同样拒请求。
        for m in _ensure_user_lead(_ta3_relocate_inline_system(list(messages))):
            raw_text = sanitize_outbound_text(m.content or "")
            if m.role in ("system", "developer"):
                system = system or raw_text
                continue
            if m.role == "user":
                blocks: list[dict] = []
                if raw_text:
                    blocks.append({"type": "text", "text": raw_text})
                if m.content_blocks:
                    # plan-147-674: OpenAI image_url 块 → Anthropic image 块（data URI
                    # 解析对齐 anthropic.py 既有实现）。此前原样透传 image_url，
                    # claude/kimi 系网关拒绝或忽略，多模态图片注入静默失效。
                    for block in m.content_blocks or []:
                        if block.get("type") == "image_url":
                            img_data = (block.get("image_url") or {}).get("url", "")
                            media_type = "image/png"
                            raw_data = ""
                            if img_data.startswith("data:"):
                                header, _, raw_data = img_data.partition(",")
                                if ";" in header:
                                    media_type = header.split(":", 1)[1].split(";", 1)[0]
                            else:
                                raw_data = img_data
                            blocks.append({
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": raw_data,
                                },
                            })
                        elif block.get("type") == "text":
                            _t = sanitize_outbound_text(str(block.get("text") or ""))
                            if _t.strip():
                                blocks.append({"type": "text", "text": _t})
                        else:
                            blocks.append(sanitize_outbound_structure(block))
                self._push_anthropic(converted, "user", blocks)
            elif m.role == "tool":
                tool_use_id = (m.tool_call_id or "").strip()
                if not tool_use_id:
                    # 官方：无 tool_call_id 的 tool 消息按普通文本块处理（:183-185）
                    self._push_anthropic(converted, "user",
                                         [{"type": "text", "text": raw_text}] if raw_text else [])
                    continue
                self._push_anthropic(converted, "user", [{
                    "type": "tool_result", "tool_use_id": tool_use_id, "content": raw_text,
                }])
            elif m.role == "assistant":
                blocks = []
                has_tool_calls = bool(m.tool_calls)
                if raw_text:
                    blocks.append({"type": "text", "text": raw_text})
                if has_tool_calls:
                    for tc in m.tool_calls or []:
                        name = str(tc.get("name") or "")
                        alias = _resolve_alias_for_outbound(name)
                        if alias is None:
                            continue
                        args = tc.get("arguments") or {}
                        if isinstance(args, str):
                            args = parse_tool_args(args)
                        clean_args = sanitize_outbound_structure(
                            disguise_args(name, args if isinstance(args, dict) else {})
                        )
                        blocks.append({
                            "type": "tool_use",
                            "id": str(tc.get("id") or ""),
                            "name": alias,
                            "input": clean_args,
                        })
                self._push_anthropic(converted, "assistant", blocks)
        return system, converted

    def _convert_anthropic_tools(self, disguised: list[dict]) -> list[dict]:
        out = []
        for t in disguised:
            fn = t.get("function") or {}
            out.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters") or {"type": "object"},
            })
        # plan-598-2482：官方在 tools 末项打 cache_control（anthropicAdapter.js:338），
        # 用于显式前缀缓存；本项目此前完全没有该字段。
        return _mark_tail_with_cache_control(out)

    def _build_anthropic_body(self, request: ChatRequest, disguised: list[dict], *,
                              text: bool = False) -> dict:
        """Anthropic 请求体（plan-601-2495，对齐 anthropicAdapter.buildAnthropicRequestBody :283-341）。

        键顺序：model, messages, system, stream, temperature, top_p, max_tokens, stop_sequences,
        thinking, output_config, tools。温度/top_p/stop 取目录 completionOptions（温度缺省 0.1，
        无 claude 名称特判），思考开启（adaptive/enabled）时删除 temperature 与 top_p；
        max_tokens = 运行时 ?? 目录值 ?? 2048，不再用 min 截断目录值。
        文本请求（text=True）stream=false，思考字段照官方同口径下发（官方 Anthropic 文本请求
        与流式共用同一构造函数，档位缺省为 none）。
        """
        opts = self._completion_opts
        from app.orchestration.history_sequence import prepare_history, validate_anthropic_wire
        system, messages = self._convert_anthropic_messages(prepare_history(request.messages))
        validate_anthropic_wire(messages)
        max_tokens = _to_number(
            _nullish(self._runtime_max_tokens(request), _opt(opts, "maxTokens", "max_tokens")),
            _DEFAULT_MAX_TOKENS)
        body: dict = {
            "model": request.model or self._model_name,
            "messages": _mark_message_tail(messages),
            "system": ([{"type": "text", "text": system, "cache_control": dict(_CACHE_CONTROL)}]
                       if system else _UNDEF),
            "stream": not text,
            "temperature": _to_number(_opt(opts, "temperature"), _DEFAULT_TEMPERATURE),
            "top_p": _opt(opts, "topP", "top_p"),
            "max_tokens": max_tokens,
            "stop_sequences": _opt(opts, "stop"),
        }
        # 文本请求的档位：官方 textRequest 不传 thinkingIntensity，等价 'none'
        effort = "none" if text else self._thinking_intensity(request)
        if self._thinking_enabled():
            if effort == "none":
                body["thinking"] = {"type": "disabled"}
            else:
                levels = opts.get("thinkingLevels")
                valid = ([str((lv or {}).get("level") or "").lower() for lv in levels
                          if isinstance(lv, dict) and (lv or {}).get("level")]
                         if isinstance(levels, list) else ["low", "medium", "high"])
                eff = effort if effort in valid else "low"
                control = str(opts.get("thinkingControl") or "anthropic-adaptive").lower()
                if control in ("anthropic-enabled", "enabled", "manual"):
                    if not isinstance(max_tokens, (int, float)) or max_tokens < 2048:
                        raise RuntimeError("Anthropic manual thinking 要求 max_tokens 至少为 2048")
                    budget = _to_number(
                        _opt(opts, "thinkingBudgetTokens", "thinking_budget_tokens"),
                        8192 if eff == "high" else 4096 if eff == "medium" else 1024)
                    body["thinking"] = {
                        "type": "enabled",
                        "budget_tokens": min(max_tokens - 1024, max(1024, budget)),
                    }
                else:
                    body["thinking"] = {"type": "adaptive"}
                    body["output_config"] = {"effort": eff}
                body.pop("temperature", None)
                body.pop("top_p", None)
        tools = self._convert_anthropic_tools(disguised) if disguised else []
        if tools:
            body["tools"] = tools
        return _prune(body)

    def _parse_anthropic_frame(self, line: str, monitor) -> bool:
        if not line.startswith("data:"):
            return False
        data = line[5:].strip()
        if not data:
            return False
        if data == "[DONE]":
            monitor["terminal"] = True
            return True
        try:
            payload = json.loads(data)
        except ValueError:
            return False
        monitor["frames"] += 1
        # 网关下发错误帧
        if isinstance(payload, dict) and payload.get("error"):
            err_obj = payload.get("error")
            err_msg = err_obj.get("message") if isinstance(err_obj, dict) else str(err_obj)
            monitor["error"] = err_msg or "网关返回错误帧"
            monitor["terminal"] = True
            logger.warning("[ta3] Anthropic协议收到网关错误帧: %s", err_msg)
            return True
        etype = payload.get("type")
        if etype == "message_start":
            # v28: Anthropic 协议的 input_tokens 在 message_start，补全 usage
            msg = payload.get("message") or {}
            usage = msg.get("usage") or {}
            if isinstance(usage, dict) and usage.get("input_tokens"):
                monitor["usage"].prompt_tokens = int(usage["input_tokens"])
        elif etype == "content_block_start":
            block = payload.get("content_block") or {}
            if block.get("type") == "tool_use":
                idx = payload.get("index", 0)
                new_id = str(block.get("id") or "")
                slot = monitor["anthropic_tools"].get(idx)
                # 与 OpenAI 支路同理：网关若把所有工具都放在 index=0，按 id 另开槽位
                if slot is not None and new_id and slot.get("id") and new_id != slot["id"]:
                    idx = max(monitor["anthropic_tools"].keys(), default=-1) + 1
                    slot = None
                if slot is None:
                    slot = {"id": "", "name": "", "arguments": ""}
                    monitor["anthropic_tools"][idx] = slot
                if new_id:
                    slot["id"] = new_id
                _name = block.get("name")
                if isinstance(_name, str) and _name:
                    slot["name"] = _name
                # 部分 Anthropic 兼容网关在 start 帧直接给完整 input（没有后续
                # input_json_delta），只认 partial_json 会导致参数永远为空。
                _input = block.get("input")
                if isinstance(_input, dict) and _input:
                    slot["arguments"] = json.dumps(_input, ensure_ascii=False)
                elif isinstance(_input, str) and _input.strip() and _input.strip() != "{}":
                    slot["arguments"] = _input
        elif etype == "content_block_delta":
            delta = payload.get("delta") or {}
            dtype = delta.get("type")
            idx = payload.get("index", 0)
            # v29 (plan-78): thinking 增量字段变体兜底——Anthropic 规范为
            # thinking_delta.thinking，部分网关用 thinking/reasoning/reasoning_delta
            # 承载；此前只认 thinking_delta.thinking，思考内容丢失会误判空响应。
            _th = (delta.get("thinking") or delta.get("thinking_delta")
                   or delta.get("reasoning") or delta.get("reasoning_delta") or "")
            if dtype == "text_delta" and delta.get("text"):
                monitor["content_parts"].append(delta["text"])
            elif _th:
                monitor["thinking_parts"].append(_th)
            elif isinstance(delta.get("partial_json"), str) and delta["partial_json"]:
                slot = monitor["anthropic_tools"].get(idx)
                if slot is not None:
                    slot["arguments"] = _merge_stream_text(
                        slot["arguments"], delta["partial_json"])
            elif isinstance(delta.get("input"), dict) and delta["input"]:
                slot = monitor["anthropic_tools"].get(idx)
                if slot is not None:
                    slot["arguments"] = json.dumps(delta["input"], ensure_ascii=False)
        elif etype == "message_delta":
            delta = payload.get("delta") or {}
            stop_reason = delta.get("stop_reason")
            if stop_reason:
                monitor["finish_reason"] = str(stop_reason)
            # v29 (plan-78): 部分网关把思考增量放在 message_delta 的 delta 里
            _th = delta.get("thinking") or delta.get("reasoning") or ""
            if _th:
                monitor["thinking_parts"].append(_th)
            # v28: output_tokens 在 message_delta 的 usage，与 message_start 的 input 合并
            usage = payload.get("usage") or {}
            if isinstance(usage, dict) and usage.get("output_tokens"):
                monitor["usage"].completion_tokens = int(usage["output_tokens"])
                monitor["usage"].total_tokens = (
                    monitor["usage"].prompt_tokens + monitor["usage"].completion_tokens
                )
        elif etype == "ping":
            # v28: Anthropic 协议心跳帧——仅用于诊断网关是否仍在 keep-alive
            monitor["last_heartbeat_at"] = time.time()
        elif etype == "message_stop":
            monitor["terminal"] = True
            return True
        return False

    def _finalize_anthropic_tools(self, monitor: dict) -> list[dict]:
        out = []
        for idx in sorted(monitor["anthropic_tools"].keys()):
            slot = monitor["anthropic_tools"][idx]
            # plan-20261016-ux（问题 3）：宽松解析，失败带 _parse_error（执行器直接回告模型）
            args = parse_tool_args(slot["arguments"])
            name = slot["name"]
            real = FROM_TA3.get(name)
            if real is not None:
                args = restore_args(name, args)
            elif is_mcp_alias(name):
                # MCP 伪装名 → 真实 mcp_<server>_<tool>（按 registry 反查）
                real = resolve_mcp_alias(name)
            out.append({"id": slot["id"], "name": real or name, "arguments": args})
        return out

    # ─────────────────────────── 流式主流程 ───────────────────────────

    def _new_monitor(self) -> dict:
        return {
            "terminal": False,
            "finish_reason": "stop",
            "content_parts": [],
            "thinking_parts": [],
            "tool_calls": {},          # OpenAI 增量
            "anthropic_tools": {},     # Anthropic 增量
            "usage": Usage(),
            "last_heartbeat_at": None,  # v28: Anthropic ping 心跳时间（诊断用）
            # v966: 成功解析的数据帧计数（不含空行/[DONE]）。0 = 网关连一个
            # 数据帧都没下发（网络卡顿/服务器处理慢/网关断流），不能判定为
            # "模型主动结束"，上层据此走重试而非静默收尾。
            "frames": 0,
        }

    def _ingest_json_response(self, payload, monitor: dict) -> None:
        """非流式 JSON 响应 → monitor（对齐 textRequest.extractOpenAiResponse /
        anthropicAdapter.extractAnthropicResponse）。"""
        if not isinstance(payload, dict):
            return
        monitor["frames"] += 1
        if payload.get("error"):
            err = payload["error"]
            monitor["error"] = (err.get("message") if isinstance(err, dict) else str(err)) or "网关返回错误"
            return
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        if self._anthropic:
            text_parts, thinking_parts = [], []
            for idx, block in enumerate(payload.get("content") or []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    text_parts.append(str(block.get("text") or ""))
                elif block.get("type") == "thinking":
                    thinking_parts.append(str(block.get("thinking") or ""))
                elif block.get("type") == "tool_use" and block.get("name"):
                    monitor["anthropic_tools"][idx] = {
                        "id": str(block.get("id") or ""), "name": str(block["name"]),
                        "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                    }
            if text_parts:
                monitor["content_parts"].append("".join(text_parts))
            if thinking_parts:
                monitor["thinking_parts"].append("".join(thinking_parts))
            if payload.get("stop_reason"):
                monitor["finish_reason"] = str(payload["stop_reason"])
            if usage:
                monitor["usage"] = Usage(
                    prompt_tokens=int(usage.get("input_tokens") or 0),
                    completion_tokens=int(usage.get("output_tokens") or 0),
                    total_tokens=int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0),
                    cached_input_tokens=int(usage.get("cache_read_input_tokens") or 0),
                )
            return
        choice = (payload.get("choices") or [{}])[0] or {}
        msg = choice.get("message") or {}
        content = msg.get("content")
        if isinstance(content, list):
            content = "".join(str((c or {}).get("text") or "") for c in content if isinstance(c, dict))
        if content:
            monitor["content_parts"].append(str(content))
        reasoning = msg.get("reasoning_content") or msg.get("reasoning") or msg.get("thinking")
        if isinstance(reasoning, str) and reasoning:
            monitor["thinking_parts"].append(reasoning)
        for idx, tc in enumerate(msg.get("tool_calls") or []):
            fn = (tc or {}).get("function") or {}
            args = fn.get("arguments")
            monitor["tool_calls"][idx] = {
                "id": str((tc or {}).get("id") or ""), "name": str(fn.get("name") or ""),
                "arguments": args if isinstance(args, str) else json.dumps(args or {}, ensure_ascii=False),
            }
        if choice.get("finish_reason"):
            monitor["finish_reason"] = str(choice["finish_reason"])
        if usage:
            details = usage.get("completion_tokens_details") or {}
            prompt_details = usage.get("prompt_tokens_details") or {}
            monitor["usage"] = Usage(
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                total_tokens=int(usage.get("total_tokens") or 0),
                cached_input_tokens=int(prompt_details.get("cached_tokens") or 0),
                reasoning_tokens=int(details.get("reasoning_tokens") or 0),
            )

    async def _refresh_llm_key(self) -> str | None:
        """401 自愈：强制重取一次目录，拿该模型的最新 llm- key（官方 llmKeyRetry.js:92-157）。

        返回新 key；无法自愈（无供应商归属 / 刷新失败 / key 未变化）时返回 None，调用方不得重试。
        """
        provider_id = getattr(self, "provider_row_id", None)
        if not provider_id:
            return None
        try:
            from app.auth.ta3.catalog import sync_ta3_models
            from app.auth.ta3.oauth import DEFAULT_TA3_API_BASE
            from app.persistence.database import async_session_factory
            from app.persistence.models.model_reg import Provider

            async with async_session_factory() as db:
                provider = await db.get(Provider, provider_id)
                if provider is None:
                    return None
                api_base = (provider.base_url or DEFAULT_TA3_API_BASE).rstrip("/")
                entries = await sync_ta3_models(db, provider, api_base)
        except Exception:
            logger.warning("[ta3] 401 自愈：目录刷新失败，放弃重发", exc_info=True)
            return None
        for entry in entries or []:
            if entry.get("name") == self._model_name:
                key = str(entry.get("api_key") or "").strip()
                if key and key != self._api_key:
                    return key
                break
        return None

    async def _stream_llm(self, request: ChatRequest,
                          parse_fn, *, text: bool = False) -> AsyncIterator[dict]:
        """发起请求并按事件格式产出 thinking/content/done。

        text=True 为辅助文本请求（plan-601-2495，对齐 textRequest.buildModelTextRequest）：
        stream=false、Accept 取 JSON 优先、OpenAI 面不带思考字段；响应若仍是 SSE 则照常流式解析。
        """
        disguised = disguise_tools(request.tools or [])
        url = self._endpoint()
        if self._anthropic:
            body = self._build_anthropic_body(request, disguised, text=text)
        else:
            body = self._build_openai_body(request, disguised, text=text)
        accept = _ACCEPT_JSON if text else _ACCEPT_SSE
        headers = self._base_headers(accept, workspace_dir=request.workspace_dir)
        # 诊断护栏（v0.5.13）：ta3 的 LLM 网关只在「模型级 dispatch 路径」下工作，
        # 且只认目录下发的 per-model `llm-` key（账号 ide-session- 一律 401）。
        # 一旦检测到站点根 URL 或非 llm- 前缀，就在发请求前明确告警——否则现场只会
        # 看到一条 404/401，难以反推是 base_url/api_key 存错了来源。
        if "/ai/dispatch/" not in url:
            logger.warning(
                "[ta3] base_url 疑似站点根而非模型级 dispatch 路径: %s → %s "
                "（应为 …/ai/dispatch/v2 或 …/ai/dispatch/v2/anthropic，"
                "请核对模型行的 base_url / 重新同步目录）", self._base_url, url)
        if self._api_key and not self._api_key.startswith("llm-"):
            logger.warning(
                "[ta3] api_key 前缀非 llm-（网关 LLM 通道只认 llm- key，"
                "ide-session- 会被拒）: %s…", self._api_key[:12])
        logger.info("[ta3] model=%s protocol=%s tools=%d text=%s → %s",
                    request.model or self._model_name,
                    "anthropic" if self._anthropic else ("deepseek" if self._deepseek else "openai"),
                    len(disguised), text, url)

        monitor = self._new_monitor()
        sent_thinking = 0
        sent_content = 0
        _started_at = time.monotonic()
        # plan-838: 移除思考看门狗——此前思考阶段（已收 thinking 帧、未产出 content/tool）
        # 使用更短的 ta3_thinking_watchdog(240s) 主动掐断，导致 kimi-k3 长思考被提前终止后
        # 带 thinking 的 thinking_timeout 被上层判为"健康"而静默结束任务。
        # 现与 TA3 其它模型完全一致：全程统一使用 ta3_stream_idle_timeout(默认 300s) 空闲超时。
        # 会话隔离：本次请求用独立客户端，结束后立即关闭（含异常/取消路径），
        # 不把 keep-alive 长连接留给下一次请求或其它会话。
        client = self._ensure_client()
        _retried_after_block = False
        _retried_after_401 = False
        try:
            while True:
                payload = _js_json(body)
                wire = _wire_headers(headers, url, len(payload))
                async with client.stream("POST", url, content=payload, headers=wire) as resp:
                    if resp.status_code != 200:
                        err_text = (await resp.aread()).decode("utf-8", errors="replace")[:400]
                        # plan-601-2495：401 换 key 原地重发一次（官方 streamRequest.js:415-438）。
                        # 恰好一次、只在请求边界；目录刷新拿不到新 key 时按原错误上报。
                        if resp.status_code == 401 and not _retried_after_401:
                            _retried_after_401 = True
                            new_key = await self._refresh_llm_key()
                            if new_key:
                                self._api_key = new_key
                                headers = self._base_headers(accept, workspace_dir=request.workspace_dir)
                                logger.warning("[ta3] 401：已重取 llm key 并重发一次")
                                continue
                        # plan-601-2495 内置拦截（第二层）收窄：只在 **403 且响应体含风控/拦截
                        # 字样**、请求体确实含竞品签名时强制脱敏重发一次。400 一律不重发——
                        # 400 是参数/额度类错误（官方 quotaFailure 处理），脱敏无效还多打一次。
                        if (not _retried_after_block
                                and _is_block_response(resp.status_code, err_text)
                                and _has_risk_signature(body)):
                            _retried_after_block = True
                            body = _force_sanitize_structure(body)
                            logger.warning(
                                "[ta3] 请求因内容特征被拦截(%s)，已自动脱敏并重试一次: %s",
                                resp.status_code, err_text[:160])
                            continue
                        # v36/v46: 请求报错时，若开启「额度自动重置 / 自动透支」配置，
                        # 查一次额度；有窗口 >=100% 则自动提交一次恢复（日窗→透支，周/月→重置；
                        # 带冷却，失败不影响上报）。
                        try:
                            from app.auth.ta3.auto_recover import maybe_auto_recover

                            await maybe_auto_recover(
                                getattr(self, "provider_row_id", None),
                                reason=f"模型请求失败 {resp.status_code}",
                            )
                        except Exception:
                            logger.debug("[ta3] 自动透支钩子异常(忽略)", exc_info=True)
                        raise RuntimeError(f"模型请求失败 {resp.status_code}：{err_text}")
                    # 官方 readModelTextResponse：content-type 为 SSE 才流式解析，否则按 JSON 读
                    _ctype = ""
                    try:
                        _ctype = str(resp.headers.get("content-type") or "").lower()
                    except Exception:  # 测试替身可能没有 headers
                        _ctype = ""
                    if text and _ctype and "text/event-stream" not in _ctype:
                        raw = await resp.aread()
                        try:
                            self._ingest_json_response(json.loads(raw.decode("utf-8", errors="replace")),
                                                       monitor)
                        except ValueError:
                            monitor["error"] = "网关返回了无法解析的响应"
                        while len(monitor["content_parts"]) > sent_content:
                            yield {"type": "content", "delta": monitor["content_parts"][sent_content]}
                            sent_content += 1
                        break
                    _lines = resp.aiter_lines()
                    while True:
                        try:
                            line = await asyncio.wait_for(_lines.__anext__(), timeout=self._stream_idle_timeout)
                        except StopAsyncIteration:
                            break
                        except asyncio.TimeoutError:
                            monitor["finish_reason"] = "timeout"
                            logger.warning(
                                "[ta3] model=%s 空闲超时: thinking=%d content=%d tools=%d, 耗时 %.1fs",
                                request.model or self._model_name,
                                len(monitor["thinking_parts"]), len(monitor["content_parts"]),
                                len(monitor["tool_calls"]) + len(monitor["anthropic_tools"]),
                                time.monotonic() - _started_at,
                            )
                            break
                        terminal = parse_fn(line, monitor)
                        # 实时产出（内容/思考逐段 yield）——parts 全量保留供 done 组装，
                        # 用游标消费避免 pop 丢失终态文本
                        while len(monitor["thinking_parts"]) > sent_thinking:
                            yield {"type": "thinking", "delta": monitor["thinking_parts"][sent_thinking]}
                            sent_thinking += 1
                        while len(monitor["content_parts"]) > sent_content:
                            yield {"type": "content", "delta": monitor["content_parts"][sent_content]}
                            sent_content += 1
                        if terminal:
                            break
                break
        except asyncio.CancelledError:
            raise
        except httpx.TimeoutException as e:
            raise RuntimeError(f"模型请求超时：{e.__class__.__name__}") from e
        except httpx.HTTPError as e:
            raise RuntimeError(f"模型请求失败：{e.__class__.__name__}: {e}") from e
        finally:
            # 会话隔离：正常结束/异常/取消一律关闭本请求的客户端与 keep-alive 连接池，
            # 不留给下一次请求或其它会话（残留长连接会占住远端并发槽）。
            await self._close_client()

        # 组装结果
        content = "".join(monitor["content_parts"]) or None
        thinking = "".join(monitor["thinking_parts"]) or None
        if self._anthropic:
            tool_calls = self._finalize_anthropic_tools(monitor)
        else:
            tool_calls = []
            for idx in sorted(monitor["tool_calls"].keys()):
                slot = monitor["tool_calls"][idx]
                # plan-20261016-ux（问题 3）：宽松解析，失败带 _parse_error（执行器直接回告模型）
                args = parse_tool_args(slot["arguments"])
                name = slot["name"]
                real = FROM_TA3.get(name)
                if real is not None:
                    args = restore_args(name, args)
                elif is_mcp_alias(name):
                    # MCP 伪装名 → 真实 mcp_<server>_<tool>（按 registry 反查）
                    real = resolve_mcp_alias(name)
                tool_calls.append({"id": slot["id"], "name": real or name, "arguments": args})

        if monitor.get("error"):
            raise RuntimeError(f"模型请求失败：{monitor['error']}")

        # v28: 流式耗时/产出统计 + 空响应兜底日志（诊断"突然停止"现场）
        _usage_desc = (
            monitor["usage"].model_dump()
            if hasattr(monitor["usage"], "model_dump") else monitor["usage"]
        )
        logger.info(
            "[ta3] model=%s done finish=%s thinking=%d content=%d tools=%d usage=%s elapsed=%.1fs",
            request.model or self._model_name, monitor["finish_reason"],
            len(thinking or ""), len(content or ""), len(tool_calls),
            _usage_desc, time.monotonic() - _started_at,
        )
        # v966: 零帧断流比"空响应"更精确——网关未下发任何数据帧即结束
        # （HTTP 200 + 空流/DONE），说明请求期间网络卡顿或网关处理超时，
        # 必须区别于"模型已应答但主动无输出"（后者有帧，见 finish_reason）。
        if monitor["frames"] == 0 and monitor["finish_reason"] == "stop":
            logger.warning(
                "[ta3] model=%s 零帧断流: 未收到任何数据帧, 耗时 %.1fs (网关空流/网络异常)",
                request.model or self._model_name, time.monotonic() - _started_at,
            )
        if not content and not thinking and not tool_calls and monitor["finish_reason"] == "stop":
            logger.warning(
                "[ta3] model=%s 空响应流: 无 content/thinking/tool_calls, finish=stop, frames=%d, 耗时 %.1fs",
                request.model or self._model_name, monitor["frames"],
                time.monotonic() - _started_at,
            )

        yield {
            "type": "done",
            "content": content,
            "thinking": thinking,
            "tool_calls": tool_calls,
            "finish_reason": monitor["finish_reason"],
            "usage": monitor["usage"],
            "frames": monitor["frames"],
        }

    async def stream_structured(self, request: ChatRequest) -> AsyncIterator[dict]:
        parse_fn = self._parse_anthropic_frame if self._anthropic else self._parse_openai_frame
        async for event in self._stream_llm(request, parse_fn):
            yield event

    async def stream(self, request: ChatRequest) -> AsyncIterator[str]:
        """纯文本流（非结构化）。"""
        parse_fn = self._parse_anthropic_frame if self._anthropic else self._parse_openai_frame
        async for event in self._stream_llm(request, parse_fn):
            if event["type"] == "content":
                yield event["delta"]

    async def chat(self, request: ChatRequest) -> ChatResponse:
        """非流式调用。

        plan-601-2495：无工具的 chat()（压缩摘要、标题、记忆提取等辅助调用）对齐官方
        textRequest（stream:false + JSON 优先 Accept + OpenAI 面不带思考字段）；带工具的
        chat() 是 agent 主循环的非流式回退，仍按流式聊天请求构造，保持与主请求同形态。
        """
        response = ChatResponse(content=None, tool_calls=[], finish_reason="stop", usage=Usage())
        parse_fn = self._parse_anthropic_frame if self._anthropic else self._parse_openai_frame
        async for event in self._stream_llm(request, parse_fn, text=not request.tools):
            if event["type"] == "done":
                response = ChatResponse(
                    content=event["content"],
                    thinking=event["thinking"],
                    tool_calls=event["tool_calls"],
                    finish_reason=event["finish_reason"],
                    usage=event["usage"],
                    model=request.model or self._model_name,
                )
        return response
