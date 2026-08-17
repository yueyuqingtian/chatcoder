/** ChatPanel（v4 r3 完全重写）：聊天面板容器。
 * 顶部错误横幅（可关闭） + 消息流 + ComposerBox
 */
import { useState } from "react";
import { IconX } from "./icons";
import { useChatStore } from "../store/chat";
import { PluginSlot } from "../plugins/registry";

export function ChatPanel() {
  const error = useChatStore((s) => s.error);
  const clearError = useChatStore((s) => s.clearError);
  const [hidden, setHidden] = useState(false);

  return (
    <div className="chat-panel">
      {error && !hidden && (
        <div className="chat-error-banner">
          <span className="chat-error-banner-icon">!</span>
          <span className="chat-error-banner-text">{error}</span>
          <button
            onClick={() => { setHidden(true); clearError(); }}
            title="关闭"
          >
            <IconX size={12} />
          </button>
        </div>
      )}
      <div className="chat-panel-flow">
        {/* v19: 插件 slot 渲染（可被用户外挂组件替换） */}
        <PluginSlot slot="message-flow" />
      </div>
      <div className="chat-panel-composer">
        <PluginSlot slot="composer" />
      </div>
    </div>
  );
}