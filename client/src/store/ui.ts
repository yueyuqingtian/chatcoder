/**
 * UI 偏好管理:面板宽度、字体、字号、毛玻璃、语言等。
 * 持久化到 localStorage + 主进程 ui-prefs.json 双通道（plan-73-344），启动时自动应用 CSS 变量。
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
  /** 毛玻璃强度:0=轻柔 1=标准 2=深邃（决定各层不透明度） */
  glassStrength: number;
  /** 玻璃渐变主色1 */
  glassGradientC1: string;
  /** 玻璃渐变主色2 */
  glassGradientC2: string;
  /** 阴影强度:0=无 0.5=轻柔 1=标准 1.5=深邃 2=戏剧 */
  shadowStrength: number;
  /** 对话字体族（历史字段，无界面入口；system 时跟随全局字体） */
  chatFontFamily: string;
  /** 全局界面字体（plan-20261017-ux-overhaul 阶段 C），取值见 UI_FONT_OPTIONS */
  uiFontFamily: string;
  /** 对话字号(px) */
  chatFontSize: number;
  /** 对话气泡最大宽度(%) */
  chatBubbleWidth: number;
  /** 消息行高倍率 */
  chatLineHeight: number;
  /** 主界面基础字号(px) */
  uiBaseFontSize: number;
  /** 内容区最大宽度(px)，默认 1000、上限 1600，0=不限（铺满） */
  contentMaxWidth: number;
  /** 左面板文字大小(px) */
  sidebarFontSize: number;
  /** 左面板图标大小(px) */
  sidebarIconSize: number;
  /** 左面板聚焦颜色 */
  sidebarFocusColor: string;
  /** 界面语言 */
  language: Language;
  /** plan-248-1258 M5: 动画效果档位——full=标准 reduced=减弱(低配机省性能) off=关闭 */
  motionLevel: MotionLevel;
  /** plan-329-1647 S6（RFL-7）：分隔条拖拽期的排版档位
   *  realtime=实时跟随（默认）/ balanced=隔帧写宽度 / frozen=约 15fps 降频跟随（不停止反馈） */
  panelDragLayout: PanelDragLayout;
  /** plan-329-1647 S6（RFL-7）：是否允许按帧预算自动降级（关闭则固定 panelDragLayout） */
  panelDragAutoDegrade: boolean;
}

export type MotionLevel = "full" | "reduced" | "off";

/** 面板拖拽期排版档位（RFL-5 帧闸门的目标档上限）。 */
export type PanelDragLayout = "realtime" | "balanced" | "frozen";

const STORAGE_KEY = "chatcoder.ui-prefs";
/** 偏好写入时间戳（plan-73-344）：与主进程备份比对，以最后写入的通道为准 */
const SAVED_AT_KEY = "chatcoder.ui-prefs.saved-at";

const DEFAULTS: UiPrefs = {
  leftPanelWidth: 264,
  rightPanelWidth: 420,
  glassmorphism: false,
  glassStrength: 1,
  glassGradientC1: "",
  glassGradientC2: "",
  shadowStrength: 1,
  chatFontFamily: "system",
  uiFontFamily: "inter",
  chatFontSize: 13,
  chatBubbleWidth: 70,
  chatLineHeight: 2.2,
  uiBaseFontSize: 13,
  contentMaxWidth: 1000,
  sidebarFontSize: 14,
  sidebarIconSize: 16,
  sidebarFocusColor: "",
  language: "zh",
  motionLevel: "full",
  panelDragLayout: "realtime",
  panelDragAutoDegrade: true,
};

const FONT_OPTIONS: Record<string, string> = {
  system: '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif',
  serif: '"Georgia", "Noto Serif SC", "Source Han Serif SC", serif',
  mono: '"JetBrains Mono", "SF Mono", "Cascadia Code", Consolas, monospace',
  rounded: '"SF Pro Rounded", "Hiragino Maru Gothic Pro", "Microsoft YaHei", sans-serif',
};

/** 全局界面字体（plan-20261017-ux-overhaul 阶段 C）。
 *  默认「Inter + 微软雅黑 UI」：Inter 为屏幕显示设计，字怀大、笔画对齐像素网格，小字号下比
 *  Segoe UI 更锐利；Inter 随应用内置（仅拉丁字符集，按需加载），中文交给系统 UI 黑体。
 *  其余选项依赖本机已安装的字体，未安装时按栈逐级回退，不会出现方块字。 */
export const UI_FONT_OPTIONS: Record<string, { label: string; desc: string; stack: string }> = {
  inter: {
    label: "Inter（推荐）", desc: "内置字体，小字号清晰锐利",
    stack: '"Inter Variable", "Microsoft YaHei UI", "PingFang SC", "Microsoft YaHei", "Segoe UI", sans-serif',
  },
  system: {
    label: "系统默认", desc: "Segoe UI / 苹方 + 微软雅黑",
    stack: 'ui-sans-serif, system-ui, -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif',
  },
  harmony: {
    label: "HarmonyOS Sans", desc: "需本机已安装",
    stack: '"HarmonyOS Sans SC", "HarmonyOS Sans", "Microsoft YaHei UI", "PingFang SC", sans-serif',
  },
  sourcehan: {
    label: "思源黑体", desc: "需本机已安装",
    stack: '"Source Han Sans SC", "Noto Sans SC", "Noto Sans CJK SC", "Microsoft YaHei UI", "PingFang SC", sans-serif',
  },
  yahei: {
    label: "微软雅黑", desc: "Windows 自带",
    stack: '"Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", sans-serif',
  },
};

/** 主进程备份的进程内缓存：启动期只读一次（loadPrefs 会被 main / App / store 创建三处调用），
 *  避免重复同步 IPC；运行期变更由 savePrefs 负责回写缓存与磁盘（plan-73-344）。 */
let _backupCache: { prefs: Partial<UiPrefs> | null; at: number } | undefined;

function readBackupPrefs(): { prefs: Partial<UiPrefs> | null; at: number } {
  if (_backupCache !== undefined) return _backupCache;
  let prefs: Partial<UiPrefs> | null = null;
  let at = 0;
  try {
    const b = window.chatcoderAPI?.getUiPrefs?.();
    if (b && b.prefs && typeof b.prefs === "object") {
      prefs = b.prefs as Partial<UiPrefs>;
      at = Number(b.savedAt) || 0;
    }
  } catch {
    prefs = null;
    at = 0;
  }
  _backupCache = { prefs, at };
  return _backupCache;
}

function loadPrefs(): UiPrefs {
  if (typeof window === "undefined") return DEFAULTS;
  // plan-73-344：偏好有两个落盘通道——localStorage 与主进程 ui-prefs.json，取 savedAt 较新者。
  // 原因：Chromium 的 DOMStorage 是异步提交，app.exit 强退 / 多实例锁竞争下最后一次写入
  // 可能未落盘；备份通道为 fs.writeFileSync 同步写，故备份更新时以其为准并回写本地。
  let stored: Partial<UiPrefs> | null = null;
  let localAt = 0;
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      stored = JSON.parse(raw) as Partial<UiPrefs>;
      localAt = Number(localStorage.getItem(SAVED_AT_KEY)) || 0;
    }
  } catch {
    stored = null;
    localAt = 0;
  }
  const backup = readBackupPrefs();
  if (backup.prefs && (!stored || backup.at > localAt)) {
    stored = backup.prefs;
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(stored));
      localStorage.setItem(SAVED_AT_KEY, String(backup.at));
    } catch {
      /* 本地回写失败不阻断：本次仍以备份值启动 */
    }
  }
  if (!stored) return DEFAULTS;
  // 内容展示宽度：上一版曾用百分比保存（<=100），现恢复为 px；残留百分比值迁回默认 1000。
  // 0（不限）与合理的像素值保持不变。
  if (typeof stored.contentMaxWidth === "number") {
    if (stored.contentMaxWidth > 0 && stored.contentMaxWidth <= 100) {
      stored.contentMaxWidth = DEFAULTS.contentMaxWidth;
    }
    if (stored.contentMaxWidth > 1600) stored.contentMaxWidth = 1600;
  }
  // S1（plan-41-197）：左栏字号/图标默认值上修（12→14 / 14→16）。
  // 旧默认值（12/14）视作“用户未自定义”，迁移到新默认；手动调过的值保持不变。
  if (stored.sidebarFontSize === 12) stored.sidebarFontSize = 14;
  if (stored.sidebarIconSize === 14) stored.sidebarIconSize = 16;
  // plan-73-344：消息行距默认值 1.7 → 2.2；旧默认值视作“未自定义”，迁移到新默认。
  if (stored.chatLineHeight === 1.7) stored.chatLineHeight = DEFAULTS.chatLineHeight;
  // plan-404-1892（图1）：左面板最小宽 200 → 240——「分组/项目」胶囊 + 两个操作图标在 200px 下必然换行。
  // 历史保存的 200px 偏好按新下限校正；上限 480 不变。
  // plan-20261016-ux（问题 10）：上限放宽到 640（大屏可拖宽，实际上限由 ResizeHandle 按窗口宽计算）
  stored.leftPanelWidth = Math.max(240, Math.min(640, Math.round(Number(stored.leftPanelWidth) || DEFAULTS.leftPanelWidth)));
  // S5（plan-41-197）：外观页已移除「动画效果 / 面板拖拽排版 / 拖拽自动降级」配置，
  // 对应行为固定：标准动画、拖拽实时排版、不降级（旧存储值一并纠正，避免行为与界面不一致）。
  stored.motionLevel = "full";
  stored.panelDragLayout = "realtime";
  stored.panelDragAutoDegrade = false;
  return { ...DEFAULTS, ...stored };
}

function savePrefs(p: UiPrefs) {
  // 净化成纯数据：调用方传入的可能是含方法的 store 快照（get() 展开），
  // IPC 的结构化克隆不接受函数（plan-73-344）。
  let data: UiPrefs;
  try {
    data = JSON.parse(JSON.stringify(p)) as UiPrefs;
  } catch {
    return;
  }
  const savedAt = Date.now();
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(data));
    localStorage.setItem(SAVED_AT_KEY, String(savedAt));
  } catch {
    /* ignore */
  }
  // 第二通道：主进程 ui-prefs.json（250ms 合并落盘；退出/关窗前由主进程 flush）。
  try {
    window.chatcoderAPI?.setUiPrefs?.({ savedAt, prefs: data });
    _backupCache = { prefs: data, at: savedAt };
  } catch {
    /* 非桌面环境忽略 */
  }
}

/** 把偏好应用到全局 CSS 变量。
 *
 *  opts.glassToggle：本次调用是否由"用户主动拨动毛玻璃开关"触发。
 *  只有这种调用才允许点亮「需重启生效」徽标——applyUiVars 在**每次**偏好变更
 *  （拖滑杆 / 改面板宽度 / 切语言）时都会跑，若不加区分，用户离开外观页后随便改点
 *  别的设置就会把徽标顶回来，违背"离开过该页面就不再提示"。
 *  用显式入参而非模块级标志：标志法在快速连点 / 并发 IPC 回包时会被后一次调用抢先
 *  消费，导致该亮的徽标不亮。 */
export function applyUiVars(p: UiPrefs, opts?: { glassToggle?: boolean }) {
  if (typeof document === "undefined") return;
  const root = document.documentElement;
  // plan-20261020-retry-cred-perf B6：`--sidebar-w` 全仓只有定义、没有任何 CSS 消费者，
  // 不再每次应用偏好都改写 :root（那会触发一次整文档样式失效）。
  root.style.setProperty("--fs-md", `${p.chatFontSize}px`);
  root.style.setProperty("--font-sans", (UI_FONT_OPTIONS[p.uiFontFamily] ?? UI_FONT_OPTIONS.inter).stack);
  // 对话字体无独立入口时跟随全局字体，避免消息区与界面字体不一致
  root.style.setProperty("--chat-font", p.chatFontFamily && p.chatFontFamily !== "system"
    ? (FONT_OPTIONS[p.chatFontFamily] || "var(--font-sans)") : "var(--font-sans)");
  root.style.setProperty("--chat-bubble-w", `${p.chatBubbleWidth}%`);
  root.style.setProperty("--chat-line-height", `${p.chatLineHeight}`);
  // 0 表示不限：写成 100%（而不是 none），下游 min()/calc() 才能始终成立
  root.style.setProperty(
    "--content-max-w",
    p.contentMaxWidth > 0 ? `${Math.min(1600, p.contentMaxWidth)}px` : "100%",
  );
  root.style.setProperty("--ui-base-fs", `${p.uiBaseFontSize}px`);
  root.style.setProperty("--sidebar-fs", `${p.sidebarFontSize}px`);
  root.style.setProperty("--sidebar-icon", `${p.sidebarIconSize}px`);
  if (p.sidebarFocusColor) root.style.setProperty("--sidebar-focus", p.sidebarFocusColor);
  else root.style.removeProperty("--sidebar-focus");
  root.style.fontSize = `${p.uiBaseFontSize}px`;
  root.setAttribute("data-glass", p.glassmorphism ? "on" : "off");
  // 清理旧版本遗留的 DOM 属性（外部穿透 / 液态玻璃 / 玻璃风格 / 诊断）。
  root.removeAttribute("data-external");
  root.removeAttribute("data-glass-style");
  root.removeAttribute("data-liquid-glass");
  root.removeAttribute("data-liquid-glass-degraded");
  root.removeAttribute("data-liquid-glass-restart");
  root.removeAttribute("data-liquid-glass-reason");
  root.removeAttribute("data-glass-backend");
  root.removeAttribute("data-glass-verified");
  root.removeAttribute("data-glass-selfcheck");
  // 玻璃强度:0=0.5x 1=1x 2=1.6x（模糊半径缩放；标准档 = 1）
  const strength = p.glassStrength === 2 ? 1.6 : p.glassStrength === 0 ? 0.5 : 1;
  root.style.setProperty("--glass-strength", String(strength));
  root.setAttribute("data-glass-strength", String(p.glassStrength ?? 1));
  // plan-26-126 M2：左列不透明度**不再由 JS 写内联变量**，改由 tokens.css 按
  //   data-glass / data-glass-strength / data-theme / data-glass-degraded 声明式驱动。
  //   原因：内联自定义属性优先级高于样式表，若在此写入浅色档数值，用户切换深浅主题时
  //   那些数值不会重算（applyUiVars 并不随主题变化调用），深色档因此会失效。
  //   这里只负责落下 data-glass-strength（三档），具体 alpha 交给 CSS。
  // plan-26-126 P1：毛玻璃改为**重启后生效**（用户明确要求）。
  // 为何不再即时生效：运行期切换需要翻转窗口底色 + 重建材质，会让 DWM 合成与
  //   渲染层缓存失步（左侧面板异常色块 / 设置页残留浅残影，**只有重启才恢复**）；
  //   且 applyUiVars 在每次偏好变更时都会走到这里（拖滑杆 / 提交面板宽度 / 切语言），
  //   于是就变成持续重创合成管线 → 界面无响应数秒。
  // 因此这里只把偏好告知主进程（**仅落盘**），实际效果由下次启动建窗时一次性应用；
  // 需重启时落 data-glass-restart 属性并写 store，供设置页提示（带一键重启）。
  try {
    const api = window.chatcoderAPI;
    if (api?.setGlassMode) {
      void api.setGlassMode(p.glassmorphism).then((res) => {
        const r = (res || {}) as { ok?: boolean; needRestart?: boolean };
        if (p.glassmorphism && r.ok === false) root.setAttribute("data-glass-degraded", "1");
        else root.removeAttribute("data-glass-degraded");
        const needRestart = r.needRestart === true;
        if (needRestart) root.setAttribute("data-glass-restart", "1");
        else root.removeAttribute("data-glass-restart");
        // glassNeedRestart = "当前确实需要重启"的事实态；glassRestartNotice = "这次切换
        // 还没被用户看到"的一次性展示态。后者只在用户**主动拨开关**那一次置位：
        // applyUiVars 在每次偏好变更（拖滑杆 / 改面板宽度 / 切语言）时都会走到这里，
        // 若无条件置位，用户离开外观页后改别的设置又会把徽标顶回来，就不再是"只提示一次"。
        const patch: { glassNeedRestart: boolean; glassRestartNotice?: boolean } = {
          glassNeedRestart: needRestart,
        };
        if (opts?.glassToggle) {
          patch.glassRestartNotice = needRestart;
        }
        useUiStore.setState(patch);
      }).catch(() => { /* 忽略：能力探测失败不阻断 */ });
    }
  } catch { /* ignore */ }
  // 玻璃渐变主色（空则使用主题默认）
  if (p.glassGradientC1) root.style.setProperty("--ambient-c1", p.glassGradientC1);
  else root.style.removeProperty("--ambient-c1");
  if (p.glassGradientC2) root.style.setProperty("--ambient-c2", p.glassGradientC2);
  else root.style.removeProperty("--ambient-c2");
  // v1.1: 阴影强度（此前完全未应用）
  root.style.setProperty("--shadow-strength", String(p.shadowStrength));
  // 消息块间距固定为舒适档（10px）：AI 消息流项间距由 --flow-gap 控制（.turn-flow gap），
  // --msg-gap 作用于独立消息项；其余行距由外观页「消息行距」（--chat-line-height）控制。
  root.style.setProperty("--msg-gap", "10px");
  root.style.removeProperty("--flow-gap");
  root.setAttribute("data-lang", p.language);
  // plan-248-1258 M5: 动画效果档位落到根节点属性，motion.css 据此降级；
  // 系统 prefers-reduced-motion 与「减弱」等价（在 CSS 侧已分别处理）。
  root.setAttribute("data-motion", p.motionLevel || "full");
}

export const FONT_LABELS: Record<string, { zh: string; en: string }> = {  system: { zh: "系统默认", en: "System Default" },
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
  /** v1.1: 后端全局设置缓存（show_todos 等） */
  showTodos: boolean;
  /** 终端字体（设置 → 常规 → 终端字体；空 = 使用默认字体）；变更后已打开的终端即时生效 */
  terminalFont: string;
  /** plan-156-619 S2：消息区搜索入口按钮（左上角）；默认关闭，关闭只隐藏入口，Ctrl+F 仍可用 */
  showMessageSearch: boolean;
  refreshGlobalFlags: () => Promise<void>;
  /** plan-26-126 M5：本次开启玻璃**未即时生效**（极少数环境需重启）——
   *  仅用于设置页一次性提示，不持久化、不常驻。 */
  glassNeedRestart: boolean;
  /** 「需重启生效」徽标的一次性展示开关：仅在用户主动切换毛玻璃开关时置位，
   *  离开外观页（组件卸载）即消费——离开过该页面就不再提示。 */
  glassRestartNotice: boolean;
  /** 消费本次提醒（外观页卸载时调用） */
  consumeGlassRestartNotice: () => void;
}

export const useUiStore = create<UiState>((set, get) => ({
  ...loadPrefs(),

  // v1.1: 消息流显示开关默认开，启动后由后端全局设置刷新
  showTodos: true,
  terminalFont: "",
  // plan-156-619 S2：搜索入口默认关闭（用户要求），由后端全局设置刷新
  showMessageSearch: false,
  glassNeedRestart: false, // plan-26-126 M5：瞬时状态，由 setGlassMode 回包驱动
  glassRestartNotice: false, // 毛玻璃「重启生效」徽标的一次性展示开关
  consumeGlassRestartNotice: () => set({ glassRestartNotice: false }),
  refreshGlobalFlags: async () => {
    try {
      // 动态 import 避免循环依赖（ui.ts 与 api/client 相互独立但被各组件引用）
      const { api } = await import("../api/client");
      const data = await api.getGlobalSettings();
      set({
        showTodos: data.show_todos !== false,
        showMessageSearch: data.show_message_search === true,
        terminalFont: data.terminal_font || "",
      });
    } catch { /* 后端不可达时保持默认 */ }
  },

  setLeftPanelWidth: (w) => {
    // plan-404-1892（图1）：下限与 App.tsx 的 ResizeHandle / tokens.css 的 --sidebar-min 保持 240 一致
    const clamped = Math.max(240, Math.min(640, Math.round(w)));
    set({ leftPanelWidth: clamped });
    savePrefs({ ...get(), leftPanelWidth: clamped });
    applyUiVars({ ...get(), leftPanelWidth: clamped });
  },

  /** 右面板宽度的**唯一数据源**是 panel store（`usePanelStore.width`）。
   *  本字段为遗留投影：历史上此处与拖拽写入的 panel store 各维护一份，形成两处独立读写，
   *  是「拖拽结束后分割线 / 内容布局更新不同步」的竞态来源之一。
   *  现在统一转发到 panel store，保证任何写入方最终都落在同一个状态上。 */
  setRightPanelWidth: (w) => {
    const clamped = Math.max(200, Math.min(1200, Math.round(w)));
    set({ rightPanelWidth: clamped });
    savePrefs({ ...get(), rightPanelWidth: clamped });
    // 动态 import 避免模块循环（ui.ts ↔ panel.ts）
    void import("./panel")
      .then(({ usePanelStore }) => usePanelStore.getState().setWidth(clamped))
      .catch(() => { /* 转发失败不阻塞：下次拖拽/设置仍会写入 */ });
  },

  setPrefs: (partial) => {
    const next = { ...get(), ...partial };
    set(partial);
    savePrefs(next);
    // 只有显式改毛玻璃开关才算"用户主动切换"——徽标仅在此时点亮
    applyUiVars(next, { glassToggle: partial.glassmorphism !== undefined });
  },

  toggleGlass: () => {
    const next = { ...get(), glassmorphism: !get().glassmorphism };
    set({ glassmorphism: next.glassmorphism });
    savePrefs(next);
    applyUiVars(next, { glassToggle: true });
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
