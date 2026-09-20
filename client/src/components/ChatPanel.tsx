/** ChatPanel（v4 r3 完全重写）：聊天面板容器。
 * 顶部错误横幅（可关闭） + 消息流 + ComposerBox
 */
import { useCallback, useState } from "react";
import { PluginSlot } from "../plugins/registry";
import { TaskProgressCapsule } from "./chat/TaskProgressCapsule";

export function ChatPanel() {
  /** plan-282-1492：胶囊可见性（由胶囊上报）。
   *  只在胶囊真的出现时给消息流让位——未出现不预留空白；出现时靠占位块的高度过渡
   *  把消息流平滑顶上去（过渡期间消息流逐帧贴底，见 MessageFlow 的占位块 RO）。 */
  const [hasCapsule, setHasCapsule] = useState(false);
  const onCapsuleVisibilityChange = useCallback((visible: boolean) => setHasCapsule(visible), []);

  return (
    <div className={"chat-panel" + (hasCapsule ? " has-capsule" : "")}>
      {/* plan-282-1421（第7项）：移除标题栏与消息流之间的错误横幅。
          系统级错误统一由右上角 Toast 呈现，任务执行错误由消息流内
          （turn-item-error / MsgType.ERROR）呈现——同一条错误此前会被
          横幅与 Toast 同时渲染两遍。 */}
      <div className="chat-panel-flow">
        {/* v19: 插件 slot 渲染（可被用户外挂组件替换） */}
        <PluginSlot slot="message-flow" />
      </div>
      <div className="chat-panel-composer">
        {/* plan-282-1421（第12项）：旧 .task-strip 贴条（与输入框共享边框、存在即常驻）
            已替换为悬浮胶囊：智能分块、无任务时完全不渲染、hover 出浮层。 */}
        <TaskProgressCapsule onVisibilityChange={onCapsuleVisibilityChange} />
        <PluginSlot slot="composer" />
      </div>
    </div>
  );
}