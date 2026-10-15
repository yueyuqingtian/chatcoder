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
import { useCallback, useEffect, useRef, useState } from "react";
import { useChatStore } from "../../store/chat";
import { normalizeUrl, sessionKey, useBrowserStore, type BrowserTabState, type ElementInfo } from "../../store/browser";
import { usePanelStore } from "../../store/panel";
import { api, type UploadOut } from "../../api/client";
import { ElementInspector } from "./ElementInspector";
import { BrowserStartPage } from "./BrowserStartPage";
import { BrowserAgentCapsule, BrowserAgentElsewhere, BrowserAgentOverlay, BrowserPageDialog } from "./BrowserAgentLayer";
import { useFrameBridge, isGuestFrame, type FrameEl } from "./frameBridge";
import {
  markFrameReady, noteLoadingStop, noteNavigationStart, pushConsole, registerFrame, unregisterFrame,
} from "../../browserAgent/frames";
import { isBusy, subscribe } from "../../perf/bus";
import { registerReconcileTask, RECONCILE_ORDER } from "../../perf/reconcile";
import { recordDerivation } from "../../perf/metrics";
import { IconButton, Input, Popover } from "../ui";
import {
  IconArrowLeft, IconArrowRight, IconRefresh, IconGlobe, IconTarget, IconArrowUp, IconX, IconCode, IconBug,
  IconTerminal, IconPlus, IconCamera, IconLock, IconMoreHorizontal, IconSparkles, IconSpinner, IconExternalLink,
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
  const setActiveTab = useBrowserStore((s) => s.setActiveTab);
  const updateTab = useBrowserStore((s) => s.updateTab);
  const navigate = useBrowserStore((s) => s.navigate);
  const goBack = useBrowserStore((s) => s.goBack);
  const goForward = useBrowserStore((s) => s.goForward);
  const reload = useBrowserStore((s) => s.reload);
  const stop = useBrowserStore((s) => s.stop);
  const noteUserActive = useBrowserStore((s) => s.noteUserActive);
  const panelOnScreen = usePanelStore((s) => s.onScreen);
  const addComposerBrowserRef = useChatStore((s) => s.addComposerBrowserRef);

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
  const [annotState, setAnnotState] = useState<{ x: number; y: number; source: string; info: ElementInfo | null; screenshotDataUrl?: string } | null>(null);
  const [annotText, setAnnotText] = useState("");
  const [sentToast, setSentToast] = useState<string | null>(null);
  const [capturing, setCapturing] = useState(false);
  const [moreOpen, setMoreOpen] = useState(false);
  const moreAnchorRef = useRef<HTMLDivElement>(null);

  const [hoverInfo, setHoverInfo] = useState<{ rect: { x: number; y: number; width: number; height: number }; info: ElementInfo } | null>(null);
  const [cursorPos, setCursorPos] = useState<{ x: number; y: number } | null>(null);

  const viewportRef = useRef<HTMLDivElement>(null);
  const tabFrameRefs = useRef<Map<string, HTMLIFrameElement | any>>(new Map());
  const frameBridge = useFrameBridge();
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
    if (!vp) return;
    let w = 0;
    let h = 0;
    if (shownRef.current) {
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
      }],
      ["did-start-navigation", (e) => {
        if (e.isMainFrame && !e.isInPlace) noteNavigationStart(sid, tabId);
      }],
      ["did-navigate", (e) => updateTab(activeSessionId, tabId, { url: e.url, error: undefined, ...nav() })],
      ["did-navigate-in-page", (e) => { if (e.isMainFrame) updateTab(activeSessionId, tabId, { url: e.url, ...nav() }); }],
      ["page-title-updated", (e) => updateTab(activeSessionId, tabId, { title: String(e.title || "") })],
      ["page-favicon-updated", (e) => {
        const icon = Array.isArray(e.favicons) ? e.favicons[0] : "";
        if (icon) updateTab(activeSessionId, tabId, { favicon: String(icon) });
      }],
      ["did-fail-load", (e) => {
        if (!e.isMainFrame || e.errorCode === -3) return; // -3 = 被新导航替代，不算失败
        noteLoadingStop(sid, tabId);
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
  }, [sKey, activeSessionId, frameBridge, updateTab]);

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
    window.setTimeout(() => setSentToast(null), 2600);
  };

  const markUser = () => noteUserActive(activeSessionId);

  const toggleSelect = () => {
    const next = !selecting;
    updateSessionState(activeSessionId, { selecting: next });
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
    if (!selecting) return;
    void frameBridge.invoke(activeTabId, INSPECT_PROBE_SRC, { waitReady: true });
    return () => {
      void frameBridge.invoke(activeTabId, "window.__ccInspect && (window.__ccInspect.enabled = false)", { waitReady: false });
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selecting, activeTabId, activeTab?.url, frameBridge]);

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
        .call(activeTabId, `window.__ccInspect ? __ccInspect.infoAt(${Math.round(x)}, ${Math.round(y)}) : null`, { waitReady: false })
        .then((res: any) => {
          q.pending = false;
          if (!selectingRef.current) return;
          if (res && res.info) setHoverInfo({ rect: res.rect, info: res.info });
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
        const res = await frameBridge.call<any>(activeTabId, `window.__ccInspect ? __ccInspect.pickAt(${Math.round(localX)}, ${Math.round(localY)}) : null`, { waitReady: true });
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
    let screenshotDataUrl = "";
    if (isGuestFrame(currentFrame)) screenshotDataUrl = await frameBridge.capture(activeTabId);
    if (!screenshotDataUrl && window.chatcoderAPI?.captureBrowserPage) {
      try { screenshotDataUrl = (await window.chatcoderAPI.captureBrowserPage()) || ""; } catch { /* ignore */ }
    }
    const CARD_W = 320, CARD_H = 340;
    let cardX = localX + 12;
    if (cardX + CARD_W > vpRect.width - 8) cardX = Math.max(8, localX - CARD_W - 12);
    let cardY = localY + 12;
    if (cardY + CARD_H > vpRect.height - 8) cardY = Math.max(8, vpRect.height - CARD_H - 8);
    setAnnotState({ x: cardX, y: cardY, source, info, screenshotDataUrl });
    setAnnotText("");
    setHoverInfo(null);
  };

  /** 截图：优先主进程隐藏态截图（不受面板显隐影响），回退 webview.capturePage */
  const captureDataUrl = async (): Promise<string> => {
    const currentFrame = tabFrameRefs.current.get(activeTabId);
    let wcId = 0;
    try { wcId = currentFrame?.getWebContentsId?.() ?? 0; } catch { wcId = 0; }
    if (wcId && window.chatcoderAPI?.browserAgentExec) {
      const res = await window.chatcoderAPI.browserAgentExec(wcId, "capture", { max_side: 2560, quality: 90 });
      if (res?.ok && res.result?.base64) return `data:${res.result.mime || "image/jpeg"};base64,${res.result.base64}`;
    }
    if (isGuestFrame(currentFrame)) {
      const d = await frameBridge.capture(activeTabId);
      if (d) return d;
    }
    if (window.chatcoderAPI?.captureBrowserPage) return (await window.chatcoderAPI.captureBrowserPage()) || "";
    return "";
  };

  const handleCaptureToComposer = async () => {
    setCapturing(true);
    try {
      const dataUrl = await captureDataUrl();
      if (!dataUrl) { showToast("截图失败：没有拿到图像数据"); return; }
      const blob = await (await fetch(dataUrl)).blob();
      const ext = blob.type.includes("png") ? "png" : "jpg";
      const file = new File([blob], `browser-shot-${Date.now().toString().slice(-6)}.${ext}`, { type: blob.type || "image/jpeg" });
      const uploaded = await api.uploadFile(file);
      addComposerBrowserRef({
        id: `ref-${Date.now()}`,
        kind: "screenshot",
        pageTitle: activeTab?.title || "网页截图",
        url: activeTab?.url || "about:blank",
        thumbUrl: uploaded.url,
        attachment: {
          file_id: uploaded.file_id, filename: uploaded.filename, path: uploaded.path, url: uploaded.url,
          size: uploaded.size, mime_type: uploaded.mime_type, type: uploaded.type,
        },
        createdAt: Date.now(),
      });
      showToast("已截图并添加到输入框");
    } catch (e: any) {
      showToast(`截图上传失败：${e?.message || e}`);
    } finally {
      setCapturing(false);
    }
  };

  const handleSendAnnotToChat = async () => {
    if (!annotState) return;
    let shot: UploadOut | null = null;
    if (annotState.screenshotDataUrl) {
      try {
        const blob = await (await fetch(annotState.screenshotDataUrl)).blob();
        const file = new File([blob], `annot-${Date.now().toString().slice(-6)}.png`, { type: "image/png" });
        shot = await api.uploadFile(file);
      } catch (e) {
        console.warn("annot screenshot upload failed:", e);
      }
    }
    const inf = annotState.info;
    const selectorStr = inf ? `<${inf.tag}> ${inf.id ? "#" + inf.id : ""} ${inf.className ? "." + inf.className.split(" ").join(".") : ""}` : "";
    addComposerBrowserRef({
      id: `ref-${Date.now()}`,
      kind: "element",
      pageTitle: activeTab?.title || "网页元素标注",
      url: activeTab?.url || "about:blank",
      selector: selectorStr,
      bbox: inf ? { width: inf.width, height: inf.height, x: inf.x, y: inf.y } : undefined,
      styleDigest: inf ? `color: ${inf.color}; bg: ${inf.backgroundColor}; font: ${inf.fontSize}` : undefined,
      text: inf?.text || undefined,
      note: annotText.trim() || undefined,
      thumbUrl: shot?.url || undefined,
      attachment: shot
        ? { file_id: shot.file_id, filename: shot.filename, path: shot.path, url: shot.url, size: shot.size, mime_type: shot.mime_type, type: shot.type }
        : undefined,
      createdAt: Date.now(),
    });
    showToast("已添加标注到输入框");
    updateSessionState(activeSessionId, { selecting: false });
    setAnnotState(null);
    setAnnotText("");
  };

  const openDevTools = async () => {
    setMoreOpen(false);
    try {
      if (frameBridge.openDevTools(activeTabId)) return;
      if (window.chatcoderAPI?.openBrowserDevTools) await window.chatcoderAPI.openBrowserDevTools();
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

  const submitUrl = () => {
    const url = normalizeUrl(inputUrl);
    if (!url || !activeTab) return;
    markUser();
    navigate(activeSessionId, url, activeTabId);
    urlInputRef.current?.blur();
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
    <div className="browser-panel bp" onPointerDownCapture={markUser} onKeyDownCapture={markUser} onWheelCapture={markUser}>
      {/* 标签条 */}
      <div className="bp-tabs" role="tablist">
        <div className="bp-tabs-list">
          {tabs.map((tab) => {
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
                onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setActiveTab(activeSessionId, tab.id); } }}
                onAuxClick={(e) => { if (e.button === 1) { e.preventDefault(); closeTab(activeSessionId, tab.id); } }}
              >
                <span className="bp-tab-ic">
                  {tab.loading ? <IconSpinner size={12} />
                    : tab.owner === "ai" ? <IconSparkles size={12} />
                      : tab.favicon ? <img src={tab.favicon} alt="" width={14} height={14} onError={(e) => { (e.currentTarget as HTMLImageElement).style.visibility = "hidden"; }} />
                        : <IconGlobe size={12} />}
                </span>
                <span className="bp-tab-title">{label}</span>
                {aiWorking && <span className="bp-tab-dot" aria-label="AI 正在操作" />}
                <button
                  type="button"
                  className="bp-tab-close"
                  aria-label={`关闭标签：${label}`}
                  onClick={(e) => { e.stopPropagation(); closeTab(activeSessionId, tab.id); }}
                >
                  <IconX size={10} />
                </button>
              </div>
            );
          })}
        </div>
        <IconButton size="xs" icon={<IconPlus size={14} />} title="新建标签页" onClick={() => newTab(activeSessionId)} />
      </div>

      {/* 工具栏 */}
      <div className="bp-toolbar">
        <IconButton size="xs" icon={<IconArrowLeft size={14} />} title="后退" disabled={!activeTab?.canGoBack} onClick={() => { markUser(); goBack(activeSessionId, activeTabId); }} />
        <IconButton size="xs" icon={<IconArrowRight size={14} />} title="前进" disabled={!activeTab?.canGoForward} onClick={() => { markUser(); goForward(activeSessionId, activeTabId); }} />
        {loading
          ? <IconButton size="xs" icon={<IconX size={14} />} title="停止加载" onClick={() => stop(activeSessionId, activeTabId)} />
          : <IconButton size="xs" icon={<IconRefresh size={14} />} title="刷新" disabled={!activeTab?.url} onClick={() => { markUser(); reload(activeSessionId, activeTabId); }} />}
        <Input
          ref={urlInputRef}
          size="sm"
          wrapClassName="bp-url"
          prefix={<span className="bp-url-ic">{activeTab?.owner === "ai" ? <IconSparkles size={13} /> : secure ? <IconLock size={13} /> : <IconGlobe size={13} />}</span>}
          value={inputUrl}
          onChange={(e) => setInputUrl(e.target.value)}
          onFocus={(e) => { setUrlFocused(true); e.currentTarget.select(); }}
          onBlur={() => setUrlFocused(false)}
          onKeyDown={(e) => {
            if (e.key === "Enter") { e.preventDefault(); submitUrl(); }
            if (e.key === "Escape") { setInputUrl(activeTab?.url || ""); e.currentTarget.blur(); }
          }}
          placeholder="输入网址，如 localhost:5173"
          spellCheck={false}
          autoCapitalize="none"
          autoCorrect="off"
          aria-label="网址"
        />
        <IconButton size="xs" icon={<IconTarget size={14} />} title={selecting ? "退出标注（Esc）" : "选择网页元素并标注"} active={selecting} disabled={!activeTab?.url} onClick={toggleSelect} />
        <IconButton size="xs" icon={<IconCamera size={14} />} title="截图并添加到输入框" disabled={capturing || !activeTab?.url} onClick={() => void handleCaptureToComposer()} />
        <div className="bp-more-anchor" ref={moreAnchorRef}>
          <IconButton size="xs" icon={<IconMoreHorizontal size={14} />} title="更多" active={moreOpen || tabView !== "preview"} onClick={() => setMoreOpen((v) => !v)} />
          <Popover open={moreOpen} onClose={() => setMoreOpen(false)} placement="bottom-end" className="bp-menu" ignoreRefs={[moreAnchorRef]} role="menu" aria-label="更多">
            <button type="button" role="menuitem" className={`pop-item${tabView === "preview" ? " is-selected" : ""}`} onClick={() => { updateSessionState(activeSessionId, { tabView: "preview" }); setMoreOpen(false); }}>
              <span className="pop-item-icon"><IconGlobe size={15} /></span><span className="pop-item-title">网页视图</span>
            </button>
            <button type="button" role="menuitem" className={`pop-item${tabView === "dom" ? " is-selected" : ""}`} onClick={() => { updateSessionState(activeSessionId, { tabView: "dom" }); setMoreOpen(false); }}>
              <span className="pop-item-icon"><IconCode size={15} /></span><span className="pop-item-title">DOM 快照</span>
            </button>
            <button type="button" role="menuitem" className={`pop-item${tabView === "console" ? " is-selected" : ""}`} onClick={() => { updateSessionState(activeSessionId, { tabView: "console" }); setMoreOpen(false); }}>
              <span className="pop-item-icon"><IconTerminal size={15} /></span><span className="pop-item-title">控制台求值</span>
            </button>
            <div className="pop-sep" />
            <button type="button" role="menuitem" className="pop-item" disabled={!activeTab?.url} onClick={openExternal}>
              <span className="pop-item-icon"><IconExternalLink size={15} /></span><span className="pop-item-title">在系统浏览器中打开</span>
            </button>
            <button type="button" role="menuitem" className="pop-item" onClick={() => void openDevTools()}>
              <span className="pop-item-icon"><IconBug size={15} /></span><span className="pop-item-title">开发者工具</span>
            </button>
          </Popover>
        </div>
        {/* 加载进度：工具栏底边的固定槽位，出现 / 消失不改变任何布局 */}
        <span className={`bp-load${loading ? " is-loading" : ""}`} aria-hidden />
      </div>

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
                <BrowserStartPage onNavigate={(url) => navigate(activeSessionId, url, tab.id)} />
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
                  <button type="button" className="btn btn-ghost btn-sm" onClick={() => reload(activeSessionId, tab.id)}>重试</button>
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
              <button
                className="btn btn-ghost btn-sm"
                onClick={async () => {
                  let snapshot = "";
                  const currentFrame = tabFrameRefs.current.get(activeTabId);
                  try {
                    if (isGuestFrame(currentFrame)) snapshot = (await frameBridge.call<string>(activeTabId, "document.documentElement.outerHTML", { waitReady: true })) || "";
                    else if (currentFrame?.contentDocument) snapshot = currentFrame.contentDocument.documentElement.outerHTML;
                  } catch { /* ignore */ }
                  const truncated = (snapshot || `[页面: ${activeTab?.url}]`).substring(0, 4000);
                  updateSessionState(activeSessionId, { domSnapshot: truncated });
                  addComposerBrowserRef({
                    id: `ref-${Date.now()}`, kind: "dom", pageTitle: activeTab?.title || "DOM 快照", url: activeTab?.url || "about:blank",
                    text: truncated.slice(0, 200) + "...", createdAt: Date.now(),
                  });
                  showToast("已发送 DOM 快照到输入框");
                }}
              >
                发送快照到输入框
              </button>
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
              <input
                className="browser-console-input"
                placeholder="例如：document.title 或 location.href"
                onKeyDown={async (e) => {
                  if (e.key !== "Enter") return;
                  const code = (e.target as HTMLInputElement).value;
                  if (!code) return;
                  let res = "";
                  const currentFrame = tabFrameRefs.current.get(activeTabId);
                  if (isGuestFrame(currentFrame)) {
                    const r = await frameBridge.invoke<string>(activeTabId, code, { waitReady: true });
                    if (r.ok) res = String(r.value);
                    else if (r.notReady) res = "Error: 页面尚未就绪，请稍后重试";
                    else res = `Error: ${r.error}`;
                  }
                  addComposerBrowserRef({
                    id: `ref-${Date.now()}`, kind: "console", pageTitle: activeTab?.title || "控制台求值", url: activeTab?.url || "about:blank",
                    text: `> ${code}\n${res}`, createdAt: Date.now(),
                  });
                  showToast("已把求值结果添加到输入框");
                }}
              />
            </div>
          </div>
        )}

        {selecting && (
          <div className="browser-annot-overlay active" onMouseMove={handleOverlayMouseMove} onClick={handleOverlayClick} onMouseLeave={() => setHoverInfo(null)}>
            <div className="browser-annot-tip">移动鼠标聚焦元素，点击添加标注 · 按 Esc 退出</div>
            <ElementInspector hoverInfo={hoverInfo} cursorPos={cursorPos} containerRect={viewportSize ?? undefined} />
          </div>
        )}

        {annotState && (
          <div className="browser-annot-card" style={{ position: "absolute", left: annotState.x, top: annotState.y }}>
            <div className="browser-annot-card-header">
              <span className="browser-annot-card-title">已选中元素，添加说明后发送：</span>
              <button className="browser-annot-card-close" aria-label="关闭" onClick={() => { setAnnotState(null); setAnnotText(""); }}>
                <IconX size={12} />
              </button>
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
            <textarea
              className="browser-annot-card-textarea"
              value={annotText}
              onChange={(e) => setAnnotText(e.target.value)}
              placeholder="描述你希望 AI 关注的问题或修改建议…"
              autoFocus
            />
            <div className="browser-annot-card-actions">
              <button className="btn btn-ghost btn-sm" onClick={() => { updateSessionState(activeSessionId, { selecting: false }); setAnnotState(null); setAnnotText(""); }}>
                完成
              </button>
              <button className="btn btn-primary btn-sm" onClick={handleSendAnnotToChat}>
                <IconArrowUp size={12} /> 发送到输入框
              </button>
            </div>
          </div>
        )}

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

        {dialog && <BrowserPageDialog sessionId={activeSessionId} dialog={dialog} />}
      </div>
    </div>
  );
}
