/** 设置中心（v13 全屏页对齐 ZCode）：左侧「返回工作区」+ 分组图标导航 + 右侧内容区。
 * 三组：基础设置 / Agent 能力 / 数据与统计。SETTINGS_INDEX 供命令中心（Cmd+K）搜索跳转。 */
import { useEffect, useRef, useState } from "react";
import { useUiStore } from "../../store/ui";
import { ResizeHandle } from "../ResizeHandle";
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
import {
  IconAnchor, IconArrowLeft, IconBarChart, IconBookOpen, IconBrain, IconCalendar,
  IconCpu, IconInfo, IconPalette, IconPlug, IconSettings,
  IconShield, IconTool, IconUsers, IconZap,
} from "../icons";

export type SettingsTab =
  | "general" | "appearance"
  | "models" | "skills" | "subagents" | "mcp" | "rules"
  | "policy"
  | "scheduled" | "hooks" | "memory" | "usage" | "diagnostics" | "about";

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
  { key: "about", label: "关于", group: "basic", keywords: "版本 信息", icon: <IconInfo size={15} /> },
];

const NAV_GROUPS: Array<{ id: SettingsIndexItem["group"]; label: string }> = [
  { id: "basic", label: "基础设置" },
  { id: "agent", label: "Agent 能力" },
  { id: "data", label: "数据与统计" },
];

function AboutPanel() {
  return <div className="settings-card"><RowItem title="ChatCoder" desc="项目任务驱动的 AI 编码工作台" /></div>;
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
    case "usage": return <div className="settings-content-inner"><div className="settings-page-title">用量统计</div><div className="settings-page-subtitle">整个软件的 token 用量：总数与各模型分布</div><div className="settings-card"><UsagePanel /></div></div>;
    case "diagnostics": return <div className="settings-content-inner"><div className="settings-page-title">诊断</div><div className="settings-page-subtitle">系统健康检查</div><div className="settings-card"><DiagnosticsPanel /></div></div>;
    case "about": return <div className="settings-content-inner"><div className="settings-page-title">关于</div><div className="settings-card"><AboutPanel /></div></div>;
    default: return null;
  }
}

export function SettingsPage({ onBack, initialTab }: { onBack: () => void; initialTab?: string }) {
  const [tab, setTab] = useState<SettingsTab>((initialTab as SettingsTab) || "general");
  useEffect(() => { if (initialTab) setTab(initialTab as SettingsTab); }, [initialTab]);
  // v1.1: 左侧导航可拖拽调宽（160~400，持久化）
  const navWidth = useUiStore((s) => s.settingsNavWidth);
  const setNavWidth = useUiStore((s) => s.setSettingsNavWidth);
  const navRef = useRef<HTMLElement>(null);
  useEffect(() => {
    if (navRef.current) navRef.current.style.width = `${navWidth}px`;
  }, [navWidth]);
  return (
    <div className="settings-page-overlay">
      <nav className="settings-nav" ref={navRef} style={{ width: `${navWidth}px` }}>
        <div className="settings-back" onClick={onBack}>
          <IconArrowLeft size={14} /> 返回工作区
        </div>
        {NAV_GROUPS.map((g) => (
          <div key={g.id} className="settings-nav-group">
            <div className="settings-nav-group-label">{g.label}</div>
            {SETTINGS_INDEX.filter((it) => it.group === g.id).map((it) => (
              <div key={it.key} className={"settings-nav-item" + (tab === it.key ? " active" : "")} onClick={() => setTab(it.key)}>
                <span className="settings-nav-icon">{it.icon}</span>
                {it.label}
              </div>
            ))}
          </div>
        ))}
      </nav>
      <ResizeHandle side="left" baseWidth={navWidth} minWidth={160} maxWidth={400}
                    panelEl={navRef} onCommit={setNavWidth} />
      <div className="settings-content">
        <Panel tab={tab} />
      </div>
    </div>
  );
}
