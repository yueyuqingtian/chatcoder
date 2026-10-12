/** 输入框模式 / 权限 / 思考深度的展示元数据（plan-20261009-ui U2）。
 *  单一来源：+ 浮窗、模式胶囊、排队托盘的模式徽标都从这里取图标与文案。 */
import type { ReactNode } from "react";
import type { ApprovalMode } from "../../store/drafts";
import { IconShield } from "../icons";
import { MgAgent, MgAsk, MgAuto, MgFull, MgGoal, MgPlan, MgReadonly } from "../iconkit/ModeGlyphs";

/** 内置执行模式（顺序即 + 浮窗中的顺序：智能体 → 计划 → 只读，最常用在最上） */
export const BUILTIN_MODES = ["agent", "plan", "readonly"] as const;

/** plan-128-518：「目标」是菜单里的第四项，但不是后端的执行模式——它的能力边界等同智能体（会话 permission_mode 仍为 agent），
 *  是否处于目标模式由会话的 goal_status 决定。前端用此伪模式名在菜单里表达选中态。 */
export const GOAL_MODE = "goal";

export interface ModeMeta {
  labelKey: string;
  /** 胶囊上的短名 */
  shortKey: string;
  descKey: string;
  icon: (size?: number) => ReactNode;
  /** 胶囊语义色（agent 中性 / plan 强调 / readonly 弱化） */
  tone: "neutral" | "accent" | "muted";
}

export const MODE_META: Record<string, ModeMeta> = {
  agent: {
    labelKey: "composer.mode_agent",
    shortKey: "composer.mode_agent_short",
    descKey: "composer.mode_agent_desc",
    icon: (s = 16) => <MgAgent size={s} />,
    tone: "neutral",
  },
  plan: {
    labelKey: "composer.mode_plan",
    shortKey: "composer.mode_plan_short",
    descKey: "composer.mode_plan_desc",
    icon: (s = 16) => <MgPlan size={s} />,
    tone: "accent",
  },
  readonly: {
    labelKey: "composer.mode_readonly",
    shortKey: "composer.mode_readonly_short",
    descKey: "composer.mode_readonly_desc",
    icon: (s = 16) => <MgReadonly size={s} />,
    tone: "muted",
  },
  // plan-128-518：目标模式（见 GOAL_MODE 注释）
  [GOAL_MODE]: {
    labelKey: "composer.mode_goal",
    shortKey: "composer.mode_goal_short",
    descKey: "composer.mode_goal_desc",
    icon: (s = 16) => <MgGoal size={s} />,
    tone: "accent",
  },
};

/** 自定义执行模式（权限档案下发）的兜底图标 */
export const customModeIcon = (s = 16) => <IconShield size={s} />;

export const APPROVAL_META: Record<ApprovalMode, { labelKey: string; descKey: string; icon: (s?: number) => ReactNode; danger?: boolean }> = {
  ask: { labelKey: "composer.approval_ask", descKey: "composer.approval_ask_desc", icon: (s = 14) => <MgAsk size={s} /> },
  auto: { labelKey: "composer.approval_auto", descKey: "composer.approval_auto_desc", icon: (s = 14) => <MgAuto size={s} /> },
  full: { labelKey: "composer.approval_full", descKey: "composer.approval_full_desc", icon: (s = 14) => <MgFull size={s} />, danger: true },
};
export const APPROVAL_ORDER: ApprovalMode[] = ["ask", "auto", "full"];

/** 思考深度档位高低序（与设置 → 模型的 REASONING_OPTS 对齐） */
export const EFFORT_RANK: Record<string, number> = {
  none: 0, minimal: 1, low: 2, medium: 3, high: 4, xhigh: 5, max: 6,
};

/** 档位中文名（胶囊与滑块共用；未知档位原样显示） */
export const EFFORT_LABEL: Record<string, string> = {
  none: "关闭", minimal: "极简", low: "低", medium: "中", high: "高", xhigh: "超高", max: "最大",
};

export function effortLabel(e: string | null | undefined): string {
  if (!e) return "默认";
  return EFFORT_LABEL[e] ?? e;
}

/** 按配置从低到高排序（未知档位排最后，保持相对顺序） */
export function sortEfforts(efforts: string[]): string[] {
  return [...efforts].sort((a, b) => (EFFORT_RANK[a] ?? 99) - (EFFORT_RANK[b] ?? 99));
}


