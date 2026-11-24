/** 设置中心（v19 并入主布局）：左侧导航由 SettingsSidebar（SidebarShell）渲染，
 * 本文件提供设置项索引（SETTINGS_INDEX，供命令中心搜索）与右侧内容区 SettingsContent。
 * 三组：基础设置 / Agent 能力 / 数据与统计。
 */
import { SettingsGlyph } from "../iconkit/SettingsGlyphs";
import { lazy, Suspense, useEffect, useState } from "react";
const AppearancePanel = lazy(() => import("./AppearancePanel").then((module) => ({ default: module.AppearancePanel })));
const GeneralPanel = lazy(() => import("./GeneralPanel").then((module) => ({ default: module.GeneralPanel })));
const ModelsPanel = lazy(() => import("./ModelsPanel").then((module) => ({ default: module.ModelsPanel })));
const InstalledPanel = lazy(() => import("./InstalledPanel").then((module) => ({ default: module.InstalledPanel })));
const SubagentsPanel = lazy(() => import("./SubagentsPanel").then((module) => ({ default: module.SubagentsPanel })));
const WorkflowSettingsPanel = lazy(() => import("./WorkflowSettingsPanel").then((module) => ({ default: module.WorkflowSettingsPanel })));
const RulesPanel = lazy(() => import("./RulesPanel").then((module) => ({ default: module.RulesPanel })));
const ScheduledPanel = lazy(() => import("./ScheduledPanel").then((module) => ({ default: module.ScheduledPanel })));
const PolicyPanel = lazy(() => import("./PolicyPanel").then((module) => ({ default: module.PolicyPanel })));
const HooksPanel = lazy(() => import("./HooksPanel").then((module) => ({ default: module.HooksPanel })));
const MemoryPanel = lazy(() => import("./MemoryPanel").then((module) => ({ default: module.MemoryPanel })));
const UsagePanel = lazy(() => import("./UsagePanel").then((module) => ({ default: module.UsagePanel })));
const DiagnosticsPanel = lazy(() => import("./DiagnosticsPanel").then((module) => ({ default: module.DiagnosticsPanel })));
const IndexLibraryPanel = lazy(() => import("./IndexLibraryPanel").then((module) => ({ default: module.IndexLibraryPanel })));
const ArchivedPanel = lazy(() => import("./ArchivedPanel").then((module) => ({ default: module.ArchivedPanel })));
const WorktreesPanel = lazy(() => import("./WorktreesPanel").then((module) => ({ default: module.WorktreesPanel })));
const PetsPanel = lazy(() => import("./PetsPanel").then((module) => ({ default: module.PetsPanel })));
// plan-334-1661：电脑操控专用设置页（内核常驻，不作为 MCP 注册，故不进连接器页）
const DesktopPanel = lazy(() => import("./DesktopPanel").then((module) => ({ default: module.DesktopPanel })));
import { IconAlertCircle, IconArrowDown, IconCheck, IconCopy, IconRefresh, IconSpinner } from "../icons";
import { Button, CardRow, Collapse, PageShell, PageTransition, Section, Switch, Tooltip } from "../ui";
import { AppLogo } from "../AppLogo";
import { ReleaseNotesView, UpdateProgress, countReleaseItems } from "../update/ReleaseNotes";
const PlaygroundPanel = lazy(() => import("./PlaygroundPanel").then((module) => ({ default: module.PlaygroundPanel })));
import { useUpdaterStore, type ChannelProbe, type ChannelSnapshot, type UpdateStatus, UPDATE_CHANNEL_IDS } from "../../store/updater";
import { useI18n } from "../../store/i18n";
import { IconChevronDown } from "../icons";

export type SettingsTab =
  | "general" | "appearance"
  | "models" | "extensions" | "subagents" | "workflows" | "rules"
  | "policy"
  | "scheduled" | "hooks" | "memory" | "usage" | "diagnostics" | "about"
  | "index"
  | "archive"
  | "worktrees"
  | "pets"
  | "desktop"
  | "playground";

export interface SettingsIndexItem {
  key: SettingsTab;
  label: string;
  group: "basic" | "agent" | "data";
  keywords: string;
  icon?: React.ReactNode;
}

/** 供命令中心（Cmd+K）搜索：设置项索引 */
export const SETTINGS_INDEX: SettingsIndexItem[] = [
  { key: "general", label: "常规", group: "basic", keywords: "语言 代理 终端 Shell 字体 搜索 todos reasoning 上下文 脱敏 零宽 关键词", icon: <SettingsGlyph name="general" /> },
  { key: "appearance", label: "外观", group: "basic", keywords: "主题 毛玻璃 布局 字号 颜色 面板", icon: <SettingsGlyph name="appearance" /> },
  { key: "pets", label: "宠物", group: "basic", keywords: "宠物 pet petdex 桌面 浮窗 任务 状态 徽标 胶囊", icon: <SettingsGlyph name="pets" /> },
  // plan-334-1661：电脑操控（内核常驻的内置工具，不注册为 MCP，因此不在连接器页）
  { key: "desktop", label: "电脑操控", group: "agent", keywords: "电脑操控 computer use 桌面 鼠标 键盘 截图 uia 无障碍 操作路线 recipe", icon: <SettingsGlyph name="desktop" /> },
  { key: "models", label: "模型设置", group: "basic", keywords: "供应商 模型 上下文 多模态 推理", icon: <SettingsGlyph name="models" /> },
  { key: "memory", label: "记忆", group: "agent", keywords: "记忆 召回 entries", icon: <SettingsGlyph name="memory" /> },
  // plan-282-1441（#6）：插件 / 技能 / MCP 三个独立页收拢为一个「拓展」页（子标签分区）
  { key: "extensions", label: "拓展", group: "agent", keywords: "插件 市场 skill 技能 技能仓库 git 导入 mcp 连接器 外部工具 slot", icon: <SettingsGlyph name="extensions" /> },
  { key: "subagents", label: "智能体", group: "agent", keywords: "子代理 探索 执行 成员 模型 提示词 并发 操控", icon: <SettingsGlyph name="subagents" /> },
  { key: "workflows", label: "工作流", group: "agent", keywords: "工作流 并发 上限 子代理 返工 预算 步骤 运行时", icon: <SettingsGlyph name="workflows" /> },
  { key: "rules", label: "AI 规则", group: "agent", keywords: "全局规则 项目规则 扫描 命令", icon: <SettingsGlyph name="rules" /> },
  { key: "hooks", label: "钩子", group: "agent", keywords: "hook 事件 回调", icon: <SettingsGlyph name="hooks" /> },
  { key: "policy", label: "执行策略", group: "agent", keywords: "命令 审批 allow deny ask", icon: <SettingsGlyph name="policy" /> },
  { key: "scheduled", label: "自动化", group: "data", keywords: "cron 定时 自动化 任务", icon: <SettingsGlyph name="scheduled" /> },
  { key: "usage", label: "使用统计", group: "data", keywords: "token 用量 统计 context", icon: <SettingsGlyph name="usage" /> },
  { key: "index", label: "索引库", group: "data", keywords: "索引 符号 codegraph symbol 代码探索", icon: <SettingsGlyph name="index" /> },
  { key: "worktrees", label: "工作树", group: "data", keywords: "worktree 工作树 分支 隔离 合并 git", icon: <SettingsGlyph name="worktrees" /> },
  { key: "diagnostics", label: "诊断", group: "data", keywords: "健康检查 系统状态 checkpoint", icon: <SettingsGlyph name="diagnostics" /> },
  { key: "archive", label: "归档恢复", group: "data", keywords: "归档 恢复 已删除 archived restore", icon: <SettingsGlyph name="archive" /> },
  { key: "about", label: "关于", group: "basic", keywords: "版本 信息", icon: <SettingsGlyph name="about" /> },
  // plan-114-449：嗒嗒的字块乐园（原创小游戏，放在数据组末尾——设置页里可以喘口气的地方）
  { key: "playground", label: "嗒嗒乐园", group: "data", keywords: "游乐场 小游戏 嗒嗒 彩蛋 playground", icon: <SettingsGlyph name="playground" /> },
];

export const NAV_GROUPS: Array<{ id: SettingsIndexItem["group"]; label: string }> = [
  { id: "basic", label: "基础设置" },
  { id: "agent", label: "Agent 能力" },
  { id: "data", label: "数据与统计" },
];

/** 关于页（plan-405-1900 重排）：
 *  - 顶部身份区用 AppLogo，与左上角应用图标保持一致（此前是嗒嗒插画，两处形象对不上）；
 *  - 「软件更新」收成一行：固定槽位的状态图标 + 主文案 + 操作按钮，下载中在下方给进度条，
 *    有新版本时内联展示更新内容；
 *  - 更新历史为版本行列表（水平分隔），展开后与首启弹窗共用 ReleaseNotesView 排版。 */
/** 下载中各阶段的一句话说明（点击下载后马上是「连接中」，而不是等第一个数据块才有变化） */
function downloadPhaseDesc(s: Extract<UpdateStatus, { state: "downloading" }>): string {
  switch (s.phase) {
    case "connecting": return "正在连接更新服务器…";
    case "waiting": return "网络已断开，恢复后会自动接着下载，已下载的部分不会丢";
    case "retrying":
      // plan-20261022-update-route：换线路时说清「哪条线路不通、几秒后试哪条」，不再只有一句「正在重试」
      if (s.nextLabel && s.retryInMs != null) {
        const sec = Math.max(1, Math.round(s.retryInMs / 1000));
        return `${s.source || "当前线路"}连接失败，${s.retryInMs < 1000 ? "正在" : `${sec} 秒后`}尝试${s.nextLabel}，已下载的部分不会丢`;
      }
      return s.attempt && s.attempt > 1 ? `网络不稳定，正在重试（第 ${s.attempt} 次），已下载的部分不会丢` : "网络不稳定，正在重试，已下载的部分不会丢";
    case "verifying": return "下载完成，正在校验安装包…";
    default: return "下载在后台进行，可以继续使用；断网会自动续传";
  }
}

/* ── plan-20261024-update-channel：下载通道卡片 ─────────────────────────────── */

/** 字节速率 → 人读文案（与主进程上报的 bytesPerSecond 同单位：字节/秒） */
function formatSpeed(bytesPerSecond: number): string {
  if (!Number.isFinite(bytesPerSecond) || bytesPerSecond <= 0) return "";
  const mb = bytesPerSecond / (1024 * 1024);
  if (mb >= 1) return `${mb.toFixed(1)} MB/s`;
  return `${Math.max(1, Math.round(bytesPerSecond / 1024))} KB/s`;
}

/** 单通道测速摘要：`0.18s · 12.4 MB/s`；未测速 / 失败各有单独文案 */
function channelMetaText(probe: ChannelProbe | undefined): string {
  if (!probe) return "未测速";
  if (!probe.ok) return "不可用";
  const sec = (probe.ms / 1000).toFixed(2);
  const speed = formatSpeed(probe.bytesPerSecond);
  return speed ? `${sec}s · ${speed}` : `${sec}s`;
}

/** 相对时间：`12 秒前`（只用于「上次测速」，超过一天就不再折算） */
function formatAgo(testedAt: number, now: number): string {
  const sec = Math.max(0, Math.round((now - testedAt) / 1000));
  if (sec < 5) return "刚刚";
  if (sec < 60) return `${sec} 秒前`;
  const min = Math.round(sec / 60);
  if (min < 60) return `${min} 分钟前`;
  const hour = Math.round(min / 60);
  if (hour < 24) return `${hour} 小时前`;
  return `${Math.round(hour / 24)} 天前`;
}

function UpdateChannelCard({ channels, downloading }: { channels: ChannelSnapshot; downloading: boolean }) {
  const measureChannels = useUpdaterStore((s) => s.measureChannels);
  const setChannel = useUpdaterStore((s) => s.setChannel);
  const { report, preferred, effective, mode, measuring, labels } = channels;
  // 「上次测速」是相对时间：状态不会自己变，挂一个秒级心跳让它保持真实
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  // 手动指定了非最优线路：给一行「当前线路较慢 + 改用最快」
  const manualSlower = mode !== "auto" && preferred != null && mode !== preferred;
  const bestName = preferred ? labels[preferred] : "";

  return (
    <div className="about-ch">
      <div className="about-ch-head">
        <div>
          <div className="about-ch-title">下载通道</div>
          <div className="about-ch-desc">检查更新时自动测速，默认使用最快的一条</div>
        </div>
        <Button
          variant="ghost"
          size="sm"
          icon={<IconRefresh size={13} />}
          loading={measuring}
          disabled={measuring}
          onClick={() => void measureChannels()}
        >
          重新测速
        </Button>
      </div>

      <div className="about-ch-grid" role="group" aria-label="下载通道">
        {UPDATE_CHANNEL_IDS.map((id) => {
          const on = effective === id;
          const probe = report?.channels?.[id];
          const tag = mode === "auto" && preferred === id;
          const metaClass = "about-ch-meta"
            + (measuring || !probe ? " is-wait" : !probe.ok ? " is-err" : "");
          return (
            <Tooltip key={id} content={probe && !probe.ok && probe.reason ? probe.reason : null}>
              <Button
                type="button"
                variant={on ? "secondary" : "outline"}
                size="sm"
                aria-pressed={on}
                className="about-ch-choice"
                onClick={() => { if (mode !== id) void setChannel(id); }}
              >
                <span className="about-ch-top">
                  <span className="about-ch-name">{labels[id]}</span>
                  {tag && <span className="about-ch-tag">推荐</span>}
                  {/* 勾选图标常驻槽位（只切 opacity），切换通道时行内不抖动 */}
                  <IconCheck size={14} className="about-ch-check" aria-hidden />
                </span>
                <span className={metaClass}>
                  {measuring ? "测速中…" : channelMetaText(probe)}
                </span>
              </Button>
            </Tooltip>
          );
        })}
      </div>

      <Collapse open={manualSlower} className="in-gap">
        <div className="about-ch-tip">
          <IconAlertCircle size={14} className="about-ch-tip-ic" aria-hidden />
          <span className="about-ch-tip-text">当前线路较慢</span>
          <Button variant="secondary" size="sm" onClick={() => void setChannel(preferred ?? "auto")}>
            改用 {bestName}
          </Button>
        </div>
      </Collapse>

      <div className="about-ch-foot">
        <span>{report != null && report.testedAt > 0 ? `上次测速 ${formatAgo(report.testedAt, now)}` : "尚未测速"}</span>
        {/* 手动指定后必须留一条回到「按测速自动选」的路，否则一旦点过就再也回不去 */}
        {downloading && <span>下载中切换通道会重新开始</span>}
        {mode !== "auto" && (
          <Button type="button" variant="ghost" size="xs" onClick={() => void setChannel("auto")}>
            恢复自动优选
          </Button>
        )}
      </div>
    </div>
  );
}

function AboutPanel() {
  const status = useUpdaterStore((s) => s.status);
  const appVersion = useUpdaterStore((s) => s.appVersion);
  const checkForUpdates = useUpdaterStore((s) => s.checkForUpdates);
  const downloadUpdate = useUpdaterStore((s) => s.downloadUpdate);
  const cancelDownload = useUpdaterStore((s) => s.cancelDownload);
  const switchToPrimary = useUpdaterStore((s) => s.switchToPrimary);
  const installUpdate = useUpdaterStore((s) => s.installUpdate);
  const releaseHistory = useUpdaterStore((s) => s.releaseHistory);
  const releaseSource = useUpdaterStore((s) => s.releaseSource);
  const loadReleaseHistory = useUpdaterStore((s) => s.loadReleaseHistory);
  const autoDownload = useUpdaterStore((s) => s.autoDownload);
  const setAutoDownload = useUpdaterStore((s) => s.setAutoDownload);
  const channels = useUpdaterStore((s) => s.channels);
  const [expandedVersions, setExpandedVersions] = useState<Set<string>>(new Set());
  const [historyLimit, setHistoryLimit] = useState(5);

  useEffect(() => { void loadReleaseHistory(); }, [loadReleaseHistory]);

  // 首次拉到历史时自动展开最新版本（用户最关心“这次更新了什么”）
  useEffect(() => {
    if (releaseHistory.length === 0) return;
    setExpandedVersions((prev) => (prev.size > 0 ? prev : new Set([`${releaseHistory[0].version}-0`])));
  }, [releaseHistory]);

  // dev 模式无 preload 更新 API，只显示静态信息
  const supported = status.state !== "unsupported";
  const cur = appVersion ? `v${appVersion}` : "";
  const sourceText = releaseSource === "mirror" ? "镜像源" : "GitHub Releases";

  const view: { tone: "ok" | "new" | "busy" | "err" | "idle"; icon: React.ReactNode; title: string; desc: string } =
    status.state === "checking" ? { tone: "busy", icon: <IconSpinner size={16} />, title: "正在检查更新…", desc: `当前 ${cur}` } :
    status.state === "available" ? { tone: "new", icon: <IconArrowDown size={16} />, title: `发现新版本 v${status.version}`, desc: `当前 ${cur}，下载完成后重启即可更新` } :
    status.state === "downloading" ? {
      tone: "busy",
      icon: status.phase === "connecting" ? <IconSpinner size={16} /> : <IconArrowDown size={16} />,
      title: `正在下载${status.version ? ` v${status.version}` : "更新"}${status.auto ? "（后台自动下载）" : ""}`,
      desc: downloadPhaseDesc(status),
    } :
    status.state === "downloaded" ? { tone: "new", icon: <IconRefresh size={16} />, title: `v${status.version} 已准备就绪`, desc: "重启应用即可完成更新" } :
    status.state === "none" ? { tone: "ok", icon: <IconCheck size={16} />, title: "已是最新版本", desc: `${cur} · 来自 ${sourceText}` } :
    status.state === "error" ? { tone: "err", icon: <IconAlertCircle size={16} />, title: "检查更新失败", desc: status.message } :
    { tone: "idle", icon: <IconRefresh size={16} />, title: "软件更新", desc: `当前 ${cur}，自动从 ${sourceText} 检查新版本` };

  // 下载中也保留「更新内容」（主进程把 notes 带进 downloading 状态），用户等待时可以先看看更新了什么
  const hasNotes = status.state === "available" || status.state === "downloading" || status.state === "downloaded";
  const activeVersion = hasNotes ? (status.version ?? "") : "";
  const activeNotes = hasNotes ? (status.notes || "") : "";
  const busy = status.state === "checking" || status.state === "downloading";
  // 校验阶段已经下完，取消没有意义（也会白白丢掉一次完整下载）
  const cancellable = status.state === "downloading" && status.phase !== "verifying";
  // 落到 GitHub 兜底线路：提示慢速，并给一键切回自建源（重置线路优先级后接着下）
  const onSlowRoute = status.state === "downloading" && status.tier === "github" && status.phase !== "verifying";

  const toggleVersion = (key: string) => setExpandedVersions((prev) => {
    const next = new Set(prev);
    if (next.has(key)) next.delete(key); else next.add(key);
    return next;
  });

  return (
    <div className="settings-card-stack about-stack">
      <Section className="about-card" flush>
        <div className="about-hero">
          <AppLogo size={64} className="about-logo" />
          <div className="about-hero-text">
            <div className="about-name">ChatCoder</div>
            <div className="about-tagline">项目任务驱动的 AI 编码工作台</div>
          </div>
          {cur && <span className="about-ver">{cur}</span>}
        </div>

        {supported && (
          <div className="about-upd">
            <div className="about-upd-row">
              <span className={"about-upd-ic tone-" + view.tone} aria-hidden>{view.icon}</span>
              <div className="about-upd-text">
                <div className="about-upd-title">{view.title}</div>
                <div className={"about-upd-desc" + (view.tone === "err" ? " is-err" : "")}>{view.desc}</div>
              </div>
              <div className="about-upd-actions">
                {status.state === "error" && (
                  <Button variant="ghost" size="sm" icon={<IconCopy size={13} />} onClick={() => { void navigator.clipboard.writeText(status.message).catch(() => { /* ignore */ }); }}>复制错误</Button>
                )}
                {status.state === "downloading" ? (
                  <Button variant="secondary" size="sm" disabled={!cancellable} onClick={() => void cancelDownload()}>取消下载</Button>
                ) : status.state !== "downloaded" && (
                  <Button variant={status.state === "available" ? "ghost" : "secondary"} size="sm" disabled={busy} onClick={() => void checkForUpdates()}>
                    {status.state === "error" ? "重试" : "检查更新"}
                  </Button>
                )}
                {status.state === "available" && (
                  <Button variant="primary" size="sm" icon={<IconArrowDown size={13} />} onClick={() => void downloadUpdate()}>下载更新</Button>
                )}
                {status.state === "downloaded" && (
                  <Button variant="primary" size="sm" icon={<IconRefresh size={13} />} onClick={() => void installUpdate()}>重启并更新</Button>
                )}
              </div>
            </div>

            {status.state === "downloading" && (
              <UpdateProgress
                percent={status.percent} transferred={status.transferred} total={status.total}
                bytesPerSecond={status.bytesPerSecond} phase={status.phase} source={status.source}
              />
            )}

            <Collapse open={onSlowRoute} className="in-gap">
              <div className="about-upd-slow">
                <IconAlertCircle size={14} className="about-upd-slow-ic" aria-hidden />
                <span className="about-upd-slow-text">当前使用 GitHub 慢速下载，镜像线路暂时连不上</span>
                <Button variant="secondary" size="sm" icon={<IconRefresh size={13} />} onClick={() => void switchToPrimary()}>切换镜像线路</Button>
              </div>
            </Collapse>

            {activeNotes && (
              <div className="about-upd-notes">
                <div className="about-sub-title">v{activeVersion} 更新内容</div>
                <ReleaseNotesView notes={activeNotes} />
              </div>
            )}
          </div>
        )}
      </Section>

      {/* plan-20261121-file-tree-settings：下载来源和自动下载属于同一组设置，使用同层卡片。 */}
      {supported && <Section flush className="about-download">
        {channels && <UpdateChannelCard channels={channels} downloading={status.state === "downloading"} />}
        <CardRow title="发现新版本时自动下载"
          desc="检查到新版本后自动下载，完成后在侧栏提示重启更新。网络中断时会从已下载的位置继续。">
          <Switch checked={autoDownload} onChange={(v) => { void setAutoDownload(v); }} aria-label="发现新版本时自动下载" />
        </CardRow>
      </Section>}

      {supported && (
        <Section title="更新历史" className="about-history-card" flush
          desc={releaseSource === "local" ? "离线模式，显示本地更新日志" :
               releaseSource === "none" ? "暂时无法获取更新历史" :
               releaseHistory.length > 0 ? `共 ${releaseHistory.length} 个版本` : ""}>
          <div className="about-history">
            {releaseHistory.length === 0 && <div className="about-history-empty">暂无更新历史</div>}
            {releaseHistory.slice(0, historyLimit).map((r, i) => {
              const isCurrent = !!appVersion && r.version === appVersion;
              const key = `${r.version}-${i}`;
              const expanded = expandedVersions.has(key);
              const count = countReleaseItems(r.notes);
              return (
                <div key={key} className={"about-rel" + (expanded ? " open" : "")}>
                  <Button variant="ghost" size="sm" className="about-rel-head" aria-expanded={expanded} onClick={() => toggleVersion(key)}>
                    <span className="about-rel-ver">{r.version ? `v${r.version}` : (r.name || "更新日志")}</span>
                    {isCurrent && <span className="about-rel-cur">当前</span>}
                    <span className="about-rel-meta">
                      {r.date && <span>{new Date(r.date).toLocaleDateString()}</span>}
                      {count > 0 && <span>{count} 项</span>}
                    </span>
                    <IconChevronDown size={14} className={"about-rel-caret" + (expanded ? " open" : "")} />
                  </Button>
                  <Collapse open={expanded}>
                    <div className="about-rel-body"><ReleaseNotesView notes={r.notes} /></div>
                  </Collapse>
                </div>
              );
            })}
            {releaseHistory.length > historyLimit && (
              <Button variant="ghost" size="sm" className="about-history-more" onClick={() => setHistoryLimit((n) => n + 10)}>
                显示更早的版本（{releaseHistory.length - historyLimit}）
              </Button>
            )}
          </div>
        </Section>
      )}
    </div>
  );
}

/** plan-282-1416（问题1 根治）：设置页装饰——tab → 内容组件 / 宽度档位。
 *
 * 旧实现把「限宽容器 + 标题 + 副标题」在 17 个 case 里各写一遍，导致：
 *  - 切 tab 时整块 DOM 重建（闪一下、无过渡）；
 *  - 680 / 960 两套宽度混用，切换时标题与卡片左边界位移；
 *  - 副标题用 margin-top:-8px 反向补偿 flex gap，长文案换行时行高错位。
 * 现改为数据驱动：骨架只渲染一次（PageShell），内容组件按 tab 查表。
 */
const PANELS: Record<SettingsTab, { Comp: React.ComponentType; width?: "standard" | "wide" | "xwide"; selfHeader?: boolean; fill?: boolean }> = {
  // 各设置面板自行通过 Section / Card / List 组织内容，外层不再套 Card
  general: { Comp: GeneralPanel },
  appearance: { Comp: AppearancePanel },
  // S6（plan-41-197）：模型页自带单卡片左右衔接布局，不外包 Card（避免“卡片套卡片”）
  // plan-114-450：模型页也走统一页头 + 填充式（左右两列在卡片内各自滚动），宽档容纳双栏
  models: { Comp: ModelsPanel, fill: true, width: "wide" },
  // plan-114-450：标题区交还 PageShell（统一页头），「市场」经 PageActions 投送到右上角
  extensions: { Comp: InstalledPanel, fill: true },
  // plan-114-450：以下面板使用 Section（组标题 + 卡片）组织内容
  // plan-20261123-agent-panel-ui：双栏成员工作区需要 wide 档（880px）容纳左右列表与提示词工作台
  subagents: { Comp: SubagentsPanel, width: "wide" },
  workflows: { Comp: WorkflowSettingsPanel },
  rules: { Comp: RulesPanel },
  // policy / usage 含表格与多栏编辑器，走宽档（唯一允许的宽度差异）
  policy: { Comp: PolicyPanel, width: "wide" },
  scheduled: { Comp: ScheduledPanel },
  hooks: { Comp: HooksPanel },
  // S19（plan-41-197c）：记忆页头部固定、列表独立滑动——走填充式布局（fill）
  memory: { Comp: MemoryPanel, fill: true },
  // plan-20261021-usage-update：调用记录表有 10 列，用 xwide 档；面板自带卡片（概览 / 调用记录各自成卡），不再外包 Card
  usage: { Comp: UsagePanel, width: "xwide" },
  diagnostics: { Comp: DiagnosticsPanel },
  index: { Comp: IndexLibraryPanel },
  worktrees: { Comp: WorktreesPanel },
  archive: { Comp: ArchivedPanel },
  about: { Comp: AboutPanel },
  // plan-73-323 / plan-73-326：宠物分区
  pets: { Comp: PetsPanel },
  // plan-334-1661：电脑操控
  desktop: { Comp: DesktopPanel },
  // plan-114-449：嗒嗒乐园，宽档画布
  playground: { Comp: PlaygroundPanel, width: "wide" },
};

function Panel({ tab }: { tab: SettingsTab }) {
  const { t } = useI18n();
  const meta = PANELS[tab];
  if (!meta) return null;
  const { Comp, width = "standard", selfHeader = false, fill = false } = meta;
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
        <Comp />
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
  return <Suspense fallback={<div className="settings-loading" role="status">正在加载设置…</div>}><Panel tab={tab} /></Suspense>;
}
