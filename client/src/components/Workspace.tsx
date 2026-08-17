/** 工作区（v19）：ws-header + 聊天面板 + 导航页。
 * RightPanel 由 App.tsx 三栏骨架渲染，不再内嵌于此。
 * v19: 空态首页输入框与消息页输入框共用 ComposerCore（插件 slot: composer/empty-state）。
 */
import type { NavKey } from "./Sidebar";
import { ChatPanel } from "./ChatPanel";
import { ScheduledPage, SkillsPage, McpPage } from "./NavPages";
import { useChatStore } from "../store/chat";
import { ComposerCore } from "./chat/ComposerCore";
import { PluginSlot } from "../plugins/registry";

export function Workspace({ nav, onSessionStart }: {
  nav: NavKey | null;
  onSessionStart?: () => void;
}) {
  const currentSessionId = useChatStore((s) => s.currentSessionId);

  if (nav && nav !== "chat") {
    return (
      <main className="workspace">
        <div key={nav} className="ws-body ws-navpage view-enter">
          {nav === "scheduled" && <ScheduledPage />}
          {nav === "skills" && <SkillsPage />}
          {nav === "mcp" && <McpPage />}
        </div>
      </main>
    );
  }

  if (!currentSessionId) {
    return (
      <main className="workspace workspace-empty">
        <div className="ws-body ws-empty">
          <PluginSlot slot="empty-state" onStarted={() => onSessionStart?.()} />
        </div>
      </main>
    );
  }

  return (
    <main className="workspace workspace-session">
      <div key={currentSessionId} className="ws-body view-enter">
        <ChatPanel />
      </div>
    </main>
  );
}

/** 时段问候语（对齐 zcode 空态首页） */
function greeting(): string {
  const h = new Date().getHours();
  if (h >= 23 || h < 5) return "夜深啦，别忘了照顾好自己哦";
  if (h < 9) return "早上好";
  if (h < 12) return "上午好";
  if (h < 14) return "中午好";
  if (h < 18) return "下午好";
  return "晚上好";
}

/** 空态首页（v19：问候语 + 共用 ComposerCore home 变体） */
export function EmptyState({ onStarted }: { onStarted?: () => void }) {
  return (
    <div className="empty-state">
      <div className="empty-state-greeting">{greeting()}</div>
      <div className="empty-state-card">
        <ComposerCore variant="home" onStarted={onStarted} />
      </div>
    </div>
  );
}
