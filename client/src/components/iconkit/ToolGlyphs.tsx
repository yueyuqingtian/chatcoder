/** 消息流工具调用图标（plan-91-393）。
 * 与左侧/右面板的线性交互图标同一笔触（24 网格、1.6 描边、圆端点），但按「工具语义」单独设计，
 * 并带三种状态（由宿主 .tc-row 上的 data-tool-state 驱动，见 styles/flow.css「工具图标状态」）：
 *  · running：主体部件循环做与动作相关的动画（读取：扫描线下移；编辑：笔尖来回；终端：光标闪；搜索：镜头绕圈…）；
 *  · done：静止线稿，hover 展开行时部件微动一次；
 *  · fail：线色转为 --error。 */
import type { ReactNode } from "react";
import { opticalStroke } from "../icons";

type K =
  | "read" | "write" | "patch" | "list" | "grep" | "term" | "web" | "search" | "git"
  | "agent" | "ask" | "collect" | "ci" | "memory" | "image" | "todo" | "question" | "mcp" | "think" | "other";

const G: Record<K, ReactNode> = {
  read: <><path d="M6 3.5h8l4 4V20.5H6z" /><path d="M14 3.5v4h4" /><path className="tg-scan" d="M8.5 12h7M8.5 15h7" /></>,
  write: <><path d="M6 3.5h7M6 3.5V20.5h12V11" /><path className="tg-pen" d="M19.5 4.5l-7 7-.8 2.8 2.8-.8 7-7z" /></>,
  patch: <><path d="M6 3.5h8l4 4V20.5H6z" /><path className="tg-pen" d="M12 10v6M9 13h6" /></>,
  list: <><path d="M3.5 7.5v10a2 2 0 0 0 2 2h13a2 2 0 0 0 2-2v-8a2 2 0 0 0-2-2h-7l-2-2.5h-4a2 2 0 0 0-2 2z" /><path className="tg-scan" d="M8 13h8" /></>,
  grep: <><path d="M5 5.5h9M5 10h6M5 14.5h4" /><g className="tg-orbit"><circle cx="15.5" cy="15" r="3.5" /><path d="M18 17.5l2.5 2.5" /></g></>,
  term: <><rect x="3.5" y="4.5" width="17" height="15" rx="2.5" /><path d="M7.5 9.5l3 2.5-3 2.5" /><path className="tg-blink" d="M13 15h4" /></>,
  web: <><circle cx="12" cy="12" r="8" /><path d="M4 12h16" /><ellipse className="tg-turn" cx="12" cy="12" rx="3.5" ry="8" /></>,
  search: <><g className="tg-orbit"><circle cx="10.5" cy="10.5" r="5.5" /><path d="M14.5 14.5L19.5 19.5" /></g></>,
  git: <><circle cx="7" cy="6" r="2" /><circle cx="7" cy="18" r="2" /><circle cx="17" cy="8" r="2" /><path d="M7 8v8" /><path className="tg-flow" d="M17 10c0 4-4 4-10 6" /></>,
  agent: <><rect x="4.5" y="8" width="15" height="11" rx="3" /><path d="M9.5 13h.01M14.5 13h.01" strokeWidth={2.4} /><g className="tg-hop"><path d="M12 8V5.5" /><circle cx="12" cy="4.5" r="1.1" /></g></>,
  ask: <><path d="M4.5 6.5a2 2 0 0 1 2-2h11a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H11l-4 3.5v-3.5h-.5a2 2 0 0 1-2-2z" /><path className="tg-blink" d="M9 10h.01M12 10h.01M15 10h.01" strokeWidth={2.2} /></>,
  collect: <><path d="M4 8.5l8-4 8 4-8 4z" /><path d="M4 12.5l8 4 8-4" /><path className="tg-drop" d="M4 16.5l8 4 8-4" /></>,
  ci: <><path d="M9.5 3.5h5M10.5 3.5v6l-5 9a1.4 1.4 0 0 0 1.2 2h10.6a1.4 1.4 0 0 0 1.2-2l-5-9v-6" /><circle className="tg-bubble" cx="11" cy="16" r="1" /><circle className="tg-bubble tg-d2" cx="14" cy="14" r=".8" /></>,
  memory: <><path d="M9 5.5a3 3 0 0 0-3 3v.3a3 3 0 0 0-1 5.5 3 3 0 0 0 4 3.9V5.5zM15 5.5a3 3 0 0 1 3 3v.3a3 3 0 0 1 1 5.5 3 3 0 0 1-4 3.9V5.5z" /><path className="tg-pulse" d="M12 6v12" /></>,
  image: <><rect x="3.5" y="4.5" width="17" height="15" rx="2.5" /><circle className="tg-hop" cx="9" cy="9.5" r="1.6" /><path d="M20.5 15.5l-5-5-9 9" /></>,
  todo: <><path d="M11 7h8.5M11 12h8.5M11 17h6" /><path className="tg-draw" d="M4.5 7l1.3 1.3L8 6M4.5 12l1.3 1.3L8 11M4.5 17l1.3 1.3L8 16" /></>,
  question: <><circle cx="12" cy="12" r="8" /><path className="tg-hop" d="M9.8 9.8a2.3 2.3 0 0 1 4.4.9c0 1.5-2.2 2-2.2 3.3" /><path d="M12 16.8h.01" strokeWidth={2.2} /></>,
  mcp: <><path d="M9 4.5v4M15 4.5v4" /><path d="M6.5 8.5h11v3a5.5 5.5 0 0 1-11 0z" /><path className="tg-flow" d="M12 17v3" /></>,
  think: <><path d="M12 4.5a5.5 5.5 0 0 0-3.2 10l.2 2.5h6l.2-2.5A5.5 5.5 0 0 0 12 4.5z" /><path d="M9.5 20h5" /><path className="tg-pulse" d="M12 8.5v4" /></>,
  other: <><path className="tg-pulse" d="M13 3.5L6 13h5l-1 7.5L18 11h-5z" /></>,
};

const TOOL_KEY: Record<string, K> = {
  fs_read: "read", fs_write: "write", editor_apply_diff: "patch", fs_list: "list", fs_grep: "grep",
  terminal_exec: "term", web_fetch: "web", web_search: "search", codebase_search: "search",
  git_diff: "git", git_root: "git", spawn_subagent: "agent", ask_subagent: "agent",
  collect_results: "collect", ci_run: "ci", memory_search: "memory", view_image: "image",
  todo_write: "todo", ask_user_question: "question", mcp: "mcp", thinking: "think",
};

export function toolGlyphKey(tool: string): K {
  if (TOOL_KEY[tool]) return TOOL_KEY[tool];
  if (tool.startsWith("mcp")) return "mcp";
  if (tool.startsWith("db_")) return "collect";
  return "other";
}

export function ToolGlyph({ tool, size = 16 }: { tool: string; size?: number }) {
  const k = toolGlyphKey(tool);
  return (
    // plan-20261014-fix4：固定 1.6 单位在 13~16px 下实际线宽不足 1.1px，改为按尺寸换算的光学线宽
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={opticalStroke(size)}
      strokeLinecap="round" strokeLinejoin="round" aria-hidden className={`ic tg tg-${k}`}>
      {G[k]}
    </svg>
  );
}
