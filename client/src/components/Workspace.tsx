/** 工作区（v4 r3 重写）：ws-header + 聊天面板 + TodoFloat + 导航页。
 * RightPanel 由 App.tsx 三栏骨架渲染，不再内嵌于此。
 */
import { useState, useEffect, useRef } from "react";
import type { NavKey } from "./Sidebar";
import { ChatPanel } from "./ChatPanel";
import { TodoFloat } from "./chat/TodoFloat";
import { ScheduledPage, SkillsPage, McpPage } from "./NavPages";
import { useChatStore } from "../store/chat";
import { api, type ModelOut } from "../api/client";
import { IconGitBranch, IconPaperclip, IconMic, IconCpu, IconArrowUp, IconImage, IconWrench, IconBrain, IconFolder } from "./icons";

export function Workspace({ nav, onSessionStart }: {
  nav: NavKey | null;
  onSessionStart?: () => void;
}) {
  const { sessions, currentSessionId, isRunning, projects, currentProjectId } = useChatStore();

  if (nav && nav !== "chat") {
    return (
      <main className="workspace">
        <header className="ws-header title-drag-region">
          <div className="ws-header-left title-no-drag">
            <span className="ws-header-title">{NAV_TITLES[nav]}</span>
          </div>
        </header>
        <div className="ws-body ws-navpage">
          {nav === "scheduled" && <ScheduledPage />}
          {nav === "skills" && <SkillsPage />}
          {nav === "mcp" && <McpPage />}
        </div>
      </main>
    );
  }

  if (!currentSessionId) {
    return (
      <main className="workspace">
        <header className="ws-header title-drag-region">
          <div className="ws-header-left title-no-drag">
            <span className="ws-header-title">新任务</span>
          </div>
        </header>
        <div className="ws-body ws-empty">
          <EmptyState onStarted={() => onSessionStart?.()} />
        </div>
      </main>
    );
  }

  const session = sessions.find((s) => s.id === currentSessionId);
  const project = projects.find((p) => p.id === (session?.project_id ?? currentProjectId));

  return (
    <main className="workspace">
      <header className="ws-header title-drag-region">
        <div className="ws-header-left title-no-drag">
          <span className="ws-header-title">{session?.title || "新任务"}</span>
          {session?.worktree_path && (
            <span className="ws-header-meta" title={session.worktree_path}>
              <IconGitBranch size={11} /> 工作树
            </span>
          )}
        </div>
        <div className="ws-header-meta title-no-drag">
          {project && <span title={project.path}>{project.path}</span>}
        </div>
        <div className="ws-header-meta title-no-drag">
          {isRunning ? (
            <span style={{ color: "var(--success)", display: "flex", alignItems: "center", gap: 4 }}>
              <span className="sidebar-session-pulse" /> 执行中
            </span>
          ) : (
            <span style={{ color: "var(--text-3)" }}>空闲</span>
          )}
        </div>
      </header>

      <div className="ws-body">
        <ChatPanel />
        <TodoFloat />
      </div>
    </main>
  );
}

const NAV_TITLES: Record<string, string> = {
  scheduled: "定时任务",
  skills: "技能",
  mcp: "MCP",
};

interface AttachmentPayload {
  filename: string;
  data_url: string;
}

/** 空态首页：Logo + 居中输入卡 + 环境/目录下拉 + 快捷胶囊 */
function EmptyState({ onStarted }: { onStarted: () => void }) {
  const { projects, currentProjectId, createSession, createProject, sendTurn } = useChatStore();
  const [input, setInput] = useState("");
  const [models, setModels] = useState<ModelOut[]>([]);
  const [selectedModel, setSelectedModel] = useState<number | null>(null);
  const [selectedProject, setSelectedProject] = useState<number | null>(currentProjectId);
  const [sending, setSending] = useState(false);
  const [addingDir, setAddingDir] = useState(false);
  const [showModels, setShowModels] = useState(false);
  const [showReasoning, setShowReasoning] = useState(false);
  const [reasoningEffort, setReasoningEffort] = useState<string | null>(null);
  const [attachments, setAttachments] = useState<AttachmentPayload[]>([]);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    api.listModels().then(setModels).catch(() => {});
  }, []);

  useEffect(() => {
    const ta = taRef.current;
    if (ta) {
      ta.style.height = "auto";
      ta.style.height = `${Math.min(ta.scrollHeight, 200)}px`;
    }
  }, [input]);

  const activeModel = models.find((m) => m.id === selectedModel);
  const supportsReasoning = !!activeModel?.reasoning_efforts && activeModel.reasoning_efforts.length > 0;

  const addFiles = (files: FileList | File[]) => {
    Array.from(files).forEach((f) => {
      const reader = new FileReader();
      reader.onload = () => {
        setAttachments((prev) => [...prev, { filename: f.name, data_url: String(reader.result || "") }]);
      };
      reader.readAsDataURL(f);
    });
  };

  const handleSend = async () => {
    const text = input.trim();
    if (!text || sending) return;
    const pid = selectedProject ?? currentProjectId;
    if (!pid) return;
    setSending(true);
    try {
      await createSession(pid, "新任务");
      const sid = useChatStore.getState().currentSessionId;
      if (selectedModel && sid) {
        try { await api.updateSession(sid, { model_id: selectedModel }); } catch { /* ignore */ }
      }
      const atts = attachments.length > 0 ? attachments.map((a) => ({ filename: a.filename, data_url: a.data_url })) : undefined;
      await sendTurn(text, atts, reasoningEffort ?? undefined);
      setAttachments([]);
      onStarted();
    } catch {
      /* error handled by store */
    } finally {
      setSending(false);
    }
  };

  // 快捷胶囊已移除（问题6）
 const [showProjectMenu, setShowProjectMenu] = useState(false);
  const activeProject = projects.find((p) => p.id === selectedProject);

  /** 提取路径末尾作为短名显示。 */
  function shortPathName(path: string): string {
    const parts = path.replace(/\\/g, "/").replace(/\/$/, "").split("/");
    return parts[parts.length - 1] || path;
  }

  const handleAddDirectory = async () => {
    if (addingDir) return;
    setAddingDir(true);
    try {
      const path = await window.chatcoderAPI?.selectDirectory?.();
      if (typeof path === "string" && path) {
        const proj = await createProject(path);
        if (proj) setSelectedProject(proj.id);
      }
    } catch { /* ignore */ } finally {
      setAddingDir(false);
    }
  };

  return (
    <div className="empty-state">
      <div className="empty-state-logo">&lt;/&gt; ChatCoder</div>
      <div className="empty-state-card">
        {attachments.length > 0 && (
          <div className="composer-attachments">
            {attachments.map((a, i) => (
              <div key={i} className="composer-attach-chip" title={a.filename}>
                <IconPaperclip size={11} />
                <span>{a.filename}</span>
                <button className="remove" onClick={() => setAttachments((prev) => prev.filter((_, j) => j !== i))}>×</button>
              </div>
            ))}
          </div>
        )}
        <textarea
          ref={taRef}
          className="empty-state-textarea"
          placeholder="输入消息，开始对话…"
          value={input}
          rows={1}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              handleSend();
            }
          }}
        />
        <div className="empty-state-toolbar">
          <div className="es-tools-left">
            <button className="es-tool" title="添加附件" onClick={() => fileRef.current?.click()}>
              <IconPaperclip size={15} />
            </button>
            <button className="es-tool" title="添加图片" onClick={() => fileRef.current?.click()}>
              <IconImage size={15} />
            </button>
            <button className="es-tool" title="技能">
              <IconWrench size={14} />
            </button>
            <input
              ref={fileRef}
              type="file"
              multiple
              style={{ display: "none" }}
              onChange={(e) => { if (e.target.files) addFiles(e.target.files); e.target.value = ""; }}
            />
          </div>
          <div className="es-tools-right">
            <div className="es-model-wrap">
              <button className="es-tool es-tool-model" onClick={() => { setShowModels((v) => !v); setShowReasoning(false); }}>
                <IconCpu size={13} />
                {activeModel ? activeModel.name.split("/").pop() : "模型"}
              </button>
              {showModels && (
                <div className="composer-menu es-model-menu">
                  {models.map((m) => (
                    <button
                      key={m.id}
                      className={m.id === selectedModel ? "active" : ""}
                      onClick={() => { setSelectedModel(m.id); setShowModels(false); }}
                    >
                      <IconCpu size={13} />
                      <span>{m.name}</span>
                    </button>
                  ))}
                </div>
              )}
            </div>
            {supportsReasoning && (
              <div className="es-model-wrap">
                <button className="es-tool es-tool-model" onClick={() => { setShowReasoning((v) => !v); setShowModels(false); }} title="思考深度">
                  <IconBrain size={11} />
                  {reasoningEffort || "默认"}
                </button>
                {showReasoning && (
                  <div className="composer-menu es-model-menu" style={{ right: 0, left: "auto" }}>
                    <div className="composer-menu-title">思考深度</div>
                    {activeModel!.reasoning_efforts.map((effort) => (
                      <button key={effort} className={effort === reasoningEffort ? "active" : ""} onClick={() => { setReasoningEffort(effort); setShowReasoning(false); }}>
                        {effort}
                      </button>
                    ))}
                    <button className={reasoningEffort === null ? "active" : ""} onClick={() => { setReasoningEffort(null); setShowReasoning(false); }}>
                      默认
                    </button>
                  </div>
                )}
              </div>
            )}
            <button className="es-tool" title="语音输入"><IconMic size={15} /></button>
            <button
              className="es-send"
              disabled={!input.trim() || sending || !selectedProject}
              onClick={handleSend}
            >
              <IconArrowUp size={16} />
            </button>
          </div>
        </div>
      </div>
     <div className="es-selectors">
       <span className="es-env">本地</span>
        <div className="es-project-dropdown">
          <button className="es-project-trigger" onClick={() => setShowProjectMenu(!showProjectMenu)} title={activeProject?.path ?? "选择项目"}>
            <IconFolder size={13} />
           <span className="es-project-name">{activeProject ? shortPathName(activeProject.path) : "选择项目…"}</span>
            <span style={{ fontSize: 10, opacity: 0.6 }}>▾</span>
          </button>
          {showProjectMenu && (
            <div className="context-menu es-project-menu" onClick={() => setShowProjectMenu(false)}>
              <div className="context-menu-item" onClick={() => { handleAddDirectory(); }}>
                <IconFolder size={12} /> <span>{addingDir ? "添加中…" : "选择本地目录…"}</span>
              </div>
              {projects.length > 0 && <div className="context-menu-divider" />}
              {projects.map((p) => (
                <div key={p.id} className={"context-menu-item" + (p.id === selectedProject ? " active" : "")} onClick={() => { setSelectedProject(p.id); setShowProjectMenu(false); }} title={p.path}>
                  <span>{shortPathName(p.path)}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
