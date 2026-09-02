/** StreamingMarkdown（v40）：流式增量 Markdown 渲染——StreamingText 专用。
 * - 按顶层空行切块（感知未闭合 ``` 围栏，不在代码块内部切分）；
 * - 已稳定块按内容 memo 缓存解析结果，每帧仅重解析最后一块，消除"全量重解析"卡顿；
 * - 尾块流式期间跳过 highlight/katex 昂贵插件，块完成后自动以完整插件渲染一次；
 * - 落库后由 MarkdownContent 全量渲染接管，观感无缝衔接。
 */
import { memo, useMemo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeHighlight from "rehype-highlight";
import rehypeKatex from "rehype-katex";
import { markdownComponents } from "../MarkdownContent";

/** 按顶层空行切块：跟踪 ```/~~~ 围栏状态，围栏内空行不切分 */
function splitBlocks(src: string): string[] {
  if (!src) return [];
  const out: string[] = [];
  let buf = "";
  let inFence = false;
  for (const line of src.split("\n")) {
    if (/^\s*(```|~~~)/.test(line)) inFence = !inFence;
    if (!inFence && line.trim() === "" && buf.trim() !== "") {
      out.push(buf);
      buf = "";
      continue;
    }
    buf += (buf ? "\n" : "") + line;
  }
  if (buf.trim()) out.push(buf);
  return out;
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

export const StreamingMarkdown = memo(function StreamingMarkdown({ children }: { children: string }) {
  const blocks = useMemo(() => splitBlocks(children), [children]);
  return (
    <div className="md-body md-body-stream">
      {blocks.map((b, i) => (
        <StreamBlock key={i} source={b} full={i < blocks.length - 1} />
      ))}
    </div>
  );
});
