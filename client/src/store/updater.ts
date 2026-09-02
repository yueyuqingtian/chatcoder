/** 自动更新全局状态（electron-updater 主进程状态机的渲染侧镜像）。
 * 主进程推送 app:updateStatus；本模块负责订阅 + 动作转发，供侧栏更新按钮
 * 与设置「关于」页共享同一份状态。 */
import { create } from "zustand";

export type UpdateStatus =
  | { state: "idle" }
  | { state: "checking" }
  | { state: "available"; version: string }
  | { state: "none" }
  | { state: "downloading"; version?: string; percent: number; transferred?: number; total?: number }
  | { state: "downloaded"; version: string }
  | { state: "error"; message: string }
  | { state: "unsupported" };

interface UpdaterStore {
  status: UpdateStatus;
  appVersion: string;
  listening: boolean;
  /** 订阅主进程状态推送（幂等，App 挂载时调用一次） */
  init: () => void;
  checkForUpdates: () => Promise<void>;
  downloadUpdate: () => Promise<void>;
  installUpdate: () => Promise<void>;
}

export const useUpdaterStore = create<UpdaterStore>((set, get) => ({
  status: { state: "idle" },
  appVersion: "",
  listening: false,
  init: () => {
    if (get().listening) return;
    set({ listening: true });
    const api = window.chatcoderAPI;
    if (!api) return;
    api.onUpdateStatus?.((s) => { if (isUpdateStatus(s)) set({ status: s }); });
    // 主进程可能在订阅前已推送（如启动检查完成），拉一次当前状态兜底
    void api.getUpdateState?.().then((s) => { if (isUpdateStatus(s)) set({ status: s }); });
    void api.getAppVersion?.().then((v) => set({ appVersion: v || "" }));
  },
  checkForUpdates: async () => {
    const api = window.chatcoderAPI;
    if (!api) return;
    try {
      set({ status: { state: "checking" } });
      const r = await api.checkForUpdates?.();
      if (isUpdateStatus(r)) set({ status: r });
    } catch { /* 主进程会推送 error 状态 */ }
  },
  downloadUpdate: async () => {
    const api = window.chatcoderAPI;
    if (!api) return;
    try {
      const r = await api.downloadUpdate?.();
      if (isUpdateStatus(r)) set({ status: r });
    } catch { /* ignore */ }
  },
  installUpdate: async () => {
    await window.chatcoderAPI?.installUpdate?.();
  },
}));

function isUpdateStatus(v: unknown): v is UpdateStatus {
  return !!v && typeof v === "object" && "state" in v;
}
