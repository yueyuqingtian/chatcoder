/** 项目外观元数据（plan-20261009-ui U1）：图标 key 与色板 key 都会落库（projects.icon / projects.color），
 *  只能新增不能改名。颜色以「名称」落库而非色值——同一名称在明暗主题下取不同色值（tokens.css --pc-*）。 */
import { PROJECT_ICON_DEFS } from "./projectIconData";

export const PROJECT_ICON_KEYS = PROJECT_ICON_DEFS.map((d) => d.key);
export const DEFAULT_PROJECT_ICON = "folder";

export interface ProjectColorDef { key: string; label: string }

/** 13 色（顺序即选择器顺序；graphite 为旧项目/未设置时的中性外观） */
export const PROJECT_COLORS: ProjectColorDef[] = [
  { key: "forest", label: "松绿" },
  { key: "green", label: "翠绿" },
  { key: "lime", label: "青柠" },
  { key: "teal", label: "湖青" },
  { key: "cyan", label: "天青" },
  { key: "blue", label: "海蓝" },
  { key: "indigo", label: "靛蓝" },
  { key: "violet", label: "紫罗兰" },
  { key: "pink", label: "樱粉" },
  { key: "red", label: "朱红" },
  { key: "orange", label: "橙" },
  { key: "amber", label: "琥珀" },
  { key: "graphite", label: "石墨" },
];

const COLOR_KEYS = new Set(PROJECT_COLORS.map((c) => c.key));
const ICON_KEYS = new Set(PROJECT_ICON_KEYS);

/** 落库值 → 可渲染值（未知/空值回落到中性默认，保证旧项目外观不变） */
export function projectColorKey(color: string | null | undefined): string {
  return color && COLOR_KEYS.has(color) ? color : "graphite";
}
export function projectIconKey(icon: string | null | undefined): string {
  return icon && ICON_KEYS.has(icon) ? icon : DEFAULT_PROJECT_ICON;
}
export function projectColorVar(color: string | null | undefined): string {
  return `var(--pc-${projectColorKey(color)})`;
}

/** 新建项目的默认颜色：按色板顺序取第一个尚未被已有项目使用的颜色（都用过则从头轮转），
 *  让连续新建的项目天然有区分度，又比随机色可预期。 */
export function suggestProjectColor(used: Array<string | null | undefined>): string {
  const taken = new Set(used.filter(Boolean));
  const order = ["blue", "green", "violet", "orange", "teal", "pink", "amber", "indigo", "red", "cyan", "lime", "forest"];
  return order.find((c) => !taken.has(c)) ?? order[used.length % order.length];
}
