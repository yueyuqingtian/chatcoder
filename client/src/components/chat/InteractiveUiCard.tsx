/** InteractiveUiCard —— 消息流内嵌可交互界面与多版式比选卡片（plan-interactive-ui）。
 *
 *  职责：
 *  1. 解析 `present_ui` 工具调用的归一化载荷（优先取 `tool.result` 的 `ui_payload`，
 *     兼容运行中 `runningToolResults` 实时结果与历史参数回退）；
 *  2. 多版式支持「标签页 / 并排对比网格」切换，单选 / 多选勾选态持久化到 localStorage，
 *     虚拟列表滚动卸载后勾选与批注不丢失；
 *  3. 源码查看走 `ChatCollapse` 平滑高度过渡，大窗查看走 `Dialog`；
 *  4. 决策底栏支持「填入输入框」（注入 `ui-selection` chip）与「选定并继续」（直发下一轮）。
 */
import { memo, useCallback, useEffect, useMemo, useState } from "react";
import type { MessageOut } from "@chatcoder/shared";
import type { ComposerRefOut } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { useDraftsStore, type ComposerRefDraft } from "../../store/drafts";
import { notify } from "../../store/toast";
import {
  IconBarChart,
  IconCheck,
  IconCode,
  IconCopy,
  IconGitGraph,
  IconLayoutGrid,
  IconMaximize,
  IconNoWrap,
  IconSparkles,
  IconSpinner,
  IconWrapText,
} from "../icons";
import { Button, Dialog, Input, Tooltip } from "../ui";
import { highlightCodeLines } from "../ui/LongCode";
import { ChatCollapse } from "./ChatCollapse";
import { InteractiveSandbox } from "./InteractiveSandbox";
import { useScoped } from "./convoScope";

export interface UiVariant {
  id: string;
  label: string;
  summary?: string;
  html: string;
  file_path?: string;
  height?: number;
}

export interface UiSelectionConfig {
  enabled: boolean;
  mode: "single" | "multiple";
  confirm_label: string;
  allow_note: boolean;
}

export interface UiPayload {
  title: string;
  kind: "comparison" | "chart" | "diagram" | "component" | "page";
  description: string;
  layout: "tabs" | "grid";
  selection: UiSelectionConfig;
  variants: UiVariant[];
}

interface PersistedSelection {
  checkedIds: string[];
  note: string;
  submitted?: { ids: string[]; labels: string[]; note: string } | null;
  updatedAt: number;
}

const SELECTION_STORAGE_KEY = "chatcoder.ui-selections";
const MAX_SAVED_SELECTIONS = 100;

function readSavedMap(): Record<string, PersistedSelection> {
  if (typeof window === "undefined") return {};
  try {
    const raw = localStorage.getItem(SELECTION_STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? (parsed as Record<string, PersistedSelection>) : {};
  } catch {
    return {};
  }
}

function getSavedSelection(key: string): PersistedSelection | null {
  if (!key) return null;
  return readSavedMap()[key] ?? null;
}

function writeSavedSelection(key: string, value: PersistedSelection): void {
  if (typeof window === "undefined" || !key) return;
  try {
    const map = readSavedMap();
    map[key] = value;
    const entries = Object.entries(map);
    if (entries.length > MAX_SAVED_SELECTIONS) {
      entries.sort((a, b) => (a[1]?.updatedAt ?? 0) - (b[1]?.updatedAt ?? 0));
      const trimmed = Object.fromEntries(entries.slice(entries.length - MAX_SAVED_SELECTIONS));
      localStorage.setItem(SELECTION_STORAGE_KEY, JSON.stringify(trimmed));
      return;
    }
    localStorage.setItem(SELECTION_STORAGE_KEY, JSON.stringify(map));
  } catch { /* 存储空间受限时忽略 */ }
}

function normalizePayload(raw: unknown): UiPayload | null {
  if (!raw || typeof raw !== "object") return null;
  const obj = raw as Record<string, unknown>;
  const title = typeof obj.title === "string" && obj.title.trim() ? obj.title.trim() : "交互界面";
  const rawKind = typeof obj.kind === "string" ? obj.kind.toLowerCase() : "component";
  const kind: UiPayload["kind"] = (
    rawKind === "comparison" || rawKind === "chart" || rawKind === "diagram" || rawKind === "page"
  ) ? rawKind : "component";
  const description = typeof obj.description === "string" ? obj.description.trim() : "";
  const layout: UiPayload["layout"] = obj.layout === "grid" ? "grid" : "tabs";

  const variants: UiVariant[] = [];
  if (Array.isArray(obj.variants)) {
    obj.variants.forEach((v, idx) => {
      if (!v || typeof v !== "object") return;
      const item = v as Record<string, unknown>;
      const html = typeof item.html === "string" ? item.html : "";
      if (!html.trim()) return;
      variants.push({
        id: typeof item.id === "string" && item.id.trim() ? item.id.trim() : `v${idx + 1}`,
        label: typeof item.label === "string" && item.label.trim() ? item.label.trim() : `方案 ${idx + 1}`,
        summary: typeof item.summary === "string" ? item.summary.trim() : undefined,
        html,
        file_path: typeof item.file_path === "string" ? item.file_path : undefined,
        height: typeof item.height === "number" ? item.height : undefined,
      });
    });
  } else if (typeof obj.html === "string" && obj.html.trim()) {
    variants.push({ id: "v1", label: title, html: obj.html });
  }

  if (variants.length === 0) return null;

  const rawSel = (obj.selection && typeof obj.selection === "object" ? obj.selection : {}) as Record<string, unknown>;
  const enabled = typeof rawSel.enabled === "boolean" ? rawSel.enabled : variants.length > 1;
  const mode = rawSel.mode === "multiple" ? "multiple" : "single";
  const confirmLabel = typeof rawSel.confirm_label === "string" && rawSel.confirm_label.trim()
    ? rawSel.confirm_label.trim()
    : "选定并继续";
  const allowNote = typeof rawSel.allow_note === "boolean" ? rawSel.allow_note : true;

  return {
    title,
    kind,
    description,
    layout,
    selection: { enabled, mode, confirm_label: confirmLabel, allow_note: allowNote },
    variants,
  };
}

function buildSelectionDetail(payload: UiPayload, chosen: UiVariant[], callKey: string): string {
  const blocks = chosen.map((v) => {
    const src = v.html.length > 6000 ? `${v.html.slice(0, 6000)}\n<!-- 源码过长已截断 -->` : v.html;
    const pathLine = v.file_path ? `\n文件路径：${v.file_path}` : "";
    const sumLine = v.summary ? `\n方案说明：${v.summary}` : "";
    return `版式标识：${v.id}（${v.label}）${pathLine}${sumLine}\n\`\`\`html\n${src}\n\`\`\``;
  });
  return `交互卡片「${payload.title}」（call_key=${callKey}）\n${blocks.join("\n\n")}`;
}

function kindIcon(kind: UiPayload["kind"]) {
  if (kind === "chart") return <IconBarChart size={14} />;
  if (kind === "diagram") return <IconGitGraph size={14} />;
  if (kind === "comparison") return <IconLayoutGrid size={14} />;
  return <IconSparkles size={14} />;
}

const KIND_SUB_LABEL: Record<UiPayload["kind"], string> = {
  comparison: "版式比选",
  chart: "交互图表",
  diagram: "流程图",
  component: "交互组件",
  page: "原型页面",
};

function summarizeBridgeAction(action: string, payload: unknown): string {
  if (payload && typeof payload === "object") {
    const p = payload as Record<string, unknown>;
    if (typeof p.summary === "string" && p.summary.trim()) return p.summary.trim();
    try {
      const compact = JSON.stringify(p);
      return `${action ? `${action}：` : ""}${compact.length > 120 ? `${compact.slice(0, 120)}…` : compact}`;
    } catch { /* ignore */ }
  }
  return payload != null ? `${action ? `${action}：` : ""}${String(payload)}` : action || "组件触发了交互操作";
}

/** 交互卡片内嵌源码视图：复用共享 Highlight Worker 语法高亮、行号槽、换行切换与常驻可见滚动条 */
const SourceCodePane = memo(function SourceCodePane({ source, filePath }: { source: string; filePath?: string }) {
  const [wrapped, setWrapped] = useState(false);
  const [copied, setCopied] = useState(false);
  const [highlighted, setHighlighted] = useState<{ source: string; lines: string[] } | null>(null);

  useEffect(() => {
    setHighlighted(null);
    return highlightCodeLines(source, "html", (lines) => {
      if (lines) setHighlighted({ source, lines });
    });
  }, [source]);

  const rawLines = useMemo(() => source.split(/\r?\n/), [source]);
  const hlLines = highlighted?.source === source ? highlighted.lines : null;
  const lineCount = rawLines.length;
  const gutterCh = Math.max(2, String(lineCount).length) + 1;
  const sizeKb = Math.max(0.1, source.length / 1024).toFixed(1);

  const handleCopy = () => {
    if (!source) return;
    void navigator.clipboard?.writeText(source).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    }).catch(() => {});
  };

  return (
    <div className="iui-source-wrap">
      <div className="iui-source-head">
        <div className="iui-source-meta">
          <span className="iui-source-badge">HTML</span>
          <span className="iui-source-stat">{lineCount} 行 · {sizeKb} KB</span>
          {filePath && <span className="iui-source-path">{filePath}</span>}
        </div>
        <div className="md-code-actions">
          <Tooltip content={wrapped ? "当前：自动换行（点击关闭）" : "当前：不自动换行（点击开启）"} side="top">
            <button
              type="button"
              className={`md-code-action-btn${wrapped ? " is-active" : ""}`}
              aria-label={wrapped ? "关闭自动换行" : "开启自动换行"}
              aria-pressed={wrapped}
              onClick={() => setWrapped((v) => !v)}
            >
              {wrapped ? <IconWrapText size={14} /> : <IconNoWrap size={14} />}
            </button>
          </Tooltip>
          <Tooltip content={copied ? "已复制到剪贴板" : "复制源码"} side="top">
            <button
              type="button"
              className={`md-code-copy-btn${copied ? " copied" : ""}`}
              aria-label={copied ? "已复制源码" : "复制源码"}
              onClick={handleCopy}
            >
              {copied ? <IconCheck size={14} /> : <IconCopy size={14} />}
            </button>
          </Tooltip>
        </div>
      </div>
      <pre
        className={`md-code-pre iui-source-pre${wrapped ? " is-wrapped" : ""}`}
        style={{ ["--iui-src-gutter" as string]: `${gutterCh}ch` }}
      >
        <code>
          {rawLines.map((line, i) => (
            <span key={i} className="iui-src-line">
              <span className="iui-src-ln" aria-hidden="true">{i + 1}</span>
              {hlLines && hlLines[i] !== undefined ? (
                <span className="iui-src-code" dangerouslySetInnerHTML={{ __html: hlLines[i] || " " }} />
              ) : (
                <span className="iui-src-code">{line || " "}</span>
              )}
            </span>
          ))}
        </code>
      </pre>
    </div>
  );
});

export const InteractiveUiCard = memo(function InteractiveUiCard({
  callKey,
  msg,
  result,
  live = false,
}: {
  callKey: string;
  msg: MessageOut;
  result?: MessageOut;
  live?: boolean;
}) {
  const sessionId = useScoped("currentSessionId");
  const sendTurn = useScoped("sendTurn");
  const liveResult = useChatStore((s) => s.runningToolResults[callKey]);

  const payload = useMemo(() => {
    const rc = (result?.content ?? {}) as Record<string, unknown>;
    const callArgs = (msg.content?.args && typeof msg.content.args === "object"
      ? msg.content.args : {}) as Record<string, unknown>;
    return (
      normalizePayload(liveResult?.ui_payload)
      ?? normalizePayload(rc.ui_payload)
      ?? normalizePayload(callArgs)
    );
  }, [liveResult, result, msg]);

  const storageKey = `${sessionId ?? msg.session_id ?? 0}:${callKey}`;
  const [savedState, setSavedState] = useState<PersistedSelection>(() => (
    getSavedSelection(storageKey) ?? { checkedIds: [], note: "", submitted: null, updatedAt: 0 }
  ));

  useEffect(() => {
    setSavedState(getSavedSelection(storageKey) ?? { checkedIds: [], note: "", submitted: null, updatedAt: 0 });
  }, [storageKey]);

  const updateSaved = useCallback((updater: (prev: PersistedSelection) => PersistedSelection) => {
    setSavedState((prev) => {
      const next = { ...updater(prev), updatedAt: Date.now() };
      writeSavedSelection(storageKey, next);
      return next;
    });
  }, [storageKey]);

  const [layout, setLayout] = useState<"tabs" | "grid">(() => payload?.layout ?? "tabs");
  const [viewingId, setViewingId] = useState<string>(() => (
    savedState.checkedIds[0] ?? payload?.variants[0]?.id ?? ""
  ));
  const [showSource, setShowSource] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [modalCopied, setModalCopied] = useState(false);
  const [sending, setSending] = useState(false);
  const [bridgeMsg, setBridgeMsg] = useState<{ summary: string; raw: unknown; action: string } | null>(null);

  useEffect(() => {
    if (!payload) return;
    setLayout(payload.layout);
    setViewingId((cur) => {
      if (cur && payload.variants.some((v) => v.id === cur)) return cur;
      return savedState.checkedIds.find((id) => payload.variants.some((v) => v.id === id))
        ?? payload.variants[0]?.id
        ?? "";
    });
  }, [payload, savedState.checkedIds]);

  const resultContent = (result?.content ?? {}) as Record<string, unknown>;
  const failed = (resultContent.ok === false || liveResult?.ok === false) && !payload;
  const errText = failed
    ? String(resultContent.error || resultContent.output || liveResult?.output_preview || "交互界面生成失败").trim()
    : "";

  if (failed) {
    return (
      <div className="turn-item turn-item-iui">
        <div className="iui-card">
          <div className="iui-error-bar">{errText}</div>
        </div>
      </div>
    );
  }

  if (!payload) {
    return (
      <div className="turn-item turn-item-iui">
        <div className="iui-card" aria-busy={live}>
          <div className="iui-skeleton">
            {live && <IconSpinner size={14} />}
            <span>{live ? "正在生成可交互界面…" : "正在加载交互界面…"}</span>
          </div>
        </div>
      </div>
    );
  }

  const variants = payload.variants;
  const current = variants.find((v) => v.id === viewingId) ?? variants[0];
  const checkedSet = new Set(savedState.checkedIds.filter((id) => variants.some((v) => v.id === id)));
  const chosenVariants = variants.filter((v) => checkedSet.has(v.id));
  const isMulti = variants.length > 1;
  const sel = payload.selection;
  const submitted = savedState.submitted;

  const toggleVariantCheck = (vid: string) => {
    if (submitted) return;
    updateSaved((prev) => {
      if (sel.mode === "single") {
        return { ...prev, checkedIds: prev.checkedIds.includes(vid) && prev.checkedIds.length === 1 ? [] : [vid] };
      }
      const has = prev.checkedIds.includes(vid);
      return {
        ...prev,
        checkedIds: has ? prev.checkedIds.filter((x) => x !== vid) : [...prev.checkedIds, vid],
      };
    });
    setViewingId(vid);
  };

  const buildRefPayload = (): ComposerRefOut => {
    const labels = chosenVariants.map((v) => v.label);
    return {
      kind: "ui-selection",
      value: `${payload.title} · ${chosenVariants.map((v) => v.id).join("+")}`,
      label: labels.join("、"),
      detail: buildSelectionDetail(payload, chosenVariants, callKey),
    };
  };

  const handleFillComposer = () => {
    if (chosenVariants.length === 0) return;
    const refOut = buildRefPayload();
    const note = savedState.note.trim();
    const draftKey = sessionId != null ? String(sessionId) : "home";
    const drafts = useDraftsStore.getState();
    const prevRefs = drafts.getDraft(draftKey)?.refs ?? [];
    const nextItem: ComposerRefDraft = {
      kind: "ui-selection",
      value: refOut.value,
      label: refOut.label,
      detail: refOut.detail,
    };
    const existIdx = prevRefs.findIndex((r) => r.kind === "ui-selection" && r.value === refOut.value);
    const nextRefs = existIdx >= 0
      ? prevRefs.map((r, i) => (i === existIdx ? nextItem : r))
      : [...prevRefs, nextItem];
    drafts.patchDraft(draftKey, { refs: nextRefs });

    window.dispatchEvent(new CustomEvent("chatcoder:composer-ui-selection", {
      detail: {
        sessionId,
        value: refOut.value,
        label: refOut.label,
        detailText: refOut.detail,
        note,
      },
    }));
    notify.success("已填入输入框，可继续补充说明后发送");
  };

  const handleConfirmAndContinue = async () => {
    if (chosenVariants.length === 0 || sending) return;
    setSending(true);
    try {
      const refOut = buildRefPayload();
      const note = savedState.note.trim();
      const labels = chosenVariants.map((v) => v.label);
      const userPrompt = note || `就按选定的「${labels.join("、")}」方案继续完善并实现。`;
      const suffix = `已选交互方案：${refOut.label}（${refOut.value}）`;
      await sendTurn(`${userPrompt}\n\n${suffix}`, undefined, undefined, undefined, undefined, [refOut]);
      updateSaved((prev) => ({
        ...prev,
        submitted: {
          ids: chosenVariants.map((v) => v.id),
          labels,
          note,
        },
      }));
    } catch (e) {
      notify.error(`发送失败：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setSending(false);
    }
  };

  const handleSendBridge = async () => {
    if (!bridgeMsg || sending) return;
    setSending(true);
    try {
      let jsonText = "";
      try {
        jsonText = JSON.stringify(bridgeMsg.raw);
      } catch {
        jsonText = String(bridgeMsg.raw ?? "");
      }
      const prompt = `基于交互界面「${payload.title}」（${current.label}）触发的动作 ${bridgeMsg.action || "interact"}：${bridgeMsg.summary}`
        + (jsonText && jsonText !== "{}" ? `\n参数：\`${jsonText.slice(0, 600)}\`` : "");
      await sendTurn(prompt);
      setBridgeMsg(null);
    } catch (e) {
      notify.error(`发送失败：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setSending(false);
    }
  };

  const handleSandboxAction = (action: string, raw: unknown) => {
    setBridgeMsg({
      action,
      raw,
      summary: summarizeBridgeAction(action, raw),
    });
  };

  const currentChecked = checkedSet.has(current.id);
  const metaSub = isMulti
    ? `· ${variants.length} 套版式${sel.enabled ? (sel.mode === "multiple" ? " · 多选" : " · 单选") : ""}`
    : `· ${KIND_SUB_LABEL[payload.kind]}`;

  return (
    <div className="turn-item turn-item-iui">
      <article className="iui-card" aria-label={payload.title}>
        <div className="iui-head">
          <div className="iui-head-meta">
            {kindIcon(payload.kind)}
            <span className="iui-title">{payload.title}</span>
            <span className="iui-sub">{metaSub}</span>
          </div>
          <div className="iui-head-tools">
            {isMulti && (
              <div className="iui-seg" role="tablist" aria-label="版式排列方式">
                <button
                  type="button"
                  role="tab"
                  aria-selected={layout === "tabs"}
                  className={`iui-seg-btn${layout === "tabs" ? " is-active" : ""}`}
                  onClick={() => setLayout("tabs")}
                >
                  标签
                </button>
                <button
                  type="button"
                  role="tab"
                  aria-selected={layout === "grid"}
                  className={`iui-seg-btn${layout === "grid" ? " is-active" : ""}`}
                  onClick={() => setLayout("grid")}
                >
                  对比
                </button>
              </div>
            )}
            <Tooltip content={showSource ? "收起源码" : "查看源码"} side="top">
              <button
                type="button"
                className={`md-code-action-btn${showSource ? " is-active" : ""}`}
                aria-label={showSource ? "收起源码" : "查看源码"}
                aria-expanded={showSource}
                onClick={() => setShowSource((v) => !v)}
              >
                <IconCode size={14} />
              </button>
            </Tooltip>
            <Tooltip content="大窗查看" side="top">
              <button
                type="button"
                className="md-code-action-btn"
                aria-label="大窗查看"
                onClick={() => setModalOpen(true)}
              >
                <IconMaximize size={13} />
              </button>
            </Tooltip>
          </div>
        </div>

        {isMulti && layout === "tabs" && (
          <div className="iui-tabs-bar">
            <div className="iui-tab-list" role="tablist" aria-label="切换预览版式">
              {variants.map((v) => {
                const viewing = v.id === current.id;
                const picked = checkedSet.has(v.id);
                return (
                  <button
                    key={v.id}
                    type="button"
                    role="tab"
                    aria-selected={viewing}
                    className={`iui-tab${viewing ? " is-viewing" : ""}`}
                    onClick={() => setViewingId(v.id)}
                  >
                    {picked && <span className="iui-tab-dot" aria-label="已勾选" />}
                    <span>{v.label}</span>
                  </button>
                );
              })}
            </div>
            {sel.enabled && !submitted && (
              <button
                type="button"
                className={`iui-pick-btn${currentChecked ? " is-picked" : ""}`}
                onClick={() => toggleVariantCheck(current.id)}
                aria-pressed={currentChecked}
              >
                <span className={`iui-check-box${sel.mode === "single" ? " is-radio" : ""}`}>
                  {currentChecked && <IconCheck size={10} />}
                </span>
                <span>{currentChecked ? "已勾选此版式" : "勾选此版式"}</span>
              </button>
            )}
          </div>
        )}

        <div className="iui-stage">
          {layout === "tabs" || !isMulti ? (
            <InteractiveSandbox
              key={current.id}
              html={current.html}
              cacheKey={`${callKey}:${current.id}`}
              minHeight={current.height ? Math.min(240, current.height) : 120}
              maxHeight={500}
              title={`${payload.title} · ${current.label}`}
              onAction={handleSandboxAction}
            />
          ) : (
            <div className="iui-grid">
              {variants.map((v) => {
                const checked = checkedSet.has(v.id);
                return (
                  <div key={v.id} className={`iui-grid-cell${checked ? " is-checked" : ""}`}>
                    <div className="iui-grid-head">
                      {sel.enabled && !submitted ? (
                        <button
                          type="button"
                          className={`iui-grid-pick${checked ? " is-checked" : ""}`}
                          onClick={() => toggleVariantCheck(v.id)}
                          aria-pressed={checked}
                        >
                          <span className={`iui-check-box${sel.mode === "single" ? " is-radio" : ""}`}>
                            {checked && <IconCheck size={10} />}
                          </span>
                          <span className="iui-grid-pick-label">{v.label}</span>
                        </button>
                      ) : (
                        <span className="iui-grid-pick-label">{v.label}</span>
                      )}
                      <Tooltip content="大窗查看此版式" side="top">
                        <button
                          type="button"
                          className="md-code-action-btn"
                          aria-label={`大窗查看 ${v.label}`}
                          onClick={() => { setViewingId(v.id); setModalOpen(true); }}
                        >
                          <IconMaximize size={13} />
                        </button>
                      </Tooltip>
                    </div>
                    <InteractiveSandbox
                      html={v.html}
                      cacheKey={`${callKey}:grid:${v.id}`}
                      minHeight={v.height ? Math.min(200, v.height) : 120}
                      maxHeight={340}
                      title={`${payload.title} · ${v.label}`}
                      onAction={handleSandboxAction}
                    />
                    {v.summary && <div className="iui-grid-note">{v.summary}</div>}
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {(layout === "tabs" || !isMulti) && (current.summary || payload.description) && (
          <div className="iui-caption">{current.summary || payload.description}</div>
        )}

        <ChatCollapse open={showSource}>
          <SourceCodePane source={current.html} filePath={current.file_path} />
        </ChatCollapse>

        {bridgeMsg && (
          <div className="iui-bridge-bar">
            <span>{bridgeMsg.summary}</span>
            <div className="iui-bridge-actions">
              <Button size="xs" variant="ghost" disabled={sending} onClick={() => setBridgeMsg(null)}>
                忽略
              </Button>
              <Button size="xs" variant="primary" loading={sending} onClick={() => void handleSendBridge()}>
                发给 AI
              </Button>
            </div>
          </div>
        )}

        {sel.enabled && (
          submitted ? (
            <div className="iui-done-bar">
              <span className="iui-done-label">
                <IconCheck size={13} />
                <span>
                  已选定：{submitted.labels.join("、")}
                  {submitted.note ? `（${submitted.note}）` : ""}
                </span>
              </span>
              <Button
                size="xs"
                variant="secondary"
                onClick={() => updateSaved((prev) => ({ ...prev, submitted: null }))}
              >
                重新选择
              </Button>
            </div>
          ) : (
            <div className="iui-decide">
              <div className="iui-decide-top">
                <span className="iui-picked-badge">
                  已选 <strong>{chosenVariants.length > 0 ? chosenVariants.map((v) => v.label).join("、") : "未选择"}</strong>
                </span>
                {sel.allow_note && (
                  <Input
                    size="sm"
                    className="iui-note-input"
                    value={savedState.note}
                    placeholder="补充调整意见（可选，如：把主色换成天空蓝）…"
                    aria-label="补充调整意见"
                    onChange={(e) => {
                      const val = e.target.value;
                      updateSaved((prev) => ({ ...prev, note: val }));
                    }}
                  />
                )}
              </div>
              <div className="iui-decide-foot">
                <span className="iui-decide-hint">确认后将把选中版式的源码与意见发回 AI 继续处理</span>
                <div className="iui-decide-actions">
                  <Button
                    size="sm"
                    variant="secondary"
                    disabled={chosenVariants.length === 0 || sending}
                    onClick={handleFillComposer}
                  >
                    填入输入框
                  </Button>
                  <Button
                    size="sm"
                    variant="primary"
                    disabled={chosenVariants.length === 0}
                    loading={sending}
                    onClick={() => void handleConfirmAndContinue()}
                  >
                    {sel.confirm_label}
                  </Button>
                </div>
              </div>
            </div>
          )
        )}
      </article>

      <Dialog
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        className="iui-modal-dialog"
        bodyClassName="iui-modal-body"
        title={
          <span className="iui-modal-title-row">
            <span className="iui-modal-kind">{KIND_SUB_LABEL[payload.kind]}</span>
            <span className="iui-modal-title-text">
              {payload.title}{isMulti ? ` · ${current.label}` : ""}
            </span>
          </span>
        }
        subtitle={current.summary || payload.description || undefined}
        width={1400}
        actions={
          <div className="iui-modal-head-actions">
            {sel.enabled && !submitted && (
              <button
                type="button"
                className={`iui-pick-btn${currentChecked ? " is-picked" : ""}`}
                onClick={() => toggleVariantCheck(current.id)}
                aria-pressed={currentChecked}
              >
                <span className={`iui-check-box${sel.mode === "single" ? " is-radio" : ""}`}>
                  {currentChecked && <IconCheck size={10} />}
                </span>
                <span>{currentChecked ? "已勾选此版式" : "勾选此版式"}</span>
              </button>
            )}
            <Tooltip content={modalCopied ? "已复制到剪贴板" : "复制当前源码"} side="bottom">
              <button
                type="button"
                className={`md-code-copy-btn${modalCopied ? " copied" : ""}`}
                aria-label={modalCopied ? "已复制源码" : "复制当前源码"}
                onClick={() => {
                  if (!current.html) return;
                  void navigator.clipboard?.writeText(current.html).then(() => {
                    setModalCopied(true);
                    window.setTimeout(() => setModalCopied(false), 1600);
                  }).catch(() => {});
                }}
              >
                {modalCopied ? <IconCheck size={14} /> : <IconCopy size={14} />}
              </button>
            </Tooltip>
          </div>
        }
        footer={sel.enabled && !submitted ? (
          <div className="iui-modal-footer">
            <span className="iui-picked-badge">
              已选 <strong>{chosenVariants.length > 0 ? chosenVariants.map((v) => v.label).join("、") : "未选择"}</strong>
            </span>
            <div className="iui-decide-actions">
              <Button
                size="sm"
                variant="secondary"
                disabled={chosenVariants.length === 0 || sending}
                onClick={() => { handleFillComposer(); setModalOpen(false); }}
              >
                填入输入框
              </Button>
              <Button
                size="sm"
                variant="primary"
                disabled={chosenVariants.length === 0}
                loading={sending}
                onClick={() => { setModalOpen(false); void handleConfirmAndContinue(); }}
              >
                {sel.confirm_label}
              </Button>
            </div>
          </div>
        ) : undefined}
      >
        {isMulti && (
          <div className="iui-modal-tabs">
            <div className="iui-tab-list" role="tablist" aria-label="大窗切换版式">
              {variants.map((v) => (
                <button
                  key={v.id}
                  type="button"
                  role="tab"
                  aria-selected={v.id === current.id}
                  className={`iui-tab${v.id === current.id ? " is-viewing" : ""}`}
                  onClick={() => setViewingId(v.id)}
                >
                  {checkedSet.has(v.id) && <span className="iui-tab-dot" aria-label="已勾选" />}
                  <span>{v.label}</span>
                </button>
              ))}
            </div>
          </div>
        )}
        <div className="iui-modal-stage">
          <InteractiveSandbox
            key={`modal:${current.id}`}
            html={current.html}
            fillHeight
            title={`${payload.title} · 大窗预览`}
            onAction={handleSandboxAction}
          />
        </div>
      </Dialog>
    </div>
  );
});
