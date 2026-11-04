import { useEffect, useRef, useState, type CSSProperties } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import {
  previewApi,
  type PreviewSource,
  type SheetMeta,
  type SheetRegion,
} from "../../api/client";
import { Button, Select, Tooltip } from "../ui";
import { IconInfo } from "../icons";
import { ResourceLoading } from "./ResourceLoading";

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
  const regions = Object.values(blocks);
  const widths = Object.assign({}, ...regions.map((r) => r.widths)) as Record<
    number,
    number
  >;
  const heights = Object.assign({}, ...regions.map((r) => r.heights)) as Record<
    number,
    number
  >;
  const rows = useVirtualizer({
    count: meta?.rows ?? 0,
    getScrollElement: () => host.current,
    estimateSize: (i) => heights[i] ?? 28,
    overscan: 5,
  });
  const cols = useVirtualizer({
    horizontal: true,
    count: meta?.cols ?? 0,
    getScrollElement: () => host.current,
    estimateSize: (i) => widths[i] ?? 96,
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
  }, [blocks]);
  useEffect(() => {
    if (!sheet) return;
    const ac = new AbortController();
    for (let r = Math.floor(r0 / 200) * 200; r <= r1; r += 200)
      for (let c = Math.floor(c0 / 50) * 50; c <= c1; c += 50) {
        const id = `${sheet}:${r}:${c}`;
        if (blocks[id] || pending.current.has(id)) continue;
        pending.current.add(id);
        previewApi
          .region(source, sheet, r, c, ac.signal)
          .then((d) => {
            if (!ac.signal.aborted) setBlocks((b) => ({ ...b, [id]: d }));
          })
          .catch((e) => {
            if (!ac.signal.aborted) setError(String(e));
          })
          .finally(() => pending.current.delete(id));
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
    setSheet(name);
    setBlocks({});
    setError("");
    pending.current.clear();
    host.current?.scrollTo(0, 0);
  };
  const loading = !error && (!meta || Object.keys(blocks).length === 0);
  return (
    <div className="resource-sheet">
      <div className="resource-tools resource-format-tools resource-sheet-tools">
        <div className="resource-sheet-picker">
          <span className="resource-control-label">工作表</span>
          <Select value={sheet} onChange={change} size="sm" disabled={!sheets.length}
            placeholder="正在读取…" options={sheets.map(s=>({value:s.name,label:s.name}))} aria-label="选择工作表" />
        </div>
        <div className="resource-format-meta">
          <span className="resource-dimensions">{meta ? `${meta.rows} 行 · ${meta.cols} 列` : "读取中…"}</span>
          {notice && <Tooltip content={notice}><Button variant="ghost" size="xs" iconOnly icon={<IconInfo size={14}/>} aria-label="表格预览说明"/></Tooltip>}
        </div>
      </div>
      <div
        className="resource-grid"
        ref={host}
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
            width: cols.getTotalSize() + 48,
            minWidth: "100%",
            minHeight: "100%",
            height: rows.getTotalSize() + 28,
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
                    left: c.start + 48,
                    top: r.start + 28,
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
                    left: offset(m[1], widths, 96) + 48,
                    top: offset(m[0], heights, 28) + 28,
                    width: offset(m[3], widths, 96) - offset(m[1], widths, 96),
                    height:
                      offset(m[2], heights, 28) - offset(m[0], heights, 28),
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
              style={{ left: c.start + 48, top: scroll.y, width: c.size }}
            >
              {columnName(c.index)}
            </div>
          ))}
          {vr.map((r) => (
            <div
              key={`r${r.index}`}
              className="resource-row"
              style={{ top: r.start + 28, left: scroll.x, height: r.size }}
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
    </div>
  );
}
