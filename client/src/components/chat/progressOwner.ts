/** 任务进度行 · 子会话归属与运行态修正（plan-20261020-progress）。
 *
 * 单独成文件的原因有两个：一是它是**纯函数**，不该被 React store 依赖牵连；
 * 二是 `client/tests` 用 Node 原生 TS 直接运行，只能加载不依赖 react / store 的模块。
 * 对外仍由 `taskProgress.ts` 导出（胶囊 / 右面板 / 工具卡统一从这里取口径）。
 */
import type { ProgressRow } from "./taskProgress";

/** 子会话运行态快照。 */
export interface SubsessionRunState {
  id: number;
  running: boolean;
}

/** 标题里的归属线索：模型常把子会话 id 写进标题（「已派发 #465」）却没填 owner。 */
const OWNER_HINT_RE = /#(\d+)/g;

/** 从标题里挑出**真实存在**的子会话 id。
 *  只认传入的 subs：标题里的 `#123` 也可能是步骤 / 消息编号，认错会把无关步骤标成在跑。 */
function inferOwner(title: string, known: Map<number, boolean>): number | null {
  for (const match of title.matchAll(OWNER_HINT_RE)) {
    const id = Number(match[1]);
    if (known.has(id)) return id;
  }
  return null;
}

/** 按子会话运行态修正进度行。
 *
 * 背景：模型并行派发多个子会话时通常只把一项标 `in_progress`，`owner` 也常漏填
 * （只在标题里写了「已派发 #465」），于是子会话都在跑、任务卡却只有一项转圈。
 * 这里以**子会话自己的运行态**为准：在跑的置为 running，已结束的沿用清单状态（不擅自改），
 * 已完成的步骤永不翻回进行中。推断出的 owner 写回行上，子会话 chip 与工具卡「当前动作」
 * 的归属判断才能拿到它。
 *
 * 必须在 `withRunningFallback` **之前**应用：否则兜底会先把下一项待办标成 running，
 * 之后按子会话状态就再也区分不出"是不是真的只剩一项在跑"。
 */
export function resolveSubsessionRows(rows: ProgressRow[], subs: SubsessionRunState[]): ProgressRow[] {
  if (subs.length === 0) return rows;
  const runById = new Map<number, boolean>();
  for (const s of subs) runById.set(s.id, Boolean(s.running));
  return rows.map((r) => {
    const owner = r.owner ?? inferOwner(r.title, runById);
    if (owner == null) return r;
    const next: ProgressRow = owner === r.owner ? r : { ...r, owner };
    if (r.status === "done") return next;
    if (runById.get(owner) !== true) return next;
    return { ...next, status: "running" };
  });
}
