import { useSyncExternalStore } from "react";
import { api, type MessageOut, type MessagePage } from "../api/client";

interface WindowState {
  before: number | null; after: number | null; revision: number; hasBefore: boolean; loading: boolean; error: string | null;
}
const EMPTY: WindowState = { before: null, after: null, revision: -1, hasBefore: false, loading: false, error: null };
const windows = new Map<string, WindowState>();
const listeners = new Set<() => void>();
const requests = new Map<string, { promise: Promise<MessagePage>; controller: AbortController; sid: number }>();
const keyOf = (sid: number, thread?: number) => `${sid}:${thread ?? "main"}`;
function publish(key: string, state: WindowState) {
  windows.set(key, state);
  for (const notify of listeners) notify();
}
const subscribe = (fn: () => void) => { listeners.add(fn); return () => { listeners.delete(fn); }; };
export function useMessageHistory(sid: number | null, thread?: number): WindowState {
  return useSyncExternalStore(subscribe, () => sid == null ? EMPTY : windows.get(keyOf(sid, thread)) ?? EMPTY);
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
export function invalidateMessageHistory(sid: number) {
  for (const [key, state] of windows) if (key.startsWith(`${sid}:`)) publish(key, { ...state, revision: -1 });
}
function messagesOf(page: MessagePage): MessageOut[] {
  return [...new Map([...page.context, ...page.items].map((m) => [m.id, m])).values()].sort((a, b) => a.id - b.id);
}
function accept(sid: number, thread: number | undefined, page: MessagePage, replace = false, prepend = false) {
  const key = keyOf(sid, thread);
  const old = windows.get(key) ?? EMPTY;
  publish(key, { before: replace ? page.before_id : old.before == null ? page.before_id : Math.min(old.before, page.before_id ?? old.before),
    after: replace ? page.after_id : Math.max(old.after ?? 0, page.after_id ?? 0) || null,
    hasBefore: replace || prepend || old.before == null || (page.before_id != null && page.before_id < old.before) ? page.has_before : old.hasBefore,
    revision: page.revision, loading: false, error: null });
}
export async function initialMessages(sid: number, thread?: number): Promise<MessageOut[]> {
  const page = await request(sid, thread);
  accept(sid, thread, page, true);
  return messagesOf(page);
}
export async function olderMessages(sid: number, thread?: number): Promise<MessageOut[]> {
  const key = keyOf(sid, thread);
  const state = windows.get(key);
  if (!state?.hasBefore || !state.before || state.loading) return [];
  publish(key, { ...state, loading: true, error: null });
  try {
    const page = await request(sid, thread, { before_id: state.before });
    accept(sid, thread, page.revision === state.revision ? page : { ...page, revision: state.revision }, false, true);
    return messagesOf(page);
  } catch (error) {
    publish(key, { ...state, loading: false, error: String(error) });
    throw error;
  }
}
/** plan-20261101-performance：正常切回只取新增；历史修订时校验已加载区间，分页外不判删除。 */
export async function refreshMessageWindow(sid: number, current: MessageOut[], thread?: number): Promise<{ items: MessageOut[]; checkedIds: Set<number> }> {
  const state = windows.get(keyOf(sid, thread));
  if (!state || current.length === 0) return { items: await initialMessages(sid, thread), checkedIds: new Set() };
  let page = await request(sid, thread, { after_id: state.after ?? 0 });
  const changed = page.revision !== state.revision;
  const checkedIds = changed ? new Set(current.filter((m) => m.id >= (state.before ?? 1) && m.id < 1e12).map((m) => m.id)) : new Set<number>();
  if (changed) page = await request(sid, thread, { after_id: Math.max(0, (state.before ?? 1) - 1) });
  let items = messagesOf(page);
  let latest = page;
  while (latest.has_after && latest.after_id != null) {
    latest = await request(sid, thread, { after_id: latest.after_id });
    items = items.concat(messagesOf(latest));
  }
  accept(sid, thread, { ...page, after_id: latest.after_id ?? state.after, has_after: false });
  return { items, checkedIds };
}
export async function aroundMessages(sid: number, id: number, thread?: number): Promise<MessageOut[]> {
  const page = await request(sid, thread, { around_id: id });
  // 定位页不改变连续历史窗口的游标，避免跳过中间未加载历史。
  return messagesOf(page);
}
export async function aroundTurnMessages(sid: number, id: number, thread?: number): Promise<MessageOut[]> {
  return messagesOf(await request(sid, thread, { turn_id: id }));
}
