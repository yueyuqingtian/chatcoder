/** 消息流里被用户关掉的报错（plan-20261025-shell 需求 9：消息流所有报错都支持叉掉）。
 *
 * 轮次内的报错来自数据库里的 ERROR 消息，刷新 / 重进会话后会重新渲染，所以「关掉」要记下来：
 * 以「消息 id + 正文开头」为键存在本机——SQLite 主键可能被复用，带上正文避免新报错恰好同 id 时被误藏。
 * 只保留最近 400 条，存储不可用时只在本次运行内生效。
 */
import { create } from "zustand";

const STORAGE_KEY = "chatcoder.dismissed-errors";
const MAX_KEYS = 400;

function load(): string[] {
  try {
    const raw = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
    return Array.isArray(raw) ? raw.filter((x): x is string => typeof x === "string") : [];
  } catch {
    return [];
  }
}

/** 报错的身份键：消息 id + 正文前 32 个字符 */
export function errorKey(msgId: number | string, text: string): string {
  return `${msgId}:${text.trim().slice(0, 32)}`;
}

interface DismissedErrorsState {
  keys: ReadonlySet<string>;
  dismiss: (key: string) => void;
}

export const useDismissedErrors = create<DismissedErrorsState>((set, get) => ({
  keys: new Set(load()),
  dismiss: (key) => {
    if (get().keys.has(key)) return;
    const list = [...get().keys, key].slice(-MAX_KEYS);
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(list)); } catch { /* 存储不可用时只在本次运行内生效 */ }
    set({ keys: new Set(list) });
  },
}));
