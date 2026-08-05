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
  /** 对话字体族 */
  chatFontFamily: string;
  /** 对话字号(px) */
  chatFontSize: number;
  /** 对话气泡最大宽度(%) */
  chatBubbleWidth: number;
  /** 消息行高倍率 */
  chatLineHeight: number;
  /** 代码块文字颜色 */
  chatCodeColor: string;
  /** 标题文字颜色 */
  chatHeadingColor: string;
  /** 链接文字颜色 */
  chatLinkColor: string;
  /** 引用块文字颜色 */
  chatQuoteColor: string;
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
  /** 界面语言 */
  language: Language;
}

const STORAGE_KEY = "chatcoder.ui-prefs";

const DEFAULTS: UiPrefs = {
  leftPanelWidth: 264,
  rightPanelWidth: 420,
  glassmorphism: false,
  chatFontFamily: "system",
  chatFontSize: 13,
  chatBubbleWidth: 70,
  chatLineHeight: 1.6,
  chatCodeColor: "#D98014",
  chatHeadingColor: "#1A1A1E",
  chatLinkColor: "#3B82F6",
  chatQuoteColor: "#6B6B74",
  uiBaseFontSize: 13,
  contentMaxWidth: 840,
  sidebarFontSize: 12,
  sidebarIconSize: 14,
  sidebarFocusColor: "",
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
    return { ...DEFAULTS, ...JSON.parse(raw) };
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
  root.style.setProperty("--chat-code-color", p.chatCodeColor);
  root.style.setProperty("--chat-heading-color", p.chatHeadingColor);
  root.style.setProperty("--chat-link-color", p.chatLinkColor);
  root.style.setProperty("--chat-quote-color", p.chatQuoteColor);
  root.style.setProperty("--content-max-w", p.contentMaxWidth > 0 ? `${p.contentMaxWidth}px` : "none");
  root.style.setProperty("--ui-base-fs", `${p.uiBaseFontSize}px`);
  root.style.setProperty("--sidebar-fs", `${p.sidebarFontSize}px`);
  root.style.setProperty("--sidebar-icon", `${p.sidebarIconSize}px`);
  if (p.sidebarFocusColor) root.style.setProperty("--sidebar-focus", p.sidebarFocusColor);
  else root.style.removeProperty("--sidebar-focus");
  root.style.fontSize = `${p.uiBaseFontSize}px`;
  root.setAttribute("data-glass", p.glassmorphism ? "on" : "off");
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
}

export const useUiStore = create<UiState>((set, get) => ({
  ...loadPrefs(),

  setLeftPanelWidth: (w) => {
    const clamped = Math.max(200, Math.min(480, Math.round(w)));
    set({ leftPanelWidth: clamped });
    savePrefs({ ...get(), leftPanelWidth: clamped });
    applyUiVars({ ...get(), leftPanelWidth: clamped });
  },

 setRightPanelWidth: (w) => {
    const clamped = Math.max(320, Math.min(640, Math.round(w)));
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
