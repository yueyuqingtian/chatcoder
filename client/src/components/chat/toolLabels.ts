/** 工具名 → 用户可读文案（plan-20261017-ux-overhaul 阶段 B）。
 *
 * 为什么单独成表：此前动词表只覆盖一部分工具，缺失项回落为「已调用 multi_file_edit」这类原生名，
 * 用户看不懂；ta3 会话里工具名还会是 Edit / Bash / MultiFileEdit 等伪装名，同一动作出现两种写法。
 * 这里统一：先把 ta3 伪装名归一到真实名，再查中文动作；查不到时把蛇形 / 驼峰名转成可读词组，
 * 永远不把原生标识符直接摆到消息流里。
 */

/** ta3 伪装名 → 真实执行名（与 server/app/models/providers/ta3_tool_aliases.py 的 FROM_TA3 保持一致） */
const FROM_TA3: Record<string, string> = {
  Read: "fs_read", List: "fs_list", Search: "fs_grep", Diff: "git_diff", Write: "fs_write",
  Edit: "editor_apply_diff", Bash: "terminal_exec", WebSearch: "web_search", TodoWrite: "todo_write",
  SubAgent: "spawn_subagent", SubAgentAsync: "spawn_subagent", get_project_memory: "memory_search",
  ReadAttachment: "read_attachment", ViewImage: "view_image", BashStatus: "terminal_bg_status",
  BashKill: "terminal_bg_kill", CompleteGoal: "goal_complete", AskUser: "ask_user_question",
  MultiFileEdit: "multi_file_edit", SymbolSearch: "symbol_search", get_file_outline: "outline",
  WebFetch: "web_fetch", CiRun: "ci_run", Git: "git", CodebaseSearch: "codebase_search",
  // plan-562-2318：后端 ta3_tool_aliases 已有这两条伪装名，前端缺映射会让消息流显示英文原名
  FeatureLookup: "feature_lookup", SymbolIndexEnable: "symbol_index_enable",
  CompactionIndex: "compaction_index", CompactionView: "compaction_view", SkillView: "skill_view",
  ReadSkill: "skill_view", MemoryWrite: "memory_write", single_find_and_replace: "editor_apply_diff",
  BrowserNavigate: "browser_navigate", BrowserScreenshot: "browser_screenshot", BrowserClick: "browser_click",
  BrowserType: "browser_type", BrowserSnapshot: "browser_snapshot", BrowserEvaluate: "browser_evaluate",
  BrowserTabs: "browser_tabs", BrowserGetText: "browser_get_text", BrowserPress: "browser_press",
  BrowserScroll: "browser_scroll", BrowserHover: "browser_hover", BrowserDrag: "browser_drag",
  BrowserFill: "browser_fill", BrowserClipboard: "browser_clipboard", BrowserWait: "browser_wait",
  BrowserLogs: "browser_logs", BrowserBatch: "browser_batch",
  BrowserDownloads: "browser_downloads", BrowserRecipe: "browser_recipe",
  DesktopWindows: "desktop_windows", DesktopSnapshot: "desktop_snapshot", DesktopHit: "desktop_hit",
  DesktopScreenshot: "desktop_screenshot", DesktopFindText: "desktop_find_text", DesktopClick: "desktop_click",
  DesktopType: "desktop_type", DesktopKeys: "desktop_keys", DesktopScroll: "desktop_scroll",
  DesktopFocus: "desktop_focus", DesktopApps: "desktop_apps", DesktopRecipe: "desktop_recipe",
  DesktopBatch: "desktop_batch",
  TaskQuery: "collect_results", TaskCancel: "cancel_subagent", SubAgentInspect: "subagent_inspect",
  SendToSubagent: "send_to_subagent", ResumeSubagent: "resume_subagent",
  PlanWorkflow: "plan_workflow", StartWorkflow: "start_workflow", WorkflowStatus: "workflow_status",
  ResumeWorkflow: "resume_workflow", CancelWorkflow: "cancel_workflow", AmendWorkflow: "amend_workflow",
  DecideWorkflow: "decide_workflow", RequestWriteScope: "request_write_scope", ReportToLeader: "report_to_leader",
};

/** 把任意来源的工具名归一为真实执行名（未知名原样返回） */
export function normalizeToolName(tool: string): string {
  return FROM_TA3[tool] ?? tool;
}

/** 完成态动词（运行中由 runningVerb 把「已」替换为「正在」） */
export const TOOL_VERBS: Record<string, string> = {
  fs_read: "已读取",
  read_file_range: "已读取",
  read_attachment: "已读取附件",
  fs_write: "已写入",
  editor_apply_diff: "已编辑",
  multi_file_edit: "已编辑",
  apply_patch: "已编辑",
  fs_list: "已列出",
  fs_grep: "已搜索",
  codebase_search: "已检索代码",
  symbol_search: "已检索符号",
  // plan-549-2281：新增的功能地图查询与符号索引导入，此前缺中文动词，
  // 消息流会退回「已调用工具 feature lookup」这类中英混杂行
  feature_lookup: "已查询功能",
  symbol_index_enable: "已开启符号索引",
  outline: "已查看文件结构",
  terminal_exec: "已运行",
  shell_exec: "已运行",
  terminal_bg_status: "已查看后台命令",
  terminal_bg_kill: "已结束后台命令",
  // plan-20261020-bgtask：后台命令的完成 / 停止通知（伪工具名，见 timeline.bgNoticeItem）
  bg_command_done: "后台命令已结束",
  bg_command_stopped: "后台命令已被停止",
  // plan-20261021-subq-row：子会话提问（伪工具名，见 timeline.subsessionQuestionItem）
  subsession_question: "子会话提问",
  // plan-20261022-release-ci：子会话汇报 / 结论（伪工具名，见 timeline.subsessionNoticeItem）
  subsession_report: "子会话汇报",
  subsession_result: "子会话已结束",
  // plan-549-2281：子代理提问 / 进度上报（伪工具名，见 timeline.subagentNoticeItem）——
  // 子会话下线后所有「向上抛」的消息都来自子代理
  subagent_question: "子代理提问",
  subagent_report: "子代理进度上报",
  subagent_completion: "子代理已结束",
  // 子会话工具：此前没有中文动词，工具行显示成「已调用工具 发送 to subsession」
  create_subsession: "已创建子会话",
  send_to_subsession: "已发消息给子会话",
  get_subsession_progress: "已查看子会话进度",
  interrupt_subsession: "已中断子会话",
  message_main: "已向主代理汇报",
  message_peer: "已发消息给同级子会话",
  git: "已执行 Git",
  git_diff: "已检查改动",
  git_root: "已定位仓库",
  ci_run: "已运行检查",
  web_fetch: "已抓取网页",
  web_search: "已联网搜索",
  memory_search: "已检索记忆",
  memory_write: "已写入记忆",
  skill_view: "已读取技能",
  compaction_index: "已查看历史摘要",
  compaction_view: "已查看历史摘要",
  view_image: "已查看图片",
  todo_write: "已更新任务清单",
  ask_user_question: "已询问用户",
  goal_complete: "已完成目标",
  spawn_subagent: "已创建子代理",
  ask_subagent: "已询问子代理",
  collect_results: "已收集子代理结果",
  cancel_subagent: "已取消子代理",
  subagent_inspect: "已查看子代理",
  send_to_subagent: "已发消息给子代理",
  resume_subagent: "已续跑子代理",
  report_to_leader: "已向主代理汇报",
  plan_workflow: "已规划工作流",
  start_workflow: "已启动工作流",
  workflow_status: "已查看工作流",
  resume_workflow: "已恢复工作流",
  cancel_workflow: "已取消工作流",
  amend_workflow: "已调整工作流",
  decide_workflow: "已提交工作流决策",
  request_write_scope: "已申请写入范围",
  mcp: "已调用连接器",
  // plan-20261015-browser-agent：内置浏览器与电脑操控工具
  browser_navigate: "已打开网页",
  browser_tabs: "已管理标签",
  browser_snapshot: "已读取页面结构",
  browser_get_text: "已读取网页正文",
  browser_screenshot: "已截图",
  browser_click: "已点击",
  browser_type: "已输入",
  browser_press: "已按键",
  browser_scroll: "已滚动",
  browser_hover: "已悬停",
  browser_drag: "已拖拽",
  browser_fill: "已填写表单",
  browser_clipboard: "已操作剪贴板",
  browser_wait: "已等待",
  browser_evaluate: "已执行页面脚本",
  browser_logs: "已查看页面日志",
  browser_batch: "已批量操作网页",
  browser_downloads: "已查看下载",
  browser_recipe: "已记录网页操作经验",
  desktop_screenshot: "已截屏",
  desktop_click: "已点击",
  desktop_type: "已输入",
  desktop_keys: "已按键",
  desktop_scroll: "已滚动",
  desktop_batch: "已批量操作",
  desktop_snapshot: "已读取界面",
  // 已移除的工具（plan-20261019-desktop-precision）：保留标签，历史消息仍能正常显示
  desktop_hit: "已识别界面元素",
  desktop_windows: "已列出窗口",
  desktop_focus: "已切换窗口",
  desktop_find_text: "已查找文字",
  desktop_apps: "已管理应用",
  desktop_recipe: "已记录操作经验",
};

/** plan-20261025-shell（需求 1）：消息流工具行的精简标签（参考 zcode「查阅 · 2 文件」「读取 a.py」）。
 *
 * [动作词, 补充说明]：
 *  · 动作词以两字为主（Git、子代理等专名例外），运行中与完成态用同一个词——运行态由流光文字与行尾转圈表达，
 *    行宽不随「正在读取 → 已读取」跳变，一列工具行的起点与节奏也更整齐；
 *  · 补充说明只在该行没有对象（文件 / 命令 / 查询词）可展示时出现，灰色小字，例如「查看 后台输出」。
 * 完成态长句（已读取 / 已编辑…）仍由 TOOL_VERBS 提供，供小窗、宠物、审批卡等只放一句话的位置使用。 */
const TOOL_LABELS: Record<string, [string, string?]> = {
  fs_read: ["读取"],
  read_file_range: ["读取"],
  read_attachment: ["读取", "附件"],
  fs_write: ["写入"],
  editor_apply_diff: ["编辑"],
  multi_file_edit: ["编辑"],
  apply_patch: ["编辑"],
  fs_list: ["列出", "目录"],
  fs_grep: ["搜索"],
  codebase_search: ["检索", "代码"],
  symbol_search: ["符号"],
  feature_lookup: ["查询", "功能地图"],
  symbol_index_enable: ["索引", "开启符号索引"],
  outline: ["大纲"],
  terminal_exec: ["运行"],
  shell_exec: ["运行"],
  terminal_bg_status: ["查看", "后台命令输出"],
  terminal_bg_kill: ["停止", "后台命令"],
  bg_command_done: ["后台"],
  bg_command_stopped: ["后台"],
  subsession_question: ["提问"],
  subsession_report: ["汇报"],
  subsession_result: ["结束"],
  subagent_question: ["提问"],
  subagent_report: ["进展"],
  subagent_completion: ["结束"],
  create_subsession: ["创建", "子会话"],
  send_to_subsession: ["发送", "给子会话"],
  get_subsession_progress: ["查看", "子会话进度"],
  interrupt_subsession: ["中断", "子会话"],
  message_main: ["汇报", "给主代理"],
  message_peer: ["发送", "给同级子会话"],
  git: ["Git"],
  git_diff: ["差异"],
  git_root: ["定位", "仓库"],
  ci_run: ["检查"],
  web_fetch: ["抓取"],
  web_search: ["联网"],
  memory_search: ["记忆"],
  memory_write: ["记住"],
  skill_view: ["技能"],
  compaction_index: ["摘要", "历史上下文"],
  compaction_view: ["摘要", "历史上下文"],
  view_image: ["看图"],
  todo_write: ["待办"],
  ask_user_question: ["提问"],
  goal_complete: ["目标", "已达成"],
  spawn_subagent: ["派发", "子代理"],
  ask_subagent: ["询问", "子代理"],
  collect_results: ["收集", "子代理结果"],
  cancel_subagent: ["取消", "子代理"],
  subagent_inspect: ["查看", "子代理"],
  send_to_subagent: ["发送", "给子代理"],
  resume_subagent: ["续跑", "子代理"],
  report_to_leader: ["汇报", "给主代理"],
  plan_workflow: ["规划", "工作流"],
  start_workflow: ["启动", "工作流"],
  workflow_status: ["进度", "工作流"],
  resume_workflow: ["恢复", "工作流"],
  cancel_workflow: ["取消", "工作流"],
  amend_workflow: ["调整", "工作流"],
  decide_workflow: ["决策", "工作流"],
  request_write_scope: ["申请", "写入范围"],
  db_query: ["查询", "数据库"],
  db_execute: ["执行", "数据变更"],
  db_ddl: ["变更", "表结构"],
  db_schema: ["读取", "表结构"],
  browser_navigate: ["打开"],
  browser_tabs: ["标签"],
  browser_snapshot: ["结构", "页面元素"],
  browser_get_text: ["正文", "网页"],
  browser_screenshot: ["截图"],
  browser_click: ["点击"],
  browser_type: ["输入"],
  browser_press: ["按键"],
  browser_scroll: ["滚动"],
  browser_hover: ["悬停"],
  browser_drag: ["拖拽"],
  browser_fill: ["填写", "表单"],
  browser_clipboard: ["剪贴"],
  browser_wait: ["等待"],
  browser_evaluate: ["脚本", "页面"],
  browser_logs: ["日志", "页面"],
  browser_batch: ["批量", "网页操作"],
  browser_downloads: ["下载"],
  browser_recipe: ["经验", "网页操作"],
  desktop_screenshot: ["截屏"],
  desktop_click: ["点击"],
  desktop_type: ["输入"],
  desktop_keys: ["按键"],
  desktop_scroll: ["滚动"],
  desktop_batch: ["批量", "桌面操作"],
  desktop_snapshot: ["界面", "窗口元素"],
  desktop_hit: ["识别", "界面元素"],
  desktop_windows: ["窗口"],
  desktop_focus: ["切换", "窗口"],
  desktop_find_text: ["查找", "文字"],
  desktop_apps: ["应用"],
  desktop_recipe: ["经验", "桌面操作"],
};

/** 常见英文词 → 中文，用于未知工具名的兜底可读化 */
const WORDS: Record<string, string> = {
  get: "获取", list: "列出", create: "创建", update: "更新", delete: "删除", remove: "删除",
  search: "搜索", query: "查询", read: "读取", write: "写入", run: "运行", exec: "执行",
  fetch: "抓取", send: "发送", open: "打开", close: "关闭", find: "查找", add: "添加",
  issue: "议题", issues: "议题", file: "文件", files: "文件", page: "页面", pages: "页面",
  user: "用户", repo: "仓库", repository: "仓库", table: "表", tables: "表", sql: "SQL",
  db: "数据库", database: "数据库", comment: "评论", message: "消息", status: "状态",
};

/** snake_case / camelCase → 空格分词后逐词翻译，翻译不了的保留原词 */
function humanize(name: string): string {
  const words = name
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .split(/[\s_.\-]+/)
    .filter(Boolean)
    .map((w) => WORDS[w.toLowerCase()] ?? w.toLowerCase());
  // 全部都翻译成中文时去掉空格（「获取 议题」→「获取议题」）
  return words.every((w) => /[一-龥]/.test(w)) ? words.join("") : words.join(" ");
}

/** MCP 包装器命名为 mcp_{server}_{tool}（server 内的 . - 被替换成 _），拆出服务与工具两段 */
function splitMcp(tool: string): { server: string; name: string } | null {
  const m = /^mcp[_.]([^_.]+)[_.](.+)$/.exec(tool);
  return m ? { server: m[1], name: m[2] } : null;
}

/** 是否有专门的中文动词（用于决定是否再额外展示一个名称标签） */
export function hasToolVerb(tool: string): boolean {
  return Boolean(TOOL_VERBS[normalizeToolName(tool)]);
}

/** 完成态动作文案：已读取 / 已编辑 / 已调用 GitHub · 创建议题 … */
export function toolVerb(tool: string): string {
  const real = normalizeToolName(tool);
  const v = TOOL_VERBS[real];
  if (v) return v;
  const mcp = splitMcp(real);
  if (mcp) return "已调用连接器";
  if (real.startsWith("db_")) return `已执行${humanize(real.slice(3))}`;
  return "已调用工具";
}

/** 运行态动作文案 */
export function runningVerb(tool: string): string {
  return toolVerb(tool).replace(/^已/, "正在");
}

/** 无专门动词时附在动作后的名称（MCP：服务 · 工具；其他：可读化名称）；有专门动词时返回空 */
export function toolDisplayName(tool: string): string {
  const real = normalizeToolName(tool);
  if (TOOL_VERBS[real]) return "";
  const mcp = splitMcp(real);
  if (mcp) return `${mcp.server} · ${humanize(mcp.name)}`;
  if (real.startsWith("db_")) return "";
  return humanize(real);
}

/** 消息流工具行的精简标签（plan-20261025-shell 需求 1）。
 *  noun：没有对象时的补充说明；alwaysNoun：连接器 / 未知工具的名称本身就是信息，有查询词时也要显示。 */
export function toolLabel(tool: string): { verb: string; noun: string; alwaysNoun: boolean } {
  const real = normalizeToolName(tool);
  const hit = TOOL_LABELS[real];
  if (hit) return { verb: hit[0], noun: hit[1] ?? "", alwaysNoun: false };
  const mcp = splitMcp(real);
  if (mcp) return { verb: "调用", noun: `${mcp.server} · ${humanize(mcp.name)}`, alwaysNoun: true };
  if (real.startsWith("db_")) return { verb: "执行", noun: humanize(real.slice(3)), alwaysNoun: true };
  return { verb: "调用", noun: humanize(real), alwaysNoun: true };
}

/** 完整可读名（审批卡、子代理摘要等只放一个名字的位置用）：动词去掉「已」+ 名称 */
export function toolTitle(tool: string): string {
  const name = toolDisplayName(tool);
  const verb = toolVerb(tool).replace(/^已/, "");
  return name ? `${verb === "调用工具" ? "" : verb + " "}${name}`.trim() : verb;
}
