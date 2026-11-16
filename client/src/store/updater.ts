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

export type UpdateTier = "primary" | "mirror" | "github";

export type UpdateStatus =
  | { state: "idle" }
  | { state: "checking" }
  | { state: "available"; version: string; notes?: string; releaseDate?: string }
  | { state: "none" }
  | {
      state: "downloading"; version?: string; notes?: string; percent: number;
      transferred?: number; total?: number; bytesPerSecond?: number;
      phase?: DownloadPhase; source?: string; attempt?: number;
      /** 当前线路层级（plan-20261022-update-route）：自建源 / 镜像 / GitHub（慢速兜底） */
      tier?: UpdateTier;
      /** 换源退避：多少毫秒后尝试 nextLabel 这条线路 */
      retryInMs?: number; nextLabel?: string;
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

/** plan-20261024-update-channel：下载通道 id —— 镜像1 / 镜像2 / GitHub 直连 */
export type UpdateChannelId = "mirror1" | "mirror2" | "github";

/** 通道候选顺序（界面按此顺序渲染卡片，与主进程 CHANNEL_IDS 一致） */
export const UPDATE_CHANNEL_IDS: UpdateChannelId[] = ["mirror1", "mirror2", "github"];

/** 单条通道的测速结果（主进程 update-channels.cjs 的 publicReport 逐字段对应） */
export interface ChannelProbe {
  ok: boolean;
  /** 首字节耗时（毫秒），不含传数据的耗时 */
  ms: number;
  bytesPerSecond: number;
  /** 失败原因（成功时为空串），界面上归并为「不可用」 */
  reason: string;
}

export interface ChannelReport {
  channels: Record<UpdateChannelId, ChannelProbe>;
  /** 测速最快的一条；全部失败时为 null */
  best: UpdateChannelId | null;
  /** 测速时间戳（毫秒）；0 = 还没测过 */
  testedAt: number;
}

/** 通道快照 —— 主进程 getUpdateChannels / measureUpdateChannels / setUpdateChannel 的返回值 */
export interface ChannelSnapshot {
  report: ChannelReport | null;
  /** 测速得出的最快通道 */
  preferred: UpdateChannelId | null;
  /** 当前实际生效通道（手动指定优先，否则测速最优，最后回落镜像1） */
  effective: UpdateChannelId;
  /** "auto" = 按测速自动选；否则为手动指定 */
  mode: UpdateChannelId | "auto";
  /** 正在测速 */
  measuring: boolean;
  labels: Record<UpdateChannelId, string>;
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
  /** plan-20261024-update-channel：下载通道快照（测速结果 / 优选 / 生效通道）；null = 未就绪或非桌面端 */
  channels: ChannelSnapshot | null;
  /** 重新测速（点击即进入「测速中」，不等主进程推送） */
  measureChannels: () => Promise<void>;
  /** 手动指定通道；"auto" 恢复按测速自动选 */
  setChannel: (id: UpdateChannelId | "auto") => Promise<void>;
  /** 订阅主进程状态推送（幂等，App 挂载时调用一次） */
  init: () => void;
  checkForUpdates: () => Promise<void>;
  downloadUpdate: () => Promise<void>;
  /** 取消下载：回到「发现新版本」，已下载的部分保留，再次下载从镜像线路开始 */
  cancelDownload: () => Promise<void>;
  /** 切回第一优先级（镜像）：重置线路优先级后接着下 */
  switchToPrimary: () => Promise<void>;
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
  channels: null,
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
    // plan-20261024-update-channel：通道快照随主进程测速推送实时刷新；再拉一次兜底
    // （测速可能在订阅之前就已完成）。init 有 listening 守卫，不会重复订阅。
    api.onUpdateChannels?.((snap) => {
      const next = normalizeChannels(snap);
      if (next) set({ channels: next });
    });
    void api.getUpdateChannels?.().then((snap) => {
      const next = normalizeChannels(snap);
      if (next) set({ channels: next });
    }).catch(() => { /* 保持 null，关于页不渲染通道区 */ });
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
  cancelDownload: async () => {
    try {
      const r = await window.chatcoderAPI?.cancelUpdateDownload?.();
      if (isUpdateStatus(r)) set({ status: normalizeStatus(r) });
    } catch { /* 主进程会推送最新状态 */ }
  },
  switchToPrimary: async () => {
    const before = get().status;
    // 点击即回到「连接中」：主进程停下旧下载再重开需要一点时间，界面不能停在 GitHub 提示上
    if (before.state === "downloading") set({ status: { ...before, phase: "connecting", tier: "mirror", source: "镜像", retryInMs: undefined, nextLabel: undefined } });
    try {
      const r = await window.chatcoderAPI?.switchUpdateSourceToPrimary?.();
      if (isUpdateStatus(r)) set({ status: normalizeStatus(r) });
    } catch { /* 主进程会推送最新状态 */ }
  },
  measureChannels: async () => {
    const api = window.chatcoderAPI;
    if (!api?.measureUpdateChannels) return;
    // 乐观态：一轮测速要等三条通道都回来（每条最多一个超时），点了必须立刻有反馈
    const prev = get().channels;
    if (prev) set({ channels: { ...prev, measuring: true } });
    try {
      const next = normalizeChannels(await api.measureUpdateChannels());
      if (next) set({ channels: next });
    } catch {
      // 失败：退回点击前的快照，避免界面一直停在「测速中」
      const cur = get().channels;
      if (cur) set({ channels: { ...cur, measuring: false } });
    }
  },
  setChannel: async (id) => {
    const api = window.chatcoderAPI;
    if (!api?.setUpdateChannel) return;
    // 修改即保存：先把选择落到界面（选中态与提示行立即变化），主进程返回的快照为准
    const prev = get().channels;
    if (prev) {
      set({ channels: { ...prev, mode: id, effective: id === "auto" ? (prev.preferred || prev.effective) : id } });
    }
    try {
      const next = normalizeChannels(await api.setUpdateChannel(id));
      if (next) set({ channels: next });
    } catch {
      if (prev) set({ channels: prev });
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

function isChannelId(v: unknown): v is UpdateChannelId {
  return v === "mirror1" || v === "mirror2" || v === "github";
}

/** plan-20261024-update-channel：主进程快照收窄。
 *  preload 缺失（dev / 非桌面端）或结构不认识时返回 null，关于页据此不渲染通道区，
 *  而不是拿半截数据画出「未测速」的假卡片。 */
function normalizeChannels(v: unknown): ChannelSnapshot | null {
  if (!v || typeof v !== "object") return null;
  const raw = v as Record<string, unknown>;
  if (!("effective" in raw) && !("report" in raw)) return null;
  const labels = (raw.labels && typeof raw.labels === "object" ? raw.labels : {}) as Record<string, unknown>;
  const report = raw.report && typeof raw.report === "object" ? (raw.report as ChannelReport) : null;
  return {
    report,
    preferred: isChannelId(raw.preferred) ? raw.preferred : null,
    effective: isChannelId(raw.effective) ? raw.effective : "mirror1",
    mode: raw.mode === "auto" ? "auto" : isChannelId(raw.mode) ? raw.mode : "auto",
    measuring: raw.measuring === true,
    labels: {
      mirror1: typeof labels.mirror1 === "string" ? labels.mirror1 : "镜像1",
      mirror2: typeof labels.mirror2 === "string" ? labels.mirror2 : "镜像2",
      github: typeof labels.github === "string" ? labels.github : "github",
    },
  };
}
