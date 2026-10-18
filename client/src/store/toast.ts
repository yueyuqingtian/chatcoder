/** 全局提示队列（plan-20261017-ux-overhaul 阶段 A）。
 *
 * 之前只有错误一种提示、只能读 chat.error 单值：连续两条报错后一条会覆盖前一条，
 * 成功 / 警告类反馈也无处可放。改为独立队列 + 四种语气，任何模块都可直接调用 notify.*。
 * chat.error 的 64 处写入点保持不变，由 Toast 组件订阅后转发进队列。
 */
import { create } from "zustand";

export type ToastTone = "info" | "success" | "warning" | "error";

export interface ToastItem {
  id: number;
  tone: ToastTone;
  text: string;
  /** 可选的标题（一行加粗），不传则只显示正文 */
  title?: string;
  /** 停留时长（毫秒）；0 表示不自动关闭 */
  duration: number;
}

/** 同屏最多堆叠条数：再多会遮挡标题栏下方的内容，旧的先出 */
const MAX_VISIBLE = 3;
/** 错误需要读完原因，停留更久；其余为轻反馈 */
const DEFAULT_DURATION: Record<ToastTone, number> = {
  info: 3500, success: 3000, warning: 5000, error: 6000,
};

interface ToastState {
  items: ToastItem[];
  push: (tone: ToastTone, text: string, opts?: { title?: string; duration?: number }) => number;
  dismiss: (id: number) => void;
}

let seq = 0;

export const useToastStore = create<ToastState>((set, get) => ({
  items: [],
  push: (tone, text, opts) => {
    const msg = text.trim();
    if (!msg) return -1;
    // 同语气同文案已在屏上：不重复堆叠（网络抖动时同一报错常被连发）
    const dup = get().items.find((t) => t.tone === tone && t.text === msg);
    if (dup) return dup.id;
    const item: ToastItem = {
      id: ++seq, tone, text: msg, title: opts?.title,
      duration: opts?.duration ?? DEFAULT_DURATION[tone],
    };
    set((s) => ({ items: [...s.items, item].slice(-MAX_VISIBLE) }));
    return item.id;
  },
  dismiss: (id) => set((s) => ({ items: s.items.filter((t) => t.id !== id) })),
}));

type NotifyOpts = { title?: string; duration?: number };

export const notify = {
  info: (text: string, opts?: NotifyOpts) => useToastStore.getState().push("info", text, opts),
  success: (text: string, opts?: NotifyOpts) => useToastStore.getState().push("success", text, opts),
  warning: (text: string, opts?: NotifyOpts) => useToastStore.getState().push("warning", text, opts),
  error: (text: string, opts?: NotifyOpts) => useToastStore.getState().push("error", text, opts),
  dismiss: (id: number) => useToastStore.getState().dismiss(id),
};
