/** 工作区（v19）：ws-header + 聊天面板 + 导航页。
 * RightPanel 由 App.tsx 三栏骨架渲染，不再内嵌于此。
 * v19: 空态首页输入框与消息页输入框共用 ComposerCore（插件 slot: composer/empty-state）。
 * plan-219: 空态首页增强——水印背景 + 副标题 + 快捷动作 chips + 技能 chips（对齐 ZCode/WorkBuddy）。
 */
import { useEffect, useState } from "react";
import type { NavKey } from "./Sidebar";
import { ChatPanel } from "./ChatPanel";
import { ScheduledPage } from "./NavPages";
import { MarketPanel } from "./settings/MarketPanel";
import { useChatStore } from "../store/chat";
import { useI18n } from "../store/i18n";
import { api, type SkillOut } from "../api/client";
import { ComposerCore } from "./chat/ComposerCore";
import { PluginSlot } from "../plugins/registry";
import { AppLogo } from "./AppLogo";
import { PageTransition } from "./ui";
import { IconAlertTriangle, IconBox, IconBookOpen, IconCheckSquare, IconClipboard, IconSearch, IconZap } from "./icons";

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
        <div className="ws-body ws-empty">
          <PluginSlot slot="empty-state" onStarted={() => onSessionStart?.()} />
        </div>
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

/** plan-219: 快捷动作 chips（编码场景，点击预填输入框）
 *  S16（plan-41-197）：扩充为 6 个有特色的细化模板（新增排查报错 / 安全重构），
 *  提示词写明角色、步骤与输出要求，替换此前“一句话占位”式文案。 */
const QUICK_ACTIONS = [
  { icon: IconAlertTriangle, labelKey: "workspace.quick_fix", promptKey: "workspace.quick_fix_prompt" },
  { icon: IconZap, labelKey: "workspace.quick_triage", promptKey: "workspace.quick_triage_prompt" },
  { icon: IconCheckSquare, labelKey: "workspace.quick_test", promptKey: "workspace.quick_test_prompt" },
  { icon: IconSearch, labelKey: "workspace.quick_review", promptKey: "workspace.quick_review_prompt" },
  { icon: IconBookOpen, labelKey: "workspace.quick_explain", promptKey: "workspace.quick_explain_prompt" },
  { icon: IconClipboard, labelKey: "workspace.quick_refactor", promptKey: "workspace.quick_refactor_prompt" },
];

/** 空态首页（plan-219：水印 + 问候语 + 副标题 + 输入卡片 + 快捷 chips + 技能 chips） */
export function EmptyState({ onStarted }: { onStarted?: () => void }) {
  const { t } = useI18n();
  const [skills, setSkills] = useState<SkillOut[]>([]);

  // 拉取已启用技能（最多 8 个），失败静默——技能行整体不渲染
  useEffect(() => {
    let cancelled = false;
    api.listSkills()
      .then((items) => { if (!cancelled) setSkills(items.filter((s) => s.is_active).slice(0, 8)); })
      .catch(() => { /* ignore */ });
    return () => { cancelled = true; };
  }, []);

  const prefill = (text: string) =>
    window.dispatchEvent(new CustomEvent("chatcoder:composer-prefill", { detail: { text } }));

  return (
    <div className="empty-state">
      <div className="empty-state-watermark" aria-hidden>
        <AppLogo variant="outline" size={320} />
      </div>
      <div className="empty-state-greeting">{t(getGreetingKey())}</div>
      <div className="empty-state-subtitle">{t("workspace.subtitle")}</div>
      <div className="empty-state-card">
        <ComposerCore variant="home" onStarted={onStarted} />
      </div>
      <div className="empty-state-quick">
        {QUICK_ACTIONS.map((a) => (
          <button key={a.labelKey} className="es-quick-chip" type="button"
            onClick={() => prefill(t(a.promptKey))}>
            <a.icon size={14} />
            {t(a.labelKey)}
          </button>
        ))}
      </div>
      {skills.length > 0 && (
        <div className="empty-state-skills">
          {skills.map((s) => (
            <button key={s.id} className="es-skill-chip" type="button"
              title={s.description || s.name}
              onClick={() => prefill(`$${s.name} `)}>
              <IconBox size={13} />
              {s.display_name || s.name}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
