"""长输出的分段传递与落盘续读（plan-156-620）。

【问题】
工具结果、子会话 / 子代理 / 工作流结论此前各自硬截断：有的标一句「已截断」，有的什么都不标，
被截掉的部分模型再也拿不到，只能凭开头臆测，或反复调用同一工具仍拿到同样的半截。

【口径】
1. **不静默截断**：任何截断都写明总字数、已给范围，以及取下一段的确切调用方式；
2. **可续读**：结论类文本按 offset 分段（page_of + paging_hint）；
   工具原始输出超出上下文预算时整段落盘（spill_text），给出 read_attachment(path, offset) 续读；
3. **调用写法跟随模型**：Ta3 会话里工具名与参数键是伪装名（TaskQuery / taskId …），
   提示里若写真实名，模型会去调用一个不存在的工具——tool_ref 按当前会话自动换算。
"""
from __future__ import annotations

import logging
import re
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# 落盘目录：放在上传目录下，read_attachment 的安全路径校验天然覆盖它
SPILL_DIRNAME = "tool-outputs"
# 文件名里只保留安全字符
_SAFE_TAG_RE = re.compile(r"[^A-Za-z0-9_.-]+")

# 工具原始输出的保留上限：只防内存失控，正常输出都在此之下；
# 超出上下文预算的部分由执行层落盘续读（present_output），工具自身不再按上下文预算硬截断
RAW_OUTPUT_CEILING = 2_000_000


def cap_raw(text: str) -> str:
    """工具原始输出的兜底上限（远大于上下文预算）；超出时写明丢弃了多少。"""
    if not text or len(text) <= RAW_OUTPUT_CEILING:
        return text
    return text[:RAW_OUTPUT_CEILING] + f"\n...（输出共 {len(text)} 字，超过 {RAW_OUTPUT_CEILING} 字的部分未保留）"


# 当前执行上下文是否为 Ta3 会话（agent_loop 解析模型后设置；各 asyncio 任务独立继承）
_TA3_VISIBLE: ContextVar[bool] = ContextVar("output_paging_ta3", default=False)


def set_ta3_visible(flag: bool) -> None:
    """agent_loop 解析出模型后调用：本任务内生成的续读提示使用 Ta3 可见的工具名。"""
    _TA3_VISIBLE.set(bool(flag))


def _fmt_value(value: object) -> str:
    if isinstance(value, str) and value != "{offset}":
        return '"' + value.replace('"', '\\"') + '"'
    return str(value)


def tool_ref(real_name: str, **kwargs: object) -> str:
    """提示里引用的一次工具调用写法，例如 `collect_results(agent_id=7, report_offset={offset})`。

    Ta3 会话下换算为伪装名与伪装参数键（键映射为 None 的参数丢弃）；值为 "{offset}" 时原样保留占位。
    """
    name = real_name
    keymap: dict[str, str | None] = {}
    if _TA3_VISIBLE.get():
        from app.models.providers.ta3_tool_aliases import ARGS_TO_TA3, TO_TA3

        name = TO_TA3.get(real_name, real_name)
        keymap = ARGS_TO_TA3.get(real_name, {})
    parts: list[str] = []
    for key, value in kwargs.items():
        out_key = keymap.get(key, key) if key in keymap else key
        if out_key is None:
            continue
        parts.append(f"{out_key}={_fmt_value(value)}")
    return f"{name}({', '.join(parts)})"


@dataclass(frozen=True)
class TextPage:
    """长文本的一段。"""

    text: str
    offset: int
    total: int

    @property
    def end(self) -> int:
        return self.offset + len(self.text)

    @property
    def complete(self) -> bool:
        return self.end >= self.total


def page_of(text: str, offset: int = 0, size: int = 12000) -> TextPage:
    """取文本从 offset 开始的一段（offset 越界时夹回合法范围）。"""
    full = text or ""
    start = max(0, min(int(offset or 0), len(full)))
    return TextPage(text=full[start:start + max(1, size)], offset=start, total=len(full))


def paging_hint(page: TextPage, next_call: str) -> str:
    """分段提示：读完返回空串；未读完写明总字数、已读范围与下一段的调用方式。

    `next_call` 中的 `{offset}` 替换为下一段起点。
    """
    if page.complete:
        return ""
    call = next_call.replace("{offset}", str(page.end))
    return (
        f"\n\n（全文共 {page.total} 字，本段为第 {page.offset + 1}–{page.end} 字，尚未读完。"
        f"请调用 {call} 继续读取，读完全部再下结论。）"
    )


def spill_text(text: str, *, session_id: int | None, tag: str) -> Path | None:
    """把完整输出写入上传目录下的落盘文件；失败返回 None（调用方退回显式截断提示）。"""
    try:
        from app.core.config import settings

        safe_tag = _SAFE_TAG_RE.sub("-", tag)[:60] or "output"
        # plan-156-621：必须 resolve 成绝对路径。read_attachment 以 resolve 后的 uploads 根做
        # safe_resolve，若这里给相对路径（uploads/...），拼接后会变成 uploads/uploads/... 命中不了，
        # 只能退回「全目录按文件名兜底」——每次都遍历整个 uploads，还可能读到同名的其他会话文件。
        folder = Path(settings.uploads_dir).resolve() / SPILL_DIRNAME / str(session_id or "common")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{safe_tag}-{uuid.uuid4().hex[:8]}.txt"
        # newline="" 关闭换行转换：Windows 文本模式会把 \n 写成 \r\n，
        # 落盘内容变长，提示里的 offset 与读回的内容就对不上了
        path.write_text(text, encoding="utf-8", newline="")
        return path
    except Exception:  # noqa: BLE001 — 落盘失败不影响工具结果本身
        logger.warning("[output] 长输出落盘失败 tag=%s", tag, exc_info=True)
        return None


def present_output(text: str, limit: int, *, session_id: int | None, tag: str) -> str:
    """工具原始输出进入模型上下文前的统一呈现。

    不超出预算原样返回；超出时完整内容落盘，返回开头一段 + 续读方式（read_attachment 按 offset 分段）。
    落盘失败时退回「开头 + 结尾 + 明确的截断说明」，至少不静默丢弃。
    """
    if not text or len(text) <= limit:
        return text
    path = spill_text(text, session_id=session_id, tag=tag)
    head_len = max(1, limit - 400)
    if path is None:
        half = max(1, limit // 2)
        return (
            text[:half]
            + f"\n\n...（输出共 {len(text)} 字，超出上下文预算，中间部分未能保存，已省略）...\n\n"
            + text[-half:]
        )
    call = tool_ref("read_attachment", path=str(path).replace("\\", "/"), offset=head_len)
    return (
        text[:head_len]
        + f"\n\n（输出共 {len(text)} 字，以上为前 {head_len} 字；完整内容已保存，"
        f"如需其余部分请调用 {call} 分段读取。）"
    )


def budget_for_tool(tool_name: str) -> tuple[int, int]:
    """按工具类型解析呈现预算，返回 (字节预算, 尾部保留字节)。

    plan-522-2209：预算口径此前只在 agent_loop 里，历史重建路径拿不到，只能自己写死一个
    字符上限——同一份工具输出在「刚跑完」与「压缩后重建」两种情况下被截成不同长度，
    后者还无法续读。这里收成唯一来源，两条路径都调它。
    """
    from app.core.config import settings

    if tool_name == "fs_grep":
        return settings.tool_output_bytes_grep, 0
    if tool_name in ("web_fetch", "web_search"):
        return settings.tool_output_bytes_web, 0
    if tool_name == "terminal_exec":
        return settings.tool_output_bytes_terminal, settings.tool_output_bytes_terminal_tail
    return settings.tool_output_bytes_default, 0


def present_for_model(
    text: str,
    *,
    tool_name: str,
    session_id: int | None,
    tag: str,
) -> str:
    """工具输出进入模型上下文的统一呈现（实时路径与历史重建路径共用）。

    fs_read 例外：它的窗口已由工具自身的字节预算封顶且完整写入落库，再截一次会出现
    「窗口→截成前段→再读」的循环。其余工具按 `budget_for_tool` 呈现，超出部分落盘续读。
    """
    if tool_name == "fs_read":
        return str(text or "")
    budget, tail = budget_for_tool(tool_name)
    return present_output_bytes(
        str(text or ""), budget, session_id=session_id, tag=tag, tail_bytes=tail,
    )


def _prefix_bytes(text: str, limit: int) -> str:
    """取不超过 limit 个 UTF-8 字节的前缀（不切断多字节字符）。"""
    return text.encode("utf-8")[:max(0, limit)].decode("utf-8", errors="ignore")


def _suffix_bytes(text: str, limit: int) -> str:
    """取不超过 limit 个 UTF-8 字节的后缀（不切断多字节字符）。"""
    raw = text.encode("utf-8")
    return raw[-max(0, limit):].decode("utf-8", errors="ignore") if limit > 0 else ""


def present_output_bytes(
    text: str,
    budget_bytes: int,
    *,
    session_id: int | None,
    tag: str,
    tail_bytes: int = 0,
) -> str:
    """按 UTF-8 字节预算呈现工具输出（字节口径，中文与 ASCII 同等计量）。

    未超预算原样返回；超出时完整内容落盘，返回：
    - 头部（以及 tail_bytes > 0 时的尾部，终端输出的报错多在尾部）；
    - 续读方式（read_attachment 按字符 offset 分段）。
    落盘失败时退回“头 + 省略说明 + 尾”，至少不静默丢弃。
    """
    if not text:
        return text
    raw_len = len(text.encode("utf-8"))
    if raw_len <= budget_bytes:
        return text
    # 续读说明本身是多字节中文 + 路径，需留足余量，保证「头 + 说明 + 尾」不超过预算——
    # 终端工具先呈现一次、agent_loop 再按同一预算呈现一次，超出会被二次截断并改写续读指引
    notice_reserve = 1500
    tail = _suffix_bytes(text, tail_bytes) if tail_bytes > 0 else ""
    head_budget = max(1, budget_bytes - notice_reserve - len(tail.encode("utf-8")))
    head = _prefix_bytes(text, head_budget)
    path = spill_text(text, session_id=session_id, tag=tag)
    if path is None:
        return (
            head
            + f"\n\n...（输出共 {raw_len} 字节，超出上下文预算，中间部分未能保存，已省略）...\n\n"
            + tail
        )
    call = tool_ref("read_attachment", path=str(path).replace("\\", "/"), offset=len(head))
    tail_note = "，尾部保留在下方" if tail else ""
    middle = (
        f"\n\n（输出共 {raw_len} 字节，以上为前 {len(head)} 字符{tail_note}；完整内容已保存，"
        f"如需其余部分请调用 {call} 分段读取。）\n\n"
    )
    return head + middle + tail
