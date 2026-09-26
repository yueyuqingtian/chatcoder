/** v20: 子代理详情面板——壳 + 头部，消息体交给共享 message-flow 插件（source="subagent"）。
 * 数据组装（REST 历史 + 实时桶合并去重）/ 加载态 / 滚动 / 流式渲染全部下沉到消息流插件，
 * 与主界面共用同一注册插件（参考 deepseek-harness「同一渲染引擎 + 数据注入」模式）。
 * 头部显示状态文案（执行中/已完成/失败），对齐消息流子代理卡片图标体系。
 */
import { useEffect, useState } from "react";
import { subscribeTick } from "../chat/useRunningTicker";
import { useChatStore } from "../../store/chat";
import { PluginSlot } from "../../plugins/registry";
import { parseUtc } from "../../utils/time";
import { IconCpu, IconSpinner, IconCheck, IconX } from "../icons";

export function SubagentPanel({ threadId, agentName, visible = true }: {
  threadId?: number;
  agentName?: string;
  /** plan-75-334 阶段3：面板是否可见。隐藏时仅暂停秒级计时与流式视觉更新，
   *  线程数据、消息与滚动位置全部保留（恢复可见时补算一次）。 */
  visible?: boolean;
}) {
  const meta = useChatStore((s) => (threadId != null ? s.subagentMeta[threadId] : undefined));
  const liveCount = useChatStore((s) => (threadId != null ? s.subagentMessages[threadId]?.length ?? 0 : 0));
  const running = meta?.status === "running" || meta?.status === "in_progress";
  const failed = meta?.status === "failed" || meta?.status === "cancelled";
  const done = meta?.status === "done";

  // v36 (plan-321-1600 M2): 头部“用时”在运行中需每秒刷新（否则计时不动）。
  // S8c：订阅共享秒级 ticker（与 ToolTree / WorkTimer 共用同一个 setInterval）。
  // plan-75-334 阶段3：面板不可见时不订阅——隐藏标签不再每秒 setState。
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    if (!running || !visible) return;
    setNowMs(Date.now()); // 恢复可见时先对齐一次，再开始秒级刷新
    return subscribeTick((now) => setNowMs(now));
  }, [running, visible]);

  // v36: 用时 = 起止时间差（运行中取当前时间；无起始时间时不展示）
  const startedMs = meta?.startedAt ? parseUtc(meta.startedAt) : null;
  const endedMs = meta?.endedAt ? parseUtc(meta.endedAt) : null;
  let elapsedLabel = "";
  if (startedMs != null) {
    const sec = Math.max(0, Math.round(((endedMs ?? nowMs) - startedMs) / 1000));
    const min = Math.floor(sec / 60);
    elapsedLabel = min > 0 ? `${min} 分 ${sec % 60} 秒` : `${sec} 秒`;
  }

  if (threadId == null) return <div className="subagent-panel-empty">未指定子代理</div>;

  return (
    <div className="subagent-panel">
      <div className="subagent-panel-head">
        <span className="tc-icon"><IconCpu size={13} /></span>
        <span className="subagent-panel-name">{agentName || meta?.name || `子代理 #${threadId}`}</span>
        {running && <span className="tc-status wait"><IconSpinner size={11} /></span>}
        {done && <span className="tc-status ok"><IconCheck size={11} /></span>}
        {failed && <span className="tc-status fail"><IconX size={11} /></span>}
        <span className="subagent-panel-status"
              title={failed && meta?.error ? String(meta.error) : undefined}>
          {running ? "执行中…" : done ? "已完成"
            : failed ? (meta?.error ? `失败：${meta.error}` : "失败")
            : meta?.status ?? ""}
        </span>
        {/* v36 (plan-321-1600 M2): 用时 / 变更文件数 / token 用量 */}
        {elapsedLabel && (
          <span className="subagent-panel-meta">{running ? "已用 " : "用时 "}{elapsedLabel}</span>
        )}
        {meta?.filesCount ? <span className="subagent-panel-meta">{meta.filesCount} 个文件</span> : null}
        {meta?.tokens ? <span className="subagent-panel-meta">{meta.tokens} tokens</span> : null}
        <span className="subagent-panel-count">{liveCount} 条消息</span>
      </div>
      <div className="subagent-panel-body">
        {/* v20: 消息体共享 message-flow 插件（source=subagent，操作仅复制）
            plan-75-334 阶段3：透传 visible——隐藏时不推进流式视觉更新，
            恢复可见时以当前缓冲一次性对齐（历史消息与线程数据不丢）。 */}
        <PluginSlot slot="message-flow" source="subagent" threadId={threadId} visible={visible} className="subagent-flow" />
      </div>
    </div>
  );
}
