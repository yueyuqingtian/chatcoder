/** v20: 子代理详情面板——壳 + 头部，消息体交给共享 message-flow 插件（source="subagent"）。
 * 数据组装（REST 历史 + 实时桶合并去重）/ 加载态 / 滚动 / 流式渲染全部下沉到消息流插件，
 * 与主界面共用同一注册插件（参考 deepseek-harness「同一渲染引擎 + 数据注入」模式）。
 * 头部显示状态文案（执行中/已完成/失败），对齐消息流子代理卡片图标体系。
 */
import { useChatStore } from "../../store/chat";
import { PluginSlot } from "../../plugins/registry";
import { IconCpu, IconSpinner, IconCheck, IconX } from "../icons";

export function SubagentPanel({ threadId, agentName }: { threadId?: number; agentName?: string }) {
  const meta = useChatStore((s) => (threadId != null ? s.subagentMeta[threadId] : undefined));
  const liveCount = useChatStore((s) => (threadId != null ? s.subagentMessages[threadId]?.length ?? 0 : 0));
  const running = meta?.status === "running" || meta?.status === "in_progress";
  const failed = meta?.status === "failed" || meta?.status === "cancelled";
  const done = meta?.status === "done";

  if (threadId == null) return <div className="subagent-panel-empty">未指定子代理</div>;

  return (
    <div className="subagent-panel">
      <div className="subagent-panel-head">
        <span className="tc-icon"><IconCpu size={13} /></span>
        <span className="subagent-panel-name">{agentName || meta?.name || `子代理 #${threadId}`}</span>
        {running && <span className="tc-status wait"><IconSpinner size={11} /></span>}
        {done && <span className="tc-status ok"><IconCheck size={11} /></span>}
        {failed && <span className="tc-status fail"><IconX size={11} /></span>}
        <span className="subagent-panel-status">
          {running ? "执行中…" : done ? "已完成" : failed ? "失败" : meta?.status ?? ""}
        </span>
        <span className="subagent-panel-count">{liveCount} 条消息</span>
      </div>
      <div className="subagent-panel-body">
        {/* v20: 消息体共享 message-flow 插件（source=subagent，操作仅复制） */}
        <PluginSlot slot="message-flow" source="subagent" threadId={threadId} className="subagent-flow" />
      </div>
    </div>
  );
}
