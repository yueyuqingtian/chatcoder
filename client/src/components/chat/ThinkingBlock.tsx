/** ThinkingBlock（v4 r3 完全重写）：§3.3.1 思考块。
 * - 默认折叠态（思考中和完成后都折叠），用户可手动展开/折叠
 * - 完成后标题显示"已完成思考（n秒）"
 * - 流式内容从 store thinkingBuffers 读取（key=agent_id）
 */
import { memo, useEffect, useRef, useState } from "react";
import { IconArrowToggle, IconBrain } from "../icons";
import { useChatStore } from "../../store/chat";
import { useUiStore } from "../../store/ui";

const MAX_HEIGHT = 160;

export const ThinkingBlock = memo(function ThinkingBlock({ text, active, turnId, agentId }: {
  text: string;
  active: boolean;
  turnId?: number;
  agentId?: number;
}) {
  void turnId; // 保留 prop 用于未来按 turn 分组
  // v1.1: 常规设置"消息流显示 reasoning"关闭时隐藏历史思考块（运行中的实时思考流不受影响）
  const showReasoning = useUiStore((s) => s.showReasoning);
  // v1.3: 默认折叠（包括思考中），用户可手动展开
  const [open, setOpen] = useState(false);
  const [duration, setDuration] = useState<number | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const startRef = useRef<number | null>(null);
  const prevLenRef = useRef(0);

  // §3.3.1: 流式内容从 store 读取，key=agent_id（与后端 thinking.delta 事件 payload.agent_id 对应）
  const streamText = useChatStore((s) => (agentId != null ? s.thinkingBuffers[agentId] : undefined));
  // v7: 已落库的思考块只显示自己的内容；仅当本思考块尚无落库文本（仍在流式）时才用流式 buffer。
  // 避免多轮思考时，上一个已完成的思考块被最新 thinking buffer 覆盖，出现"上面还在思考、下面已有消息/工具调用"的错位观感。
  const displayText = text || streamText || "";
  const isStreaming = active && !text && (streamText ?? "").length > 0;

  // 计时（完成后记录耗时）
  useEffect(() => {
    if (active) {
      startRef.current = Date.now();
      setDuration(null);
    } else if (startRef.current) {
      setDuration(Math.round((Date.now() - startRef.current) / 1000));
      startRef.current = null;
    }
  }, [active]);

  // 流式自动滚到底（仅展开时）
  useEffect(() => {
    const el = scrollRef.current;
    if (el && open && displayText.length !== prevLenRef.current) {
      el.scrollTop = el.scrollHeight;
      prevLenRef.current = displayText.length;
    }
  }, [displayText, open]);

  if (!showReasoning || (!active && !text)) return null;

  return (
    <div className={`thinking-block${active ? " active" : ""}${open ? " open" : ""}`}>
      <button className="thinking-block-head" onClick={() => setOpen((v) => !v)}>
        <span className="thinking-block-icon"><IconBrain size={12} /></span>
        <span className="thinking-block-title">
          {isStreaming ? (
            <>
              <span className="thinking-block-breath" />
              <span className="thinking-block-status">思考中…</span>
            </>
          ) : (
            <>
              思考过程
              <span className="thinking-block-duration">{duration != null ? `持续了 ${duration} 秒` : "持续了几秒"}</span>
            </>
          )}
        </span>
        <span className="thinking-block-chev">
          <IconArrowToggle open={open} size={12} />
        </span>
      </button>
      {open && (
        <div className="thinking-block-body" ref={scrollRef} style={{ maxHeight: MAX_HEIGHT }}>
          <pre className="thinking-block-content">{displayText}</pre>
        </div>
      )}
    </div>
  );
});