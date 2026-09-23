/** StreamingMarkdown（v40 → plan-329-1647 S10a）：流式增量 Markdown 渲染——StreamingText 专用。
 * - 按顶层空行切块（感知未闭合 ``` 围栏，不在代码块内部切分）；
 * - **增量切块**：缓存上次文本与切块结果，仅扫描新增字符（流式文本恒为"上次 + 新增"）；
 *   文本不再以上次为前缀（新 turn / 落库整段替换）时自动退回全量重算；
 * - 已稳定块按内容 memo 缓存解析结果，每帧仅重解析最后一块，消除"全量重解析"卡顿；
 * - **尾块节流**：新增极少（<24 字）且距上次提交不足约 2 帧时延后合并，避免逐帧重解析；
 * - 尾块流式期间跳过 highlight/katex 昂贵插件，块完成后自动以完整插件渲染一次；
 * - 落库后由 MarkdownContent 全量渲染接管，观感无缝衔接。
 */
import { memo, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeHighlight from "rehype-highlight";
import rehypeKatex from "rehype-katex";
import { markdownComponents } from "../MarkdownContent";

/** 增量切块缓存：已完成块 + 尾块（尚未遇到顶层空行）+ 续扫所需的围栏状态。 */
interface BlockCache {
  /** 上次输入文本（用于判断是否只是"追加"） */
  src: string;
  /** 已完成的块（遇到顶层空行即封块） */
  blocks: string[];
  /** 当前未完成的尾块（其内无顶层空行） */
  pending: string;
  /** 扫描结束时的围栏状态（尾块内的 ``` 代码块是否未闭合） */
  inFence: boolean;
}

/** 扫描一段文本：把新封的块推进 cache.blocks，剩余部分留在 cache.pending。
 *  与原全量实现同构（逐行 + 围栏跟踪 + 顶层空行切分），只是从"上次停下的地方"续扫。 */
function scanTail(cache: BlockCache, tail: string): void {
  let buf = "";
  let inFence = cache.inFence;
  for (const line of tail.split("\n")) {
    if (/^\s*(```|~~~)/.test(line)) inFence = !inFence;
    if (!inFence && line.trim() === "" && buf.trim() !== "") {
      cache.blocks.push(buf);
      buf = "";
      continue;
    }
    buf += (buf ? "\n" : "") + line;
  }
  cache.inFence = inFence;
  cache.pending = buf;
}

/** 全量扫描：初始化缓存（首次挂载 / 非追加变化时使用）。 */
function scanFull(src: string): BlockCache {
  const cache: BlockCache = { src, blocks: [], pending: "", inFence: false };
  scanTail(cache, src);
  return cache;
}

/** 增量切块入口：追加场景只扫「尾块 + 新增」，把 O(全文) 降为 O(新增)。 */
function splitBlocksCached(cacheRef: { current: BlockCache | null }, src: string): string[] {
  const cached = cacheRef.current;
  if (!cached || !src.startsWith(cached.src)) {
    cacheRef.current = scanFull(src);
  } else if (src !== cached.src) {
    const delta = src.slice(cached.src.length);
    cached.src = src;
    scanTail(cached, cached.pending + delta);
  }
  const c = cacheRef.current!;
  // 返回新数组：块数通常很小（几十），避免调用方持有被后续 push 改动的数组
  return c.pending ? [...c.blocks, c.pending] : c.blocks.slice();
}

/** 单块渲染：memo 按内容缓存——稳定块（full=true，含高亮/公式）不再重复解析 */
const StreamBlock = memo(function StreamBlock({ source, full }: { source: string; full: boolean }) {
  return (
    <ReactMarkdown
      remarkPlugins={full ? [remarkGfm, remarkMath] : [remarkGfm]}
      rehypePlugins={full ? [rehypeHighlight, rehypeKatex] : []}
      components={markdownComponents}
    >
      {source}
    </ReactMarkdown>
  );
});

/** S10a 尾块节流参数：新增不足 MIN_CHARS 且距上次提交不足 MIN_GAP_MS 时延后合并。
 *  DEFER_MS 是兜底——保证"最后一次小增量"一定被提交，不会永久丢字。 */
const THROTTLE_MIN_CHARS = 24;
const THROTTLE_MIN_GAP_MS = 32; // ≈ 2 帧
const THROTTLE_DEFER_MS = 40;

export const StreamingMarkdown = memo(function StreamingMarkdown({ children }: { children: string }) {
  // S10a：渲染文本（shown）与输入（children）解耦——小增量高频到达时不必逐帧重解析尾块。
  const [shown, setShown] = useState(children);
  const shownRef = useRef(children);
  /** 最新输入（defer 定时器到期时提交它，避免用过期文本回退） */
  const latestRef = useRef(children);
  latestRef.current = children;
  const lastCommitRef = useRef(0);
  const deferTimerRef = useRef(0);
  const cacheRef = useRef<BlockCache | null>(null);

  useEffect(() => {
    const commit = (text: string) => {
      if (deferTimerRef.current) { window.clearTimeout(deferTimerRef.current); deferTimerRef.current = 0; }
      shownRef.current = text;
      lastCommitRef.current = performance.now();
      setShown(text);
    };
    const prev = shownRef.current;
    if (children === prev) return;
    const delta = children.length - prev.length;
    // 非追加（新 turn / 落库整段替换 / 清空）：立即对齐，不节流
    if (delta <= 0 || !children.startsWith(prev)) { commit(children); return; }
    // 尾块节流：小增量且间隔很近 → 延后到 DEFER_MS 后提交（提交时取最新文本）
    if (delta < THROTTLE_MIN_CHARS && performance.now() - lastCommitRef.current < THROTTLE_MIN_GAP_MS) {
      if (!deferTimerRef.current) {
        deferTimerRef.current = window.setTimeout(() => {
          deferTimerRef.current = 0;
          commit(latestRef.current);
        }, THROTTLE_DEFER_MS);
      }
      return;
    }
    commit(children);
  }, [children]);

  // 卸载清理兜底定时器
  useEffect(() => () => { if (deferTimerRef.current) window.clearTimeout(deferTimerRef.current); }, []);

  const blocks = useMemo(() => splitBlocksCached(cacheRef, shown), [shown]);

  return (
    <div className="md-body md-body-stream">
      {blocks.map((b, i) => (
        <StreamBlock key={i} source={b} full={i < blocks.length - 1} />
      ))}
    </div>
  );
});
