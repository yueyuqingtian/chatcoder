/** 浏览器面板（v5）：iframe + 地址栏 + 元素标注 + 发送到主对话。
 * 标注模式：overlay 拦截鼠标，移动时实时高亮 iframe 内元素，
 * 点击后弹出标注卡，保持选择模式可连续标注，Esc 退出。
 * 修复：overlay 需 inset:0 撑满；坐标用 clientX/clientY 直接传 elementFromPoint；
 *       跨域 iframe 降级为坐标标注。
 */
import { useEffect, useRef, useState } from "react";
import { useChatStore } from "../../store/chat";
import { IconArrowLeft, IconArrowRight, IconRefresh, IconGlobe, IconTarget, IconArrowUp, IconX } from "../icons";

/** 选中元素的 devtools 风格信息。 */
interface ElementInfo {
  tag: string;
  id: string;
  className: string;
  width: number;
  height: number;
  x: number;
  y: number;
  display: string;
  position: string;
  color: string;
  background: string;
  fontSize: string;
  padding: string;
  margin: string;
  text: string;
}

export function BrowserPanel() {
  const [url, setUrl] = useState("https://example.com");
  const [current, setCurrent] = useState("https://example.com");
  const [history, setHistory] = useState<string[]>(["https://example.com"]);
  const [hIdx, setHIdx] = useState(0);
  const [selecting, setSelecting] = useState(false);
  const [annotState, setAnnotState] = useState<{ x: number; y: number; source: string; info: ElementInfo | null } | null>(null);
  const [annotText, setAnnotText] = useState("");
  const [sentMsg, setSentMsg] = useState(false);
  // 悬停元素的光标跟随标签（devtools 风格：<tag> W×H）
  const [hoverTag, setHoverTag] = useState<{ x: number; y: number; text: string } | null>(null);

  // 选中元素的 devtools 风格信息
  const getElementInfo = (el: HTMLElement): ElementInfo => {
    const rect = el.getBoundingClientRect();
    const cs = window.getComputedStyle(el);
    const cls = Array.from(el.classList || []).slice(0, 8).join(" ");
    const id = el.id || "";
    const innerText = (el.textContent || "").trim().replace(/\s+/g, " ").slice(0, 120);
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
      background: cs.backgroundColor,
      fontSize: cs.fontSize,
      padding: `${cs.paddingTop} ${cs.paddingRight} ${cs.paddingBottom} ${cs.paddingLeft}`,
      margin: `${cs.marginTop} ${cs.marginRight} ${cs.marginBottom} ${cs.marginLeft}`,
      text: innerText,
    };
  };
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const viewportRef = useRef<HTMLDivElement>(null);
  const setComposerDraft = useChatStore((s) => s.setComposerDraft);

  const navigate = (target: string) => {
    let t = target.trim();
    if (!t) return;
    if (!/^https?:\/\//i.test(t)) t = `https://${t}`;
    const next = [...history.slice(0, hIdx + 1), t];
    setHistory(next); setHIdx(next.length - 1); setCurrent(t); setUrl(t);
  };
  const goBack = () => { if (hIdx <= 0) return; setHIdx(hIdx - 1); setCurrent(history[hIdx - 1]); setUrl(history[hIdx - 1]); };
  const goForward = () => { if (hIdx >= history.length - 1) return; setHIdx(hIdx + 1); setCurrent(history[hIdx + 1]); setUrl(history[hIdx + 1]); };
  const toggleSelect = () => { setSelecting((v) => !v); setAnnotState(null); setSentMsg(false); setHoverTag(null); };

  const clearHighlight = () => {
    setHoverTag(null);
    try {
      const doc = iframeRef.current?.contentDocument;
      if (doc) {
        doc.querySelectorAll(".__cc_hover").forEach((n) => {
          const el = n as HTMLElement;
          el.classList.remove("__cc_hover");
          el.style.outline = el.dataset.__cc_outline ?? "";
        });
      }
    } catch { /* 跨域：忽略 */ }
  };

  // 尝试获取 iframe document（跨域返回 null）
  const getIframeDoc = (): Document | null => {
    try {
      return iframeRef.current?.contentDocument ?? null;
    } catch {
      return null;
    }
  };

  // Esc 退出选择模式
  useEffect(() => {
    if (!selecting) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") { setSelecting(false); clearHighlight(); setAnnotState(null); } };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selecting]);

  const highlightAt = (e: React.MouseEvent) => {
    const doc = getIframeDoc();
    if (!doc) return; // 跨域：无法高亮，仅显示提示
    const iframe = iframeRef.current;
    const viewport = viewportRef.current;
    if (!iframe || !viewport) return;
    // 将浏览器视口坐标转换为 iframe 内部视口坐标
    const iframeRect = iframe.getBoundingClientRect();
    const localX = e.clientX - iframeRect.left;
    const localY = e.clientY - iframeRect.top;
    const el = doc.elementFromPoint(localX, localY) as HTMLElement | null;
    if (el && el !== doc.body && el !== doc.documentElement) {
      clearHighlight();
      el.dataset.__cc_outline = el.style.outline;
      el.classList.add("__cc_hover");
      el.style.outline = "2px solid #3B82F6";
      el.style.outlineOffset = "1px";
      // 光标跟随标签（overlay 坐标系 = viewport 坐标系）
      const vpRect = viewport.getBoundingClientRect();
      const tag = el.tagName.toLowerCase();
      const id = el.id ? `#${el.id}` : "";
      const cls = (el.classList?.[0] ? `.${el.classList[0]}` : "");
      const r = el.getBoundingClientRect();
      setHoverTag({
        x: e.clientX - vpRect.left,
        y: e.clientY - vpRect.top,
        text: `<${tag}${id}${cls}> ${Math.round(r.width)}×${Math.round(r.height)}`,
      });
    } else {
      clearHighlight();
    }
  };

  const handleOverlayClick = (e: React.MouseEvent) => {
    const rect = viewportRef.current?.getBoundingClientRect();
    if (!rect) return;
    const localX = e.clientX - rect.left;
    const localY = e.clientY - rect.top;
    let source = "";
    let info: ElementInfo | null = null;
    const doc = getIframeDoc();
    if (doc) {
      const iframe = iframeRef.current;
      if (iframe) {
        // 将浏览器视口坐标转换为 iframe 内部视口坐标
        const iframeRect = iframe.getBoundingClientRect();
        const elX = e.clientX - iframeRect.left;
        const elY = e.clientY - iframeRect.top;
        const el = doc.elementFromPoint(elX, elY) as HTMLElement | null;
        if (el) {
          source = el.outerHTML.slice(0, 600);
          try { info = getElementInfo(el); } catch {}
        }
      }
    }
    if (!source) source = `页面坐标 (${Math.round(localX)}, ${Math.round(localY)}) - 跨域页面无法获取元素`;
    clearHighlight();
    // 标注卡定位：贴点击点展开，触及视口右/下边缘时翻转到左/上侧，保证不溢出
    const CARD_W = 300, CARD_H = 300;
    let cardX = localX + 12;
    if (cardX + CARD_W > rect.width - 8) cardX = Math.max(8, localX - CARD_W - 12);
    let cardY = localY + 12;
    if (cardY + CARD_H > rect.height - 8) cardY = Math.max(8, rect.height - CARD_H - 8);
    setAnnotState({ x: cardX, y: cardY, source, info });
    setAnnotText("");
  };

  const sendToChat = () => {
    if (!annotState) return;
    const payload = `【浏览器标注】\n页面: ${current}\n${annotState.source}\n\n【标注说明】${annotText}`;
    setComposerDraft(payload);
    setSentMsg(true);
    clearHighlight();
    setAnnotState(null);
    setAnnotText("");
    setTimeout(() => setSentMsg(false), 3000);
  };

  return (
    <div className="browser-panel">
      <div className="browser-toolbar">
        <button className="browser-btn" onClick={goBack} disabled={hIdx <= 0} title="后退"><IconArrowLeft size={13} /></button>
        <button className="browser-btn" onClick={goForward} disabled={hIdx >= history.length - 1} title="前进"><IconArrowRight size={13} /></button>
        <button className="browser-btn" onClick={() => setCurrent(url)} title="刷新"><IconRefresh size={13} /></button>
        <div className="browser-url"><IconGlobe size={12} /><input value={url} onChange={(e) => setUrl(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") navigate(url); }} spellCheck={false} /></div>
        <button className={`browser-btn${selecting ? " active" : ""}`} onClick={toggleSelect} title={selecting ? "取消选择" : "选择元素标注"}><IconTarget size={13} /></button>
      </div>

      {sentMsg && <div style={{ padding: "6px 12px", background: "var(--success-soft)", color: "var(--success)", fontSize: 12 }}>已发送到主对话输入框</div>}

      <div className="browser-viewport" ref={viewportRef} style={{ cursor: selecting ? "crosshair" : "default" }}>
        <iframe ref={iframeRef} src={current} sandbox="allow-scripts allow-same-origin allow-forms allow-popups" title="browser" />
        {selecting && (
          <div
            className="browser-annot-overlay active"
            style={{ position: "absolute", inset: 0, zIndex: 10, cursor: "crosshair" }}
            onMouseMove={highlightAt}
            onClick={handleOverlayClick}
            onMouseLeave={clearHighlight}
          >
            <div className="browser-annot-tip" style={{
              position: "absolute", top: 8, left: "50%", transform: "translateX(-50%)",
              padding: "4px 10px", borderRadius: 4, background: "var(--bg-elevated)",
              border: "1px solid var(--border)", fontSize: 11, color: "var(--text-2)",
              whiteSpace: "nowrap", pointerEvents: "none",
            }}>
              移动鼠标聚焦元素，点击添加标注 · Esc 退出
            </div>
            {hoverTag && (
              <div style={{
                position: "absolute",
                left: Math.min(hoverTag.x + 14, Math.max(8, (viewportRef.current?.clientWidth ?? 300) - 170)),
                top: hoverTag.y + 16,
                padding: "2px 7px", borderRadius: 4, background: "#3B82F6", color: "#fff",
                fontSize: 10, fontFamily: "var(--font-mono)", whiteSpace: "nowrap",
                pointerEvents: "none", zIndex: 15,
              }}>
                {hoverTag.text}
              </div>
            )}
          </div>
        )}
        {annotState && (
          <div className="browser-annot-card" style={{
            position: "absolute", left: annotState.x, top: annotState.y,
            width: 300, maxHeight: "calc(100% - 16px)", overflowY: "auto",
            padding: 10, borderRadius: 8, background: "var(--bg-elevated)",
            border: "1px solid var(--border)", boxShadow: "var(--shadow-md)", zIndex: 20,
            display: "flex", flexDirection: "column", gap: 6,
          }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <span style={{ fontSize: 11, color: "var(--text-3)" }}>已选中元素，添加标注说明后发送给 AI：</span>
              <button onClick={() => { setAnnotState(null); setAnnotText(""); }} style={{ width: 18, height: 18, display: "flex", alignItems: "center", justifyContent: "center", color: "var(--text-3)" }}><IconX size={12} /></button>
            </div>
            <div style={{ fontSize: 10, color: "var(--text-3)", maxHeight: 60, overflow: "auto", background: "var(--bg-hover)", padding: 4, borderRadius: 4, fontFamily: "var(--font-mono)" }}>{annotState.source.substring(0, 200)}</div>
            {annotState.info && (
              <div className="browser-annot-info">
                <div className="browser-annot-info-title">
                  &lt;{annotState.info.tag}&gt;{annotState.info.id && <span className="browser-annot-info-id">#{annotState.info.id}</span>}
                  {annotState.info.className && <span className="browser-annot-info-cls">.{annotState.info.className.split(" ").join(".")}</span>}
                </div>
                <div className="browser-annot-info-grid">
                  <span>尺寸</span><b>{annotState.info.width} × {annotState.info.height}px</b>
                  <span>位置</span><b>({annotState.info.x}, {annotState.info.y})</b>
                  <span>显示</span><b>{annotState.info.display}</b>
                  <span>定位</span><b>{annotState.info.position}</b>
                  <span>字体</span><b>{annotState.info.fontSize}</b>
                  <span>文字色</span><b style={{ color: annotState.info.color }}>{annotState.info.color}</b>
                  <span>背景</span><b style={{ color: annotState.info.background }}>{annotState.info.background}</b>
                  <span>内边距</span><b>{annotState.info.padding}</b>
                </div>
                {annotState.info.text && (
                  <div className="browser-annot-info-text" title={annotState.info.text}>"{annotState.info.text}"</div>
                )}
              </div>
            )}
            <textarea value={annotText} onChange={(e) => setAnnotText(e.target.value)} placeholder="描述你希望 AI 关注的内容…" autoFocus style={{ minHeight: 50, resize: "vertical" }} />
            <div className="browser-annot-card-actions" style={{ display: "flex", justifyContent: "flex-end", gap: 6 }}>
              <button className="btn btn-ghost btn-sm" onClick={() => { setSelecting(false); clearHighlight(); setAnnotState(null); setAnnotText(""); }}>完成</button>
              <button className="btn btn-primary btn-sm" onClick={sendToChat}><IconArrowUp size={12} /> 发送到对话</button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
