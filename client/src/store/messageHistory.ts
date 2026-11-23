import { useSyncExternalStore } from "react";
import { api, type MessageOut, type MessagePage } from "../api/client";

interface WindowState {
  before: number | null;
  after: number | null;
  revision: number;
  hasBefore: boolean;
  /** plan-20261123-ux-fixes：是否由跳页（around）在连续窗口之前加载出非连续更早区间。
   *  仅由 aroundMessages / aroundTurnMessages 置位，绝不受分页 page.context 影响。 */
  disjointBefore: boolean;
  disjointMinId: number | null;
  loading: boolean;
  error: string | null;
}
const EMPTY: WindowState = {
  before: null,
  after: null,
  revision: -1,
  hasBefore: false,
  disjointBefore: false,
  disjointMinId: null,
  loading: false,
  error: null,
};
const windows = new Map<string, WindowState>();
const listeners = new Set<() => void>();
const requests = new Map<string, { promise: Promise<MessagePage>; controller: AbortController; sid: number }>();
const keyOf = (sid: number, thread?: number) => `${sid}:${thread ?? "main"}`;
function publish(key: string, state: WindowState) {
  windows.set(key, state);
  for (const notify of listeners) notify();
}
const subscribe = (fn: () => void) => { listeners.add(fn); return () => { listeners.delete(fn); }; };
export function getMessageHistoryState(sid: number | null, thread?: number): WindowState {
  return sid == null ? EMPTY : windows.get(keyOf(sid, thread)) ?? EMPTY;
}
export function useMessageHistory(sid: number | null, thread?: number): WindowState {
  return useSyncExternalStore(subscribe, () => getMessageHistoryState(sid, thread));
}
function request(sid: number, thread: number | undefined, cursor: { before_id?: number; after_id?: number; around_id?: number; turn_id?: number } = {}) {
  const key = keyOf(sid, thread) + JSON.stringify(cursor);
  const existing = requests.get(key);
  if (existing) return existing.promise;
  const controller = new AbortController();
  const promise = api.messagePage(sid, { thread_id: thread, ...cursor }, controller.signal).finally(() => {
    if (requests.get(key)?.controller === controller) requests.delete(key);
  });
  requests.set(key, { promise, controller, sid });
  return promise;
}
export function abortMessageHistory(sid: number) {
  for (const value of requests.values()) if (value.sid === sid) value.controller.abort();
}
/** plan-20261120-message-loss：冷缓存淘汰 / 会话删除 / id 复用时同步清除历史游标，避免旧游标残留导致跳过历史。 */
export function clearMessageHistory(sid: number) {
  abortMessageHistory(sid);
  let removed = false;
  for (const key of Array.from(windows.keys())) {
    if (key.startsWith(`${sid}:`)) {
      windows.delete(key);
      removed = true;
    }
  }
  if (removed) for (const notify of listeners) notify();
}
export function invalidateMessageHistory(sid: number) {
  for (const [key, state] of windows) if (key.startsWith(`${sid}:`)) publish(key, { ...state, revision: -1 });
}
export interface MessageWindowBatch {
  items: MessageOut[];
  commit: () => void;
}
export interface RefreshWindowBatch extends MessageWindowBatch {
  checkedIds: Set<number>;
}
function messagesOf(page: MessagePage): MessageOut[] {
  return [...new Map([...page.context, ...page.items].map((m) => [m.id, m])).values()].sort((a, b) => a.id - b.id);
}
function maxConfirmedMessageId(messages: readonly MessageOut[]): number {
  let maxId = 0;
  for (const m of messages) {
    const id = Number(m.id) || 0;
    if (id > maxId && id < 1e12) maxId = id;
  }
  return maxId;
}
function accept(sid: number, thread: number | undefined, page: MessagePage, replace = false, prepend = false) {
  const key = keyOf(sid, thread);
  const old = windows.get(key) ?? EMPTY;
  const nextBefore = replace ? page.before_id : old.before == null ? page.before_id : Math.min(old.before, page.before_id ?? old.before);
  const nextDisjointMinId = replace
    ? null
    : old.disjointMinId != null && nextBefore != null && nextBefore <= old.disjointMinId
      ? null
      : old.disjointMinId;
  publish(key, {
    before: nextBefore,
    after: replace ? page.after_id : Math.max(old.after ?? 0, page.after_id ?? 0) || null,
    hasBefore: replace || prepend || old.before == null || (page.before_id != null && page.before_id < old.before) ? page.has_before : old.hasBefore,
    disjointBefore: nextDisjointMinId != null && (nextBefore == null || nextDisjointMinId < nextBefore),
    disjointMinId: nextDisjointMinId,
    revision: page.revision,
    loading: false,
    error: null,
  });
}
export async function initialMessages(sid: number, thread?: number): Promise<MessageWindowBatch> {
  const page = await request(sid, thread);
  // plan-20261120-message-loss：延迟到调用方确认仍在该会话并写入 store 后再提交游标，防止切走丢弃消息却提前推进游标。
  return { items: messagesOf(page), commit: () => accept(sid, thread, page, true) };
}
export async function olderMessages(sid: number, thread?: number): Promise<MessageWindowBatch> {
  const key = keyOf(sid, thread);
  const state = windows.get(key);
  if (!state?.hasBefore || !state.before || state.loading) return { items: [], commit: () => {} };
  publish(key, { ...state, loading: true, error: null });
  try {
    const page = await request(sid, thread, { before_id: state.before });
    const latest = windows.get(key);
    if (latest) publish(key, { ...latest, loading: false, error: null });
    const nextPage = page.revision === state.revision ? page : { ...page, revision: state.revision };
    return { items: messagesOf(page), commit: () => accept(sid, thread, nextPage, false, true) };
  } catch (error) {
    const latest = windows.get(key) ?? state;
    const isAbort = error instanceof DOMException && error.name === "AbortError";
    publish(key, { ...latest, loading: false, error: isAbort ? null : String(error) });
    throw error;
  }
}
/** plan-20261101-performance：正常切回只取新增；历史修订时校验已加载区间，分页外不判删除。 */
export async function refreshMessageWindow(sid: number, current: MessageOut[], thread?: number): Promise<RefreshWindowBatch> {
  const state = windows.get(keyOf(sid, thread));
  const maxConfirmed = maxConfirmedMessageId(current);
  // plan-20261120-message-loss：无窗口、无消息、或本地最新消息已完全落在窗口左界之前（游标失配）时，回退首屏重新对齐。
  if (!state || current.length === 0 || (state.before != null && maxConfirmed > 0 && maxConfirmed < state.before)) {
    const batch = await initialMessages(sid, thread);
    return { items: batch.items, checkedIds: new Set(), commit: batch.commit };
  }
  // 增量起点取 state.after 与本地已确认最大 ID 的较小值，避免因游标超前跳过中间已落库消息。
  const afterCursor = state.after != null && maxConfirmed > 0 ? Math.min(state.after, maxConfirmed) : (state.after ?? 0);
  let page = await request(sid, thread, { after_id: afterCursor });
  const changed = page.revision !== state.revision;
  const checkedIds = changed ? new Set(current.filter((m) => m.id >= (state.before ?? 1) && m.id < 1e12).map((m) => m.id)) : new Set<number>();
  if (changed) page = await request(sid, thread, { after_id: Math.max(0, (state.before ?? 1) - 1) });
  let items = messagesOf(page);
  let latest = page;
  while (latest.has_after && latest.after_id != null) {
    latest = await request(sid, thread, { after_id: latest.after_id });
    items = items.concat(messagesOf(latest));
  }
  const mergedPage: MessagePage = { ...page, after_id: latest.after_id ?? state.after, has_after: false };
  return { items, checkedIds, commit: () => accept(sid, thread, mergedPage) };
}
function markDisjointIfOlder(sid: number, thread: number | undefined, page: MessagePage) {
  const key = keyOf(sid, thread);
  const state = windows.get(key);
  if (!state || state.before == null) return;
  const firstId = page.items.reduce<number | null>((min, m) => (m.id > 0 && (min == null || m.id < min) ? m.id : min), null);
  if (firstId == null || firstId >= state.before) return;
  const nextMin = state.disjointMinId == null ? firstId : Math.min(state.disjointMinId, firstId);
  if (state.disjointBefore && state.disjointMinId === nextMin) return;
  publish(key, { ...state, disjointBefore: true, disjointMinId: nextMin });
}
export async function aroundMessages(sid: number, id: number, thread?: number): Promise<MessageOut[]> {
  const page = await request(sid, thread, { around_id: id });
  // 定位页不改变连续历史窗口的游标，避免跳过中间未加载历史；仅记录跳页断层供顶部自动加载守卫识别。
  markDisjointIfOlder(sid, thread, page);
  return messagesOf(page);
}
export async function aroundTurnMessages(sid: number, id: number, thread?: number): Promise<MessageOut[]> {
  const page = await request(sid, thread, { turn_id: id });
  markDisjointIfOlder(sid, thread, page);
  return messagesOf(page);
}
