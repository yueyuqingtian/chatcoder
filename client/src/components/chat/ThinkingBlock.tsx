/** plan-20261114-message-flow：历史思考只展示已落库内容，实时思考由活动过程组承接。
 *  默认收起，展开使用统一动画容器；不单独计时，箭头紧随内容并在悬停或键盘聚焦时显示。
 */
import { ToolGlyph } from "../iconkit/ToolGlyphs";
import { memo } from "react";
import { IconChevronRight } from "../icons";
import { ThinkingTicker } from "./ThinkingTicker";
import { ThinkingExpanded } from "./ThinkingExpanded";
import { ChatCollapse } from "./ChatCollapse";
import { useChatStore } from "../../store/chat";
import { thinkingKey, liveThinkingKey, useFlowExpand } from "../../store/flowExpand";

export const ThinkingBlock = memo(function ThinkingBlock({ text, active, turnId, agentId, msgId }: {
  text: string;
  active: boolean;
  turnId?: number;
  agentId?: number;
  /** 思考消息 id：展开状态按它记忆，块重挂载（如并入过程段）后保持用户的选择 */
  msgId: number;
}) {
  void turnId;
  const [open, setOpen] = useFlowExpand(msgId === 0 ? liveThinkingKey(agentId) : thinkingKey(msgId));

  // plan-20261020-retry-cred-perf E13：只有「正在思考且尚未落库」的块才消费流式缓冲。
  // 历史块已有 text、根本用不到订阅值；而主代理 id 全库单例——此前所有已挂载的历史块都订阅
  // thinkingBuffers[agentId]，任一思考在流式时会让它们逐帧重渲染。不需要时选择器返回常量
  // undefined，zustand 用 Object.is 比较引用，不相等才通知，因此这些块彻底脱离该更新流。
  const needStream = active && !text;
  const streamText = useChatStore((s) => (needStream && agentId != null ? s.thinkingBuffers[agentId] : undefined));
  const displayText = text || streamText || "";
  // v40: active 且尚未落库 = 思考中（首 token 到达前也显示「正在思考」+ 占位）
  const isThinking = active;

  // plan-20261023-thinking-blank：纯空白文本等同于没有思考内容——不渲染。
  // 旧版后端会把「网关回一个空格」的占位思考落库，这类块只显示「思考过程 持续了几秒」，
  // 展开后一片空白；时间线已过滤存量数据，这里再兜一层，避免其它渲染入口（如插件替换）重现。
  if (!active && !text.trim()) return null;

  return (
    <div className="tc-node thinking-node">
      <div
        className={`tc-row has-output thinking-row${open ? " expanded" : ""}${active ? " active" : ""}`}
        onClick={() => setOpen(!open)}
        role="button" tabIndex={0} aria-expanded={open}
        onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setOpen(!open); } }}
      >
        <span className="tc-icon"><ToolGlyph tool="thinking" size={16} /></span>
        <span className={"tc-verb" + (isThinking ? " text-shine" : "")}>{isThinking ? "正在思考" : "思考过程"}</span>
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
        ) : null}
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
