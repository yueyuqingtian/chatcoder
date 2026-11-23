/**
 * 内置浏览器面板（plan-20261015-browser-agent 重构）
 *
 * 1. 多标签保活：所有标签的 webview 常驻，切换标签 / 面板折叠 / 切换会话都不重载；
 * 2. **停泊**：面板不在屏幕上（折叠、页面态、看别的面板标签、别的会话）或标签不在前台时，
 *    webview 不再 display:none（那会让页面视口变成 0×0，AI 什么都做不了），而是收进视口内 1px 的
 *    近透明容器，保持原有逻辑尺寸继续渲染——AI 在后台照常点击、输入、截图，用户展开面板时页面
 *    原样就在那里（实验依据见 ai/chatcoder-plan-20261015-browser-agent.md §3）；
 * 3. 地址 / 标题 / 图标 / 加载态 / 前进后退以 webview 导航事件为准；导航走命令式 API；
 * 4. AI 操作可视化（光标、高亮、波纹、截图闪白、状态胶囊）与网页弹窗（应用内对话框）；
 * 5. 元素标注、截图发送到输入框、DOM 快照、控制台求值、开发者工具（低频项收进「更多」菜单）。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useChatStore, type BrowserRef } from "../../store/chat";
import { normalizeUrl, sessionKey, useBrowserStore, type BrowserTabState, type ElementInfo } from "../../store/browser";
import { usePanelStore } from "../../store/panel";
import { api, type BrowserBookmarkOut } from "../../api/client";
import { ElementInspector } from "./ElementInspector";
import { BrowserStartPage } from "./BrowserStartPage";
import { BrowserFindBar, type FindGuest } from "./BrowserFindBar";
import { BrowserManagerModals, type BrowserDownload, type BrowserManager, type VaultItem } from "./BrowserManagerModals";
import { BrowserScreenshotAnnotator, type BrowserScreenshot } from "./BrowserScreenshotAnnotator";
import { browserSearchTarget, isBrowserHttpUrl, useBrowserLibrary } from "./browserLibrary";
import { BrowserAgentCapsule, BrowserAgentElsewhere, BrowserAgentOverlay, BrowserPageDialog } from "./BrowserAgentLayer";
import { useFrameBridge, isGuestFrame, type FrameEl } from "./frameBridge";
import {
  findFrameByWcId, getFrame, markFrameReady, noteLoadingStop, noteNavigationStart, pushConsole, registerFrame, unregisterFrame,
} from "../../browserAgent/frames";
import { isBusy, subscribe } from "../../perf/bus";
import { healParkedWrapper, releaseParkHeal } from "../../browserAgent/parkGuard";
import { registerReconcileTask, RECONCILE_ORDER } from "../../perf/reconcile";
import { recordDerivation } from "../../perf/metrics";
import { Button, Collapse, IconButton, Input, Menu, Popover, Textarea, Tooltip } from "../ui";
import { PointContextMenu } from "../ui/ContextMenu";
import {
  IconArrowLeft, IconArrowRight, IconRefresh, IconGlobe, IconTarget, IconArrowUp, IconX, IconCode, IconBug,
  IconTerminal, IconPlus, IconMinus, IconCamera, IconLock, IconMoreHorizontal, IconSparkles, IconSpinner, IconExternalLink,
  IconHistory, IconPin, IconStar, IconFolder, IconDownload, IconLayoutGrid, IconCheck, IconLayers, IconSearch,
} from "../icons";

/** 从未显示过的面板：按「面板宽度 × 窗口高度」估算视口，用户展开时页面尺寸基本不跳变 */
function estimateViewport(): { width: number; height: number } {
  const w = Math.max(320, Math.round(usePanelStore.getState().width) - 2);
  const h = Math.max(360, Math.round(window.innerHeight) - 150);
  return { width: w, height: h };
}

function hostOf(url: string): string {
  try { return new URL(url).host; } catch { return ""; }
}

function safeContextUrl(value: string): string {
  try {
    const url = new URL(value);
    if (url.protocol === "file:") return url.pathname;
    return isBrowserHttpUrl(value) ? url.origin + url.pathname : "about:blank";
  } catch { return "about:blank"; }
}

function redactDiagnostic(value: string): string {
  return value
    .replace(/https?:\/\/[^\s"'<>]+/gi, (url) => safeContextUrl(url))
    .replace(/\b(?:authorization|proxy-authorization|cookie|set-cookie|password|passwd|secret|api[_-]?key|access[_-]?token)\s*["']?\s*[:=][^\n\r]*/gi, "[敏感字段已隐藏]")
    .replace(/\b(?:bearer|basic)\s+[^\s"'<>]+/gi, "[凭据已隐藏]");
}

type LoginOffer = { id: string; origin: string; username: string; update: boolean; tabId: string };
type ElementAnnotation = { x: number; y: number; source: string; info: ElementInfo | null; clickX: number; clickY: number };
type CaptureRegion = { x: number; y: number; width: number; height: number };

export function BrowserPanel({ sessionId, visible = true }: {
  /** plan-41-233：标签所属会话（undefined=跟随当前会话；null=无会话的 home 桶）。 */
  sessionId?: number | null;
  /** 面板内容是否为当前可见标签（RightPanel 传入；不含面板是否折叠） */
  visible?: boolean;
} = {}) {
  const followedSessionId = useChatStore((s) => s.currentSessionId);
  const activeSessionId = sessionId !== undefined ? sessionId : followedSessionId;
  const sKey = sessionKey(activeSessionId);
  const browserState = useBrowserStore((s) => s.getSessionState(activeSessionId));
  const updateSessionState = useBrowserStore((s) => s.updateSessionState);
  const newTab = useBrowserStore((s) => s.newTab);
  const closeTab = useBrowserStore((s) => s.closeTab);
  const restoreClosedTab = useBrowserStore((s) => s.restoreClosedTab);
  const goHome = useBrowserStore((s) => s.goHome);
  const setActiveTab = useBrowserStore((s) => s.setActiveTab);
  const updateTab = useBrowserStore((s) => s.updateTab);
  const navigate = useBrowserStore((s) => s.navigate);
  const goBack = useBrowserStore((s) => s.goBack);
  const goForward = useBrowserStore((s) => s.goForward);
  const reload = useBrowserStore((s) => s.reload);
  const stop = useBrowserStore((s) => s.stop);
  const noteUserActive = useBrowserStore((s) => s.noteUserActive);
  const panelOnScreen = usePanelStore((s) => s.onScreen);
  const putBrowserRef = useChatStore((s) => s.addComposerBrowserRef);
  const addComposerBrowserRef = (ref: BrowserRef) => putBrowserRef({ ...ref, sessionId: activeSessionId });
  const bookmarks = useBrowserLibrary((s) => s.bookmarks);
  const history = useBrowserLibrary((s) => s.history);
  const bookmarkBar = useBrowserLibrary((s) => s.bookmarkBar);
  const refreshBookmarks = useBrowserLibrary((s) => s.refreshBookmarks);
  const refreshHistory = useBrowserLibrary((s) => s.refreshHistory);
  const toggleBookmarkBar = useBrowserLibrary((s) => s.toggleBookmarkBar);

  const isElectron = typeof window !== "undefined" && Boolean(window.chatcoderAPI?.openBrowserDevTools || (window as any).process?.versions?.electron);

  const tabs = browserState.tabs || [];
  const activeTabId = browserState.activeTabId || (tabs[0]?.id ?? "");
  const activeTab = tabs.find((t) => t.id === activeTabId) || tabs[0];
  const tabView = browserState.tabView;
  const domSnapshot = browserState.domSnapshot;
  const selecting = browserState.selecting;
  const agent = browserState.agent;
  /** 面板此刻真的显示在屏幕上（RightPanel 可见标签 ∧ 右面板展开且不在页面态） */
  const shown = visible && panelOnScreen;

  const [inputUrl, setInputUrl] = useState(activeTab?.url || "");
  const [urlFocused, setUrlFocused] = useState(false);
  const urlInputRef = useRef<HTMLInputElement>(null);
  const activeIsBlank = !!activeTab && !activeTab.url && !activeTab.src;
  useEffect(() => {
    if (shown && activeIsBlank) urlInputRef.current?.focus({ preventScroll: true });
  }, [shown, activeIsBlank, activeTabId]);
  const [annotState, setAnnotState] = useState<ElementAnnotation | null>(null);
  const [elementBatch, setElementBatch] = useState<ElementAnnotation[]>([]);
  const [multiSelect, setMultiSelect] = useState(false);
  const [annotText, setAnnotText] = useState("");
  const [sentToast, setSentToast] = useState<string | null>(null);
  const [capturing, setCapturing] = useState(false);
  const busyCapture = useRef(false);
  const [moreOpen, setMoreOpen] = useState(false);
  const moreAnchorRef = useRef<HTMLDivElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const addressRef = useRef<HTMLDivElement>(null);
  const [suggesting, setSuggesting] = useState(false);
  const [suggestionIndex, setSuggestionIndex] = useState(-1);
  const [suggestedHistory, setSuggestedHistory] = useState<typeof history>([]);
  const [manager, setManager] = useState<BrowserManager | null>(null);
  const [bookmarkToEdit, setBookmarkToEdit] = useState<BrowserBookmarkOut | null>(null);
  const [bookmarkMenu, setBookmarkMenu] = useState<{ x: number; y: number; item: BrowserBookmarkOut } | null>(null);
  const [find, setFind] = useState<{ seed: string; tabId: string } | null>(null);
  const [shot, setShot] = useState<BrowserScreenshot | null>(null);
  const [shotOpen, setShotOpen] = useState(false);
  const [shotSequence, setShotSequence] = useState(0);
  const [tabMenu, setTabMenu] = useState<{ x: number; y: number; tabId: string } | null>(null);
  const [linkTarget, setLinkTarget] = useState("");
  const linkTimer = useRef(0);
  const toastTimer = useRef(0);
  const [zoom, setZoom] = useState(1);
  const [vaultMatches, setVaultMatches] = useState<VaultItem[]>([]);
  const [vaultRevision, setVaultRevision] = useState(0);
  const [loginOffer, setLoginOffer] = useState<LoginOffer | null>(null);
  const [downloads, setDownloads] = useState<BrowserDownload[]>([]);
  const [newDownload, setNewDownload] = useState(false);
  const activeBookmark = bookmarks.find((item) => item.url === activeTab?.url);
  const loginProtected = activeTab?.loginProtected === true;
  const isUserPage = activeTab?.owner !== "ai";
  const canAttach = Boolean(activeTab?.url) && !loginProtected;

  const [hoverInfo, setHoverInfo] = useState<{ rect: { x: number; y: number; width: number; height: number }; info: ElementInfo } | null>(null);
  const [cursorPos, setCursorPos] = useState<{ x: number; y: number } | null>(null);

  const viewportRef = useRef<HTMLDivElement>(null);
  // plan-20261018-operator-boost：停泊 / 取消停泊后检查停泊网页是否仍在视口原点（被祖先包含块带离视口会不出帧），
  // 取消停泊后还原守卫临时覆盖的祖先样式。放在下一帧做：等面板几何与类名都已提交。
  useEffect(() => {
    const raf = requestAnimationFrame(() => {
      const root = viewportRef.current;
      root?.querySelectorAll<HTMLElement>(".browser-tab-frame-wrapper.is-parked").forEach((w) => { healParkedWrapper(w); });
      releaseParkHeal();
    });
    return () => cancelAnimationFrame(raf);
  }, [shown, activeTabId, tabView, tabs.length]);
  const tabFrameRefs = useRef<Map<string, HTMLIFrameElement | any>>(new Map());
  const frameBridge = useFrameBridge();
  const visitSequence = useRef(new Map<string, string>());
  /** 每个标签挂载 webview 时使用的地址：之后的导航走命令式 API，src 不再随地址变化（否则会二次加载） */
  const mountSrcRef = useRef<Map<string, { src: string; seen: string }>>(new Map());
  const tabBindingsRef = useRef<Map<string, { ref: (el: FrameEl | null) => void; el?: FrameEl; off?: () => void }>>(new Map());

  // ── 视口尺寸 ───────────────────────────────────────────
  const viewportSizeRef = useRef<{ width: number; height: number } | null>(null);
  const [viewportSize, setViewportSize] = useState<{ width: number; height: number } | null>(null);
  const shownRef = useRef(shown);
  /** 给所有 webview / iframe 显式赋 px 尺寸。
   *  可见时按视口实测；不可见时不改（停泊期保持原逻辑尺寸，页面不重排），
   *  从未量到过尺寸的元素先给估算值——否则停泊容器只有 1px，页面会按 1px 排版。 */
  const syncFrameSize = useCallback(() => {
    const vp = viewportRef.current;
    // 视口容器尚未挂载（面板从未展开）时也要给停泊的 webview 估算尺寸，否则页面按 1px 排版、点击全部落空（plan-20261015-browser-perf R17）
    let w = 0;
    let h = 0;
    if (vp && shownRef.current) {
      w = vp.clientWidth;
      h = vp.clientHeight;
    }
    const measured = w > 0 && h > 0;
    if (measured) {
      const prev = viewportSizeRef.current;
      if (!prev || prev.width !== w || prev.height !== h) {
        viewportSizeRef.current = { width: w, height: h };
        setViewportSize({ width: w, height: h });
      }
    }
    recordDerivation("browserResize");
    const size = measured ? { width: w, height: h } : (viewportSizeRef.current ?? estimateViewport());
    for (const el of tabFrameRefs.current.values()) {
      if (!el || !el.style) continue;
      if (!measured && el.style.width) continue; // 停泊期：已有尺寸就保持
      el.style.width = `${size.width}px`;
      el.style.height = `${size.height}px`;
    }
  }, []);
  const syncFrameSizeRef = useRef(syncFrameSize);
  useEffect(() => { syncFrameSizeRef.current = syncFrameSize; }, [syncFrameSize]);

  useEffect(() => {
    shownRef.current = shown;
    if (!shown) return;
    const raf = requestAnimationFrame(() => { syncFrameSizeRef.current(); });
    return () => cancelAnimationFrame(raf);
  }, [shown]);

  useEffect(() => {
    if (shown) {
      void refreshBookmarks().catch(() => {});
      void refreshHistory().catch(() => {});
    }
  }, [shown, refreshBookmarks, refreshHistory]);

  const recordVisit = useCallback((tabId: string, url: string, title?: string) => {
    if (!isBrowserHttpUrl(url)) return;
    const tab = useBrowserStore.getState().getSessionState(activeSessionId).tabs.find((item) => item.id === tabId);
    if (!tab || tab.owner !== "user" || tab.loginProtected) return;
    const parsed = new URL(url);
    // OAuth 回调或一次性链接可能把密钥放在 URL，跳过此类访问记录。
    if ([...parsed.searchParams.keys()].some((key) => /password|token|secret|credential|authorization|^code$/i.test(key))) return;
    void api.recordBrowserHistory({ url: url.slice(0, 2000), title: (title || tab.title || "").slice(0, 300),
      favicon_url: tab.favicon?.slice(0, 2000) }).then(() => refreshHistory()).catch(() => {});
  }, [activeSessionId, refreshHistory]);

  // ── 标签宿主元素：注册表 + 导航事件同步 ────────────────
  const bindFrameEvents = useCallback((tabId: string, el: FrameEl): (() => void) => {
    const sid = sKey;
    const onReady = () => {
      frameBridge.markReady(tabId, el);
      markFrameReady(sid, tabId, el);
      syncFrameSizeRef.current();
    };
    if (el.tagName !== "WEBVIEW") {
      const onLoad = () => {
        onReady();
        updateTab(activeSessionId, tabId, { loading: false });
        recordVisit(tabId, String(el.src || ""));
      };
      el.addEventListener("load", onLoad);
      return () => el.removeEventListener("load", onLoad);
    }
    const nav = () => {
      try { return { canGoBack: el.canGoBack(), canGoForward: el.canGoForward() }; } catch { return {}; }
    };
    const handlers: Array<[string, (e: any) => void]> = [
      ["dom-ready", onReady],
      ["did-finish-load", onReady],
      ["did-start-loading", () => updateTab(activeSessionId, tabId, { loading: true })],
      ["did-stop-loading", () => {
        noteLoadingStop(sid, tabId);
        let title = "";
        try { title = el.getTitle(); } catch { /* ignore */ }
        updateTab(activeSessionId, tabId, { loading: false, ...nav(), ...(title ? { title } : {}) });
        const visited = visitSequence.current.get(tabId);
        if (visited) {
          visitSequence.current.delete(tabId);
          recordVisit(tabId, visited, title);
        }
      }],
      ["did-start-navigation", (e) => {
        if (e.isMainFrame && !e.isInPlace) noteNavigationStart(sid, tabId);
      }],
      ["did-navigate", (e) => {
        if (!(e.httpResponseCode >= 400)) visitSequence.current.set(tabId, String(e.url || ""));
        updateTab(activeSessionId, tabId, { url: e.url, error: undefined, ...nav() });
      }],
      ["did-navigate-in-page", (e) => {
        if (!e.isMainFrame) return;
        updateTab(activeSessionId, tabId, { url: e.url, ...nav() });
        recordVisit(tabId, e.url);
      }],
      ["update-target-url", (e) => {
        if (useBrowserStore.getState().getSessionState(activeSessionId).activeTabId !== tabId) return;
        window.clearTimeout(linkTimer.current);
        setLinkTarget(String(e.url || ""));
        if (e.url) linkTimer.current = window.setTimeout(() => setLinkTarget(""), 3600);
      }],
      ["page-title-updated", (e) => updateTab(activeSessionId, tabId, { title: String(e.title || "") })],
      ["page-favicon-updated", (e) => {
        const icon = Array.isArray(e.favicons) ? e.favicons[0] : "";
        if (icon) updateTab(activeSessionId, tabId, { favicon: String(icon) });
      }],
      ["did-fail-load", (e) => {
        if (!e.isMainFrame || e.errorCode === -3) return; // -3 = 被新导航替代，不算失败
        noteLoadingStop(sid, tabId);
        visitSequence.current.delete(tabId);
        updateTab(activeSessionId, tabId, { loading: false, error: `${e.errorDescription || "ERR"} (${e.errorCode})` });
      }],
      ["render-process-gone", () => updateTab(activeSessionId, tabId, { loading: false, error: "页面渲染进程已退出" })],
      ["console-message", (e) => {
        const lv = typeof e.level === "number" ? (["verbose", "info", "warning", "error"][e.level] ?? "info") : String(e.level || "info");
        pushConsole(sid, tabId, lv, String(e.message ?? ""), `${e.sourceId ?? ""}${e.line != null ? `:${e.line}` : ""}`);
      }],
    ];
    for (const [name, fn] of handlers) el.addEventListener(name, fn);
    return () => { for (const [name, fn] of handlers) el.removeEventListener(name, fn); };
  }, [sKey, activeSessionId, frameBridge, updateTab, recordVisit]);

  const getTabRefCallback = useCallback((tabId: string) => {
    const bindings = tabBindingsRef.current;
    let binding = bindings.get(tabId);
    if (!binding) {
      binding = {
        ref: (el: FrameEl | null) => {
          const b = bindings.get(tabId);
          if (!b) return;
          if (el) {
            if (b.el === el) return;
            b.off?.();
            b.el = el;
            tabFrameRefs.current.set(tabId, el);
            registerFrame(sKey, tabId, el);
            b.off = bindFrameEvents(tabId, el);
            frameBridge.attach(tabId, el);
            if (frameBridge.markReadyIfAttached(tabId, el)) markFrameReady(sKey, tabId, el);
            // 先给尺寸（停泊期用上次 / 估算值），避免页面以 1px 视口排版
            syncFrameSizeRef.current();
          } else {
            const prev = b.el;
            b.el = undefined;
            b.off?.();
            b.off = undefined;
            tabFrameRefs.current.delete(tabId);
            frameBridge.detach(tabId);
            if (prev) unregisterFrame(sKey, tabId, prev);
          }
        },
      };
      bindings.set(tabId, binding);
    }
    return binding.ref;
  }, [sKey, bindFrameEvents, frameBridge]);

  useEffect(() => {
    const bindings = tabBindingsRef.current;
    return () => {
      for (const [tabId, b] of Array.from(bindings.entries())) {
        b.off?.();
        frameBridge.detach(tabId);
        if (b.el) unregisterFrame(sKey, tabId, b.el);
      }
      bindings.clear();
      tabFrameRefs.current.clear();
    };
  }, [frameBridge, sKey]);

  // 视口尺寸变化（拖面板、窗口缩放、折叠恢复）时重算；运动期只记账，结束后补一次
  useEffect(() => {
    const vp = viewportRef.current;
    if (!vp) return;
    let raf = 0;
    let missed = false;
    const schedule = () => {
      if (isBusy()) { missed = true; return; }
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => { missed = false; syncFrameSize(); });
    };
    schedule();
    const offReconcile = registerReconcileTask("browser-frame-size", RECONCILE_ORDER.browserFrameSize, "浏览器框架尺寸", () => syncFrameSize());
    let ro: ResizeObserver | null = null;
    if (typeof ResizeObserver !== "undefined") {
      ro = new ResizeObserver(schedule);
      ro.observe(vp);
    }
    window.addEventListener("resize", schedule);
    const off = subscribe((busy) => { if (!busy && missed) schedule(); });
    return () => {
      offReconcile();
      cancelAnimationFrame(raf);
      ro?.disconnect();
      window.removeEventListener("resize", schedule);
      off();
    };
  }, [syncFrameSize]);

  useEffect(() => {
    const raf = requestAnimationFrame(syncFrameSize);
    return () => cancelAnimationFrame(raf);
  }, [syncFrameSize, activeTabId, tabView, tabs.length, selecting]);

  // 地址栏：切换标签或页面导航时同步（输入中不打断）
  const lastSyncedRef = useRef<{ tabId?: string; url?: string }>({});
  useEffect(() => {
    const curUrl = activeTab?.url || activeTab?.src || "";
    if (urlFocused && lastSyncedRef.current.tabId === activeTab?.id) return;
    if (lastSyncedRef.current.tabId !== activeTab?.id || lastSyncedRef.current.url !== curUrl) {
      lastSyncedRef.current = { tabId: activeTab?.id, url: curUrl };
      setInputUrl(curUrl);
    }
  }, [activeTab?.id, activeTab?.url, activeTab?.src, urlFocused]);

  const showToast = (msg: string) => {
    setSentToast(msg);
    window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setSentToast(null), 2600);
  };

  useEffect(() => () => {
    window.clearTimeout(toastTimer.current);
    window.clearTimeout(linkTimer.current);
  }, []);

  useEffect(() => {
    setLinkTarget(""); setZoom(1); setAnnotState(null); setElementBatch([]); setFind(null); setSuggesting(false);
    const frame = tabFrameRefs.current.get(activeTabId);
    try { setZoom(frame?.getZoomFactor?.() ?? 1); } catch { /* 网页未准备完成 */ }
  }, [activeTabId]);

  useEffect(() => {
    setVaultMatches([]);
    const url = activeTab?.url ?? "";
    if (!shown || !isUserPage || !isBrowserHttpUrl(url)) return;
    let current = true;
    void window.chatcoderAPI?.browserVaultList?.(new URL(url).origin).then((result) => {
      if (current && result.ok && result.encryptedAvailable) setVaultMatches(result.items ?? []);
    }).catch(() => {});
    return () => { current = false; };
  }, [activeTab?.url, shown, vaultRevision, isUserPage]);

  useEffect(() => {
    setSuggestedHistory([]);
    if (!suggesting || !inputUrl.trim() || inputUrl === activeTab?.url) return;
    let active = true;
    const timeout = window.setTimeout(() => {
      void api.listBrowserHistory(inputUrl.trim(), 7).then((result) => {
        if (active) setSuggestedHistory(result.items);
      }).catch(() => {});
    }, 220);
    return () => { active = false; window.clearTimeout(timeout); };
  }, [inputUrl, suggesting, activeTab?.url]);

  const markUser = () => noteUserActive(activeSessionId);

  const toggleSelect = () => {
    const next = !selecting;
    updateSessionState(activeSessionId, { selecting: next, ...(next ? { tabView: "preview" } : {}) });
    setAnnotText("");
    if (next) {
      setMultiSelect(false);
      setElementBatch([]);
    }
    setAnnotState(null);
    setHoverInfo(null);
    setCursorPos(null);
  };

  useEffect(() => {
    if (!selecting) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        updateSessionState(activeSessionId, { selecting: false });
        setHoverInfo(null);
        setCursorPos(null);
        setAnnotState(null);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selecting, activeSessionId, updateSessionState]);

  const extractElementInfo = (el: HTMLElement): ElementInfo => {
    const rect = el.getBoundingClientRect();
    const cs = window.getComputedStyle(el);
    return {
      tag: el.tagName.toLowerCase(), id: el.id || "", className: Array.from(el.classList || []).slice(0, 6).join(" "),
      width: Math.round(rect.width), height: Math.round(rect.height), x: Math.round(rect.x), y: Math.round(rect.y),
      display: cs.display, position: cs.position, color: cs.color, backgroundColor: cs.backgroundColor, fontSize: cs.fontSize,
      padding: `${cs.paddingTop} ${cs.paddingRight} ${cs.paddingBottom} ${cs.paddingLeft}`,
      margin: `${cs.marginTop} ${cs.marginRight} ${cs.marginBottom} ${cs.marginLeft}`,
      text: (el.textContent || "").trim().replace(/\s+/g, " ").slice(0, 100),
      role: el.getAttribute("role") || undefined, ariaLabel: el.getAttribute("aria-label") || undefined,
      placeholder: (el as HTMLInputElement).placeholder || undefined,
    };
  };

  // 标注探针：注入 guest 页面，元素命中在页面内判定（坐标天然一致）
  const INSPECT_PROBE_SRC = `(() => {
    if (window.__ccInspect) { window.__ccInspect.enabled = true; return; }
    function buildInfo(el) {
      const r = el.getBoundingClientRect();
      const s = window.getComputedStyle(el);
      return {
        tag: el.tagName.toLowerCase(), id: el.id || '',
        className: el.className ? (typeof el.className === 'string' ? el.className : (el.className.baseVal || '')) : '',
        width: Math.round(r.width), height: Math.round(r.height), x: Math.round(r.left), y: Math.round(r.top),
        display: s.display, position: s.position, color: s.color, backgroundColor: s.backgroundColor, fontSize: s.fontSize,
        padding: s.padding, margin: s.margin, text: (el.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 100),
        role: el.getAttribute('role') || undefined, ariaLabel: el.getAttribute('aria-label') || undefined, placeholder: el.placeholder || undefined
      };
    }
    window.__ccInspect = {
      enabled: true,
      infoAt(x, y) {
        if (!window.__ccInspect.enabled) return null;
        const el = document.elementFromPoint(x, y);
        if (!el || el === document.documentElement || el === document.body) return null;
        const r = el.getBoundingClientRect();
        return { rect: { x: Math.round(r.left), y: Math.round(r.top), width: Math.round(r.width), height: Math.round(r.height) }, info: buildInfo(el) };
      },
      pickAt(x, y) {
        if (!window.__ccInspect.enabled) return null;
        const el = document.elementFromPoint(x, y);
        if (!el) return null;
        const hit = window.__ccInspect.infoAt(x, y);
        return { source: (el.outerHTML || '').slice(0, 800), info: hit ? hit.info : buildInfo(el) };
      }
    };
  })()`;

  const selectingRef = useRef(selecting);
  useEffect(() => { selectingRef.current = selecting; }, [selecting]);
  const hoverQueryRef = useRef({ pending: false });

  useEffect(() => {
    if (!selecting || !canAttach) return;
    void frameBridge.invoke(activeTabId, INSPECT_PROBE_SRC, { waitReady: true });
    return () => {
      void frameBridge.invoke(activeTabId, "window.__ccInspect && (window.__ccInspect.enabled = false)", { waitReady: false });
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selecting, canAttach, activeTabId, activeTab?.url, frameBridge]);

  const handleOverlayMouseMove = (e: React.MouseEvent) => {
    if (!selectingRef.current || !viewportRef.current) return;
    const vpRect = viewportRef.current.getBoundingClientRect();
    const x = e.clientX - vpRect.left;
    const y = e.clientY - vpRect.top;
    setCursorPos({ x, y });
    const frame = tabFrameRefs.current.get(activeTabId);
    if (!frame) { setHoverInfo(null); return; }
    if (isGuestFrame(frame)) {
      const q = hoverQueryRef.current;
      if (q.pending) return;
      q.pending = true;
      frameBridge
        .call(activeTabId, `window.__ccInspect ? __ccInspect.infoAt(${Math.round(x / zoom)}, ${Math.round(y / zoom)}) : null`, { waitReady: false })
        .then((res: any) => {
          q.pending = false;
          if (!selectingRef.current) return;
          if (res?.info && res.rect) setHoverInfo({
            rect: { x: res.rect.x * zoom, y: res.rect.y * zoom, width: res.rect.width * zoom, height: res.rect.height * zoom },
            info: res.info,
          });
          else setHoverInfo(null);
        })
        .catch(() => { q.pending = false; setHoverInfo(null); });
    } else {
      try {
        const doc = frame.contentDocument;
        if (!doc) { setHoverInfo(null); return; }
        const fr = frame.getBoundingClientRect();
        const el = doc.elementFromPoint(x - (fr.left - vpRect.left), y - (fr.top - vpRect.top)) as HTMLElement | null;
        if (el && el !== doc.body && el !== doc.documentElement) {
          const elRect = el.getBoundingClientRect();
          setHoverInfo({
            rect: { x: elRect.left + (fr.left - vpRect.left), y: elRect.top + (fr.top - vpRect.top), width: elRect.width, height: elRect.height },
            info: extractElementInfo(el),
          });
        } else {
          setHoverInfo(null);
        }
      } catch {
        setHoverInfo(null);
      }
    }
  };

  const handleOverlayClick = async (e: React.MouseEvent) => {
    const vp = viewportRef.current;
    if (!vp) return;
    const vpRect = vp.getBoundingClientRect();
    const localX = e.clientX - vpRect.left;
    const localY = e.clientY - vpRect.top;
    let source = "";
    let info: ElementInfo | null = null;
    const currentFrame = tabFrameRefs.current.get(activeTabId);
    if (isGuestFrame(currentFrame)) {
      try {
        const res = await frameBridge.call<any>(activeTabId, `window.__ccInspect ? __ccInspect.pickAt(${Math.round(localX / zoom)}, ${Math.round(localY / zoom)}) : null`, { waitReady: true });
        if (res) { source = res.source || ""; info = res.info || null; }
      } catch (err) {
        console.warn("inspect error:", err);
      }
    }
    if (!source && currentFrame) {
      try {
        const doc = currentFrame.contentDocument;
        if (doc) {
          const fr = currentFrame.getBoundingClientRect();
          const el = doc.elementFromPoint(localX - (fr.left - vpRect.left), localY - (fr.top - vpRect.top)) as HTMLElement | null;
          if (el) { source = el.outerHTML.slice(0, 800); info = extractElementInfo(el); }
        }
      } catch { /* 跨域 iframe */ }
    }
    if (!source) source = `页面坐标 (${Math.round(localX)}, ${Math.round(localY)})`;
    const current = useBrowserStore.getState().getSessionState(activeSessionId);
    if (current.activeTabId !== activeTabId || current.tabs.find((tab) => tab.id === activeTabId)?.url !== activeTab?.url || !current.selecting) return;
    const CARD_W = Math.min(320, vpRect.width - 16), CARD_H = 340;
    let cardX = localX + 12;
    if (cardX + CARD_W > vpRect.width - 8) cardX = Math.max(8, localX - CARD_W - 12);
    let cardY = localY + 12;
    if (cardY + CARD_H > vpRect.height - 8) cardY = Math.max(8, vpRect.height - CARD_H - 8);
    const annotation = { x: cardX, y: cardY, source, info, clickX: localX, clickY: localY };
    if (multiSelect) setElementBatch((old) => [...old.slice(0, 11), annotation]);
    else { setAnnotState(annotation); setAnnotText(""); }
    setHoverInfo(null);
  };

  /** 截图：优先主进程隐藏态截图（不受面板显隐影响），回退 webview.capturePage */
  const captureDataUrl = async (tabId = activeTabId, fullPage = false, clip?: CaptureRegion): Promise<string> => {
    const currentFrame = tabFrameRefs.current.get(tabId);
    let wcId = 0;
    try { wcId = currentFrame?.getWebContentsId?.() ?? 0; } catch { wcId = 0; }
    if (wcId && window.chatcoderAPI?.browserAgentExec) {
      const res = await window.chatcoderAPI.browserAgentExec(wcId, "capture", {
        max_side: fullPage ? 4096 : 2560, quality: 90, full_page: fullPage, user_interface: true, clip,
      });
      if (fullPage && res.ok && res.result?.mode !== "full") showToast("整页截图不可用，已打开视口截图");
      if (res?.ok && res.result?.base64) return `data:${res.result.mime || "image/jpeg"};base64,${res.result.base64}`;
    }
    if (isGuestFrame(currentFrame) && !clip) {
      const d = await frameBridge.capture(tabId);
      if (d) return d;
    }
    return "";
  };

  const ensureShareable = async (tabId: string) => {
    const state = useBrowserStore.getState().getSessionState(activeSessionId);
    if (!state.tabs.some((tab) => tab.id === tabId && !tab.loginProtected)) throw new Error("此标签页包含受保护的登录内容");
    const id = getFrame(sKey, tabId)?.wcId;
    if (!id || !window.chatcoderAPI?.browserAgentExec) return;
    const response = await window.chatcoderAPI.browserAgentExec(id, "loginProtected");
    if (!response.ok || response.result?.protected) throw new Error("此标签页包含受保护的登录内容");
  };
  const sourceOf = (tabId = activeTabId) => {
    const tab = useBrowserStore.getState().getSessionState(activeSessionId).tabs.find((item) => item.id === tabId);
    return { tabId, title: tab?.title || "网页", url: tab?.url || "about:blank" };
  };
  const attach = async (source: ReturnType<typeof sourceOf>, payload: Pick<BrowserRef, "kind" | "text" | "note" | "selector" | "bbox" | "styleDigest">, blob?: Blob) => {
    await ensureShareable(source.tabId);
    let uploaded: BrowserRef["attachment"];
    if (blob) {
      const extension = blob.type === "image/png" ? "png" : "jpg";
      uploaded = await api.uploadFile(new File([blob], `网页标注-${Date.now()}.${extension}`, { type: blob.type }));
    }
    await ensureShareable(source.tabId);
    addComposerBrowserRef({
      ...payload, id: `ref-${crypto.randomUUID()}`, pageTitle: source.title, url: safeContextUrl(source.url),
      thumbUrl: uploaded?.url, attachment: uploaded, createdAt: Date.now(),
    });
    showToast("已添加到对话输入框");
  };
  const captureAction = async (action: () => Promise<void>) => {
    if (busyCapture.current) return;
    busyCapture.current = true; setCapturing(true);
    try { await action(); }
    catch (error) { showToast(error instanceof Error ? error.message : String(error)); }
    finally { busyCapture.current = false; setCapturing(false); }
  };
  const openScreenshot = (source: ReturnType<typeof sourceOf>, image: string, crop = false) => {
    setShot({ ...source, image, initialTool: crop ? "crop" : undefined });
    setShotSequence((value) => value + 1);
    setShotOpen(true);
  };
  const startCapture = (fullPage = false, crop = false) => void captureAction(async () => {
    const source = sourceOf();
    const image = await captureDataUrl(source.tabId, fullPage);
    if (!image) throw new Error("截图失败，请等待网页加载后重试");
    openScreenshot(source, image, crop);
  });
  const capturePageImage = (x: number, y: number, url: string) => void captureAction(async () => {
    const source = sourceOf();
    await ensureShareable(source.tabId);
    const coordinates = [x / zoom, y / zoom].map((value) => Number.isFinite(value) ? Math.round(value) : 0);
    const clip = await frameBridge.call<CaptureRegion>(source.tabId, `(() => {
      const hit = document.elementFromPoint(${coordinates[0]}, ${coordinates[1]});
      const image = hit?.closest("img") || Array.from(document.images).find((item) => item.currentSrc === ${JSON.stringify(url)});
      if (!image) return null;
      const r = image.getBoundingClientRect();
      const x = Math.max(0, r.left), y = Math.max(0, r.top);
      const width = Math.min(innerWidth, r.right) - x, height = Math.min(innerHeight, r.bottom) - y;
      return width > 4 && height > 4 ? { x, y, width, height } : null;
    })()`, { waitReady: true });
    const cropped = clip ? await captureDataUrl(source.tabId, false, clip) : "";
    const image = cropped || await captureDataUrl(source.tabId);
    if (!image) throw new Error("无法截取这张网页图片");
    openScreenshot(source, image, !cropped);
  });
  const handleCaptureToComposer = () => void captureAction(async () => {
    const source = sourceOf();
    await ensureShareable(source.tabId);
    const image = await captureDataUrl(source.tabId);
    if (!image) throw new Error("截图失败，请等待网页加载后重试");
    await attach(source, { kind: "screenshot" }, await (await fetch(image)).blob());
  });
  const imageFrom = async (url: string) => {
    const image = new Image();
    image.src = url;
    await image.decode();
    return image;
  };
  const describe = (item: ElementAnnotation, i: number) => {
    const info = item.info;
    const detail = info ? `${info.selector || info.tag} | (${info.x}, ${info.y}) ${info.width}×${info.height} | ${info.display}, ${info.fontSize}, ${info.color}` : "";
    return `${i + 1}. ${detail}\n${redactDiagnostic(item.source).slice(0, 550)}`;
  };
  const handleSendAnnotToChat = () => void captureAction(async () => {
    const items = multiSelect ? elementBatch : annotState ? [annotState] : [];
    if (!items.length) return;
    const source = sourceOf();
    await ensureShareable(source.tabId);
    const viewport = viewportRef.current?.getBoundingClientRect();
    const data = await captureDataUrl(source.tabId);
    let blob: Blob | undefined;
    if (data && viewport) {
      const image = await imageFrom(data);
      const canvas = document.createElement("canvas");
      canvas.width = image.width; canvas.height = image.height;
      const ctx = canvas.getContext("2d");
      if (ctx) {
        ctx.drawImage(image, 0, 0);
        const scaleX = canvas.width / viewport.width, scaleY = canvas.height / viewport.height;
        items.forEach((item, index) => {
          const x = Math.max(13, Math.min(canvas.width - 13, item.clickX * scaleX));
          const y = Math.max(13, Math.min(canvas.height - 13, item.clickY * scaleY));
          ctx.fillStyle = "#3478f6"; ctx.strokeStyle = "#ffffff"; ctx.lineWidth = 2;
          ctx.beginPath(); ctx.arc(x, y, 12, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
          ctx.fillStyle = "#ffffff"; ctx.font = "bold 13px sans-serif"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
          ctx.fillText(String(index + 1), x, y);
        });
        blob = await new Promise<Blob | undefined>((resolve) => canvas.toBlob((png) => resolve(png ?? undefined), "image/png"));
      }
    }
    await attach(source, { kind: "element", text: items.map(describe).join("\n"), note: annotText.trim() || undefined }, blob);
    updateSessionState(activeSessionId, { selecting: false });
    setAnnotState(null); setElementBatch([]); setAnnotText("");
  });
  const addPageContext = (diagnostic: boolean) => void captureAction(async () => {
    const source = sourceOf();
    await ensureShareable(source.tabId);
    const rec = getFrame(sKey, source.tabId);
    let text = "";
    if (diagnostic) {
      const errors = rec?.console.filter((entry) => entry.navSeq === rec.navSeq && /error/i.test(entry.level)).slice(-10) ?? [];
      const network = rec?.wcId ? await window.chatcoderAPI?.browserAgentExec?.(rec.wcId, "diagnosticNetwork") : null;
      const failures: Array<{ method: string; url: string; status: string | number }> = network?.result?.entries ?? [];
      text = [
        "请诊断当前页面的报错。",
        errors.length ? `控制台错误\n${errors.map((entry) => redactDiagnostic(entry.text).slice(0, 420)).join("\n")}` : "当前页面没有记录到控制台错误。",
        failures.length ? `失败的网络请求\n${failures.map((entry) => `${entry.method} ${entry.url} (${entry.status})`).join("\n")}`
          : network?.result?.available ? "没有记录到失败的网络请求。" : "网络诊断通道不可用。",
      ].join("\n\n");
    } else {
      const summary = await frameBridge.call<string>(source.tabId,
        "(() => (document.querySelector('main, article, [role=main]') || document.body)?.innerText.slice(0, 5500) || '')()", { waitReady: true });
      text = `请分析当前页面。\n\n页面正文\n${redactDiagnostic(summary || "")}`;
    }
    const image = await captureDataUrl(source.tabId);
    await attach(source, { kind: diagnostic ? "diagnostic" : "page", text }, image ? await (await fetch(image)).blob() : undefined);
  });

  const refreshDownloads = async () => {
    const response = await window.chatcoderAPI?.browserAgentExec?.(0, "downloads", { limit: 50, user_interface: true });
    if (response?.ok && Array.isArray(response.result?.items)) setDownloads(response.result.items);
  };
  const openManager = (mode: BrowserManager, editing: BrowserBookmarkOut | null = null) => {
    setMoreOpen(false);
    setBookmarkToEdit(editing);
    setManager(mode);
    if (mode === "downloads") {
      setNewDownload(false);
      void refreshDownloads().catch(() => {});
    }
  };
  const beginFind = (seed = "") => {
    if (!activeTab?.url || !isElectron) return;
    updateSessionState(activeSessionId, { tabView: "preview" });
    setFind({ seed, tabId: activeTabId });
  };
  const changeZoom = (next: number) => {
    const frame = tabFrameRefs.current.get(activeTabId);
    const factor = Math.min(3, Math.max(0.25, Math.round(next * 100) / 100));
    try { if (frame?.setZoomFactor) { frame.setZoomFactor(factor); setZoom(factor); } } catch { /* 网页仍在加载 */ }
  };
  const saveCurrentBookmark = async (url = activeTab?.url ?? "", title = activeTab?.title ?? "", edit = true) => {
    if (!isBrowserHttpUrl(url) || loginProtected) return;
    try {
      const saved = useBrowserLibrary.getState().bookmarks.find((item) => item.url === url);
      if (saved && edit) { openManager("bookmarks", saved); return; }
      if (saved) { showToast("当前网页已收藏"); return; }
      await api.saveBrowserBookmark({ url: url.slice(0, 2000), title: title.slice(0, 300), favicon_url: activeTab?.favicon?.slice(0, 2000) });
      await refreshBookmarks();
      showToast("已添加书签");
    } catch (error) { showToast(error instanceof Error ? error.message : String(error)); }
  };

  const openDevTools = async () => {
    setMoreOpen(false);
    const frame = tabFrameRefs.current.get(activeTabId);
    try {
      if (!isGuestFrame(frame)) return;
      if (frame.isDevToolsOpened?.()) frame.closeDevTools();
      else frameBridge.openDevTools(activeTabId);
    } catch (e) {
      console.warn("openDevTools error:", e);
    }
  };

  const openExternal = () => {
    setMoreOpen(false);
    const u = activeTab?.url;
    if (!u) return;
    if (window.chatcoderAPI?.openExternal) void window.chatcoderAPI.openExternal(u);
    else window.open(u, "_blank", "noopener");
  };

  const submitUrl = (value = browserSearchTarget(inputUrl)) => {
    const url = normalizeUrl(value);
    if (!url || !activeTab) return;
    if (!/^(https?:\/\/|file:\/\/)/i.test(url)) { showToast("请输入 HTTP、HTTPS 或本地文件网址"); return; }
    markUser();
    setSuggesting(false);
    updateSessionState(activeSessionId, { tabView: "preview", selecting: false });
    navigate(activeSessionId, url, activeTabId);
    urlInputRef.current?.blur();
  };

  const suggestions = useMemo(() => {
    const query = inputUrl.trim().toLowerCase();
    if (!query || query === activeTab?.url.toLowerCase()) return [];
    const matches = bookmarks.filter((item) => [item.title, item.url, item.folder].some((value) => value.toLowerCase().includes(query)));
    const combined = [...matches.map((item) => ({ url: item.url, title: item.title, type: "书签" })),
      ...suggestedHistory.map((item) => ({ url: item.url, title: item.title, type: "历史" }))];
    const seen = new Set<string>();
    const result = combined.filter((item) => {
      if (seen.has(item.url)) return false;
      seen.add(item.url); return true;
    }).slice(0, 6);
    const target = browserSearchTarget(inputUrl);
    if (target !== inputUrl.trim()) result.push({ url: target, title: `搜索「${inputUrl.trim()}」`, type: "搜索" });
    return result;
  }, [bookmarks, suggestedHistory, inputUrl, activeTab?.url]);
  const suggestionsOpen = shown && suggesting && suggestions.length > 0;
  const selectedTab = tabMenu ? tabs.find((tab) => tab.id === tabMenu.tabId) : undefined;
  const displayTabs = [...tabs.filter((tab) => tab.pinned), ...tabs.filter((tab) => !tab.pinned)];
  const tabItems = selectedTab ? [
    { label: selectedTab.pinned ? "取消固定" : "固定标签", onClick: () => updateTab(activeSessionId, selectedTab.id, { pinned: !selectedTab.pinned }) },
    { label: "刷新标签", onClick: () => reload(activeSessionId, selectedTab.id) },
    { label: "复制标签", onClick: () => newTab(activeSessionId, selectedTab.loginProtected ? "" : selectedTab.url) },
    { label: "关闭其他标签", onClick: () => tabs.filter((tab) => tab.id !== selectedTab.id && !tab.pinned).forEach((tab) => closeTab(activeSessionId, tab.id)) },
    { label: "关闭右侧标签", onClick: () => displayTabs.slice(displayTabs.indexOf(selectedTab) + 1)
      .filter((tab) => !tab.pinned).forEach((tab) => closeTab(activeSessionId, tab.id)) },
    { label: "关闭标签", onClick: () => closeTab(activeSessionId, selectedTab.id) },
  ] : [];
  const bookmarkFolders = new Map<string, BrowserBookmarkOut[]>();
  for (const item of bookmarks) {
    if (item.folder) bookmarkFolders.set(item.folder, [...(bookmarkFolders.get(item.folder) ?? []), item]);
  }
  const directBookmarks = bookmarks.filter((item) => !item.folder || item.sort_order < 0);
  const visibleLoginOffer = loginOffer?.tabId === activeTabId ? loginOffer : null;

  const resolveLogin = async (action: "save" | "never" | "skip") => {
    if (!visibleLoginOffer) return;
    const result = await window.chatcoderAPI?.browserVaultResolveLogin?.(visibleLoginOffer.id, action).catch(() => null);
    if (!result?.ok && action !== "skip") showToast(result?.error || "无法保存登录凭据");
    else if (action === "save") { setVaultRevision((value) => value + 1); showToast("登录凭据已加密保存"); }
    setLoginOffer(null);
  };

  const handleShortcut = (action: string) => {
    if (!shown || manager || shotOpen) return false;
    switch (action) {
      case "address": urlInputRef.current?.focus(); urlInputRef.current?.select(); break;
      case "new-tab": newTab(activeSessionId); updateSessionState(activeSessionId, { tabView: "preview" }); break;
      case "close-tab": closeTab(activeSessionId, activeTabId); break;
      case "restore-tab": restoreClosedTab(activeSessionId); break;
      case "reload": reload(activeSessionId, activeTabId); break;
      case "find": beginFind(); break;
      case "zoom-in": changeZoom(zoom + 0.1); break;
      case "zoom-out": changeZoom(zoom - 0.1); break;
      case "zoom-reset": changeZoom(1); break;
      case "bookmark": void saveCurrentBookmark(); break;
      case "devtools": void openDevTools(); break;
      default: return false;
    }
    markUser();
    return true;
  };
  const handleGuestEvent = (event: { type: string; wcId: number; [key: string]: unknown }) => {
    if (event.type === "download") {
      if (!shown) return;
      if (manager !== "downloads") setNewDownload(true);
      const download = event as unknown as BrowserDownload;
      if (typeof download.id === "number") setDownloads((old) => [...old.filter((item) => item.id !== download.id), download]
        .sort((a, b) => a.id - b.id).slice(-50));
      return;
    }
    const record = findFrameByWcId(Number(event.wcId));
    if (!record || record.sid !== sKey) return;
    if (event.type === "login-offer") {
      const tab = tabs.find((item) => item.id === record.tabId);
      if (tab?.owner === "user" && typeof event.id === "string") setLoginOffer({ tabId: record.tabId, id: event.id,
        origin: String(event.origin), username: String(event.username), update: event.update === true });
      return;
    }
    if (!shown || record.tabId !== activeTabId) return;
    if (event.type === "browser-shortcut") { handleShortcut(String(event.action)); return; }
    if (event.type !== "context-action") return;
    const action = String(event.action);
    const value = String(event.text ?? "");
    if (action === "open-new-tab") {
      const url = String(event.url ?? "");
      if (/^(https?:\/\/|file:\/\/)/i.test(url)) newTab(activeSessionId, url);
    } else if (action === "find-in-page") beginFind(value);
    else if (action === "bookmark-page") void saveCurrentBookmark(String(event.pageUrl ?? ""), String(event.title ?? ""), false);
    else if (action === "screenshot-annotate") startCapture();
    else if (action === "send-image-ai") capturePageImage(Number(event.x), Number(event.y), String(event.srcUrl ?? ""));
    else if (action === "autofill-failed") showToast(String(event.error || "自动填充失败"));
    else if (action === "open-vault") openManager("vault");
    else if (action === "ask-ai-selection" && value) {
      void captureAction(() => attach(sourceOf(), { kind: "selection", text: `请解释页面选区。\n\n${redactDiagnostic(value)}` }));
    }
  };
  const eventHandler = useRef(handleGuestEvent);
  eventHandler.current = handleGuestEvent;
  useEffect(() => window.chatcoderAPI?.onBrowserAgentEvent?.((event) => eventHandler.current(event)), []);
  useEffect(() => {
    if (!loginOffer) return;
    const timer = window.setTimeout(() => setLoginOffer(null), 118_000);
    return () => window.clearTimeout(timer);
  }, [loginOffer]);
  const handleBrowserKeys = (event: React.KeyboardEvent) => {
    markUser();
    if (event.altKey || event.defaultPrevented || event.nativeEvent.isComposing) return;
    const key = event.key.toLowerCase();
    const chord = event.ctrlKey || event.metaKey;
    const action = key === "f12" ? "devtools" : key === "f5" ? "reload"
      : chord && key === "t" ? (event.shiftKey ? "restore-tab" : "new-tab")
      : chord && !event.shiftKey ? ({
        l: "address", w: "close-tab", r: "reload", f: "find", "=": "zoom-in", "+": "zoom-in",
        "-": "zoom-out", "0": "zoom-reset", d: "bookmark",
      } as Record<string, string>)[key] : null;
    if (action && handleShortcut(action)) { event.preventDefault(); event.stopPropagation(); }
  };

  const agentTab = agent.tabId ? tabs.find((t) => t.id === agent.tabId) : undefined;
  const elsewhereTab = browserState.agentElsewhere ? tabs.find((t) => t.id === browserState.agentElsewhere) : undefined;
  const dialog = browserState.dialog;
  const secure = (activeTab?.url || "").startsWith("https://");
  const loading = !!activeTab?.loading;

  /** 某个标签的 webview 当前该用的挂载地址（见 mountSrcRef 说明） */
  const mountSrcOf = (tab: BrowserTabState): string => {
    const m = mountSrcRef.current;
    const cur = m.get(tab.id);
    if (!cur) {
      const src = tab.url || tab.src;
      m.set(tab.id, { src, seen: tab.src });
      return src;
    }
    if (cur.seen !== tab.src) {
      // 显式导航在 webview 未就绪时写入了新 src：此时才需要改属性触发加载
      cur.seen = tab.src;
      cur.src = tab.src;
    }
    return cur.src;
  };

  return (
    <div className="browser-panel bp" ref={panelRef} onPointerDownCapture={markUser} onKeyDownCapture={handleBrowserKeys} onWheelCapture={markUser}>
      {/* 标签条 */}
      <div className="bp-tabs" role="tablist">
        <div className="bp-tabs-list">
          {displayTabs.map((tab) => {
            const isActive = tab.id === activeTabId;
            const aiWorking = agent.phase === "working" && agent.tabId === tab.id;
            const label = tab.title || (tab.url ? hostOf(tab.url) || tab.url : "新标签页");
            return (
              <div
                key={tab.id}
                className={`bp-tab${isActive ? " is-active" : ""}${tab.owner === "ai" ? " is-ai" : ""}`}
                role="tab"
                aria-selected={isActive}
                tabIndex={0}
                onClick={() => setActiveTab(activeSessionId, tab.id)}
                onKeyDown={(e) => {
                  if (e.target !== e.currentTarget) return;
                  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setActiveTab(activeSessionId, tab.id); }
                  if (e.key === "ContextMenu" || (e.key === "F10" && e.shiftKey)) {
                    e.preventDefault(); const rect = e.currentTarget.getBoundingClientRect();
                    setTabMenu({ x: rect.left, y: rect.bottom, tabId: tab.id });
                  }
                }}
                onAuxClick={(e) => { if (e.button === 1 && !tab.pinned) { e.preventDefault(); closeTab(activeSessionId, tab.id); } }}
                onDoubleClick={() => updateTab(activeSessionId, tab.id, { pinned: !tab.pinned })}
                onContextMenu={(e) => { e.preventDefault(); setTabMenu({ x: e.clientX, y: e.clientY, tabId: tab.id }); }}
              >
                <span className="bp-tab-ic">
                  {tab.loading ? <IconSpinner size={12} />
                    : tab.owner === "ai" ? <IconSparkles size={12} />
                      : tab.favicon ? <img src={tab.favicon} alt="" width={14} height={14} onError={(e) => { (e.currentTarget as HTMLImageElement).style.visibility = "hidden"; }} />
                        : <IconGlobe size={12} />}
                </span>
                <Tooltip content={label}><span className="bp-tab-title">{label}</span></Tooltip>
                {aiWorking && <span className="bp-tab-dot" aria-label="AI 正在操作" />}
                {tab.pinned ? <span className="bp-tab-pin"><IconPin size={12} /></span>
                  : <IconButton size="xs" icon={<IconX size={10} />} aria-label={`关闭标签：${label}`}
                    onDoubleClick={(e) => e.stopPropagation()} onClick={(e) => { e.stopPropagation(); closeTab(activeSessionId, tab.id); }} />}
              </div>
            );
          })}
        </div>
        <IconButton size="xs" icon={<IconHistory size={14} />} title="恢复最近关闭的标签页" disabled={!browserState.closedTabs.length}
          onClick={() => restoreClosedTab(activeSessionId)} />
        <IconButton size="xs" icon={<IconPlus size={14} />} title="新建标签页"
          onClick={() => { newTab(activeSessionId); updateSessionState(activeSessionId, { tabView: "preview" }); }} />
      </div>

      {/* 工具栏 */}
      <div className="bp-toolbar">
        <div className="bp-tool-group bp-nav">
        <IconButton size="xs" icon={<IconArrowLeft size={14} />} title="后退" disabled={!activeTab?.canGoBack} onClick={() => { markUser(); goBack(activeSessionId, activeTabId); }} />
        <IconButton size="xs" icon={<IconArrowRight size={14} />} title="前进" disabled={!activeTab?.canGoForward} onClick={() => { markUser(); goForward(activeSessionId, activeTabId); }} />
        {loading
          ? <IconButton size="xs" icon={<IconX size={14} />} title="停止加载" onClick={() => stop(activeSessionId, activeTabId)} />
          : <IconButton size="xs" icon={<IconRefresh size={14} />} title="刷新" disabled={!activeTab?.url} onClick={() => { markUser(); reload(activeSessionId, activeTabId); }} />}
        <IconButton size="xs" icon={<IconLayoutGrid size={14} />} title="回到起始页" disabled={activeIsBlank} onClick={() => goHome(activeSessionId, activeTabId)} />
        </div>
        <div className="bp-address" ref={addressRef} onBlur={(event) => {
          if (!event.currentTarget.contains(event.relatedTarget)) { setSuggesting(false); setUrlFocused(false); }
        }}>
        <Input
          ref={urlInputRef}
          size="sm"
          wrapClassName="bp-url"
          prefix={vaultMatches.length > 0 ? <Menu align="start" trigger={<IconButton size="xs" icon={<IconLock size={13} />}
            aria-label="自动填充已保存的账号" />}
            entries={[
              { label: hostOf(activeTab?.url || ""), groupLabel: true },
              ...vaultMatches.map((item) => ({ label: item.label ? `${item.username} · ${item.label}` : item.username, onSelect: () => {
                const wcId = getFrame(sKey, activeTabId)?.wcId;
                if (!wcId) return;
                void window.chatcoderAPI?.browserVaultAutofill?.(wcId, item.id).then((result) => {
                  showToast(result.ok ? "已填充登录表单" : result.error || "自动填充失败");
                }).catch(() => showToast("自动填充失败"));
              } })),
              { label: "管理密码库", onSelect: () => openManager("vault") },
            ]} /> : <Tooltip content={secure ? "HTTPS 连接" : hostOf(activeTab?.url || "") || "起始页"}>
              <span className="bp-url-ic">{activeTab?.owner === "ai" ? <IconSparkles size={13} /> : secure ? <IconLock size={13} /> : <IconGlobe size={13} />}</span>
            </Tooltip>}
          suffix={<>
            {zoom !== 1 && <Button size="xs" variant="ghost" aria-label="重置缩放" onClick={() => changeZoom(1)}>{Math.round(zoom * 100)}%</Button>}
            <IconButton size="xs" icon={<IconStar size={13} />} active={!!activeBookmark}
              title={activeBookmark ? "编辑书签" : "收藏当前页面"}
              disabled={loginProtected || !isBrowserHttpUrl(activeTab?.url || "")} onClick={() => void saveCurrentBookmark()} />
          </>}
          value={inputUrl}
          onChange={(e) => { setInputUrl(e.target.value); setSuggestionIndex(-1); setSuggesting(true); }}
          onFocus={(e) => { setUrlFocused(true); setSuggesting(true); e.currentTarget.select(); }}
          onKeyDown={(e) => {
            if (e.nativeEvent.isComposing) return;
            if (e.key === "Enter") { e.preventDefault(); submitUrl(suggestionsOpen ? suggestions[suggestionIndex]?.url : undefined); }
            if (e.key === "Escape") { e.stopPropagation(); setSuggesting(false); setInputUrl(activeTab?.url || ""); e.currentTarget.blur(); }
            if (suggestionsOpen && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
              e.preventDefault();
              setSuggestionIndex((index) => (index + (e.key === "ArrowDown" ? 1 : index < 0 ? 0 : -1) + suggestions.length) % suggestions.length);
            }
          }}
          placeholder="输入网址，如 localhost:5173"
          spellCheck={false}
          autoCapitalize="none"
          autoCorrect="off"
          aria-label="网址"
          aria-expanded={suggestionsOpen}
          aria-controls={suggestionsOpen ? `bp-suggestions-${sKey}` : undefined}
          aria-activedescendant={suggestionsOpen && suggestionIndex >= 0 ? `bp-suggestion-${sKey}-${suggestionIndex}` : undefined}
          aria-autocomplete="list"
          role="combobox"
        />
        <Popover open={suggestionsOpen} onClose={() => setSuggesting(false)} ignoreRefs={[addressRef]}
          placement="bottom-start" className="bp-suggestions" role="listbox" aria-label="网址建议">
          <div id={`bp-suggestions-${sKey}`} className="bp-suggestions-list">
            {suggestions.map((item, index) => (
              <button
                key={item.url}
                type="button"
                id={`bp-suggestion-${sKey}-${index}`}
                className={`pop-item bp-suggestion${suggestionIndex === index ? " is-hl" : ""}`}
                role="option"
                aria-selected={suggestionIndex === index}
                onMouseDown={(event) => event.preventDefault()}
                onMouseEnter={() => setSuggestionIndex(index)}
                onClick={() => submitUrl(item.url)}
              >
                <span className="pop-item-icon">
                  {item.type === "书签" ? <IconStar size={14} /> : <IconHistory size={14} />}
                </span>
                <span className="pop-item-main">
                  <span className="pop-item-title">{item.title || item.url}</span>
                  <span className="pop-item-desc">{item.url}</span>
                </span>
                <span className="pop-item-end">
                  <span className="bp-suggestion-tag">{item.type}</span>
                </span>
              </button>
            ))}
          </div>
        </Popover>
        </div>
        <div className="bp-tool-group bp-tools">
        <IconButton size="xs" icon={<IconTarget size={14} />} title={selecting ? "退出标注（Esc）" : "选择网页元素并标注"} active={selecting} disabled={!canAttach} onClick={toggleSelect} />
        <Menu trigger={<IconButton size="xs" icon={<IconCamera size={14} />} aria-label="截图标注" loading={capturing} disabled={capturing || !activeTab?.url || !isElectron} />}
          entries={[
            { label: "标注当前视口", onSelect: () => startCapture() },
            { label: "标注整页长图", onSelect: () => startCapture(true) },
            { label: "框选区域", onSelect: () => startCapture(false, true) },
            { label: "直接添加视口截图到对话", disabled: !canAttach, onSelect: handleCaptureToComposer },
          ]} />
        <Menu trigger={<IconButton size="xs" icon={<IconSparkles size={14} />} aria-label="网页 AI 操作" disabled={!canAttach || capturing} />}
          entries={[
            { label: "分析当前页面", onSelect: () => addPageContext(false) },
            { label: "诊断页面报错", onSelect: () => addPageContext(true) },
            { label: "连续标注多个元素", onSelect: () => {
              setMultiSelect(true); setElementBatch([]); setAnnotState(null);
              updateSessionState(activeSessionId, { tabView: "preview", selecting: true });
            } },
          ]} />
        <span className="bp-download-slot">
          <IconButton size="xs" icon={<IconDownload size={14} />} title="下载" disabled={!isElectron} onClick={() => openManager("downloads")} />
          {newDownload && <span className="bp-download-dot" aria-label="有新的下载" />}
        </span>
        <div className="bp-more-anchor" ref={moreAnchorRef}>
          <IconButton size="xs" icon={<IconMoreHorizontal size={14} />} title="更多" active={moreOpen || tabView !== "preview"} onClick={() => setMoreOpen((v) => !v)} />
          {newDownload && <span className="bp-download-dot bp-download-dot-compact" aria-label="有新的下载" />}
          <Popover open={moreOpen} onClose={() => setMoreOpen(false)} placement="bottom-end" className="bp-menu" ignoreRefs={[moreAnchorRef]} role="menu" aria-label="更多">
            <button type="button" role="menuitem" className="pop-item" onClick={() => openManager("bookmarks")}>
              <span className="pop-item-icon"><IconStar size={15} /></span>
              <span className="pop-item-title">管理书签</span>
            </button>
            <button type="button" role="menuitem" className="pop-item" onClick={() => openManager("history")}>
              <span className="pop-item-icon"><IconHistory size={15} /></span>
              <span className="pop-item-title">浏览历史</span>
            </button>
            <button type="button" role="menuitem" className="pop-item" disabled={!isElectron} onClick={() => openManager("vault")}>
              <span className="pop-item-icon"><IconLock size={15} /></span>
              <span className="pop-item-title">密码库</span>
            </button>
            <button type="button" role="menuitem" className="pop-item" disabled={!isElectron} onClick={() => openManager("downloads")}>
              <span className="pop-item-icon"><IconDownload size={15} /></span>
              <span className="pop-item-title">下载管理</span>
              {downloads.length > 0 && (
                <span className="pop-item-end"><span className="bp-menu-badge">{downloads.length}</span></span>
              )}
            </button>
            <div className="pop-sep" />
            <button type="button" role="menuitem" className={`pop-item${bookmarkBar ? " is-selected" : ""}`} onClick={() => { toggleBookmarkBar(); setMoreOpen(false); }}>
              <span className="pop-item-icon"><IconLayers size={15} /></span>
              <span className="pop-item-title">{bookmarkBar ? "隐藏书签栏" : "显示书签栏"}</span>
              {bookmarkBar && <span className="pop-item-end"><span className="pop-item-check"><IconCheck size={14} /></span></span>}
            </button>
            <button type="button" role="menuitem" className="pop-item" disabled={!activeTab?.url || !isElectron} onClick={() => { beginFind(); setMoreOpen(false); }}>
              <span className="pop-item-icon"><IconSearch size={15} /></span>
              <span className="pop-item-title">页内查找</span>
            </button>
            <div className="bp-menu-zoom" role="group" aria-label="网页缩放">
              <span className="bp-menu-zoom-label">页面缩放</span>
              <div className="bp-zoom-pill">
                <Button size="xs" variant="ghost" className="bp-zoom-btn" icon={<IconMinus size={12} />} iconOnly
                  aria-label="缩小" disabled={!activeTab?.url || !isElectron || zoom <= 0.25} onClick={() => changeZoom(zoom - 0.1)} />
                <Button size="xs" variant="ghost" className="bp-zoom-val"
                  aria-label="重置缩放" disabled={!activeTab?.url || !isElectron} onClick={() => changeZoom(1)}>
                  {Math.round(zoom * 100)}%
                </Button>
                <Button size="xs" variant="ghost" className="bp-zoom-btn" icon={<IconPlus size={12} />} iconOnly
                  aria-label="放大" disabled={!activeTab?.url || !isElectron || zoom >= 3} onClick={() => changeZoom(zoom + 0.1)} />
              </div>
            </div>
            <div className="pop-sep" />
            <button type="button" role="menuitem" className={`pop-item${tabView === "preview" ? " is-selected" : ""}`} onClick={() => { updateSessionState(activeSessionId, { tabView: "preview" }); setMoreOpen(false); }}>
              <span className="pop-item-icon"><IconGlobe size={15} /></span>
              <span className="pop-item-title">网页视图</span>
              {tabView === "preview" && <span className="pop-item-end"><span className="pop-item-check"><IconCheck size={14} /></span></span>}
            </button>
            <button type="button" role="menuitem" className={`pop-item${tabView === "dom" ? " is-selected" : ""}`} onClick={() => { updateSessionState(activeSessionId, { tabView: "dom" }); setMoreOpen(false); }}>
              <span className="pop-item-icon"><IconCode size={15} /></span>
              <span className="pop-item-title">DOM 快照</span>
              {tabView === "dom" && <span className="pop-item-end"><span className="pop-item-check"><IconCheck size={14} /></span></span>}
            </button>
            <button type="button" role="menuitem" className={`pop-item${tabView === "console" ? " is-selected" : ""}`} onClick={() => { updateSessionState(activeSessionId, { tabView: "console" }); setMoreOpen(false); }}>
              <span className="pop-item-icon"><IconTerminal size={15} /></span>
              <span className="pop-item-title">控制台求值</span>
              {tabView === "console" && <span className="pop-item-end"><span className="pop-item-check"><IconCheck size={14} /></span></span>}
            </button>
            <div className="pop-sep" />
            <button type="button" role="menuitem" className="pop-item" disabled={!activeTab?.url} onClick={openExternal}>
              <span className="pop-item-icon"><IconExternalLink size={15} /></span>
              <span className="pop-item-title">在系统浏览器中打开</span>
            </button>
            <button type="button" role="menuitem" className="pop-item" onClick={() => void openDevTools()}>
              <span className="pop-item-icon"><IconBug size={15} /></span>
              <span className="pop-item-title">开发者工具</span>
            </button>
          </Popover>
        </div>
        </div>
        {/* 加载进度：工具栏底边的固定槽位，出现 / 消失不改变任何布局 */}
        <span className={`bp-load${loading ? " is-loading" : ""}`} aria-hidden />
      </div>
      <Collapse open={bookmarkBar && bookmarks.length > 0} lazy>
        <div className="bp-bookmarks-bar" aria-label="书签栏">
          {directBookmarks.slice(0, 4).map((bookmark) => <Tooltip key={bookmark.id} content={bookmark.title}>
            <Button size="xs" variant="ghost" className="bp-bookmark" icon={bookmark.favicon_url && isBrowserHttpUrl(bookmark.favicon_url)
              ? <img src={bookmark.favicon_url} alt="" /> : <IconGlobe size={12} />}
              onContextMenu={(event) => { event.preventDefault(); setBookmarkMenu({ x: event.clientX, y: event.clientY, item: bookmark }); }}
              onClick={() => submitUrl(bookmark.url)}>
              <span>{bookmark.title}</span>
            </Button>
          </Tooltip>)}
          {Array.from(bookmarkFolders).slice(0, 2).map(([folder, items]) => <Menu key={folder}
            trigger={<Button size="xs" variant="ghost" className="bp-bookmark" icon={<IconFolder size={13} />}><span>{folder}</span></Button>}
            entries={items.map((item) => ({ label: item.title, onSelect: () => submitUrl(item.url) }))} />)}
          <Menu trigger={<IconButton size="xs" aria-label="书签栏更多" icon={<IconMoreHorizontal size={13} />} />}
            entries={[
              ...directBookmarks.slice(4).map((item) => ({ label: item.title, onSelect: () => submitUrl(item.url) })),
              ...Array.from(bookmarkFolders).slice(2).flatMap(([folder, items]) => [
                { label: folder, groupLabel: true }, ...items.map((item) => ({ label: item.title, onSelect: () => submitUrl(item.url) })),
              ]),
              { label: "管理书签", onSelect: () => openManager("bookmarks") },
            ]} />
        </div>
      </Collapse>
      <Collapse open={visibleLoginOffer != null} lazy>
        {visibleLoginOffer && <div className="bp-login-offer" role="status">
          <span>{visibleLoginOffer.update ? "更新" : "保存"} {hostOf(visibleLoginOffer.origin)} 的账号「{visibleLoginOffer.username}」？</span>
          <div className="bp-tool-group">
            <Button size="xs" variant="primary" onClick={() => void resolveLogin("save")}>{visibleLoginOffer.update ? "更新" : "保存"}</Button>
            <Button size="xs" variant="ghost" onClick={() => void resolveLogin("never")}>此站点不提醒</Button>
            <IconButton size="xs" title="暂不保存" icon={<IconX size={13} />} onClick={() => void resolveLogin("skip")} />
          </div>
        </div>}
      </Collapse>
      <BrowserFindBar open={find?.tabId === activeTabId && shown} frame={(tabFrameRefs.current.get(activeTabId) ?? null) as FindGuest | null}
        seed={find?.seed ?? ""} onClose={() => setFind(null)} />

      {/* 视口 */}
      <div className="browser-viewport bp-viewport" ref={viewportRef} style={{ cursor: selecting ? "crosshair" : undefined }}>
        {tabs.map((tab) => {
          const isFront = tab.id === activeTabId && tabView === "preview";
          const hasPage = Boolean(tab.url || tab.src);
          // 前台且面板可见：正常显示；其余（有页面的）标签停泊——保持尺寸继续渲染，AI 可在后台操作
          const parked = hasPage && !(isFront && shown);
          const hiddenBlank = !hasPage && !isFront;
          return (
            <div
              key={tab.id}
              className={`browser-tab-frame-wrapper${parked ? " is-parked" : ""}${hiddenBlank ? " is-hidden" : ""}`}
              aria-hidden={parked || hiddenBlank}
            >
              {!hasPage ? (
                <BrowserStartPage onNavigate={submitUrl} />
              ) : isElectron ? (
                <webview
                  ref={getTabRefCallback(tab.id)}
                  src={mountSrcOf(tab)}
                  className="browser-frame-element"
                  // 允许网页请求新窗口：主进程拦下后改为在面板里新开标签（见 browser-agent.cjs）
                  {...({ allowpopups: "true" } as Record<string, string>)}
                />
              ) : (
                <iframe
                  ref={getTabRefCallback(tab.id)}
                  src={tab.src}
                  className="browser-frame-element"
                  sandbox="allow-scripts allow-same-origin allow-forms allow-popups"
                  title={`browser-${tab.id}`}
                />
              )}
              {tab.error && isFront && (
                <div className="bp-error">
                  <div className="bp-error-title">页面无法打开</div>
                  <div className="bp-error-msg">{tab.error}</div>
                  <Button size="sm" variant="ghost" onClick={() => reload(activeSessionId, tab.id)}>重试</Button>
                </div>
              )}
              <BrowserAgentOverlay agent={agent} tabId={tab.id} shown={isFront && shown} />
            </div>
          );
        })}

        {tabView === "dom" && (
          <div className="browser-subview browser-dom-view">
            <div className="browser-subview-header">
              <span className="browser-subview-title">DOM 结构快照（{activeTab?.title || "当前页面"}）</span>
              <Button size="sm" variant="ghost" disabled={!canAttach || capturing}
                onClick={() => void captureAction(async () => {
                  const source = sourceOf();
                  await ensureShareable(source.tabId);
                  let snapshot = "";
                  const currentFrame = tabFrameRefs.current.get(activeTabId);
                  try {
                    if (isGuestFrame(currentFrame)) snapshot = (await frameBridge.call<string>(activeTabId, "document.documentElement.outerHTML", { waitReady: true })) || "";
                    else if (currentFrame?.contentDocument) snapshot = currentFrame.contentDocument.documentElement.outerHTML;
                  } catch { /* ignore */ }
                  const truncated = redactDiagnostic(snapshot || `[页面 ${safeContextUrl(source.url)}]`).slice(0, 4000);
                  await attach(source, { kind: "dom", text: truncated });
                  updateSessionState(activeSessionId, { domSnapshot: truncated });
                })}
              >
                发送快照到输入框
              </Button>
            </div>
            <pre className="browser-code-pre">{domSnapshot || "暂无快照，点击上方按钮捕获当前页面 DOM"}</pre>
          </div>
        )}

        {tabView === "console" && (
          <div className="browser-subview browser-console-view">
            <div className="browser-subview-header">
              <span className="browser-subview-title">控制台求值</span>
            </div>
            <div className="browser-console-tip">输入 JavaScript 表达式，按 Enter 执行并把结果添加到输入框：</div>
            <div className="browser-console-input-row">
              <Input
                className="browser-console-input"
                placeholder="例如：document.title 或 location.href"
                aria-label="控制台表达式"
                disabled={!canAttach || capturing}
                onKeyDown={(e) => {
                  if (e.key !== "Enter" || e.nativeEvent.isComposing) return;
                  const code = e.currentTarget.value;
                  if (!code) return;
                  void captureAction(async () => {
                    const source = sourceOf();
                    await ensureShareable(source.tabId);
                    const result = await frameBridge.invoke<string>(source.tabId, code, { waitReady: true });
                    const output = result.ok ? String(result.value) : result.notReady ? "页面尚未就绪" : result.error;
                    await attach(source, { kind: "console", text: redactDiagnostic(`> ${code}\n${output}`).slice(0, 5500) });
                  });
                }}
              />
            </div>
          </div>
        )}

        {selecting && canAttach && (
          <div className="browser-annot-overlay active" onMouseMove={handleOverlayMouseMove} onClick={handleOverlayClick} onMouseLeave={() => setHoverInfo(null)}>
            <div className="browser-annot-tip">{multiSelect ? "依次点击要标注的网页元素" : "点击网页元素进行标注"} · Esc 退出</div>
            <ElementInspector hoverInfo={hoverInfo} cursorPos={cursorPos} containerRect={viewportSize ?? undefined} />
            {multiSelect && elementBatch.map((item, i) => <span key={i} className="bp-element-pin"
              style={{ left: item.clickX, top: item.clickY }}>{i + 1}</span>)}
          </div>
        )}

        <Collapse className="bp-annot-card-layer" open={!!annotState && canAttach} lazy>
        {annotState && (
          <div className="browser-annot-card" style={{ left: annotState.x, top: annotState.y }}>
            <div className="browser-annot-card-header">
              <span className="browser-annot-card-title">已选中元素</span>
              <IconButton size="xs" icon={<IconX size={12} />} title="关闭" onClick={() => { setAnnotState(null); setAnnotText(""); }} />
            </div>
            <div className="browser-annot-card-source">{annotState.source.substring(0, 160)}</div>
            {annotState.info && (
              <div className="browser-annot-info">
                <div className="browser-annot-info-title">
                  &lt;{annotState.info.tag}&gt;
                  {annotState.info.id && <span className="browser-annot-info-id">#{annotState.info.id}</span>}
                  {annotState.info.className && (
                    <span className="browser-annot-info-cls">.{annotState.info.className.split(" ").filter(Boolean).slice(0, 3).join(".")}</span>
                  )}
                </div>
                <div className="browser-annot-info-grid">
                  <span>尺寸</span><b>{annotState.info.width} × {annotState.info.height}px</b>
                  <span>位置</span><b>({annotState.info.x}, {annotState.info.y})</b>
                  <span>布局</span><b>{annotState.info.display}</b>
                  <span>字体</span><b>{annotState.info.fontSize}</b>
                  {annotState.info.color && <span>文字色</span>}
                  {annotState.info.color && <b style={{ color: annotState.info.color }}>{annotState.info.color}</b>}
                </div>
              </div>
            )}
            <Textarea
              className="browser-annot-card-textarea"
              value={annotText}
              onChange={(e) => setAnnotText(e.target.value)}
              placeholder="描述你希望 AI 关注的问题或修改建议…"
              autoFocus
            />
            <div className="browser-annot-card-actions">
              <Button size="sm" variant="ghost" onClick={() => { setMultiSelect(true); setElementBatch([annotState]); setAnnotState(null); }}>
                继续选取
              </Button>
              <Button size="sm" variant="primary" disabled={capturing} icon={<IconArrowUp size={12} />} onClick={handleSendAnnotToChat}>
                添加到对话
              </Button>
            </div>
          </div>
        )}
        </Collapse>
        <Collapse className="bp-element-batch-layer" open={selecting && multiSelect && canAttach} lazy>
          <div className="bp-element-batch">
            <span>已选 {elementBatch.length} 个元素</span>
            <div className="bp-element-list">{elementBatch.map((item, i) => <div key={i} className="bp-element-entry">
              <span>{i + 1}. {item.info?.selector || item.info?.tag || item.source}</span>
              <IconButton size="xs" title={`移除元素 ${i + 1}`} icon={<IconX size={12} />}
                onClick={() => setElementBatch((old) => old.filter((_, index) => index !== i))} />
            </div>)}</div>
            <Input size="sm" value={annotText} aria-label="批注说明" placeholder="添加说明（选填）" onChange={(event) => setAnnotText(event.target.value)} />
            <div className="browser-annot-card-actions">
              <Button variant="ghost" size="sm" onClick={toggleSelect}>退出</Button>
              <Button variant="primary" size="sm" disabled={!elementBatch.length || capturing} onClick={handleSendAnnotToChat}>添加到对话</Button>
            </div>
          </div>
        </Collapse>

        {/* 顶部提示：AI 在用户没在看的标签里工作 */}
        {elsewhereTab && elsewhereTab.id !== activeTabId && (
          <BrowserAgentElsewhere
            title={elsewhereTab.title || hostOf(elsewhereTab.url)}
            onView={() => { setActiveTab(activeSessionId, elsewhereTab.id); updateSessionState(activeSessionId, { agentElsewhere: null }); }}
            onDismiss={() => updateSessionState(activeSessionId, { agentElsewhere: null })}
          />
        )}

        {/* 底部状态胶囊：AI 正在做什么（AI 在别的标签工作时点击可切过去） */}
        {shown && (
          <BrowserAgentCapsule
            agent={agent}
            onReveal={agentTab && agentTab.id !== activeTabId ? () => setActiveTab(activeSessionId, agentTab.id) : undefined}
          />
        )}

        {sentToast && <div className="bp-toast" role="status">{sentToast}</div>}
        <div className={`bp-statusbar${linkTarget ? " is-visible" : ""}`} aria-hidden={!linkTarget}>{linkTarget}</div>

        {dialog && <BrowserPageDialog sessionId={activeSessionId} dialog={dialog} />}
      </div>
      <PointContextMenu point={tabMenu} items={tabItems} onClose={() => setTabMenu(null)} />
      <PointContextMenu point={bookmarkMenu} onClose={() => setBookmarkMenu(null)}
        items={bookmarkMenu ? [
          { label: "打开书签", onClick: () => submitUrl(bookmarkMenu.item.url) },
          { label: "编辑书签", onClick: () => openManager("bookmarks", bookmarkMenu.item) },
          { label: "删除书签", danger: true, onClick: () => {
            void api.deleteBrowserBookmark(bookmarkMenu.item.id).then(refreshBookmarks).catch((error: unknown) => showToast(String(error)));
          } },
        ] : []} />
      <BrowserManagerModals mode={manager} onMode={setManager} url={activeTab?.url || ""} title={activeTab?.title || ""}
        activeBookmark={activeBookmark} bookmarkToEdit={bookmarkToEdit} downloads={downloads} isElectron={isElectron}
        onNavigate={submitUrl} onVaultChanged={() => setVaultRevision((value) => value + 1)} />
      {shot && <BrowserScreenshotAnnotator key={shotSequence} open={shotOpen} shot={shot}
        canAttach={tabs.some((tab) => tab.id === shot.tabId && !tab.loginProtected)} onClose={() => setShotOpen(false)}
        onAttach={async (blob, note) => { await attach(shot, { kind: "screenshot", note }, blob); setShotOpen(false); }} />}
    </div>
  );
}
