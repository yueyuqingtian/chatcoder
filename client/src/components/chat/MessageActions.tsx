/** MessageActions（v5）：turn 级消息操作行。
 * hover 浮现：复制 / Markdown / 👍 / 👎 / 重试（AI 回复）/ 回滚（用户消息）
 *
 * v7: 不再挂在每个消息片段上——操作行收敛到 turn 末尾，
 * 复制/Markdown 以"用户消息 + AI 完整回复"整个 turn 为单位。
 */
import { useState } from "react";
import { useChatStore } from "../../store/chat";
import { IconCopy, IconMarkdown, IconRotateCcw, IconThumbsUp, IconThumbsDown, IconRefresh } from "../icons";
import type { TimelineEntry } from "./timeline";
import { msgText } from "./timeline";
import { turnToMarkdown, turnToPlainText } from "./markdown";

export function MessageActions({ entry, onRollback }: {
  entry: TimelineEntry;
  onRollback?: () => void;
}) {
  const [copied, setCopied] = useState<"text" | "md" | null>(null);
  const [feedback, setFeedback] = useState<"up" | "down" | null>(null);
  const sendTurn = useChatStore((s) => s.sendTurn);

  // v7: 根据 turn 内容判断——有 AI 回复才显示赞/踩/重试；有用户消息才显示回滚
  const hasAi = entry.kind === "turn" && entry.items.some((it) => it.kind !== "user");
  const hasUser = entry.kind === "turn" && entry.items.some((it) => it.kind === "user");

  const copy = async (mode: "text" | "md") => {
    const content = mode === "md" ? turnToMarkdown(entry) : turnToPlainText(entry);
    try {
      await navigator.clipboard.writeText(content);
      setCopied(mode);
      setTimeout(() => setCopied(null), 1500);
    } catch { /* ignore */ }
  };

  const handleRetry = () => {
    if (entry.kind !== "turn") return;
    const userItem = entry.items.find((it) => it.kind === "user");
    if (!userItem || userItem.kind !== "user") return;
    const content = msgText(userItem.msg.content);
    if (content) sendTurn(content);
  };

  return (
    <div className="msg-actions">
      <button className="msg-action" title="复制整个回合的纯文本" onClick={() => copy("text")}>
        <IconCopy size={12} />
        <span>{copied === "text" ? "已复制" : "复制"}</span>
      </button>
      <button className="msg-action" title="复制整个回合为 Markdown" onClick={() => copy("md")}>
        <IconMarkdown size={12} />
        <span>{copied === "md" ? "已复制" : "Markdown"}</span>
      </button>
      {hasAi && (
        <>
          <button
            className={`msg-action${feedback === "up" ? " active" : ""}`}
            title="赞"
            onClick={() => setFeedback(feedback === "up" ? null : "up")}
          >
            <IconThumbsUp size={12} />
          </button>
          <button
            className={`msg-action${feedback === "down" ? " active" : ""}`}
            title="踩"
            onClick={() => setFeedback(feedback === "down" ? null : "down")}
          >
            <IconThumbsDown size={12} />
          </button>
          <button className="msg-action" title="重新生成此回复" onClick={handleRetry}>
            <IconRefresh size={12} />
            <span>重试</span>
          </button>
        </>
      )}
      {hasUser && onRollback && (
        <button className="msg-action danger" title="回滚此消息及其后的更改" onClick={onRollback}>
          <IconRotateCcw size={12} />
          <span>回滚</span>
        </button>
      )}
      {hasAi && (
        <span className="msg-ai-label">由 AI 生成</span>
      )}
    </div>
  );
}
