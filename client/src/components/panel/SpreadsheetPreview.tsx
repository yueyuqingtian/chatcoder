import { useEffect, useRef, useState, type CSSProperties } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import {
  previewApi,
  type PreviewSource,
  type SheetMeta,
  type SheetRegion,
} from "../../api/client";
import { Button, Tooltip } from "../ui";
import { IconInfo, IconMinus, IconPlus } from "../icons";
import { ResourceLoading } from "./ResourceLoading";
import { wheelZoom } from "./wordZoom";

function columnName(n: number) {
  let s = "";
  for (n++; n; n = Math.floor((n - 1) / 26))
    s = String.fromCharCode(65 + ((n - 1) % 26)) + s;
  return s;
}
export default function SpreadsheetPreview({
  source,
}: {
  source: PreviewSource;
}) {
  const [zoom, setZoom] = useState(100);
  const scale = zoom / 100;
  const [customWidths, setCustomWidths] = useState<Record<string, number>>({});
  const drag = useRef<{ col: number; x: number; width: number } | null>(null);
  const generation = useRef(0);
  const [sheets, setSheets] = useState<SheetMeta[]>([]);
  const [sheet, setSheet] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [blocks, setBlocks] = useState<Record<string, SheetRegion>>({});
  const pending = useRef(new Set<string>());
  const host = useRef<HTMLDivElement>(null);
  const [scroll, setScroll] = useState({ x: 0, y: 0 });
  const scrollFrame = useRef(0);
  useEffect(() => () => cancelAnimationFrame(scrollFrame.current), []);
  const key = JSON.stringify(source);
  useEffect(() => {
    const ac = new AbortController();
    setError("");
    setZoom(100);
    setCustomWidths({});
    setSheets([]);
    setSheet("");
    setBlocks({});
    previewApi
      .workbook(source, ac.signal)
      .then((d) => {
        if(ac.signal.aborted)return;
        setSheets(d.sheets);
        setSheet(d.sheets[0]?.name ?? "");
        setNotice(d.notice);
      })
      .catch((e) => {
        if (!ac.signal.aborted) setError(String(e));
      });
    return () => ac.abort();
  }, [key]);
  const meta = sheets.find((s) => s.name === sheet);
  const regions = Object.entries(blocks).filter(([id]) => id.startsWith(`${sheet}:`)).map(([, region]) => region);
  const widths = Object.assign({}, ...regions.map((r) => r.widths)) as Record<
    number,
    number
  >;
  for (const [id, width] of Object.entries(customWidths)) {
    if (id.startsWith(`${sheet}:`)) widths[Number(id.slice(sheet.length + 1))] = width;
  }
  const heights = Object.assign({}, ...regions.map((r) => r.heights)) as Record<
    number,
    number
  >;
  const rows = useVirtualizer({
    count: meta?.rows ?? 0,
    getScrollElement: () => host.current,
    estimateSize: (i) => (heights[i] ?? 28) * scale,
    overscan: 5,
  });
  const cols = useVirtualizer({
    horizontal: true,
    count: meta?.cols ?? 0,
    getScrollElement: () => host.current,
    estimateSize: (i) => (widths[i] ?? 96) * scale,
    overscan: 2,
  });
  const vr = rows.getVirtualItems(),
    vc = cols.getVirtualItems();
  const r0 = vr[0]?.index ?? 0,
    r1 = vr.at(-1)?.index ?? 0,
    c0 = vc[0]?.index ?? 0,
    c1 = vc.at(-1)?.index ?? 0;
  useEffect(() => {
    rows.measure();
    cols.measure();
  }, [blocks, customWidths, zoom, sheet]);
  useEffect(() => {
    if (!sheet) return;
    const ac = new AbortController();
    const request = ++generation.current;
    for (let r = Math.floor(r0 / 200) * 200; r <= r1; r += 200)
      for (let c = Math.floor(c0 / 50) * 50; c <= c1; c += 50) {
        const id = `${sheet}:${r}:${c}`;
        if (blocks[id] || pending.current.has(id)) continue;
        pending.current.add(id);
        previewApi
          .region(source, sheet, r, c, ac.signal)
          .then((d) => {
            if (!ac.signal.aborted && generation.current === request) setBlocks((b) => ({ ...b, [id]: d }));
          })
          .catch((e) => {
            if (!ac.signal.aborted) setError(String(e));
          })
          .finally(() => { if (generation.current === request) pending.current.delete(id); });
      }
    return () => {
      ac.abort();
      pending.current.clear();
    };
  }, [key, sheet, r0, r1, c0, c1]);
  const cellMap = new Map(
    regions.flatMap((b) => b.cells).map((c) => [`${c.row}:${c.col}`, c]),
  );
  const merges = Array.from(
    new Map(
      regions.flatMap((b) => b.merges).map((m) => [m.join(":"), m]),
    ).values(),
  );
  const offset = (
    index: number,
    sizes: Record<number, number>,
    fallback: number,
  ) => {
    let value = 0;
    for (let i = 0; i < index; i++) value += sizes[i] ?? fallback;
    return value;
  };
  const change = (name: string) => {
    if (name === sheet) return;
    setSheet(name);
    setBlocks({});
    setError("");
    pending.current.clear();
    host.current?.scrollTo(0, 0);
  };
  const setWidth = (col: number, width: number) => setCustomWidths(w => ({ ...w, [`${sheet}:${col}`]: Math.max(40, Math.min(1600, width)) }));
  const autoWidth = (col: number) => {
    const canvas = document.createElement("canvas");
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.font = "12px sans-serif";
    const texts = [...cellMap.values()].filter(c => c.col === col);
    setWidth(col, Math.max(64, ...texts.map(c => Math.min(1000, ctx.measureText(c.text).width + 24))));
  };
  useEffect(() => {
    const el = host.current;
    if (!el) return;
    const wheel = (event: WheelEvent) => {
      if (!event.ctrlKey) return;
      event.preventDefault();
      const next = wheelZoom(zoom, event.deltaY, event.deltaMode);
      const rect = el.getBoundingClientRect();
      const x = event.clientX - rect.left, y = event.clientY - rect.top;
      const left = (el.scrollLeft + x) * next / zoom - x;
      const top = (el.scrollTop + y) * next / zoom - y;
      setZoom(next);
      requestAnimationFrame(() => el.scrollTo(left, top));
    };
    el.addEventListener("wheel", wheel, { passive: false });
    return () => el.removeEventListener("wheel", wheel);
  }, [zoom]);
  const loading = !error && (!meta || Object.keys(blocks).length === 0);
  return (
    <div className="resource-sheet">
      <div className="resource-tools resource-format-tools resource-sheet-tools">
        <div className="resource-actions resource-zoom-actions">
          <Button variant="ghost" size="xs" iconOnly icon={<IconMinus size={14}/>} aria-label="缩小表格" onClick={() => setZoom(z => Math.max(10, z - 10))}/>
          <Tooltip content="恢复 100%；Ctrl + 滚轮可连续缩放"><Button size="xs" variant="ghost" onClick={() => setZoom(100)}>{Math.round(zoom)}%</Button></Tooltip>
          <Button variant="ghost" size="xs" iconOnly icon={<IconPlus size={14}/>} aria-label="放大表格" onClick={() => setZoom(z => Math.min(400, z + 10))}/>
        </div>
        <div className="resource-format-meta">
          <span className="resource-dimensions">{meta ? `${meta.rows} 行 · ${meta.cols} 列` : "读取中…"}</span>
          {notice && <Tooltip content={notice}><Button variant="ghost" size="xs" iconOnly icon={<IconInfo size={14}/>} aria-label="表格预览说明"/></Tooltip>}
        </div>
      </div>
      <div
        className="resource-grid"
        ref={host}
        style={{ "--sheet-scale": scale } as CSSProperties}
        onPointerMove={e => { if (drag.current) setWidth(drag.current.col, drag.current.width + (e.clientX - drag.current.x) / scale); }}
        onPointerUp={() => { drag.current = null; }}
        onPointerCancel={() => { drag.current = null; }}
        onScroll={() => {
          if (scrollFrame.current) return;
          scrollFrame.current = requestAnimationFrame(() => {
            scrollFrame.current = 0;
            const el = host.current;
            if (el) setScroll({ x: el.scrollLeft, y: el.scrollTop });
          });
        }}
        role="region"
        aria-label="只读工作表"
        aria-busy={loading}
      >
        {loading && <div className="resource-grid-overlay"><ResourceLoading>{meta ? "正在加载工作表…" : "正在读取工作簿…"}</ResourceLoading></div>}
        {error && <div className="resource-grid-overlay"><div className="resource-state" role="alert"><strong>无法读取工作表</strong><p>{error}</p></div></div>}
        <div
          style={{
            width: cols.getTotalSize() + 48 * scale,
            minWidth: "100%",
            minHeight: "100%",
            height: rows.getTotalSize() + 28 * scale,
            position: "relative",
          }}
        >
          {vr.map((r) =>
            vc.map((c) => {
              const merged = merges.find(
                (m) =>
                  r.index >= m[0] &&
                  r.index < m[2] &&
                  c.index >= m[1] &&
                  c.index < m[3],
              );
              if (merged) return null;
              const cell = cellMap.get(`${r.index}:${c.index}`);
              return (
                <div
                  key={`${r.index}:${c.index}`}
                  className="resource-cell"
                  style={{
                    ...(cell?.style as CSSProperties),
                    fontSize: (Number(cell?.style.fontSize) || 12) * scale,
                    left: c.start + 48 * scale,
                    top: r.start + 28 * scale,
                    width: c.size,
                    height: r.size,
                  }}
                >
                  {cell?.text ?? ""}
                </div>
              );
            }),
          )}
          {merges
            .filter((m) => m[0] <= r1 && m[2] > r0 && m[1] <= c1 && m[3] > c0)
            .map((m) => {
              const cell = cellMap.get(`${m[0]}:${m[1]}`);
              return (
                <div
                  key={m.join(":")}
                  className="resource-cell"
                  style={{
                    ...(cell?.style as CSSProperties),
                    fontSize: (Number(cell?.style.fontSize) || 12) * scale,
                    left: (offset(m[1], widths, 96) + 48) * scale,
                    top: (offset(m[0], heights, 28) + 28) * scale,
                    width: (offset(m[3], widths, 96) - offset(m[1], widths, 96)) * scale,
                    height:
                      (offset(m[2], heights, 28) - offset(m[0], heights, 28)) * scale,
                  }}
                >
                  {cell?.text ?? ""}
                </div>
              );
            })}
          {vc.map((c) => (
            <div
              key={`c${c.index}`}
              className="resource-column"
              style={{ left: c.start + 48 * scale, top: scroll.y, width: c.size }}
            >
              {columnName(c.index)}
              <div className="resource-column-resize" role="separator" aria-label={`调整 ${columnName(c.index)} 列宽`} aria-orientation="vertical" tabIndex={0}
                onPointerDown={e => { e.preventDefault(); drag.current = { col: c.index, x: e.clientX, width: widths[c.index] ?? 96 }; e.currentTarget.setPointerCapture(e.pointerId); }}
                onDoubleClick={() => autoWidth(c.index)}
                onKeyDown={e => { if (e.key === "ArrowLeft" || e.key === "ArrowRight") { e.preventDefault(); setWidth(c.index, (widths[c.index] ?? 96) + (e.key === "ArrowRight" ? 16 : -16)); } }}/>

            </div>
          ))}
          {vr.map((r) => (
            <div
              key={`r${r.index}`}
              className="resource-row"
              style={{ top: r.start + 28 * scale, left: scroll.x, height: r.size }}
            >
              {r.index + 1}
            </div>
          ))}
          <div
            className="resource-grid-corner"
            style={{ top: scroll.y, left: scroll.x }}
          />
        </div>
      </div>
      <div className="resource-sheet-tabs" role="tablist" aria-label="工作表">
        {sheets.map(s => <Button key={s.name} variant={s.name === sheet ? "subtle" : "ghost"} size="xs" role="tab" aria-selected={s.name === sheet} onClick={() => change(s.name)}
          onKeyDown={e => { if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) { e.preventDefault(); const i = sheets.findIndex(x => x.name === sheet); const next = e.key === "Home" ? 0 : e.key === "End" ? sheets.length - 1 : (i + (e.key === "ArrowRight" ? 1 : -1) + sheets.length) % sheets.length; change(sheets[next].name); (e.currentTarget.parentElement?.children[next] as HTMLElement)?.focus(); } }}>{s.name}</Button>)}
      </div>
    </div>
  );
}
