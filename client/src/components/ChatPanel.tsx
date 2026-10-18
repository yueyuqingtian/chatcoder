/** ChatPanel（v4 r3 完全重写）：聊天面板容器。
 * 顶部错误横幅（可关闭） + 消息流 + ComposerBox
 */
import { useCallback, useMemo, useRef, useState } from "react";
import { PluginSlot } from "../plugins/registry";
import { ContentWidthResizer } from "./chat/ContentWidthResizer";
import { useContentColumnGeometry } from "./chat/useContentColumnGeometry";
import { SessionToolCard } from "./chat/toolcard/SessionToolCard";
import { CARD_RESERVE_EXPANDED, CARD_RESERVE_NONE } from "./chat/contentColumnGeometry";
import { useChatStore } from "../store/chat";
import { IconArrowLeft } from "./icons";
import type { ToolCardForm } from "./chat/toolcard/useToolCardFit";

export function ChatPanel() {
  const panelRef = useRef<HTMLDivElement>(null);

  const currentSessionId = useChatStore((s) => s.currentSessionId);
  const sessions = useChatStore((s) => s.sessions);
  const subsessionsByParent = useChatStore((s) => s.subsessionsByParent);
  const switchSession = useChatStore((s) => s.switchSession);

  /** plan-156-619 S5e：当前会话若是子会话，取出它所属主会话，用于顶部返回导航。
   *  `list_sessions` 过滤了子会话，所以 sessions 里查不到——必须再翻一层子会话桶；
   *  每主会话最多 8 个子会话，线性查找开销可忽略。 */
  const back = useMemo(() => {
    if (currentSessionId == null) return null;
    const own = sessions.find((s) => s.id === currentSessionId);
    if (own) return null; // 主会话不需要返回条
    for (const [parentId, list] of Object.entries(subsessionsByParent)) {
      const hit = list.find((c) => c.id === currentSessionId);
      if (hit) {
        const parent = sessions.find((s) => s.id === Number(parentId));
        return { parentId: Number(parentId), label: parent?.title || "主会话" };
      }
    }
    return null;
  }, [currentSessionId, sessions, subsessionsByParent]);

  /** plan-156-619 S7：工具卡占位。展开态才让出列宽（336 = 卡片 320 + 间隙 16），
   *  胶囊/隐藏态占位 0（胶囊浮在右上角空白区，不压正文）。 */
  const [cardReserve, setCardReserve] = useState(CARD_RESERVE_NONE);
  const onCardFormChange = useCallback((form: ToolCardForm) => {
    setCardReserve(form === "expanded" ? CARD_RESERVE_EXPANDED : CARD_RESERVE_NONE);
  }, []);

  /** plan-156-619 S3（需求 C1）：内容列宽度冻结。
   *  宽度只在「窗口缩放 / 拖面板 / 折叠展开面板 / 改内容宽度」时重算，
   *  右侧工具卡占位变化只推动水平位置——避免长回答因卡片出现而整体重排。 */
  const { shortfall } = useContentColumnGeometry(panelRef, cardReserve);

  return (
    <div ref={panelRef} className="chat-panel">
      {/* plan-156-619 S5e：子会话的返回导航——子会话按普通会话渲染，用户需要一个
          明确的出口回到主会话（否则只能靠左栏再点一次，长会话里容易迷路）。
          视觉：一行细导航，只用留白与箭头表达层级，不加色块、不加左侧竖线。 */}
      {back && (
        <div className="chat-subsession-bar">
          <button
            type="button"
            className="chat-subsession-back"
            onClick={() => void switchSession(back.parentId)}
          >
            <IconArrowLeft size={13} />
            <span className="chat-subsession-back-label">返回 {back.label}</span>
          </button>
          <span className="chat-subsession-kind">子会话</span>
        </div>
      )}
      {/* plan-282-1421（第7项）：移除标题栏与消息流之间的错误横幅。
          系统级错误统一由顶部居中的全局提示呈现，任务执行错误由消息流内
          （turn-item-error / MsgType.ERROR）呈现——同一条错误此前会被
          横幅与 Toast 同时渲染两遍。 */}
      <div className="chat-panel-flow">
        {/* v19: 插件 slot 渲染（可被用户外挂组件替换） */}
        <PluginSlot slot="message-flow" />
      </div>
      <div className="chat-panel-composer">
        {/* plan-156-619 S7：输入框上方的三段胶囊已移除——
            任务进度 / 工作流 / 子代理 / 计划 / Git 统一收进右上角综合工具卡；
            「未审核变更」段按用户要求整体下线（回滚仍在 Git 面板的丢弃语义里）。 */}
        <PluginSlot slot="composer" />
      </div>
      {/* plan-156-619 S7：综合工具卡（绝对定位覆盖层，不占列宽） */}
      <SessionToolCard hostRef={panelRef} shortfall={shortfall} onFormChange={onCardFormChange} />
      {/* plan-20261017-ux-overhaul 阶段 C：消息列边缘停留 1 秒出现拖拽柄，拖动调整内容宽度 */}
      <ContentWidthResizer hostRef={panelRef} />
    </div>
  );
}
