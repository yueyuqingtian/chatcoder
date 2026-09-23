/**
 * StreamingTail —— 流式尾部槽位（plan-329-1647 S8 / FlowEngine）。
 *
 * ── 为什么要把流式尾部单独拆出来 ──
 * 流式缓冲（streamingBuffers / thinkingBuffers）在会话运行期间**每帧换新引用**
 * （store/chat.ts 的 rAF flush）。此前由 MainMessageFlow 直接订阅这两个 map 并计算
 * join 后的全文，于是：
 *   · 整个消息流组件每帧重渲染（含虚拟列表 map、每个可见项的包裹层）；
 *   · 每帧做 O(全文) 的 join（长回答时每帧复制几十~几百 KB）。
 * 这就是用户反馈「会话运行期间拖面板/拖窗口更卡」的头号来源：几何动画与它抢同一份帧预算。
 *
 * ── 现在 ──
 * 订阅下沉到本组件：缓冲变化只重渲染「流式尾部」这一个组件，主树与 delta 完全解耦。
 * selector 直接返回 join 后的字符串——字符串按值比较，内容没变就不会触发重渲染。
 *
 * 主会话与子代理共用：传 threadId 时读子代理桶（subagentThinking / subagentStreams）。
 */

import { memo } from "react";
import { useChatStore } from "../../store/chat";
import { StreamingText } from "./StreamingText";

/** 拼接缓冲 map（主会话可能有多个 agent 桶）。 */
function joinBuffers(map: Record<number, string> | undefined): string {
  if (!map) return "";
  let out = "";
  for (const k in map) out += map[k];
  return out;
}

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

export const StreamingTail = memo(function StreamingTail({ source = "main", active, statusLabel, persistedText }: StreamingTailProps) {
  const isSub = typeof source === "number";
  const thinking = useChatStore((s) => (isSub
    ? (s.subagentThinking[source as number] || "")
    : joinBuffers(s.thinkingBuffers)));
  const text = useChatStore((s) => (isSub
    ? (s.subagentStreams[source as number] || "")
    : joinBuffers(s.streamingBuffers)));

  if (!active) return null;
  // plan-31-152 S5-5：流式正文与已落库正文完全一致 ⇒ 隐藏流式尾部，避免重复显示。
  //   仅比较正文（thinking 不参与：它落库为独立的 thinking 消息，不构成"重复的正文"）。
  if (text && persistedText && text.trim() === persistedText.trim()) return null;
  return <StreamingText active={active} thinking={thinking} text={text} statusLabel={statusLabel} />;
});
