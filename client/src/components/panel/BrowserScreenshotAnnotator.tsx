import { useEffect, useRef, useState, type PointerEvent } from "react";
import { Button, Dialog, Input, Select } from "../ui";
import { downloadBlob } from "./BrowserManagerModals";

const tools = [
  { value: "rectangle", label: "矩形" }, { value: "ellipse", label: "椭圆" }, { value: "arrow", label: "箭头" },
  { value: "pen", label: "画笔" }, { value: "number", label: "序号" }, { value: "text", label: "文字" },
  { value: "mosaic", label: "马赛克" }, { value: "crop", label: "裁剪" },
] as const;
type Tool = (typeof tools)[number]["value"];
type Point = { x: number; y: number };
type Mark = { tool: Tool; points: Point[]; color: string; width: number; label?: string };
type Region = Point & { width: number; height: number };
const colors = [
  { value: "#e03942", label: "红色" }, { value: "#3478f6", label: "蓝色" }, { value: "#159a62", label: "绿色" },
  { value: "#e38219", label: "橙色" }, { value: "#ffffff", label: "白色" }, { value: "#18181b", label: "黑色" },
];
const widths = [{ value: "2", label: "细线" }, { value: "4", label: "中线" }, { value: "8", label: "粗线" }];

function region(mark: Mark): Region {
  const first = mark.points[0];
  const last = mark.points[mark.points.length - 1];
  const x = Math.floor(Math.min(first.x, last.x));
  const y = Math.floor(Math.min(first.y, last.y));
  return { x, y, width: Math.ceil(Math.max(first.x, last.x)) - x, height: Math.ceil(Math.max(first.y, last.y)) - y };
}

function drawMark(ctx: CanvasRenderingContext2D, mark: Mark) {
  const first = mark.points[0];
  const last = mark.points[mark.points.length - 1];
  const box = region(mark);
  ctx.save();
  ctx.strokeStyle = ctx.fillStyle = mark.color;
  ctx.lineWidth = mark.width;
  ctx.lineCap = ctx.lineJoin = "round";
  switch (mark.tool) {
    case "rectangle": ctx.strokeRect(box.x, box.y, box.width, box.height); break;
    case "ellipse":
      ctx.beginPath();
      ctx.ellipse(box.x + box.width / 2, box.y + box.height / 2, box.width / 2, box.height / 2, 0, 0, 2 * Math.PI);
      ctx.stroke();
      break;
    case "arrow": {
      const angle = Math.atan2(last.y - first.y, last.x - first.x);
      const head = Math.max(10, mark.width * 4);
      ctx.beginPath(); ctx.moveTo(first.x, first.y); ctx.lineTo(last.x, last.y);
      for (const direction of [-1, 1]) {
        ctx.moveTo(last.x, last.y);
        ctx.lineTo(last.x - head * Math.cos(angle + direction * 0.55), last.y - head * Math.sin(angle + direction * 0.55));
      }
      ctx.stroke();
      break;
    }
    case "pen":
      ctx.beginPath(); ctx.moveTo(first.x, first.y);
      for (const point of mark.points.slice(1)) ctx.lineTo(point.x, point.y);
      if (mark.points.length === 1) ctx.lineTo(first.x + 0.01, first.y);
      ctx.stroke();
      break;
    case "number": {
      const radius = Math.max(11, mark.width * 3);
      ctx.beginPath(); ctx.arc(first.x, first.y, radius, 0, 2 * Math.PI); ctx.fill();
      ctx.strokeStyle = "#ffffff"; ctx.lineWidth = 2; ctx.stroke();
      ctx.fillStyle = mark.color === "#ffffff" ? "#18181b" : "#ffffff";
      ctx.font = `bold ${Math.round(radius * 1.15)}px sans-serif`;
      ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.fillText(mark.label ?? "", first.x, first.y);
      break;
    }
    case "text":
      ctx.font = `bold ${Math.max(14, mark.width * 4)}px sans-serif`;
      ctx.textBaseline = "top"; ctx.lineWidth = 3;
      ctx.strokeStyle = mark.color === "#ffffff" ? "#18181b" : "#ffffff";
      ctx.strokeText(mark.label ?? "", first.x, first.y); ctx.fillText(mark.label ?? "", first.x, first.y);
      break;
    case "mosaic": {
      if (box.width < 1 || box.height < 1) break;
      const tile = Math.max(16, mark.width * 6);
      const small = document.createElement("canvas");
      small.width = Math.ceil(box.width / tile); small.height = Math.ceil(box.height / tile);
      const reduced = small.getContext("2d");
      if (!reduced) break;
      reduced.drawImage(ctx.canvas, box.x, box.y, box.width, box.height, 0, 0, small.width, small.height);
      ctx.imageSmoothingEnabled = false;
      ctx.drawImage(small, box.x, box.y, box.width, box.height);
      break;
    }
    case "crop": break;
  }
  ctx.restore();
}

function compose(canvas: HTMLCanvasElement, image: HTMLImageElement, marks: Mark[], preview: boolean) {
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("无法绘制截图");
  ctx.drawImage(image, 0, 0);
  for (const mark of marks) drawMark(ctx, mark);
  const crop = [...marks].reverse().find((mark) => mark.tool === "crop");
  const box = crop ? region(crop) : null;
  if (preview && box) {
    ctx.save();
    ctx.fillStyle = "rgba(15, 15, 15, .45)";
    ctx.beginPath(); ctx.rect(0, 0, canvas.width, canvas.height); ctx.rect(box.x, box.y, box.width, box.height);
    ctx.fill("evenodd");
    ctx.strokeStyle = "#ffffff"; ctx.lineWidth = 2; ctx.setLineDash([6, 4]);
    ctx.strokeRect(box.x, box.y, box.width, box.height);
    ctx.restore();
  }
  return box;
}

export interface BrowserScreenshot {
  image: string;
  title: string;
  url: string;
  tabId: string;
  initialTool?: Tool;
}

export function BrowserScreenshotAnnotator({ open, shot, canAttach, onClose, onAttach }: {
  open: boolean;
  shot: BrowserScreenshot;
  canAttach: boolean;
  onClose: () => void;
  onAttach: (blob: Blob, note: string) => Promise<void>;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const activePointer = useRef<number | null>(null);
  const draftRef = useRef<Mark | null>(null);
  const [image, setImage] = useState<HTMLImageElement | null>(null);
  const [tool, setTool] = useState<Tool>(shot.initialTool ?? "rectangle");
  const [color, setColor] = useState(colors[0].value);
  const [width, setWidth] = useState("4");
  const [text, setText] = useState("");
  const [history, setHistory] = useState<{ past: Mark[][]; marks: Mark[]; future: Mark[][] }>({ past: [], marks: [], future: [] });
  const [working, setWorking] = useState(false);
  const [status, setStatus] = useState("");

  useEffect(() => {
    const loaded = new Image();
    let current = true;
    loaded.onload = () => { if (current) setImage(loaded); };
    loaded.onerror = () => { if (current) setStatus("无法读取截图"); };
    loaded.src = shot.image;
    return () => { current = false; };
  }, [shot.image]);

  const repaint = (draft: Mark | null) => {
    if (canvas.current && image) compose(canvas.current, image, draft ? [...history.marks, draft] : history.marks, true);
  };
  useEffect(() => { repaint(null); }, [image, history.marks]);

  const commit = (marks: Mark[]) => setHistory(({ past, marks: before }) => ({
    past: [...past.slice(-99), before], marks, future: [],
  }));
  const undo = () => setHistory(({ past, marks, future }) => past.length
    ? { past: past.slice(0, -1), marks: past[past.length - 1], future: [marks, ...future] } : { past, marks, future });
  const redo = () => setHistory(({ past, marks, future }) => future.length
    ? { past: [...past, marks], marks: future[0], future: future.slice(1) } : { past, marks, future });

  const pointOf = (event: PointerEvent<HTMLCanvasElement>): Point => {
    const target = event.currentTarget;
    const rect = target.getBoundingClientRect();
    return {
      x: Math.max(0, Math.min(target.width, (event.clientX - rect.left) / rect.width * target.width)),
      y: Math.max(0, Math.min(target.height, (event.clientY - rect.top) / rect.height * target.height)),
    };
  };

  const start = (event: PointerEvent<HTMLCanvasElement>) => {
    if (!image || working || event.button !== 0 || activePointer.current != null) return;
    const first = pointOf(event);
    const mark: Mark = { tool, color, width: Number(width), points: [first] };
    if (tool === "text" || tool === "number") {
      const note = text.trim();
      if (tool === "text" && !note) { setStatus("先输入批注文字，再点击截图放置"); return; }
      mark.label = tool === "text" ? note : String(history.marks.filter((entry) => entry.tool === "number").length + 1);
      commit([...history.marks, mark]); setStatus("");
      return;
    }
    event.currentTarget.setPointerCapture(event.pointerId);
    activePointer.current = event.pointerId;
    draftRef.current = mark;
  };
  const move = (event: PointerEvent<HTMLCanvasElement>) => {
    const mark = draftRef.current;
    if (!mark || activePointer.current !== event.pointerId) return;
    const point = pointOf(event);
    mark.points = mark.tool === "pen" ? [...mark.points, point] : [mark.points[0], point];
    repaint(mark);
  };
  const finish = (event: PointerEvent<HTMLCanvasElement>, cancelled: boolean) => {
    if (event.pointerId !== activePointer.current) return;
    if (!cancelled) move(event);
    const mark = draftRef.current;
    activePointer.current = null; draftRef.current = null;
    if (mark && !cancelled) {
      const box = region(mark);
      if (mark.tool === "pen" || ((mark.tool === "crop" || mark.tool === "mosaic")
        ? box.width > 2 && box.height > 2 : box.width > 2 || box.height > 2)) {
        commit([...history.marks, mark]); return;
      }
    }
    repaint(null);
  };

  const output = async (action: "copy" | "save" | "attach") => {
    if (!image || working) return;
    setWorking(true); setStatus("");
    try {
      const result = document.createElement("canvas");
      result.width = image.naturalWidth; result.height = image.naturalHeight;
      const selection = compose(result, image, history.marks, false);
      let exportCanvas = result;
      if (selection) {
        exportCanvas = document.createElement("canvas");
        exportCanvas.width = selection.width; exportCanvas.height = selection.height;
        exportCanvas.getContext("2d")?.drawImage(result, selection.x, selection.y, selection.width, selection.height,
          0, 0, selection.width, selection.height);
      }
      const blob = await new Promise<Blob>((resolve, reject) => exportCanvas.toBlob(
        (item) => item ? resolve(item) : reject(new Error("无法导出 PNG")), "image/png",
      ));
      if (action === "copy") {
        if (window.chatcoderAPI?.writeBrowserImage) {
          const png = await new Promise<string>((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => resolve(String(reader.result));
            reader.onerror = reject;
            reader.readAsDataURL(blob);
          });
          if (!await window.chatcoderAPI.writeBrowserImage(png)) throw new Error("无法复制截图");
        } else await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]);
        setStatus("已复制合成图片");
      } else if (action === "save") {
        downloadBlob(blob, `网页批注-${Date.now()}.png`);
        setStatus("已导出 PNG");
      } else {
        const notes = history.marks.filter((mark) => mark.tool === "text").map((mark) => mark.label).join("\n");
        await onAttach(blob, notes);
      }
    } catch (reason) { setStatus(reason instanceof Error ? reason.message : String(reason)); }
    finally { setWorking(false); }
  };

  return <Dialog open={open} title="截图标注" subtitle={shot.title || shot.url} width={1200}
    className="bp-annotator-dialog" bodyClassName="bp-annotator-body"
    onClose={() => { if (!working) onClose(); }}
    footer={<>
      <Button size="sm" variant="ghost" disabled={!image || working} onClick={() => void output("copy")}>复制图片</Button>
      <Button size="sm" variant="secondary" disabled={!image || working} onClick={() => void output("save")}>保存 PNG</Button>
      {canAttach && <Button size="sm" variant="primary" disabled={!image || working} onClick={() => void output("attach")}>添加到对话</Button>}
    </>}>
    <div className="bp-annotator" onKeyDown={(event) => {
      if ((!event.ctrlKey && !event.metaKey) || event.target instanceof HTMLInputElement) return;
      const key = event.key.toLowerCase();
      if (key === "z" || key === "y") { event.preventDefault(); if (key === "y" || event.shiftKey) redo(); else undo(); }
    }}>
      <div className="bp-annotator-toolbar" role="toolbar" aria-label="截图工具">
        <div className="bp-annotator-tools">
          {tools.map((item) => <Button size="xs" key={item.value} variant={tool === item.value ? "secondary" : "ghost"}
            aria-pressed={tool === item.value} onClick={() => setTool(item.value)}>{item.label}</Button>)}
        </div>
        <div className="bp-annotator-style">
          <Select size="sm" value={color} options={colors} onChange={setColor} aria-label="标注颜色" />
          <Select size="sm" value={width} options={widths} onChange={setWidth} aria-label="线宽" />
        </div>
        <div className="bp-annotator-history">
          <Button size="xs" variant="ghost" disabled={!history.past.length} onClick={undo}>撤销</Button>
          <Button size="xs" variant="ghost" disabled={!history.future.length} onClick={redo}>重做</Button>
          <Button size="xs" variant="danger-ghost" disabled={!history.marks.length} onClick={() => commit([])}>清空</Button>
        </div>
      </div>
      {tool === "text" && <Input value={text} size="sm" maxLength={180} aria-label="批注文字"
        placeholder="输入文字，点击图片放置" onChange={(event) => setText(event.target.value)} />}
      <div className="bp-annotator-stage">
        {image && <canvas ref={canvas} width={image.naturalWidth} height={image.naturalHeight} tabIndex={0}
          aria-label="截图画布" onPointerDown={start} onPointerMove={move} onPointerUp={(event) => finish(event, false)}
          onPointerCancel={(event) => finish(event, true)} onLostPointerCapture={(event) => finish(event, true)} />}
      </div>
      <div className="bp-annotator-status" role="status">{status || (image ? `${image.naturalWidth} × ${image.naturalHeight}` : "正在打开截图…")}</div>
    </div>
  </Dialog>;
}
