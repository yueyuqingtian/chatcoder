/** v2.2 (对齐 zcode 3.13): 子代理详情面板——按 thread_id 展示子代理完整消息流。 */
import { useEffect, useState } from "react";
import { api, type MessageOut } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { MarkdownContent } from "../MarkdownContent";

export function SubagentPanel({ threadId, agentName }: { threadId?: number; agentName?: string }) {
  const currentSessionId = useChatStore((s) => s.currentSessionId);
  const [messages, setMessages] = useState<MessageOut[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!currentSessionId || threadId == null) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    (async () => {
      try {
        const msgs = await api.listSessionMessages(currentSessionId, threadId);
        if (!cancelled) setMessages(msgs);
      } catch (e) {
        if (!cancelled) setError(String(e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [currentSessionId, threadId]);

  if (threadId == null) return <div className="subagent-panel-empty">未指定子代理</div>;
  if (loading) return <div className="subagent-panel-empty">加载子代理消息流…</div>;
  if (error) return <div className="subagent-panel-empty">加载失败：{error}</div>;
  if (messages.length === 0) return <div className="subagent-panel-empty">该子代理暂无消息</div>;

  return (
    <div className="subagent-panel">
      <div className="subagent-panel-head">
        <span className="subagent-panel-name">{agentName || `子代理 #${threadId}`}</span>
        <span className="subagent-panel-count">{messages.length} 条消息</span>
      </div>
      <div className="subagent-panel-body">
        {messages.map((m) => {
          const c = m.content as Record<string, unknown>;
          if (m.msg_type === "thinking") {
            return (
              <div key={m.id} className="sa-item sa-thinking">
                <div className="sa-label">思考</div>
                <pre className="sa-thinking-text">{String(c.text ?? "")}</pre>
              </div>
            );
          }
          if (m.msg_type === "tool_call") {
            const args = typeof c.args === "object" ? JSON.stringify(c.args).slice(0, 200) : "";
            return (
              <div key={m.id} className="sa-item sa-toolcall">
                <div className="sa-label">工具调用</div>
                <div className="sa-tool-name">{String(c.tool ?? "")}</div>
                {args && <pre className="sa-args">{args}</pre>}
              </div>
            );
          }
          if (m.msg_type === "tool_result") {
            return (
              <div key={m.id} className="sa-item sa-toolresult">
                <div className="sa-label">结果 {c.ok === false ? "（失败）" : ""}</div>
                <pre className="sa-output">{(typeof c.output === "string" ? c.output : c.error ? String(c.error) : "").slice(0, 2000)}</pre>
              </div>
            );
          }
          if (m.msg_type === "error") {
            return (
              <div key={m.id} className="sa-item sa-error">
                <div className="sa-label">错误</div>
                <div className="sa-error-text">{String(c.text ?? "")}</div>
              </div>
            );
          }
          const text = typeof c.text === "string" ? c.text : "";
          if (!text) return null;
          return (
            <div key={m.id} className={`sa-item ${m.sender_type === "user" ? "sa-user" : "sa-agent"}`}>
              <div className="sa-label">{m.sender_type === "user" ? "用户" : (c.agent_name ? String(c.agent_name) : "AI")}</div>
              <div className="sa-text"><MarkdownContent>{text}</MarkdownContent></div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
