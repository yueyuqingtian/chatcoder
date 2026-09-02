/** 设置中心（v19 并入主布局）：左侧导航由 SettingsSidebar（SidebarShell）渲染，
 * 本文件提供设置项索引（SETTINGS_INDEX，供命令中心搜索）与右侧内容区 SettingsContent。
 * 三组：基础设置 / Agent 能力 / 数据与统计。
 */
import { AppearancePanel } from "./AppearancePanel";
import { GeneralPanel } from "./GeneralPanel";
import { ModelsPanel } from "./ModelsPanel";
import { SkillsPanel } from "./SkillsPanel";
import { McpPanel } from "./McpPanel";
import { SubagentsPanel } from "./SubagentsPanel";
import { RulesPanel } from "./RulesPanel";
import { ScheduledPanel } from "./ScheduledPanel";
import { PolicyPanel } from "./PolicyPanel";
import { HooksPanel } from "./HooksPanel";
import { MemoryPanel } from "./MemoryPanel";
import { UsagePanel } from "./UsagePanel";
import { DiagnosticsPanel } from "./DiagnosticsPanel";
import { PluginsPanel } from "./PluginsPanel";
import { ArchivedPanel } from "./ArchivedPanel";
import { IconDownload, IconRefresh } from "../icons";
import { useUpdaterStore } from "../../store/updater";
import {
  IconAnchor, IconBarChart, IconBookOpen, IconBrain, IconCalendar,
  IconCpu, IconInfo, IconPalette, IconPlug, IconRotateCcw, IconSettings,
  IconShield, IconTool, IconUsers, IconZap, IconBox,
} from "../icons";

export type SettingsTab =
  | "general" | "appearance"
  | "models" | "skills" | "subagents" | "mcp" | "rules"
  | "policy"
  | "scheduled" | "hooks" | "memory" | "usage" | "diagnostics" | "plugins" | "about"
  | "archive";

export interface SettingsIndexItem {
  key: SettingsTab;
  label: string;
  group: "basic" | "agent" | "data";
  keywords: string;
  icon?: React.ReactNode;
}

/** 供命令中心（Cmd+K）搜索：设置项索引 */
export const SETTINGS_INDEX: SettingsIndexItem[] = [
  { key: "general", label: "常规", group: "basic", keywords: "语言 代理 终端 Shell 字体 搜索 todos reasoning", icon: <IconSettings size={15} /> },
  { key: "appearance", label: "外观", group: "basic", keywords: "主题 毛玻璃 布局 字号 颜色 面板", icon: <IconPalette size={15} /> },
  { key: "models", label: "模型设置", group: "basic", keywords: "供应商 模型 上下文 多模态 推理", icon: <IconCpu size={15} /> },
  { key: "plugins", label: "插件", group: "basic", keywords: "插件 组件 替换 slot 外挂", icon: <IconBox size={15} /> },
  { key: "memory", label: "记忆", group: "agent", keywords: "记忆 召回 entries", icon: <IconBrain size={15} /> },
  { key: "skills", label: "技能", group: "agent", keywords: "skill 技能仓库 git 导入", icon: <IconZap size={15} /> },
  { key: "subagents", label: "子智能体", group: "agent", keywords: "子代理 profile 工具白名单", icon: <IconUsers size={15} /> },
  { key: "mcp", label: "MCP 服务器", group: "agent", keywords: "mcp server stdio sse 外部工具", icon: <IconPlug size={15} /> },
  { key: "rules", label: "AI 规则", group: "agent", keywords: "全局规则 项目规则 扫描 命令", icon: <IconBookOpen size={15} /> },
  { key: "hooks", label: "钩子", group: "agent", keywords: "hook 事件 回调", icon: <IconAnchor size={15} /> },
  { key: "policy", label: "执行策略", group: "agent", keywords: "命令 审批 allow deny ask", icon: <IconShield size={15} /> },
  { key: "scheduled", label: "定时任务", group: "data", keywords: "cron 定时 自动化", icon: <IconCalendar size={15} /> },
  { key: "usage", label: "使用统计", group: "data", keywords: "token 用量 统计 context", icon: <IconBarChart size={15} /> },
  { key: "diagnostics", label: "诊断", group: "data", keywords: "健康检查 系统状态 索引", icon: <IconTool size={15} /> },
  { key: "archive", label: "归档恢复", group: "data", keywords: "归档 恢复 已删除 archived restore", icon: <IconRotateCcw size={15} /> },
  { key: "about", label: "关于", group: "basic", keywords: "版本 信息", icon: <IconInfo size={15} /> },
];

export const NAV_GROUPS: Array<{ id: SettingsIndexItem["group"]; label: string }> = [
  { id: "basic", label: "基础设置" },
  { id: "agent", label: "Agent 能力" },
  { id: "data", label: "数据与统计" },
];

function AboutPanel() {
  const status = useUpdaterStore((s) => s.status);
  const appVersion = useUpdaterStore((s) => s.appVersion);
  const checkForUpdates = useUpdaterStore((s) => s.checkForUpdates);
  const downloadUpdate = useUpdaterStore((s) => s.downloadUpdate);
  const installUpdate = useUpdaterStore((s) => s.installUpdate);

  // dev 模式无 preload 更新 API，显示纯静态信息
  const supported = status.state !== "unsupported";
  const hint =
    status.state === "checking" ? "正在检查更新…" :
    status.state === "available" ? `发现新版本 v${status.version}（当前 v${appVersion}）` :
    status.state === "downloading" ? `正在下载更新 ${status.percent}%…` :
    status.state === "downloaded" ? `新版本 v${status.version} 已就绪` :
    status.state === "none" ? `已是最新版本（v${appVersion}）` :
    status.state === "error" ? `检查更新失败：${status.message}` : "";

  const updateBtn =
    status.state === "downloaded" ? (
      <button className="btn btn-primary btn-sm" onClick={() => void installUpdate()}><IconDownload size={13} /> 重启更新</button>
    ) : status.state === "available" ? (
      <button className="btn btn-primary btn-sm" onClick={() => void downloadUpdate()}><IconDownload size={13} /> 下载更新</button>
    ) : status.state === "downloading" ? (
      <button className="btn btn-sm" disabled>{status.percent}%</button>
    ) : null;

  return (
    <div className="settings-card">
      <RowItem title="ChatCoder" desc="项目任务驱动的 AI 编码工作台" />
      <RowItem title="当前版本" desc={appVersion ? `v${appVersion}` : "v0.1.0"} />
      {supported && (
        <div className="settings-row">
          <div className="settings-row-info">
            <div className="settings-row-title">软件更新</div>
            <div className="settings-row-desc">{hint || "从 GitHub Releases 自动检查新版本"}</div>
          </div>
          <div className="settings-row-control">
            {updateBtn}
            <button className="btn btn-ghost btn-sm" disabled={status.state === "checking" || status.state === "downloading"} onClick={() => void checkForUpdates()}><IconRefresh size={13} /> 检查更新</button>
          </div>
        </div>
      )}
    </div>
  );
}
function RowItem({ title, desc }: { title: string; desc: string }) {
  return <div className="settings-row"><div className="settings-row-info"><div className="settings-row-title">{title}</div><div className="settings-row-desc">{desc}</div></div><div className="settings-row-control" /></div>;
}

function Panel({ tab }: { tab: SettingsTab }) {
  switch (tab) {
    case "general": return <div className="settings-content-inner"><div className="settings-page-title">常规</div><div className="settings-page-subtitle">语言、代理、终端与显示选项</div><GeneralPanel /></div>;
    case "appearance": return <div className="settings-content-inner"><div className="settings-page-title">外观</div><div className="settings-page-subtitle">主题、毛玻璃、布局与个性化外观</div><AppearancePanel /></div>;
    case "models": return <div className="settings-content-inner"><div className="settings-page-title">模型管理</div><div className="settings-page-subtitle">按供应商配置模型：填 URL/Key 后扫描，勾选启用并设置上下文 / 多模态</div><div className="settings-card"><ModelsPanel /></div></div>;
    case "skills": return <div className="settings-content-inner"><div className="settings-page-title">技能管理</div><div className="settings-page-subtitle">可被 Agent 加载的 Skill 资源</div><div className="settings-card"><SkillsPanel /></div></div>;
    case "subagents": return <div className="settings-content-inner"><div className="settings-page-title">子代理</div><div className="settings-page-subtitle">子代理类型配置：工具白名单、模型覆盖与系统提示词</div><div className="settings-card"><SubagentsPanel /></div></div>;
    case "mcp": return <div className="settings-content-inner"><div className="settings-page-title">MCP 服务器</div><div className="settings-page-subtitle">连接外部工具与数据源</div><div className="settings-card"><McpPanel /></div></div>;
    case "rules": return <div className="settings-content-inner"><div className="settings-page-title">AI 规则</div><div className="settings-page-subtitle">全局 / 项目规则，以及多 AI 软件规则文档的扫描与启用</div><div className="settings-card"><RulesPanel /></div></div>;
    case "policy": return <div className="settings-content-inner"><div className="settings-page-title">执行策略</div><div className="settings-page-subtitle">控制命令执行审批规则，allow 放行 / deny 拒绝 / ask 需审批</div><div className="settings-card"><PolicyPanel /></div></div>;
    case "scheduled": return <div className="settings-content-inner"><div className="settings-page-title">定时任务</div><div className="settings-card"><ScheduledPanel /></div></div>;
    case "hooks": return <div className="settings-content-inner"><div className="settings-page-title">钩子</div><div className="settings-card"><HooksPanel /></div></div>;
    case "memory": return <div className="settings-content-inner"><div className="settings-page-title">记忆</div><div className="settings-card"><MemoryPanel /></div></div>;
    case "usage": return <div className="settings-content-inner-wide"><div className="settings-page-title">用量统计</div><div className="settings-page-subtitle">整个软件的 token 用量：总数、趋势与各模型分布</div><div className="settings-card"><UsagePanel /></div></div>;
    case "diagnostics": return <div className="settings-content-inner"><div className="settings-page-title">诊断</div><div className="settings-page-subtitle">系统健康检查</div><div className="settings-card"><DiagnosticsPanel /></div></div>;
    case "archive": return <div className="settings-content-inner"><div className="settings-page-title">归档恢复</div><div className="settings-page-subtitle">已归档的项目与会话，支持一键恢复</div><div className="settings-card"><ArchivedPanel /></div></div>;
    case "plugins": return <div className="settings-content-inner"><div className="settings-page-title">插件</div><div className="settings-page-subtitle">系统组件插件化：查看可替换的 slot 组件，像拼积木一样替换内置/外挂组件</div><div className="settings-card"><PluginsPanel /></div></div>;
    case "about": return <div className="settings-content-inner"><div className="settings-page-title">关于</div><div className="settings-card"><AboutPanel /></div></div>;
    default: return null;
  }
}

/** v19: 设置右侧内容区（主布局 main 内渲染，顶部 TitleBar 与左栏宽度共用）。
 * tab 状态由 App 持有（左栏 SettingsSidebar 与内容区共享）。 */
export function SettingsContent({ tab }: { tab: SettingsTab }) {
  return (
    <div className="settings-content" style={{ height: "100%", overflowY: "auto" }}>
      <Panel tab={tab} />
    </div>
  );
}
