/** TurnGroup（v5）：单个 turn 容器。移除消息操作行。 */
import { ThinkingBlock } from "./ThinkingBlock";
import { ToolTree } from "./ToolTree";
import { ArtifactList } from "./ArtifactList";
import { MarkdownContent } from "../MarkdownContent";
import type { TimelineEntry } from "./timeline";
import { msgText } from "./timeline";

export function TurnGroup({ entry, isRunning }: {
  entry: Extract<TimelineEntry, { kind: "turn" }>;
  isRunning: boolean;
}) {
  let lastThinkingIdx = -1;
  for (let k = entry.items.length - 1; k >= 0; k--) {
    if (entry.items[k].kind === "thinking") { lastThinkingIdx = k; break; }
  }

  return (
    <div className="turn-group">
      {entry.items.map((item, i) => {
        switch (item.kind) {
          case "user":
            return (
              <div key={i} className="turn-item turn-item-user">
                <div className="turn-user-bubble">{msgText(item.msg.content)}</div>
              </div>
            );
          case "thinking":
            return (
              <ThinkingBlock key={i} text={msgText(item.msg.content)} active={isRunning && i === lastThinkingIdx} turnId={entry.turnId ?? undefined} agentId={item.msg.sender_id ?? undefined} />
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
            return <ArtifactList key={i} msgs={item.msgs} />;
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
