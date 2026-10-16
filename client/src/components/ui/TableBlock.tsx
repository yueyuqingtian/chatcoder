/** TableBlock —— Markdown 表格的统一渲染（plan-20261016-ux 问题 8，对齐参考图 5 / 图 6）
 *
 * 卡片容器（12px 圆角、1px 边框、表头浅字、行分隔线）+ 右上角工具条：
 *  - 复制为 Markdown：按渲染结果还原（行内代码 / 加粗 / 链接保留 Markdown 写法，单元格里的「|」转义）；
 *  - 下载 CSV：带 UTF-8 BOM，Excel 直接打开中文不乱码；
 *  - 展开：「表格预览」浮窗，大视图可横纵滚动，浮窗里同样可以复制与下载。
 * 数据从渲染后的 DOM 读取——流式与落库共用同一个组件，不需要另外解析 Markdown 源文。
 */
import { useCallback, useRef, useState, type ReactNode } from "react";
import { IconCheck, IconCopy, IconDownload, IconMaximize } from "../icons";
import { Dialog } from "./Dialog";
import { Tooltip } from "./Tooltip";

/** 单元格 → Markdown 行内文本 */
function cellMarkdown(el: Element): string {
  let out = "";
  el.childNodes.forEach((n) => {
    if (n.nodeType === Node.TEXT_NODE) {
      out += n.textContent ?? "";
      return;
    }
    if (!(n instanceof Element)) return;
    const tag = n.tagName;
    if (tag === "CODE") out += "`" + (n.textContent ?? "") + "`";
    else if (tag === "STRONG" || tag === "B") out += "**" + cellMarkdown(n) + "**";
    else if (tag === "EM" || tag === "I") out += "*" + cellMarkdown(n) + "*";
    else if (tag === "DEL" || tag === "S") out += "~~" + cellMarkdown(n) + "~~";
    else if (tag === "BR") out += "<br>";
    else if (tag === "A") {
      const href = n.getAttribute("href") ?? "";
      const text = cellMarkdown(n);
      out += /^(https?:|mailto:)/.test(href) ? `[${text}](${href})` : text;
    } else if (tag !== "svg") out += cellMarkdown(n);
  });
  return out;
}

function rowsOf(table: HTMLTableElement | null): HTMLTableRowElement[] {
  return table ? Array.from(table.rows) : [];
}

/** 表格 → Markdown（GFM 表格，保留列对齐） */
function tableToMarkdown(table: HTMLTableElement | null): string {
  const rows = rowsOf(table);
  if (rows.length === 0) return "";
  const esc = (s: string) => s.replace(/\r?\n/g, " ").replace(/\|/g, "\\|").trim();
  const lines: string[] = [];
  const head = Array.from(rows[0].cells);
  lines.push(`| ${head.map((c) => esc(cellMarkdown(c))).join(" | ")} |`);
  lines.push(`| ${head.map((c) => {
    const align = (c as HTMLElement).style.textAlign || c.getAttribute("align") || "";
    if (align === "center") return ":---:";
    if (align === "right") return "---:";
    if (align === "left") return ":---";
    return "---";
  }).join(" | ")} |`);
  for (const r of rows.slice(1)) lines.push(`| ${Array.from(r.cells).map((c) => esc(cellMarkdown(c))).join(" | ")} |`);
  return lines.join("\n");
}

/** 表格 → CSV（RFC 4180 引号转义；BOM 让 Excel 按 UTF-8 打开） */
function tableToCsv(table: HTMLTableElement | null): string {
  const q = (s: string) => {
    const v = s.replace(/\s+/g, " ").trim();
    return /[",\r\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v;
  };
  const body = rowsOf(table).map((r) => Array.from(r.cells).map((c) => q(c.textContent ?? "")).join(",")).join("\r\n");
  return "﻿" + body;
}

function downloadCsv(csv: string) {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, "0");
  const name = `表格-${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}.csv`;
  const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 2000);
}

/** 工具条（行内与浮窗共用）：复制 / 下载 /（行内才有的）展开 */
function TableTools({ getTable, onExpand }: { getTable: () => HTMLTableElement | null; onExpand?: () => void }) {
  const [copied, setCopied] = useState(false);
  const copy = useCallback(() => {
    const md = tableToMarkdown(getTable());
    if (!md) return;
    void navigator.clipboard?.writeText(md).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    });
  }, [getTable]);
  return (
    <div className="md-table-tools" role="toolbar" aria-label="表格操作">
      <Tooltip content={copied ? "已复制" : "复制为 Markdown"} side="top">
        <button type="button" className={"md-table-tool" + (copied ? " is-done" : "")} aria-label="复制为 Markdown" onClick={copy}>
          {copied ? <IconCheck size={14} /> : <IconCopy size={14} />}
        </button>
      </Tooltip>
      <Tooltip content="下载 CSV" side="top">
        <button type="button" className="md-table-tool" aria-label="下载 CSV" onClick={() => downloadCsv(tableToCsv(getTable()))}>
          <IconDownload size={14} />
        </button>
      </Tooltip>
      {onExpand && (
        <Tooltip content="展开查看" side="top">
          <button type="button" className="md-table-tool" aria-label="展开查看表格" onClick={onExpand}>
            <IconMaximize size={14} />
          </button>
        </Tooltip>
      )}
    </div>
  );
}

export function TableBlock({ children }: { children?: ReactNode }) {
  const tableRef = useRef<HTMLTableElement>(null);
  const modalTableRef = useRef<HTMLTableElement>(null);
  const [open, setOpen] = useState(false);
  const getTable = useCallback(() => tableRef.current, []);
  const getModalTable = useCallback(() => modalTableRef.current ?? tableRef.current, []);
  return (
    <div className="md-table-block">
      <TableTools getTable={getTable} onExpand={() => setOpen(true)} />
      <div className="md-table-card">
        <div className="md-table-scroll">
          <table className="md-table" ref={tableRef}>{children}</table>
        </div>
      </div>
      {/* 弹窗常驻挂载（内容只在打开时渲染）：关闭时能播放退场动画 */}
      <Dialog
        open={open}
        onClose={() => setOpen(false)}
        title="表格预览"
        subtitle="在更大的可滚动视图中查看表格。"
        width={1080}
        actions={<TableTools getTable={getModalTable} />}
        className="md-table-dialog"
        bodyClassName="md-table-dialog-body"
      >
        <div className="md-body md-table-modal">
          <div className="md-table-card">
            <div className="md-table-scroll">
              <table className="md-table" ref={modalTableRef}>{children}</table>
            </div>
          </div>
        </div>
      </Dialog>
    </div>
  );
}
