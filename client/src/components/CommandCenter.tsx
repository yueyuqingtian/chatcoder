/** 命令中心（v7 完全对齐 ZCode）：Cmd/Ctrl+K。
 * 结构：搜索框 + Tab（全部/操作/任务/文件）+ 分组列表
 * 分组：最近任务（相对时间）/ 建议（新任务、打开工作区、设置）/ 面板（侧边栏、终端）
 * 查询时追加斜杠命令与设置项（归入"操作"）。
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { useChatStore } from "../store/chat";
import { usePanelStore } from "../store/panel";
import { useI18n } from "../store/i18n";
import { formatRelativeTime } from "../utils/time";
import { SETTINGS_INDEX, type SettingsTab } from "./settings";
import {
  IconFileText, IconFolderOpen, IconSortDesc, IconMessageSquare, IconPanelLeft,
  IconPlus, IconSearch, IconSettings, IconTerminal, IconZap,
} from "./icons";

type TabKey = "all" | "action" | "task" | "file";

interface Entry {
  icon: React.ReactNode;
  label: string;
  hint?: string; // 右侧快捷键或相对时间
  run: () => void;
}
interface Group { title: string; items: Entry[] }

function highlight(text: string, q: string) {
  if (!q) return text;
  const idx = text.toLowerCase().indexOf(q.toLowerCase());
  if (idx < 0) return text;
  return (
    <>
      {text.slice(0, idx)}
      <mark>{text.slice(idx, idx + q.length)}</mark>
      {text.slice(idx + q.length)}
    </>
  );
}

/** 打开工作区目录（Ctrl+O / 命令中心共用） */
async function openWorkspace() {
  const dir = await window.chatcoderAPI?.selectDirectory?.();
  if (dir) await useChatStore.getState().createProject(dir);
}

export function CommandCenter() {
  const { t, language } = useI18n();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [tab, setTab] = useState<TabKey>("all");
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const wasComposerFocusedRef = useRef(false);
  const sessions = useChatStore((s) => s.sessions);
  const switchSession = useChatStore((s) => s.switchSession);
  const currentSessionId = useChatStore((s) => s.currentSessionId);

  const slashCommands = useMemo(() => [
    { cmd: "/plan", desc: t("cmd.slash_plan") },
    { cmd: "/chat", desc: t("cmd.slash_chat") },
    { cmd: "/clear", desc: t("cmd.slash_clear") },
    { cmd: "/compact", desc: t("cmd.slash_compact") },
    { cmd: "/init", desc: t("cmd.slash_init") },
  ], [t]);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((v) => !v);
        setQuery("");
        setTab("all");
        setActive(0);
      }
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "o") {
        e.preventDefault();
        void openWorkspace();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);

  useEffect(() => {
    if (open) {
      const el = document.activeElement;
      wasComposerFocusedRef.current = !!(el && el.tagName === "TEXTAREA" && el.closest(".composer-input"));
      setTimeout(() => inputRef.current?.focus(), 10);
    } else setQuery("");
  }, [open]);

  const closeAndRestoreFocus = () => {
    setOpen(false);
    if (wasComposerFocusedRef.current) {
      window.dispatchEvent(new CustomEvent("chatcoder:focus-composer"));
    }
  };

  const groups = useMemo<Group[]>(() => {
    const q = query.trim().toLowerCase();
    const out: Group[] = [];
    const wantTask = tab === "all" || tab === "task";
    const wantAction = tab === "all" || tab === "action";

    // 最近任务
    if (wantTask) {
      const matches = sessions
        .filter((s) => s.status !== "archived" && (!q || (s.title || `#${s.id}`).toLowerCase().includes(q)))
        .slice(0, 8)
        .map((s): Entry => ({
          icon: <IconMessageSquare size={14} />,
          label: s.title || `会话 #${s.id}`,
          hint: formatRelativeTime(s.last_activity_at, language),
          run: () => {
            if (s.id !== currentSessionId) void switchSession(s.id);
            window.dispatchEvent(new CustomEvent("chatcoder:focus-composer"));
          },
        }));
      if (matches.length > 0) out.push({ title: t("cmd.recent_tasks"), items: matches });
    }

    // 建议
    if (wantAction) {
      const suggest: Entry[] = [
        {
          icon: <IconPlus size={14} />, label: t("cmd.new_task"), hint: "Ctrl+N",
          run: () => {
            useChatStore.setState({ currentSessionId: null, messages: [], turns: [], tasks: [], runningTurnId: null, isRunning: false, interruptedTurnId: null, streamingBuffers: {}, thinkingBuffers: {}, usage: null, pendingApproval: null, pendingPlan: null, reviewedFiles: {}, injectMarks: [] });
          },
        },
        { icon: <IconFolderOpen size={14} />, label: t("cmd.open_workspace"), hint: "Ctrl+O", run: () => { void openWorkspace(); } },
        { icon: <IconSettings size={14} />, label: t("cmd.settings"), run: () => window.dispatchEvent(new CustomEvent("chatcoder:open-settings")) },
      ].filter((e) => !q || e.label.toLowerCase().includes(q));
      if (suggest.length > 0) out.push({ title: t("cmd.suggestions"), items: suggest });
    }

    // 面板
    if (wantAction && !q) {
      out.push({
        title: t("cmd.panel"),
        items: [
          {
            icon: <IconPanelLeft size={14} />, label: t("cmd.toggle_sidebar"), hint: "Ctrl+B",
            run: () => window.dispatchEvent(new KeyboardEvent("keydown", { key: "b", ctrlKey: true, bubbles: true, cancelable: true })),
          },
          { icon: <IconTerminal size={14} />, label: t("cmd.toggle_terminal"), hint: "Ctrl+J", run: () => usePanelStore.getState().openTab("terminal") },
          { icon: <IconTerminal size={14} />, label: t("cmd.add_terminal_tab"), run: () => usePanelStore.getState().openNewTab("terminal") },
        ],
      });
    }

    // 查询时：斜杠命令 + 设置项（归入"操作"）
    if (wantAction && q) {
      const slash = slashCommands.filter((c) => c.cmd.includes(q) || c.desc.toLowerCase().includes(q))
        .map((c): Entry => ({
          icon: <IconZap size={14} />, label: c.cmd, hint: c.desc,
          run: () => window.dispatchEvent(new CustomEvent("chatcoder:insert-slash", { detail: { cmd: c.cmd } })),
        }));
      const settingEntries = SETTINGS_INDEX.filter((it) => it.label.toLowerCase().includes(q) || it.keywords.toLowerCase().includes(q))
        .map((it): Entry => ({
          icon: <IconSettings size={14} />, label: it.label, hint: t("cmd.settings"),
          run: () => window.dispatchEvent(new CustomEvent("chatcoder:open-settings", { detail: { tab: it.key as SettingsTab } })),
        }));
      const ops = [...slash, ...settingEntries];
      if (ops.length > 0) out.push({ title: t("cmd.tab_action"), items: ops });
    }

    return out;
  }, [query, tab, sessions, currentSessionId, switchSession, t, language, slashCommands]);

  const flat = useMemo(() => groups.flatMap((g) => g.items), [groups]);
  useEffect(() => { setActive(0); }, [query, tab]);

  if (!open) return null;
  let rowIdx = -1;
  return (
    <div className="cmd-center-overlay" onMouseDown={closeAndRestoreFocus}>
      <div className="cmd-center" onMouseDown={(e) => e.stopPropagation()}>
        <div className="cmd-center-input-wrap">
          <IconSearch size={14} />
          <input
            ref={inputRef}
            className="cmd-center-input"
            placeholder={t("cmd.placeholder")}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Escape") { e.preventDefault(); closeAndRestoreFocus(); return; }
              if (e.key === "ArrowDown") { e.preventDefault(); setActive((i) => Math.min(flat.length - 1, i + 1)); return; }
              if (e.key === "ArrowUp") { e.preventDefault(); setActive((i) => Math.max(0, i - 1)); return; }
              if (e.key === "Enter" && flat[active]) { e.preventDefault(); closeAndRestoreFocus(); flat[active].run(); return; }
            }}
          />
        </div>
        <div className="cmd-center-tabs">
          {([["all", t("cmd.tab_all")], ["action", t("cmd.tab_action")], ["task", t("cmd.tab_task")], ["file", t("cmd.tab_file")]] as [TabKey, string][]).map(([k, label]) => (
            <button key={k} className={tab === k ? "active" : ""} onClick={() => setTab(k)}>
              {k === "all" && <IconSortDesc size={12} />}
              {k === "action" && <IconZap size={12} />}
              {k === "task" && <IconMessageSquare size={12} />}
              {k === "file" && <IconFileText size={12} />}
              {label}
            </button>
          ))}
        </div>
        <div className="cmd-center-list">
          {tab === "file" && (
            <div className="cmd-center-empty">{t("cmd.file_search_unimpl")}</div>
          )}
          {tab !== "file" && (
            groups.map((g) => (
              <div className="cmd-center-group" key={g.title}>
                <div className="cmd-center-group-title">{g.title}</div>
                {g.items.map((item) => {
                  rowIdx++;
                  const idx = rowIdx;
                  return (
                    <div
                      key={idx}
                      className={`cmd-center-item${idx === active ? " active" : ""}`}
                      onMouseEnter={() => setActive(idx)}
                      onClick={() => { setOpen(false); item.run(); }}
                    >
                      <span className="cmd-center-item-icon">{item.icon}</span>
                      <span className="cmd-center-item-label">{highlight(item.label, query.trim())}</span>
                      {item.hint && <span className="cmd-center-item-hint">{item.hint}</span>}
                    </div>
                  );
                })}
              </div>
            ))
          )}
          {tab !== "file" && flat.length === 0 && (
            <div className="cmd-center-empty">{t("cmd.no_match")}</div>
          )}
        </div>
      </div>
    </div>
  );
}
