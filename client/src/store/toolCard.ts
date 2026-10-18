/** 会话右上角综合工具卡的状态（plan-156-619 S7）。
 *
 * 【为什么单独一个 store】
 * 工具卡有「会话级展开状态」「栏目级折叠状态」「全局默认策略」三层，
 * 前两层必须按会话独立记忆（用户要求：重启前有效），第三层来自后端设置（跨重启）。
 * 塞进 chat store 会让会话切片与 UI 偏好混在一起，切换会话时容易互相覆盖。
 *
 * 【记忆口径】
 * · `expandedBySession` / `sectionsBySession`：纯内存，进程重启即失效（符合用户要求）；
 * · `globalSectionsExpanded`：来自 `GET /settings/global` 的 `tool_card_sections_expanded`，
 *   **只作为「该会话还没有本地记录时」的初始值**——否则用户改一次全局策略，
 *   会把所有会话里手工折叠过的栏目一起清掉。
 */
import { create } from "zustand";

/** 五个栏目。顺序即展示顺序（Git → 计划 → 进程 → 子代理 → 工作流）。 */
export type ToolCardSection = "git" | "plan" | "process" | "subagent" | "workflow";

export const TOOL_CARD_SECTIONS: readonly ToolCardSection[] = [
  "git", "plan", "process", "subagent", "workflow",
] as const;

/** 折叠态胶囊只显示一项时的优先级（用户指定）：进程 > 工作流 > 子代理 > 计划 > git。 */
export const CAPSULE_PRIORITY: readonly ToolCardSection[] = [
  "process", "workflow", "subagent", "plan", "git",
] as const;

export const SECTION_LABEL: Record<ToolCardSection, string> = {
  git: "Git",
  plan: "计划",
  process: "进程",
  subagent: "子代理",
  workflow: "工作流",
};

type SectionMap = Record<ToolCardSection, boolean>;

const allExpanded = (): SectionMap => ({
  git: true, plan: true, process: true, subagent: true, workflow: true,
});

interface ToolCardState {
  /** 会话 id → 卡片整体是否展开（内存记忆） */
  expandedBySession: Record<number, boolean>;
  /** 会话 id → 各栏目是否展开（内存记忆） */
  sectionsBySession: Record<number, SectionMap>;
  /** 全局默认：栏目是否默认展开（来自后端设置，仅作初始值） */
  globalSectionsExpanded: boolean;
  /** 从全局设置同步默认策略（applyUi/settings 加载完成后调用一次） */
  setGlobalDefault: (expanded: boolean) => void;
  /** 启动时从后端设置注水（只改默认值，不清会话记录） */
  hydrateGlobalDefault: (expanded: boolean) => void;
  /** 是否已从后端读过全局默认（只读一次，避免每次挂载卡片都请求设置接口） */
  hydrated: boolean;
  markHydrated: () => void;
  /** 切换卡片整体展开态 */
  setExpanded: (sessionId: number, expanded: boolean) => void;
  /** 切换某栏目展开态（首次操作时用全局默认补齐其余栏目） */
  toggleSection: (sessionId: number, section: ToolCardSection) => void;
  /** 读某会话的栏目展开表（未记录时返回全局默认，不写 state） */
  sectionsOf: (sessionId: number | null) => SectionMap;
  /** 读某会话卡片是否展开（未记录时默认收起） */
  isExpanded: (sessionId: number | null) => boolean;
}

const allCollapsed = (): SectionMap => ({
  git: false, plan: false, process: false, subagent: false, workflow: false,
});

/** 订阅某会话的栏目展开表（plan-156-620）。
 *
 * 旧写法 `useToolCardStore((s) => s.sectionsOf)(id)` 订阅的是**函数引用本身**——
 * 它永远不变，toggleSection 改了 sectionsBySession 组件也不会重渲染，
 * 表现为「栏目折叠按钮点了没反应」。这里改为订阅真正的数据字段。 */
export function useSectionsOf(sessionId: number | null): SectionMap {
  const own = useToolCardStore((s) => (sessionId == null ? undefined : s.sectionsBySession[sessionId]));
  const globalExpanded = useToolCardStore((s) => s.globalSectionsExpanded);
  if (own) return own;
  return globalExpanded ? allExpanded() : allCollapsed();
}

export const useToolCardStore = create<ToolCardState>((set, get) => ({
  expandedBySession: {},
  sectionsBySession: {},
  globalSectionsExpanded: true,
  hydrated: false,
  markHydrated: () => set({ hydrated: true }),

  /* plan-156-620：改全局策略即时作用于所有会话（清掉会话级手动记录）——
     用户在 ⋯ 菜单里选「默认折叠」时期望眼前的卡片立刻折叠，而不是只影响「还没碰过的会话」。 */
  setGlobalDefault: (expanded) => set({ globalSectionsExpanded: expanded, sectionsBySession: {} }),
  hydrateGlobalDefault: (expanded) => set({ globalSectionsExpanded: expanded }),

  setExpanded: (sessionId, expanded) => set((s) => ({
    expandedBySession: { ...s.expandedBySession, [sessionId]: expanded },
  })),

  toggleSection: (sessionId, section) => set((s) => {
    const base = s.sectionsBySession[sessionId]
      ?? (s.globalSectionsExpanded ? allExpanded() : allCollapsed());
    return {
      sectionsBySession: {
        ...s.sectionsBySession,
        [sessionId]: { ...base, [section]: !base[section] },
      },
    };
  }),

  sectionsOf: (sessionId) => {
    if (sessionId == null) return allExpanded();
    const own = get().sectionsBySession[sessionId];
    if (own) return own;
    // 未记录 → 全局默认（不写 state，避免读操作产生副作用）
    return get().globalSectionsExpanded ? allExpanded() : allCollapsed();
  },

  isExpanded: (sessionId) => {
    if (sessionId == null) return false;
    return get().expandedBySession[sessionId] === true;
  },
}));
