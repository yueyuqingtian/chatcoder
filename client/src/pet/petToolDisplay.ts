/** 工具调用的可读化（plan-73-340）。
 *
 * 问题：浮窗此前直接展示工具原始名（`multi_file_edit`、`terminal_exec`）与
 * 参数预览原文，用户看不出「这一步在做什么、动了哪个文件、跑了什么命令」。
 *
 * 口径与主窗 `components/chat/ToolTree.tsx` 的 `TOOL_VERBS` 对齐，只是时态改为**进行中**
 * （浮窗展示的是正在发生的事）：主窗「已编辑」→ 浮窗「编辑文件」。
 *
 * 输出三段：图标类型（决定第二行图标与配色）、动词、目标（文件/命令/查询）。
 */
import type { ActivityKind } from "./PetIcons";

export interface ToolDisplay {
  kind: ActivityKind;
  /** 进行中动词，如「读取文件」 */
  verb: string;
  /** 已完成动词，如「已读取」 */
  doneVerb: string;
  /** 目标摘要（文件路径 / 命令 / 查询词 / 数量），可能为空 */
  target: string;
}

/** 工具 → { 活动类型, 进行中动词, 已完成动词 } */
const TOOL_MAP: Record<string, { kind: ActivityKind; verb: string; doneVerb: string }> = {
  // 读文件
  fs_read: { kind: "read", verb: "读取文件", doneVerb: "已读取" },
  read_attachment: { kind: "read", verb: "读取附件", doneVerb: "已读取" },
  view_image: { kind: "read", verb: "查看图片", doneVerb: "已查看" },
  fs_list: { kind: "read", verb: "浏览目录", doneVerb: "已列出" },
  outline: { kind: "read", verb: "查看结构", doneVerb: "已查看" },
  // 改文件
  fs_write: { kind: "edit", verb: "编辑文件", doneVerb: "已编辑" },
  editor_apply_diff: { kind: "edit", verb: "编辑文件", doneVerb: "已编辑" },
  multi_file_edit: { kind: "edit", verb: "批量编辑", doneVerb: "已编辑" },
  // 搜索
  fs_grep: { kind: "search", verb: "搜索代码", doneVerb: "已搜索" },
  codebase_search: { kind: "search", verb: "搜索代码", doneVerb: "已搜索" },
  symbol_search: { kind: "search", verb: "查找符号", doneVerb: "已查找" },
  memory_search: { kind: "search", verb: "搜索记忆", doneVerb: "已搜索" },
  memory_write: { kind: "edit", verb: "记录记忆", doneVerb: "已记录" },
  // 执行
  terminal_exec: { kind: "run", verb: "执行命令", doneVerb: "已执行" },
  terminal_bg_status: { kind: "run", verb: "查看后台进程", doneVerb: "已查看" },
  terminal_bg_kill: { kind: "run", verb: "结束后台进程", doneVerb: "已结束" },
  ci_run: { kind: "run", verb: "运行检查", doneVerb: "已运行" },
  // 网络
  web_fetch: { kind: "web", verb: "抓取网页", doneVerb: "已抓取" },
  web_search: { kind: "web", verb: "搜索网络", doneVerb: "已搜索" },
  google: { kind: "web", verb: "搜索网络", doneVerb: "已搜索" },
  duckduckgo: { kind: "web", verb: "搜索网络", doneVerb: "已搜索" },
  workbuddy_search: { kind: "web", verb: "搜索网络", doneVerb: "已搜索" },
  // 版本控制
  git: { kind: "run", verb: "执行 Git 操作", doneVerb: "已执行" },
  git_diff: { kind: "read", verb: "查看变更", doneVerb: "已查看" },
  git_root: { kind: "read", verb: "定位仓库", doneVerb: "已定位" },
  // 协作与流程
  todo_write: { kind: "other", verb: "更新任务清单", doneVerb: "已更新" },
  ask_user_question: { kind: "other", verb: "询问用户", doneVerb: "已询问" },
  spawn_subagent: { kind: "other", verb: "派发子代理", doneVerb: "已派发" },
  ask_subagent: { kind: "other", verb: "询问子代理", doneVerb: "已询问" },
  collect_results: { kind: "other", verb: "收集结果", doneVerb: "已收集" },
  skill_view: { kind: "other", verb: "加载技能", doneVerb: "已加载" },
  goal_complete: { kind: "other", verb: "标记目标", doneVerb: "已标记" },
  compaction_index: { kind: "other", verb: "查看上下文", doneVerb: "已查看" },
  compaction_view: { kind: "other", verb: "读取上下文", doneVerb: "已读取" },
  outside_access: { kind: "other", verb: "访问工作区外", doneVerb: "已访问" },
  mcp: { kind: "other", verb: "调用工具", doneVerb: "已调用" },
};

/** 浏览器类工具按前缀归组（browser_navigate / click / type ...） */
function browserDisplay(tool: string): { kind: ActivityKind; verb: string; doneVerb: string } {
  const tail = tool.slice("browser_".length);
  const label: Record<string, string> = {
    navigate: "打开网页",
    screenshot: "截图",
    click: "点击页面",
    type: "输入内容",
    snapshot: "读取页面",
    evaluate: "执行脚本",
  };
  return { kind: "web", verb: label[tail] || "操作浏览器", doneVerb: "已操作" };
}

function lookup(tool: string): { kind: ActivityKind; verb: string; doneVerb: string } {
  if (TOOL_MAP[tool]) return TOOL_MAP[tool];
  if (tool.startsWith("browser_")) return browserDisplay(tool);
  // mcp 包装工具（mcp_<server>_<name>）与未知工具统一回退
  return { kind: "other", verb: "调用工具", doneVerb: "已调用" };
}

function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}

/** 路径压缩：只保留末两段，便于一眼认出是哪个文件（完整路径在 title 里） */
export function shortenPath(p: string): string {
  const parts = String(p).split(/[\\/]+/).filter(Boolean);
  if (parts.length <= 2) return parts.join("/");
  return parts.slice(-2).join("/");
}

/** 单行化 + 截断（浮窗只有一行，超出由 CSS 省略号，这里先限长避免长文本驻留） */
function oneLine(text: string, max = 72): string {
  const s = text.replace(/\s+/g, " ").trim();
  return s.length > max ? `${s.slice(0, max)}…` : s;
}

/** 从工具入参提取可读目标：文件路径 > 命令 > 查询词 > 网址 > 数量摘要 */
export function toolTarget(tool: string, args: Record<string, unknown> | undefined): string {
  const a = args || {};
  const path = str(a.path) || str(a.file_path) || str(a.file);
  if (path) return shortenPath(path);
  const cmd = str(a.command) || str(a.cmd);
  if (cmd) return oneLine(cmd, 56);
  const query = str(a.pattern) || str(a.query) || str(a.keyword) || str(a.symbol) || str(a.name);
  if (query) return oneLine(query, 40);
  const url = str(a.url);
  if (url) return oneLine(url.replace(/^https?:\/\//, ""), 48);
  if (Array.isArray(a.edits)) return `${a.edits.length} 处修改`;
  if (Array.isArray(a.files)) return `${a.files.length} 个文件`;
  if (Array.isArray(a.todos)) return `${a.todos.length} 项清单`;
  if (Array.isArray(a.questions)) return `${a.questions.length} 个问题`;
  if (str(a.slug)) return str(a.slug);
  if (tool === "skill_view" && str(a.name)) return str(a.name);
  return "";
}

/** 组装展示信息 */
export function toolDisplay(tool: string, args: Record<string, unknown> | undefined): ToolDisplay {
  const meta = lookup(tool);
  return { ...meta, target: toolTarget(tool, args) };
}

/** 工具原始名（用于 title 提示，鼠标悬停可看全称） */
export function rawToolName(tool: string): string {
  return tool || "工具";
}

/** 从 tool.call 的 `args_preview` 解析入参。
 *
 * 服务端已把入参截断到 200 字符，超长时 JSON 不完整——此时用正则兜底捞出
 * 路径/命令/查询等关键字段，保证浮窗在长参数下仍能显示「动了哪个文件」。
 */
export function parseArgsPreview(raw: string): Record<string, unknown> | undefined {
  const s = String(raw || "").trim();
  if (!s) return undefined;
  try {
    const v = JSON.parse(s);
    return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : undefined;
  } catch {
    const out: Record<string, unknown> = {};
    const grab = (key: string): string => {
      const m = s.match(new RegExp(`"${key}"\\s*:\\s*"((?:[^"\\\\]|\\\\.){0,160})`));
      return m ? m[1].replace(/\\\\/g, "\\").replace(/\\"/g, '"') : "";
    };
    const path = grab("path") || grab("file_path");
    if (path) out.path = path;
    const cmd = grab("command") || grab("cmd");
    if (cmd) out.command = cmd;
    const q = grab("pattern") || grab("query") || grab("url");
    if (q) out.query = q;
    const url = grab("url");
    if (url) out.url = url;
    return Object.keys(out).length > 0 ? out : undefined;
  }
}

/** 浮窗第二行的工具文案：进行中「动词 · 目标」，完成后「已动词 · 目标」 */
export function toolLine(
  tool: string,
  argsPreview: string,
  done: boolean
): { kind: ActivityKind; text: string; title: string } {
  const args = parseArgsPreview(argsPreview);
  const d = toolDisplay(tool, args);
  const verb = done ? d.doneVerb : d.verb;
  const text = d.target ? `${verb} · ${d.target}` : verb;
  const rawArgs = argsPreview ? ` ${String(argsPreview).slice(0, 160)}` : "";
  return { kind: d.kind, text, title: `${rawToolName(tool)}${rawArgs}` };
}
