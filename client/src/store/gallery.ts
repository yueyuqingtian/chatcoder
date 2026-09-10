/** 全局图片查看器状态（会话 229）：所有图片入口共用同一查看器（左右切换/缩放/下载/关闭）。
 *
 * 调用方需先经 resolveFileUrl 转换 URL 再传入；非组件环境可调 openGallery。
 */
import { create } from "zustand";

export interface GalleryImage {
  /** 可直接访问的图片地址（调用方已 resolveFileUrl） */
  url: string;
  /** 文件名（下载与 alt 用） */
  name: string;
  size?: number;
}

interface GalleryState {
  images: GalleryImage[];
  index: number;
  open: (images: GalleryImage[], index?: number) => void;
  close: () => void;
  setIndex: (i: number) => void;
}

export const useGalleryStore = create<GalleryState>((set) => ({
  images: [],
  index: 0,
  open: (images, index = 0) => {
    if (!images || images.length === 0) return;
    set({ images, index: Math.max(0, Math.min(index, images.length - 1)) });
  },
  close: () => set({ images: [], index: 0 }),
  setIndex: (i) =>
    set((s) => ({ index: Math.max(0, Math.min(i, Math.max(0, s.images.length - 1))) })),
}));

/** 便捷入口（事件回调/非组件环境可直接调用）。 */
export function openGallery(images: GalleryImage[], index = 0) {
  useGalleryStore.getState().open(images, index);
}
