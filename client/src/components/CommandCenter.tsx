/** 命令中心（v7 完全对齐 ZCode / S2 升级 plan-41-197）：Cmd/Ctrl+K。
 * 结构：搜索框 + Tab（全部/操作/任务/文件）+ 分组列表
 * 分组：最近任务（相对时间）/ 建议（新任务、打开工作区、设置）/ 面板（侧边栏、终端）
 * 查询时追加斜杠命令与设置项（归入"操作"）。
 *
 * S2 改动（plan-41-197）：
 *  - 弹窗放大（640 → 720px，行高/内距同步放大），观感不再局促；
 *  - 「文件」tab 落地真实文件搜索（复用 GET /projects/:id/files/search），
 *    点击结果直接打开右侧文件面板并定位到该文件；
 *  - 僵尸功能修复：斜杠命令原派发 `chatcoder:insert-slash` 事件但全仓无监听方
 *    （点了没反应），现改为复用已实现的 `chatcoder:composer-prefill` 通道，
 *    把命令写入输入框并聚焦，用户可继续补参数后发送。
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api/client";
import { useChatStore } from "../store/chat";
import { usePanelStore } from "../store/panel";
import { useI18n } from "../store/i18n";
import { formatRelativeTime } from "../utils/time";
import { SETTINGS_INDEX, type SettingsTab } from "./settings";
import {
  IconFileText, IconFolderOpen, IconLayers, IconMessageSquare, IconPanelLeft,
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

/** S2：把文件打开到右侧文件面板（折叠时自动展开）并定位 */
function openFileAt(path: string) {
  const panel = usePanelStore.getState();
  panel.openPanel();
  panel.openTab("files");
  panel.setPreviewPath(path);
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
  const currentProjectId = useChatStore((s) => s.currentProjectId);

  // S2：文件搜索结果（「文件」tab）——debounce 200ms，避免逐键打后端
  const [fileResults, setFileResults] = useState<string[]>([]);
  const [fileLoading, setFileLoading] = useState(false);

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

  // S2：文件搜索（仅「文件」tab 且有查询时请求；项目切换/关闭弹窗自动清理）
  useEffect(() => {
    if (!open || tab !== "file") return;
    const q = query.trim();
    if (!q || currentProjectId == null) { setFileResults([]); setFileLoading(false); return; }
    let cancelled = false;
    setFileLoading(true);
    const timer = window.setTimeout(() => {
      api.projectFileSearch(currentProjectId, q, 60)
        .then((list) => { if (!cancelled) setFileResults(list ?? []); })
        .catch(() => { if (!cancelled) setFileResults([]); })
        .finally(() => { if (!cancelled) setFileLoading(false); });
    }, 200);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [open, tab, query, currentProjectId]);

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
            useChatStore.setState({ currentSessionId: null, messages: [], turns: [], tasks: [], runningTurnId: null, isRunning: false, interruptedTurnId: null, streamingBuffers: {}, thinkingBuffers: {}, usage: null, pendingApproval: null, pendingPlan: null, reviewedFiles: {} });
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
          // S2 修复：原派发 `chatcoder:insert-slash` 无监听方（僵尸功能），
          // 改用已实现的 composer-prefill 通道写入输入框并聚焦。
          run: () => window.dispatchEvent(new CustomEvent("chatcoder:composer-prefill", { detail: { text: `${c.cmd} ` } })),
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
          <IconSearch size={15} />
          <input
            ref={inputRef}
            className="cmd-center-input"
            placeholder={t("cmd.placeholder")}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Escape") { e.preventDefault(); closeAndRestoreFocus(); return; }
              if (tab === "file") {
                // S2：文件 tab 的键盘导航（↑↓ 移动、Enter 打开）
                if (e.key === "ArrowDown" || e.key === "ArrowUp") {
                  e.preventDefault();
                  return;
                }
                if (e.key === "Enter" && fileResults.length > 0) {
                  e.preventDefault();
                  closeAndRestoreFocus();
                  openFileAt(fileResults[0]);
                  return;
                }
                return;
              }
              if (e.key === "ArrowDown") { e.preventDefault(); setActive((i) => Math.min(flat.length - 1, i + 1)); return; }
              if (e.key === "ArrowUp") { e.preventDefault(); setActive((i) => Math.max(0, i - 1)); return; }
              if (e.key === "Enter" && flat[active]) { e.preventDefault(); closeAndRestoreFocus(); flat[active].run(); return; }
            }}
          />
        </div>
        <div className="cmd-center-tabs">
          {([["all", t("cmd.tab_all")], ["action", t("cmd.tab_action")], ["task", t("cmd.tab_task")], ["file", t("cmd.tab_file")]] as [TabKey, string][]).map(([k, label]) => (
            <button key={k} className={tab === k ? "active" : ""} onClick={() => setTab(k)}>
              {k === "all" && <IconLayers size={12} />}
              {k === "action" && <IconZap size={12} />}
              {k === "task" && <IconMessageSquare size={12} />}
              {k === "file" && <IconFileText size={12} />}
              {label}
            </button>
          ))}
        </div>
        <div className="cmd-center-list">
          {tab === "file" ? (
            currentProjectId == null ? (
              <div className="cmd-center-empty">请先在左侧选择一个项目，再搜索项目内文件</div>
            ) : !query.trim() ? (
              <div className="cmd-center-empty">输入文件名或路径片段开始搜索</div>
            ) : fileLoading ? (
              <div className="cmd-center-empty">搜索中…</div>
            ) : fileResults.length === 0 ? (
              <div className="cmd-center-empty">{t("cmd.no_match")}</div>
            ) : (
              <div className="cmd-center-group">
                <div className="cmd-center-group-title">文件（{fileResults.length}）</div>
                {fileResults.map((p) => {
                  const idx = p.lastIndexOf("/");
                  const dir = idx >= 0 ? p.slice(0, idx) : "";
                  const name = idx >= 0 ? p.slice(idx + 1) : p;
                  return (
                    <div
                      key={p}
                      className="cmd-center-item"
                      onClick={() => { closeAndRestoreFocus(); openFileAt(p); }}
                    >
                      <span className="cmd-center-item-icon"><IconFileText size={14} /></span>
                      <span className="cmd-center-item-label">
                        <span className="cmd-center-file-name">{highlight(name, query.trim())}</span>
                        {dir && <span className="cmd-center-file-dir">{dir}</span>}
                      </span>
                    </div>
                  );
                })}
              </div>
            )
          ) : (
            <>
              {groups.map((g) => (
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
              ))}
              {flat.length === 0 && (
                <div className="cmd-center-empty">{t("cmd.no_match")}</div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
