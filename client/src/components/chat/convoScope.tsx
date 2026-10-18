/** 会话作用域（plan-20261017-side-session P2/P3）：让消息流与输入框的共享组件无感地切换数据源。
 *
 * - 没有作用域（主对话）：读取主聊天仓库（chat.ts），行为与改造前完全一致；
 * - 处于 ConvoScopeProvider（侧边会话）：会话相关字段读取侧边仓库（sideChat.ts），动作绑定到该子会话。
 * 与会话无关的全局字段（模型、项目、会话列表等）两种情况都读取主仓库。 */
import { createContext, useContext, type ReactNode } from "react";
import { useChatStore } from "../../store/chat";
import { useSideChatStore, type SideConvo } from "../../store/sideChat";
import type { ComposerRefOut } from "../../api/client";

type MainState = ReturnType<typeof useChatStore.getState>;

export const ConvoScopeContext = createContext<number | null>(null);

export function ConvoScopeProvider({ sessionId, children }: { sessionId: number; children: ReactNode }) {
  return <ConvoScopeContext.Provider value={sessionId}>{children}</ConvoScopeContext.Provider>;
}

/** 作用域内需要覆盖的字段（与主仓库同名）。未列出的字段一律读主仓库。 */
const SCOPED_KEYS = new Set<string>([
  "currentSessionId",
  "messages", "turns", "streamingBuffers", "thinkingBuffers", "runningTurnId", "isRunning", "turnStatus",
  "pendingApproval", "approvalQueue", "respondApproval",
  "flowError", "clearFlowError", "isCompacting", "subagentMeta", "pendingSubagents", "pendingPlan",
  "plansByTurn", "hiddenTurnErrors", "dismissTurnError", "debugState",
  "sendTurn", "stopSessionExecution", "cancelTurn", "questionDraft", "setQuestionDraft",
  "queuedInputs", "updateQueuedInput", "flushQueuedInput", "usage",
  "composerBrowserRefs", "removeComposerBrowserRef", "clearComposerBrowserRefs", "refreshMessages",
]);

const EMPTY_MESSAGES: MainState["messages"] = [];
const EMPTY_TURNS: MainState["turns"] = [];
const EMPTY_OBJ: Record<string, never> = {};
const EMPTY_ARR: never[] = [];
const NOOP = () => {};

/** 每个子会话的动作函数只创建一次，保证引用稳定（避免无意义的重渲染）。 */
const actionCache = new Map<number, Record<string, unknown>>();
function sideActions(sid: number): Record<string, unknown> {
  let cached = actionCache.get(sid);
  if (!cached) {
    const side = () => useSideChatStore.getState();
    cached = {
      sendTurn: (content: string, attachments?: Record<string, unknown>[], reasoningEffort?: string, mode?: string | null, modelId?: number | null, refs?: ComposerRefOut[]) => {
        void side().sendSide(sid, {
          content,
          attachments,
          reasoningEffort,
          mode,
          modelId,
          refs,
        });
        return Promise.resolve(null);
      },
      stopSessionExecution: () => { void side().stopSide(sid); },
      cancelTurn: () => { void side().stopSide(sid); },
      clearComposerBrowserRefs: NOOP,
      refreshMessages: () => side().refreshSide(sid),
      respondApproval: (approvalId: string, approved: boolean, remember?: boolean, answer?: Record<string, unknown>) => {
        side().respondSide(sid, approvalId, approved, remember, answer);
      },
      setQuestionDraft: NOOP,
      clearFlowError: () => side().clearSideError(sid),
      dismissTurnError: NOOP,
      updateQueuedInput: NOOP,
      flushQueuedInput: NOOP,
      removeComposerBrowserRef: NOOP,
    };
    actionCache.set(sid, cached);
  }
  return cached;
}

function sideField(c: SideConvo | undefined, sid: number, key: string): unknown {
  switch (key) {
    case "currentSessionId": return sid;
    case "messages": return c?.messages ?? EMPTY_MESSAGES;
    case "turns": return c?.turns ?? EMPTY_TURNS;
    case "streamingBuffers": return c?.streamingBuffers ?? EMPTY_OBJ;
    case "thinkingBuffers": return c?.thinkingBuffers ?? EMPTY_OBJ;
    case "runningTurnId": return c?.runningTurnId ?? null;
    case "isRunning": return c?.isRunning ?? false;
    case "turnStatus": return c?.turnStatus ?? null;
    case "pendingApproval": return c?.pendingApproval ?? null;
    case "approvalQueue": return c?.approvalQueue ?? EMPTY_ARR;
    case "flowError": return c?.error ?? null;
    case "isCompacting": return false;
    case "subagentMeta": return EMPTY_OBJ;
    case "pendingSubagents": return EMPTY_ARR;
    case "pendingPlan": return null;
    case "plansByTurn": return EMPTY_OBJ;
    case "hiddenTurnErrors": return EMPTY_ARR;
    case "debugState": return null;
    case "questionDraft": return null;
    case "queuedInputs": return EMPTY_ARR;
    case "usage": return null;
    case "composerBrowserRefs": return EMPTY_ARR;
    default: return sideActions(sid)[key];
  }
}

/** 读取一个会话相关字段：有作用域读侧边仓库，否则读主仓库。 */
export function useScoped<K extends keyof MainState & string>(key: K): MainState[K] {
  const sid = useContext(ConvoScopeContext);
  const mainVal = useChatStore((s) => s[key]);
  const sideVal = useSideChatStore((st) => (sid != null && SCOPED_KEYS.has(key) ? sideField(st.convos[sid], sid, key) : undefined));
  if (sid != null && SCOPED_KEYS.has(key)) return sideVal as MainState[K];
  return mainVal;
}
/** 在事件回调里读取当前作用域的完整视图（替代 useChatStore.getState()）。 */
export function useScopedGetter(): () => MainState {
  const sid = useContext(ConvoScopeContext);
  return () => {
    const main = useChatStore.getState();
    if (sid == null) return main;
    const st = useSideChatStore.getState();
    const view: Record<string, unknown> = { ...main };
    for (const key of SCOPED_KEYS) view[key] = sideField(st.convos[sid], sid, key);
    return view as unknown as MainState;
  };
}
