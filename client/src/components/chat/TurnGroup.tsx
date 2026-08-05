/** TurnGroup（v8）：单个 turn 容器。
 * - 思考块按时间顺序穿插在消息与工具调用之间（思考 → 工具 → 思考 → 结果 的 Agent 真实节奏）
 * - 操作行（复制/Markdown/赞/踩/重试/回滚）挂到每条消息下方：
 *   用户消息项靠右、AI 回复项靠左（对应各自消息的展示方向）
 * - 运行中（AI 正在回复）不渲染操作行，避免 hover 浮现显得杂乱
 */
import { ThinkingBlock } from "./ThinkingBlock";
import { ToolTree } from "./ToolTree";
import { ArtifactList } from "./ArtifactList";
import { MessageActions } from "./MessageActions";
import { MarkdownContent } from "../MarkdownContent";
import type { TimelineEntry } from "./timeline";
import { msgText } from "./timeline";
import { useChatStore } from "../../store/chat";

export function TurnGroup({ entry, isRunning }: {
  entry: Extract<TimelineEntry, { kind: "turn" }>;
  isRunning: boolean;
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
  const onRollback = turnId != null ? () => requestRollbackPreview(turnId) : undefined;

  return (
    <div className="turn-group">
      {items.map((item, i) => {
        switch (item.kind) {
          case "user":
            return (
              <div key={i} className="turn-item turn-item-user">
                {/* v9: 操作按钮内联进消息气泡内部（右上角），
                    避免连续两条消息时操作行错位/重叠 */}
                <div className="turn-user-bubble">
                  {msgText(item.msg.content)}
                  {!isRunning && (
                    <MessageActions entry={entry} onRollback={onRollback} scope="user" />
                  )}
                </div>
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
                {!isRunning && (
                  <MessageActions entry={entry} onRollback={onRollback} scope="ai" />
                )}
              </div>
            );
          case "tools":
            return <ToolTree key={i} nodes={item.nodes} />;
          case "artifacts":
            return <ArtifactList key={i} msgs={item.msgs} turnId={turnId} />;
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
    </div>
  );
}
