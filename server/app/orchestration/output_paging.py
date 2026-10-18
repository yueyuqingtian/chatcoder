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
