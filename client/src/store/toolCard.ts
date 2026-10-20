/** 会话右上角综合工具卡的状态（plan-156-619 S7；plan-20261019-toolcard-v2 重做）。
 *
 * 【为什么单独一个 store】
 * 工具卡有「会话级形态与栏目折叠」「全局默认」两层，
 * 前者必须按会话独立记忆（用户要求：重启前有效），后者来自后端全局设置（跨重启）。
 * 塞进 chat store 会让会话切片与 UI 偏好混在一起，切换会话时容易互相覆盖。
 *
 * 【记忆口径】
 * · `expandedBySession` / `sectionsBySession`：纯内存，进程重启即失效；
 *   会话里**没有**本地记录时，形态与各栏目的初值取全局默认（`defaultForm` / `sectionDefaults`）；
 * · `defaultForm` / `sectionDefaults`：来自 `GET /settings/global` 的
 *   `tool_card_default_form` / `tool_card_section_defaults`（缺省的栏目回落旧布尔
 *   `tool_card_sections_expanded`），同时缓存在 localStorage——首屏先按缓存渲染，
 *   避免等接口返回期间卡片先按「胶囊 / 全展开」画一帧、再跳成用户配置的样子；
 * · 改默认值 = 「修改即保存」：立即写 store + 缓存 + 后端，并清掉会话级手动记录——
 *   用户在菜单里改默认时期望眼前的卡片立刻照着变，而不是只影响「还没碰过的会话」。
 */
import { create } from "zustand";
import { api } from "../api/client";

/** 六个栏目。顺序即展示顺序（Git → 计划 → 进程 → 子代理 → 工作流 → 后台任务）。
 *  plan-20261020-bgtask：新增「后台任务」——运行中的后台命令（dev server、watch、耗时构建），带运行时间与停止按钮。 */
export type ToolCardSection = "git" | "plan" | "process" | "subagent" | "workflow" | "bgtask";

export const TOOL_CARD_SECTIONS: readonly ToolCardSection[] = [
  "git", "plan", "process", "subagent", "workflow", "bgtask",
] as const;

/** 折叠态胶囊只显示一项时的优先级（用户指定）：进程 > 工作流 > 子代理 > 后台任务 > 计划 > git。 */
export const CAPSULE_PRIORITY: readonly ToolCardSection[] = [
  "process", "workflow", "subagent", "bgtask", "plan", "git",
] as const;

export const SECTION_LABEL: Record<ToolCardSection, string> = {
  git: "Git 工具",
  plan: "计划",
  process: "进程",
  subagent: "子代理",
  workflow: "工作流",
  bgtask: "后台任务",
};

/** 工具卡默认形态：capsule = 右上角悬浮胶囊；expanded = 展开卡片。 */
export type ToolCardDefaultForm = "capsule" | "expanded";

export type SectionMap = Record<ToolCardSection, boolean>;

const fillSections = (v: boolean): SectionMap => ({
  git: v, plan: v, process: v, subagent: v, workflow: v, bgtask: v,
});

interface Prefs {
  defaultForm: ToolCardDefaultForm;
  sectionDefaults: SectionMap;
}

const PREFS_KEY = "chatcoder.toolcard.prefs";
const DEFAULT_PREFS: Prefs = { defaultForm: "capsule", sectionDefaults: fillSections(true) };

/** 首屏缓存：读失败 / 脏数据一律回落默认，绝不抛。cached = 本机确实存过用户的选择。 */
function loadCachedPrefs(): Prefs & { cached: boolean } {
  try {
    const raw = typeof localStorage !== "undefined" ? localStorage.getItem(PREFS_KEY) : null;
    if (!raw) return { ...DEFAULT_PREFS, cached: false };
    const parsed = JSON.parse(raw) as Partial<Prefs> | null;
    const sections = fillSections(true);
    for (const key of TOOL_CARD_SECTIONS) {
      const v = parsed?.sectionDefaults?.[key];
      if (typeof v === "boolean") sections[key] = v;
    }
    return {
      defaultForm: parsed?.defaultForm === "expanded" ? "expanded" : "capsule",
      sectionDefaults: sections,
      cached: true,
    };
  } catch {
    return { ...DEFAULT_PREFS, cached: false };
  }
}

function cachePrefs(p: Prefs): void {
  try {
    localStorage.setItem(PREFS_KEY, JSON.stringify(p));
  } catch { /* 存储不可用时只在本次生效 */ }
}

/** 后端全局设置里与工具卡相关的字段（见 GlobalSettingsOut）。 */
interface PrefsFromServer {
  tool_card_sections_expanded?: boolean;
  tool_card_default_form?: string;
  tool_card_section_defaults?: Partial<Record<ToolCardSection, boolean>>;
}

interface ToolCardState {
  /** 会话 id → 卡片是否展开（内存记忆；未记录时取全局默认形态） */
  expandedBySession: Record<number, boolean>;
  /** 会话 id → 各栏目是否展开（内存记忆；未记录时取逐栏默认） */
  sectionsBySession: Record<number, SectionMap>;
  /** 全局默认形态 */
  defaultForm: ToolCardDefaultForm;
  /** 全局逐栏默认展开 */
  sectionDefaults: SectionMap;
  /** 是否已从后端读过全局设置（只读一次，避免每次挂载卡片都请求设置接口） */
  hydrated: boolean;
  /** 本机是否存过用户的选择（localStorage）——老后端不回新字段时，决定要不要沿用旧布尔 */
  prefsCached: boolean;
  markHydrated: () => void;
  /** 从后端全局设置注水：只改全局默认，不清会话记录 */
  hydratePrefs: (s: PrefsFromServer) => void;
  /** 改全局默认形态（清掉会话级形态记录，立即生效） */
  setDefaultForm: (form: ToolCardDefaultForm) => void;
  /** 改某栏目的全局默认（清掉会话级栏目记录，立即生效） */
  setSectionDefault: (section: ToolCardSection, expanded: boolean) => void;
  /** 切换卡片形态 */
  setExpanded: (sessionId: number, expanded: boolean) => void;
  /** 切换某栏目展开态（首次操作时用全局默认补齐其余栏目） */
  toggleSection: (sessionId: number, section: ToolCardSection) => void;
}

const cached = loadCachedPrefs();

export const useToolCardStore = create<ToolCardState>((set, get) => ({
  expandedBySession: {},
  sectionsBySession: {},
  defaultForm: cached.defaultForm,
  sectionDefaults: cached.sectionDefaults,
  hydrated: false,
  prefsCached: cached.cached,
  markHydrated: () => set({ hydrated: true }),

  hydratePrefs: (s) => {
    const cur = get();
    // 新后端的响应里一定带 tool_card_default_form / tool_card_section_defaults（哪怕是默认值）：以后端为准。
    // 老后端不认识这两个字段、原样不回——此时不能拿「缺省」把用户本机缓存的选择冲掉
    // （前端比后端新是开发时的常态：前端热更新，后端还是已安装的版本）。
    const newBackend = s.tool_card_default_form !== undefined || s.tool_card_section_defaults !== undefined;
    let defaultForm = cur.defaultForm;
    let sectionDefaults = cur.sectionDefaults;
    if (newBackend) {
      defaultForm = s.tool_card_default_form === "expanded" ? "expanded" : "capsule";
      const legacy = typeof s.tool_card_sections_expanded === "boolean" ? s.tool_card_sections_expanded : true;
      sectionDefaults = fillSections(legacy);
      for (const key of TOOL_CARD_SECTIONS) {
        const v = s.tool_card_section_defaults?.[key];
        if (typeof v === "boolean") sectionDefaults[key] = v;
      }
    } else if (!cur.prefsCached && typeof s.tool_card_sections_expanded === "boolean") {
      // 老后端 + 本机没存过选择：沿用旧的全局布尔（用户在旧版菜单里选过「全部折叠」）
      sectionDefaults = fillSections(s.tool_card_sections_expanded);
    }
    const next: Prefs = { defaultForm, sectionDefaults };
    cachePrefs(next);
    set({ ...next, prefsCached: true });
  },

  setDefaultForm: (form) => {
    const next: Prefs = { defaultForm: form, sectionDefaults: get().sectionDefaults };
    cachePrefs(next);
    set({ defaultForm: form, expandedBySession: {} });
  },

  setSectionDefault: (section, expanded) => {
    const sectionDefaults = { ...get().sectionDefaults, [section]: expanded };
    cachePrefs({ defaultForm: get().defaultForm, sectionDefaults });
    set({ sectionDefaults, sectionsBySession: {} });
  },

  setExpanded: (sessionId, expanded) => set((s) => ({
    expandedBySession: { ...s.expandedBySession, [sessionId]: expanded },
  })),

  toggleSection: (sessionId, section) => set((s) => {
    const base = s.sectionsBySession[sessionId] ?? s.sectionDefaults;
    return {
      sectionsBySession: {
        ...s.sectionsBySession,
        [sessionId]: { ...base, [section]: !base[section] },
      },
    };
  }),
}));

/** 订阅某会话的栏目展开表。
 *
 * 旧写法订阅的是**函数引用本身**——它永远不变，toggleSection 改了 sectionsBySession 组件也不会重渲染，
 * 表现为「栏目折叠按钮点了没反应」。这里订阅真正的数据字段。 */
export function useSectionsOf(sessionId: number | null): SectionMap {
  const own = useToolCardStore((s) => (sessionId == null ? undefined : s.sectionsBySession[sessionId]));
  const defaults = useToolCardStore((s) => s.sectionDefaults);
  return own ?? defaults;
}

/** 订阅某会话是否想要展开卡片：会话里有手动记录取记录，否则取全局默认形态。 */
export function useWantExpanded(sessionId: number | null): boolean {
  const own = useToolCardStore((s) => (sessionId == null ? undefined : s.expandedBySession[sessionId]));
  const defaultForm = useToolCardStore((s) => s.defaultForm);
  return own ?? defaultForm === "expanded";
}

/** 修改全局默认并「修改即保存」：先写 store（界面立即跟随），再落后端；落库失败只影响下次启动。 */
export function saveToolCardForm(form: ToolCardDefaultForm): void {
  useToolCardStore.getState().setDefaultForm(form);
  void api.setGlobalSettings({ tool_card_default_form: form }).catch(() => { /* 落库失败仅影响下次启动 */ });
}

export function saveToolCardSection(section: ToolCardSection, expanded: boolean): void {
  const store = useToolCardStore.getState();
  store.setSectionDefault(section, expanded);
  void api.setGlobalSettings({ tool_card_section_defaults: { ...useToolCardStore.getState().sectionDefaults } })
    .catch(() => { /* 落库失败仅影响下次启动 */ });
}

/** 从后端读一次全局默认（工具卡与设置页都可能先挂载，谁先到谁读，其余复用）。 */
export function hydrateToolCardPrefs(): void {
  const store = useToolCardStore.getState();
  if (store.hydrated) return;
  store.markHydrated();
  void api.getGlobalSettings()
    .then((g) => useToolCardStore.getState().hydratePrefs(g))
    .catch(() => { /* 读不到就沿用缓存 / 默认 */ });
}
