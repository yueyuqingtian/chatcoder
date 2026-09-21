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
  /** plan-308-1542 需求4：玻璃风格——solid=不透明纯色（仅开应用内层次）
   *  liquid=液态玻璃（高光/内描边/柔和阴影 + 系统模糊透出桌面） */
  glassStyle: "solid" | "liquid";
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
  /** plan-248-1258 M5: 动画效果档位——full=标准 reduced=减弱(低配机省性能) off=关闭 */
  motionLevel: MotionLevel;
}

export type MotionLevel = "full" | "reduced" | "off";

const STORAGE_KEY = "chatcoder.ui-prefs";

const DEFAULTS: UiPrefs = {
  leftPanelWidth: 264,
  rightPanelWidth: 420,
  glassmorphism: false,
  glassStyle: "liquid",
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
  motionLevel: "full",
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
  // plan-308-1542 需求4：玻璃风格（liquid=液态玻璃高光层；solid=纯色）
  root.setAttribute("data-glass-style", p.glassStyle || "liquid");
  // plan-546: 通知主进程切换系统级模糊（Win11 acrylic / Win10 ACCENT / mac vibrancy）；
  // 浏览器/开发模式无 chatcoderAPI 时仅走 CSS 效果。
  // plan-308-1542：并探测后端能力，落 data-glass-backend 供设置页与 CSS 降级分支使用。
  // plan-308-1555 M5：降级判定改为以 **DWM 回读值**为准——
  //   历史上只看"后端名"就报成功（setBackgroundMaterial 返回无异常即算成功），
  //   但实测存在"API 被接受、系统实际未启用材质"的情况，会误导排查。
  //   主进程 applyGlass 现返回 verified（DWMWA_SYSTEMBACKDROP_TYPE 回读值），
  //   只有 verified >= 2（mica/acrylic/tabbed）才算真的生效。
  try {
    const api = window.chatcoderAPI;
    if (api?.setGlassMode) {
      void api.setGlassMode(p.glassmorphism).then((res) => {
        const r = (res || {}) as { backend?: string; ok?: boolean; verified?: number | null; reason?: string };
        const backend = r.backend || "none";
        root.setAttribute("data-glass-backend", backend);
        if (typeof r.verified === "number") {
          root.setAttribute("data-glass-verified", String(r.verified));
        } else {
          root.removeAttribute("data-glass-verified");
        }
        // 关闭玻璃时不判降级；开启时要求"有后端 + 回读值 >= 2"
        const live = !p.glassmorphism
          || (r.ok !== false && (r.verified == null || r.verified >= 2));
        if (!live) root.setAttribute("data-glass-degraded", "1");
        else root.removeAttribute("data-glass-degraded");
      }).catch(() => { /* 忽略：能力探测失败不阻断 */ });
    } else if (api?.glassCapability) {
      void api.glassCapability().then((cap) => {
        if (cap?.backend) root.setAttribute("data-glass-backend", cap.backend);
        if (!cap?.supported) root.setAttribute("data-glass-degraded", "1");
      }).catch(() => { /* ignore */ });
    }
  } catch { /* ignore */ }
  // v15: 外部穿透已移除；清理旧版本可能遗留的 DOM 属性。
  root.removeAttribute("data-external");
  // 玻璃强度:0=0.5x 1=1x 2=1.6x 模糊
  const strength = p.glassStrength === 2 ? 1.6 : p.glassStrength === 0 ? 0.5 : 1;
  root.style.setProperty("--glass-strength", String(strength));
  // plan-548 + plan-308-1555 M4：侧栏/面板透出桌面的比例。
  // 旧值 0.78/0.68/0.58 在深色主题 + 深色桌面的组合下对比度趋近于 0，
  // 用户"看不出透出"多半就是这个原因（实测：改 alpha 像素确实变化，说明链路是通的）。
  // 新值整体下调，保证"微微透出"肉眼可辨，同时保留可读性底线。
  const sidebarAlpha = p.glassStrength === 2 ? 0.42 : p.glassStrength === 0 ? 0.62 : 0.52;
  root.style.setProperty("--glass-sidebar-alpha", String(sidebarAlpha));
  // 面板（标题栏/右面板）不透明度：比侧栏略高，平衡可读性与透出感
  const panelAlpha = p.glassStrength === 2 ? 0.52 : p.glassStrength === 0 ? 0.72 : 0.62;
  root.style.setProperty("--lg-alpha-panel", String(panelAlpha));
  // 玻璃渐变主色（空则使用主题默认）
  if (p.glassGradientC1) root.style.setProperty("--ambient-c1", p.glassGradientC1);
  else root.style.removeProperty("--ambient-c1");
  if (p.glassGradientC2) root.style.setProperty("--ambient-c2", p.glassGradientC2);
  else root.style.removeProperty("--ambient-c2");
  // v1.1: 阴影强度（此前完全未应用）
  root.style.setProperty("--shadow-strength", String(p.shadowStrength));
  // 消息流密度：舒适(默认 10px 块间距) / 紧凑(2px)。
  // 注意：AI 消息流项间距由 --flow-gap 控制（.turn-flow gap），--msg-gap 只作用于
  // 独立消息项；密度同时覆盖两者才真正生效。
  root.style.setProperty("--msg-gap", p.msgDensity === "compact" ? "2px" : "10px");
  if (p.msgDensity === "compact") root.style.setProperty("--flow-gap", "2px");
  else root.style.removeProperty("--flow-gap");
  root.setAttribute("data-lang", p.language);
  // plan-248-1258 M5: 动画效果档位落到根节点属性，motion.css 据此降级；
  // 系统 prefers-reduced-motion 与「减弱」等价（在 CSS 侧已分别处理）。
  root.setAttribute("data-motion", p.motionLevel || "full");
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
