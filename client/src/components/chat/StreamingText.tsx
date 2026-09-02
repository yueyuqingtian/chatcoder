/** StreamingText（v20→v40）：流式渲染共享组件——主会话与子代理面板共用。
 * - 思考中：与 ThinkingBlock 同款紧凑行（脑图标 +「正在思考」+ 单行 ticker 随产字速度滚动），
 *   不再展示展开式 mono 大块，排版与工具行完全一致；
 * - 正文：块缓存增量 Markdown（StreamingMarkdown）+ 自适应"产字"平滑 + stream-caret 三点光标；
 * - 无思考无文本时状态行「处理中…/等待响应…」（status-pulse）。
 */
import { useEffect, useRef, useState } from "react";
import { IconBrain, IconChevronRight } from "../icons";
import { StreamingMarkdown } from "./StreamingMarkdown";
import { ThinkingTicker } from "./ThinkingTicker";
import { ThinkingExpanded } from "./ThinkingExpanded";

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
    const tick = () => {
      const t = targetRef.current;
      let cur = shownRef.current;
      if (!t.startsWith(cur) || t.length < cur.length) {
        cur = t; // 内容重置（新 turn / 落库清缓冲）：直接对齐
      } else if (cur.length < t.length) {
        const backlog = t.length - cur.length;
        const step = Math.min(backlog, Math.max(3, Math.ceil(backlog * 0.18)));
        cur = t.slice(0, cur.length + step);
      }
      if (cur !== shownRef.current) {
        shownRef.current = cur;
        setShown(cur);
      }
      if (shownRef.current.length < targetRef.current.length) raf = requestAnimationFrame(tick);
    };
    if (shownRef.current.length < target.length || !target.startsWith(shownRef.current)) tick();
    return () => cancelAnimationFrame(raf);
  }, [target]);
  return shown;
}

export function StreamingText({ active, thinking, text, processingLabel = "处理中…", waitingLabel = "等待响应…", statusLabel }: StreamingTextProps) {
  const thinkingText = thinking.trim();
  const smoothThinking = useSmoothText(thinkingText);
  const smoothBody = useSmoothText(text);
  // v41: 思考尾部行可展开——展开后隐藏右侧刷字 ticker，下方渲染实时更新的思考全文
  const [tailOpen, setTailOpen] = useState(false);

  if (!active) return null;

  return (
    <div className="turn-group turn-flow streaming-tail">
      {/* 当前轮次思考中：紧凑行（可点击展开）+ 单行滚动 ticker（思考落库后此行消失，思考块按时间顺序出现在消息流中） */}
      {smoothThinking && (
        <div className="tc-node thinking-node">
          <div
            className={`tc-row has-output thinking-row${tailOpen ? " expanded" : ""} active`}
            onClick={() => setTailOpen((v) => !v)}
          >
            <span className="tc-icon"><IconBrain size={13} /></span>
            <span className="tc-verb">正在思考</span>
            {!tailOpen && (
              <span className="thinking-ticker-wrap">
                <span className="thinking-breath-dot" />
                <ThinkingTicker text={smoothThinking} />
              </span>
            )}
            <span className={`tc-chevron${tailOpen ? " open" : ""}`}>
              <IconChevronRight size={11} />
            </span>
          </div>
          {tailOpen && <ThinkingExpanded text={smoothThinking} />}
        </div>
      )}
      {smoothBody && (
        <div className="turn-item turn-item-text">
          <div className="turn-agent-text">
            <StreamingMarkdown>{smoothBody}</StreamingMarkdown>
            <span className="stream-caret"><i /><i /><i /></span>
          </div>
        </div>
      )}
      {statusLabel ? (
        // v35: 重试/恢复等瞬态状态——无论有无思考/正文都展示在状态行
        <div className="turn-status-line">
          <span className="thinking-breath-dot" style={{ marginRight: 6 }} />
          <span className="thinking-block-status">{statusLabel}</span>
        </div>
      ) : (!smoothThinking && (
        <div className="turn-status-line">
          <span className="thinking-breath-dot" style={{ marginRight: 6 }} />
          <span className="thinking-block-status">{smoothBody ? processingLabel : waitingLabel}</span>
        </div>
      ))}
    </div>
  );
}
