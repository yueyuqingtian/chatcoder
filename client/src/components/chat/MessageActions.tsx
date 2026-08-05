/** MessageActions（v9）：消息级操作行——无边框 SVG 图标按钮。
 *
 * v9 变更（按用户要求）：
 * - scope="user"：仅「复制」「回滚」两个图标按钮，内联在用户气泡内，鼠标聚焦时显示
 * - scope="ai"  ：默认在回复下方展示「复制」图标按钮；「赞/踩/重试」hover 才显示
 * - 移除带文字的按钮（复制/Markdown/重试/回滚 等文字标签），全部改为纯图标
 */
import { memo, useState } from "react";
import { useChatStore } from "../../store/chat";
import { IconCopy, IconRotateCcw, IconThumbsUp, IconThumbsDown, IconRefresh, IconCheck } from "../icons";
import type { TimelineEntry } from "./timeline";
import { turnToPlainText } from "./markdown";

export const MessageActions = memo(function MessageActions({ entry, onRollback, scope = "full" }: {
  entry: TimelineEntry;
  onRollback?: () => void;
  /** user=仅用户消息按钮（复制/回滚）；ai=仅 AI 回复按钮（复制+赞踩重试） */
  scope?: "full" | "user" | "ai";
}) {
  const [copied, setCopied] = useState(false);
  const [feedback, setFeedback] = useState<"up" | "down" | null>(null);
  const sendTurn = useChatStore((s) => s.sendTurn);

  const hasAi = scope === "user" ? false : (entry.kind === "turn" && entry.items.some((it) => it.kind !== "user"));
  const hasUser = scope === "ai" ? false : (entry.kind === "turn" && entry.items.some((it) => it.kind === "user"));

  const copy = async () => {
    const content = turnToPlainText(entry);
    try {
      await navigator.clipboard.writeText(content);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch { /* ignore */ }
  };

  const handleRetry = () => {
    if (entry.kind !== "turn") return;
    const userItem = entry.items.find((it) => it.kind === "user");
    if (!userItem || userItem.kind !== "user") return;
    const content = userItem.msg.content as Record<string, unknown>;
    const text = typeof content?.text === "string" ? content.text : "";
    if (text) sendTurn(text);
  };

  return (
    <div className={`msg-actions scope-${scope}`}>
      <button className={`msg-action${copied ? " active" : ""}`} title="复制" onClick={copy}>
        {copied ? <IconCheck size={13} /> : <IconCopy size={13} />}
      </button>
      {scope === "user" && hasUser && onRollback && (
        <button className="msg-action danger" title="回滚此消息及其后的更改" onClick={onRollback}>
          <IconRotateCcw size={13} />
        </button>
      )}
      {scope === "ai" && hasAi && (
        <>
          <button
            className={`msg-action msg-action-hover${feedback === "up" ? " active" : ""}`}
            title="赞"
            onClick={() => setFeedback(feedback === "up" ? null : "up")}
          >
            <IconThumbsUp size={13} />
          </button>
          <button
            className={`msg-action msg-action-hover${feedback === "down" ? " active" : ""}`}
            title="踩"
            onClick={() => setFeedback(feedback === "down" ? null : "down")}
          >
            <IconThumbsDown size={13} />
          </button>
          <button className="msg-action msg-action-hover" title="重新生成此回复" onClick={handleRetry}>
            <IconRefresh size={13} />
          </button>
        </>
      )}
    </div>
  );
});
