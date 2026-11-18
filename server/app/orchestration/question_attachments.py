"""提问附件在模型请求中的图片投影。"""
import logging

from app.models.schemas import ChatMessage
from app.services.attachment_service import run_attachment_io, uploads_root
from app.services.doc_parser import extract_plain_text

logger = logging.getLogger(__name__)


def _project(messages: list[ChatMessage], images: bool, read_tool: bool) -> list[ChatMessage]:
    from app.orchestration.context_manager import _load_inline_image_blocks

    result = []
    pending = []
    for index, message in enumerate(messages):
        result.append(message)
        if message.role != "tool":
            continue
        pending.extend(message.answer_attachments or ())
        # plan-20261118-runtime-question-cache：先发完整工具结果批次，模型才可收到图片。
        if index + 1 < len(messages) and messages[index + 1].role == "tool":
            continue
        if not pending:
            continue
        # 该批次在本条消息列表里已投影过（重试 / 多步复用同一列表）→ 不重复追加；
        # 只跳过**这一批**，后面的新提问附件仍照常投影（此前是整段跳过，导致第二次提问起
        # 图片永久不进上下文，模型只能拿到文件名与路径）。
        if index + 1 < len(messages) and messages[index + 1].context_kind == "question_attachment":
            pending.clear()
            continue
        blocks, notes = _load_inline_image_blocks(pending) if images else ([], [])
        if not images and any(item.get("type") == "image" for item in pending):
            notes.append("当前模型通道以文件引用接收回答图片，无法直接查看图片内容。")
        if not read_tool:
            notes.extend(_document_previews(pending))
        logger.info(
            "[question-attach] 提问附件投影 images=%s 附件=%d 图片块=%d 说明=%d",
            images, len(pending), len(blocks), len(notes),
        )
        if blocks or notes:
            result.append(ChatMessage(
                role="user", content="用户提问回答附带的文件" + ("\n" + "\n".join(notes) if notes else ""),
                content_blocks=blocks or None, context_kind="question_attachment",
            ))
        pending.clear()
    return result


def _document_previews(attachments: list[dict]) -> list[str]:
    documents = [item for item in attachments if item.get("type") in ("document", "spreadsheet", "text")]
    if not documents:
        return []
    root = uploads_root()
    previews = []
    for item in documents[:4]:
        try:
            target = (root / item["path"]).resolve()
            target.relative_to(root)
            text = extract_plain_text(target.name, target.read_bytes())
        except (OSError, ValueError, KeyError):
            text = None
        summary = text[:3000] if text else "本轮无法解析此文件。"
        previews.append(f"第 {item['question_index'] + 1} 题附件「{item['filename']}」节选\n{summary}")
    if len(documents) > 4:
        previews.append("其余文档保留在回答的附件清单中。")
    return previews


async def project_answer_images(
    messages: list[ChatMessage], *, images: bool = True, read_tool: bool = True,
) -> list[ChatMessage]:
    # 幂等由 _project 逐条负责（已投影的批次跳过、新批次照常投影）；
    # 此前这里只要历史里出现过一条投影消息就整段返回，导致第二次提问起图片永远进不了上下文。
    if any(message.answer_attachments for message in messages):
        return await run_attachment_io(_project, messages, images, read_tool)
    return messages
