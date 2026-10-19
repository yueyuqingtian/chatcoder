/** StreamingMarkdown（v40 → plan-329-1647 S10a → 流式 Markdown 统一策略）：流式增量 Markdown 渲染。
 * 主消息流（StreamingText）、子代理（经 StreamingTail → StreamingText）、右侧「侧边对话」（MiniChat）共用。
 * - 切块与半截结构处理全部在 streamMarkdown.ts（纯函数，有单测）：
 *   顶层空行 / 顶层围栏闭合处封块，只有「已换行的行」参与判定；表头行等分隔行成立后整表出现，
 *   未闭合围栏立即按代码块渲染，半截围栏标记等换行后再出现，不先显示成纯文本再跳变；
 * - **增量切块**：缓存上次文本与切块结果，仅扫描新增字符；非追加变化（新 turn / 落库整段替换）全量重算；
 * - 已封块按内容 memo 缓存解析结果（含高亮/公式），每帧只重解析尾块，长输出保持流畅；
 * - **尾块节流**：新增极少（<24 字）且距上次提交不足约 2 帧时延后合并，避免逐帧重解析；
 * - 尾块流式期间跳过 katex；代码块尾块（体量不大时）带语法高亮，封块后无缝换成完整插件；
 * - 落库后由 MarkdownContent 全量渲染接管，观感无缝衔接。
 */
import { memo, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeHighlight from "rehype-highlight";
import rehypeKatex from "rehype-katex";
import { markdownComponents } from "../MarkdownContent";
import { advanceSplit, prepareTail, type SplitState } from "./streamMarkdown";

/** 插件数组提到模块级：引用稳定，memo 的 props 比较与 react-markdown 内部都不必重建处理器 */
const PLAIN_REMARK = [remarkGfm];
const FULL_REMARK = [remarkGfm, remarkMath];
const NO_REHYPE: never[] = [];
const CODE_REHYPE = [rehypeHighlight];
const FULL_REHYPE = [rehypeHighlight, rehypeKatex];

/** 尾块代码块做语法高亮的体量上限：更长的输出每帧高亮成本偏高，封块后再高亮 */
const TAIL_HIGHLIGHT_MAX = 12000;

type BlockMode = "full" | "code" | "plain";

/** 单块渲染：memo 按内容缓存——封块（full，含高亮/公式）不再重复解析 */
const StreamBlock = memo(function StreamBlock({ source, mode }: { source: string; mode: BlockMode }) {
  return (
    <ReactMarkdown
      remarkPlugins={mode === "full" ? FULL_REMARK : PLAIN_REMARK}
      rehypePlugins={mode === "full" ? FULL_REHYPE : mode === "code" ? CODE_REHYPE : NO_REHYPE}
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
  const splitRef = useRef<SplitState | null>(null);

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

  const { sealed, tail, tailKind } = useMemo(() => {
    const st = advanceSplit(splitRef.current, shown);
    splitRef.current = st;
    const t = prepareTail(st.pending, st.pendingFence);
    return { sealed: st.sealed.slice(), tail: t.text, tailKind: t.kind };
  }, [shown]);

  return (
    <div className="md-body md-body-stream">
      {sealed.map((b, i) => (
        <StreamBlock key={i} source={b} mode="full" />
      ))}
      {/* 尾块 key 取封块数：封块时同一实例原地升级为完整插件渲染，不重新挂载 */}
      {tail && (
        <StreamBlock
          key={sealed.length}
          source={tail}
          mode={tailKind === "fence" && tail.length <= TAIL_HIGHLIGHT_MAX ? "code" : "plain"}
        />
      )}
    </div>
  );
});
