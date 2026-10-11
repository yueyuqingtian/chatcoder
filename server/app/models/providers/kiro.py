"""KiroProvider — AWS Kiro（CodeWhisperer RTS）模型供应商实现。

严格复刻 Kiro IDE / kiro-cli 的 GenerateAssistantResponse 契约（指纹头 + 请求体），
与 Ta3Provider 同思路：不走任何 SDK，用 httpx 裸请求完全控制头与体。

- 端点：POST {chat}（https://q.{region}.amazonaws.com/generateAssistantResponse）
- 指纹头：KiroIDE 系 x-amz-user-agent / user-agent + amz-sdk-*（缺失即被风控）
- 请求体：conversationState（agentTaskType=vibe / currentMessage.userInputMessage /
  history）+ profileArn
- 响应：事件流（AWS event stream 文本形态），按 JSON 块逐个提取
  assistantResponseEvent / reasoningContentEvent / toolUseEvent / metadataEvent
- 工具：出站 OpenAI/Anthropic schema → toolSpecification；入站 toolUseEvent →
  {id, name, arguments}；历史 tool 结果 → userInputMessageContext.toolResults
- 401：调用注入的 refresh 回调（async 无参 → 返回新 token）后重试一次

上下文管理、压缩、审批、编排全部沿用当前项目（本类只负责「请求怎么发」）。
"""
from __future__ import annotations

import base64
import codecs
import hashlib
import io
import json
import logging
import re
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.models.base import ModelProvider
from app.models.schemas import ChatMessage, ChatRequest, ChatResponse, Usage

logger = logging.getLogger(__name__)

# Kiro IDE 客户端标识（与 claude-kiro.js KIRO_CONSTANTS 对齐，可经 meta 覆盖）
_KIRO_VERSION = "0.11.63"
_SDK_VER = "1.0.34"
_ORIGIN = "AI_EDITOR"
_AGENT_MODE = "vibe"
_CHAT_TRIGGER = "MANUAL"

# 思考档位 → Kiro effort。Kiro /effort 官方档位为 low|medium|high|xhigh|max
# （kiro-cli 2.12+ 实测；本机 Kiro IDE 选择器亦提供 xHigh / Max）。minimal
# 收敛到 low，其余原样透传——此前 xhigh/max 被归一为 high，用户选的
# 「最大思考深度」实际只发了 high。
_THINKING_EFFORT_MAP = {
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}

_CONNECT_TIMEOUT_S = 15.0
_STREAM_IDLE_TIMEOUT_S = 180.0


def _machine_id(seed: str) -> str:
    """稳定机器指纹：sha256(profileArn||clientId||兜底)。"""
    return hashlib.sha256((seed or "KIRO_DEFAULT_MACHINE").encode("utf-8")).hexdigest()


def _user_agent(machine_id: str) -> str:
    import os
    import platform

    os_name = platform.system().lower()
    release = platform.release()
    if os_name == "windows":
        os_name = f"windows#{release}"
    elif os_name == "darwin":
        os_name = f"macos#{release}"
    else:
        os_name = f"{os_name}#{release}"
    node_ver = f"{os.sys.version_info.major}.{os.sys.version_info.minor}.{os.sys.version_info.micro}"
    return (
        f"aws-sdk-js/{_SDK_VER} ua/2.1 os/{os_name} lang/js md/nodejs#{node_ver} "
        f"api/codewhispererstreaming#{_SDK_VER} m/E KiroIDE-{_KIRO_VERSION}-{machine_id}"
    )


def _is_token_invalid(status_code: int, body: str) -> bool:
    """判断响应是否属于「令牌失效」类认证错误（决定是否刷新后重试）。

    实测：Kiro 对过期/无效令牌返回 403 + "The bearer token included in the
    request is invalid."（plan-116-458）；401 同属认证失败。其余 403（权限、
    配额等）不刷新——盲目刷新会把非认证错误也掩盖成一次无谓等待。
    """
    if status_code == 401:
        return True
    text = (body or "").lower()
    if "token" not in text:
        return False
    return any(k in text for k in ("invalid", "expired", "unauthorized", "not valid"))


# ─────────────────────────── 内联图片预算 ───────────────────────────
# 对齐 KiroCrew imaging.py 的实测值：多图会话（>20 张）中任一图片宽或高超过
# 2000px，后端拒绝整个请求（实测报错 IMAGE_DIMENSION_EXCEEDED）；单图 base64
# 编码另受 5 MiB 上限约束。由于对话历史每轮重放，一张超限图会永久卡住会话
# （历史下标固定，重传小图也无法顶掉），因此超限图必须缩放到合规；缩不出来
# （解码失败等）则放弃内联（fail closed），绝不把原图送上去。
_MAX_IMAGE_EDGE_PX = 2000
_MAX_IMAGE_B64_BYTES = 5 * 1024 * 1024
# 编码超预算时的逐轮收缩：系数 0.8（约减 36% 面积/轮）、最多 6 轮
_ENCODE_SHRINK_FACTOR = 0.8
_MAX_ENCODE_ATTEMPTS = 6
# 缩放下限：再小就失去可读性，放弃内联而非缩成无意义小图
_MIN_IMAGE_EDGE_PX = 256
# 源像素上限：单次解码的内存/耗时兜底（64MP 解码峰值约 256MiB）
_MAX_IMAGE_SOURCE_PIXELS = 64_000_000

# mime → Pillow 保存格式。GIF 落为 PNG 首帧：视觉模型只读首帧，
# 且调色板图直接缩放有损，静止帧更忠实。表外格式（如 TIFF）重编码为 PNG。
_PIL_SAVE_FORMAT = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/webp": "WEBP",
    "image/bmp": "BMP",
    "image/gif": "PNG",
}
# Pillow 格式 → 实际 mime（重编码后回填，避免「PNG 字节贴 JPEG 标签」）
_FORMAT_MIME = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "MPO": "image/jpeg",
    "WEBP": "image/webp",
    "BMP": "image/bmp",
    "GIF": "image/gif",
}

_pil_checked = False
_pil_handle = None


def _pil():
    """惰性导入 Pillow，返回 (Image, ImageOps) 或 None（未安装）。"""
    global _pil_checked, _pil_handle
    if not _pil_checked:
        try:
            from PIL import Image, ImageOps

            _pil_handle = (Image, ImageOps)
        except ImportError:  # pragma: no cover - Pillow 已是项目依赖
            _pil_handle = None
        _pil_checked = True
    return _pil_handle


def _b64_len(raw_len: int) -> int:
    """base64 编码后的精确长度（每 3 字节 4 字符，含填充）。"""
    return 4 * ((raw_len + 2) // 3)


def _render_image(raw_bytes: bytes, mime: str, max_edge: int):
    """把图片渲染为最长边 ≤ max_edge 的合规副本（保持宽高比）。

    已在限内且格式受支持 → 原样返回（仅读头部，不解码）；
    超限或格式不受支持 → 解码、按 EXIF 方向摆正、LANCZOS 缩放后重编码。
    任何失败返回 None（调用方必须放弃内联，绝不能把原图送上去）。
    """
    pil = _pil()
    if pil is None:
        # 无 Pillow：无法判定尺寸，保持原行为（原样返回）
        return raw_bytes, mime
    image_mod, imageops_mod = pil
    try:
        with image_mod.open(io.BytesIO(raw_bytes)) as img:
            detected = img.format or ""
            if max(img.width, img.height) <= max_edge and detected in _FORMAT_MIME:
                img.verify()
                return raw_bytes, _FORMAT_MIME[detected]
            # EXIF 方向先烘焙进像素（重编码会丢掉方向标签，手机照片会转 90°）
            oriented = imageops_mod.exif_transpose(img) or img
            # 调色板模式先提升为 RGBA：直接缩放会插值调色板索引而非真实通道
            src = oriented.convert("RGBA") if oriented.mode in ("P", "PA") else oriented
            long_edge = max(src.width, src.height)
            if max_edge > 0 and long_edge > max_edge:
                scale = max_edge / long_edge
                new_size = (max(1, round(src.width * scale)), max(1, round(src.height * scale)))
                resample = getattr(image_mod, "LANCZOS", getattr(image_mod, "ANTIALIAS", 1))
                src = src.resize(new_size, resample)
            # 按解码出的真实格式重编码，不认调用方声明的后缀
            detected_mime = _FORMAT_MIME.get(detected, "")
            fmt = _PIL_SAVE_FORMAT.get(detected_mime, "PNG")
            if fmt in ("JPEG", "BMP") and src.mode not in ("RGB", "L"):
                # JPEG/BMP 无 alpha 通道，先拍平
                src = src.convert("RGB")
            buf = io.BytesIO()
            if fmt == "JPEG":
                src.save(buf, format="JPEG", quality=90)
            elif fmt == "WEBP":
                # 源 WEBP 可能是无损的；Pillow 默认有损 q80，保持无损避免二次损失
                src.save(buf, format="WEBP", lossless=True)
            else:
                src.save(buf, format=fmt)
            return buf.getvalue(), _FORMAT_MIME.get(fmt, "image/png")
    except Exception:
        logger.warning("[kiro] 图片缩放/校验失败（mime=%s），跳过该图", mime, exc_info=True)
        return None


def _fit_image_bytes(raw_bytes: bytes, mime: str):
    """渲染出同时满足尺寸（2000px）与编码（5 MiB）双预算的图片副本。

    先按尺寸缩放；若 base64 编码仍超预算，按 0.8 系数逐轮收缩最长边（最多 6 轮）。
    返回 (bytes, mime) 或 None（没有合规副本，调用方放弃内联）。
    """
    pil = _pil()
    if pil is None:
        return raw_bytes, mime
    # 源像素上限：只读头部即可判定，防解压炸弹
    dims = None
    try:
        with pil[0].open(io.BytesIO(raw_bytes)) as probe:
            dims = (probe.width, probe.height)
    except Exception:
        return None
    if dims is not None and dims[0] * dims[1] > _MAX_IMAGE_SOURCE_PIXELS:
        logger.warning("[kiro] 图片源像素过大（%dx%d），跳过该图", dims[0], dims[1])
        return None

    fitted = _render_image(raw_bytes, mime, _MAX_IMAGE_EDGE_PX)
    if fitted is None:
        return None
    out_bytes, out_mime = fitted
    if _b64_len(len(out_bytes)) <= _MAX_IMAGE_B64_BYTES:
        return out_bytes, out_mime
    # 编码超预算：按自身最长边逐轮收缩（每轮重新解码原图，避免重采样叠加损失）
    edge = max(dims) if dims else _MAX_IMAGE_EDGE_PX
    for _ in range(_MAX_ENCODE_ATTEMPTS):
        edge = max(_MIN_IMAGE_EDGE_PX, int(edge * _ENCODE_SHRINK_FACTOR))
        fitted = _render_image(raw_bytes, mime, edge)
        if fitted is None:
            return None
        out_bytes, out_mime = fitted
        if _b64_len(len(out_bytes)) <= _MAX_IMAGE_B64_BYTES:
            return out_bytes, out_mime
        if edge <= _MIN_IMAGE_EDGE_PX:
            break
    return None


def _fit_image_data_url(url: str):
    """data:URL → (base64, mime)：超限图先缩放到合规；不可处理返回 None。"""
    try:
        head, b64 = url.split(",", 1)
        media = head.split(";")[0].split(":")[1] if ":" in head else "image/png"
    except (ValueError, IndexError):
        return None
    if not b64:
        return None
    try:
        raw = base64.b64decode(b64)
    except Exception:
        return None
    fitted = _fit_image_bytes(raw, media)
    if fitted is None:
        return None
    out_bytes, out_mime = fitted
    return base64.b64encode(out_bytes).decode("ascii"), out_mime

class _EventStreamParser:
    """解析 Kiro 事件流文本：帧头（:event-type / :message-type）+ 裸 JSON payload。

    实测结构（claude-sonnet-5.5，2026-10 两次真实抓包）每个事件形如：

        <二进制帧头>:event-type reasoningContentEvent
        :content-type application/json
        :message-type event{"text":"9"}

    即事件名在帧头、payload 是不含事件名包装的裸 JSON——此前实现假设 payload
    自带事件名键（如 {"reasoningContentEvent": {...}}），于是 reasoningContentEvent
    的 {"text": ...} 不匹配任何分支被整段丢弃（思考块不显示的根因，plan-116-457）。

    解析规则（实测帧头为 :event-type 后跟 1 字节值类型 + 2 字节长度 + 事件名，
    长度字节可能与行分隔符同码、不能按行切分）：取出 :event-type 与 :content-type
    之间文本中的事件名，再在该事件的 :message-type 之后定位第一个 { 做括号平衡
    扫描，二者配对输出；二进制帧头噪声在找不到标记时按尾部窗口丢弃。
    """

    _MARK = ":event-type"
    _TAIL_KEEP = 96

    def __init__(self) -> None:
        self._buf = ""
        self._event = ""
        self._json = False

    @staticmethod
    def _scan_json(text: str, start: int) -> int:
        """从 start（'{'）做括号平衡扫描；返回闭合下标，未闭合返回 -1。"""
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        return i
        return -1

    def feed(self, text: str) -> list[tuple[str, dict]]:
        self._buf += text
        out: list[tuple[str, dict]] = []
        while True:
            if not self._event:
                idx = self._buf.find(self._MARK)
                if idx == -1:
                    # 无事件标记：二进制帧头噪声，保留尾部窗口防标记跨块
                    self._buf = self._buf[-self._TAIL_KEEP:]
                    break
                # 事件名区 = ":event-type" 与 ":content-type" 之间。实测帧头为
                # ":event-type\x07\x00\xNN<name>"：长度字节 0x0D（meteringEvent）
                # 恰与行分隔符同码，不能按行切分——按标记定位后取字母段。
                nxt = self._buf.find(":content-type", idx + len(self._MARK))
                if nxt == -1:
                    # 事件名区可能跨块，等更多数据
                    self._buf = self._buf[idx:]
                    break
                region = self._buf[idx + len(self._MARK):nxt]
                m = re.search(r"[A-Za-z][A-Za-z0-9]*", region)
                if not m:
                    # 异常数据：消费该标记避免死循环
                    self._buf = self._buf[nxt:]
                    continue
                self._event = m.group(0)
                self._buf = self._buf[nxt:]
                continue
            if not self._json:
                # 状态 2a：定位 payload 起点（:message-type 之后第一个 '{'）
                mid = self._buf.find(":message-type")
                if mid == -1:
                    self._buf = self._buf[-self._TAIL_KEEP:]
                    break
                start = self._buf.find("{", mid)
                if start == -1:
                    self._buf = self._buf[mid:]
                    break
                # 切到 JSON 起点后即与帧头标记解耦：此后只等 JSON 闭合。
                # 逐字节喂入时标记已被切走，若仍依赖 :message-type 会死锁。
                self._buf = self._buf[start:]
                self._json = True
            # 状态 2b：等待 JSON 闭合（缓冲保证以 '{' 开头）
            end = self._scan_json(self._buf, 0)
            if end == -1:
                break
            raw = self._buf[:end + 1]
            self._buf = self._buf[end + 1:]
            event, self._event = self._event, ""
            self._json = False
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            if isinstance(data, dict) and event:
                out.append((event, data))
        return out


class KiroProvider(ModelProvider):
    name = "kiro"

    def __init__(self, *, api_key: str, base_url: str, model: str, meta: dict | None = None,
                 refresh_token=None, client_kwargs: dict | None = None):
        self._client_kwargs = dict(client_kwargs or {})
        self._token = api_key
        self._base_url = base_url or ""
        self._model_name = model
        self._meta = meta or {}
        # 401 时调用（async 无参）→ 返回新 token；由 registry 注入（依赖 DB 会话）
        self._refresh_token = refresh_token
        self._client = self._make_client()

    # ─────────────────────────── 基础设施 ───────────────────────────

    def _make_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(connect=_CONNECT_TIMEOUT_S, write=30.0,
                                  read=_STREAM_IDLE_TIMEOUT_S, pool=10.0),
            headers={"Accept-Encoding": "identity"},
            follow_redirects=False,
            **self._client_kwargs,
        )

    def _ensure_client(self) -> httpx.AsyncClient:
        client = self._client
        if client is None or getattr(client, "is_closed", False):
            client = self._make_client()
            self._client = client
        return client

    def supports_thinking(self) -> bool:
        return True

    def _profile_arn(self) -> str:
        return str(self._meta.get("profile_arn") or "")

    def _machine(self) -> str:
        seed = self._profile_arn() or str(self._meta.get("client_id") or "")
        return _machine_id(seed)

    def _headers(self) -> dict[str, str]:
        machine = self._machine()
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "x-amz-user-agent": f"aws-sdk-js/{_SDK_VER} KiroIDE-{_KIRO_VERSION}-{machine}",
            "user-agent": _user_agent(machine),
            "amz-sdk-invocation-id": str(uuid.uuid4()),
            "amz-sdk-request": "attempt=1; max=3",
            "x-amzn-codewhisperer-optout": "true",
            "x-amzn-kiro-agent-mode": _AGENT_MODE,
            "Connection": "close",
        }

    # ─────────────────────── 请求体构造（出站） ───────────────────────

    def _server_model_id(self) -> str:
        from app.auth.kiro.catalog import normalize_model_id

        return normalize_model_id(str(self._meta.get("model_id") or self._model_name or ""))

    @staticmethod
    def _content_text(msg: ChatMessage) -> str:
        if msg.content:
            return msg.content
        if msg.content_blocks:
            parts = [b.get("text") or "" for b in msg.content_blocks if b.get("type") == "text"]
            return "".join(parts)
        return ""

    @staticmethod
    def _images_of(msg: ChatMessage) -> list[dict]:
        """消息图片 → Kiro images 载荷（超限图先缩放到合规，失败则跳过）。

        plan-116-459: 此前直接把 base64 塞入请求。多图会话中任一图片宽或高超过
        2000px，后端拒绝整个请求（IMAGE_DIMENSION_EXCEEDED），且对话历史每轮重放，
        超限图会永久卡住会话——因此先缩放到合规；缩不出来（解码失败等）放弃内联。
        """
        out: list[dict] = []
        for block in msg.content_blocks or []:
            if block.get("type") != "image_url":
                continue
            url = (block.get("image_url") or {}).get("url") or ""
            if not url.startswith("data:"):
                continue
            fitted = _fit_image_data_url(url)
            if fitted is None:
                logger.warning("[kiro] 图片无法渲染为合规副本，已跳过内联")
                continue
            b64, media = fitted
            out.append({
                "format": media.split("/")[-1],
                "source": {"bytes": b64},
            })
        return out

    @staticmethod
    def _convert_tools(tools: list[dict] | None) -> list[dict]:
        """OpenAI / Anthropic 工具 schema → Kiro toolSpecification。"""
        out: list[dict] = []
        for tool in tools or []:
            fn = tool.get("function") if isinstance(tool, dict) else None
            if isinstance(fn, dict):  # OpenAI 形态
                name = fn.get("name") or ""
                desc = fn.get("description") or ""
                schema = fn.get("parameters") or {}
            elif isinstance(tool, dict) and tool.get("name"):  # Anthropic 形态
                name = tool.get("name") or ""
                desc = tool.get("description") or ""
                schema = tool.get("input_schema") or {}
            else:
                continue
            if not name:
                continue
            out.append({
                "toolSpecification": {
                    "name": name,
                    "description": desc,
                    "inputSchema": {"json": schema},
                }
            })
        if not out:
            # 服务端要求至少一个工具项，占位工具避免请求被拒
            out.append({
                "toolSpecification": {
                    "name": "no_tool_available",
                    "description": "Placeholder tool when no tools are available.",
                    "inputSchema": {"json": {"type": "object", "properties": {}}},
                }
            })
        return out

    def _thinking_prefix(self, request: ChatRequest) -> str:
        """思考激活前缀（Kiro 无独立字段，靠 system 段文本标签激活）。

        plan-116-457：Kiro 服务端靠 <thinking_mode>/<thinking_effort> 文本标签
        激活思考（对齐参考实现 claude-kiro.js _generateThinkingPrefix）——此前
        从不注入该前缀，故服务端从不产出思考内容（用户实测「没有显示思考块」）。
        档位 none / thinking=False → 空串（不激活，交由服务端默认行为）。
        """
        effort = (request.reasoning_effort or "").strip().lower()
        if request.thinking is False or effort == "none":
            return ""
        if not effort:
            if request.thinking is True:
                effort = "medium"
            else:
                return ""
        mapped = _THINKING_EFFORT_MAP.get(effort, "high")
        return (f"<thinking_mode>adaptive</thinking_mode>"
                f"<thinking_effort>{mapped}</thinking_effort>")

    def _build_payload(self, request: ChatRequest) -> dict:
        """chatcoder 内部消息 → Kiro conversationState（plan-116-457）。

        1) 工具配对：assistant.tool_calls → toolUses；tool 消息 → 所属轮次 user
           消息的 toolResults，出站前双向过滤。Kiro 要求每个 toolResult 都有对应
           toolUse，否则上游 400「unexpected tool_use_id found in tool_result
           blocks」——此前只收集结果、从不生成 toolUses，孤儿结果必然被拒。
        2) 思考：档位经 _thinking_prefix 拼入 system 段最前。
        """
        model_id = self._server_model_id()
        system_parts: list[str] = []
        turns: list[dict] = []
        pending_results: list[dict] = []

        for msg in request.messages:
            if msg.role in ("system", "developer"):
                if msg.content:
                    system_parts.append(msg.content)
                continue

            if msg.role == "user":
                # 工具结果与用户输入同轮：挂到本条 user 消息上（Anthropic 语义）
                results, pending_results = pending_results, []
                turns.append({
                    "kind": "user",
                    "content": self._content_text(msg),
                    "images": self._images_of(msg),
                    "tool_results": results,
                })
                continue

            if msg.role == "assistant":
                # 未挂载的工具结果先独立成一条 user turn（后接 assistant 的场景）
                if pending_results:
                    turns.append({
                        "kind": "user", "content": "", "images": [],
                        "tool_results": pending_results,
                    })
                    pending_results = []
                tool_uses: list[dict] = []
                for tc in msg.tool_calls or []:
                    tid = str(tc.get("id") or "").strip()
                    if not tid:
                        continue
                    raw_args = tc.get("arguments")
                    if isinstance(raw_args, str):
                        try:
                            raw_args = json.loads(raw_args) if raw_args.strip() else {}
                        except ValueError:
                            raw_args = {}
                    if not isinstance(raw_args, dict):
                        raw_args = {}
                    tool_uses.append({
                        "toolUseId": tid,
                        "name": str(tc.get("name") or ""),
                        "input": raw_args,
                    })
                turns.append({
                    "kind": "assistant",
                    "content": self._content_text(msg),
                    "tool_uses": tool_uses,
                })
                continue

            if msg.role == "tool":
                tid = str(msg.tool_call_id or "").strip()
                if not tid:
                    # 无 id 无法配对，剔除（避免孤儿结果触发上游 400）
                    continue
                pending_results.append({
                    "content": [{"text": self._content_text(msg)}],
                    "status": "success",
                    "toolUseId": tid,
                })
                continue

        # 序列以工具结果收尾（工具刚执行完、等 AI 继续）→ 结果自成一轮
        if pending_results:
            turns.append({
                "kind": "user", "content": "", "images": [],
                "tool_results": pending_results,
            })
            pending_results = []

        # 双向配对过滤：只保留 use/result 成对的 id，其余剔除
        use_ids = {tu["toolUseId"] for t in turns
                   if t["kind"] == "assistant" for tu in t.get("tool_uses") or []}
        result_ids = {tr["toolUseId"] for t in turns
                      if t["kind"] == "user" for tr in t.get("tool_results") or []}
        paired = use_ids & result_ids
        for t in turns:
            if t["kind"] == "assistant" and t.get("tool_uses"):
                t["tool_uses"] = [tu for tu in t["tool_uses"] if tu["toolUseId"] in paired]
            if t["kind"] == "user" and t.get("tool_results"):
                t["tool_results"] = [tr for tr in t["tool_results"] if tr["toolUseId"] in paired]

        if not turns:
            turns.append({"kind": "user", "content": "Continue",
                          "images": [], "tool_results": []})

        # system 段（含思考激活前缀）拼到首条 user 消息（Kiro 无 system 通道）
        system_text = "\n\n".join(p for p in system_parts if p)
        prefix = self._thinking_prefix(request)
        if prefix:
            system_text = f"{prefix}\n{system_text}" if system_text else prefix
        if system_text:
            first_user = next((t for t in turns if t["kind"] == "user"), None)
            if first_user is not None:
                first_user["content"] = (
                    f"{system_text}\n\n{first_user['content']}"
                    if first_user["content"] else system_text
                )
            else:
                turns.insert(0, {"kind": "user", "content": system_text,
                                 "images": [], "tool_results": []})

        # 拆分 current / history：currentMessage 必须为 user（否则移入 history 并以
        # "Continue" 触发继续）；history 必须以 assistantResponseMessage 结尾。
        current = turns[-1]
        history_turns = turns[:-1]
        if current["kind"] == "assistant":
            history_turns = list(turns)
            current = {"kind": "user", "content": "Continue",
                       "images": [], "tool_results": []}
        if history_turns and history_turns[-1]["kind"] == "user":
            history_turns.append({"kind": "assistant", "content": "Continue", "tool_uses": []})

        def _to_user_item(turn: dict, *, with_tools: bool) -> dict:
            content = turn.get("content") or ""
            results = turn.get("tool_results") or []
            if not content:
                content = "Tool results provided." if results else "Continue"
            item: dict[str, Any] = {
                "content": content, "modelId": model_id, "origin": _ORIGIN,
            }
            if turn.get("images"):
                item["images"] = turn["images"]
            ctx: dict[str, Any] = {}
            if results:
                ctx["toolResults"] = results
            if with_tools:
                ctx["tools"] = self._convert_tools(request.tools)
            if ctx:
                item["userInputMessageContext"] = ctx
            return item

        history: list[dict] = []
        for t in history_turns:
            if t["kind"] == "user":
                history.append({"userInputMessage": _to_user_item(t, with_tools=False)})
            else:
                am: dict[str, Any] = {"content": t.get("content") or ""}
                if t.get("tool_uses"):
                    am["toolUses"] = t["tool_uses"]
                history.append({"assistantResponseMessage": am})

        state: dict[str, Any] = {
            "agentTaskType": _AGENT_MODE,
            "chatTriggerType": _CHAT_TRIGGER,
            "conversationId": request.session_id or str(uuid.uuid4()),
            "currentMessage": {"userInputMessage": _to_user_item(current, with_tools=True)},
        }
        if history:
            state["history"] = history

        payload: dict[str, Any] = {"conversationState": state}
        profile_arn = self._profile_arn()
        if profile_arn:
            payload["profileArn"] = profile_arn
        return payload

    # ─────────────────────────── 事件解析 ───────────────────────────

    async def _iter_events(self, resp: httpx.Response) -> AsyncIterator[tuple[str, dict]]:
        """逐块读取响应，按 (事件名, payload) 输出（解析规则见 _EventStreamParser）。

        用增量解码器跨块拼接 UTF-8：HTTP 块边界可能切在多字节字符中间，
        逐块独立 decode 会丢掉断口字节（中文内容尤甚）。
        """
        parser = _EventStreamParser()
        decoder = codecs.getincrementaldecoder("utf-8")("ignore")
        async for chunk in resp.aiter_bytes():
            text = decoder.decode(chunk)
            if text:
                for item in parser.feed(text):
                    yield item
        tail = decoder.decode(b"", final=True)
        if tail:
            for item in parser.feed(tail):
                yield item

    # ─────────────────────────── 主流程 ───────────────────────────

    async def stream_structured(self, request: ChatRequest) -> AsyncIterator[dict[str, Any]]:
        """结构化流式：yield content / thinking / done 事件。"""
        payload = self._build_payload(request)
        client = self._ensure_client()

        full_content: list[str] = []
        full_thinking: list[str] = []
        tool_calls_map: dict[str, dict] = {}
        usage = Usage()
        finish_reason = "stop"
        frames = 0

        for attempt in (1, 2):
            try:
                async with client.stream(
                    "POST", self._base_url, json=payload, headers=self._headers()
                ) as resp:
                    if (resp.status_code in (401, 403) and attempt == 1
                            and self._refresh_token is not None):
                        # plan-116-458: 令牌失效兜底——Kiro 对过期令牌返回 403
                        # （401 亦属认证失败），刷新后重试一次；非令牌类错误或刷新
                        # 失败自动落到下方统一抛错，不吞真实原因。
                        _peek = (await resp.aread()).decode("utf-8", errors="replace")
                        if _is_token_invalid(resp.status_code, _peek):
                            logger.warning(
                                "[kiro] 令牌失效（HTTP %s），刷新后重试: %s",
                                resp.status_code, _peek[:200])
                            new_token = await self._refresh_token()
                            if new_token:
                                self._token = new_token
                                continue
                    if resp.status_code != 200:
                        body = (await resp.aread()).decode("utf-8", errors="replace")
                        logger.error("[kiro] 请求失败 HTTP %s: %s", resp.status_code, body[:300])
                        raise RuntimeError(
                            f"Kiro upstream error (HTTP {resp.status_code}): {body[:200]}")

                    async for event_type, data in self._iter_events(resp):
                        frames += 1
                        if event_type == "assistantResponseEvent":
                            delta = str(data.get("content") or "")
                            if delta:
                                full_content.append(delta)
                                yield {"type": "content", "delta": delta}
                        elif event_type == "reasoningContentEvent":
                            # 实测：Kiro 的思考走独立事件 {"text": "..."}（plan-116-457）
                            delta = str(data.get("text") or "")
                            if delta:
                                full_thinking.append(delta)
                                yield {"type": "thinking", "delta": delta}
                        elif event_type == "toolUseEvent":
                            tid = str(data.get("toolUseId") or "")
                            if not tid:
                                continue
                            entry = tool_calls_map.setdefault(
                                tid, {"id": tid, "name": str(data.get("name") or ""), "arguments": ""})
                            if data.get("name"):
                                entry["name"] = str(data["name"])
                            raw_input = data.get("input")
                            if isinstance(raw_input, dict):
                                entry["arguments"] = json.dumps(raw_input, ensure_ascii=False)
                            elif isinstance(raw_input, str) and raw_input:
                                entry["arguments"] += raw_input
                        elif event_type in ("messageMetadataEvent", "metadataEvent"):
                            u = data.get("usage") if isinstance(data.get("usage"), dict) else data
                            if isinstance(u, dict):
                                pt = int(u.get("inputTokens") or u.get("promptTokens") or 0)
                                ct = int(u.get("outputTokens") or u.get("completionTokens") or 0)
                                if pt or ct:
                                    usage = Usage(prompt_tokens=pt, completion_tokens=ct,
                                                  total_tokens=pt + ct)
                        elif event_type == "meteringEvent":
                            credits = data.get("usage")
                            if isinstance(credits, (int, float)):
                                logger.debug("[kiro] turn metering: %s credits", credits)
                    break
            except httpx.TimeoutException as te:
                raise RuntimeError(f"Kiro 请求超时（{_STREAM_IDLE_TIMEOUT_S}s）: {te}") from te

        final_tool_calls: list[dict] = []
        for tc in tool_calls_map.values():
            args = tc.get("arguments") or ""
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args.strip() else {}
                except ValueError:
                    args = {"_raw": args}
            final_tool_calls.append({"id": tc["id"], "name": tc["name"], "arguments": args})

        yield {
            "type": "done",
            "content": "".join(full_content) or None,
            "thinking": "".join(full_thinking) or None,
            "tool_calls": final_tool_calls,
            "finish_reason": "tool_calls" if final_tool_calls else finish_reason,
            "usage": usage,
            "frames_received": frames,
        }

    async def stream(self, request: ChatRequest) -> AsyncIterator[str]:
        async for ev in self.stream_structured(request):
            if ev.get("type") == "content" and ev.get("delta"):
                yield ev["delta"]

    async def chat(self, request: ChatRequest) -> ChatResponse:
        content: list[str] = []
        thinking: list[str] = []
        tool_calls: list[dict] = []
        usage = Usage()
        finish_reason = "stop"
        frames = 0
        async for ev in self.stream_structured(request):
            etype = ev.get("type")
            if etype == "content" and ev.get("delta"):
                content.append(ev["delta"])
            elif etype == "thinking" and ev.get("delta"):
                thinking.append(ev["delta"])
            elif etype == "done":
                tool_calls = ev.get("tool_calls") or []
                usage = ev.get("usage") or Usage()
                finish_reason = ev.get("finish_reason") or "stop"
                frames = int(ev.get("frames_received") or 0)
        return ChatResponse(
            content="".join(content) or None,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
            model=self._server_model_id() or self._model_name,
            thinking="".join(thinking) or None,
            frames_received=frames,
        )
