/** 设置中心（v19 并入主布局）：左侧导航由 SettingsSidebar（SidebarShell）渲染，
 * 本文件提供设置项索引（SETTINGS_INDEX，供命令中心搜索）与右侧内容区 SettingsContent。
 * 三组：基础设置 / Agent 能力 / 数据与统计。
 */
import { useEffect, useState } from "react";
import { AppearancePanel } from "./AppearancePanel";
import { GeneralPanel } from "./GeneralPanel";
import { ModelsPanel } from "./ModelsPanel";
import { InstalledPanel } from "./InstalledPanel";
import { SubagentsPanel } from "./SubagentsPanel";
import { RulesPanel } from "./RulesPanel";
import { ScheduledPanel } from "./ScheduledPanel";
import { PolicyPanel } from "./PolicyPanel";
import { HooksPanel } from "./HooksPanel";
import { MemoryPanel } from "./MemoryPanel";
import { UsagePanel } from "./UsagePanel";
import { DiagnosticsPanel } from "./DiagnosticsPanel";
import { IndexLibraryPanel } from "./IndexLibraryPanel";
import { ArchivedPanel } from "./ArchivedPanel";
import { WorktreesPanel } from "./WorktreesPanel";
import { PetsPanel } from "./PetsPanel";
import { IconDownload, IconRefresh } from "../icons";
import { MarkdownContent } from "../MarkdownContent";
import { Card, PageShell, PageTransition } from "../ui";
import { AppLogo } from "../AppLogo";
import { useUpdaterStore } from "../../store/updater";
import { useI18n } from "../../store/i18n";
import {
  IconAnchor, IconBarChart, IconBookOpen, IconBrain, IconCalendar,
  IconChevronDown,
  IconCpu, IconInfo, IconPalette, IconRotateCcw, IconSettings,
  IconShield, IconTool, IconUsers, IconBox, IconGitBranch, IconPet,
} from "../icons";

export type SettingsTab =
  | "general" | "appearance"
  | "models" | "extensions" | "subagents" | "rules"
  | "policy"
  | "scheduled" | "hooks" | "memory" | "usage" | "diagnostics" | "about"
  | "index"
  | "archive"
  | "worktrees"
  | "pets";

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
  { key: "pets", label: "宠物", group: "basic", keywords: "宠物 pet petdex 桌面 浮窗 任务 状态 徽标 胶囊", icon: <IconPet size={15} /> },
  { key: "models", label: "模型设置", group: "basic", keywords: "供应商 模型 上下文 多模态 推理", icon: <IconCpu size={15} /> },
  { key: "memory", label: "记忆", group: "agent", keywords: "记忆 召回 entries", icon: <IconBrain size={15} /> },
  // plan-282-1441（#6）：插件 / 技能 / MCP 三个独立页收拢为一个「拓展」页（子标签分区）
  { key: "extensions", label: "拓展", group: "agent", keywords: "插件 市场 skill 技能 技能仓库 git 导入 mcp 连接器 外部工具 slot", icon: <IconBox size={15} /> },
  { key: "subagents", label: "子智能体", group: "agent", keywords: "子代理 profile 工具白名单", icon: <IconUsers size={15} /> },
  { key: "rules", label: "AI 规则", group: "agent", keywords: "全局规则 项目规则 扫描 命令", icon: <IconBookOpen size={15} /> },
  { key: "hooks", label: "钩子", group: "agent", keywords: "hook 事件 回调", icon: <IconAnchor size={15} /> },
  { key: "policy", label: "执行策略", group: "agent", keywords: "命令 审批 allow deny ask", icon: <IconShield size={15} /> },
  { key: "scheduled", label: "自动化", group: "data", keywords: "cron 定时 自动化 任务", icon: <IconCalendar size={15} /> },
  { key: "usage", label: "使用统计", group: "data", keywords: "token 用量 统计 context", icon: <IconBarChart size={15} /> },
  { key: "index", label: "索引库", group: "data", keywords: "索引 符号 codegraph symbol 代码探索", icon: <IconCpu size={15} /> },
  { key: "worktrees", label: "工作树", group: "data", keywords: "worktree 工作树 分支 隔离 合并 git", icon: <IconGitBranch size={15} /> },
  { key: "diagnostics", label: "诊断", group: "data", keywords: "健康检查 系统状态 checkpoint", icon: <IconTool size={15} /> },
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
  const releaseHistory = useUpdaterStore((s) => s.releaseHistory);
  const releaseSource = useUpdaterStore((s) => s.releaseSource);
  const loadReleaseHistory = useUpdaterStore((s) => s.loadReleaseHistory);
  // plan-230-1144 M4.2: 更新说明展开态与历史区展开态
  const [notesOpen, setNotesOpen] = useState<boolean>(true);
  const [historyOpen, setHistoryOpen] = useState<boolean>(false);
  // S7（plan-41-197）：更新历史展示重构——版本行可逐条展开/收起（默认展开最新版本），
  // 长历史分段加载；头部给出条目数摘要，便于快速定位“这次更新了什么”。
  const [expandedVersions, setExpandedVersions] = useState<Set<string>>(new Set());
  const [historyLimit, setHistoryLimit] = useState(5);

  useEffect(() => { void loadReleaseHistory(); }, [loadReleaseHistory]);

  // S7：首次拉到历史时自动展开最新版本（用户最关心“这次更新了什么”）
  useEffect(() => {
    if (releaseHistory.length === 0) return;
    setExpandedVersions((prev) => {
      if (prev.size > 0) return prev;
      const first = releaseHistory[0];
      return new Set([`${first.version}-0`]);
    });
  }, [releaseHistory]);

  /** S7：更新条目计数（只数 "- 条目"，分组标题不计） */
  const countItems = (notes?: string) =>
    (notes || "").split("\n").filter((l) => /^\s*-\s+/.test(l)).length;

  // dev 模式无 preload 更新 API，显示纯静态信息
  const supported = status.state !== "unsupported";
  const hint =
    status.state === "checking" ? "正在检查更新…" :
    status.state === "available" ? `发现新版本 v${status.version}（当前 v${appVersion}）` :
    status.state === "downloading" ? `正在下载更新 ${status.percent}%…` :
    status.state === "downloaded" ? `新版本 v${status.version} 已就绪` :
    status.state === "none" ? `已是最新版本（v${appVersion}）` :
    status.state === "error" ? `检查更新失败：${status.message}` : "";

  // plan-230-1144 M4.2: 状态机携带的 release notes（available/downloaded 时可用）
  const activeNotes = (status.state === "available" || status.state === "downloaded") ? (status.notes || "") : "";
  const activeVersion = (status.state === "available" || status.state === "downloaded") ? status.version : "";

  const updateBtn =
    status.state === "downloaded" ? (
      <button className="btn btn-primary btn-sm" onClick={() => void installUpdate()}><IconDownload size={13} /> 重启更新</button>
    ) : status.state === "available" ? (
      <button className="btn btn-primary btn-sm" onClick={() => void downloadUpdate()}><IconDownload size={13} /> 下载更新</button>
    ) : null;

  const fmtBytes = (n?: number) => {
    if (!n || n <= 0) return "";
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
    return `${(n / 1024 / 1024).toFixed(1)} MB`;
  };

  return (
    <div className="settings-card">
      <div className="settings-row">
        <div className="settings-row-info" style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <AppLogo size={36} />
          <div>
            <div className="settings-row-title">ChatCoder</div>
            <div className="settings-row-desc">项目任务驱动的 AI 编码工作台</div>
          </div>
        </div>
        <div className="settings-row-control" />
      </div>
      <RowItem title="当前版本" desc={appVersion ? `v${appVersion}` : "v0.1.0"} />
      {supported && (
        <>
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

          {/* 下载进度条（此前仅显示百分数文本；transferred/total/bytesPerSecond 数据主进程早已提供） */}
          {status.state === "downloading" && (
            <div className="upd-progress-wrap">
              <div className="upd-progress-bar"><div className="upd-progress-fill" style={{ width: `${status.percent}%` }} /></div>
              <div className="upd-progress-meta">
                <span>{status.percent}%</span>
                {status.total ? <span>{fmtBytes(status.transferred)} / {fmtBytes(status.total)}</span> : null}
                {status.bytesPerSecond ? <span>{fmtBytes(status.bytesPerSecond)}/s</span> : null}
              </div>
            </div>
          )}

          {/* 更新说明：版本可用/已就绪时内联展示（问题6 核心修复——更新前可见更新内容） */}
          {activeNotes && (
            <div className="upd-notes">
              <button className="upd-notes-head" onClick={() => setNotesOpen((v) => !v)}>
                <IconChevronDown size={12} className={"upd-notes-caret" + (notesOpen ? " open" : "")} />
                新版本 v{activeVersion} 更新内容
              </button>
              {notesOpen && (
                <div className="upd-notes-body release-notes">
                  <MarkdownContent>{activeNotes}</MarkdownContent>
                </div>
              )}
            </div>
          )}
          {status.state === "error" && (
            <div className="settings-row">
              <div className="settings-row-info">
                <div className="settings-row-desc" style={{ color: "var(--error)" }}>{status.message}</div>
              </div>
              <div className="settings-row-control">
                <button className="btn btn-ghost btn-sm" onClick={() => void checkForUpdates()}><IconRefresh size={13} /> 重试</button>
                <button className="btn btn-ghost btn-sm" onClick={() => { void navigator.clipboard.writeText(status.message).catch(() => { /* ignore */ }); }}>复制错误</button>
              </div>
            </div>
          )}

          {/* 完整更新历史（时间线，按版本分组；离线回落本地 CHANGELOG） */}
          <div className="settings-row upd-history-row">
            <div className="settings-row-info">
              <div className="settings-row-title">更新历史</div>
              <div className="settings-row-desc">
                {releaseSource === "local" ? "离线模式：显示本地更新日志" :
                 releaseSource === "none" ? "暂无法获取更新历史（网络不可用且无本地日志）" :
                 `共 ${releaseHistory.length} 个版本`}
              </div>
            </div>
            <div className="settings-row-control">
              <button className="btn btn-ghost btn-sm" onClick={() => setHistoryOpen((v) => !v)}>
                <IconChevronDown size={12} className={"upd-notes-caret" + (historyOpen ? " open" : "")} />
                {historyOpen ? "收起" : "查看"}
              </button>
            </div>
          </div>
          {historyOpen && (
            <div className="upd-history">
              {releaseHistory.length === 0 && <div className="navpage-empty">暂无更新历史</div>}
              {releaseHistory.slice(0, historyLimit).map((r, i) => {
                const isCurrent = r.version === appVersion;
                const key = `${r.version}-${i}`;
                const expanded = expandedVersions.has(key);
                const count = countItems(r.notes);
                return (
                  <div key={key} className={"upd-history-item" + (isCurrent ? " current" : "")}>
                    {/* S7：版本行可点击——默认只看到“版本 + 日期 + 条目数”，
                        点击展开该版本完整内容，避免多个版本的长文一次性铺开。 */}
                    <button
                      type="button"
                      className={"upd-history-head" + (expanded ? " open" : "")}
                      aria-expanded={expanded}
                      onClick={() => setExpandedVersions((prev) => {
                        const next = new Set(prev);
                        if (next.has(key)) next.delete(key); else next.add(key);
                        return next;
                      })}
                    >
                      <span className="upd-history-version">
                        {r.version ? `v${r.version}` : (r.name || "更新日志")}
                        {isCurrent && <span className="upd-history-cur">当前版本</span>}
                        {r.date && <span className="upd-history-date">{new Date(r.date).toLocaleDateString()}</span>}
                      </span>
                      <span className="upd-history-meta">
                        {count > 0 && <span>{count} 条更新</span>}
                        <IconChevronDown size={12} className={"upd-notes-caret" + (expanded ? " open" : "")} />
                      </span>
                    </button>
                    {expanded && (
                      <div className="upd-history-notes release-notes"><MarkdownContent>{r.notes || "(无说明)"}</MarkdownContent></div>
                    )}
                  </div>
                );
              })}
              {releaseHistory.length > historyLimit && (
                <button className="btn btn-ghost btn-sm upd-history-more" onClick={() => setHistoryLimit((n) => n + 10)}>
                  显示更早的版本（还有 {releaseHistory.length - historyLimit} 个）
                </button>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}
function RowItem({ title, desc }: { title: string; desc: string }) {
  return <div className="settings-row"><div className="settings-row-info"><div className="settings-row-title">{title}</div><div className="settings-row-desc">{desc}</div></div><div className="settings-row-control" /></div>;
}

/** plan-282-1416（问题1 根治）：设置页装饰——tab → 内容组件 / 宽度档位。
 *
 * 旧实现把「限宽容器 + 标题 + 副标题」在 17 个 case 里各写一遍，导致：
 *  - 切 tab 时整块 DOM 重建（闪一下、无过渡）；
 *  - 680 / 960 两套宽度混用，切换时标题与卡片左边界位移；
 *  - 副标题用 margin-top:-8px 反向补偿 flex gap，长文案换行时行高错位。
 * 现改为数据驱动：骨架只渲染一次（PageShell），内容组件按 tab 查表。
 */
const PANELS: Record<SettingsTab, { Comp: React.ComponentType; width?: "standard" | "wide"; card?: boolean; selfHeader?: boolean; fill?: boolean }> = {
  // general / appearance / memory / about 自带 settings-card(-stack) 结构，不再外包 Card
  general: { Comp: GeneralPanel },
  appearance: { Comp: AppearancePanel },
  // S6（plan-41-197）：模型页自带单卡片左右衔接布局，不外包 Card（避免“卡片套卡片”）
  // S18（plan-41-197b）：自带标题区（模型设置 + 副标题 + 刷新），外层不再渲染标题
  models: { Comp: ModelsPanel, selfHeader: true },
  // plan-41-198：拓展页 = 已安装管理（市场在左面板「拓展」）；自带标题区 +
  // 头部固定、列表独立滚动（fill）
  extensions: { Comp: InstalledPanel, selfHeader: true, fill: true },
  subagents: { Comp: SubagentsPanel, card: true },
  rules: { Comp: RulesPanel, card: true },
  // policy / usage 含表格与图表，走宽档（唯一允许的宽度差异）
  policy: { Comp: PolicyPanel, width: "wide", card: true },
  scheduled: { Comp: ScheduledPanel, card: true },
  hooks: { Comp: HooksPanel, card: true },
  // S19（plan-41-197c）：记忆页头部固定、列表独立滑动——走填充式布局（fill）
  memory: { Comp: MemoryPanel, fill: true },
  usage: { Comp: UsagePanel, width: "wide", card: true },
  diagnostics: { Comp: DiagnosticsPanel, card: true },
  index: { Comp: IndexLibraryPanel, card: true },
  worktrees: { Comp: WorktreesPanel, width: "wide", card: true },
  archive: { Comp: ArchivedPanel, card: true },
  about: { Comp: AboutPanel },
  // plan-73-323 / plan-73-326：宠物分区（面板自带 settings-card 结构，不再外包 Card——
  // 此前 card:true 会形成“卡片套卡片”，与「设置 → 常规」的排版不一致）
  pets: { Comp: PetsPanel },
};

function Panel({ tab }: { tab: SettingsTab }) {
  const { t } = useI18n();
  const meta = PANELS[tab];
  if (!meta) return null;
  const { Comp, width = "standard", card = false, selfHeader = false, fill = false } = meta;
  // 副标题仅在该 tab 确有文案时渲染（缺省不占位，保证卡片区起点恒定）
  // S18：selfHeader 的 tab（模型页）由内容组件自渲染标题区，外层整体不渲染标题。
  const subtitle = selfHeader ? "" : t(`settings.ps.${tab}`);
  // plan-282-1421（第3项）：tab 切换过渡——只动 transform/opacity；
  // 外层滚动容器（PageShell 的 .ui-page）不随 tab 重建，滚动位置与宽度均不跳变。
  return (
    <PageTransition id={tab} direction="left">
      <PageShell
        title={selfHeader ? undefined : t(`settings.pt.${tab}`)}
        subtitle={subtitle || undefined}
        width={width}
        fill={fill}
      >
        {card ? <Card>{<Comp />}</Card> : <Comp />}
      </PageShell>
    </PageTransition>
  );
}

/** v19: 设置右侧内容区（主布局 main 内渲染，顶部 TitleBar 与左栏宽度共用）。
 * tab 状态由 App 持有（左栏 SettingsSidebar 与内容区共享）。
 *
 * plan-282-1416（问题1 根治）：此处不再自带滚动容器与内联 style——
 * 旧实现这里又写了一份 `height:100%; overflowY:auto`，与 global.css 的
 * `.settings-content`（flex+padding）语义冲突，造成滚动容器与限宽容器分层、
 * 滚动条出现/消失时内容盒宽度跳变。现由 PageShell 独占滚动并恒定预留 gutter。 */
export function SettingsContent({ tab }: { tab: SettingsTab }) {
  return <Panel tab={tab} />;
}
