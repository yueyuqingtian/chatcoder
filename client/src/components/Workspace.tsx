/** 工作区（v19）：ws-header + 聊天面板 + 导航页。
 * RightPanel 由 App.tsx 三栏骨架渲染，不再内嵌于此。
 * v19: 空态首页输入框与消息页输入框共用 ComposerCore（插件 slot: composer/empty-state）。
 * plan-20261009-ui U2：空态首页重做——
 *   主题「在对话中即可完成编码」：对话邮票（灰度静置→聚焦变彩色 + 随指针摆动的幽默浮签，点击换一枚）
 *   → 主副标语 → 用量热力图卡片（与输入框同宽）→ 输入框固定在底部（与会话态同位置，发送后不跳位）；输入框左下为工作区。
 */
import type { NavKey } from "./Sidebar";
import { ChatPanel } from "./ChatPanel";
import { ScheduledPage } from "./NavPages";
import { MarketPanel } from "./settings/MarketPanel";
import { useChatStore } from "../store/chat";
import { useI18n } from "../store/i18n";
import { ComposerCore } from "./chat/ComposerCore";
import { PluginSlot } from "../plugins/registry";
import { PageTransition } from "./ui";
import { StampHero } from "./home/StampHero";
import { UsageCard } from "./home/UsageCard";

export function Workspace({ nav, onSessionStart }: {
  nav: NavKey | null;
  onSessionStart?: () => void;
}) {
  const currentSessionId = useChatStore((s) => s.currentSessionId);

  if (nav && nav !== "chat") {
    return (
      <main className="workspace">
        {/* plan-282-1421（第3项）：导航页切换过渡（id=nav 变化即播放一次入场） */}
        <PageTransition id={nav} className={"ws-body ws-navpage" + (nav === "skills" ? " is-fill" : "")}>
          {nav === "scheduled" && <ScheduledPage />}
          {/* plan-41-198：左面板「拓展」= 市场视图（浏览与安装）；设置页「扩展管理」只展示
              已安装内容。MarketPanel 自带头部与内容滚动区，容器加 is-fill 让外层不再滚动
              （滚动发生在其内部 .market-scroll），实现「头部固定 + 内容滑动」。 */}
          {nav === "skills" && <MarketPanel />}
        </PageTransition>
      </main>
    );
  }

  if (!currentSessionId) {
    return (
      <main className="workspace workspace-empty">
        {/* plan-353-1738 M3（S9）：空态 ⇄ 会话态此前是裸 div 瞬切（外层 id 也不变）。
            这里包一层 PageTransition，让「新建会话 / 退出会话」也有与其它切换一致的入场。
            fill=false：保持原 .ws-body 的盒模型（flex:1 撑满，不引入 height:100%）。 */}
        <PageTransition id="workspace-empty" fill={false} className="ws-body ws-empty">
          <PluginSlot slot="empty-state" onStarted={() => onSessionStart?.()} />
        </PageTransition>
      </main>
    );
  }

  return (
    <main className="workspace workspace-session">
      {/* plan-282-1421（第3项）：会话切换过渡（id=sessionId） */}
      <PageTransition id={currentSessionId ?? 0} className="ws-body">
        <ChatPanel />
      </PageTransition>
    </main>
  );
}

/** 空态首页（plan-20261009-ui U2） */
export function EmptyState({ onStarted }: { onStarted?: () => void }) {
  const { t } = useI18n();
  return (
    <div className="empty-state es-home">
      <div className="es-hero">
        <div className="es-hero-inner mo-stagger">
          <StampHero />
          <div className="es-headline">
            <h1 className="es-title">{t("workspace.tagline")}</h1>
            <p className="es-sub">{t("workspace.tagline_sub")}</p>
          </div>
          <UsageCard />
        </div>
      </div>
      <div className="es-composer">
        <ComposerCore variant="home" onStarted={onStarted} />
      </div>
    </div>
  );
}
