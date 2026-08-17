/** 消息序列化工具：将 turn 内容转换为 Markdown / 纯文本（供复制）。 */
import type { TimelineEntry, TurnItem, ToolNode, ToolLeaf } from "./timeline";
import { msgText } from "./timeline";

function toolNodesToMd(nodes: ToolNode[], depth: number): string {
  const indent = "  ".repeat(depth);
  const lines: string[] = [];
  const leafLine = (l: ToolLeaf) => {
    const status = l.ok === null ? "⏳" : l.ok ? "✓" : "✕";
    return `${indent}- \`${l.tool}\` ${status} ${l.args?.path ? `\`${String(l.args.path)}\`` : ""}`;
  };
  for (const n of nodes) {
    if (n.kind === "group") {
      lines.push(`${indent}- **${n.tool}** × ${n.count}`);
      lines.push(toolNodesToMd(n.leaves.map((l) => ({ kind: "leaf", leaf: l })), depth + 1));
    } else if (n.kind === "action-cluster" || n.kind === "write-merged") {
      for (const l of n.leaves) lines.push(leafLine(l));
    } else {
      const l = n.leaf;
      lines.push(leafLine(l));
      if (l.output) lines.push(`${indent}  \`\`\`\n${l.output.slice(0, 2000)}\n${indent}  \`\`\``);
    }
  }
  return lines.join("\n");
}

/** 将单个 turn 条目序列化为 Markdown。 */
export function turnToMarkdown(entry: TimelineEntry): string {
  if (entry.kind === "standalone") {
    return msgText(entry.msg.content);
  }
  const parts: string[] = [];
  for (const item of entry.items) {
    switch (item.kind) {
      case "user":
        parts.push(`**用户**：${msgText(item.msg.content)}`);
        break;
      case "thinking":
        parts.push(`<details><summary>思考</summary>\n\n${msgText(item.msg.content)}\n\n</details>`);
        break;
      case "text":
        parts.push(msgText(item.msg.content));
        break;
      case "tools":
        parts.push(toolNodesToMd(item.nodes, 0));
        break;
      case "artifacts":
        parts.push("**产物**");
        for (const a of item.msgs) {
          const c = a.content as Record<string, unknown>;
          if (typeof c.text === "string") parts.push(c.text);
        }
        break;
      case "summary":
        parts.push(`> ${msgText(item.msg.content)}`);
        break;
      case "error":
        parts.push(`**错误**：${msgText(item.msg.content)}`);
        break;
      default:
        break;
    }
  }
  return parts.filter(Boolean).join("\n\n");
}

/** 将 turn 条目序列化为纯文本。 */
export function turnToPlainText(entry: TimelineEntry): string {
  if (entry.kind === "standalone") {
    return msgText(entry.msg.content);
  }
  const parts: string[] = [];
  for (const item of entry.items) {
    switch (item.kind) {
      case "user":
        parts.push(msgText(item.msg.content));
        break;
      case "thinking":
        parts.push(`[思考] ${msgText(item.msg.content)}`);
        break;
      case "text":
        parts.push(msgText(item.msg.content));
        break;
      case "tools": {
        const walk = (nodes: ToolNode[], d: number): void => {
          for (const n of nodes) {
            if (n.kind === "group") {
              parts.push(`${"  ".repeat(d)}- ${n.tool} × ${n.count}`);
              walk(n.leaves.map((l) => ({ kind: "leaf", leaf: l })), d + 1);
            } else if (n.kind === "action-cluster" || n.kind === "write-merged") {
              for (const l of n.leaves) {
                parts.push(
                  `${"  ".repeat(d)}- [${l.tool}] ${l.ok === null ? "…" : l.ok ? "ok" : "fail"}${l.args?.path ? ` ${String(l.args.path)}` : ""}`,
                );
              }
            } else {
              const l = n.leaf;
              parts.push(
                `${"  ".repeat(d)}- [${l.tool}] ${l.ok === null ? "…" : l.ok ? "ok" : "fail"}${l.args?.path ? ` ${String(l.args.path)}` : ""}`,
              );
            }
          }
        };
        walk(item.nodes, 0);
        break;
      }
      case "artifacts": {
        for (const a of item.msgs) {
          const c = a.content as Record<string, unknown>;
          if (typeof c.text === "string") parts.push(c.text);
        }
        break;
      }
      case "summary":
        parts.push(msgText(item.msg.content));
        break;
      case "error":
        parts.push(`错误: ${msgText(item.msg.content)}`);
        break;
      default:
        break;
    }
  }
  return parts.filter(Boolean).join("\n");
}

/** v19: 按归属序列化 turn（复制用户消息只复制用户内容，复制 AI 回复不含用户消息）。 */
export function turnPartToPlainText(entry: TimelineEntry, part: "user" | "ai"): string {
  if (entry.kind === "standalone") {
    return part === "user" ? msgText(entry.msg.content) : "";
  }
  const parts: string[] = [];
  for (const item of entry.items) {
    const isUser = item.kind === "user";
    if (part === "user" ? !isUser : isUser) continue;
    switch (item.kind) {
      case "user":
      case "text":
      case "summary":
        parts.push(msgText(item.msg.content));
        break;
      case "thinking":
        parts.push(`[思考] ${msgText(item.msg.content)}`);
        break;
      case "tools": {
        const walk = (nodes: ToolNode[], d: number): void => {
          for (const n of nodes) {
            if (n.kind === "group") {
              parts.push(`${"  ".repeat(d)}- ${n.tool} × ${n.count}`);
              walk(n.leaves.map((l) => ({ kind: "leaf", leaf: l })), d + 1);
            } else if (n.kind === "action-cluster" || n.kind === "write-merged") {
              for (const l of n.leaves) {
                parts.push(`${"  ".repeat(d)}- [${l.tool}] ${l.ok === null ? "…" : l.ok ? "ok" : "fail"}${l.args?.path ? ` ${String(l.args.path)}` : ""}`);
              }
            } else {
              const l = n.leaf;
              parts.push(`${"  ".repeat(d)}- [${l.tool}] ${l.ok === null ? "…" : l.ok ? "ok" : "fail"}${l.args?.path ? ` ${String(l.args.path)}` : ""}`);
            }
          }
        };
        walk(item.nodes, 0);
        break;
      }
      case "artifacts": {
        for (const a of item.msgs) {
          const c = a.content as Record<string, unknown>;
          if (typeof c.text === "string") parts.push(c.text);
        }
        break;
      }
      case "error":
        parts.push(`错误: ${msgText(item.msg.content)}`);
        break;
      default:
        break;
    }
  }
  return parts.filter(Boolean).join("\n");
}

export type { TurnItem };
