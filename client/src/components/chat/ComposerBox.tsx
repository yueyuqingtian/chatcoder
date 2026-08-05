/** ComposerBox（v5）：对话态输入框，textarea 独占一行，工具栏在底部。 */
import { useCallback, useEffect, useRef, useState } from "react";
import { api, type ModelOut } from "../../api/client";
import { useChatStore, type UsageDetail } from "../../store/chat";
import {
  IconArrowUp, IconStop, IconMic, IconPaperclip, IconImage,
  IconCpu, IconBrain, IconWrench, IconX,
} from "../icons";

interface AttachmentPayload {
  filename: string;
  mime_type: string;
  size: number;
  data_url: string;
}

const REASONING_STORAGE_PREFIX = "reasoning:";
const MAX_TEXTAREA_HEIGHT = 200;

export function ComposerBox() {
  const currentSessionId = useChatStore((s) => s.currentSessionId);
  const isRunning = useChatStore((s) => s.isRunning);
  const usage = useChatStore((s) => s.usage);
  const isCompacting = useChatStore((s) => s.isCompacting);
 const sendTurn = useChatStore((s) => s.sendTurn);
 const forceStop = useChatStore((s) => s.forceStop);
 const pendingPlan = useChatStore((s) => s.pendingPlan);
 const confirmPlan = useChatStore((s) => s.confirmPlan);
 const dismissPlan = useChatStore((s) => s.dismissPlan);

  const [input, setInput] = useState("");
  const [attachments, setAttachments] = useState<AttachmentPayload[]>([]);
  const [models, setModels] = useState<ModelOut[]>([]);
  const [sessionModelId, setSessionModelId] = useState<number | null>(null);
  const [reasoningEffort, setReasoningEffort] = useState<string | null>(null);
  const [showModels, setShowModels] = useState(false);
  const [showReasoning, setShowReasoning] = useState(false);
  const [showSlash, setShowSlash] = useState(false);
  const [showAt, setShowAt] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const [listening, setListening] = useState(false);

  const taRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const recogRef = useRef<{ stop: () => void } | null>(null);

  useEffect(() => {
    api.listModels().then((ms) => setModels(ms)).catch(() => {});
    if (currentSessionId) {
      api.getSession(currentSessionId).then((s) => setSessionModelId(s.model_id)).catch(() => {});
    }
  }, [currentSessionId]);

  useEffect(() => {
    if (currentSessionId) {
      setReasoningEffort(localStorage.getItem(REASONING_STORAGE_PREFIX + currentSessionId));
    } else {
      setReasoningEffort(null);
    }
  }, [currentSessionId]);

  useEffect(() => {
    const ta = taRef.current;
    if (ta) {
      ta.style.height = "auto";
      ta.style.height = `${Math.min(ta.scrollHeight, MAX_TEXTAREA_HEIGHT)}px`;
    }
  }, [input]);

  // 全局弹窗点击外部关闭：模型菜单/思考深度菜单/斜杠菜单/@菜单
  useEffect(() => {
    if (!showModels && !showReasoning && !showSlash && !showAt) return;
    const handler = (e: MouseEvent) => {
      const target = e.target as Element;
      const withinMenu = target.closest?.(".composer-menu") || target.closest?.(".composer-model-badge") || target.closest?.(".composer-reasoning-btn") || target.closest?.(".composer-usage-ring");
      if (!withinMenu) {
        setShowModels(false);
        setShowReasoning(false);
        setShowSlash(false);
        setShowAt(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [showModels, showReasoning, showSlash, showAt]);

  const activeModel = models.find((m) => m.id === sessionModelId);
  const supportsReasoning = activeModel?.reasoning_efforts && activeModel.reasoning_efforts.length > 0;
  const canSend = input.trim().length > 0 && !isRunning;

  const changeModel = async (modelId: number) => {
    setSessionModelId(modelId);
    setShowModels(false);
    if (currentSessionId) {
      try { await api.updateSession(currentSessionId, { model_id: modelId }); } catch { /* ignore */ }
    }
  };

  const changeReasoning = (effort: string | null) => {
    setReasoningEffort(effort);
    if (currentSessionId) {
      if (effort) localStorage.setItem(REASONING_STORAGE_PREFIX + currentSessionId, effort);
      else localStorage.removeItem(REASONING_STORAGE_PREFIX + currentSessionId);
    }
    setShowReasoning(false);
  };

  const handleSend = () => {
    if (!canSend) return;
    let content = input.trim();
    // /plan 先规划后执行；/chat 只读审阅
    let mode: "readonly" | "plan" | null = null;
    if (content.startsWith("/plan")) { mode = "plan"; content = content.replace(/^\/plan\s*/, "").trim(); }
    else if (content.startsWith("/chat")) { mode = "readonly"; content = content.replace(/^\/chat\s*/, "").trim(); }
    if (!content) return;
    const atts = attachments.length > 0 ? attachments.map((a) => ({ ...a })) : undefined;
    sendTurn(content, atts, reasoningEffort ?? undefined, mode);
    setInput("");
    setAttachments([]);
    setShowSlash(false);
    setShowAt(false);
  };

  const handleCancel = () => { forceStop(); };

  const addFiles = useCallback((files: FileList | File[]) => {
    const arr = Array.from(files);
    Promise.all(
      arr.map((f) => new Promise<AttachmentPayload>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => {
          const dataUrl = String(reader.result || "");
          resolve({ filename: f.name, mime_type: f.type || "application/octet-stream", size: f.size, data_url: dataUrl });
        };
        reader.onerror = () => reject(reader.error);
        reader.readAsDataURL(f);
      })),
    ).then((atts) => { setAttachments((prev) => [...prev, ...atts]); }).catch(() => {});
  }, []);

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    if (e.dataTransfer.files.length > 0) addFiles(e.dataTransfer.files);
  };

  const toggleVoice = () => {
    if (listening) { recogRef.current?.stop(); return; }
    const SR = (window as unknown as { webkitSpeechRecognition?: new () => unknown }).webkitSpeechRecognition;
    if (!SR) { alert("当前浏览器不支持语音识别"); return; }
    const r = new SR() as { lang: string; continuous: boolean; interimResults: boolean; onresult: (e: { results: ArrayLike<ArrayLike<{ transcript: string }>> }) => void; onend: () => void; onerror: () => void; start: () => void; stop: () => void; };
    r.lang = "zh-CN"; r.continuous = false; r.interimResults = false;
    r.onresult = (e) => { const text = e.results[0]?.[0]?.transcript ?? ""; if (text) setInput((v) => v + text); };
    r.onend = () => setListening(false);
    r.onerror = () => setListening(false);
    setListening(true);
    r.start();
    recogRef.current = { stop: () => r.stop() };
  };

  const usagePct = usage && usage.context_window ? Math.min(100, Math.round((usage.total / usage.context_window) * 100)) : 0;

  return (
    <div className={`composer${dragOver ? " drag-over" : ""}`} onDragOver={(e) => { e.preventDefault(); setDragOver(true); }} onDragLeave={(e) => { e.preventDefault(); setDragOver(false); }} onDrop={handleDrop}>
      {attachments.length > 0 && (
        <div className="composer-attachments">
          {attachments.map((a, i) => (
            <div key={i} className="composer-attach-chip" title={a.filename}>
              {a.mime_type.startsWith("image/") && a.data_url ? <img src={a.data_url} alt={a.filename} /> : <IconPaperclip size={11} />}
              <span>{a.filename}</span>
              <button className="remove" onClick={() => setAttachments((prev) => prev.filter((_, j) => j !== i))}><IconX size={10} /></button>
            </div>
          ))}
        </div>
      )}
      <div className="composer-main">
        <textarea
          ref={taRef}
          className="composer-input"
          placeholder="输入消息…  / 命令  ·  @ 文件"
          value={input}
          rows={2}
          onChange={(e) => { setInput(e.target.value); setShowSlash(e.target.value.startsWith("/")); setShowAt(e.target.value.endsWith("@") || /@\S*$/.test(e.target.value)); }}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleSend(); } if (e.key === "Escape") { setShowSlash(false); setShowAt(false); } }}
        />
        <div className="composer-toolbar">
          <div className="composer-tools-left">
            <button className="composer-attach" title="添加附件" onClick={() => fileRef.current?.click()}><IconPaperclip size={15} /></button>
            <button className="composer-attach" title="添加图片" onClick={() => fileRef.current?.click()}><IconImage size={15} /></button>
            <button className="composer-attach" title="技能命令" onClick={() => { setInput("/"); taRef.current?.focus(); }}><IconWrench size={14} /></button>
            <input ref={fileRef} type="file" multiple style={{ display: "none" }} onChange={(e) => { if (e.target.files) addFiles(e.target.files); e.target.value = ""; }} />
          </div>
          <div className="composer-right">
            <div className="composer-model-wrap">
              <button className="composer-model-badge" onClick={() => { setShowModels((v) => !v); setShowReasoning(false); }}>
                <IconCpu size={13} />
                {activeModel ? activeModel.name.split("/").pop() : "模型"}
              </button>
              {showModels && (
                <div className="composer-menu composer-model-menu">
                  <div className="composer-menu-title">选择模型</div>
                  {models.length === 0 && <div className="composer-menu-empty">暂无模型</div>}
                  {models.map((m) => (
                    <button key={m.id} className={m.id === sessionModelId ? "active" : ""} onClick={() => changeModel(m.id)}>
                      <IconCpu size={13} /><span className="composer-model-name" title={m.name}>{m.name.split("/").pop()}</span>
                    </button>
                  ))}
                </div>
              )}
            </div>
            {supportsReasoning && (
              <div className="composer-reasoning">
                <button className="composer-reasoning-btn" onClick={() => { setShowReasoning((v) => !v); setShowModels(false); }} title="思考深度">
                  <IconBrain size={11} />{reasoningEffort || "默认"}
                </button>
                {showReasoning && (
                  <div className="composer-menu composer-reasoning-menu">
                    <div className="composer-menu-title">思考深度</div>
                    {activeModel!.reasoning_efforts.map((effort) => (
                      <button key={effort} className={effort === reasoningEffort ? "active" : ""} onClick={() => changeReasoning(effort)}>{effort}</button>
                    ))}
                    <button className={reasoningEffort === null ? "active" : ""} onClick={() => changeReasoning(null)}>默认</button>
                  </div>
                )}
              </div>
            )}
            <div className="composer-usage">
              <UsageRing pct={usagePct} usage={usage} compacting={isCompacting} />
            </div>
            {isCompacting && (
              <span className="composer-compacting-badge" title="上下文接近窗口上限，正在压缩历史对话">
                正在压缩上下文…
              </span>
            )}
            <button className={`composer-ctx${listening ? " listening" : ""}`} onClick={toggleVoice} title={listening ? "停止录音" : "语音输入"}><IconMic size={15} /></button>
            {isRunning ? (
              <button className="composer-send stop" onClick={handleCancel} title="停止"><IconStop size={14} /></button>
            ) : (
              <button className="composer-send" disabled={!canSend} onClick={handleSend} title="发送"><IconArrowUp size={16} /></button>
            )}
          </div>
        </div>
      </div>
      {dragOver && (<div className="composer-drag-overlay"><IconPaperclip size={24} /><span>松开以添加附件</span></div>)}
      {showSlash && (<div className="composer-menu composer-slash"><div className="composer-menu-title">命令</div><button onClick={() => { setInput("/plan "); setShowSlash(false); }}>/plan <span className="composer-slash-desc">先规划再执行</span></button><button onClick={() => { setInput("/chat "); setShowSlash(false); }}>/chat <span className="composer-slash-desc">只读审阅</span></button><button onClick={() => { setInput("/clear"); setShowSlash(false); }}>/clear</button><button onClick={() => { setInput("/compact"); setShowSlash(false); }}>/compact</button><button onClick={() => { setInput("/init"); setShowSlash(false); }}>/init</button></div>)}
      {showAt && (<div className="composer-menu composer-at"><div className="composer-menu-title">提及文件</div><div className="composer-menu-empty">继续输入文件名以补全…</div></div>)}
      {pendingPlan && (
        <div className="plan-confirm-overlay">
          <div className="plan-confirm-card">
            <div className="plan-confirm-title">计划已生成</div>
            <div className="plan-confirm-desc">
              AI 已在项目根目录 <code>ai/</code> 目录生成计划文档 <code>chatcoder-plan.md</code>，请审阅后确认是否按计划执行。
            </div>
            <div className="plan-confirm-task">
              <span className="plan-confirm-label">任务</span>
              <div className="plan-confirm-task-text">{pendingPlan.task}</div>
            </div>
            <div className="plan-confirm-actions">
              <button className="btn-ghost" onClick={() => dismissPlan()}>取消</button>
              <button className="btn-primary" onClick={() => confirmPlan(pendingPlan.task)}>确认执行</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function fmtTokens(n: number): string {
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

/** token 占用圆环：无文字，hover 显示百分比 tooltip，点击弹窗详情 */
function UsageRing({ pct, usage, compacting }: { pct: number; usage: UsageDetail | null; compacting?: boolean }) {
  const [showDetail, setShowDetail] = useState(false);
  const ringRef = useRef<HTMLButtonElement>(null);
  const radius = 10;
  const circ = 2 * Math.PI * radius;
  const offset = circ - (pct / 100) * circ;
  const color = compacting ? "var(--warning)" : pct > 80 ? "var(--error)" : pct > 50 ? "var(--warning)" : "var(--success)";

  useEffect(() => {
    if (!showDetail) return;
    const handler = (e: MouseEvent) => {
      if (ringRef.current && !ringRef.current.contains(e.target as Node)) {
        setShowDetail(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [showDetail]);

  return (
    <div className="composer-usage-ring-wrap">
      <button
        ref={ringRef}
        className={`composer-usage-ring${compacting ? " compacting" : ""}`}
        title={compacting ? `正在压缩上下文（${pct}%）` : `上下文占用 ${pct}%`}
        onClick={() => setShowDetail((v) => !v)}
        style={{ position: "relative", width: 26, height: 26, display: "flex", alignItems: "center", justifyContent: "center", borderRadius: "50%", border: "none", background: "transparent", cursor: "pointer" }}
      >
        <svg width="26" height="26" viewBox="0 0 26 26">
          <circle cx="13" cy="13" r={radius} fill="none" stroke="var(--border)" strokeWidth="2.5" />
          <circle
            cx="13" cy="13" r={radius} fill="none" stroke={color} strokeWidth="2.5"
            strokeDasharray={circ} strokeDashoffset={offset}
            strokeLinecap="round" transform="rotate(-90 13 13)"
            style={{ transition: "stroke-dashoffset var(--dur-med, 0.3s) ease" }}
          />
        </svg>
      </button>
      {showDetail && (
        <div className="composer-usage-detail" style={{ display: "block" }}>
          {usage ? (
            <>
              <div className="composer-usage-title">上下文占用 {pct}%{compacting ? " · 正在压缩" : ""}</div>
              <div className="composer-usage-row"><span>输入</span><span>{fmtTokens(usage.input)}</span></div>
              <div className="composer-usage-row"><span>缓存输入</span><span>{fmtTokens(usage.cached_input)}</span></div>
              <div className="composer-usage-row"><span>输出</span><span>{fmtTokens(usage.output)}</span></div>
              <div className="composer-usage-row"><span>推理</span><span>{fmtTokens(usage.reasoning_output)}</span></div>
              <div className="composer-usage-row total"><span>当前占用</span><span>{fmtTokens(usage.total)}</span></div>
              <div className="composer-usage-row"><span>窗口</span><span>{fmtTokens(usage.context_window)}</span></div>
              {usage.agent_name && <div className="composer-usage-agent">{usage.agent_name}</div>}
            </>
          ) : (
            <div className="composer-usage-title" style={{ color: "var(--text-3)" }}>
              暂无上下文占用数据
              <div style={{ fontSize: 10, marginTop: 4, color: "var(--text-3)" }}>发送消息后将显示用量</div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
