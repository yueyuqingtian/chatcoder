/** CompactCard（v30.1）：上下文压缩块卡片——工具调用统计风格。
 *
 * 由 context_compressor 落库的 SUMMARY 消息（content.checkpoint === true）渲染：
 * - 折叠行（对齐 ToolTree 工具调用行）：图标 + 「上下文压缩 #序号」+ 统计
 *   （遮蔽 N 条消息 · 节省 X tokens）+ 触发标签 + 展开箭头；
 * - 展开态：压缩索引信息 + checkpoint 摘要（Markdown）+ 被压缩消息原文列表
 *   （按需从 /sessions/{id}/compactions/{id}/messages 拉取）+ 「还原」按钮
 *   （调用 restore 接口把被压缩消息恢复回上下文构建）。
 * - 被压缩消息本身不隐藏：仍按时间线排序展示；本卡片是压缩块的折叠入口与还原出口。
 */
import { memo, useState } from "react";
import type { MessageOut } from "../../api/client";
import { api } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { MarkdownContent } from "../MarkdownContent";
import { IconArrowToggle, IconCompress, IconRotateCcw } from "../icons";

const OPEN_TAG = "<compacted-summary>";
const CLOSE_TAG = "</compacted-summary>";

/** 从 checkpoint 帧文本中提取摘要正文（剥掉 preamble 与帧标签）。 */
export function extractCheckpointText(raw: string): string {
  if (raw.includes(OPEN_TAG)) {
    const inner = raw.split(OPEN_TAG)[1];
    if (inner) {
      const body = inner.split(CLOSE_TAG)[0] ?? inner;
      return body.trim();
    }
  }
  return raw.trim();
}

/** 被压缩消息 → 单行摘要文本（展开态原文列表用）。 */
function restoredLine(m: MessageOut): { role: string; text: string } {
  const c = m.content as Record<string, unknown>;
  if (m.msg_type === "tool_call") {
    return { role: "工具调用", text: `${String(c.tool ?? "unknown")}(${JSON.stringify(c.args ?? {})})` };
  }
  if (m.msg_type === "tool_result") {
    return { role: "工具结果", text: String(c.output ?? c.error ?? "(无输出)") };
  }
  if (m.msg_type === "thinking") {
    return { role: "思考", text: String(c.text ?? "") };
  }
  const role = m.sender_type === "user" ? "用户" : m.sender_type === "system" ? "系统" : String(c.agent_name ?? "AI");
  return { role, text: String(c.text ?? c.note ?? "(非文本)") };
}

export const CompactCard = memo(function CompactCard({ msg }: { msg: MessageOut }) {
  const c = msg.content as Record<string, unknown>;
  const [open, setOpen] = useState(false);
  const [restoredMsgs, setRestoredMsgs] = useState<MessageOut[] | null>(null);
  const [loadingMsgs, setLoadingMsgs] = useState(false);
  const [restoring, setRestoring] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const sessionId = useChatStore((s) => s.currentSessionId);
  const refreshMessages = useChatStore((s) => s.refreshMessages);

  const inner = extractCheckpointText(String(c.text ?? ""));
  const compactionId = String(c.compaction_id ?? "");
  const index = Number(c.index ?? 0);
  const shadowedIds = Array.isArray(c.shadowed_ids) ? (c.shadowed_ids as number[]) : [];
  const shadowedTokens = Number(c.shadowed_tokens ?? 0);
  const savedTokens = Number(c.saved_tokens ?? 0);
  const restored = c.restored === true;
  const trigger = String(c.trigger ?? "pressure");
  const triggerLabel = trigger === "context-overflow" ? "溢出恢复" : "压力触发";

  const toggle = () => {
    const next = !open;
    setOpen(next);
    // 首次展开时按需拉取被压缩消息原文（还原查看）
    if (next && restoredMsgs == null && compactionId && sessionId != null && !loadingMsgs) {
      setLoadingMsgs(true);
      setError(null);
      api.getCompactedMessages(sessionId, compactionId)
        .then((msgs) => setRestoredMsgs(msgs))
        .catch((e) => setError(String(e)))
        .finally(() => setLoadingMsgs(false));
    }
  };

  const handleRestore = () => {
    if (restoring || compactionId == null || sessionId == null) return;
    setRestoring(true);
    setError(null);
    api.restoreCompaction(sessionId, compactionId)
      .then(() => {
        setOpen(false);
        setRestoredMsgs(null);
        void refreshMessages();
      })
      .catch((e) => setError(String(e)))
      .finally(() => setRestoring(false));
  };

  return (
    <div className={`compact-card${open ? " open" : ""}${restored ? " restored" : ""}`}>
      <button type="button" className="compact-card-head" onClick={toggle}>
        <span className="compact-card-icon"><IconCompress size={12} /></span>
        <span className="compact-card-title">上下文压缩{index > 0 ? ` #${index}` : ""}</span>
        <span className="compact-card-stat">
          {shadowedIds.length > 0 && `${shadowedIds.length} 条消息 · `}
          节省 {savedTokens.toLocaleString()} tokens
        </span>
        <span className="compact-card-trigger">{restored ? "已还原" : triggerLabel}</span>
        <span className="compact-card-chev">
          <IconArrowToggle open={open} size={12} />
        </span>
      </button>
      {open && (
        <div className="compact-card-body">
          <div className="compact-card-meta">
            {shadowedIds.length > 0 && <span>遮蔽 {shadowedIds.length} 条消息（ID {shadowedIds[0]}…{shadowedIds[shadowedIds.length - 1]}）</span>}
            {shadowedTokens > 0 && <span>原占用 {shadowedTokens.toLocaleString()} tokens</span>}
            {index > 0 && <span>AI 可用 compaction_view 按 # {index} 查看压缩前消息</span>}
          </div>
          {!restored && (
            <div className="compact-card-actions">
              <button type="button" className="compact-card-restore-btn" onClick={handleRestore} disabled={restoring}>
                <IconRotateCcw size={11} />
                {restoring ? "还原中…" : "还原被压缩消息"}
              </button>
            </div>
          )}
          <div className="compact-card-subtitle">Checkpoint 摘要</div>
          <MarkdownContent>{inner}</MarkdownContent>
          <div className="compact-card-subtitle">压缩前消息（原文）</div>
          {loadingMsgs ? (
            <div className="compact-card-loading">加载原文…</div>
          ) : error ? (
            <div className="compact-card-loading">{error}</div>
          ) : restoredMsgs == null ? (
            <div className="compact-card-loading">被压缩消息保留在会话中，AI 可按索引查看（compaction_view）。</div>
          ) : restoredMsgs.length === 0 ? (
            <div className="compact-card-loading">该压缩块没有遮蔽消息。</div>
          ) : (
            <div className="compact-restored-list">
              {restoredMsgs.map((m) => {
                const { role, text } = restoredLine(m);
                return (
                  <div key={m.id} className="compact-restored-msg">
                    <span className="compact-restored-role">{role}</span>
                    <span className="compact-restored-text">{text}</span>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}
    </div>
  );
});

/** v30: "压缩中"进度卡——compact.started 后消息流尾部展示，完成后消失。 */
export function CompactingCard({ info }: { info: { usedTokens?: number; contextWindow?: number; ratio?: number } | null }) {
  const ratio = info?.ratio;
  const used = info?.usedTokens;
  const window_ = info?.contextWindow;
  return (
    <div className="compact-card compacting">
      <span className="compact-card-icon"><IconCompress size={12} /></span>
      <span className="compact-card-title">正在压缩上下文…</span>
      {used != null && (
        <span className="compact-card-stat">
          占用 {used.toLocaleString()} tokens{ratio != null ? `（${ratio}%）` : ""}
          {window_ != null ? ` / ${window_.toLocaleString()}` : ""}
        </span>
      )}
      <span className="thinking-block-breath" />
    </div>
  );
}
