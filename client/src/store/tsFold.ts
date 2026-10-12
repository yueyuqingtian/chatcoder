/** 任务摘要分区折叠状态（plan-128-518）。
 *  按「会话 + 分区 id」记录是否折叠，写 localStorage，刷新后保持；缺省展开。 */
import { create } from "zustand";

const KEY = "chatcoder.ts-fold";

function load(): Record<string, boolean> {
  try {
    const raw = localStorage.getItem(KEY);
    const obj = raw ? JSON.parse(raw) : {};
    return obj && typeof obj === "object" ? (obj as Record<string, boolean>) : {};
  } catch { return {}; }
}

interface TsFoldState {
  /** key = `${sessionId}:${sectionId}`，值 true = 已折叠 */
  folded: Record<string, boolean>;
  toggle: (key: string) => void;
}

export const useTsFoldStore = create<TsFoldState>((set, get) => ({
  folded: load(),
  toggle: (key) => {
    const next = { ...get().folded, [key]: !get().folded[key] };
    // 展开是默认态，不落库，避免表无限增长
    if (!next[key]) delete next[key];
    try { localStorage.setItem(KEY, JSON.stringify(next)); } catch { /* 隐私模式等：忽略 */ }
    set({ folded: next });
  },
}));
