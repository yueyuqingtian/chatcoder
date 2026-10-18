/** 侧边对话（plan-20261017-side-session P2–P5）：右侧面板里的子会话。
 *
 * - 侧边对话是主会话的子会话：可以新建多个，各自拥有完整的消息流与工具调用；
 * - 消息流与输入框与主对话使用同一组件（MessageFlow / ComposerCore），通过会话作用域切换数据源，
 *   因此插件、技能、模式、模型、思考深度、附件的交互与主输入一致；
 * - 「引用主对话」：按需挑选主对话的若干轮，随下一条提问发送给子会话（只发送一次）；
 * - 「发回主对话」：把子会话的最后一条回答预填到主输入框，由用户确认后再发送。 */
import { useEffect, useMemo, useRef, useState } from "react";
import { api, type MessageOut, type SessionOut } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { buildContextBlock, useSideChatStore } from "../../store/sideChat";
import { ComposerCore } from "../chat/ComposerCore";
import { MessageFlow } from "../chat/MessageFlow";
import { ConvoScopeProvider } from "../chat/convoScope";
import { msgText } from "../chat/timeline";
import { ConfirmDialog } from "../ConfirmDialog";
import { Button, Empty, IconButton, Tooltip } from "../ui";
import { IconArrowUpRight, IconAt, IconMessageSquare, IconPlus, IconTrash, IconX } from "../icons";

/** 主对话的一轮：用户提问 + 其后的 AI 正文（仅作为引用候选，不含工具调用与思考） */
interface MainTurn { key: string; user: string; agent: string }

/** 每轮引用的字数上限（与后端口径保持一致，避免无谓的长请求） */
const TURN_CLIP = 4000;

function buildTurns(messages: MessageOut[]): MainTurn[] {
  const turns: MainTurn[] = [];
  const agentParts: string[][] = [];
  let cur: MainTurn | null = null;
  for (const m of messages) {
    if (m.thread_id != null) continue;
    if (m.sender_type === "user") {
      const text = msgText(m.content).trim();
      if (!text) continue;
      cur = { key: `m${m.id}`, user: text, agent: "" };
      turns.push(cur);
      agentParts.push([]);
      continue;
    }
    if (m.sender_type !== "agent" || m.msg_type !== "text" || !cur) continue;
    if ((m.content ?? {}).thinking) continue;
    const text = msgText(m.content).trim();
    if (text) agentParts[agentParts.length - 1].push(text);
  }
  turns.forEach((t, i) => { t.agent = agentParts[i].join("\n\n"); });
  return turns;
}

const clip = (s: string) => (s.length > TURN_CLIP ? `${s.slice(0, TURN_CLIP)}…（已截断）` : s);
const oneLine = (s: string, n = 40) => {
  const t = s.replace(/\s+/g, " ").trim();
  return t.length > n ? `${t.slice(0, n)}…` : t;
};

/** 子会话里最后一条非空的 AI 正文 */
function lastAssistantText(messages: MessageOut[]): string {
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (m.sender_type !== "agent" || m.msg_type !== "text" || m.thread_id != null) continue;
    if ((m.content ?? {}).thinking) continue;
    const text = msgText(m.content).trim();
    if (text) return text;
  }
  return "";
}

export function SidePanel() {
  const parentId = useChatStore((s) => s.currentSessionId);
  const mainMessages = useChatStore((s) => s.messages);
  const mainTurns = useMemo(() => buildTurns(mainMessages), [mainMessages]);

  const [children, setChildren] = useState<SessionOut[]>([]);
  const [activeId, setActiveId] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refPicking, setRefPicking] = useState(false);
  /** 引用面板是浮层：脱离文档流，展开/收起不会改变消息流高度（否则空态插图会上下跳） */
  const pickerRef = useRef<HTMLDivElement>(null);
  const quoteBtnRef = useRef<HTMLSpanElement>(null);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  /** 待删除的侧边会话（删除前确认） */
  const [removeTarget, setRemoveTarget] = useState<SessionOut | null>(null);
  const [removing, setRemoving] = useState(false);

  // 随主会话切换：重新读取它名下的侧边会话
  useEffect(() => {
    setActiveId(null);
    setChildren([]);
    setError(null);
    if (parentId == null) return;
    let cancelled = false;
    setLoading(true);
    api.listSideSessions(parentId)
      .then((list) => {
        if (cancelled) return;
        setChildren(list);
        setActiveId(list.length > 0 ? list[list.length - 1].id : null);
      })
      .catch((e) => { if (!cancelled) setError(e instanceof Error ? e.message : String(e)); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [parentId]);

  // 打开当前子会话的连接；切换或离开时关闭，避免后台连接堆积
  useEffect(() => {
    if (activeId == null) return;
    void useSideChatStore.getState().openSide(activeId);
    return () => { useSideChatStore.getState().closeSide(activeId); };
  }, [activeId]);

  // 引用面板是浮层（展开不改变消息流高度）：按项目规则补上「点外关闭 + Esc 关闭」
  useEffect(() => {
    if (!refPicking) return;
    const onDown = (e: PointerEvent) => {
      const t = e.target as Node | null;
      if (!t) return;
      if (pickerRef.current?.contains(t)) return;
      // 触发按钮自己处理开合，不在这里抢先关闭
      if (quoteBtnRef.current?.contains(t)) return;
      setRefPicking(false);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setRefPicking(false); };
    document.addEventListener("pointerdown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [refPicking]);

  const convo = useSideChatStore((s) => (activeId != null ? s.convos[activeId] : undefined));
  const convos = useSideChatStore((s) => s.convos);
  const contextDraft = convo?.contextDraft ?? "";
  const replyText = useMemo(() => lastAssistantText(convo?.messages ?? []), [convo?.messages]);
  /** 上下文块每轮以「【第 N 轮】」开头：直接数出来，切换标签后仍能准确显示 */
  const refCount = useMemo(() => (contextDraft.match(/【第 \d+ 轮】/g) ?? []).length, [contextDraft]);

  const createSide = async () => {
    if (parentId == null || creating) return;
    setCreating(true);
    setError(null);
    try {
      const child = await api.createSideSession(parentId);
      setChildren((list) => [...list, child]);
      setActiveId(child.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setCreating(false);
    }
  };

  /** 删除侧边会话：先停止运行中的 turn，再永久删除；删除当前会话后自动切到相邻一个。 */
  const removeSide = async (target: SessionOut) => {
    setRemoving(true);
    setError(null);
    try {
      if (convos[target.id]?.isRunning) {
        await useSideChatStore.getState().stopSide(target.id).catch(() => {});
      }
      await api.deleteSession(target.id, true);
      const idx = children.findIndex((c) => c.id === target.id);
      const next = children.filter((c) => c.id !== target.id);
      setChildren(next);
      if (activeId === target.id) {
        // 优先保留原位置的下一个，没有则回退到上一个
        setActiveId(next[Math.min(idx, next.length - 1)]?.id ?? null);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRemoving(false);
      setRemoveTarget(null);
    }
  };

  const togglePick = (key: string) => {
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key); else next.add(key);
      return next;
    });
  };

  /** 把所选的主对话轮次写入子会话的上下文草稿（随下一条提问发送一次） */
  const applyContext = () => {
    if (activeId == null) return;
    const chosen = mainTurns.filter((t) => picked.has(t.key));
    const block = buildContextBlock(chosen.map((t) => ({ user: clip(t.user), assistant: clip(t.agent) })));
    useSideChatStore.getState().setSideContext(activeId, block);
    setRefPicking(false);
  };

  const allPicked = mainTurns.length > 0 && picked.size === mainTurns.length;
  const toggleAll = () =>
    setPicked(allPicked ? new Set<string>() : new Set(mainTurns.map((t) => t.key)));
  /** 上下文草稿的体量：让用户在发送前知道这次会多带多少内容 */
  const draftChars = contextDraft.length;
  const draftSize = draftChars >= 1000 ? `${(draftChars / 1000).toFixed(1)}k` : String(draftChars);

  const sendBack = () => {
    if (!replyText || parentId == null) return;
    useChatStore.setState({ composerBackfill: { key: String(parentId), text: replyText, attachments: [] } });
  };

  if (parentId == null) {
    return (
      <div className="sd-panel">
        <Empty size="sm" mood="calm" title="先选择一个会话" desc="侧边对话属于某个主会话，选中会话后即可开启。" />
      </div>
    );
  }

  return (
    <div className="sd-panel">
      <div className="sd-head">
        <div className="sd-tabs" role="tablist" aria-label="侧边会话">
          {children.length === 0 && (
            <span className="sd-head-title"><IconMessageSquare size={13} />侧边对话</span>
          )}
          {children.map((c, i) => (
            <button
              key={c.id}
              type="button"
              role="tab"
              aria-selected={c.id === activeId}
              className={`sd-tab${c.id === activeId ? " is-active" : ""}`}
              onClick={() => setActiveId(c.id)}
            >
              {convos[c.id]?.isRunning && <span className="sd-tab-dot is-live" aria-hidden />}
              <span>侧边 {i + 1}</span>
            </button>
          ))}
        </div>
        <div className="sd-head-ops">
          {activeId != null && (
            <>
              <Tooltip content={refCount > 0 ? `已引用主对话 ${refCount} 轮，点击调整` : "引用主对话上下文"} side="bottom">
                <span className="sd-quote-wrap" ref={quoteBtnRef}>
                  <IconButton
                    icon={<IconAt size={14} />}
                    aria-label="引用主对话上下文"
                    aria-expanded={refPicking}
                    active={refPicking || refCount > 0}
                    size="xs"
                    onClick={() => setRefPicking((v) => !v)}
                  />
                  {refCount > 0 && !refPicking && (
                    <span className="sd-quote-badge" aria-hidden>{refCount}</span>
                  )}
                </span>
              </Tooltip>
              <Tooltip content="把最后一条回答预填到主输入框" side="bottom">
                <span>
                  <IconButton icon={<IconArrowUpRight size={13} />} aria-label="发回到主对话" size="xs"
                    disabled={!replyText} onClick={sendBack} />
                </span>
              </Tooltip>
              <Tooltip content="删除这个侧边会话" side="bottom">
                <IconButton icon={<IconTrash size={13} />} aria-label="删除侧边会话" tone="danger" size="xs"
                  disabled={removing}
                  onClick={() => setRemoveTarget(children.find((c) => c.id === activeId) ?? null)} />
              </Tooltip>
            </>
          )}
          <Tooltip content="新建侧边会话" side="bottom">
            <IconButton
              icon={<IconPlus size={14} />}
              aria-label="新建侧边会话"
              size="xs"
              disabled={creating || loading}
              onClick={() => void createSide()}
            />
          </Tooltip>
        </div>
      </div>

      {error && <div className="sd-notice" role="alert">{error}</div>}

      {activeId == null ? (
        <div className="sd-empty">
          <Empty
            size="sm"
            mood="calm"
            title={loading ? "正在读取侧边会话…" : "还没有侧边会话"}
            desc="侧边会话与主对话共享同一套输入与消息展示。可以引用主对话的若干轮作为上下文，用来开启子任务。"
          />
          {!loading && (
            <Button variant="secondary" size="sm" loading={creating} onClick={() => void createSide()}>
              新建侧边会话
            </Button>
          )}
        </div>
      ) : (
        <ConvoScopeProvider sessionId={activeId}>
          <div className="sd-convo">
            {refPicking && (
              <div className="sd-picker" ref={pickerRef}>
                <div className="sd-picker-head">
                  <div className="sd-picker-title">
                    引用主对话上下文
                    <span className="sd-picker-sub">随下一条提问发送一次，之后自动移除。</span>
                  </div>
                  <Tooltip content="收起" side="bottom">
                    <IconButton icon={<IconX size={12} />} aria-label="收起引用选择器"
                      onClick={() => setRefPicking(false)} />
                  </Tooltip>
                </div>
                {mainTurns.length === 0 ? (
                  <div className="sd-picker-empty">主对话还没有可引用的轮次。</div>
                ) : (
                  <>
                    <div className="sd-picker-list">
                      {mainTurns.map((t, i) => (
                        <label key={t.key} className={`sd-pick${picked.has(t.key) ? " is-checked" : ""}`}>
                          <input type="checkbox" checked={picked.has(t.key)} onChange={() => togglePick(t.key)} />
                          <span className="sd-pick-index">{i + 1}</span>
                          <span className="sd-pick-main">
                            <span className="sd-pick-q">{oneLine(t.user, 60)}</span>
                            <span className="sd-pick-a">{t.agent ? oneLine(t.agent, 80) : "（这一轮没有文字回答）"}</span>
                          </span>
                        </label>
                      ))}
                    </div>
                    <div className="sd-picker-foot">
                      <div className="sd-picker-left">
                        <button type="button" className="sd-link" onClick={toggleAll}>
                          {allPicked ? "取消全选" : "全选"}
                        </button>
                        <span className="sd-picker-count">已选 {picked.size} 轮</span>
                      </div>
                      <div className="sd-picker-btns">
                        <Button variant="ghost" size="xs"
                          onClick={() => { setRefPicking(false); setPicked(new Set<string>()); }}>
                          取消
                        </Button>
                        <Button variant="primary" size="xs" disabled={picked.size === 0} onClick={applyContext}>
                          引用到提问
                        </Button>
                      </div>
                    </div>
                  </>
                )}
              </div>
            )}

            <div className="sd-body">
              {/* key 按会话切换：消息流内部的构建器 / 滚动位置 / 展开态不得跨会话复用，
                  否则切换侧边标签时会短暂看到上一个会话的内容。 */}
              <MessageFlow
                key={activeId}
                features={{ jumpDots: false, search: false, planCard: false }}
                emptyText="这是新的侧边会话，直接在下方提问"
              />
            </div>

            {/* 上下文注入条紧贴输入卡：说明「只是一次性上下文」，可一键移除 */}
            {contextDraft && (
              <div className="sd-context">
                <IconMessageSquare size={12} />
                <span className="sd-context-text">
                  已引用主对话 {refCount} 轮上下文
                  <span className="sd-context-sub"> · 约 {draftSize} 字 · 随下一条提问发送一次</span>
                </span>
                <button type="button" className="sd-context-x" aria-label="取消引用"
                  onClick={() => useSideChatStore.getState().setSideContext(activeId, "")}>
                  <IconX size={12} />
                </button>
              </div>
            )}

            <div className="sd-composer">
              <ComposerCore variant="chat" />
            </div>
          </div>
        </ConvoScopeProvider>
      )}

      <ConfirmDialog
        open={removeTarget != null}
        title="删除侧边会话"
        message={removeTarget
          ? `将永久删除这个侧边会话及其全部消息与运行记录，无法恢复。若它正在运行，会先停止当前回复。`
          : ""}
        confirmLabel="删除"
        danger
        busy={removing}
        busyLabel="正在删除…"
        onCancel={() => setRemoveTarget(null)}
        onConfirm={() => { if (removeTarget) void removeSide(removeTarget); }}
      />
    </div>
  );
}
