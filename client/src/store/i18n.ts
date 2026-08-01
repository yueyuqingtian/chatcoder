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
