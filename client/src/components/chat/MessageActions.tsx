/** MessageActions（plan-282-1416 重构）：消息级操作行的归属与显隐协议。
 *
 * scope 显式区分用户消息和 AI 回复，避免两者共享复制内容或操作行。
 *
 * 归属规则：
 *  - scope="user"：复制 + 回滚（回滚需宿主传 onRollback 且该轮未回滚）；
 *  - scope="ai"  ：复制 + 赞 / 踩 / 重试（仅出现在 AI 回复块内部末尾）；
 *  - scope="full"：仅复制（兼容旧调用与子代理「copy-only」场景）。
 *
 * plan-20261107-message-meta-implementation：用户时间与操作统一 hover / 键盘聚焦显示，
 * AI 由宿主确认整轮结束后常显；预留行高以免显隐推挤消息。
 */
import { memo, useState } from "react";
import { useChatStore } from "../../store/chat";
import { IconCheck } from "../icons";
import { IxCopy, IxRollback, IxThumbUp, IxThumbDown, IxRegenerate } from "../iconkit/StackIcons";
import { Button, Tooltip } from "../ui";
import { formatMessageTime } from "../../utils/time";
import type { TimelineEntry } from "./timeline";
import { turnPartToPlainText, turnToPlainText } from "./markdown";

export const MessageActions = memo(function MessageActions({ entry, onRollback, scope = "full", actions = "full", timestampMs = 0, ownerId }: {
  entry: TimelineEntry;
  onRollback?: () => void;
  /** user=仅用户消息按钮（复制/回滚）；ai=仅 AI 回复按钮（复制+赞踩重试） */
  scope?: "full" | "user" | "ai";
  /** 操作能力开关——full=完整；copy-only=仅复制；none=无操作行 */
  actions?: "full" | "copy-only" | "none";
  /** 归属消息发送或整轮结束时刻，缺失时不补造时间。 */
  timestampMs?: number;
  /** plan-282-1416: 归属锚点（消息 id），用于多归属块共存时的 React key 与去重 */
  ownerId?: number;
}) {
  const [copied, setCopied] = useState(false);
  const [feedback, setFeedback] = useState<"up" | "down" | null>(null);
  const sendTurn = useChatStore((s) => s.sendTurn);

  const copy = async () => {
    // 复制按归属拆分——用户消息按钮只复制用户内容，AI 按钮只复制 AI 回复
    // plan-20261107-message-meta-implementation：中途追加消息按 ID 复制，避免复制到同轮首条输入。
    const userItem = scope === "user" && entry.kind === "turn"
      ? entry.items.find((item) => item.kind === "user" && item.msg.id === ownerId)
      : undefined;
    const content = scope === "user"
      ? entry.kind === "turn" && userItem?.kind === "user"
        ? turnPartToPlainText({ ...entry, items: [userItem] }, "user")
        : turnPartToPlainText(entry, "user")
      : scope === "ai"
        ? turnPartToPlainText(entry, "ai")
        : turnToPlainText(entry);
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

  if (actions === "none") return null;
  const inherited = entry.kind === "turn" && entry.items.some((it) => "msg" in it && it.msg.content?.fork_history === true);
  const copyOnly = actions === "copy-only";

  const cls = `msg-actions scope-${scope}`;
  const timeLabel = formatMessageTime(timestampMs);
  const time = timeLabel ? (
    <time className="msg-timestamp" dateTime={new Date(timestampMs).toISOString()}>{timeLabel}</time>
  ) : null;

  return (
    <div className={cls} data-owner={ownerId}>
      {scope === "user" && time}
      <div className="msg-action-buttons">
        <Tooltip title="复制" side="top">
          <Button size="xs" iconOnly className={`msg-action${copied ? " active" : ""}`} aria-label="复制" onClick={copy}
            icon={copied ? <IconCheck size={13} /> : <IxCopy size={14} />} />
        </Tooltip>
        {!copyOnly && scope === "user" && (onRollback || inherited) && (
          <Tooltip title="撤销该轮及之后的对话与改动" side="top">
            <Button size="xs" iconOnly className="msg-action danger" aria-label="撤销该轮及之后的对话与改动" disabled={inherited} onClick={inherited ? undefined : onRollback} icon={<IxRollback size={14} />} />
          </Tooltip>
        )}
        {!copyOnly && scope === "ai" && (
          <>
            <Tooltip title="赞" side="top">
              <Button size="xs" iconOnly
                className={`msg-action${feedback === "up" ? " active" : ""}`}
                aria-label="赞"
                aria-pressed={feedback === "up"}
                onClick={() => setFeedback(feedback === "up" ? null : "up")}
                icon={<IxThumbUp size={14} />} />
            </Tooltip>
            <Tooltip title="踩" side="top">
              <Button size="xs" iconOnly
                className={`msg-action${feedback === "down" ? " active" : ""}`}
                aria-label="踩"
                aria-pressed={feedback === "down"}
                onClick={() => setFeedback(feedback === "down" ? null : "down")}
                icon={<IxThumbDown size={14} />} />
            </Tooltip>
            <Tooltip title="重新生成此回复" side="top">
              <Button size="xs" iconOnly className="msg-action" aria-label="重新生成此回复" onClick={handleRetry} icon={<IxRegenerate size={14} />} />
            </Tooltip>
          </>
        )}
      </div>
      {scope !== "user" && time}
    </div>
  );
});
