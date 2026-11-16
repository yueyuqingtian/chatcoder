/** 微应用引用注入（plan-693-2868 §7 P4-2 / P1）。
 *
 *  「注入到输入框」要跨两个互斥视图生效，光靠事件不够：
 *  - 左面板「拓展 → 微应用」打开时，中间区域被 NavPage 顶替（Workspace.tsx:30-39），
 *    ComposerCore **没有挂载**——这时派发事件没有任何监听者，用户会看到「已注入」但输入框没变化；
 *  - 回到对话页时 ComposerCore 才挂载，它从 `store/drafts.ts` 的草稿恢复 refs。
 *
 *  所以这里双写：
 *  ① patch 当前草稿（按 isHome 口径取 key）——保证切回对话时 chip 在；
 *  ② 再派发 `chatcoder:composer-microapp`——ComposerCore 已挂载时立即生效。
 *     事件只传 {appId,label,version}，chip 的构造与去重规则仍以 ComposerCore 为权威
 *     （ComposerCore.insertMicroApp 与这里的 dedupe 口径一致：同 appId 不重复追加）。
 *
 *  本模块**不打开右侧面板**：注入 ≠ 打开界面（§7 P1 验收）。
 */
import type { ComposerRefDraft } from "../store/drafts";
import { useDraftsStore } from "../store/drafts";
import { useChatStore } from "../store/chat";

/** 事件名：ComposerCore 里同名监听（本文件与它是一对，改名两处一起改） */
export const COMPOSER_MICROAPP_EVENT = "chatcoder:composer-microapp";

export interface MicroAppInjectInput {
  appId: string;
  label: string;
  /** 记下版本，「查看界面」按同一版本开标签 */
  version?: string;
}

/** 当前草稿 key：与 ComposerCore 的 draftKey 口径一致。
 *  ComposerCore 用 `isHome ? "home" : currentSessionId ?? "new"`；这里在会话内取会话 key，
 *  没有会话时用 home（首页输入框与「新建任务」共用 home 草稿）。 */
function activeDraftKey(): string {
  const sid = useChatStore.getState().currentSessionId;
  return sid != null ? String(sid) : "home";
}

/** 把微应用引用写进草稿（供 ComposerCore 挂载时恢复）。返回是否真的写入了新 ref。 */
function patchDraftRefs(input: MicroAppInjectInput): boolean {
  const key = activeDraftKey();
  const store = useDraftsStore.getState();
  const prev = store.getDraft(key)?.refs ?? [];
  if (prev.some((r) => r.kind === "microapp" && r.value === input.appId)) return false;
  const next: ComposerRefDraft[] = [
    ...prev,
    { kind: "microapp", value: input.appId, label: input.label, version: input.version },
  ];
  store.patchDraft(key, { refs: next });
  return true;
}

/** 注入微应用引用：先落草稿，再通知已挂载的输入框。 */
export function injectMicroAppRef(input: MicroAppInjectInput): { drafted: boolean } {
  const appId = String(input.appId || "").trim();
  if (!appId) return { drafted: false };
  const label = String(input.label || appId);
  const drafted = patchDraftRefs({ appId, label, version: input.version });
  window.dispatchEvent(new CustomEvent(COMPOSER_MICROAPP_EVENT, {
    detail: { appId, label, version: input.version },
  }));
  return { drafted };
}
