/** StreamingText（v20）：流式渲染共享组件——主会话与子代理面板共用。
 * 参数化数据源（thinking/text 由宿主注入），渲染与主界面同款：
 * - 思考块 active+open（IconBrain + 呼吸点 + 「思考中…」+ 内容滚动 maxHeight 160）
 * - Markdown 正文 + stream-caret 三点打字光标
 * - 无思考无文本时状态行「处理中…/等待响应…」（status-pulse）
 */
import { useEffect, useRef } from "react";
import { IconBrain } from "../icons";
import { MarkdownContent } from "../MarkdownContent";

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
}

export function StreamingText({ active, thinking, text, processingLabel = "处理中…", waitingLabel = "等待响应…" }: StreamingTextProps) {
  const bodyRef = useRef<HTMLDivElement>(null);
  const thinkingText = thinking.trim();

  // 思考流自动滚到底（zcode 风格）
  useEffect(() => {
    const el = bodyRef.current;
    if (el && thinkingText) {
      el.scrollTop = el.scrollHeight;
    }
  }, [thinkingText]);

  if (!active) return null;

  return (
    <div className="turn-group" style={{ minHeight: "36px", paddingBottom: 8 }}>
      {/* 当前轮次思考中：实时思考内容（思考落库后此块消失，思考块按时间顺序出现在消息流中） */}
      {thinkingText && (
        <div className="thinking-block active open">
          <div className="thinking-block-head" style={{ cursor: "default" }}>
            <span className="thinking-block-icon"><IconBrain size={12} /></span>
            <span className="thinking-block-title">
              <span className="thinking-block-breath" />
              <span className="thinking-block-status">思考中…</span>
            </span>
            <span className="thinking-block-chev" />
          </div>
          <div className="thinking-block-body" ref={bodyRef} style={{ maxHeight: 160, overflowY: "auto" }}>
            <pre className="thinking-block-content">{thinkingText}</pre>
          </div>
        </div>
      )}
      {text && (
        <div className="turn-item turn-item-text">
          <div className="turn-agent-text">
            <MarkdownContent>{text}</MarkdownContent>
            <span className="stream-caret"><i /><i /><i /></span>
          </div>
        </div>
      )}
      {!thinkingText && (
        <div className="turn-status-line">
          <span className="thinking-block-breath" style={{ marginRight: 6 }} />
          <span className="thinking-block-status">{text ? processingLabel : waitingLabel}</span>
        </div>
      )}
    </div>
  );
}
