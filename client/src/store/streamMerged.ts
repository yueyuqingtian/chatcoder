/**
 * 主会话流式「合并文本」派生（plan-334-1661 S3）。
 *
 * ── 问题 ──
 * `streamingBuffers` / `thinkingBuffers` 是「agentId -> 文本」的分桶 map，flush 每次替换 map
 * 引用。StreamingTail 此前在 selector 里 `joinBuffers`：每次订阅者求值都做一遍 O(全文) 拼接
 * 并产生一个大字符串，长回答时每帧要复制几十~几百 KB，还要做内容比较才能判断是否重渲染。
 *
 * ── 现在（维护点唯一） ──
 * 本模块自订阅 store，只要两个 map 的**引用**发生变化就推进合并文本：
 *   · 增量路径：桶键集合与顺序不变、只有最后一个桶增长 ⇒ 只追加差值（O(增量)）；
 *   · 兜底路径：任何非追加变化（落库清空、会话切换、子代理桶迁移、done 覆盖全文、
 *     多桶并发写中间桶）⇒ 重建一次（O(全文)，低频）。
 * 由于走「改了就重算」的单一入口，**不依赖**各 store 写入点逐点同步，不会漏不掉。
 *
 * StreamingTail 通过 `useMergedStreamText` / `useMergedThinkingText` 订阅；
 * 字符串按引用返回，内容没变就是同一个引用 ⇒ 不触发重渲染。
 */

import { useSyncExternalStore } from "react";
import { useChatStore } from "./chat";

type Buckets = Record<number, string>;

let mergedStream = "";
let mergedThinking = "";
let lastStreaming: Buckets | null = null;
let lastThinking: Buckets | null = null;

const listeners = new Set<() => void>();

function emit(): void {
  for (const fn of Array.from(listeners)) {
    try { fn(); } catch { /* 单个订阅者异常不影响其它 */ }
  }
}

/** 全量拼接（兜底路径 / 首次初始化）。 */
function joinBuckets(map: Buckets): string {
  let out = "";
  for (const k in map) out += map[k];
  return out;
}

/**
 * 推进合并文本。
 * 增量条件（全部满足才走 O(增量)）：
 *   1) 桶键集合与顺序一致（数字键的 Object.keys 顺序稳定）；
 *   2) 除最后一个桶外，其余桶的字符串引用完全未变；
 *   3) 最后一个桶的新值以上次值为前缀（流式追加的固有形态）。
 * 任一不满足 ⇒ 重建（覆盖 done 全文替换、清空、中间桶并发写等场景）。
 */
function advance(prev: Buckets, next: Buckets, merged: string): string {
  const prevKeys = Object.keys(prev);
  const nextKeys = Object.keys(next);
  if (prevKeys.length !== nextKeys.length) return joinBuckets(next);
  for (let i = 0; i < nextKeys.length; i++) {
    if (prevKeys[i] !== nextKeys[i]) return joinBuckets(next);
  }
  if (nextKeys.length === 0) return "";
  const lastIdx = nextKeys.length - 1;
  for (let i = 0; i < lastIdx; i++) {
    const k = Number(nextKeys[i]);
    if (prev[k] !== next[k]) return joinBuckets(next);
  }
  const lastKey = Number(nextKeys[lastIdx]);
  const prevLast = prev[lastKey] ?? "";
  const nextLast = next[lastKey] ?? "";
  if (nextLast === prevLast) return merged;
  if (nextLast.startsWith(prevLast)) return merged + nextLast.slice(prevLast.length);
  return joinBuckets(next);
}

// 初始化：以当前 store 快照为准建立基线（模块加载早于任何流式写入）。
{
  const s = useChatStore.getState();
  lastStreaming = s.streamingBuffers;
  lastThinking = s.thinkingBuffers;
  mergedStream = joinBuckets(s.streamingBuffers);
  mergedThinking = joinBuckets(s.thinkingBuffers);
}

useChatStore.subscribe((s) => {
  let changed = false;
  if (s.streamingBuffers !== lastStreaming) {
    mergedStream = lastStreaming ? advance(lastStreaming, s.streamingBuffers, mergedStream) : joinBuckets(s.streamingBuffers);
    lastStreaming = s.streamingBuffers;
    changed = true;
  }
  if (s.thinkingBuffers !== lastThinking) {
    mergedThinking = lastThinking ? advance(lastThinking, s.thinkingBuffers, mergedThinking) : joinBuckets(s.thinkingBuffers);
    lastThinking = s.thinkingBuffers;
    changed = true;
  }
  if (changed) emit();
});

function subscribe(fn: () => void): () => void {
  listeners.add(fn);
  return () => { listeners.delete(fn); };
}

/** 主会话流式正文（所有 agent 桶按序拼接）。 */
export function useMergedStreamText(): string {
  return useSyncExternalStore(subscribe, () => mergedStream, () => mergedStream);
}

/** 主会话流式思考（所有 agent 桶按序拼接）。 */
export function useMergedThinkingText(): string {
  return useSyncExternalStore(subscribe, () => mergedThinking, () => mergedThinking);
}

/** 仅供 dev 自测/回归：合并文本是否与逐桶 join 恒等。 */
export function debugCheckConsistency(): { text: boolean; thinking: boolean } {
  const s = useChatStore.getState();
  return {
    text: mergedStream === joinBuckets(s.streamingBuffers),
    thinking: mergedThinking === joinBuckets(s.thinkingBuffers),
  };
}

// dev 构建挂到全局，便于控制台随时核对增量维护是否与逐桶 join 恒等（生产不挂载 ⇒ 零开销）。
if (import.meta.env.DEV) {
  (window as unknown as { __chatcoderStreamMergedConsistent?: () => unknown })
    .__chatcoderStreamMergedConsistent = debugCheckConsistency;
}
