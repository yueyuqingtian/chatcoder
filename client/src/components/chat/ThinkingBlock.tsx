/** ThinkingBlock（v4 r3 完全重写）：§3.3.1 思考块。
 * - 默认折叠态（思考中和完成后都折叠），用户可手动展开/折叠
 * - 完成后标题显示"已完成思考（n秒）"
 * - 流式内容从 store thinkingBuffers 读取（key=agent_id）
 */
import { useEffect, useRef, useState } from "react";
import { IconArrowToggle } from "../icons";
import { useChatStore } from "../../store/chat";

const MAX_HEIGHT = 160;

export function ThinkingBlock({ text, active, turnId, agentId }: {
  text: string;
  active: boolean;
  turnId?: number;
  agentId?: number;
}) {
  void turnId; // 保留 prop 用于未来按 turn 分组
  // v1.3: 默认折叠（包括思考中），用户可手动展开
  const [open, setOpen] = useState(false);
  const [duration, setDuration] = useState<number | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const startRef = useRef<number | null>(null);
  const prevLenRef = useRef(0);

  // §3.3.1: 流式内容从 store 读取，key=agent_id（与后端 thinking.delta 事件 payload.agent_id 对应）
  const streamText = useChatStore((s) => (agentId != null ? s.thinkingBuffers[agentId] : undefined));
  const displayText = active ? (streamText ?? text) : text;

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
    if (el && open && active && displayText.length !== prevLenRef.current) {
      el.scrollTop = el.scrollHeight;
      prevLenRef.current = displayText.length;
    }
  }, [displayText, active, open]);

  if (!active && !text) return null;

  return (
    <div className={`thinking-block${active ? " active" : ""}${open ? " open" : ""}`}>
      <button className="thinking-block-head" onClick={() => setOpen((v) => !v)}>
        <span className="thinking-block-chev">
          <IconArrowToggle open={open} size={12} />
        </span>
        <span className="thinking-block-title">
          {active ? (
            <>
              <span className="thinking-block-breath" />
              思考中…
            </>
          ) : (
            <>已完成思考{duration != null ? `（${duration} 秒）` : ""}</>
          )}
        </span>
      </button>
      {open && (
        <div className="thinking-block-body" ref={scrollRef} style={{ maxHeight: MAX_HEIGHT }}>
          <pre className="thinking-block-content">{displayText}</pre>
        </div>
      )}
    </div>
  );
}