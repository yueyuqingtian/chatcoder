/** 自动更新全局状态（electron-updater 主进程状态机的渲染侧镜像）。
 * 主进程推送 app:updateStatus；本模块负责订阅 + 动作转发，供侧栏更新按钮
 * 与设置「关于」页共享同一份状态。
 *
 * plan-230-1144 M4.2: 扩展发布内容可见能力——
 * - available/downloaded 状态携带 releaseNotes/releaseDate（主进程透传）；
 * - releaseHistory：按需拉取 GitHub Releases 全量历史（失败回落本地 CHANGELOG）；
 * - pendingWhatsNew：升级后首启"本次更新"弹窗数据（consumeWhatsNew 判定）。 */
import { create } from "zustand";

import { htmlReleaseNotesToMarkdown } from "../utils/releaseNotes";

/** 下载阶段（plan-20261021-usage-update）：点击后先「连接中」，有数据后「下载中」；
 *  断网 / 抖动时「等待网络 / 重试中」（已下载的部分保留，恢复后接着下）；下完后「校验中」 */
export type DownloadPhase = "connecting" | "downloading" | "waiting" | "retrying" | "verifying";

export type UpdateStatus =
  | { state: "idle" }
  | { state: "checking" }
  | { state: "available"; version: string; notes?: string; releaseDate?: string }
  | { state: "none" }
  | {
      state: "downloading"; version?: string; notes?: string; percent: number;
      transferred?: number; total?: number; bytesPerSecond?: number;
      phase?: DownloadPhase; source?: string; attempt?: number;
      /** true = 发现新版本后的静默自动下载（非用户点击触发） */
      auto?: boolean;
    }
  | { state: "downloaded"; version: string; notes?: string; releaseDate?: string }
  | { state: "error"; message: string }
  | { state: "unsupported" };

export interface ReleaseNote {
  version: string;
  name: string;
  date: string;
  notes: string;
  prerelease?: boolean;
}

interface UpdaterStore {
  status: UpdateStatus;
  appVersion: string;
  listening: boolean;
  /** 更新历史（自建源 → GitHub Releases；离线时回落本地 changelog） */
  releaseHistory: ReleaseNote[];
  releaseSource: "mirror" | "github" | "local" | "none" | "";
  /** 升级后首启"本次更新"数据；null=无需展示 */
  pendingWhatsNew: ReleaseNote[] | null;
  /** 发现新版本时自动静默下载（设置「关于」里的开关；默认开，与主进程默认一致） */
  autoDownload: boolean;
  setAutoDownload: (on: boolean) => Promise<void>;
  /** 订阅主进程状态推送（幂等，App 挂载时调用一次） */
  init: () => void;
  checkForUpdates: () => Promise<void>;
  downloadUpdate: () => Promise<void>;
  installUpdate: () => Promise<void>;
  /** 拉取更新历史（AboutPanel 打开时调用，缓存于 store） */
  loadReleaseHistory: (force?: boolean) => Promise<void>;
  /** 升级后首启判定：返回需要展示的"本次更新"列表并置 pendingWhatsNew */
  checkWhatsNew: () => Promise<void>;
  dismissWhatsNew: () => void;
}

export const useUpdaterStore = create<UpdaterStore>((set, get) => ({
  status: { state: "idle" },
  appVersion: "",
  listening: false,
  autoDownload: true,
  releaseHistory: [],
  releaseSource: "",
  pendingWhatsNew: null,
  init: () => {
    if (get().listening) return;
    set({ listening: true });
    const api = window.chatcoderAPI;
    if (!api) return;
    api.onUpdateStatus?.((s) => { if (isUpdateStatus(s)) set({ status: normalizeStatus(s) }); });
    // 主进程可能在订阅前已推送（如启动检查完成），拉一次当前状态兜底
    void api.getUpdateState?.().then((s) => { if (isUpdateStatus(s)) set({ status: normalizeStatus(s) }); });
    void api.getAppVersion?.().then((v) => set({ appVersion: v || "" }));
    void api.getUpdateSettings?.().then((s) => { if (s && typeof s.autoDownload === "boolean") set({ autoDownload: s.autoDownload }); }).catch(() => { /* 保持默认 */ });
    // plan-230-1144 M4.2: 升级后首启判定（异步，不阻塞初始化）
    void get().checkWhatsNew();
  },
  checkForUpdates: async () => {
    const api = window.chatcoderAPI;
    if (!api) return;
    try {
      set({ status: { state: "checking" } });
      const r = await api.checkForUpdates?.();
      if (isUpdateStatus(r)) set({ status: normalizeStatus(r) });
    } catch { /* 主进程会推送 error 状态 */ }
  },
  downloadUpdate: async () => {
    const api = window.chatcoderAPI;
    if (!api) return;
    // plan-20261021-usage-update：点击即进入「连接中」，不等主进程的第一个进度事件
    // （此前要等连上服务器、拿到第一个数据块才有变化，点了没反应的体感长达数秒）
    const before = get().status;
    if (before.state === "available") {
      set({ status: { state: "downloading", version: before.version, notes: before.notes, percent: 0, phase: "connecting" } });
    }
    try {
      const r = await api.downloadUpdate?.();
      if (isUpdateStatus(r)) set({ status: normalizeStatus(r) });
    } catch {
      // IPC 失败：还原成点击前的状态，让用户可以再点
      if (get().status.state === "downloading" && before.state === "available") set({ status: before });
    }
  },
  setAutoDownload: async (on) => {
    const prev = get().autoDownload;
    set({ autoDownload: on }); // 修改即保存：先落到界面，失败再还原
    try {
      const s = await window.chatcoderAPI?.setUpdateSettings?.({ autoDownload: on });
      if (s && typeof s.autoDownload === "boolean") set({ autoDownload: s.autoDownload });
    } catch {
      set({ autoDownload: prev });
    }
  },
  installUpdate: async () => {
    await window.chatcoderAPI?.installUpdate?.();
  },
  loadReleaseHistory: async (force = false) => {
    const api = window.chatcoderAPI;
    if (!api?.getReleaseNotes) return;
    if (!force && get().releaseHistory.length > 0) return;  // 已缓存
    try {
      const r = await api.getReleaseNotes({ limit: 20 });
      if (r?.ok) {
        set({ releaseHistory: normalizeReleases(r.releases || []), releaseSource: r.source });
      } else {
        set({ releaseHistory: [], releaseSource: r?.source || "none" });
      }
    } catch { set({ releaseSource: "none" }); }
  },
  checkWhatsNew: async () => {
    const api = window.chatcoderAPI;
    if (!api?.consumeWhatsNew) return;
    try {
      const verdict = await api.consumeWhatsNew();
      if (!verdict?.show) return;
      const r = await api.getWhatsNew?.({ from: verdict.from, to: verdict.version });
      const releases = normalizeReleases(r?.releases || []);
      if (r?.ok && releases.length > 0) {
        set({ pendingWhatsNew: releases });
      } else if (get().status.state === "downloaded" || get().status.state === "available") {
        // 离线回落：至少展示当前版本的状态机携带的 notes
        const st = get().status as { version?: string; notes?: string; releaseDate?: string };
        if (st.notes) {
          set({ pendingWhatsNew: [{
            version: st.version || verdict.version,
            name: "", date: st.releaseDate || "",
            notes: htmlReleaseNotesToMarkdown(st.notes),
          }] });
        }
      }
    } catch { /* ignore */ }
  },
  dismissWhatsNew: () => set({ pendingWhatsNew: null }),
}));

function isUpdateStatus(v: unknown): v is UpdateStatus {
  return !!v && typeof v === "object" && "state" in v;
}

/** 状态机里的 notes 可能是 HTML（electron-builder 写入 latest.yml 的 releaseNotes），
 * 统一转成 Markdown 后再入库，保证侧栏浮窗 / 设置页 / 首启弹窗排版一致。 */
function normalizeStatus(s: UpdateStatus): UpdateStatus {
  if ((s.state === "available" || s.state === "downloading" || s.state === "downloaded") && s.notes) {
    return { ...s, notes: htmlReleaseNotesToMarkdown(s.notes) };
  }
  return s;
}

function normalizeReleases(list: ReleaseNote[]): ReleaseNote[] {
  return list.map((r) => ({ ...r, notes: htmlReleaseNotesToMarkdown(r.notes || "") }));
}
