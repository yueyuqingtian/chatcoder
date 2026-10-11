/** 主题状态管理：浅色/深色两套 + 主题世界 + localStorage 持久化。
 * v4 r2：裁剪 sepia/midnight，旧值回退到 light/dark。
 */
import { create } from "zustand";

export type Theme = "light" | "dark";
/** plan-114-447：主题从「色调」改为「世界」——只决定强调色、场景元素与嗒嗒配件，底色与侧栏保持中性 */
export type World = "graphite" | "forest" | "campfire" | "breeze" | "tide" | "sakura";
export const WORLDS: World[] = ["graphite", "forest", "campfire", "breeze", "tide", "sakura"];
const WORLD_KEY = "chatcoder.world";
/** 旧色调键，仅用于一次性迁移 */
const LEGACY_TONE_KEY = "chatcoder.tone";
/** plan-114-447：旧色调到新世界的迁移表，不保留自定义色相 */
const LEGACY_MAP: Record<string, World> = {
  graphite: "graphite", mint: "forest", ocean: "tide", apricot: "campfire", lavender: "breeze", sakura: "sakura",
};

export function normalizeWorld(v: string | null | undefined): World {
  if (!v) return "graphite";
  if ((WORLDS as string[]).includes(v)) return v as World;
  return LEGACY_MAP[v] ?? "graphite";
}

function getInitialWorld(): World {
  if (typeof window === "undefined") return "graphite";
  const saved = localStorage.getItem(WORLD_KEY);
  if (saved) return normalizeWorld(saved);
  // 迁移旧色调并回写，之后不再读旧键
  const legacy = localStorage.getItem(LEGACY_TONE_KEY);
  const w = normalizeWorld(legacy);
  try {
    localStorage.setItem(WORLD_KEY, w);
    localStorage.removeItem(LEGACY_TONE_KEY);
  } catch { /* 存储不可用时忽略 */ }
  return w;
}

function applyWorld(world: World) {
  if (typeof document === "undefined") return;
  document.documentElement.setAttribute("data-world", world);
  // 清掉旧属性，避免残留的色调选择器继续生效
  document.documentElement.removeAttribute("data-tone");
}

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

function syncWorldToMain(w: World) {
  // 主进程据此决定启动页的场景元素；沿用旧 IPC 名，主进程侧兼容新旧值
  try { (window.chatcoderAPI as { setTonePref?: (t: string) => void } | undefined)?.setTonePref?.(w); } catch { /* 非桌面环境忽略 */ }
}

interface ThemeState {
  theme: Theme;
  world: World;
  setWorld: (w: World) => void;
  setTheme: (t: Theme) => void;
  toggle: () => void;
}

export const useThemeStore = create<ThemeState>((set, get) => ({
  theme: getInitialTheme(),
  world: getInitialWorld(),
  setWorld: (w) => {
    set({ world: w });
    localStorage.setItem(WORLD_KEY, w);
    applyWorld(w);
    syncWorldToMain(w);
  },
  setTheme: (t) => {
    set({ theme: t });
    localStorage.setItem(STORAGE_KEY, t);
    applyTheme(t);
    // 同步主进程落盘：下次启动 loading 页按此适配深浅色（localStorage 主进程读不到）
    try { window.chatcoderAPI?.setThemePref?.(t); } catch { /* 非桌面环境忽略 */ }
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
  const { theme, world } = useThemeStore.getState();
  applyTheme(theme);
  applyWorld(world);
  // 首次把当前主题同步给主进程（老用户无 theme-pref.json 时补齐；迁移后的世界值也一并回写）
  try { window.chatcoderAPI?.setThemePref?.(theme); } catch { /* 非桌面环境忽略 */ }
  syncWorldToMain(world);
}
