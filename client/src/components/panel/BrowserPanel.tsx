/**
 * 现代多标签浏览器面板（v6）
 *
 * 核心特性：
 * 1. 完整支持一个会话内的多标签页（新建、关闭、切换、历史、独立标题/URL）；
 * 2. 默认空起始页（BrowserStartPage），不打开写死的默认站点；
 * 3. 所有标签的 webview / iframe 保活挂载（display: none 切换），切换与面板折叠绝不重载；
 * 4. DevTools 级高灵敏元素标注：实时盒模型覆盖层、尺寸、颜色、字体、无障碍等属性卡，Esc 退出；
 * 5. 一键截屏与元素标注同时生成真实图片附件 + 结构化引用卡注入输入框；
 * 6. 支持 AI 工具（Playwright）镜像广播：AI 操作时标签自动同步并向用户呈现操作反馈。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { useChatStore } from "../../store/chat";
import { useBrowserStore, type ElementInfo } from "../../store/browser";
import { api, type UploadOut } from "../../api/client";
import { ElementInspector } from "./ElementInspector";
import { BrowserStartPage } from "./BrowserStartPage";
import { useFrameBridge, isGuestFrame, type FrameEl } from "./frameBridge";
import { isBusy, subscribe } from "../../perf/bus";
import { registerReconcileTask, RECONCILE_ORDER } from "../../perf/reconcile";
import {
  IconArrowLeft,
  IconArrowRight,
  IconRefresh,
  IconGlobe,
  IconTarget,
  IconArrowUp,
  IconX,
  IconCode,
  IconBug,
  IconTerminal,
  IconPlus,
  IconImage,
} from "../icons";

export function BrowserPanel() {
  const activeSessionId = useChatStore((s) => s.currentSessionId);
  const browserState = useBrowserStore((s) => s.getSessionState(activeSessionId));
  const updateSessionState = useBrowserStore((s) => s.updateSessionState);
  const newTab = useBrowserStore((s) => s.newTab);
  const closeTab = useBrowserStore((s) => s.closeTab);
  const setActiveTab = useBrowserStore((s) => s.setActiveTab);
  const navigate = useBrowserStore((s) => s.navigate);
  const goBack = useBrowserStore((s) => s.goBack);
  const goForward = useBrowserStore((s) => s.goForward);

  const addComposerBrowserRef = useChatStore((s) => s.addComposerBrowserRef);

  const isElectron = typeof window !== "undefined" && Boolean(window.chatcoderAPI?.openBrowserDevTools || (window as any).process?.versions?.electron);

  const tabs = browserState.tabs || [];
  const activeTabId = browserState.activeTabId || (tabs[0]?.id ?? "");
  const activeTab = tabs.find((t) => t.id === activeTabId) || tabs[0];

  const tabView = browserState.tabView;
  const domSnapshot = browserState.domSnapshot;
  const selecting = browserState.selecting;

  const [inputUrl, setInputUrl] = useState(activeTab?.current || "");
  const [annotState, setAnnotState] = useState<{ x: number; y: number; source: string; info: ElementInfo | null; screenshotDataUrl?: string } | null>(null);
  const [annotText, setAnnotText] = useState("");
  const [sentToast, setSentToast] = useState<string | null>(null);
  const [capturing, setCapturing] = useState(false);

  // 悬停检查状态
  const [hoverInfo, setHoverInfo] = useState<{
    rect: { x: number; y: number; width: number; height: number };
    info: ElementInfo;
  } | null>(null);
  const [cursorPos, setCursorPos] = useState<{ x: number; y: number } | null>(null);

  const viewportRef = useRef<HTMLDivElement>(null);
  // 多标签 webview / iframe 引用映射表
  const tabFrameRefs = useRef<Map<string, HTMLIFrameElement | any>>(new Map());
  // guest 安全调用桥：未 attach / 未 dom-ready 时挂起或丢弃调用，绝不让同步异常冒泡
  const frameBridge = useFrameBridge();

  // 记录上一次已同步的 tab 与 URL，避免用户输入中被 re-render 冲掉
  const lastSyncedRef = useRef<{ tabId?: string; current?: string }>({});

  /** 每个标签页的宿主元素句柄（稳定 ref 回调 + 已注册的事件处理器，便于精确解绑） */
  const tabBindingsRef = useRef<
    Map<string, { ref: (el: FrameEl | null) => void; el?: FrameEl; handler?: () => void }>
  >(new Map());

  /** 视口尺寸统一入口（plan-329-1647 S5）。
   *
   *  原有职责：webview（Electron guest 宿主）与 iframe 仅靠 CSS 百分比，在面板折叠恢复、
   *  全屏切换、标签切换、窗口 resize 等时机不会自动重算，会停留在初始尺寸导致网页只显示
   *  约 1/5 高度；故按视口实测尺寸显式赋值 px。
   *
   *  S5 追加两件事：
   *   ① 顺带把视口尺寸交给 ElementInspector（此前它在 render 期直接读 clientWidth，
   *      布局 dirty 时会升级为强制同步布局）——同一次读取两处共用；
   *   ② 整个函数在运动期（PerfBus.isBusy）**不执行**：这些布局读在拖面板/拖窗口时
   *      每帧都会经 RO 触发，是“面板一拖就卡”的组成之一。运动中只记账，结束后补一次。
   */
  const viewportSizeRef = useRef<{ width: number; height: number } | null>(null);
  const [viewportSize, setViewportSize] = useState<{ width: number; height: number } | null>(null);
  const syncFrameSize = useCallback(() => {
    const vp = viewportRef.current;
    if (!vp) return;
    const w = vp.clientWidth;
    const h = vp.clientHeight;
    if (w <= 0 || h <= 0) return;
    // 视口尺寸缓存（供 ElementInspector 用；尺寸未变不触发重渲染）
    const prev = viewportSizeRef.current;
    if (!prev || prev.width !== w || prev.height !== h) {
      viewportSizeRef.current = { width: w, height: h };
      setViewportSize({ width: w, height: h });
    }
    // 获取当前视口内全部 webview 与 iframe 元素统一设置尺寸
    const elements: HTMLElement[] = Array.from(vp.querySelectorAll("webview, iframe"));
    const fromRef = tabFrameRefs.current.get(activeTabId);
    if (fromRef && !elements.includes(fromRef)) elements.push(fromRef);
    for (const el of elements) {
      if (!el || !el.style) continue;
      el.style.width = `${w}px`;
      el.style.height = `${h}px`;
    }
  }, [activeTabId]);

  // syncFrameSize 在 ref 回调/事件监听里使用，用 ref 持有最新实现，避免为它重建 ref 回调
  const syncFrameSizeRef = useRef(syncFrameSize);
  useEffect(() => { syncFrameSizeRef.current = syncFrameSize; }, [syncFrameSize]);

  /**
   * 取得某标签页宿主元素的稳定 ref 回调。
   *
   * 为什么必须稳定：React 每次渲染都会以 null → 元素 的顺序重放内联 ref 回调，
   * 内联写法会在窗口 resize 等无关重渲染时反复解绑/重绑事件（并触发同步调用）。
   * 这里按 tabId 缓存回调，只在元素真正挂载/卸载时执行绑定逻辑。
   */
  const getTabRefCallback = useCallback(
    (tabId: string) => {
      const bindings = tabBindingsRef.current;
      let binding = bindings.get(tabId);
      if (!binding) {
        binding = {
          ref: (el: FrameEl | null) => {
            const b = bindings.get(tabId);
            if (!b) return;
            if (el) {
              if (b.el === el) return; // 同一元素重复回调：无需重复绑定
              b.el = el;
              tabFrameRefs.current.set(tabId, el);

              // guest 就绪（webview: dom-ready / iframe: load）后再算尺寸并放行挂起调用。
              // 保留 handler 引用，卸载时才能精确 removeEventListener（markReady 幂等，重复触发无副作用）。
              const handler = () => {
                frameBridge.markReady(tabId, el);
                syncFrameSizeRef.current();
              };
              b.handler = handler;
              if (el.tagName === "WEBVIEW") {
                el.addEventListener?.("dom-ready", handler);
                el.addEventListener?.("did-finish-load", handler);
              } else {
                el.addEventListener?.("load", handler);
              }
              // 元素可能已就绪（面板重挂载时 dom-ready 早于 ref 回调，或 iframe 已 complete）
              frameBridge.attach(tabId, el);
              frameBridge.markReadyIfAttached(tabId, el);
              requestAnimationFrame(syncFrameSizeRef.current);
            } else {
              const prev = b.el;
              b.el = undefined;
              if (prev && b.handler) {
                if (prev.tagName === "WEBVIEW") {
                  prev.removeEventListener?.("dom-ready", b.handler);
                  prev.removeEventListener?.("did-finish-load", b.handler);
                } else {
                  prev.removeEventListener?.("load", b.handler);
                }
                b.handler = undefined;
              }
              tabFrameRefs.current.delete(tabId);
              frameBridge.detach(tabId);
            }
          },
        };
        bindings.set(tabId, binding);
      }
      return binding.ref;
    },
    [frameBridge],
  );

  // 面板卸载（折叠右面板 / 切换会话）时清空宿主绑定，防止下次挂载复用失效回调，
  // 同时作废所有挂起中的 guest 调用（否则它们会在面板已折叠后补执行而抛错）
  useEffect(() => {
    const bindings = tabBindingsRef.current;
    return () => {
      for (const tabId of Array.from(bindings.keys())) frameBridge.detach(tabId);
      bindings.clear();
      tabFrameRefs.current.clear();
    };
  }, [frameBridge]);

  // 视口尺寸变化（面板宽度拖拽、窗口缩放、面板折叠恢复）时重算框架尺寸。
  // plan-329-1647 S5：运动期只记账不执行（见 syncFrameSize 注释），
  // 运动结束由 PerfBus 订阅补一次——终态一次即可，中间帧的框架尺寸没人看得到。
  useEffect(() => {
    const vp = viewportRef.current;
    if (!vp) return;
    let raf = 0;
    let missed = false;
    const schedule = () => {
      if (isBusy()) { missed = true; return; } // 运动期：不做布局读
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => { missed = false; syncFrameSize(); });
    };
    schedule();
    // RFL-6（S6）：注册进唯一收敛序列——order 60 浏览器框架尺寸。
    // 与此前新增的 PerfBus 订阅互补：订阅负责「运动结束时补一次」，
    // 收敛序列则把它排进统一的同帧序列（在终端 fit 之后、输入框 remeasure 之前）。
    const offReconcile = registerReconcileTask("browser-frame-size", RECONCILE_ORDER.browserFrameSize,
      "浏览器框架尺寸", () => syncFrameSize());
    let ro: ResizeObserver | null = null;
    if (typeof ResizeObserver !== "undefined") {
      ro = new ResizeObserver(schedule);
      ro.observe(vp);
    }
    window.addEventListener("resize", schedule);
    // 运动结束补一次：PerfBus 只在 busy 翻转时回调，不会引入逐帧开销。
    const off = subscribe((busy) => { if (!busy && missed) schedule(); });
    return () => {
      offReconcile();
      cancelAnimationFrame(raf);
      ro?.disconnect();
      window.removeEventListener("resize", schedule);
      off();
    };
  }, [syncFrameSize]);

  // 标签切换 / 视图切换（dom/console/preview）/ 标签增减 / 标注模式切换后重算，
  // 覆盖 wrapper 由 display:none 恢复显示的时机（此时需要延后一帧等布局完成）。
  useEffect(() => {
    const raf = requestAnimationFrame(syncFrameSize);
    return () => cancelAnimationFrame(raf);
  }, [syncFrameSize, activeTabId, tabView, tabs.length, selecting]);

  // 仅在切换标签页或页面真实导航改变时同步当前激活标签的 URL 到输入框
  useEffect(() => {
    const curTabId = activeTab?.id;
    const curUrl = activeTab?.url || activeTab?.current || "";
    if (lastSyncedRef.current.tabId !== curTabId || lastSyncedRef.current.current !== curUrl) {
      lastSyncedRef.current = { tabId: curTabId, current: curUrl };
      setInputUrl(curUrl);
    }
  }, [activeTab?.id, activeTab?.url, activeTab?.current]);

  const showToast = (msg: string) => {
    setSentToast(msg);
    setTimeout(() => setSentToast(null), 3000);
  };

  const toggleSelect = () => {
    const next = !selecting;
    updateSessionState(activeSessionId, { selecting: next });
    setAnnotState(null);
    setHoverInfo(null);
    setCursorPos(null);
  };

  // Esc 退出选择模式
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

  // 从当前活动页面的 DOM 节点提取 DevTools 风格元素信息
  const extractElementInfo = (el: HTMLElement): ElementInfo => {
    const rect = el.getBoundingClientRect();
    const cs = window.getComputedStyle(el);
    const cls = Array.from(el.classList || []).slice(0, 6).join(" ");
    const id = el.id || "";
    const innerText = (el.textContent || "").trim().replace(/\s+/g, " ").slice(0, 100);
    return {
      tag: el.tagName.toLowerCase(),
      id,
      className: cls,
      width: Math.round(rect.width),
      height: Math.round(rect.height),
      x: Math.round(rect.x),
      y: Math.round(rect.y),
      display: cs.display,
      position: cs.position,
      color: cs.color,
      backgroundColor: cs.backgroundColor,
      fontSize: cs.fontSize,
      padding: `${cs.paddingTop} ${cs.paddingRight} ${cs.paddingBottom} ${cs.paddingLeft}`,
      margin: `${cs.marginTop} ${cs.marginRight} ${cs.marginBottom} ${cs.marginLeft}`,
      text: innerText,
      role: el.getAttribute("role") || undefined,
      ariaLabel: el.getAttribute("aria-label") || undefined,
      placeholder: (el as HTMLInputElement).placeholder || undefined,
    };
  };

  // ── 标注探针（注入 webview guest 页面，宿主通过 executeJavaScript 查询光标处元素）──
  // 旧方案缺陷：覆盖层拦截鼠标后 guest 侧 IPC 回传是空壳，且坐标使用覆盖层坐标系导致选不准。
  // 新方案：覆盖层只负责追踪光标位置，元素命中判定在 guest 内部由探针完成，坐标天然一致。
  const INSPECT_PROBE_SRC = `(() => {
    if (window.__ccInspect) { window.__ccInspect.enabled = true; return; }
    function buildInfo(el) {
      const r = el.getBoundingClientRect();
      const s = window.getComputedStyle(el);
      return {
        tag: el.tagName.toLowerCase(),
        id: el.id || '',
        className: el.className ? (typeof el.className === 'string' ? el.className : (el.className.baseVal || '')) : '',
        width: Math.round(r.width),
        height: Math.round(r.height),
        x: Math.round(r.left),
        y: Math.round(r.top),
        display: s.display,
        position: s.position,
        color: s.color,
        backgroundColor: s.backgroundColor,
        fontSize: s.fontSize,
        padding: s.padding,
        margin: s.margin,
        text: (el.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 100),
        role: el.getAttribute('role') || undefined,
        ariaLabel: el.getAttribute('aria-label') || undefined,
        placeholder: el.placeholder || undefined
      };
    }
    window.__ccInspect = {
      enabled: true,
      infoAt(x, y) {
        if (!window.__ccInspect.enabled) return null;
        const el = document.elementFromPoint(x, y);
        if (!el || el === document.documentElement || el === document.body) return null;
        const r = el.getBoundingClientRect();
        return {
          rect: { x: Math.round(r.left), y: Math.round(r.top), width: Math.round(r.width), height: Math.round(r.height) },
          info: buildInfo(el)
        };
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

  // selecting 异步回调中需要读到最新值（executeJavaScript 返回时可能已退出标注模式）
  const selectingRef = useRef(selecting);
  useEffect(() => { selectingRef.current = selecting; }, [selecting]);
  const hoverQueryRef = useRef({ pending: false });

  // 开启标注（或页面导航）时注入/激活探针；退出时置 disabled
  //
  // 全部走 frameBridge：面板折叠后 BrowserPanel 被卸载、webview 随之脱离 DOM，
  // 但 cleanup 与重新挂载的 effect 都会立即执行到 executeJavaScript。
  // 此前直接调用会在未 attach/未 dom-ready 时**同步抛错**，被顶层 ErrorBoundary
  // 接住导致整页白屏（见 logs: "The WebView must be attached to the DOM..."）。
  useEffect(() => {
    if (!selecting) return;
    // waitReady：面板刚展开、guest 尚未 dom-ready 时先挂起，就绪后自动补注入
    void frameBridge.invoke(activeTabId, INSPECT_PROBE_SRC, { waitReady: true });
    return () => {
      // 退出标注时关闭探针；此时 guest 若已不可达就直接丢弃（无副作用价值）
      void frameBridge.invoke(activeTabId, "window.__ccInspect && (window.__ccInspect.enabled = false)", { waitReady: false });
    };
    // 依赖 tab.current：页面导航会重置 guest 上下文，需要重新注入
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selecting, activeTabId, activeTab?.current, frameBridge]);

  // 鼠标移动：光标追踪（同步）+ 元素命中查询（webview 异步节流 / 同源 iframe 同步直查）
  const handleOverlayMouseMove = (e: React.MouseEvent) => {
    if (!selectingRef.current || !viewportRef.current) return;
    const vpRect = viewportRef.current.getBoundingClientRect();
    const x = e.clientX - vpRect.left;
    const y = e.clientY - vpRect.top;
    setCursorPos({ x, y });

    const frame = tabFrameRefs.current.get(activeTabId);
    if (!frame) { setHoverInfo(null); return; }

    if (isGuestFrame(frame)) {
      // Electron webview：节流异步查询，仅保留最新一次在途请求，返回时丢弃过期结果。
      // waitReady=false：悬停是即时交互，未就绪就跳过本次（重挂载后由就绪唤醒处理）。
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
      // 同源 iframe：同步直查 contentDocument
      try {
        const doc = frame.contentDocument;
        if (!doc) { setHoverInfo(null); return; }
        const fr = frame.getBoundingClientRect();
        const el = doc.elementFromPoint(x - (fr.left - vpRect.left), y - (fr.top - vpRect.top)) as HTMLElement | null;
        if (el && el !== doc.body && el !== doc.documentElement) {
          const elRect = el.getBoundingClientRect();
          setHoverInfo({
            rect: {
              x: elRect.left + (fr.left - vpRect.left),
              y: elRect.top + (fr.top - vpRect.top),
              width: elRect.width,
              height: elRect.height,
            },
            info: extractElementInfo(el),
          });
        } else {
          setHoverInfo(null);
        }
      } catch {
        // 跨域 iframe：contentDocument 不可达，无法高亮
        setHoverInfo(null);
      }
    }
  };

  // 点击选择元素：webview 用探针 pickAt 精确取 guest 内元素（坐标即 webview 内部坐标），
  // 同源 iframe 直查 contentDocument，均失败时降级为页面坐标占位
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
        const res = await frameBridge.call<any>(
          activeTabId,
          `window.__ccInspect ? __ccInspect.pickAt(${Math.round(localX)}, ${Math.round(localY)}) : null`,
          { waitReady: true },
        );
        if (res) {
          source = res.source || "";
          info = res.info || null;
        }
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
          if (el) {
            source = el.outerHTML.slice(0, 800);
            info = extractElementInfo(el);
          }
        }
      } catch {}
    }

    if (!source) source = `页面坐标 (${Math.round(localX)}, ${Math.round(localY)})`;

    // 捕获当前页面截图作为标注凭据（桥接层保证未就绪时不抛错，降级到主进程 IPC）
    let screenshotDataUrl = "";
    if (isGuestFrame(currentFrame)) {
      screenshotDataUrl = await frameBridge.capture(activeTabId);
    }
    if (!screenshotDataUrl && window.chatcoderAPI?.captureBrowserPage) {
      try {
        screenshotDataUrl = (await window.chatcoderAPI.captureBrowserPage()) || "";
      } catch {
        /* 主进程截图不可用时忽略：标注卡仍可携带元素信息 */
      }
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

  // 截屏并一键上传为真实图片附件 + 结构化引用卡
  const handleCaptureToComposer = async () => {
    const currentFrame = tabFrameRefs.current.get(activeTabId);
    if (!currentFrame && !window.chatcoderAPI?.captureBrowserPage) return;
    setCapturing(true);

    try {
      // 截屏走桥接：webview 未就绪（面板刚展开/dom-ready 未触发）时返回空串而非抛错
      let dataUrl = "";
      if (isGuestFrame(currentFrame)) dataUrl = await frameBridge.capture(activeTabId);
      if (!dataUrl && window.chatcoderAPI?.captureBrowserPage) {
        dataUrl = (await window.chatcoderAPI.captureBrowserPage()) || "";
      }

      if (!dataUrl) {
        showToast("截屏失败：未获取到图像数据");
        setCapturing(false);
        return;
      }

      // Base64 DataURL 转 File 对象并上传到后端
      const res = await fetch(dataUrl);
      const blob = await res.blob();
      const filename = `browser-shot-${Date.now().toString().slice(-6)}.png`;
      const file = new File([blob], filename, { type: "image/png" });

      const uploaded = await api.uploadFile(file);

      // 截图仅作为截图引用卡的缩略图，不再单独挂附件
      addComposerBrowserRef({
        id: `ref-${Date.now()}`,
        kind: "screenshot",
        pageTitle: activeTab?.title || "网页截图",
        url: activeTab?.current || activeTab?.url || "about:blank",
        thumbUrl: uploaded.url,
        // 保存完整上传结果，发送时转换为消息附件（仅 thumbUrl 会导致 AI 收不到图片）
        attachment: {
          file_id: uploaded.file_id,
          filename: uploaded.filename,
          path: uploaded.path,
          url: uploaded.url,
          size: uploaded.size,
          mime_type: uploaded.mime_type,
          type: uploaded.type,
        },
        createdAt: Date.now(),
      });

      showToast("已截屏并添加到输入框标注块");
    } catch (e: any) {
      showToast(`截屏上传失败: ${e.message || e}`);
    } finally {
      setCapturing(false);
    }
  };

  // 确认发送当前标注到聊天输入框（v7：不注入 textarea 文字、不独立挂附件，
  // 截图/元素信息/备注全部收进一张内嵌标注块卡片）
  const handleSendAnnotToChat = async () => {
    if (!annotState) return;
    let shot: UploadOut | null = null;

    if (annotState.screenshotDataUrl) {
      try {
        const res = await fetch(annotState.screenshotDataUrl);
        const blob = await res.blob();
        const filename = `annot-${Date.now().toString().slice(-6)}.png`;
        const file = new File([blob], filename, { type: "image/png" });
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
      url: activeTab?.current || activeTab?.url || "about:blank",
      selector: selectorStr,
      bbox: inf ? { width: inf.width, height: inf.height, x: inf.x, y: inf.y } : undefined,
      styleDigest: inf ? `color: ${inf.color}; bg: ${inf.backgroundColor}; font: ${inf.fontSize}` : undefined,
      text: inf?.text || undefined,
      note: annotText.trim() || undefined,
      thumbUrl: shot?.url || undefined,
      // 保存完整上传结果，发送时转换为消息附件（仅 thumbUrl 会导致 AI 收不到图片）
      attachment: shot
        ? {
            file_id: shot.file_id,
            filename: shot.filename,
            path: shot.path,
            url: shot.url,
            size: shot.size,
            mime_type: shot.mime_type,
            type: shot.type,
          }
        : undefined,
      createdAt: Date.now(),
    });

    showToast("已添加标注块到输入框");
    updateSessionState(activeSessionId, { selecting: false });
    setAnnotState(null);
    setAnnotText("");
  };

  const openDevTools = async () => {
    try {
      // 桥接层在 guest 未就绪时返回 false，降级到主进程 IPC（避免 executeJavaScript/capturePage 同类抛错）
      if (frameBridge.openDevTools(activeTabId)) return;
      if (window.chatcoderAPI?.openBrowserDevTools) {
        await window.chatcoderAPI.openBrowserDevTools();
      }
    } catch (e) {
      console.warn("openDevTools error:", e);
    }
  };

  return (
    <div className="browser-panel">
      {/* 多标签页导航条 */}
      <div className="browser-tabs-bar">
        <div className="browser-tabs-list">
          {tabs.map((tab) => {
            const isActive = tab.id === activeTabId;
            return (
              <div
                key={tab.id}
                className={`browser-tab-item${isActive ? " active" : ""}`}
                onClick={() => setActiveTab(activeSessionId, tab.id)}
                title={tab.title || tab.url || "新标签页"}
              >
                <IconGlobe size={11} className="browser-tab-icon" />
                <span className="browser-tab-title">{tab.title || "新标签页"}</span>
                {tabs.length > 1 && (
                  <button
                    className="browser-tab-close"
                    onClick={(e) => {
                      e.stopPropagation();
                      closeTab(activeSessionId, tab.id);
                    }}
                    title="关闭标签页"
                  >
                    <IconX size={10} />
                  </button>
                )}
              </div>
            );
          })}
        </div>
        <button
          className="browser-new-tab-btn"
          onClick={() => newTab(activeSessionId)}
          title="新建标签页"
        >
          <IconPlus size={13} />
        </button>
      </div>

      {/* 浏览器核心工具栏 */}
      <div className="browser-toolbar">
        <button
          className="browser-btn"
          onClick={() => goBack(activeSessionId, activeTabId)}
          disabled={!activeTab || activeTab.hIdx <= 0}
          title="后退"
        >
          <IconArrowLeft size={13} />
        </button>
        <button
          className="browser-btn"
          onClick={() => goForward(activeSessionId, activeTabId)}
          disabled={!activeTab || activeTab.hIdx >= activeTab.history.length - 1}
          title="前进"
        >
          <IconArrowRight size={13} />
        </button>
        <button
          className="browser-btn"
          onClick={() => navigate(activeSessionId, activeTab?.current || "", activeTabId)}
          disabled={!activeTab?.current}
          title="刷新"
        >
          <IconRefresh size={13} />
        </button>

        <div className="browser-url">
          <IconGlobe size={12} />
          <input
            value={inputUrl}
            onChange={(e) => setInputUrl(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                navigate(activeSessionId, inputUrl, activeTabId);
                e.currentTarget.blur();
              }
            }}
            placeholder="输入网址（如 localhost:5173 或 https://example.com）"
            spellCheck={false}
            autoCapitalize="none"
            autoCorrect="off"
          />
        </div>

        <button
          className={`browser-btn${tabView === "preview" ? " active" : ""}`}
          onClick={() => updateSessionState(activeSessionId, { tabView: "preview" })}
          title="网页视图"
        >
          <IconGlobe size={13} />
        </button>
        <button
          className={`browser-btn${tabView === "dom" ? " active" : ""}`}
          onClick={() => updateSessionState(activeSessionId, { tabView: "dom" })}
          title="DOM 快照"
        >
          <IconCode size={13} />
        </button>
        <button
          className={`browser-btn${tabView === "console" ? " active" : ""}`}
          onClick={() => updateSessionState(activeSessionId, { tabView: "console" })}
          title="控制台"
        >
          <IconTerminal size={13} />
        </button>
        <button
          className={`browser-btn${selecting ? " active" : ""}`}
          onClick={toggleSelect}
          title={selecting ? "退出标注模式 (Esc)" : "选择并标注网页元素"}
        >
          <IconTarget size={13} />
        </button>
        <button
          className="browser-btn"
          onClick={handleCaptureToComposer}
          disabled={capturing || !activeTab?.current}
          title="截图并添加到聊天输入框"
        >
          <IconImage size={13} />
        </button>
        <button className="browser-btn" onClick={openDevTools} title="原生开发者工具 (F12)">
          <IconBug size={13} />
        </button>
      </div>

      {/* 提示条 / AI 镜像操作动态条 */}
      {sentToast && (
        <div className="browser-toast-bar">
          {sentToast}
        </div>
      )}

      {browserState.mirrorActivity && (
        <div className="browser-mirror-banner">
          <span className="browser-mirror-dot" />
          <span>AI 正在操作页面：{browserState.mirrorActivity.text || browserState.mirrorActivity.action}</span>
        </div>
      )}

      {/* 浏览器视口容器 */}
      <div
        className="browser-viewport"
        ref={viewportRef}
        style={{ cursor: selecting ? "crosshair" : "default" }}
      >
        {/* 多标签保活挂载（全部保存在 DOM 中，用 display 切换） */}
        {tabs.map((tab) => {
          const isCurrentActive = tab.id === activeTabId;
          const hasUrl = Boolean(tab.current && tab.current !== "about:blank");

          return (
            <div
              key={tab.id}
              className={`browser-tab-frame-wrapper${isCurrentActive && tabView === "preview" ? " active" : " hidden"}`}
              style={{
                display: isCurrentActive && tabView === "preview" ? "block" : "none",
              }}
            >
              {!hasUrl ? (
                <BrowserStartPage
                  onNavigate={(url) => navigate(activeSessionId, url, tab.id)}
                />
              ) : isElectron ? (
                <webview
                  ref={getTabRefCallback(tab.id)}
                  src={tab.current}
                  className="browser-frame-element"
                />
              ) : (
                <iframe
                  ref={getTabRefCallback(tab.id)}
                  src={tab.current}
                  className="browser-frame-element"
                  sandbox="allow-scripts allow-same-origin allow-forms allow-popups"
                  title={`browser-${tab.id}`}
                />
              )}
            </div>
          );
        })}

        {/* DOM 快照视图 */}
        {tabView === "dom" && (
          <div className="browser-subview browser-dom-view">
            <div className="browser-subview-header">
              <span className="browser-subview-title">DOM 结构快照 ({activeTab?.title || "当前页面"})</span>
              <button
                className="btn btn-ghost btn-sm"
                onClick={async () => {
                  let snapshot = "";
                  const currentFrame = tabFrameRefs.current.get(activeTabId);
                  try {
                    if (isGuestFrame(currentFrame)) {
                      snapshot = (await frameBridge.call<string>(activeTabId, "document.documentElement.outerHTML", { waitReady: true })) || "";
                    } else if (currentFrame?.contentDocument) {
                      snapshot = currentFrame.contentDocument.documentElement.outerHTML;
                    }
                  } catch {}
                  const truncated = (snapshot || `[页面: ${activeTab?.current}]`).substring(0, 4000);
                  updateSessionState(activeSessionId, { domSnapshot: truncated });

                  addComposerBrowserRef({
                    id: `ref-${Date.now()}`,
                    kind: "dom",
                    pageTitle: activeTab?.title || "DOM 快照",
                    url: activeTab?.current || "about:blank",
                    text: truncated.slice(0, 200) + "...",
                    createdAt: Date.now(),
                  });
                  showToast("已发送 DOM 快照到聊天框");
                }}
              >
                发送快照到输入框
              </button>
            </div>
            <pre className="browser-code-pre">{domSnapshot || "暂无快照，点击上方按钮捕获当前页面 DOM"}</pre>
          </div>
        )}

        {/* 控制台视图 */}
        {tabView === "console" && (
          <div className="browser-subview browser-console-view">
            <div className="browser-subview-header">
              <span className="browser-subview-title">控制台与 JS 求值</span>
            </div>
            <div className="browser-console-tip">输入 JavaScript 表达式按 Enter 快速执行并捕获结果：</div>
            <div className="browser-console-input-row">
              <input
                className="browser-console-input"
                placeholder="例如: document.title 或 location.href"
                onKeyDown={async (e) => {
                  if (e.key === "Enter") {
                    const code = (e.target as HTMLInputElement).value;
                    if (!code) return;
                    let res = "";
                    const currentFrame = tabFrameRefs.current.get(activeTabId);
                    if (isGuestFrame(currentFrame)) {
                      // 用 invoke 而非 call：需要把 guest 内的求值异常回显到结果里
                      const r = await frameBridge.invoke<string>(activeTabId, code, { waitReady: true });
                      if (r.ok) res = String(r.value);
                      else if (r.notReady) res = "Error: 页面尚未就绪，请稍后重试";
                      else res = `Error: ${r.error}`;
                    }

                    addComposerBrowserRef({
                      id: `ref-${Date.now()}`,
                      kind: "console",
                      pageTitle: activeTab?.title || "控制台求值",
                      url: activeTab?.current || "about:blank",
                      text: `> ${code}\n${res}`,
                      createdAt: Date.now(),
                    });
                    showToast("已发送求值结果到输入框");
                  }
                }}
              />
            </div>
          </div>
        )}

        {/* 标注覆盖层与 DevTools 悬停检查器。
            覆盖层仅拦截光标事件用于定位；元素命中由页面内探针/同源直查完成（见 handleOverlayMouseMove）。 */}
        {selecting && (
          <div
            className="browser-annot-overlay active"
            onMouseMove={handleOverlayMouseMove}
            onClick={handleOverlayClick}
            onMouseLeave={() => setHoverInfo(null)}
          >
            <div className="browser-annot-tip">
              移动鼠标聚焦元素，点击添加标注 · 按 Esc 退出
            </div>

            {/* 用缓存的视口尺寸（S5）：不在 render 期读 clientWidth，避免强制同步布局。
                缓存由 syncFrameSize 在同一处更新，且运动期不更新（运动中不刷新悬停卡）。 */}
            <ElementInspector
              hoverInfo={hoverInfo}
              cursorPos={cursorPos}
              containerRect={viewportSize ?? undefined}
            />
          </div>
        )}

        {/* 选中元素标注卡 */}
        {annotState && (
          <div
            className="browser-annot-card"
            style={{
              position: "absolute",
              left: annotState.x,
              top: annotState.y,
            }}
          >
            <div className="browser-annot-card-header">
              <span className="browser-annot-card-title">已选中元素，添加说明后发送：</span>
              <button
                className="browser-annot-card-close"
                onClick={() => {
                  setAnnotState(null);
                  setAnnotText("");
                }}
              >
                <IconX size={12} />
              </button>
            </div>

            <div className="browser-annot-card-source">
              {annotState.source.substring(0, 160)}
            </div>

            {annotState.info && (
              <div className="browser-annot-info">
                <div className="browser-annot-info-title">
                  &lt;{annotState.info.tag}&gt;
                  {annotState.info.id && <span className="browser-annot-info-id">#{annotState.info.id}</span>}
                  {annotState.info.className && (
                    <span className="browser-annot-info-cls">
                      .{annotState.info.className.split(" ").filter(Boolean).slice(0, 3).join(".")}
                    </span>
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
              <button
                className="btn btn-ghost btn-sm"
                onClick={() => {
                  updateSessionState(activeSessionId, { selecting: false });
                  setAnnotState(null);
                  setAnnotText("");
                }}
              >
                完成
              </button>
              <button className="btn btn-primary btn-sm" onClick={handleSendAnnotToChat}>
                <IconArrowUp size={12} /> 发送到输入框
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
