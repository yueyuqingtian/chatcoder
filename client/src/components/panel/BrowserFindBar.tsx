import { useEffect, useRef, useState } from "react";
import { IconChevronDown, IconChevronUp, IconSearch, IconX } from "../icons";
import { Collapse, IconButton, Input } from "../ui";

export interface FindGuest extends EventTarget {
  findInPage: (text: string, options: { forward: boolean; findNext: boolean }) => number;
  stopFindInPage: (action: "clearSelection") => void;
}

export function BrowserFindBar({ open, frame, seed, onClose }: {
  open: boolean;
  frame: FindGuest | null;
  seed: string;
  onClose: () => void;
}) {
  const [query, setQuery] = useState("");
  const [match, setMatch] = useState({ current: 0, total: 0 });
  const input = useRef<HTMLInputElement>(null);
  const request = useRef(0);
  const search = (text: string, forward = true, fresh = true) => {
    if (!frame) return;
    try {
      if (text) request.current = frame.findInPage(text, { forward, findNext: fresh });
      else {
        frame.stopFindInPage("clearSelection");
        request.current = 0;
        setMatch({ current: 0, total: 0 });
      }
    } catch { /* 页面仍在加载 */ }
  };

  useEffect(() => {
    if (!open) return;
    setQuery(seed);
    setMatch({ current: 0, total: 0 });
    const raf = requestAnimationFrame(() => { input.current?.focus(); input.current?.select(); });
    return () => cancelAnimationFrame(raf);
  }, [open, seed]);

  useEffect(() => {
    if (!open || !frame) return;
    const onMatch = (event: Event) => {
      const { result } = event as Event & { result?: { requestId: number; matches: number; activeMatchOrdinal: number; finalUpdate: boolean } };
      if (result?.requestId === request.current && result.finalUpdate) {
        setMatch({ current: Math.max(0, result.activeMatchOrdinal), total: Math.max(0, result.matches) });
      }
    };
    frame.addEventListener("found-in-page", onMatch);
    return () => {
      frame.removeEventListener("found-in-page", onMatch);
      try { frame.stopFindInPage("clearSelection"); } catch { /* 标签已关闭 */ }
    };
  }, [open, frame]);

  useEffect(() => {
    if (open) search(query);
  }, [query, open, frame]);

  return <Collapse open={open} lazy>
    <div className="bp-findbar">
      <Input ref={input} size="sm" value={query} onChange={(event) => setQuery(event.target.value)}
        prefix={<IconSearch size={13} />} aria-label="页内查找" placeholder="在此页查找"
        onKeyDown={(event) => {
          if (event.key === "Enter") { event.preventDefault(); search(query, !event.shiftKey, false); }
          if (event.key === "Escape") { event.stopPropagation(); onClose(); }
        }} />
      <span className="bp-find-count" aria-live="polite">{query ? `${match.current}/${match.total}` : ""}</span>
      <IconButton size="xs" title="上一处" icon={<IconChevronUp size={13} />} disabled={!query} onClick={() => search(query, false, false)} />
      <IconButton size="xs" title="下一处" icon={<IconChevronDown size={13} />} disabled={!query} onClick={() => search(query, true, false)} />
      <IconButton size="xs" title="关闭查找" icon={<IconX size={13} />} onClick={onClose} />
    </div>
  </Collapse>;
}
