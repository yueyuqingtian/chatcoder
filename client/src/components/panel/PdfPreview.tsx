import { useEffect, useRef, useState, type CSSProperties } from "react";
import { getDocument, GlobalWorkerOptions, TextLayer, type PDFDocumentProxy } from "pdfjs-dist";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import { Button, Tooltip } from "../ui";
import { IconMinus, IconPlus } from "../icons";
import { ResourceLoading } from "./ResourceLoading";
import { wheelZoom } from "./wordZoom";

GlobalWorkerOptions.workerSrc = workerUrl;

/** plan-20261105-file-review：自主控制缩放与多页换行，避免浏览器 PDF 插件吞掉滚轮。 */
export default function PdfPreview({ url }: { url: string }) {
  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null);
  const [error, setError] = useState("");
  const [zoom, setZoom] = useState<number | null>(null);
  const [width, setWidth] = useState(400);
  const [pageWidth, setPageWidth] = useState(794);
  const host = useRef<HTMLDivElement>(null);
  const scale = zoom == null ? Math.min(1, Math.max(0.1, (width - 32) / pageWidth)) : zoom / 100;
  useEffect(() => {
    let disposed = false;
    const assets = new URL(`${import.meta.env.BASE_URL}pdf-assets/`, document.baseURI).href;
    const task = getDocument({ url, cMapUrl: `${assets}cmaps/`, cMapPacked: true,
      standardFontDataUrl: `${assets}standard_fonts/`, wasmUrl: `${assets}wasm/` });
    setError(""); setPdf(null); setZoom(null);
    task.promise.then(async doc => {
      const page = await doc.getPage(1);
      if (!disposed) { setPageWidth(page.getViewport({ scale: 1 }).width); setPdf(doc); }
    }).catch(e => { if (!disposed) setError(`无法预览 PDF：${String(e)}`); });
    return () => { disposed = true; void task.destroy(); };
  }, [url]);
  useEffect(() => {
    const el = host.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setWidth(el.clientWidth));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  useEffect(() => {
    const el = host.current;
    if (!el) return;
    const wheel = (e: WheelEvent) => {
      if (!e.ctrlKey) return;
      e.preventDefault();
      setZoom(wheelZoom(scale * 100, e.deltaY, e.deltaMode));
    };
    el.addEventListener("wheel", wheel, { passive: false });
    return () => el.removeEventListener("wheel", wheel);
  }, [scale]);
  return <div className="resource-document">
    <div className="resource-tools resource-format-tools">
      <div className="resource-actions">
        <Button size="xs" variant="ghost" iconOnly icon={<IconMinus size={14}/>} aria-label="缩小 PDF" onClick={() => setZoom(Math.max(10, scale * 100 - 10))}/>
        <Tooltip content="恢复 100%；Ctrl + 滚轮连续缩放"><Button size="xs" variant="ghost" onClick={() => setZoom(100)}>{Math.round(scale * 100)}%</Button></Tooltip>
        <Button size="xs" variant="ghost" iconOnly icon={<IconPlus size={14}/>} aria-label="放大 PDF" onClick={() => setZoom(Math.min(400, scale * 100 + 10))}/>
        <Button size="xs" variant="ghost" onClick={() => setZoom(null)}>适应宽度</Button>
      </div>
      <span className="resource-format-hint">{pdf ? `${pdf.numPages} 页` : "读取中…"}</span>
    </div>
    <div className="resource-pages-scroll" ref={host}>
      {error ? <div className="resource-state" role="alert">{error}</div> : !pdf ? <ResourceLoading>正在读取 PDF…</ResourceLoading> :
        <div className="resource-pages">{Array.from({ length: pdf.numPages }, (_, i) => <PdfPage key={`${url}:${i}`} pdf={pdf} number={i + 1} scale={scale} scrollHost={host.current!}/>)}</div>}
    </div>
  </div>;
}

function PdfPage({ pdf, number, scale, scrollHost }: { pdf: PDFDocumentProxy; number: number; scale: number; scrollHost: HTMLElement }) {
  const host = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const textHost = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(false);
  const [size, setSize] = useState({ width: 794, height: 1123 });
  const [error, setError] = useState("");
  useEffect(() => {
    const observer = new IntersectionObserver(([entry]) => setVisible(entry.isIntersecting), { root: scrollHost, rootMargin: "500px" });
    if (host.current) observer.observe(host.current);
    return () => observer.disconnect();
  }, [scrollHost]);
  useEffect(() => {
    if (!visible) return;
    let disposed = false;
    let textLayer: TextLayer | undefined;
    let render: ReturnType<Awaited<ReturnType<PDFDocumentProxy["getPage"]>>["render"]> | undefined;
    void pdf.getPage(number).then(async page => {
      if (disposed || !canvas.current) return;
      const natural = page.getViewport({ scale: 1 });
      setSize({ width: natural.width, height: natural.height });
      const viewport = page.getViewport({ scale });
      // plan-20261105-file-review：限制超高缩放的像素面积，避免大幅页面一次占满显存。
      const ratio = Math.min(devicePixelRatio || 1, 2, Math.sqrt(16_000_000 / (viewport.width * viewport.height)));
      const el = canvas.current;
      el.width = Math.ceil(viewport.width * ratio); el.height = Math.ceil(viewport.height * ratio);
      render = page.render({ canvas: el, viewport, transform: [ratio, 0, 0, ratio, 0, 0] });
      await render.promise;
      const text = await page.getTextContent();
      if (!disposed && textHost.current) {
        textHost.current.replaceChildren();
        textLayer = new TextLayer({ textContentSource: text, container: textHost.current, viewport });
        await textLayer.render();
      }
    }).catch(e => { if (!disposed) setError(`第 ${number} 页无法显示：${String(e)}`); });
    return () => { disposed = true; render?.cancel(); textLayer?.cancel(); textHost.current?.replaceChildren(); if (canvas.current) { canvas.current.width = 0; canvas.current.height = 0; } };
  }, [visible, pdf, number, scale]);
  return <div ref={host} className="resource-pdf-page" style={{ width: size.width * scale, height: size.height * scale, "--total-scale-factor": scale } as CSSProperties} aria-label={`第 ${number} 页`}>
    {error ? <div className="resource-state">{error}</div> : <><canvas ref={canvas} style={{ width: "100%", height: "100%" }}/><div className="resource-pdf-text" ref={textHost}/></>}
    <span className="resource-page-number">{number}</span>
  </div>;
}
