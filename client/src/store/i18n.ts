/**
 * 轻量级国际化工具。
 * 支持 zh / en 两种语言,通过 useUiStore 的 language 字段驱动。
 *
 * 用法: const { t } = useI18n();
 *       t('sidebar.chat')  // → "群聊" / "Chat"
 *
 * 或直接: import { translate } from './i18n';
 *         translate('sidebar.chat', 'en')
 */
import { useUiStore, type Language } from "./ui";

type Dict = Record<string, [string, string]>; // [zh, en]

const DICT: Dict = {
  // ── 侧边栏 ──
  "sidebar.chat":      ["群聊", "Chat"],
  "sidebar.team":      ["团队", "Team"],
  "sidebar.board":     ["看板", "Board"],
  "sidebar.knowledge": ["知识", "Knowledge"],
  "sidebar.settings":  ["设置", "Settings"],

  // ── 设置页 ──
  "settings.title":       ["偏好设置", "Preferences"],
  "settings.subtitle":    ["主题、语言、字体与个性化外观。", "Theme, language, fonts and appearance."],
  "settings.general":     ["通用", "General"],
  "settings.models":      ["模型", "Models"],
  "settings.rules":       ["规则", "Rules"],
  "settings.memory":      ["记忆", "Memory"],
  "settings.skills":      ["技能", "Skills"],
  "settings.mcp":         ["MCP 服务", "MCP Servers"],

  "settings.theme_mode":     ["主题模式", "Theme Mode"],
  "settings.theme_desc":     ["浅色或深色界面", "Light or dark interface"],
  "settings.light":          ["浅色", "Light"],
  "settings.dark":           ["深色", "Dark"],

  "settings.language":       ["界面语言", "Interface Language"],
  "settings.language_desc":  ["切换界面显示语言", "Switch the display language"],

  "settings.glass":          ["毛玻璃效果", "Glassmorphism"],
  "settings.glass_desc":     ["为面板添加半透明模糊背景", "Add translucent blur to panels"],

  "settings.font":           ["对话字体", "Chat Font"],
  "settings.font_desc":      ["选择对话区域使用的字体族", "Choose the font family for chat"],

  "settings.font_size":      ["对话字号", "Chat Font Size"],
  "settings.font_size_desc": ["控制对话消息的文字大小", "Control message text size"],
  "settings.ui_size":        ["界面基础字号", "Base Font Size"],
  "settings.ui_size_desc":   ["控制整个应用的基础文字大小", "Control base text size for the whole app"],
  "settings.bubble_w":       ["对话气泡宽度", "Bubble Width"],
  "settings.bubble_w_desc":  ["用户消息气泡占容器宽度的百分比", "User message bubble width percentage"],
  "settings.content_w":      ["内容展示宽度", "Content Width"],
  "settings.content_w_desc": ["中间面板消息列表的最大宽度", "Max width for the message list"],

  "settings.left_w":         ["左侧面板宽度", "Left Panel Width"],
  "settings.right_w":        ["右侧面板宽度", "Right Panel Width"],

  // ── 通用 ──
  "common.save":    ["保存", "Save"],
  "common.cancel":  ["取消", "Cancel"],
  "common.create":  ["创建", "Create"],
  "common.delete":  ["删除", "Delete"],
  "common.saving":  ["保存中…", "Saving…"],
  "common.loading": ["加载中…", "Loading…"],
  "common.copy":    ["复制", "Copy"],
  "common.copied":  ["已复制", "Copied"],
  "common.reply":   ["回复", "Reply"],
  "common.scan":    ["扫描导入", "Scan & Import"],
  "common.scanning":["扫描中…", "Scanning…"],
  "common.new":     ["新建", "New"],
  "common.back":    ["返回应用", "Back to App"],

  // ── 右侧面板 ──
  "rp.tasks":     ["任务", "Tasks"],
  "rp.history":   ["历史", "History"],
  "rp.artifacts": ["产物", "Artifacts"],
  "rp.agents":    ["智能体", "Agents"],

  // ── 设置页：分组 ──
  "settingsGroup.basic": ["基础设置", "General"],
  "settingsGroup.agent": ["Agent 能力", "Agent Capabilities"],
  "settingsGroup.data":  ["数据与统计", "Data & Stats"],

  // ── 设置页：导航项 ──
  "settings.tab.general":     ["常规", "General"],
  "settings.tab.appearance":  ["外观", "Appearance"],
  "settings.tab.models":      ["模型管理", "Models"],
  "settings.tab.plugins":     ["插件", "Plugins"],
  "settings.tab.memory":      ["记忆", "Memory"],
  "settings.tab.skills":      ["技能管理", "Skills"],
  "settings.tab.subagents":   ["子代理", "Subagents"],
  "settings.tab.mcp":         ["MCP 服务器", "MCP Servers"],
  "settings.tab.rules":       ["AI 规则", "AI Rules"],
  "settings.tab.hooks":       ["钩子", "Hooks"],
  "settings.tab.policy":      ["执行策略", "Execution Policy"],
  "settings.tab.scheduled":   ["定时任务", "Scheduled Tasks"],
  "settings.tab.usage":       ["使用统计", "Usage Stats"],
  "settings.tab.diagnostics": ["诊断", "Diagnostics"],
  "settings.tab.archive":     ["归档恢复", "Archive"],
  "settings.tab.about":       ["关于", "About"],

  // ── 设置页：页面标题/副标题 ──
  "settings.pt.general":     ["常规", "General"],
  "settings.ps.general":     ["语言、代理、终端与显示选项", "Language, proxy, terminal & display options"],
  "settings.pt.appearance":  ["外观", "Appearance"],
  "settings.ps.appearance":  ["主题、毛玻璃、布局与个性化外观", "Theme, glassmorphism, layout & appearance"],
  "settings.pt.models":      ["模型管理", "Model Management"],
  "settings.ps.models":      ["按供应商配置模型：填 URL/Key 后扫描，勾选启用", "Configure models per provider: fill URL/Key, scan and enable"],
  "settings.pt.skills":      ["技能管理", "Skills"],
  "settings.ps.skills":      ["可被 Agent 加载的 Skill 资源", "Skill resources loadable by agents"],
  "settings.pt.subagents":   ["子代理", "Subagents"],
  "settings.ps.subagents":   ["子代理类型配置：工具白名单、模型覆盖与系统提示词", "Subagent profiles: tool whitelist, model override & system prompt"],
  "settings.pt.mcp":         ["MCP 服务器", "MCP Servers"],
  "settings.ps.mcp":         ["连接外部工具与数据源", "Connect external tools & data sources"],
  "settings.pt.rules":       ["AI 规则", "AI Rules"],
  "settings.ps.rules":       ["全局 / 项目规则，以及多 AI 软件规则文档的扫描与启用", "Global/project rules and scanning AI software rule docs"],
  "settings.pt.policy":      ["执行策略", "Execution Policy"],
  "settings.ps.policy":      ["控制命令执行审批规则", "Command approval rules"],
  "settings.pt.usage":       ["用量统计", "Usage Stats"],
  "settings.ps.usage":       ["整个软件的 token 用量：总数、趋势与各模型分布", "Global token usage: totals, trends & model distribution"],
  "settings.pt.diagnostics": ["诊断", "Diagnostics"],
  "settings.ps.diagnostics": ["系统健康检查", "System health check"],
  "settings.pt.archive":     ["归档恢复", "Archive"],
  "settings.ps.archive":     ["已归档的项目与会话，支持一键恢复", "Archived projects & sessions, one-click restore"],
  "settings.pt.plugins":     ["插件", "Plugins"],
  "settings.ps.plugins":     ["系统组件插件化：查看可替换的 slot 组件", "Plugins: replaceable slot components"],
  "settings.pt.about":       ["关于", "About"],

  // ── 标题栏 ──
  "titlebar.settings": ["偏好设置", "Preferences"],
  "titlebar.back":     ["返回工作区", "Back to Workspace"],
  "titlebar.app":      ["ChatCoder", "ChatCoder"],

  // ── 通用面板（GeneralPanel）──
  "gp.language":          ["界面语言", "Interface Language"],
  "gp.language_desc":     ["中文 / English", "中文 / English"],
  "gp.http_proxy":        ["HTTP 代理", "HTTP Proxy"],
  "gp.http_proxy_desc":   ["全局 HTTP/HTTPS 代理，立即生效", "Global HTTP/HTTPS proxy, applied immediately"],
  "gp.terminal_shell":    ["集成终端 Shell", "Terminal Shell"],
  "gp.terminal_shell_desc": ["新终端标签使用的 Shell（重启终端生效）", "Shell for new terminal tabs (applies after restart)"],
  "gp.terminal_font":     ["终端字体", "Terminal Font"],
  "gp.terminal_font_desc": ["选择集成终端使用的字体（留空继承系统终端字体）", "Terminal font (empty = inherit system)"],
  "gp.enhanced_search":   ["增强搜索（ripgrep）", "Enhanced Search (ripgrep)"],
  "gp.enhanced_search_desc": ["使用 ripgrep 进行更快的全库文本搜索", "Use ripgrep for faster full-repo text search"],
  "gp.memory":            ["AI 主动生成记忆", "Auto Memory"],
  "gp.memory_desc":       ["每轮对话结束时 AI 自主提取关键事实/偏好写入记忆库", "AI extracts key facts/preferences into memory after each turn"],
  "gp.reasoning":         ["消息流显示 reasoning", "Show reasoning in message flow"],
  "gp.reasoning_desc":    ["在消息流中渲染思考过程块（ThinkingBlock）", "Render thinking blocks in the message flow"],
  "gp.auto_approve":      ["自动批准工具调用", "Auto-approve Tool Calls"],
  "gp.auto_approve_desc": ["自动允许工具请求（「始终需要审批」列表内的仍会弹审批）", "Auto-allow tool requests (forced-approval tools still prompt)"],
  "gp.force_approve":     ["始终需要审批的工具", "Always-Require-Approval Tools"],
  "gp.force_approve_desc": ["即使开启自动批准或危险全访问沙箱也不可跳过的工具", "Tools that cannot bypass approval even with auto-approve"],
  "gp.sandbox":           ["沙箱模式", "Sandbox Mode"],
  "gp.sandbox_desc":      ["工作区写访问/只读沙箱/危险全访问；项目 .chatcoder/config.toml 优先", "Write access / read-only / full access; project config takes priority"],
  "gp.max_steps":         ["AI 最大执行步数", "Max Steps"],
  "gp.max_steps_desc":    ["单轮对话中模型可调用的最大工具步数", "Max tool steps per turn"],
  "gp.browser":           ["开启内置浏览器工具", "Enable Browser Tool"],
  "gp.browser_desc":      ["允许 AI 调用浏览器进行网页访问、点击、填表与截图", "Allow AI to browse, click, fill forms and screenshot"],
  "gp.browser_headless":  ["浏览器后台无头模式", "Browser Headless Mode"],
  "gp.browser_headless_desc": ["开启后 Playwright/Chromium 后台静默运行", "Run Chromium silently in background"],
  "gp.plan_outside":      ["计划模式允许访问工作区外", "Plan Mode Outside Access"],
  "gp.plan_outside_desc": ["允许「计划模式」AI 访问工作区外目录", "Allow plan-mode AI to access paths outside workspace"],
  "gp.density":           ["消息流密度", "Message Density"],
  "gp.density_desc":      ["思考/工具调用/文本等消息块之间的行间距（立即生效）", "Spacing between message blocks (applies immediately)"],
  "gp.save":              ["保存设置", "Save Settings"],
  "gp.saving":            ["保存中…", "Saving…"],
  "gp.saved":             ["已保存 ✓", "Saved ✓"],
  "gp.plus":              ["附加设置", "Additional Settings"],
};

export function translate(key: string, lang: Language): string {
  const entry = DICT[key];
  if (!entry) return key;
  return lang === "en" ? entry[1] : entry[0];
}

export function useI18n() {
  const language = useUiStore((s) => s.language);
  return {
    language,
    t: (key: string) => translate(key, language),
  };
}
