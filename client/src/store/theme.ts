/** 主题状态管理：浅色/深色两套 + localStorage 持久化。
 * v4 r2：裁剪 sepia/midnight，旧值回退到 light/dark。
 */
import { create } from "zustand";

export type Theme = "light" | "dark";

const STORAGE_KEY = "chatcoder.theme";

function getInitialTheme(): Theme {
  if (typeof window === "undefined") return "dark";
  const saved = localStorage.getItem(STORAGE_KEY);
  // 旧值 sepia/midnight 回退
  if (saved === "light" || saved === "dark") return saved;
  // 首次访问跟随系统偏好
  return window.matchMedia?.("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

function applyTheme(theme: Theme) {
  if (typeof document === "undefined") return;
  document.documentElement.setAttribute("data-theme", theme);
}

interface ThemeState {
  theme: Theme;
  setTheme: (t: Theme) => void;
  toggle: () => void;
}

export const useThemeStore = create<ThemeState>((set, get) => ({
  theme: getInitialTheme(),
  setTheme: (t) => {
    set({ theme: t });
    localStorage.setItem(STORAGE_KEY, t);
    applyTheme(t);
  },
  toggle: () => {
    const next = get().theme === "dark" ? "light" : "dark";
    get().setTheme(next);
  },
}));

/** 在应用启动时调用一次，把初始主题应用到 <html>。 */
export function initTheme() {
  // 清理旧主题值
  const saved = localStorage.getItem(STORAGE_KEY);
  if (saved && saved !== "light" && saved !== "dark") {
    localStorage.setItem(STORAGE_KEY, getInitialTheme());
  }
  applyTheme(useThemeStore.getState().theme);
}
