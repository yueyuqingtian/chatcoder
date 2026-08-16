/** 工作区（v4 r3 重写）：ws-header + 聊天面板 + TodoFloat + 导航页。
 * RightPanel 由 App.tsx 三栏骨架渲染，不再内嵌于此。
 */
import { useState, useEffect, useRef } from "react";
import type { NavKey } from "./Sidebar";
import { ChatPanel } from "./ChatPanel";
import { TodoFloat } from "./chat/TodoFloat";
import { ScheduledPage, SkillsPage, McpPage } from "./NavPages";
import { useChatStore } from "../store/chat";
import { api, type AttachmentInfo, type ModelOut, resolveFileUrl } from "../api/client";
import { IconPaperclip, IconArrowUp, IconBrain, IconFolder, IconX, IconPlus, IconChevronDown, IconShield } from "./icons";
import { ModelPicker } from "./chat/ModelPicker";

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
          <EmptyState onStarted={() => onSessionStart?.()} />
        </div>
      </main>
    );
  }

  return (
    <main className="workspace workspace-session">
      <div key={currentSessionId} className="ws-body view-enter">
        <ChatPanel />
        <TodoFloat />
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

/** 空态首页：问候语 + 居中输入卡（项目 chip 内嵌） + 权限/模型/思考工具行 */
function EmptyState({ onStarted }: { onStarted: () => void }) {
  const projects = useChatStore((s) => s.projects);
  const currentProjectId = useChatStore((s) => s.currentProjectId);
  const createSession = useChatStore((s) => s.createSession);
  const createProject = useChatStore((s) => s.createProject);
  const sendTurn = useChatStore((s) => s.sendTurn);
  const [input, setInput] = useState("");
  const [models, setModels] = useState<ModelOut[]>([]);
  const [selectedModel, setSelectedModel] = useState<number | null>(null);
  const [selectedProject, setSelectedProject] = useState<number | null>(currentProjectId);
  const [sending, setSending] = useState(false);
  const [addingDir, setAddingDir] = useState(false);
  const [showModels, setShowModels] = useState(false);
  const [showReasoning, setShowReasoning] = useState(false);
  const [reasoningEffort, setReasoningEffort] = useState<string | null>(null);
  const [attachments, setAttachments] = useState<AttachmentInfo[]>([]);
  const [uploading, setUploading] = useState(false);
  const [permMode, setPermMode] = useState<"default" | "accept_edits" | "plan">("accept_edits");
  const [showPerm, setShowPerm] = useState(false);
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

  // v14: 上传优先——文件先 POST /api/upload 落盘，附件统一为文件地址
  const addFiles = async (files: FileList | File[]) => {
    const arr = Array.from(files);
    if (arr.length === 0) return;
    setUploading(true);
    const uploaded: AttachmentInfo[] = [];
    const failed: string[] = [];
    try {
      for (const f of arr) {
        try {
          const up = await api.uploadFile(f);
          uploaded.push({
            file_id: up.file_id, filename: up.filename, path: up.path,
            url: up.url, size: up.size, mime_type: up.mime_type, type: up.type,
          });
        } catch (e) {
          console.warn("[workspace] 上传失败", f.name, e);
          failed.push(`${f.name}: ${e instanceof Error ? e.message : String(e)}`);
        }
      }
      if (uploaded.length > 0) setAttachments((prev) => [...prev, ...uploaded]);
      // v16: 上传失败必须给用户可见反馈，不能静默吞掉（"选了文件没反应"的根因之一）
      if (failed.length > 0) {
        useChatStore.setState({ error: `附件上传失败（${failed.length} 个）\n${failed.join("\n")}` });
      }
    } finally {
      setUploading(false);
    }
  };

  const handleSend = async () => {
    const text = input.trim();
    const pid = selectedProject ?? currentProjectId;
    if ((!text && attachments.length === 0) || sending || !pid) return;
    setSending(true);
    try {
      await createSession(pid, "新任务");
      const sid = useChatStore.getState().currentSessionId;
      if (sid) {
        try {
          await api.updateSession(sid, { model_id: selectedModel ?? undefined, permission_mode: permMode });
          // 同步 store，让聊天输入框的模型显示与空态选择一致（避免挂载竞态显示成列表第一个模型）
          if (selectedModel != null) {
            useChatStore.setState((st) => ({ sessions: st.sessions.map((x) => (x.id === sid ? { ...x, model_id: selectedModel } : x)) }));
          }
        } catch { /* ignore */ }
      }
      const atts = attachments.length > 0 ? attachments.map((a) => ({ ...a })) : undefined;
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
      <div className="empty-state-greeting">{greeting()}</div>
      <div className="empty-state-card">
        {/* 项目选择 chip（对齐 zcode：位于输入卡顶部） */}
        <div className="es-card-project">
          <button className="es-project-trigger" onClick={() => setShowProjectMenu(!showProjectMenu)} title={activeProject?.path ?? "选择项目"}>
            <IconFolder size={13} />
            <span className="es-project-name">{activeProject ? shortPathName(activeProject.path) : "选择项目…"}</span>
            <IconChevronDown size={11} />
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
        {attachments.length > 0 && (
          <div className="composer-attachments">
            {attachments.map((a, i) => (
              <div key={a.file_id || i} className={`composer-attach-chip${a.type === "image" ? " image" : ""}`} title={a.filename}>
                {a.type === "image" ? (
                  <img
                    src={resolveFileUrl(a.url)} alt={a.filename}
                    className="attach-chip-thumb"
                    onClick={() => window.open(resolveFileUrl(a.url), "_blank", "noopener")}
                  />
                ) : (
                  <IconPaperclip size={11} />
                )}
                <span className="attach-chip-name" onClick={() => window.open(resolveFileUrl(a.url), "_blank", "noopener")}>{a.filename}</span>
                <button className="remove" onClick={() => setAttachments((prev) => prev.filter((_, j) => j !== i))}><IconX size={10} /></button>
              </div>
            ))}
            {uploading && <span className="composer-attach-uploading">上传中…</span>}
          </div>
        )}
        <textarea
          ref={taRef}
          className="empty-state-textarea"
          placeholder="向 ChatCoder 提问，使用 @ 添加上下文，使用 / 选择命令或能力"
          value={input}
          rows={1}
          onPaste={(e) => {
            // v16: 粘贴图片/文件 → 提取剪贴板 File 后走上传流程
            const files: File[] = [];
            for (const item of Array.from(e.clipboardData?.items || [])) {
              if (item.kind === "file") {
                const f = item.getAsFile();
                if (f) {
                  const ext = (f.type.split("/")[1] || "png").replace(/[^a-z0-9]/gi, "") || "png";
                  const ts = new Date().toISOString().replace(/[-:T]/g, "").slice(0, 14);
                  files.push(f.name && f.name.trim() && f.name !== "image.png" ? f : new File([f], `paste-${ts}.${ext}`, { type: f.type || "image/png" }));
                }
              }
            }
            if (files.length > 0) { e.preventDefault(); void addFiles(files); }
          }}
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
              <IconPlus size={16} />
            </button>
            <div className="es-model-wrap">
              <button className="es-tool es-tool-perm" onClick={() => { setShowPerm((v) => !v); setShowModels(false); setShowReasoning(false); }} title="权限模式">
                <IconShield size={13} />
                {permMode === "accept_edits" ? "完全访问" : permMode === "plan" ? "计划模式" : "默认"}
                <IconChevronDown size={11} />
              </button>
              {showPerm && (
                <div className="composer-menu es-model-menu" style={{ left: 0 }}>
                  <div className="composer-menu-title">权限模式</div>
                  <button className={permMode === "default" ? "active" : ""} onClick={() => { setPermMode("default"); setShowPerm(false); }}>默认</button>
                  <button className={permMode === "accept_edits" ? "active" : ""} onClick={() => { setPermMode("accept_edits"); setShowPerm(false); }}>完全访问</button>
                  <button className={permMode === "plan" ? "active" : ""} onClick={() => { setPermMode("plan"); setShowPerm(false); }}>计划模式</button>
                </div>
              )}
            </div>
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
              <ModelPicker
                models={models}
                value={selectedModel}
                onChange={(id) => { setSelectedModel(id); setShowModels(false); }}
                open={showModels}
                onToggle={() => { setShowModels((v) => !v); setShowReasoning(false); }}
              />
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
                      <button key={effort} className={effort === reasoningEffort ? "active" : ""} onClick={() => { setReasoningEffort(effort); useChatStore.setState({ lastReasoningEffort: effort }); setShowReasoning(false); }}>
                        {effort}
                      </button>
                    ))}
                    <button className={reasoningEffort === null ? "active" : ""} onClick={() => { setReasoningEffort(null); useChatStore.setState({ lastReasoningEffort: null }); setShowReasoning(false); }}>
                      默认
                    </button>
                  </div>
                )}
              </div>
            )}
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
    </div>
  );
}
