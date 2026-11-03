/** R4：任务完成系统通知（仅桌面版）。
 *
 * 三档来自「设置 → 常规」（global settings 的 task_notify_mode，缓存在 ui store）：
 *   always     始终弹；
 *   unfocused  仅窗口未聚焦时弹（默认）——正在看着窗口就不打扰；
 *   off        关闭。
 * 窗口聚焦与否只有主进程能判定（webContents.isFocused），因此这里只负责取档位、
 * 生成文案、去重，然后把 mode 一并交给主进程决定是否真的弹。
 * 非桌面版（网页）没有 chatcoderAPI.notifyTaskFinished，直接静默跳过。
 */
import { useUiStore } from "../store/ui";
import { translate } from "../store/i18n";

/** 已弹过通知的 turn（`sessionId:turnId`）。
 *  断线重连补发 / 迟到事件会让同一个 turn.completed 再来一次，不去重会重复弹窗。 */
const notifiedTurns = new Set<string>();
const MAX_TRACKED_TURNS = 200;

function markNotified(key: string): boolean {
  if (notifiedTurns.has(key)) return false;
  notifiedTurns.add(key);
  if (notifiedTurns.size > MAX_TRACKED_TURNS) {
    const oldest = notifiedTurns.values().next().value;
    if (oldest !== undefined) notifiedTurns.delete(oldest);
  }
  return true;
}

/** 去掉正文里的换行/超长内容——系统通知只显示一行摘要，过长会被系统截断。 */
function brief(text: string | undefined | null, max = 120): string {
  const raw = String(text ?? "").replace(/\s+/g, " ").trim();
  return raw.length > max ? `${raw.slice(0, max)}…` : raw;
}

/** 任务结束（完成 / 失败）时调用；内部按设置档位与去重规则决定是否弹系统通知。 */
export function notifyTaskFinished(opts: {
  turnId: number;
  sessionId?: number | null;
  ok: boolean;
  /** 会话标题（通知标题优先用它，用户一眼看到是哪个任务） */
  sessionTitle?: string | null;
  /** 附加摘要（完成时的 summary / 失败原因） */
  detail?: string | null;
}): void {
  if (typeof window === "undefined") return;
  const api = window.chatcoderAPI;
  if (typeof api?.notifyTaskFinished !== "function") return; // 网页版无系统通知能力
  const ui = useUiStore.getState();
  const mode = ui.taskNotifyMode ?? "unfocused";
  if (mode === "off") return;
  if (!Number.isFinite(opts.turnId)) return;
  if (!markNotified(`${opts.sessionId ?? 0}:${opts.turnId}`)) return;

  const language = ui.language;
  const statusText = translate(opts.ok ? "gp.task_notify_done" : "gp.task_notify_failed", language);
  const title = brief(opts.sessionTitle, 60) || translate("gp.task_notify", language);
  const extra = brief(opts.detail);
  const body = extra ? `${statusText}：${extra}` : statusText;
  try {
    void api.notifyTaskFinished({
      mode,
      title,
      body,
      sessionId: opts.sessionId ?? null,
    }).catch(() => { /* 通知失败不影响主流程 */ });
  } catch {
    /* 非桌面端或 IPC 异常：通知是增益功能，静默忽略 */
  }
}
