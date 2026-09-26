/** 宠物系统前端状态（plan-73-323 阶段4）。
 *
 * 偏好真相在主进程（`userData/pet-pref.json`）：本 store 只缓存一份快照供设置页渲染，
 * 变更后由主进程广播 `pet:prefChanged` 回写 —— 主窗与宠物窗口不各自维护副本，
 * 避免"设置页改了、宠物窗口没变"这类双份真相问题。
 *
 * 图库与安装动作的 UI 状态（加载中 / 错误 / 忙碌项）也集中在此，面板组件保持无状态渲染。
 */
import { create } from "zustand";
import type { InstalledPet, PetPref } from "../pet/petApi";

export interface GalleryPet {
  slug: string;
  displayName: string;
  kind: string;
  author: string;
  /** 缩略图（petdex preview.webp；失败时前端回退到精灵图首帧裁剪） */
  previewUrl: string;
  spritesheetUrl: string;
  installed: boolean;
}

interface PetsState {
  pref: PetPref | null;
  installed: InstalledPet[];
  installedLoading: boolean;
  gallery: GalleryPet[];
  galleryTotal: number;
  galleryShown: number;
  galleryLoading: boolean;
  galleryError: string | null;
  galleryFromCache: boolean;
  /** 正在安装/移除的宠物 slug（用于按钮禁用与进度提示） */
  busySlug: string | null;
  actionError: string | null;

  init: () => Promise<void>;
  /** 接收主进程广播的偏好快照 */
  syncPref: (pref: PetPref) => void;
  setPref: (patch: Partial<PetPref>) => Promise<void>;
  loadInstalled: () => Promise<void>;
  loadGallery: (opts?: { force?: boolean; query?: string }) => Promise<void>;
  install: (slug: string) => Promise<boolean>;
  remove: (slug: string) => Promise<boolean>;
  importLocal: () => Promise<boolean>;
  /** 恢复显示（宠物窗口下方隐藏角标置 visible=false 后从这里恢复） */
  showPet: () => Promise<void>;
}

const errText = (e: unknown): string => {
  const msg = e instanceof Error ? e.message : String(e);
  // Electron IPC 抛错会带 "Error invoking remote method ...: Error: " 前缀，剥掉更易读
  return msg.replace(/^Error invoking remote method '[^']+':\s*(Error:\s*)?/, "");
};

export const usePetsStore = create<PetsState>((set, get) => ({
  pref: null,
  installed: [],
  installedLoading: false,
  gallery: [],
  galleryTotal: 0,
  galleryShown: 0,
  galleryLoading: false,
  galleryError: null,
  galleryFromCache: false,
  busySlug: null,
  actionError: null,

  init: async () => {
    const api = window.chatcoderAPI;
    if (!api?.petGetPref) return;
    try {
      const pref = await api.petGetPref();
      set({ pref });
    } catch {
      // 偏好读取失败不阻断面板（后续操作会再次尝试）
    }
    await get().loadInstalled();
  },

  syncPref: (pref) => set({ pref }),

  setPref: async (patch) => {
    const api = window.chatcoderAPI;
    if (!api?.petSetPref) return;
    // 乐观更新：开关类偏好要求"修改即保存"且即时反馈，失败再回滚
    const before = get().pref;
    if (before) set({ pref: { ...before, ...patch }, actionError: null });
    try {
      const next = await api.petSetPref(patch);
      set({ pref: next });
    } catch (e) {
      set({ pref: before ?? null, actionError: errText(e) });
    }
  },

  loadInstalled: async () => {
    const api = window.chatcoderAPI;
    if (!api?.petListInstalled) return;
    set({ installedLoading: true });
    try {
      const list = await api.petListInstalled();
      set({ installed: Array.isArray(list) ? list : [], installedLoading: false });
    } catch (e) {
      set({ installedLoading: false, actionError: errText(e) });
    }
  },

  loadGallery: async (opts) => {
    const api = window.chatcoderAPI;
    if (!api?.petListManifest) return;
    set({ galleryLoading: true, galleryError: null });
    try {
      const res = await api.petListManifest({ force: !!opts?.force, query: opts?.query || "" });
      set({
        gallery: res.pets || [],
        galleryTotal: res.total || 0,
        galleryShown: res.shown || (res.pets ? res.pets.length : 0),
        galleryFromCache: !!res.fromCache,
        galleryLoading: false,
        galleryError: res.error || null,
      });
    } catch (e) {
      set({ galleryLoading: false, galleryError: errText(e) });
    }
  },

  install: async (slug) => {
    const api = window.chatcoderAPI;
    if (!api?.petInstall) return false;
    set({ busySlug: slug, actionError: null });
    try {
      await api.petInstall(slug);
      await get().loadInstalled();
      // 图库列表里的"已安装"标记同步（避免整表刷新）
      set({ gallery: get().gallery.map((p) => (p.slug === slug ? { ...p, installed: true } : p)) });
      const pref = get().pref;
      // 首个宠物：安装后直接启用，用户不必再去点开关（一步到位）
      if (pref && !pref.slug) {
        await get().setPref({ slug, enabled: true });
      } else if (pref && pref.slug === slug && !pref.enabled) {
        await get().setPref({ enabled: true });
      }
      set({ busySlug: null });
      return true;
    } catch (e) {
      set({ busySlug: null, actionError: errText(e) });
      return false;
    }
  },

  remove: async (slug) => {
    const api = window.chatcoderAPI;
    if (!api?.petRemove) return false;
    set({ busySlug: slug, actionError: null });
    try {
      await api.petRemove(slug);
      await get().loadInstalled();
      set({ gallery: get().gallery.map((p) => (p.slug === slug ? { ...p, installed: false } : p)) });
      const pref = get().pref;
      if (pref && pref.slug === slug) set({ pref: { ...pref, slug: "" } });
      set({ busySlug: null });
      return true;
    } catch (e) {
      set({ busySlug: null, actionError: errText(e) });
      return false;
    }
  },

  importLocal: async () => {
    const api = window.chatcoderAPI;
    if (!api?.petImportLocal) return false;
    set({ actionError: null });
    try {
      const res = await api.petImportLocal();
      if (!res || res.canceled) return false;
      await get().loadInstalled();
      const pref = get().pref;
      if (pref && !pref.slug && res.slug) await get().setPref({ slug: res.slug, enabled: true });
      return true;
    } catch (e) {
      set({ actionError: errText(e) });
      return false;
    }
  },

  // plan-73-326：隐藏角标写入 visible=false，恢复入口在设置页
  showPet: async () => {
    const api = window.chatcoderAPI;
    if (!api?.petShowPet) return;
    try {
      await api.petShowPet();
      const pref = get().pref;
      if (pref) set({ pref: { ...pref, visible: true } });
    } catch (e) {
      set({ actionError: errText(e) });
    }
  },
}));
