import { useEffect, useRef } from "react";
import { isBusy, subscribe } from "../perf/bus";

type SearchRanges = { ranges: Range[]; active: boolean };
type HighlightRegistry = { set(name: string, value: unknown): void; delete(name: string): void };
const roots = new Map<HTMLElement, SearchRanges>();

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
      const ranges: Range[] = [];
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        if (node.parentElement?.closest("script,style,button,textarea,input")) continue;
        const text = node.nodeValue ?? "";
        const lowered = text.toLowerCase();
        for (let index = lowered.indexOf(word); index >= 0 && index < text.length; index = lowered.indexOf(word, index + word.length)) {
          const range = document.createRange();
          range.setStart(node, index); range.setEnd(node, Math.min(text.length, index + word.length));
          ranges.push(range);
        }
      }
      roots.set(root, { ranges, active: isActiveMatch });
      publish();
    };
    const schedule = () => { if (!frame) frame = requestAnimationFrame(apply); };
    const observer = new MutationObserver(schedule);
    observer.observe(root, { subtree: true, childList: true, characterData: true });
    const off = subscribe((busy) => { if (!busy && missed) schedule(); });
    apply();
    return () => {
      observer.disconnect(); off(); if (frame) cancelAnimationFrame(frame);
      roots.delete(root); publish();
    };
  }, [keyword, isActiveMatch]);
  return ref;
}
