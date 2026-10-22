/** StreamingText（v20→v40）：流式渲染共享组件——主会话与子代理面板共用。
 * - 思考中：与 ThinkingBlock 同款紧凑行（脑图标 +「正在思考」+ 单行 ticker 随产字速度滚动），
 *   不再展示展开式 mono 大块，排版与工具行完全一致；
 * - 正文：块缓存增量 Markdown（StreamingMarkdown）+ 自适应"产字"平滑；
 * - 状态行：统一"进行中"提示（呼吸点 + 文案），四态共用——重试/事务提示（statusLabel）、
 *   有正文「处理中…」、无内容「等待响应…」。正文与思考流并存时尾部同样保留该行，
 *   不与上方思考行互相抑制（plan-282-1421 第4项：三点光标已移除，由本行统一承担）。
 */
import { ToolGlyph } from "../iconkit/ToolGlyphs";
import { useEffect, useMemo, useRef, useState } from "react";
import { rewriteDsmlForStream } from "./dsmlStream";
import { IconChevronRight } from "../icons";
import { StreamingMarkdown } from "./StreamingMarkdown";
import { isBusy } from "../../perf/bus";
import { ThinkingTicker } from "./ThinkingTicker";
import { ThinkingExpanded } from "./ThinkingExpanded";
import { ChatCollapse } from "./ChatCollapse";
import { useLiveThinkingExpand } from "../../store/flowExpand";

export interface StreamingTextProps {
  /** 是否处于运行中（不运行返回 null） */
  active: boolean;
  /** 实时思考内容（空字符串则不显示思考块） */
  thinking: string;
  /** 实时正文（空字符串则不显示正文） */
  text: string;
  /** 有正文时的处理中文案（默认「处理中…」） */
  processingLabel?: string;
  /** 无任何内容时的等待文案（默认「等待响应…」） */
  waitingLabel?: string;
  /** v35: turn 级瞬态状态提示（如「调用异常，正在重试 1/2…」）；设置时优先展示，覆盖默认文案 */
  statusLabel?: string;
  /** 子代理线程 id（主会话不传）：流式思考的展开态按线程区分 */
  threadId?: number;
}

/** v40: 自适应"产字"平滑——delta 常成簇到达，按积压量比例逐帧释放，
 * 文字匀速出现而非成段跳变（追赶速率 backlog×12%/帧，延迟 ≲200ms，落库时整段对齐）。 */
function useSmoothText(target: string): string {
  const [shown, setShown] = useState(target);
  const shownRef = useRef(target);
  const targetRef = useRef(target);
  targetRef.current = target;
  useEffect(() => {
    let raf = 0;
    let waitingForMotion = false;
    // 判定统一走 PerfBus（S3）：窗口运动 / 拖分隔条 / 面板过渡三源合并
    const isMotion = () => isBusy();
    const schedule = () => {
      if (!raf) raf = requestAnimationFrame(tick);
    };
    const onMotion = (e: Event) => {
      if ((e as CustomEvent<{ active?: boolean }>).detail?.active === false) resume();
    };
    const onPointerUp = () => { if (!isMotion()) resume(); };
    const onPanelDragEnd = () => { if (!isMotion()) resume(); };
    const resume = () => {
      if (!waitingForMotion) return;
      waitingForMotion = false;
      window.removeEventListener("chatcoder:window-motion", onMotion);
      window.removeEventListener("pointerup", onPointerUp, true);
      window.removeEventListener("chatcoder:panel-drag-end", onPanelDragEnd);
      schedule();
    };
    const tick = () => {
      raf = 0;
      if (isMotion()) {
        // 不在每一帧 setState / 重解析 markdown；只保留最新 target，结束事件再追平。
        if (!waitingForMotion) {
          waitingForMotion = true;
          window.addEventListener("chatcoder:window-motion", onMotion);
          window.addEventListener("pointerup", onPointerUp, true);
          window.addEventListener("chatcoder:panel-drag-end", onPanelDragEnd);
        }
        return;
      }
      const t = targetRef.current;
      let cur = shownRef.current;
      if (!t.startsWith(cur) || t.length < cur.length) {
        cur = t; // 内容重置（新 turn / 落库清缓冲）：直接对齐
      } else {
        const backlog = t.length - cur.length;
        // 窗口几何运动期间流式更新会暂停，结束后可能积累了较大 backlog；
        // 超过 240 字直接一次追平，避免动画结束后又占据多个帧造成二次掉帧。
        const step = backlog > 240 ? backlog : Math.min(backlog, Math.max(3, Math.ceil(backlog * 0.18)));
        cur = t.slice(0, cur.length + step);
      }
      if (cur !== shownRef.current) {
        shownRef.current = cur;
        setShown(cur);
      }
      if (shownRef.current.length < targetRef.current.length) schedule();
    };
    if (shownRef.current.length < target.length || !target.startsWith(shownRef.current)) schedule();
    return () => {
      cancelAnimationFrame(raf);
      if (waitingForMotion) {
        window.removeEventListener("chatcoder:window-motion", onMotion);
        window.removeEventListener("pointerup", onPointerUp, true);
        window.removeEventListener("chatcoder:panel-drag-end", onPanelDragEnd);
      }
    };
  }, [target]);
  return shown;
}

export function StreamingText({ active, thinking, text, processingLabel = "处理中…", waitingLabel = "等待响应…", statusLabel, threadId }: StreamingTextProps) {
  const thinkingText = thinking.trim();
  // plan-511-2182：思考行是否出现改用原始缓冲判定。平滑器要下一帧才追上 target，
  // 若仍用平滑后的文本，状态行卸载与思考行出现会落在两次提交里，中间出现高度为 0 的空窗。
  // 用原始缓冲后，状态切换与数据到达同帧；思考落库（缓冲清空）时本行同样在同一次渲染里卸载。
  const hasThinking = thinkingText !== "";
  const smoothThinking = useSmoothText(thinkingText);
  const smoothBody = useSmoothText(text);
  // plan-511-2182：正文可见性与正文渲染同帧判定，避免槽位折叠与正文出现错帧
  const hasBody = smoothBody !== "";
  // plan-511-2182：槽位内「等待状态行」与「正在思考行」两层同格叠放，槽位高度由格内最高层决定。
  // 状态层只在既无思考也无正文时显示；思考行一到就由它接棒，状态层淡出但不退出布局，
  // 于是槽位在任何时刻都至少撑起一行，切换过程中没有 0 高度窗口。
  const slotStateShown = !hasThinking && !hasBody;
  // plan-511-2182：有正文且无思考时槽位整体折叠为 0（样式侧同时抵消它在 flex gap 中的一份间距），
  // 否则正文上方会多留一行空白。
  const slotEmpty = !hasThinking && hasBody;
  // plan-511-2182：尾部状态行位置语义不变——有正文时固定在正文下方；无正文时只有瞬时提示
  // （重试 / 压缩等 statusLabel）才与思考行并存，默认「等待响应…」交给槽位状态层，避免两行重复。
  const tailStateShown = hasBody || (hasThinking && Boolean(statusLabel));
  // plan-20261016-ux（问题 5）：正文里以 DSML 标签写出的工具调用（DeepSeek / 智谱）在流式期间改写成
  // 「正在准备 …」+ 代码块——正在输出的代码直接进代码框，而不是夹着标签的普通文本（见 dsmlStream）
  const displayBody = useMemo(() => rewriteDsmlForStream(smoothBody), [smoothBody]);

  if (!active) return null;

  return (
    <div className="turn-group turn-flow streaming-tail">
      {/* plan-511-2182：把「等待响应…」状态行与「正在思考」紧凑行放进同一个 grid 单元（同格叠放），
          槽位在任何时刻都至少撑起一行。原来状态行是 .turn-flow 的独立子项，思考行出现的那一帧
          状态行被卸载、而 ChatCollapse 的 children 要到下一次提交才挂载，中间出现 0 高度空窗，
          虚拟列表两次测高、贴底跟随两次修正，看起来就是整块先下移一行再上移一行。
          叠放后状态层只淡出、不退出布局，高度由格内最高层决定，空窗不存在。 */}
      <div className="stream-state-slot" data-empty={slotEmpty ? "true" : undefined}>
        <div className="stream-state-grid">
          {/* 思考层：可点击展开 + 单行滚动 ticker（思考落库后此行消失，思考块按时间顺序出现在消息流中）；
              plan-511-2182：槽位内部是 grid 且无 gap，折叠间距只需由槽位自己抵消，无需再给折叠容器加间距类 */}
          <div className="stream-state-layer" data-show={hasThinking ? "true" : "false"}>
            <ChatCollapse open={hasThinking}>
              <StreamingThinkingRow text={smoothThinking} threadId={threadId} />
            </ChatCollapse>
          </div>
          {/* 状态层：思考行出现前的「等待响应…」/ 瞬时提示；被思考层接棒后仍留在格内撑住一行高度 */}
          <div className="stream-state-layer" data-show={slotStateShown ? "true" : "false"}>
            <StateLine label={statusLabel ?? waitingLabel} />
          </div>
        </div>
      </div>
      {displayBody && (
        <div className="turn-item turn-item-text">
          <div className="turn-agent-text">
            <StreamingMarkdown>{displayBody}</StreamingMarkdown>
            {/* plan-282-1421（第4项）：移除流式尾部三点光标（.stream-caret）。
                它与状态行的呼吸点在语义上重复、且不同步；现统一由下方状态行
                （呼吸点 + 文案）承担"进行中"表达，视觉只保留一种语言。 */}
          </div>
        </div>
      )}
      {/* plan-282-1421（第4项）：进行中状态统一为**唯一一条**状态行。
          四态（重试中/处理中/等待响应/补充提示）共用同一结构与同一动画，
          不再与三点光标并存。

          修复：门控此前写作 `statusLabel || !smoothThinking`——只要存在思考流，
          状态行被整条跳过。而思考行渲染在**正文上方**，当「思考流 + 正文同时产出」
          （典型：AI 边思考边写方案文档/正文）时，正文尾部既无三点光标、又无状态行，
          消息流底部完全静默，用户看不到"还在进行"。

          plan-511-2182：门控收敛为 tailStateShown——有正文时本条固定在正文下方；
          无正文时只有 statusLabel 才与思考行并存，默认「等待响应…」已移交槽位状态层，
          语义与改动前一致，且不再需要与思考行互相抑制。 */}
      {tailStateShown && <StateLine label={statusLabel ?? processingLabel} />}
    </div>
  );
}

/** 思考中的紧凑行：可展开查看实时更新的思考全文。展开态按流式键记在 flowExpand 中——
 *  思考落库时移交给落库后的思考块（同一段思考），本行随之卸载；下一段思考从收起开始。 */
function StreamingThinkingRow({ text, threadId }: { text: string; threadId?: number }) {
  // v41: 展开后隐藏右侧刷字 ticker，下方渲染实时更新的思考全文
  const [open, setOpen] = useLiveThinkingExpand(threadId);
  return (
    <div className="tc-node thinking-node">
      <div
        className={`tc-row has-output thinking-row${open ? " expanded" : ""} active`}
        data-tool-state="running"
        onClick={() => setOpen(!open)}
      >
        <span className="tc-icon"><ToolGlyph tool="thinking" size={16} /></span>
        <span className="tc-verb">正在思考</span>
        {!open && (
          <span className="thinking-ticker-wrap">
            <span className="thinking-breath-dot" />
            <ThinkingTicker text={text} />
          </span>
        )}
        <span className={`tc-chevron${open ? " open" : ""}`}>
          <IconChevronRight size={11} />
        </span>
      </div>
      <ChatCollapse open={open}>
        <ThinkingExpanded text={text} />
      </ChatCollapse>
    </div>
  );
}

/** plan-91-393：消息流底部「进行中」标记——三枚圆点沿正弦依次起伏并在末端汇成光标，
 *  像对方正在输入；颜色跟随主题强调色，动效时长走令牌（降低动效时静止）。 */
function WaitMark() {
  return (
    <svg className="wait-mark" width="22" height="12" viewBox="0 0 22 12" aria-hidden>
      <circle className="wm-dot" cx="3" cy="6" r="2" style={{ animationDelay: "0ms" }} />
      <circle className="wm-dot" cx="9" cy="6" r="2" style={{ animationDelay: "140ms" }} />
      <circle className="wm-dot" cx="15" cy="6" r="2" style={{ animationDelay: "280ms" }} />
      <rect className="wm-caret" x="19.5" y="1.5" width="1.6" height="9" rx="0.8" />
    </svg>
  );
}

/** 统一"进行中"状态行：呼吸点 + 文案（四态共用，唯一样式出口）。 */
function StateLine({ label }: { label: string }) {
  return (
    <div className="turn-status-line">
      <WaitMark />
      <span className="thinking-block-status">{label}</span>
    </div>
  );
}
