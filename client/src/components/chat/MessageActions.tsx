/** MessageActions（plan-282-1416 重构）：消息级操作行的归属与显隐协议。
 *
 * 本轮修复两个问题的核心：
 *  - 问题2：操作行不再依赖「全局 isRunning」推断——宿主（TurnGroup / MessageFlow）
 *    按**本 turn 的行状态**决定是否渲染，异常中断 / 手动停止 / 回滚后一律可见；
 *  - 问题3：归属显式化——`scope` 直接声明这行按钮属于谁（用户消息 / AI 回复块），
 *    不再用「turn 内是否存在非 user 项」反推，避免 AI 与用户消息共用一个整体。
 *
 * 归属规则：
 *  - scope="user"：复制 + 回滚（回滚需宿主传 onRollback 且该轮未回滚）；
 *  - scope="ai"  ：复制 + 赞 / 踩 / 重试（仅出现在 AI 回复块内部末尾）；
 *  - scope="full"：仅复制（兼容旧调用与子代理「copy-only」场景）。
 *
 * 显隐：行高固定 24px，仅 opacity 切换 → 显隐零布局位移；
 * `alwaysVisible`（最近一条用户消息）常显，其余 hover / 键盘聚焦显示。
 */
import { memo, useState } from "react";
import { useChatStore } from "../../store/chat";
import { IconCopy, IconRotateCcw, IconThumbsUp, IconThumbsDown, IconRefresh, IconCheck } from "../icons";
import { Tooltip } from "../ui";
import type { TimelineEntry } from "./timeline";
import { turnPartToPlainText, turnToPlainText } from "./markdown";

export const MessageActions = memo(function MessageActions({ entry, onRollback, scope = "full", actions = "full", alwaysVisible = false, ownerId }: {
  entry: TimelineEntry;
  onRollback?: () => void;
  /** user=仅用户消息按钮（复制/回滚）；ai=仅 AI 回复按钮（复制+赞踩重试） */
  scope?: "full" | "user" | "ai";
  /** 操作能力开关——full=完整；copy-only=仅复制；none=无操作行 */
  actions?: "full" | "copy-only" | "none";
  /** plan-282-1416: 常显（最近一条用户消息——聚焦后立即可见，无需 hover） */
  alwaysVisible?: boolean;
  /** plan-282-1416: 归属锚点（消息 id），用于多归属块共存时的 React key 与去重 */
  ownerId?: number;
}) {
  const [copied, setCopied] = useState(false);
  const [feedback, setFeedback] = useState<"up" | "down" | null>(null);
  const sendTurn = useChatStore((s) => s.sendTurn);

  const copy = async () => {
    // 复制按归属拆分——用户消息按钮只复制用户内容，AI 按钮只复制 AI 回复
    const content = scope === "user"
      ? turnPartToPlainText(entry, "user")
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
  const copyOnly = actions === "copy-only";

  const cls = [
    "msg-actions",
    `scope-${scope}`,
    alwaysVisible ? "is-latest" : "",
  ].filter(Boolean).join(" ");

  return (
    <div className={cls} data-owner={ownerId}>
      <Tooltip title="复制" side="top">
        <button className={`msg-action${copied ? " active" : ""}`} aria-label="复制" onClick={copy}>
          {copied ? <IconCheck size={13} /> : <IconCopy size={13} />}
        </button>
      </Tooltip>
      {!copyOnly && scope === "user" && onRollback && (
        <Tooltip title="回滚此消息及其后的更改" side="top">
          <button className="msg-action danger" aria-label="回滚此消息及其后的更改" onClick={onRollback}>
            <IconRotateCcw size={13} />
          </button>
        </Tooltip>
      )}
      {!copyOnly && scope === "ai" && (
        <>
          <Tooltip title="赞" side="top">
            <button
              className={`msg-action msg-action-hover${feedback === "up" ? " active" : ""}`}
              aria-label="赞"
              onClick={() => setFeedback(feedback === "up" ? null : "up")}
            >
              <IconThumbsUp size={13} />
            </button>
          </Tooltip>
          <Tooltip title="踩" side="top">
            <button
              className={`msg-action msg-action-hover${feedback === "down" ? " active" : ""}`}
              aria-label="踩"
              onClick={() => setFeedback(feedback === "down" ? null : "down")}
            >
              <IconThumbsDown size={13} />
            </button>
          </Tooltip>
          <Tooltip title="重新生成此回复" side="top">
            <button className="msg-action msg-action-hover" aria-label="重新生成此回复" onClick={handleRetry}>
              <IconRefresh size={13} />
            </button>
          </Tooltip>
        </>
      )}
    </div>
  );
});
