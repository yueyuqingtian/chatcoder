import { useEffect, useRef } from "react";
import { isBusy, subscribe } from "../perf/bus";

type SearchRanges = { ranges: Range[]; active: boolean };
type HighlightRegistry = { set(name: string, value: unknown): void; delete(name: string): void };
const roots = new Map<HTMLElement, SearchRanges>();

/** 折叠态 / 装饰性区域：里面的文字用户看不见，既不该高亮、也不该计入命中。
 *
 *  此前只排除 script/style/button/textarea/input，于是折叠起来的工具输出、思考全文
 *  （收起后 DOM 在过渡期仍然挂载，非 lazy 的 Collapse 更是常驻）也会被搜到并高亮——
 *  用户看到的是「计数说有命中，屏幕上却找不到」。 */
const HIDDEN = [
  "script", "style", "noscript", "button", "textarea", "input", "select",
  "[inert]",
  ".turn-process-container.collapsed",
  '.tc-collapse[data-open="false"]',
  '.ui-collapse[data-open="false"]',
  '.collapse-grid[data-open="false"]',
].join(",");

/** display 结果缓存：同一批节点会反复经过这些容器，逐次 getComputedStyle 太浪费 */
const inlineMemo = new WeakMap<Element, boolean>();

/** 元素是否 inline 级（contents 没有自己的盒子，子节点直接参与父级文本流） */
function isInline(el: Element): boolean {
  const cached = inlineMemo.get(el);
  if (cached !== undefined) return cached;
  const display = getComputedStyle(el).display;
  const value = display === "inline" || display === "contents";
  inlineMemo.set(el, value);
  return value;
}

/** 两个相邻文本节点是否处在同一段 inline 文本流里（中间没有块级断点）。
 *
 *  是 → 拼接时不插分隔符，于是 **关键词**、行内 code、highlight.js 把代码拆成的多个
 *  节点都能连起来命中；否 → 插入分隔符，避免跨段落拼出并不存在的假命中。
 *
 *  判定口径：两节点向上到最近公共祖先（LCA）的路径上，每一层都必须是 inline 级元素。
 *  同一父元素下的两个文本节点天然同流（父级即使是块级，它们仍在同一行内）。 */
function sameInlineFlow(a: Text, b: Text): boolean {
  const pa = a.parentElement;
  if (!pa) return false;
  if (pa === b.parentElement) return true;
  const chainA: Element[] = [];
  for (let el: Element | null = pa; el; el = el.parentElement) chainA.push(el);
  const ancestors = new Set(chainA);
  let lca: Element | null = null;
  for (let el: Element | null = b.parentElement; el; el = el.parentElement) {
    if (ancestors.has(el)) { lca = el; break; }
  }
  if (!lca) return false;
  for (let el: Element | null = pa; el && el !== lca; el = el.parentElement) if (!isInline(el)) return false;
  for (let el: Element | null = b.parentElement; el && el !== lca; el = el.parentElement) if (!isInline(el)) return false;
  return true;
}

/** 元素自身是否可见——不支持 checkVisibility 的环境直接放行（退回按折叠态排除） */
function isVisible(el: Element): boolean {
  const probe = (el as Element & { checkVisibility?: unknown }).checkVisibility;
  if (typeof probe !== "function") return true;
  return (probe as () => boolean).call(el);
}

/** 把一条命中（拼接串里的 [from, to)）按文本节点切段，每段各建一个 Range。 */
function pushRanges(spans: Array<{ node: Text; start: number }>, from: number, to: number, out: Range[]) {
  for (const { node, start } of spans) {
    const length = node.nodeValue?.length ?? 0;
    if (start + length <= from) continue;
    if (start >= to) break;
    const localFrom = Math.max(from, start) - start;
    const localTo = Math.min(to, start + length) - start;
    if (localTo <= localFrom) continue;
    const range = document.createRange();
    range.setStart(node, localFrom);
    range.setEnd(node, localTo);
    out.push(range);
  }
}

/** plan-20261101-performance：原生高亮不改 React 管理的文本节点，离屏块挂卸载也不会破坏 DOM。 */
function publish() {
  const registry = (globalThis.CSS as typeof CSS & { highlights?: HighlightRegistry })?.highlights;
  const HighlightClass = (globalThis as typeof globalThis & { Highlight?: new () => { add(range: Range): void } }).Highlight;
  if (!registry || !HighlightClass) return;
  const normal = new HighlightClass(), active = new HighlightClass();
  for (const entry of roots.values()) for (const range of entry.ranges) (entry.active ? active : normal).add(range);
  registry.set("chatcoder-search", normal);
  registry.set("chatcoder-search-active", active);
}

export function useTextHighlight<T extends HTMLElement>(keyword: string, isActiveMatch: boolean) {
  const ref = useRef<T>(null);
  useEffect(() => {
    const root = ref.current;
    const word = keyword.trim().toLowerCase();
    if (!root || !word) return;
    let frame = 0;
    let missed = false;
    const apply = () => {
      frame = 0;
      if (isBusy()) { missed = true; return; }
      missed = false;
      // 1) 收集可见文本节点（跳过折叠区与不可见元素）
      const nodes: Text[] = [];
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        const text = node as Text;
        const host = text.parentElement;
        if (!host || !text.nodeValue) continue;
        if (host.closest(HIDDEN)) continue;
        if (!isVisible(host)) continue;
        nodes.push(text);
      }
      // 2) 按文档序拼成一条虚拟串：inline 相邻直接相连、块边界插 \n
      const spans: Array<{ node: Text; start: number }> = [];
      let haystack = "";
      for (let i = 0; i < nodes.length; i++) {
        if (i > 0 && !sameInlineFlow(nodes[i - 1], nodes[i])) haystack += "\n";
        spans.push({ node: nodes[i], start: haystack.length });
        haystack += nodes[i].nodeValue ?? "";
      }
      // 3) 在整串上找命中，再映射回各节点——跨节点的命中不会被漏掉
      const lowered = haystack.toLowerCase();
      const ranges: Range[] = [];
      for (let index = lowered.indexOf(word); index >= 0; index = lowered.indexOf(word, index + word.length)) {
        pushRanges(spans, index, index + word.length, ranges);
      }
      roots.set(root, { ranges, active: isActiveMatch });
      publish();
    };
    const schedule = () => { if (!frame) frame = requestAnimationFrame(apply); };
    /* 属性也要看：折叠容器开合往往只改属性 / class（内容已常驻挂载时没有 childList 变化），
     * 不观察就永远不会重建——展开后的命中仍是一片空白。
     *
     * class 单独收紧：流式输出的动画类每帧都在变，只有折叠容器的 class 才代表可见性变化
     * （TurnGroup 的 ProcessCollapse 用 `.turn-process-container.collapsed`，不是 data-open）。 */
    const observer = new MutationObserver((records) => {
      for (const record of records) {
        if (record.type === "attributes" && record.attributeName === "class") {
          if (!(record.target instanceof Element) || !record.target.matches(".turn-process-container")) continue;
        }
        schedule();
        return;
      }
    });
    observer.observe(root, {
      subtree: true, childList: true, characterData: true,
      attributes: true, attributeFilter: ["data-open", "aria-hidden", "hidden", "inert", "class"],
    });
    const off = subscribe((busy) => { if (!busy && missed) schedule(); });
    apply();
    return () => {
      observer.disconnect(); off(); if (frame) cancelAnimationFrame(frame);
      roots.delete(root); publish();
    };
  }, [keyword, isActiveMatch]);
  return ref;
}
