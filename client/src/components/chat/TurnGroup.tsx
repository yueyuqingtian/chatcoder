/** TurnGroup（v7）：单个 turn 容器。
 * - 思考块按时间顺序穿插在消息与工具调用之间（思考 → 工具 → 思考 → 结果 的 Agent 真实节奏）
 * - 操作行（复制/Markdown/赞/踩/重试/回滚）收敛到 turn 末尾，以整个 turn（用户消息 + AI 完整回复）为单位，
 *   不再在每个消息片段中间重复出现复制按钮
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
  const rollbackTurn = useChatStore((s) => s.rollbackTurn);
  // v7: 按消息时间顺序渲染；timeline 已保证用户消息在最前
  const items = entry.items;
  // 最后一个思考块（运行中时仅它处于"思考中"态）
  let lastThinkingIdx = -1;
  for (let k = items.length - 1; k >= 0; k--) {
    if (items[k].kind === "thinking") { lastThinkingIdx = k; break; }
  }
  const turnId = entry.turnId;
  const onRollback = turnId != null ? () => rollbackTurn(turnId) : undefined;

  return (
    <div className="turn-group">
      {items.map((item, i) => {
        switch (item.kind) {
          case "user":
            return (
              <div key={i} className="turn-item turn-item-user">
                <div className="turn-user-bubble">{msgText(item.msg.content)}</div>
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
      {/* turn 级操作行：以整个 turn 为单位复制/反馈 */}
      <div className="turn-actions">
        <MessageActions entry={entry} onRollback={onRollback} />
      </div>
    </div>
  );
}
