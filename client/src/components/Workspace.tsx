/** 工作区（v19）：ws-header + 聊天面板 + 导航页。
 * RightPanel 由 App.tsx 三栏骨架渲染，不再内嵌于此。
 * v19: 空态首页输入框与消息页输入框共用 ComposerCore（插件 slot: composer/empty-state）。
 */
import type { NavKey } from "./Sidebar";
import { ChatPanel } from "./ChatPanel";
import { ScheduledPage, SkillsPage, McpPage } from "./NavPages";
import { useChatStore } from "../store/chat";
import { useI18n } from "../store/i18n";
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

/** 时段问候语（对齐 zcode 空态首页，支持双语） */
function getGreetingKey(): string {
  const h = new Date().getHours();
  if (h >= 23 || h < 5) return "workspace.greet_night";
  if (h < 9) return "workspace.greet_morning";
  if (h < 12) return "workspace.greet_forenoon";
  if (h < 14) return "workspace.greet_noon";
  if (h < 18) return "workspace.greet_afternoon";
  return "workspace.greet_evening";
}

/** 空态首页（v19：问候语 + 共用 ComposerCore home 变体） */
export function EmptyState({ onStarted }: { onStarted?: () => void }) {
  const { t } = useI18n();
  return (
    <div className="empty-state">
      <div className="empty-state-greeting">{t(getGreetingKey())}</div>
      <div className="empty-state-card">
        <ComposerCore variant="home" onStarted={onStarted} />
      </div>
    </div>
  );
}
