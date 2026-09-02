/**
 * UI 偏好管理:面板宽度、字体、字号、毛玻璃、语言等。
 * 全部持久化到 localStorage,启动时自动应用 CSS 变量。
 */
import { create } from "zustand";

export type Language = "zh" | "en";

export interface UiPrefs {
  /** 左侧面板宽度(px) */
  leftPanelWidth: number;
  /** 右侧面板宽度(px) */
  rightPanelWidth: number;
  /** 毛玻璃效果开关 */
  glassmorphism: boolean;
  /** 毛玻璃玻璃强度:0=轻柔 1=标准 2=深邃 */
  glassStrength: number;
  /** 玻璃渐变主色1 */
  glassGradientC1: string;
  /** 玻璃渐变主色2 */
  glassGradientC2: string;
  /** 阴影强度:0=无 0.5=轻柔 1=标准 1.5=深邃 2=戏剧 */
  shadowStrength: number;
  /** 对话字体族 */
  chatFontFamily: string;
  /** 对话字号(px) */
  chatFontSize: number;
  /** 对话气泡最大宽度(%) */
  chatBubbleWidth: number;
  /** 消息行高倍率 */
  chatLineHeight: number;
  /** 主界面基础字号(px) */
  uiBaseFontSize: number;
  /** 内容区最大宽度(px),0=不限 */
  contentMaxWidth: number;
  /** 左面板文字大小(px) */
  sidebarFontSize: number;
  /** 左面板图标大小(px) */
  sidebarIconSize: number;
  /** 左面板聚焦颜色 */
  sidebarFocusColor: string;
  /** 消息流密度：comfortable=舒适(默认) compact=紧凑 */
  msgDensity: "comfortable" | "compact";
  /** 界面语言 */
  language: Language;
}

const STORAGE_KEY = "chatcoder.ui-prefs";

const DEFAULTS: UiPrefs = {
  leftPanelWidth: 264,
  rightPanelWidth: 420,
  glassmorphism: false,
  glassStrength: 1,
  glassGradientC1: "",
  glassGradientC2: "",
  shadowStrength: 1,
  chatFontFamily: "system",
  chatFontSize: 13,
  chatBubbleWidth: 70,
  chatLineHeight: 1.7,
  uiBaseFontSize: 13,
  contentMaxWidth: 1120,
  sidebarFontSize: 12,
  sidebarIconSize: 14,
  sidebarFocusColor: "",
  msgDensity: "comfortable",
  language: "zh",
};

const FONT_OPTIONS: Record<string, string> = {
  system: '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif',
  serif: '"Georgia", "Noto Serif SC", "Source Han Serif SC", serif',
  mono: '"JetBrains Mono", "SF Mono", "Cascadia Code", Consolas, monospace',
  rounded: '"SF Pro Rounded", "Hiragino Maru Gothic Pro", "Microsoft YaHei", sans-serif',
};

function loadPrefs(): UiPrefs {
  if (typeof window === "undefined") return DEFAULTS;
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return DEFAULTS;
    const stored = JSON.parse(raw) as Partial<UiPrefs>;
    // v14: 840px 是旧版默认值；仅迁移未主动调整过的旧默认，不覆盖用户自定义宽度。
    if (stored.contentMaxWidth === 840) stored.contentMaxWidth = DEFAULTS.contentMaxWidth;
    return { ...DEFAULTS, ...stored };
  } catch {
    return DEFAULTS;
  }
}

function savePrefs(p: UiPrefs) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(p));
  } catch {
    /* ignore */
  }
}

/** 把偏好应用到全局 CSS 变量 */
export function applyUiVars(p: UiPrefs) {
  if (typeof document === "undefined") return;
  const root = document.documentElement;
  root.style.setProperty("--sidebar-w", `${p.leftPanelWidth}px`);
  root.style.setProperty("--fs-md", `${p.chatFontSize}px`);
  root.style.setProperty("--chat-font", FONT_OPTIONS[p.chatFontFamily] || FONT_OPTIONS.system);
  root.style.setProperty("--chat-bubble-w", `${p.chatBubbleWidth}%`);
  root.style.setProperty("--chat-line-height", `${p.chatLineHeight}`);
  root.style.setProperty("--content-max-w", p.contentMaxWidth > 0 ? `${p.contentMaxWidth}px` : "none");
  root.style.setProperty("--ui-base-fs", `${p.uiBaseFontSize}px`);
  root.style.setProperty("--sidebar-fs", `${p.sidebarFontSize}px`);
  root.style.setProperty("--sidebar-icon", `${p.sidebarIconSize}px`);
  if (p.sidebarFocusColor) root.style.setProperty("--sidebar-focus", p.sidebarFocusColor);
  else root.style.removeProperty("--sidebar-focus");
  root.style.fontSize = `${p.uiBaseFontSize}px`;
  root.setAttribute("data-glass", p.glassmorphism ? "on" : "off");
  // plan-546: 通知主进程切换 Win11 acrylic 真磨砂（不支持时静默降级 CSS 半透明）；
  // 浏览器/开发模式无 chatcoderAPI 时仅走 CSS 效果。
  try {
    void window.chatcoderAPI?.setGlassMode?.(p.glassmorphism);
  } catch { /* ignore */ }
  // v15: 外部穿透已移除；清理旧版本可能遗留的 DOM 属性。
  root.removeAttribute("data-external");
  // 玻璃强度:0=0.5x 1=1x 2=1.6x 模糊
  const strength = p.glassStrength === 2 ? 1.6 : p.glassStrength === 0 ? 0.5 : 1;
  root.style.setProperty("--glass-strength", String(strength));
  // plan-548: 侧栏透出桌面的比例随玻璃强度变化（叠在 DWM acrylic 之上的侧栏底色不透明度）
  const sidebarAlpha = p.glassStrength === 2 ? 0.58 : p.glassStrength === 0 ? 0.78 : 0.68;
  root.style.setProperty("--glass-sidebar-alpha", String(sidebarAlpha));
  // 玻璃渐变主色（空则使用主题默认）
  if (p.glassGradientC1) root.style.setProperty("--ambient-c1", p.glassGradientC1);
  else root.style.removeProperty("--ambient-c1");
  if (p.glassGradientC2) root.style.setProperty("--ambient-c2", p.glassGradientC2);
  else root.style.removeProperty("--ambient-c2");
  // v1.1: 阴影强度（此前完全未应用）
  root.style.setProperty("--shadow-strength", String(p.shadowStrength));
  // 消息流密度：舒适(默认 10px 块间距) / 紧凑(2px)
  root.style.setProperty("--msg-gap", p.msgDensity === "compact" ? "2px" : "10px");
  root.setAttribute("data-lang", p.language);
}

export const FONT_LABELS: Record<string, { zh: string; en: string }> = {
  system: { zh: "系统默认", en: "System Default" },
  serif: { zh: "衬线体", en: "Serif" },
  mono: { zh: "等宽体", en: "Monospace" },
  rounded: { zh: "圆角体", en: "Rounded" },
};

interface UiState extends UiPrefs {
  setLeftPanelWidth: (w: number) => void;
  setRightPanelWidth: (w: number) => void;
  setPrefs: (partial: Partial<UiPrefs>) => void;
  toggleGlass: () => void;
  setLanguage: (lang: Language) => void;
  /** v1.1: 后端全局设置缓存（show_todos/show_reasoning 等） */
  showTodos: boolean;
  showReasoning: boolean;
  refreshGlobalFlags: () => Promise<void>;
}

export const useUiStore = create<UiState>((set, get) => ({
  ...loadPrefs(),

  // v1.1: 消息流显示开关默认开，启动后由后端全局设置刷新
  showTodos: true,
  showReasoning: true,
  refreshGlobalFlags: async () => {
    try {
      // 动态 import 避免循环依赖（ui.ts 与 api/client 相互独立但被各组件引用）
      const { api } = await import("../api/client");
      const data = await api.getGlobalSettings();
      set({
        showTodos: data.show_todos !== false,
        showReasoning: data.show_reasoning !== false,
      });
    } catch { /* 后端不可达时保持默认 */ }
  },

  setLeftPanelWidth: (w) => {
    const clamped = Math.max(200, Math.min(480, Math.round(w)));
    set({ leftPanelWidth: clamped });
    savePrefs({ ...get(), leftPanelWidth: clamped });
    applyUiVars({ ...get(), leftPanelWidth: clamped });
  },

 setRightPanelWidth: (w) => {
    const clamped = Math.max(320, Math.min(1200, Math.round(w))); // v1.1: 上限 640 → 1200，与外观滑杆 max 一致
    set({ rightPanelWidth: clamped });
    savePrefs({ ...get(), rightPanelWidth: clamped });
    applyUiVars({ ...get(), rightPanelWidth: clamped });
  },

  setPrefs: (partial) => {
    const next = { ...get(), ...partial };
    set(partial);
    savePrefs(next);
    applyUiVars(next);
  },

  toggleGlass: () => {
    const next = { ...get(), glassmorphism: !get().glassmorphism };
    set({ glassmorphism: next.glassmorphism });
    savePrefs(next);
    applyUiVars(next);
  },

  setLanguage: (lang) => {
    const next = { ...get(), language: lang };
    set({ language: lang });
    savePrefs(next);
    applyUiVars(next);
    // 同步到后端全局设置
    try {
      fetch("/api/settings/global", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ language: lang }),
      }).catch(() => {});
    } catch { /* ignore */ }
  },
}));

/** 在应用启动时调用一次 */
export function initUi() {
  applyUiVars(loadPrefs());
}
