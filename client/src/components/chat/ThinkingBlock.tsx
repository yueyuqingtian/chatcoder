/** ThinkingBlock：思考块（与工具调用保持 100% 一致的平铺展示与展开卡片样式）。
 * - 默认折叠；思考中在「正在思考」右侧以单行 ticker 展示思考内容（滚动速度随产字速率自适应）；
 * - v41: 思考中同样可展开——展开后隐藏右侧刷字 ticker，展开块实时更新；
 *   自动滚动用户优先（上滑取消、滚回底部恢复，见 ThinkingExpanded/useAutoScroll）；
 * - 思考结束不在外部展示内容，仅保留「思考过程 持续了 N 秒」；
 * - 最右侧箭头与工具调用对齐，展开时顺畅旋转并展示内嵌 output 块。
 */
import { ToolGlyph } from "../iconkit/ToolGlyphs";
import { memo, useEffect, useRef, useState } from "react";
import { IconChevronRight } from "../icons";
import { ThinkingTicker } from "./ThinkingTicker";
import { ThinkingExpanded } from "./ThinkingExpanded";
import { ChatCollapse } from "./ChatCollapse";
import { useChatStore } from "../../store/chat";
import { thinkingKey, useFlowExpand } from "../../store/flowExpand";

export const ThinkingBlock = memo(function ThinkingBlock({ text, active, turnId, agentId, msgId }: {
  text: string;
  active: boolean;
  turnId?: number;
  agentId?: number;
  /** 思考消息 id：展开状态按它记忆，块重挂载（如并入过程段）后保持用户的选择 */
  msgId: number;
}) {
  void turnId;
  const [open, setOpen] = useFlowExpand(thinkingKey(msgId));
  const [duration, setDuration] = useState<number | null>(null);
  const startRef = useRef<number | null>(null);

  // plan-20261020-retry-cred-perf E13：只有「正在思考且尚未落库」的块才消费流式缓冲。
  // 历史块已有 text、根本用不到订阅值；而主代理 id 全库单例——此前所有已挂载的历史块都订阅
  // thinkingBuffers[agentId]，任一思考在流式时会让它们逐帧重渲染。不需要时选择器返回常量
  // undefined，zustand 用 Object.is 比较引用，不相等才通知，因此这些块彻底脱离该更新流。
  const needStream = active && !text;
  const streamText = useChatStore((s) => (needStream && agentId != null ? s.thinkingBuffers[agentId] : undefined));
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

  // plan-20261023-thinking-blank：纯空白文本等同于没有思考内容——不渲染。
  // 旧版后端会把「网关回一个空格」的占位思考落库，这类块只显示「思考过程 持续了几秒」，
  // 展开后一片空白；时间线已过滤存量数据，这里再兜一层，避免其它渲染入口（如插件替换）重现。
  if (!active && !text.trim()) return null;

  return (
    <div className="tc-node thinking-node">
      <div
        className={`tc-row has-output thinking-row${open ? " expanded" : ""}${active ? " active" : ""}`}
        onClick={() => setOpen(!open)}
      >
        <span className="tc-icon"><ToolGlyph tool="thinking" size={16} /></span>
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
      <ChatCollapse open={open}>
        <ThinkingExpanded text={displayText} />
      </ChatCollapse>
    </div>
  );
});
