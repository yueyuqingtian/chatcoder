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
import { api } from "../../api/client";
import { ElementInspector } from "./ElementInspector";
import { BrowserStartPage } from "./BrowserStartPage";
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

  // 记录上一次已同步的 tab 与 URL，避免用户输入中被 re-render 冲掉
  const lastSyncedRef = useRef<{ tabId?: string; current?: string }>({});

  /**
   * 视口内框架尺寸兜底：webview（Electron guest 宿主）与 iframe 仅靠 CSS 百分比
   * 在面板折叠恢复、全屏切换、标签切换、窗口 resize 等时机不会自动重算，
   * 会停留在初始尺寸导致网页只显示约 1/5 高度。此处按视口实测尺寸显式赋值 px。
   */
  const syncFrameSize = useCallback(() => {
    const vp = viewportRef.current;
    if (!vp) return;
    const w = vp.clientWidth;
    const h = vp.clientHeight;
    if (w <= 0 || h <= 0) return;
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

  // 视口尺寸变化（面板宽度拖拽、窗口缩放、面板折叠恢复）时重算框架尺寸
  useEffect(() => {
    const vp = viewportRef.current;
    if (!vp) return;
    let raf = 0;
    const schedule = () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(syncFrameSize);
    };
    schedule();
    let ro: ResizeObserver | null = null;
    if (typeof ResizeObserver !== "undefined") {
      ro = new ResizeObserver(schedule);
      ro.observe(vp);
    }
    window.addEventListener("resize", schedule);
    return () => {
      cancelAnimationFrame(raf);
      ro?.disconnect();
      window.removeEventListener("resize", schedule);
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
  useEffect(() => {
    if (!selecting) return;
    const frame = tabFrameRefs.current.get(activeTabId);
    if (frame && typeof frame.executeJavaScript === "function") {
      frame.executeJavaScript(INSPECT_PROBE_SRC).catch(() => {});
    }
    return () => {
      const f = tabFrameRefs.current.get(activeTabId);
      if (f && typeof f.executeJavaScript === "function") {
        f.executeJavaScript("window.__ccInspect && (window.__ccInspect.enabled = false)").catch(() => {});
      }
    };
    // 依赖 tab.current：页面导航会重置 guest 上下文，需要重新注入
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selecting, activeTabId, activeTab?.current]);

  // 鼠标移动：光标追踪（同步）+ 元素命中查询（webview 异步节流 / 同源 iframe 同步直查）
  const handleOverlayMouseMove = (e: React.MouseEvent) => {
    if (!selectingRef.current || !viewportRef.current) return;
    const vpRect = viewportRef.current.getBoundingClientRect();
    const x = e.clientX - vpRect.left;
    const y = e.clientY - vpRect.top;
    setCursorPos({ x, y });

    const frame = tabFrameRefs.current.get(activeTabId);
    if (!frame) { setHoverInfo(null); return; }

    if (typeof frame.executeJavaScript === "function") {
      // Electron webview：节流异步查询，仅保留最新一次在途请求，返回时丢弃过期结果
      const q = hoverQueryRef.current;
      if (q.pending) return;
      q.pending = true;
      frame.executeJavaScript(`window.__ccInspect ? __ccInspect.infoAt(${Math.round(x)}, ${Math.round(y)}) : null`)
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

    if (currentFrame && typeof currentFrame.executeJavaScript === "function") {
      try {
        const res = await currentFrame.executeJavaScript(
          `window.__ccInspect ? __ccInspect.pickAt(${Math.round(localX)}, ${Math.round(localY)}) : null`
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

    // 捕获当前页面截图作为标注凭据
    let screenshotDataUrl = "";
    try {
      if (currentFrame && typeof currentFrame.capturePage === "function") {
        const img = await currentFrame.capturePage();
        screenshotDataUrl = img?.toDataURL() || "";
      } else if (window.chatcoderAPI?.captureBrowserPage) {
        screenshotDataUrl = (await window.chatcoderAPI.captureBrowserPage()) || "";
      }
    } catch {}

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
      let dataUrl = "";
      if (currentFrame && typeof currentFrame.capturePage === "function") {
        const img = await currentFrame.capturePage();
        dataUrl = img?.toDataURL() || "";
      } else if (window.chatcoderAPI?.captureBrowserPage) {
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
    let shotUrl = "";

    if (annotState.screenshotDataUrl) {
      try {
        const res = await fetch(annotState.screenshotDataUrl);
        const blob = await res.blob();
        const filename = `annot-${Date.now().toString().slice(-6)}.png`;
        const file = new File([blob], filename, { type: "image/png" });
        const uploaded = await api.uploadFile(file);
        shotUrl = uploaded.url;
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
      thumbUrl: shotUrl || undefined,
      createdAt: Date.now(),
    });

    showToast("已添加标注块到输入框");
    updateSessionState(activeSessionId, { selecting: false });
    setAnnotState(null);
    setAnnotText("");
  };

  const openDevTools = async () => {
    try {
      const currentFrame = tabFrameRefs.current.get(activeTabId);
      if (currentFrame && typeof currentFrame.openDevTools === "function") {
        currentFrame.openDevTools();
        return;
      }
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
                  ref={(el: any) => {
                    if (el) {
                      tabFrameRefs.current.set(tab.id, el);
                      el.addEventListener?.("dom-ready", syncFrameSize);
                      el.addEventListener?.("did-finish-load", syncFrameSize);
                      requestAnimationFrame(syncFrameSize);
                    } else {
                      const prev = tabFrameRefs.current.get(tab.id);
                      prev?.removeEventListener?.("dom-ready", syncFrameSize);
                      prev?.removeEventListener?.("did-finish-load", syncFrameSize);
                      tabFrameRefs.current.delete(tab.id);
                    }
                  }}
                  src={tab.current}
                  className="browser-frame-element"
                />
              ) : (
                <iframe
                  ref={(el) => {
                    if (el) {
                      tabFrameRefs.current.set(tab.id, el);
                      el.addEventListener?.("load", syncFrameSize);
                      requestAnimationFrame(syncFrameSize);
                    } else {
                      const prev = tabFrameRefs.current.get(tab.id);
                      prev?.removeEventListener?.("load", syncFrameSize);
                      tabFrameRefs.current.delete(tab.id);
                    }
                  }}
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
                    if (currentFrame && typeof currentFrame.executeJavaScript === "function") {
                      snapshot = await currentFrame.executeJavaScript("document.documentElement.outerHTML");
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
                    try {
                      if (currentFrame && typeof currentFrame.executeJavaScript === "function") {
                        res = String(await currentFrame.executeJavaScript(code));
                      }
                    } catch (err: any) {
                      res = `Error: ${err.message || err}`;
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

            <ElementInspector
              hoverInfo={hoverInfo}
              cursorPos={cursorPos}
              containerRect={viewportRef.current ? {
                width: viewportRef.current.clientWidth,
                height: viewportRef.current.clientHeight,
              } : undefined}
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
