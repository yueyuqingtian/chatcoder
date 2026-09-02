/** ThinkingBlock：思考块（与工具调用保持 100% 一致的平铺展示与展开卡片样式）。
 * - 默认折叠；思考中在「正在思考」右侧以单行 ticker 展示思考内容（滚动速度随产字速率自适应）；
 * - v41: 思考中同样可展开——展开后隐藏右侧刷字 ticker，展开块实时更新；
 *   自动滚动用户优先（上滑取消、滚回底部恢复，见 ThinkingExpanded/useAutoScroll）；
 * - 思考结束不在外部展示内容，仅保留「思考过程 持续了 N 秒」；
 * - 最右侧箭头与工具调用对齐，展开时顺畅旋转并展示内嵌 output 块。
 */
import { memo, useEffect, useRef, useState } from "react";
import { IconChevronRight, IconBrain } from "../icons";
import { ThinkingTicker } from "./ThinkingTicker";
import { ThinkingExpanded } from "./ThinkingExpanded";
import { useChatStore } from "../../store/chat";
import { useUiStore } from "../../store/ui";

export const ThinkingBlock = memo(function ThinkingBlock({ text, active, turnId, agentId }: {
  text: string;
  active: boolean;
  turnId?: number;
  agentId?: number;
}) {
  void turnId;
  const showReasoning = useUiStore((s) => s.showReasoning);
  const [open, setOpen] = useState(false);
  const [duration, setDuration] = useState<number | null>(null);
  const startRef = useRef<number | null>(null);

  const streamText = useChatStore((s) => (agentId != null ? s.thinkingBuffers[agentId] : undefined));
  const displayText = text || streamText || "";
  // v40: active 且尚未落库 = 思考中（首 token 到达前也显示「正在思考」+ 占位）
  const isThinking = active && !text;

  useEffect(() => {
    // 仅对「正在流式思考」的块计时；已落库文本（重挂载/新思考段接力）不再从头计时
    if (active && !text) {
      if (!startRef.current) startRef.current = Date.now();
      setDuration(null);
    } else if (!active && startRef.current) {
      setDuration(Math.max(1, Math.round((Date.now() - startRef.current) / 1000)));
      startRef.current = null;
    }
  }, [active, text]);

  if (!showReasoning || (!active && !text)) return null;

  return (
    <div className="tc-node thinking-node">
      <div
        className={`tc-row has-output thinking-row${open ? " expanded" : ""}${active ? " active" : ""}`}
        onClick={() => setOpen(!open)}
      >
        <span className="tc-icon"><IconBrain size={13} /></span>
        <span className="tc-verb">
          {isThinking ? "正在思考" : "思考过程"}
        </span>
        {isThinking ? (
          open ? (
            // v41: 展开时隐藏刷字 ticker，仅保留呼吸点作为活动指示
            <span className="thinking-ticker-wrap">
              <span className="thinking-breath-dot" />
            </span>
          ) : (
            <span className="thinking-ticker-wrap">
              <span className="thinking-breath-dot" />
              <ThinkingTicker text={displayText} />
            </span>
          )
        ) : (
          <span className="tc-query thinking-done-label">
            {duration != null ? `持续了 ${duration} 秒` : "持续了几秒"}
          </span>
        )}
        <span className={`tc-chevron${open ? " open" : ""}`}>
          <IconChevronRight size={11} />
        </span>
      </div>
      {open && <ThinkingExpanded text={displayText} />}
    </div>
  );
});
