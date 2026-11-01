import { memo, useEffect, useMemo, useState } from "react";
import { ViewportBlock } from "./ViewportBlock";

let worker: Worker | null = null;
let serial = 0;
const pending = new Map<number, (lines: string[] | null) => void>();
const cache = new Map<string, { lines: string[]; chars: number }>();
let cacheChars = 0;

function highlight(source: string, language: string, done: (lines: string[] | null) => void): () => void {
  const key = language + "\0" + source;
  const hit = cache.get(key);
  if (hit) { cache.delete(key); cache.set(key, hit); done(hit.lines); return () => {}; }
  try {
    if (!worker) {
      worker = new Worker(new URL("./highlight.worker.ts", import.meta.url), { type: "module" });
      worker.onmessage = (event: MessageEvent<{ id: number; lines: string[] | null }>) => {
        const callback = pending.get(event.data.id);
        pending.delete(event.data.id);
        callback?.(event.data.lines);
      };
      worker.onerror = () => {
        for (const callback of pending.values()) callback(null);
        pending.clear(); worker?.terminate(); worker = null;
      };
    }
    const id = ++serial;
    pending.set(id, (lines) => {
      const chars = lines ? key.length + lines.reduce((sum, line) => sum + line.length, 0) : 0;
      if (lines && chars <= 512 * 1024 && !cache.has(key)) {
        cache.set(key, { lines, chars }); cacheChars += chars;
        while (cache.size > 16 || cacheChars > 1024 * 1024) {
          const oldest = cache.keys().next().value!;
          cacheChars -= cache.get(oldest)!.chars; cache.delete(oldest);
        }
      }
      done(lines);
    });
    worker.postMessage({ id, source, language });
    return () => { pending.delete(id); };
  } catch { done(null); return () => {}; }
}

/** plan-20261101-performance：只挂载附近的代码行，复制按钮仍取完整原文。 */
export const LongCode = memo(function LongCode({ source, language, live = false }: { source: string; language: string; live?: boolean }) {
  const [highlighted, setHighlighted] = useState<{ source: string; lines: string[] } | null>(null);
  useEffect(() => live ? undefined : highlight(source, language, (lines) => {
    if (lines) setHighlighted({ source, lines });
  }), [source, language, live]);
  const chunks = useMemo(() => {
    const html = highlighted?.source === source ? highlighted.lines : null;
    const lines = html ?? source.split("\n");
    const out: Array<{ content: string; count: number; html: boolean }> = [];
    for (let start = 0; start < lines.length; start += 60) {
      const part = lines.slice(start, start + 60);
      out.push({ content: part.join("\n"), count: part.length, html: html != null });
    }
    return out;
  }, [source, highlighted]);
  return <code className={`language-${language || "text"} md-long-code`}>{chunks.map((chunk, index) => (
    <ViewportBlock key={index} inline estimate={chunk.count * 20}>
      {chunk.html ? <span dangerouslySetInnerHTML={{ __html: chunk.content }} /> : chunk.content}
    </ViewportBlock>
  ))}</code>;
});
