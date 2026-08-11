/** TurnGroup（v8）：单个 turn 容器。
 * - 思考块按时间顺序穿插在消息与工具调用之间（思考 → 工具 → 思考 → 结果 的 Agent 真实节奏）
 * - 操作行（复制/Markdown/赞/踩/重试/回滚）挂到每条消息下方：
 *   用户消息项靠右、AI 回复项靠左（对应各自消息的展示方向）
 * - 运行中（AI 正在回复）不渲染操作行，避免 hover 浮现显得杂乱
 */
import { useCallback, memo } from "react";
import { ThinkingBlock } from "./ThinkingBlock";
import { ToolTree } from "./ToolTree";
import { ArtifactList } from "./ArtifactList";
import { ReviewCard } from "./ReviewCard";
import { MessageActions } from "./MessageActions";
import { MarkdownContent } from "../MarkdownContent";
import { IconRotateCcw } from "../icons";
import type { TimelineEntry } from "./timeline";
import { msgText } from "./timeline";
import { useChatStore } from "../../store/chat";

/**
 * 性能优化：memo 包裹——entries 由 MessageFlow 按 messages 记忆化，
 * 流式 delta 只更新 streamingBuffers，entry 引用不变，
 * 已完成的历史 turn 在每帧流式刷新时跳过重渲染。
 */
export const TurnGroup = memo(function TurnGroup({ entry, isRunning, rolledBack = false }: {
  entry: Extract<TimelineEntry, { kind: "turn" }>;
  isRunning: boolean;
  /** v12: 该 turn 已回滚：显示已回滚横幅，隐藏回滚入口、产物灰置。 */
  rolledBack?: boolean;
}) {
  const requestRollbackPreview = useChatStore((s) => s.requestRollbackPreview);
  // v7: 按消息时间顺序渲染；timeline 已保证用户消息在最前
  const items = entry.items;
  // 最后一个思考块（运行中时仅它处于"思考中"态）
  let lastThinkingIdx = -1;
  for (let k = items.length - 1; k >= 0; k--) {
    if (items[k].kind === "thinking") { lastThinkingIdx = k; break; }
  }
  const turnId = entry.turnId;
  // v9: 回滚为高风险操作——先展示文件级回滚预览，确认后再执行
  const rollbackFn = useCallback(
    () => { if (turnId != null) requestRollbackPreview(turnId); },
    [turnId, requestRollbackPreview],
  );
  // v12: 已回滚 turn 不再提供回滚入口
  const onRollback = turnId != null && !rolledBack ? rollbackFn : undefined;
  // v10: 整个 AI 回复作为整体——复制按钮只出现在最后一个 text 段下方
  let lastTextIdx = -1;
  for (let k = items.length - 1; k >= 0; k--) {
    if (items[k].kind === "text") { lastTextIdx = k; break; }
  }

  return (
    <div className="turn-group">
      {/* v12: 已回滚横幅（消息已软删，以横幅占位区分「回滚了」与「没执行」） */}
      {rolledBack && (
        <>
          <style>{`
            .turn-rolledback-banner {
              display: flex; align-items: center; gap: 6px;
              padding: 7px 12px; margin: 2px 0 10px 28px;
              border: 1px dashed var(--text-3); border-radius: var(--radius-sm);
              color: var(--text-3); font-size: 12px; background: var(--bg-muted);
            }
          `}</style>
          <div className="turn-rolledback-banner">
            <IconRotateCcw size={12} />
            该轮次已回滚（其改动已撤销，期间消息已清理）
          </div>
        </>
      )}
      {items.map((item, i) => {
        switch (item.kind) {
          case "user":
            return (
              <div key={i} className="turn-item turn-item-user">
                <div className="turn-user-bubble">
                  {msgText(item.msg.content)}
                </div>
                {/* v10: 用户消息按钮放气泡下方（流内、不重叠文字），
                    鼠标聚焦消息时显示复制/回滚；各自归属自己的消息，不会错位 */}
                {!isRunning && (
                  <MessageActions entry={entry} onRollback={onRollback} scope="user" />
                )}
              </div>
            );
          case "thinking":
            return (
              <ThinkingBlock
                key={i}
                text={msgText(item.msg.content)}
                active={isRunning && i === lastThinkingIdx}
                turnId={entry.turnId ?? undefined}
                agentId={item.msg.sender_id ?? undefined}
              />
            );
          case "text":
            return (
              <div key={i} className="turn-item turn-item-text">
                <div className="turn-agent-text">
                  <MarkdownContent>{msgText(item.msg.content)}</MarkdownContent>
                </div>
                {/* v10: 整段回复只显示一次复制按钮（最后一段文本下方），
                    复制内容为整个回复（turnToPlainText） */}
                {!isRunning && i === lastTextIdx && (
                  <MessageActions entry={entry} onRollback={onRollback} scope="ai" />
                )}
              </div>
            );
          case "tools":
            return <ToolTree key={i} nodes={item.nodes} />;
          case "artifacts":
            return <ArtifactList key={i} msgs={item.msgs} turnId={turnId} rolledBack={rolledBack} />;
          case "summary":
            return (
              <div key={i} className="turn-item turn-item-summary">
                <MarkdownContent>{msgText(item.msg.content)}</MarkdownContent>
              </div>
            );
          case "error":
            return (
              <div key={i} className="turn-item turn-item-error">
                {msgText(item.msg.content) || "执行出错"}
              </div>
            );
          default:
            return null;
        }
      })}
      {/* v11: turn 完成后的变更审核卡片（仅在完成且有写盘变更时显示；已回滚 turn 不显示） */}
      {!rolledBack && <ReviewCard turnId={turnId} isRunning={isRunning} />}
    </div>
  );
});
