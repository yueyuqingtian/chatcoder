/** 右侧面板（v5）：多标签页 + 全屏按钮 + 加号菜单修复。
 * v19: 标签溢出治理——tab 不再被 flex 压缩（flex-shrink:0 + 文字省略），
 *      支持滚轮横滚；溢出时头部出现「全部标签」下拉，可直接跳转/关闭。
 * plan-20261009-ui U8：标签条改为「图标 + 文案」胶囊（关闭按钮占固定槽位，悬停才出现，不抖动）；
 *      加号 / 全部标签菜单统一为 Popover（点外部、Esc、进出场）；头部按钮统一 28px 并用 Tooltip；
 *      空态由四条灰色长条改为 2×2 工具启动卡（角色色磁贴，闲置灰度、聚焦彩色）。
 *      例外：「微应用」磁贴是强调卡，常驻角色色（--pc-cyan），不遵循闲置灰度。
 */
import { IxTasks, IxBrowser, IxTerminal, IxFiles, IxBot, IxBug, IxWorkflow, IxExtensions } from "../iconkit/StackIcons";
import { lazy, memo, Suspense, useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, type MicroAppOut } from "../../api/client";
import { isBusy, subscribe } from "../../perf/bus";
import { usePanelStore, bucketSessionId, panelTabKey } from "../../store/panel";
import { sessionKey, useBrowserStore } from "../../store/browser";
import type { PanelTab, PanelTabId } from "../../store/panel";
import { useI18n } from "../../store/i18n";
import { useChatStore } from "../../store/chat";
import { useMicroAppsStore } from "../../store/microapps";
import { describeExtError, useMicroappsExtStore } from "../../store/microappsExt";
import { notify } from "../../store/toast";
import { openMicroAppWindow } from "../../utils/microappWindow";
import {
  IconCheck, IconChevronDown, IconGitBranch, IconMessageSquare, IconPlus, IconPuzzle, IconRotateCcw, IconX,
} from "../icons";
import { Badge, Button, Collapse, Popover, Tooltip } from "../ui";
import { TaskSummaryPanel } from "./TaskSummaryPanel";
import { BrowserPanel } from "./BrowserPanel";
import { SubagentPanel } from "./SubagentPanel";
import { DebugPanel } from "./DebugPanel";
import { WorkflowPanel } from "../workflow/WorkflowPanel";
import { SidePanel } from "./SidePanel";
import { GitPanel } from "./GitPanel";
import { MicroAppPanel } from "./MicroAppPanel";
import { ErrorBoundary } from "../ErrorBoundary";

/** 文件树（内含 Monaco 编辑器）与终端（内含 xterm）体积大，只在对应标签打开时才加载，不进首屏包。
 *  占位层与面板同样撑满剩余空间，加载瞬间不产生布局跳动。 */
const FileTreePanel = lazy(() => import("./FileTreePanel").then((m) => ({ default: m.FileTreePanel })));
const TerminalPanel = lazy(() => import("./TerminalPanel").then((m) => ({ default: m.TerminalPanel })));
const LazyPanelFallback = () => <div style={{ flex: 1, minHeight: 0 }} aria-busy="true" />;

/** plan-75-334 阶段3：面板内容按 tab/sessionId/visible 记忆化。
 *  此前标签列表无关变化（新增/关闭其它标签、全屏切换等）会重建**全部**面板内容，
 *  包括已保活的终端与浏览器；memo 后只有本标签自身的可见性与 tab 变化才重渲染。 */
const PanelContent = memo(function PanelContent({ tab, sessionId, visible }: { tab: PanelTab; sessionId: number | null; visible: boolean }) {
  switch (tab.id) {
    case "task-summary": return <TaskSummaryPanel visible={visible} />;
    case "browser": return <BrowserPanel sessionId={sessionId} visible={visible} />;
    case "terminal": return (
      <Suspense fallback={<LazyPanelFallback />}>
        <TerminalPanel tab={tab} sessionId={sessionId} visible={visible} />
      </Suspense>
    );
    case "files": return (
      <Suspense fallback={<LazyPanelFallback />}>
        <FileTreePanel key={`files-${sessionId ?? "home"}`} visible={visible} sessionId={sessionId} />
      </Suspense>
    );
    case "subagent": return <SubagentPanel threadId={tab.meta?.threadId} agentName={tab.meta?.agentName} visible={visible} />;
    case "debug": return <DebugPanel sessionId={sessionId} />;
    case "workflow": return <WorkflowPanel sessionId={sessionId} visible={visible} />;
    case "side": return <SidePanel visible={visible} />;
    case "git": return <GitPanel key={`git-${sessionId ?? "home"}`} sessionId={sessionId} visible={visible} />;
    case "microapp": return (
      <MicroAppPanel
        appId={tab.meta?.appId ?? ""}
        version={tab.meta?.version ?? ""}
        entry={tab.meta?.entry}
        visible={visible}
        // plan-693-2868 · 协议 §6：uid 是宿主桥的实例标识（webview ?uid= 的来源）。
        // 顶层 tab.uid 是权威值（panelTabKey 用它），老状态没有 uid 时退回同名 key。
        uid={tab.uid ?? panelTabKey(tab)}
        // 关键：传的是**本标签所属会话桶**的 sessionId（GuardedPanelContent 的入参来自
        // bucketSessionId(bKey)），不是当前活动会话——后台会话里的微应用发起命令时，
        // 后端按它定位工作区，落到当前活动会话就会把命令跑进用户正在看的项目（协议 §7）。
        sessionId={sessionId}
        // plan-external-microapps：外置微应用直连它自己的 http 地址（不带宿主桥、不拼 uid）
        externalUrl={tab.meta?.externalUrl}
      />
    );
    default: return null;
  }
});

/**
 * 单个面板内容的错误隔离层：某块面板（浏览器 / 终端 / 文件树）抛错时只让该面板
 * 显示局部兜底，不再冒泡到 App 顶层 ErrorBoundary 把整个应用打成白屏。
 * resetKey 用「桶 + tab key」：切 tab 或重开同名 tab 时自动复位并重试渲染。
 */
const GuardedPanelContent = memo(function GuardedPanelContent({ tabKey, tab, sessionId, visible }: {
  tabKey: string;
  tab: PanelTab;
  sessionId: number | null;
  visible: boolean;
}) {
  return (
    <ErrorBoundary variant="panel" resetKey={tabKey}>
      <PanelContent tab={tab} sessionId={sessionId} visible={visible} />
    </ErrorBoundary>
  );
});

/** 标签图标（与加号菜单、启动卡共用同一套） */
function tabIcon(id: PanelTabId, size: number) {
  switch (id) {
    case "task-summary": return <IxTasks size={size} />;
    case "browser": return <IxBrowser size={size} />;
    case "terminal": return <IxTerminal size={size} />;
    case "files": return <IxFiles size={size} />;
    case "subagent": return <IxBot size={size} />;
    case "debug": return <IxBug size={size} />;
    case "workflow": return <IxWorkflow size={size} />;
    case "side": return <IconMessageSquare size={size} />;
    case "git": return <IconGitBranch size={size} />;
    case "microapp": return <IxExtensions size={size} />;
  }
}

/** 启动卡「微应用」展开区的 id：磁贴用 aria-controls 指向它，读屏才知道这枚按钮控制的是哪一块。
 *  列表走 Collapse（lazy）折叠时节点不存在，所以只在展开时挂 aria-controls（见 microAppTile）。 */
const APPS_LIST_ID = "rp-launch-apps";

export function RightPanel({ visible = true }: { visible?: boolean }) {
  const { t } = useI18n();
  // tabs/activeKey 为「当前会话桶」的投影（见 store/panel.ts）；buckets 用于跨会话保活渲染
  const tabs = usePanelStore((s) => s.tabs);
  const activeKey = usePanelStore((s) => s.activeKey);
  const buckets = usePanelStore((s) => s.buckets);
  const activeBucket = usePanelStore((s) => s.activeBucket);
  const hotSessions = useChatStore((s) => Object.keys(s.sessionState).join(","));
  const openTab = usePanelStore((s) => s.openTab);
  const openNewTab = usePanelStore((s) => s.openNewTab);
  const closeTab = usePanelStore((s) => s.closeTab);
  const closedStack = usePanelStore((s) => s.closedStack);
  const reopenClosedTab = usePanelStore((s) => s.reopenClosedTab);
  const setActiveTab = usePanelStore((s) => s.setActiveTab);
  const [showAddMenu, setShowAddMenu] = useState(false);
  const [showTabsMenu, setShowTabsMenu] = useState(false);
  // plan-20261015-browser-agent：当前会话的 AI 正在操作浏览器——「浏览器」标签上给出状态点
  const browserAgentBusy = useBrowserStore((s) => (s.sessions[sessionKey(bucketSessionId(activeBucket))]?.agent.phase ?? "idle") === "working");
  /** 连接器「开发调试」是否已启用——启用后才提供调试面板入口（需求：如果 mcp 开启了）。
   *  面板入口是低频操作，在打开「+」菜单时重查即可，无需全局订阅。 */
  const [debugEnabled, setDebugEnabled] = useState(false);
  const refreshDebugEnabled = () => {
    void api.listMcpServers()
      .then((list) => setDebugEnabled(list.some((m) => m.name === "debugger" && m.is_active)))
      .catch(() => setDebugEnabled(false));
  };
  useEffect(refreshDebugEnabled, []);
  const tabsRef = useRef<HTMLDivElement>(null);
  const addBtnRef = useRef<HTMLButtonElement>(null);
  const tabsBtnRef = useRef<HTMLButtonElement>(null);
  const [tabsOverflow, setTabsOverflow] = useState(false);

  const tabLabel = (tTab: PanelTab): string => {
    const base: Record<PanelTabId, string> = {
      "task-summary": t("rp.tab_task_summary"),
      browser: t("rp.tab_browser"),
      terminal: t("rp.tab_terminal"),
      files: t("rp.tab_files"),
      subagent: t("rp.tab_subagent"),
      debug: t("rp.tab_debug"),
      workflow: t("rp.tab_workflow"),
      side: "侧边对话",
      git: "Git",
      microapp: "微应用",
    };
    if (tTab.id === "subagent" && tTab.meta?.agentName) return tTab.meta.agentName.slice(0, 10);
    // plan-693-2868：微应用标签标题取 appId（manifest.name 需要异步读，标签条不等异步数据）
    if (tTab.id === "microapp" && tTab.meta?.appId) return tTab.meta.appId.slice(0, 12);
    // 序号按「当前标签条里的同类型顺序」实时算：标签关闭后不会留下「终端 3」这类空编号
    const sameKind = tabs.filter((t) => t.id === tTab.id).indexOf(tTab);
    const ordinal = sameKind >= 0 ? sameKind + 1 : tTab.instance;
    return ordinal > 1 ? `${base[tTab.id]} ${ordinal}` : base[tTab.id];
  };

  // 标签条溢出检测（scrollWidth > clientWidth 时展示「全部标签」入口）。
  // plan-329-1647 S5：两个布局读（scrollWidth / clientWidth）在运动期跳过——
  // 拖分隔条/折叠面板时标签条宽度每帧都变，逐帧读会升级为强制同步布局。
  // 运动中只记账，运动结束由 PerfBus 订阅补一次。
  useEffect(() => {
    const el = tabsRef.current;
    if (!el) return;
    let missed = false;
    const check = () => {
      if (isBusy()) { missed = true; return; }
      missed = false;
      const overflow = el.scrollWidth > el.clientWidth + 2;
      setTabsOverflow(overflow);
      // 面板被拖窄后激活的标签可能被挤出可视区：溢出时顺手把它滚回来
      if (overflow) el.querySelector<HTMLElement>(".rp-tab.is-active")?.scrollIntoView({ block: "nearest", inline: "nearest" });
    };
    check();
    const ro = new ResizeObserver(check);
    ro.observe(el);
    const off = subscribe((busy) => { if (!busy && missed) check(); });
    return () => { ro.disconnect(); off(); };
  }, [tabs.length]);

  // 激活的标签滚入可视区（标签很多、从菜单跳转时尤其需要）
  useEffect(() => {
    const el = tabsRef.current?.querySelector<HTMLElement>(".rp-tab.is-active");
    el?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [activeKey]);

  // plan-91-393：激活标签的底色滑块——量出激活标签位置，滑块用弹簧曲线跟随移动
  const [ind, setInd] = useState<{ x: number; w: number } | null>(null);
  useLayoutEffect(() => {
    const el = tabsRef.current?.querySelector<HTMLElement>(".rp-tab.is-active");
    setInd(el ? { x: el.offsetLeft, w: el.offsetWidth } : null);
  }, [activeKey, tabs.length]);

  // 滚轮纵转横：标签过多时直接滚轮浏览
  const onTabsWheel = (e: React.WheelEvent) => {
    const el = tabsRef.current;
    if (!el || el.scrollWidth <= el.clientWidth) return;
    e.preventDefault();
    el.scrollLeft += e.deltaY;
  };

  const handleAdd = (id: PanelTabId) => { openTab(id); setShowAddMenu(false); };
  const handleAddTerminal = () => { openNewTab("terminal"); setShowAddMenu(false); };

  // ── plan-693-2868（方案 C + A）：微应用快捷入口 ───────────────────────────
  // 已装清单与「/」菜单、工具卡「查看界面」共用 store 里那一份（store 侧按 loadedAt 去重，
  // 打开菜单只调 refreshIfStale()，不无脑 load(true)——那会每次开菜单都扫一遍磁盘库）。
  const microApps = useMicroAppsStore((s) => s.items);
  // 审查修复①：光看 items 会把「还没取到 / 正在取」当成「一个都没装」——冷启动后端未就绪时
  // items 恒为 []，用户看到「0 个可用」+「还没有微应用，去装一个」，而入口本来就是给已装应用用的。
  // 这里补订阅 loaded / loading，三态分别渲染（见 renderMicroAppBody）。
  const microAppsLoaded = useMicroAppsStore((s) => s.loaded);
  const microAppsLoading = useMicroAppsStore((s) => s.loading);
  /** 空态启动卡里的微应用列表是否就地展开（方案 C：点卡片展开，选中才开标签） */
  const [showAppsList, setShowAppsList] = useState(false);
  const refreshMicroApps = () => { void useMicroAppsStore.getState().refreshIfStale(); };
  useEffect(() => { void useMicroAppsStore.getState().load(); }, []);

  /** plan-external-microapps：外置微应用（自带前后端与进程）与内置清单并列展示。
   *  清单来自主进程扫描，与后端库无关；桌面端不可用时为空数组（入口照常显示内置部分）。 */
  const extItems = useMicroappsExtStore((s) => s.items);
  useEffect(() => { void useMicroappsExtStore.getState().load(); }, []);
  const refreshAllMicroApps = () => {
    refreshMicroApps();
    void useMicroappsExtStore.getState().refreshIfStale();
  };

  /** 两个入口的同一个落点：用户主动点选 → 开标签（openMicroApp 会顺带展开面板，
   *  这是用户点的，符合 docs/app1「面板打开权在用户手上」）。 */
  const openMicroAppTab = (app: MicroAppOut) => {
    usePanelStore.getState().openMicroApp(app.appId, app.version, app.entry ?? undefined);
    setShowAddMenu(false);
    setShowAppsList(false);
  };

  /** 外置微应用：先让主进程把它拉起来拿到真实地址，再在**独立窗口**里打开。
   *  为什么是独立窗口而不是面板标签：外置微应用自带前后端、是一整个小应用，
   *  放在窄面板里既显示不全也与会话绑定无意义（它不参与宿主桥）。
   *  进程由 chatcoder 按声明拉起（协议 §5），所以「点开」必须先启动。 */
  const openExtMicroApp = (app: MicroappsExtStatus) => {
    setShowAddMenu(false);
    setShowAppsList(false);
    const name = app.name || app.id;
    if (app.state === "running" && app.url) {
      void openMicroAppWindow({ appId: app.id, version: app.version || "1.0.0", externalUrl: app.url, name });
      return;
    }
    if (!app.valid) {
      notify.error(`微应用声明不完整：${app.problems.join("；")}`);
      return;
    }
    void useMicroappsExtStore.getState().start(app.id).then((res) => {
      if (res.ok && res.url) {
        void openMicroAppWindow({ appId: app.id, version: app.version || "1.0.0", externalUrl: res.url, name });
      } else {
        notify.error(describeExtError(String(res.error || "")));
      }
    });
  };

  /** 微应用空态①「让 AI 创建」：与「/」菜单（CommandMenu）同一套做法——只把诉求写进输入框、
   *  聚焦过去，**不替用户按发送**（composer-prefill 是既有回填通道）。 */
  const requestCreateMicroApp = () => {
    window.dispatchEvent(new CustomEvent("chatcoder:composer-prefill", { detail: { text: "帮我创建一个微应用：", ifEmpty: true } }));
    setShowAppsList(false);
    setShowAddMenu(false);
  };

  /** 微应用空态②「去拓展里安装」：微应用是拓展的第四类，落到设置 → 拓展（与「/」菜单同一个跳转）。 */
  const goInstallMicroApp = () => {
    window.dispatchEvent(new CustomEvent("chatcoder:open-settings", { detail: { tab: "extensions" } }));
    setShowAppsList(false);
    setShowAddMenu(false);
  };

  /** 微应用清单的一行：内置与外置**同一套结构**（同一个 `.pop-item`、同一个图标槽、
   *  同一种「名称 + 版本」两行布局）。外置微应用不再额外标「外置」也不要状态点——
   *  它在用户视角就是一个装在本机的微应用。
   *  只有菜单里的行才是 `role="menuitem"`：列表里的行若套 `role="listitem"` 会覆盖 button 语义。 */
  const renderMicroAppItem = (app: { appId: string; name: string; version: string }, onOpen: () => void, role?: "menuitem") => (
    <button key={app.appId} type="button" role={role} className="pop-item" onClick={onOpen}>
      {/* 图标用 16：与同一菜单里其它行（entries / 最近关闭）同尺寸，15 会小一档、图标槽不对齐 */}
      <span className="pop-item-icon"><IxExtensions size={16} /></span>
      <span className="pop-item-main">
        <span className="pop-item-title">{app.name || app.appId}</span>
        <span className="pop-item-desc">v{app.version}</span>
      </span>
    </button>
  );

  /** 计数口径（两个入口统一）：只有「已取到清单、且确实有应用」才带数字。
   *  0 个 / 正在读取 / 没取到三种情况都只显示「微应用」——「0 个可用」等于把「还不知道」说成「没有」。 */
  const microAppCount = microAppsLoaded ? microApps.length : 0;
  const microAppCountLabel = microAppCount > 0 ? `${microAppCount} 个可用` : null;

  /** 清单没取到的手动出口（失败时 store 保持 loaded=false，下次打开菜单仍会自动重试）。 */
  const retryMicroApps = () => { void useMicroAppsStore.getState().load(true); };

  /** 一个都没装时的空态（两个入口共用）：一句话 + 两个既有出口，文案与「/」菜单保持一致。
   *  inMenu：写在 `role="menu"` 的浮层里时行必须是 menuitem，否则读屏的菜单模式选不到这两个出口。 */
  const renderMicroAppEmpty = (inMenu: boolean) => (
    <div className="rp-apps-empty">
      <span>还没有微应用</span>
      <span className="rp-apps-empty-actions">
        <Tooltip content="把创建诉求写进输入框，由 AI 生成微应用">
          <Button size="xs" variant="subtle" role={inMenu ? "menuitem" : undefined} icon={<IconPlus size={12} />} onClick={requestCreateMicroApp}>让 AI 创建</Button>
        </Tooltip>
        <Tooltip content="打开设置 → 拓展，安装别人做好的微应用">
          <Button size="xs" role={inMenu ? "menuitem" : undefined} icon={<IconPuzzle size={12} />} onClick={goInstallMicroApp}>去拓展里安装</Button>
        </Tooltip>
      </span>
    </div>
  );

  /** 清单没取到：必须与「一个都没装」分开——冷启动后端还没就绪时 items 也是 []，
   *  沿用空态等于告诉用户「你没装」（入口本来就是给已装应用用的）。 */
  const renderMicroAppUnavailable = (inMenu: boolean) => (
    <div className="rp-apps-empty">
      <span>清单没取到</span>
      <span className="rp-apps-empty-actions">
        <Tooltip content="重新读取本机已安装的微应用">
          <Button size="xs" variant="subtle" role={inMenu ? "menuitem" : undefined} icon={<IconRotateCcw size={12} />} onClick={retryMicroApps}>重试</Button>
        </Tooltip>
      </span>
    </div>
  );

  /** 已装清单的三态（两个入口共用同一份判断，避免两处口径走偏）：
   *  · 还没成功取过 + 正在拉 → 一行「正在读取」，不给"没装"的空态出口；
   *  · 还没成功取过 + 不在拉 → 「清单没取到」+ 重试（load(true)）；
   *  · 已取到 → 0 个才用空态，否则逐行列出。
   *  外置微应用并列在同一个清单里、**与内置行同款**：点它就是打开它的界面（独立窗口），
   *  不额外标「外置」也不要状态点（实现形态不该出现在用户的清单里）。 */
  const renderMicroAppBody = (inMenu: boolean) => {
    const role = inMenu ? "menuitem" as const : undefined;
    const extRows = extItems.map((app) => renderMicroAppItem(
      { appId: app.id, name: app.name || app.id, version: app.version || "1.0.0" },
      () => openExtMicroApp(app),
      role,
    ));
    if (!microAppsLoaded) {
      const builtin = microAppsLoading
        ? <div className="rp-apps-status">正在读取已装微应用</div>
        : renderMicroAppUnavailable(inMenu);
      return extRows.length ? [...extRows, builtin] : builtin;
    }
    if (microApps.length === 0 && extRows.length === 0) return renderMicroAppEmpty(inMenu);
    return [...extRows, ...microApps.map((app) => renderMicroAppItem(app, () => openMicroAppTab(app), role))];
  };

  /** 启动卡（方案 C）：微应用不占固定标签，所以它**不进** entries（entries 同时是「＋」菜单的
   *  固定标签清单），而是在同一张 2×2/3 列卡表里单列一张卡，点它展开已装应用列表。
   *  无 role：这枚按钮的语义就是 button，`role="listitem"` 会覆盖它、让 aria-expanded 变成非法属性。 */
  const microAppTile = (
    <Tooltip key="microapp" content={showAppsList ? "收起列表" : "展开已装微应用列表"} side="bottom">
      <button
        type="button"
        className={"rp-launch-tile is-app" + (showAppsList ? " is-open" : "")}
        aria-expanded={showAppsList}
        aria-controls={showAppsList ? APPS_LIST_ID : undefined}
        onClick={() => { const next = !showAppsList; setShowAppsList(next); if (next) refreshAllMicroApps(); }}
      >
        <span className="rp-launch-ic"><IxExtensions size={18} /></span>
        <span className="rp-launch-name">微应用</span>
        {microAppCountLabel && <Badge size="xs" variant="soft" className="rp-launch-tag">{microAppCountLabel}</Badge>}
      </button>
    </Tooltip>
  );

  /** 启动卡 / 加号菜单共用的入口清单（调试按条件出现） */
  const entries: Array<{ id: PanelTabId; title: string; desc: string; onPick: () => void }> = [
    { id: "task-summary", title: t("rp.tab_task_summary"), desc: t("rp.desc_task"), onPick: () => handleAdd("task-summary") },
    { id: "browser", title: t("rp.tab_browser"), desc: t("rp.desc_browser"), onPick: () => handleAdd("browser") },
    { id: "terminal", title: t("rp.tab_terminal"), desc: t("rp.desc_terminal"), onPick: handleAddTerminal },
    { id: "files", title: t("rp.tab_files_full"), desc: t("rp.desc_files"), onPick: () => handleAdd("files") },
    { id: "workflow", title: t("rp.tab_workflow"), desc: t("rp.desc_workflow"), onPick: () => handleAdd("workflow") },
    { id: "side", title: "侧边对话", desc: "单独提问，可按需挑选主对话的上下文", onPick: () => handleAdd("side") },
    { id: "git", title: "Git", desc: "查看变更、暂存、提交、同步与切换分支", onPick: () => handleAdd("git") },
    ...(debugEnabled ? [{ id: "debug" as const, title: t("rp.tab_debug"), desc: t("rp.desc_debug"), onPick: () => handleAdd("debug") }] : []),
  ];

  /** 启动卡的单张磁贴（沿用原有标记，抽成函数以把微应用卡插到第 3 位）。
   *  同样去掉 `role="listitem"`：磁贴就是按钮（见微应用卡的说明）。 */
  const launchTileOf = (en: (typeof entries)[number]) => (
    <Tooltip key={en.id} content={en.desc} side="bottom">
      <button type="button" className="rp-launch-tile" onClick={en.onPick}>
        <span className="rp-launch-ic">{tabIcon(en.id, 18)}</span>
        <span className="rp-launch-name">{en.title}</span>
      </button>
    </Tooltip>
  );

  return (
    <div className="right-panel">
      <div className="rp-head">
        <div className={"rp-tabs" + (tabsOverflow ? " is-overflow" : "")} ref={tabsRef} onWheel={onTabsWheel} role="tablist">
          {ind && <span className="rp-tab-ind" aria-hidden style={{ transform: `translateX(${ind.x}px)`, width: ind.w }} />}
          {tabs.map((tTab) => {
            const key = panelTabKey(tTab);
            const label = tabLabel(tTab);
            const active = activeKey === key;
            return (
              <div
                key={key}
                className={`rp-tab${active ? " is-active" : ""}`}
                role="tab"
                aria-selected={active}
                tabIndex={0}
                onClick={() => setActiveTab(key)}
                onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setActiveTab(key); } }}
              >
                <span className="rp-tab-ic">{tabIcon(tTab.id, 14)}</span>
                <span className="rp-tab-label">{label}</span>
                {tTab.id === "browser" && browserAgentBusy && <span className="rp-tab-ai-dot" aria-label="AI 正在操作浏览器" />}
                <Tooltip content={t("rp.close_tab")} side="bottom">
                  <button type="button" className="rp-tab-close" aria-label={`${t("rp.close_tab")}：${label}`} onClick={(e) => { e.stopPropagation(); closeTab(key); }}>
                    <IconX size={11} />
                  </button>
                </Tooltip>
              </div>
            );
          })}
          {tabs.length === 0 && <div className="rp-tabs-empty">{t("rp.tabs_empty")}</div>}
        </div>
        <div className="rp-head-actions">
          {/* 没有标签时只有一行空态提示，不提供「全部标签」入口（此前空态文字被判为溢出，出现一枚空下拉） */}
          {tabsOverflow && tabs.length > 0 && (
            <div className="rp-anchor">
              <Tooltip content={t("rp.all_tabs")} side="bottom" disabled={showTabsMenu}>
                <button
                  ref={tabsBtnRef}
                  type="button"
                  className={"rp-hbtn" + (showTabsMenu ? " is-open" : "")}
                  aria-label={t("rp.all_tabs")}
                  aria-expanded={showTabsMenu}
                  onClick={() => { setShowTabsMenu((v) => !v); setShowAddMenu(false); }}
                >
                  {/* 与同排的「新建 +」等大：图标已按 opticalScale 归一到 89.3% 跨度，
                      这里用 14 与加号同尺寸即自动一致，不需再为箭头单独放缩 */}
                  <IconChevronDown size={14} />
                </button>
              </Tooltip>
              <Popover open={showTabsMenu} onClose={() => setShowTabsMenu(false)} placement="bottom-end" className="rp-menu" ignoreRefs={[tabsBtnRef]} role="menu" aria-label={t("rp.all_tabs")}>
                {tabs.map((tTab) => {
                  const key = panelTabKey(tTab);
                  const active = activeKey === key;
                  return (
                    <button key={key} type="button" role="menuitem" className={"pop-item" + (active ? " is-selected" : "")} onClick={() => { setActiveTab(key); setShowTabsMenu(false); }}>
                      <span className="pop-item-icon">{tabIcon(tTab.id, 15)}</span>
                      <span className="pop-item-title">{tabLabel(tTab)}</span>
                      {active && <span className="pop-item-end"><span className="pop-item-check"><IconCheck size={14} /></span></span>}
                    </button>
                  );
                })}
              </Popover>
            </div>
          )}
          <div className="rp-anchor">
            <Tooltip content={t("rp.new_tab")} side="bottom" disabled={showAddMenu}>
              <button
                ref={addBtnRef}
                type="button"
                className={"rp-hbtn" + (showAddMenu ? " is-open" : "")}
                aria-label={t("rp.new_tab")}
                aria-expanded={showAddMenu}
                onClick={() => {
                  const next = !showAddMenu;
                  setShowAddMenu(next);
                  setShowTabsMenu(false);
                  // plan-693-2868：打开菜单时才补一次微应用清单（store 侧按 loadedAt 去重，3s 内不重复拉）
                  if (next) { refreshDebugEnabled(); refreshAllMicroApps(); }
                }}
              >
                <IconPlus size={14} />
              </button>
            </Tooltip>
            <Popover open={showAddMenu} onClose={() => setShowAddMenu(false)} placement="bottom-end" className="rp-menu rp-menu-add" ignoreRefs={[addBtnRef]} role="menu" aria-label={t("rp.new_tab")}>
              {entries.map((en) => (
                <button key={en.id} type="button" role="menuitem" className="pop-item" onClick={en.onPick}>
                  <span className="pop-item-icon">{tabIcon(en.id, 16)}</span>
                  <span className="pop-item-main">
                    <span className="pop-item-title">{en.title}</span>
                    <span className="pop-item-desc">{en.desc}</span>
                  </span>
                </button>
              ))}
              {/* plan-693-2868（方案 A）：「＋」菜单在既有面板分组之后追加「微应用」分组——与空态启动卡
                  共用同一份已装清单；选中一项直接开标签（openMicroApp 会顺带展开面板）。
                  清单没取到 / 一个都没装时都给对应说明与出口，菜单不会空着也不会报错。 */}
              <div className="pop-sep" />
              <div className="pop-label">
                微应用
                {microAppCountLabel && <span>{microAppCountLabel}</span>}
              </div>
              {renderMicroAppBody(true)}
              {closedStack.length > 0 && (
                <>
                  <div className="pop-sep" />
                  <div className="pop-label">{t("rp.recent_closed")}</div>
                  {closedStack.slice(0, 5).map((tTab, i) => (
                    <button key={panelTabKey(tTab)} type="button" role="menuitem" className="pop-item" onClick={() => { reopenClosedTab(i); setShowAddMenu(false); }}>
                      <span className="pop-item-icon"><IconRotateCcw size={14} /></span>
                      <span className="pop-item-title">{tabLabel(tTab)}</span>
                    </button>
                  ))}
                </>
              )}
            </Popover>
          </div>
          {/* plan-20261025-shell：「全屏 / 收起」与窗口按钮合并为右上角全局控件组（WindowControls），
              它压在本标签栏右端——标签栏只按 shell.css 的 --wc-full-w 预留位置，不再自带这两枚按钮 */}
        </div>
      </div>
      <div className="rp-content">
        {/* plan-41-233：跨会话保活渲染——所有会话桶内的标签实例都保持挂载（display 切换可见性），
            切换会话 / 折叠面板都不会卸载终端与浏览器（PTY 不关闭、网页不刷新）。
            可见性 = 「属于当前会话桶」且「是该桶的激活标签」。
            plan-282-1421（第3项）：激活 tab 播放统一过渡（类名 "" → "ui-tab-enter" 触发动画）。 */}
        {Object.entries(buckets).flatMap(([bKey, bucket]) =>
          bucket.tabs.map((tTab) => {
            const key = panelTabKey(tTab);
            const isActive = visible && bKey === activeBucket && key === activeKey;
            const keepMessageView = bKey === activeBucket || hotSessions.split(",").includes(String(bucketSessionId(bKey)));
            // plan-20261015-browser-agent：浏览器标签不能 display:none（网页视口会变 0×0，AI 无法在后台操作），
            // 改为不可见但保留布局；其中的 webview 由 BrowserPanel 停泊在视口内继续渲染
            const keepAlive = !isActive && tTab.id === "browser";
            return (
              <div
                key={`${bKey}:${key}`}
                className={isActive ? "ui-tab-enter" : keepAlive ? "rp-keepalive" : ""}
                style={{ display: isActive || keepAlive ? "block" : "none", height: "100%" }}
                aria-hidden={!isActive || undefined}
              >
                {(tTab.id !== "subagent" || keepMessageView) && <GuardedPanelContent tabKey={`${bKey}:${key}`} tab={tTab} sessionId={bucketSessionId(bKey)} visible={isActive} />}
              </div>
            );
          })
        )}
        {tabs.length === 0 && (
          <div className="rp-launch">
            <div className="rp-launch-inner">
              <div className="rp-launch-head">
                <div className="rp-launch-title">{t("rp.launch_title")}</div>
                <div className="rp-launch-sub">{t("rp.launch_sub")}</div>
              </div>
              {/* plan-693-2868（方案 C）：卡表沿用原有磁贴，「微应用」插在第 3 位（同预览稿
                  docs/app1/10-microapp-quick-entry.html）；其余磁贴顺序不变。
                  不声明 role="list"：磁贴是按钮，配 list 就必须给 listitem，而 listitem 会覆盖 button 语义。 */}
              <div className="rp-launch-list">
                {entries.slice(0, 2).map(launchTileOf)}
                {microAppTile}
                {entries.slice(2).map(launchTileOf)}
              </div>
              {/* plan-693-2868（方案 C）：点「微应用」卡就地展开已装列表——展开/收起统一走 ui/Collapse
                  （不给 {open && <X/>} 硬切）；`.in-gap` + 容器的 --collapse-gap 让折叠态不多占一份 gap。
                  每行是整行宽度，240px 最窄面板里也不会换行、不会横向溢出。
                  role="group" 而不是 list：行是按钮不是 listitem（review：listitem 会吃掉 button 语义），
                  组里还会出现「正在读取 / 清单没取到」这类非列表项内容。 */}
              <Collapse open={showAppsList} lazy className="in-gap">
                <div id={APPS_LIST_ID} className="rp-apps" role="group" aria-label="已装微应用">
                  {renderMicroAppBody(false)}
                </div>
              </Collapse>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
