/** 设置导航图标（plan-91-393，参考图4：彩色圆角方块 + 白色线稿）。
 * 每枚由「底块 tile + 白色字形 glyph」组成：
 *  · 常规：底块为该分类色，字形 1.7 描边；
 *  · hover：字形内的 .sg-move 部件做一次与含义相关的微动（齿轮转、画笔摆、柱状图长高…）；
 *  · 选中：底块轻微放大并加内高光。
 * 颜色取 --pc-* 项目色板（明暗主题各自取值），所以深色下不会刺眼。 */
import type { ReactNode } from "react";

export type SettingsGlyphKey =
  | "general" | "appearance" | "pets" | "desktop" | "models" | "memory" | "extensions"
  | "subagents" | "rules" | "hooks" | "policy" | "scheduled" | "usage" | "index"
  | "worktrees" | "diagnostics" | "archive" | "about" | "playground" | "workflows";

const TONE: Record<SettingsGlyphKey, string> = {
  general: "graphite", appearance: "violet", pets: "pink", desktop: "blue", models: "indigo",
  memory: "teal", extensions: "orange", subagents: "cyan", rules: "forest", hooks: "amber",
  policy: "red", scheduled: "green", usage: "blue", index: "lime", worktrees: "violet",
  diagnostics: "orange", archive: "graphite", about: "cyan", playground: "amber", workflows: "indigo",
};

/** 字形：24 网格，白色描边；.sg-move 为 hover 微动部件 */
const GLYPH: Record<SettingsGlyphKey, ReactNode> = {
  general: <><circle cx="12" cy="12" r="2.6" /><path className="sg-move sg-spin" d="M12 4.5v2.2M12 17.3v2.2M4.5 12h2.2M17.3 12h2.2M6.7 6.7l1.6 1.6M15.7 15.7l1.6 1.6M6.7 17.3l1.6-1.6M15.7 8.3l1.6-1.6" /></>,
  appearance: <><path d="M12 4.5a7.5 7.5 0 1 0 0 15c1 0 1.6-.8 1.3-1.7-.4-1 .3-2 1.3-2h1.6a3.3 3.3 0 0 0 3.3-3.3C19.5 7.8 16.2 4.5 12 4.5z" /><g className="sg-move sg-pop"><circle cx="8.5" cy="11" r="1" fill="#fff" /><circle cx="11.5" cy="8" r="1" fill="#fff" /><circle cx="15.3" cy="9.2" r="1" fill="#fff" /></g></>,
  pets: <><path d="M7.5 18.5c0-2.6 2-4.5 4.5-4.5s4.5 1.9 4.5 4.5c0 1-1 1.4-2 1-1.6-.6-3.4-.6-5 0-1 .4-2 0-2-1z" /><g className="sg-move sg-hop"><circle cx="7" cy="10.5" r="1.6" /><circle cx="10.2" cy="7.2" r="1.6" /><circle cx="13.8" cy="7.2" r="1.6" /><circle cx="17" cy="10.5" r="1.6" /></g></>,
  desktop: <><rect x="4" y="5" width="16" height="11" rx="2" /><path d="M9.5 19.5h5M12 16v3.5" /><path className="sg-move sg-nudge" d="M10.5 8.5l3.5 2-1.6.5-.6 1.6z" fill="#fff" /></>,
  models: <><rect x="6.5" y="6.5" width="11" height="11" rx="2.5" /><path d="M9.5 4v2.5M14.5 4v2.5M9.5 17.5V20M14.5 17.5V20M4 9.5h2.5M4 14.5h2.5M17.5 9.5H20M17.5 14.5H20" /><rect className="sg-move sg-pop" x="9.8" y="9.8" width="4.4" height="4.4" rx="1" fill="#fff" stroke="none" /></>,
  memory: <><path d="M9 5.5a3 3 0 0 0-3 3v.3a3 3 0 0 0-1 5.5 3 3 0 0 0 4 3.9V5.5zM15 5.5a3 3 0 0 1 3 3v.3a3 3 0 0 1 1 5.5 3 3 0 0 1-4 3.9V5.5z" /><path className="sg-move sg-pop" d="M12 6v12" /></>,
  extensions: <><path d="M10 4.5h4v2.2a1.6 1.6 0 1 0 3.2 0V4.5" /><rect x="4.5" y="8" width="15" height="11.5" rx="2.5" /><path className="sg-move sg-nudge" d="M9.5 13.5h5" /></>,
  subagents: <><circle cx="9" cy="9" r="2.8" /><path d="M4 18.5c.5-2.8 2.5-4.5 5-4.5s4.5 1.7 5 4.5" /><g className="sg-move sg-slide"><circle cx="16.5" cy="9.5" r="2.2" /><path d="M15.5 14.2c2.2-.1 4 1.4 4.5 4.3" /></g></>,
  rules: <><path d="M6 4.5h9.5L18.5 7.5v12H6z" /><path className="sg-move sg-nudge" d="M9 11h6.5M9 14h6.5M9 17h4" /></>,
  hooks: <><circle cx="12" cy="6" r="1.8" /><path d="M12 7.8v10.7" /><path className="sg-move sg-swing" d="M6 13.5a6 6 0 0 0 12 0M5 15l1-1.7 1.8 1M19 15l-1-1.7-1.8 1" /></>,
  policy: <><path d="M12 4l6.5 2.5v5c0 4-2.8 7-6.5 8.5-3.7-1.5-6.5-4.5-6.5-8.5v-5z" /><path className="sg-move sg-draw" d="M9 12l2.2 2.2L15.2 10" /></>,
  scheduled: <><rect x="4.5" y="5.5" width="15" height="14" rx="2.5" /><path d="M8.5 4v3M15.5 4v3M4.5 10h15" /><path className="sg-move sg-pop" d="M12.8 11.5L10.5 15h3l-.8 3" /></>,
  usage: <><path d="M4.5 19.5h15" /><g className="sg-move sg-grow"><path d="M7.5 16.5v-4M12 16.5v-9M16.5 16.5V10" /></g></>,
  index: <><ellipse cx="12" cy="6.5" rx="6.5" ry="2.5" /><path d="M5.5 6.5v11c0 1.4 2.9 2.5 6.5 2.5s6.5-1.1 6.5-2.5v-11" /><path className="sg-move sg-nudge" d="M5.5 12c0 1.4 2.9 2.5 6.5 2.5s6.5-1.1 6.5-2.5" /></>,
  worktrees: <><circle cx="7" cy="6" r="2" /><circle cx="7" cy="18" r="2" /><path d="M7 8v8" /><g className="sg-move sg-slide"><circle cx="17" cy="8" r="2" /><path d="M17 10c0 4-4 4-10 6" /></g></>,
  diagnostics: <><path d="M4 12h3.5l2-4.5 3.5 9 2-4.5H20" className="sg-move sg-draw" /></>,
  archive: <><rect x="4" y="5" width="16" height="4" rx="1.2" /><path d="M5.5 9v8.5a2 2 0 0 0 2 2h9a2 2 0 0 0 2-2V9" /><path className="sg-move sg-hop" d="M10 13h4" /></>,
  about: <><circle cx="12" cy="12" r="7.5" /><path d="M12 11v5" /><circle className="sg-move sg-hop" cx="12" cy="8.2" r="1" fill="#fff" stroke="none" /></>,
  // 工作流：三节点分支，末端节点沿分支滑入（hover 微动）
  workflows: <><circle cx="6" cy="6" r="2.2" /><circle cx="6" cy="18" r="2.2" /><g className="sg-move sg-slide"><circle cx="17.5" cy="12" r="2.2" /></g><path d="M6 8.2v7.6M8.2 6.4c4.2 0 5.6 2 7 4.2M8.2 17.6c4.2 0 5.6-2 7-4.2" /></>,
  // plan-114-449：乐园玩法是「字块堆叠」，图标即两块垫底 + 一块跳上顶
  playground: <><rect x="4.5" y="13" width="6.5" height="6.5" rx="1.5" /><rect x="13" y="13" width="6.5" height="6.5" rx="1.5" /><rect className="sg-move sg-hop" x="8.75" y="4.5" width="6.5" height="6.5" rx="1.5" /></>,
};

export function SettingsGlyph({ name, size = 22 }: { name: SettingsGlyphKey; size?: number }) {
  return (
    <span className="sg-tile" data-tone={TONE[name]} style={{ width: size, height: size }} aria-hidden>
      <svg width={size - 6} height={size - 6} viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
        {GLYPH[name]}
      </svg>
    </span>
  );
}
