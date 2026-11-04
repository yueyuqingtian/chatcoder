import { useEffect, useRef, useState } from "react";
import { IconMinus, IconPlus } from "../icons";
import { wheelZoom } from "./wordZoom";
import { Button, Tooltip } from "../ui";

/** plan-20261103-attachment-preview：文档 CSS 隔离，禁止活动内容与远程资源。 */
export default function WordPreview({ url }: { url: string }) {
  const frame = useRef<HTMLIFrameElement>(null);
  const effectiveZoom = useRef(100);
  const [zoom, setZoom] = useState<number | null>(null);
  const fit = () => {
    const doc = frame.current?.contentDocument;
    const page = doc?.querySelector<HTMLElement>("section.docx");
    if (doc && page && frame.current) {
      const factor =
        zoom == null
          ? Math.min(
              1,
              Math.max(
                0.1,
                (frame.current.clientWidth - 32) / (page.offsetWidth + 40),
              ),
            )
          : zoom / 100;
      effectiveZoom.current = factor * 100;
      doc.body.style.zoom = String(factor);
    }
  };
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    fit();
    const el = frame.current;
    if (!el) return;
    const ro = new ResizeObserver(fit);
    ro.observe(el);
    return () => ro.disconnect();
  }, [zoom, loading]);
  useEffect(() => {
    if (loading) return;
    const doc = frame.current?.contentDocument;
    if (!doc) return;
    const wheel = (event: WheelEvent) => {
      if (!event.ctrlKey) return;
      event.preventDefault();
      event.stopPropagation();
      const current = effectiveZoom.current;
      const next = wheelZoom(current, event.deltaY, event.deltaMode);
      const scroll = doc.scrollingElement;
      const left = scroll?.scrollLeft ?? 0;
      const top = scroll?.scrollTop ?? 0;
      effectiveZoom.current = next;
      doc.body.style.zoom = String(next / 100);
      if (scroll) {
        scroll.scrollLeft =
          ((left + event.clientX) * next) / current - event.clientX;
        scroll.scrollTop =
          ((top + event.clientY) * next) / current - event.clientY;
      }
      setZoom(next);
    };
    doc.addEventListener("wheel", wheel, { passive: false, capture: true });
    return () => doc.removeEventListener("wheel", wheel, { capture: true });
  }, [loading, url]);
  useEffect(() => {
    const controller = new AbortController();
    let disposed = false;
    setLoading(true);
    setError("");
    void (async () => {
      try {
        const [response, lib] = await Promise.all([
          fetch(url, { signal: controller.signal }),
          import("docx-preview"),
        ]);
        if (!response.ok) throw new Error("文档无法读取，可能已损坏或加密");
        const blob = await response.blob();
        if (disposed) return;
        const doc = frame.current?.contentDocument;
        if (!doc) throw new Error("文档预览容器未就绪");
        doc.open();
        doc.write(
          `<!doctype html><html><head><meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src blob: data:; font-src blob: data:; style-src 'unsafe-inline';"><style>body{margin:0;background:#e8e8e8;} .docx-wrapper{padding:20px!important;background:#e8e8e8!important;width:max-content;min-width:100%;box-sizing:border-box;} section.docx{margin-bottom:16px!important;} a{pointer-events:none;}</style></head><body></body></html>`,
        );
        doc.close();
        await lib.renderAsync(blob, doc.body, undefined, {
          inWrapper: true,
          breakPages: true,
          renderAltChunks: false,
          useBase64URL: true,
        });
        if (!disposed) setLoading(false);
      } catch (e) {
        if (!disposed) {
          setError(String(e));
          setLoading(false);
        }
      }
    })();
    return () => {
      disposed = true;
      controller.abort();
    };
  }, [url]);
  return (
    <div className="resource-word">
      <div className="resource-tools resource-format-tools resource-word-tools">
        <div className="resource-actions resource-zoom-actions">
          <Button
            variant="ghost"
            size="xs"
            onClick={() =>
              setZoom((z) => Math.max(10, (z ?? effectiveZoom.current) - 25))
            }
            iconOnly
            icon={<IconMinus size={14}/>}
            aria-label="缩小文档"
          >
            −
          </Button>
          <Tooltip content="按住 Ctrl 滚动鼠标滚轮可连续缩放">
            <span className="resource-zoom" aria-label="文档缩放比例">
              {zoom == null ? "适应" : `${Math.round(zoom * 10) / 10}%`}
            </span>
          </Tooltip>
          <Button
            variant="ghost"
            size="xs"
            onClick={() =>
              setZoom((z) => Math.min(400, (z ?? effectiveZoom.current) + 25))
            }
            iconOnly
            icon={<IconPlus size={14}/>}
            aria-label="放大文档"
          >
            ＋
          </Button>
          <Button variant="ghost" size="xs" onClick={() => setZoom(null)}>
            适应宽度
          </Button>
        </div>
        <span className="resource-format-hint" role="status">{loading ? "正在排版…" : "Ctrl + 滚轮缩放"}</span>
      </div>
      {error && (
        <div className="resource-state" role="alert">
          {error}
        </div>
      )}
      <iframe
        ref={frame}
        className="resource-word-frame"
        aria-label="Word 文档"
        sandbox="allow-same-origin"
        onLoad={() => {
          const doc = frame.current?.contentDocument;
          if (doc)
            doc.addEventListener("click", (e) => {
              if ((e.target as Element).closest?.("a")) e.preventDefault();
            });
        }}
      />
    </div>
  );
}
