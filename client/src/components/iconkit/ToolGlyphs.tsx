/** 消息流工具调用图标（plan-91-393）。
 * 与左侧/右面板的线性交互图标同一笔触（24 网格、1.6 描边、圆端点），但按「工具语义」单独设计，
 * 并带三种状态（由宿主 .tc-row 上的 data-tool-state 驱动，见 styles/flow.css「工具图标状态」）：
 *  · running：主体部件循环做与动作相关的动画（读取：扫描线下移；编辑：笔尖来回；终端：光标闪；搜索：镜头绕圈…）；
 *  · done：静止线稿，hover 展开行时部件微动一次；
 *  · fail：线色转为 --error。 */
import type { ReactNode } from "react";
import { opticalStroke } from "../icons";
import { normalizeToolName } from "../chat/toolLabels";

type K =
  | "read" | "write" | "patch" | "list" | "grep" | "term" | "web" | "search" | "git"
  | "agent" | "ask" | "collect" | "ci" | "memory" | "image" | "todo" | "question" | "mcp" | "think" | "other"
  // plan-630-2590：浏览器 / 桌面操控工具此前只有「地球」和「闪电」两个落点，
  // 执行脚本、读取正文、读页面结构、截图四类动作在消息流里长得一样，用户看不出这次到底做了什么；
  // 这里为每类动作单独开一个字形键，让「看图标就知道动作性质」重新成立。
  | "script" | "readtext" | "snapshot" | "click" | "type" | "key" | "scroll" | "wait"
  | "clipboard" | "download" | "batch" | "recipe" | "tabs" | "logs"
  /** plan-630-2590 返工：消息流「操作」聚合行专属字形（见 ToolTree 的 ClusterGlyph）。
   *  必须是聚合语义——不能借用任何具体工具的字形，否则展开后明细行会出现同款图标，
   *  用户会以为「这行图标和里面第一条是同一个操作」。 */
  | "cluster";

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

  // ── plan-630-2590：浏览器 / 桌面操控工具的语义字形 ──
  // 全部沿用既有 tg-* 动效类（styles/flow.css 已有规则），不引入需要新 CSS 的部件类名。
  // 执行页面脚本：代码括号 + 斜杠，不能再用地球——地球的语义是「联网 / 抓取」，与执行脚本相反。
  script: <><path d="M8.4 8.2 4.6 12l3.8 3.8" /><path d="M15.6 8.2 19.4 12l-3.8 3.8" /><path className="tg-pen" d="M13.5 7.2l-3 9.6" /></>,
  // 读取网页正文：与 read（带折角的文件）同一家人，但整页 + 三行正文更接近「一页文本」而不是「一个文件」
  readtext: <><rect x="4.5" y="3.5" width="15" height="17" rx="2.5" /><path className="tg-scan" d="M8 8.5h8M8 12h8M8 15.5h4.6" /></>,
  // 页面结构 / 界面元素快照：外框 + 元素块 + 结构行，和「截图」（图片图形）、「读正文」都不同
  snapshot: <><rect x="3.5" y="3.5" width="17" height="17" rx="2.5" /><rect x="7" y="7" width="4.6" height="4.6" rx="1.2" /><path className="tg-scan" d="M14.2 9.3h4.2M7 15h11.4M7 17.8h6.6" /></>,
  // 点击（含悬停 / 拖拽 / 切换窗口这类「指针落到某处」的动作）：指针 + 点击波纹
  click: <><path className="tg-hop" d="M8.2 7.4 18 11.4l-4.2 1.5-1.5 4.2z" /><path className="tg-pulse" d="M4.6 4.6l1.7 1.7M4 9.7h2.3M9.7 4v2.3" /></>,
  // 输入文字（含填写表单）：文字行 + 闪烁的插入光标，光标带上下衬线才像「输入点」而不是「一行文字」
  type: <><path d="M4.5 8h8M4.5 12.5h5.6" /><path className="tg-blink" d="M16.4 6.5v11" /><path d="M14.9 6.5h3M14.9 17.5h3" /></>,
  // 按键：键帽 + 键面上的横条，下方呼吸的高亮线表示「按下去」
  key: <><rect x="4.5" y="6" width="15" height="9" rx="2.5" /><path d="M9.6 10.5h4.8" /><path className="tg-pulse" d="M8.4 18.4h7.2" /></>,
  // 滚动：滚轮槽 + 上下箭头，两个箭头反向微动
  scroll: <><rect x="8" y="3.5" width="8" height="17" rx="4" /><path className="tg-hop" d="M10.5 8.6 12 7.1l1.5 1.5" /><path className="tg-drop" d="M10.5 15.4 12 16.9l1.5-1.5" /></>,
  // 等待：时钟。用表针与「联网」的地球线、「提问」的问号区分，圆本身不是判据
  wait: <><circle cx="12" cy="12" r="8" /><path className="tg-pulse" d="M12 7.6V12l3.2 2" /></>,
  // 剪贴板：带夹子的板面，夹子是与「正文」（整页文档）区别的关键
  clipboard: <><rect x="4.5" y="5" width="15" height="15.5" rx="2.5" /><rect x="9" y="3.2" width="6" height="3.4" rx="1.4" /><path className="tg-scan" d="M8 11.5h8M8 15.2h5.6" /></>,
  // 下载：下箭头落入托盘
  download: <><path d="M12 3.8v8.6" /><path className="tg-hop" d="M8.3 9.2 12 12.9l3.7-3.7" /><path d="M4.5 15.5v2.6a2.4 2.4 0 0 0 2.4 2.4h10.2a2.4 2.4 0 0 0 2.4-2.4v-2.6" /></>,
  // 批量步骤：多个方格连成一批，最后一个格子被勾完成
  batch: <><rect x="3.5" y="3.5" width="7" height="7" rx="1.6" /><rect x="13.5" y="3.5" width="7" height="7" rx="1.6" /><rect x="3.5" y="13.5" width="7" height="7" rx="1.6" /><path className="tg-draw" d="M13.9 17.6l1.9 1.9 3.7-4.3" /></>,
  // 经验 / 路线：起点到终点的虚线路径（虚线是这个字形与 git 分支图的区别）
  recipe: <><circle cx="6.2" cy="17.8" r="1.9" /><circle cx="17.8" cy="6.2" r="1.9" /><path className="tg-flow" d="M6.2 15.9v-3.6a5.7 5.7 0 0 1 5.7-5.7h4" /></>,
  // 标签页 / 窗口：叠放的两张页面，前一张带标签条
  tabs: <><path d="M8.2 5.5h9.3a2 2 0 0 1 2 2v9.3" /><rect x="3.5" y="8.2" width="12.5" height="12" rx="2" /><path className="tg-hop" d="M6.8 11.6h4.6" /></>,
  // 日志：行首行号点 + 多行记录，与「待办」（勾）、「检索」（放大镜）各自可辨
  logs: <><g className="tg-scan"><path d="M5 6.6h.01M5 12h.01M5 17.4h.01" strokeWidth={2.2} /><path d="M8.5 6.6h10.5M8.5 12h10.5M8.5 17.4h7" /></g></>,
  // 「操作」聚合行：三个错位堆叠的圆角块 = 「一叠不同的调用」。
  // 刻意做成**实心填充 + 轮廓**的组合，与上面所有单线动作字形拉开距离：
  // 聚合行本来就是「摘要」，不是某一次具体动作。
  cluster: <><rect x="3.2" y="3.2" width="8.4" height="8.4" rx="2" /><rect x="12.4" y="3.2" width="8.4" height="8.4" rx="2" /><rect x="3.2" y="12.4" width="8.4" height="8.4" rx="2" /><path className="tg-pulse" d="M12.4 16.6h4.4M14.6 14.4v4.4" /></>,
};

const TOOL_KEY: Record<string, K> = {
  fs_read: "read", fs_write: "write", editor_apply_diff: "patch", fs_list: "list", fs_grep: "grep",
  terminal_exec: "term", web_fetch: "web", web_search: "search", codebase_search: "search",
  git_diff: "git", git_root: "git", spawn_subagent: "agent", ask_subagent: "agent",
  collect_results: "collect", ci_run: "ci", memory_search: "memory", view_image: "image",
  todo_write: "todo", ask_user_question: "question", mcp: "mcp", thinking: "think",
  // plan-20261015-browser-agent：截图类工具用图片图形（plan-630-2590 起其余浏览器工具改为逐项登记，见下）
  browser_screenshot: "image", desktop_screenshot: "image",
  // plan-20261017-ux-overhaul：补齐此前落到「other」闪电图标的常用工具
  multi_file_edit: "patch", apply_patch: "patch", read_file_range: "read", read_attachment: "read", read_session_context: "read",
  symbol_search: "search", outline: "list", shell_exec: "term", terminal_bg_status: "term", terminal_bg_kill: "term", bg_command_done: "term", bg_command_stopped: "term",
  // plan-549-2281：功能地图查询与符号索引导入——此前落到 other 闪电图标，与「检索类工具」语义不符
  feature_lookup: "search", symbol_index_enable: "list",
  // plan-20261021-subq-row：子会话提问用对话气泡
  subsession_question: "ask",
  // plan-20261022-release-ci：子会话汇报 / 结论与子会话工具
  subsession_report: "ask", subsession_result: "collect",
  create_subsession: "agent", send_to_subsession: "ask", get_subsession_progress: "collect",
  interrupt_subsession: "agent", message_main: "ask", message_peer: "ask",
  // plan-549-2281：子代理提问 / 进度上报 / 结束（伪工具名，见 timeline.subagentNoticeItem）
  subagent_question: "ask", subagent_report: "ask", subagent_completion: "collect",
  git: "git", memory_write: "memory", skill_view: "read", compaction_index: "memory", compaction_view: "memory",
  goal_complete: "todo", cancel_subagent: "agent", subagent_inspect: "agent",
  send_to_subagent: "agent", resume_subagent: "agent", report_to_leader: "agent",
  plan_workflow: "todo", start_workflow: "todo", workflow_status: "todo", resume_workflow: "todo",
  cancel_workflow: "todo", amend_workflow: "todo", decide_workflow: "todo", request_write_scope: "write",
  // plan-630-2590：浏览器 / 桌面操控工具逐项登记。
  // 此前 browser_* 被前缀一刀切成「地球」、desktop_* 全部落到「闪电」，于是「执行页面脚本」与
  // 「读取网页正文」共用同一张图，桌面的点击 / 输入 / 读界面也无从区分——工具行的图标因此不再传递信息。
  // 页面侧与桌面侧同义动作允许共用字形（点击 / 输入 / 按键 / 滚动 / 批量 / 经验），
  // 但执行脚本、读正文、读结构、截图四类必须各自可辨。
  browser_navigate: "web", browser_tabs: "tabs", browser_snapshot: "snapshot", browser_get_text: "readtext",
  browser_click: "click", browser_type: "type", browser_press: "key", browser_scroll: "scroll",
  browser_hover: "click", browser_drag: "click", browser_fill: "type", browser_clipboard: "clipboard",
  browser_wait: "wait", browser_evaluate: "script", browser_logs: "logs", browser_batch: "batch",
  browser_downloads: "download", browser_recipe: "recipe",
  desktop_click: "click", desktop_type: "type", desktop_keys: "key", desktop_scroll: "scroll",
  desktop_batch: "batch", desktop_snapshot: "snapshot", desktop_recipe: "recipe",
  desktop_hit: "search", desktop_find_text: "search", desktop_windows: "list", desktop_apps: "tabs",
  desktop_focus: "click",
  // plan-630-2590 返工：消息流「操作」聚合行的专属字形（ToolTree 的 ClusterGlyph 直接传 "cluster"）。
  // 不登记会落到 other 的闪电，与「未知工具」撞图。
  cluster: "cluster",
};

export function toolGlyphKey(raw: string): K {
  const tool = normalizeToolName(raw);
  // plan-630-2590：逐项登记必须优先于前缀兜底——原来的 browser_ 兜底会盖住具体工具，
  // 让「执行脚本」和「读正文」拿到同一个字形，等于图标不再区分动作性质。
  if (TOOL_KEY[tool]) return TOOL_KEY[tool];
  if (tool.startsWith("mcp")) return "mcp";
  if (tool.startsWith("db_")) return "collect";
  // 未登记的新浏览器 / 桌面工具：网页类退回地球、其余退回闪电；
  // 误标成「联网」或「未知」都好过误标成「执行脚本」或「读取」。
  if (tool.startsWith("browser_")) return "web";
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
