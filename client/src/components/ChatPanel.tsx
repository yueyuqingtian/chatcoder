/** ChatPanel（v4 r3 完全重写）：聊天面板容器。
 * 顶部错误横幅（可关闭） + 消息流 + ComposerBox
 */
import { useCallback, useRef, useState } from "react";
import { PluginSlot } from "../plugins/registry";
import { ContentWidthResizer } from "./chat/ContentWidthResizer";
import { useContentColumnGeometry } from "./chat/useContentColumnGeometry";
import { SessionToolCard } from "./chat/toolcard/SessionToolCard";
import { CLEARANCE_NONE, clearanceForForm } from "./chat/contentColumnGeometry";
import type { ToolCardForm } from "./chat/toolcard/useToolCardFit";

export function ChatPanel() {
  const panelRef = useRef<HTMLDivElement>(null);

  /** plan-156-619 S7 / plan-156-620：工具卡对右侧空间的需求随形态变化（展开 / 胶囊 / 隐藏）。
   *  内容列只在右缘真的会被遮住时才左移，且只移需要的那一段（宽度冻结）。
   *  plan-20261019：胶囊是内容自适应宽度，按实测宽让位——此前一律按上限 260px 计算，
   *  折叠后右侧明明放得下也会把消息列与输入框整体左移。 */
  const [clearance, setClearance] = useState(CLEARANCE_NONE);
  const onCardFormChange = useCallback((form: ToolCardForm, capsuleW?: number) => {
    setClearance(clearanceForForm(form, capsuleW));
  }, []);

  /** plan-156-619 S3（需求 C1）；plan-156-623 性能重做：内容列宽度只由宿主宽与用户偏好
   *  决定（拖面板 / 窗口缩放时逐帧跟随），卡片形态变化只推动水平位置——避免长回答因卡片
   *  出现而整体重排。几何写入合帧到 rAF，且形态判定带滞回，拖拽时不再每帧重渲染。 */
  const fit = useContentColumnGeometry(panelRef, clearance);

  return (
    <div ref={panelRef} className="chat-panel">
      {/* plan-156-620：子会话顶部的「返回主会话」条已移除（用户反馈多余且不美观）——
          层级与返回入口由左栏「主会话 → 缩进子会话」承担，标题栏显示子会话自身标题。 */}
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
      <SessionToolCard fit={fit} onFormChange={onCardFormChange} />
      {/* plan-20261017-ux-overhaul 阶段 C：消息列边缘停留 1 秒出现拖拽柄，拖动调整内容宽度 */}
      <ContentWidthResizer hostRef={panelRef} />
    </div>
  );
}
