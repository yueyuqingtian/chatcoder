/**
 * StreamingTail —— 流式尾部槽位（plan-329-1647 S8 / FlowEngine）。
 *
 * ── 为什么要把流式尾部单独拆出来 ──
 * 流式缓冲（streamingBuffers / thinkingBuffers）在会话运行期间**每次 flush 都换新引用**。
 * 此前由 MainMessageFlow 直接订阅这两个 map 并计算 join 后的全文，于是：
 *   · 整个消息流组件每帧重渲染（含虚拟列表 map、每个可见项的包裹层）；
 *   · 每帧做 O(全文) 的 join（长回答时每帧复制几十~几百 KB）。
 * 这就是用户反馈「会话运行期间拖面板/拖窗口更卡」的头号来源：几何动画与它抢同一份帧预算。
 *
 * ── 现在 ──
 * 订阅下沉到本组件；并且（plan-334-1661 S3）主会话不再在订阅者里做 O(全文) 拼接，改为订阅
 * store 侧唯一维护点派生出的合并文本（store/streamMerged：增量 append，非追加变化才重建）。
 * 子代理按 threadId 单桶读取，本就无需跨桶拼接。
 *
 * 主会话与子代理共用：传 threadId 时读子代理桶（subagentThinking / subagentStreams）。
 */

import { memo } from "react";
import { useChatStore } from "../../store/chat";
import { useMergedStreamText, useMergedThinkingText } from "../../store/streamMerged";
import { StreamingText } from "./StreamingText";
import { recordComponentRender } from "../../perf/metrics";

export interface StreamingTailProps {
  /** true = 主会话桶；数字 = 该 threadId 的子代理桶 */
  source?: "main" | number;
  /** 是否处于运行中（不运行则尾部不占槽位） */
  active: boolean;
  /** turn 级瞬态状态提示（如"调用异常，正在重试 1/2…"） */
  statusLabel?: string;
  /** plan-31-152 S5-5：时间线中最后一条已落库正文（见 timeline.lastPersistedText）。
   *  与流式正文完全一致时不再渲染流式正文——后端已落库、缓冲尚未清空，
   *  否则同一条消息会显示两份（用户反馈"偶尔重复显示一条消息，过几秒又变回一条"）。 */
  persistedText?: string;
}

/** 尾部渲染体：不运行不占位；正文与已落库正文一致时隐藏，其余交 StreamingText。 */
function TailBody({ active, statusLabel, persistedText, text, thinking }: {
  active: boolean;
  statusLabel?: string;
  persistedText?: string;
  text: string;
  thinking: string;
}) {
  if (!active) return null;
  if (text && persistedText && text.trim() === persistedText.trim()) return null;
  return <StreamingText active={active} thinking={thinking} text={text} statusLabel={statusLabel} />;
}

/** 主会话尾部：订阅合并文本派生（唯一维护点在 store/streamMerged）。 */
const MainTail = memo(function MainTail({ active, statusLabel, persistedText }: {
  active: boolean;
  statusLabel?: string;
  persistedText?: string;
}) {
  const text = useMergedStreamText();
  const thinking = useMergedThinkingText();
  return (
    <TailBody
      active={active}
      statusLabel={statusLabel}
      persistedText={persistedText}
      text={text}
      thinking={thinking}
    />
  );
});

/** 子代理尾部：直接订阅本线程桶（按 threadId 隔离，无跨桶拼接）。 */
const SubagentTail = memo(function SubagentTail({ threadId, active, statusLabel, persistedText }: {
  threadId: number;
  active: boolean;
  statusLabel?: string;
  persistedText?: string;
}) {
  const text = useChatStore((s) => s.subagentStreams[threadId] || "");
  const thinking = useChatStore((s) => s.subagentThinking[threadId] || "");
  return (
    <TailBody
      active={active}
      statusLabel={statusLabel}
      persistedText={persistedText}
      text={text}
      thinking={thinking}
    />
  );
});

export const StreamingTail = memo(function StreamingTail({ source = "main", active, statusLabel, persistedText }: StreamingTailProps) {
  // plan-75-334 阶段0：记录组件渲染（仅采集期间统计，零开销）
  recordComponentRender("streamingTail");
  
  // plan-334-1661 S3：按数据源分流订阅——主会话不订阅子代理桶、子代理不订阅主会话合并文本，
  // 两边互不牵动重渲染（子代理面板在未就绪时传 -1，按单桶读取自然得到空串）。
  return typeof source === "number"
    ? <SubagentTail threadId={source} active={active} statusLabel={statusLabel} persistedText={persistedText} />
    : <MainTail active={active} statusLabel={statusLabel} persistedText={persistedText} />;
});
