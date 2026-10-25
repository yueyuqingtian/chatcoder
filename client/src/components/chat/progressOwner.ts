/** 任务进度行 · 执行者归属与运行态修正（plan-20261020-progress · plan-549-2281）。
 *
 * 单独成文件的原因有两个：一是它是**纯函数**，不该被 React store 依赖牵连；
 * 二是 `client/tests` 用 Node 原生 TS 直接运行，只能加载不依赖 react / store 的模块。
 * 对外仍由 `taskProgress.ts` 导出（胶囊 / 右面板 / 工具卡统一从这里取口径）。
 *
 * ── owner 语义（plan-549-2281）──
 * 并行执行已统一到子代理，`owner` 从此表示**子代理 id**；升级前落库的步骤带的是子会话 id，
 * 前端无法凭数字本身区分两者，因此运行态快照把两种来源都喂进来（子代理优先），
 * 由「谁在这个 id 上真的在跑」反推归属类型——历史子会话步骤照常显示，不需要迁移数据。
 */
import type { ProgressRow } from "./taskProgress";

/** 执行者运行态快照：id 既可能是子代理 id（新数据），也可能是子会话 id（升级前的历史数据）。 */
export interface OwnerRunState {
  id: number;
  running: boolean;
  /** 归属类型。缺省时按「先子代理、后子会话」的兜底顺序判定（见 taskProgress 的快照组装）。 */
  kind?: "subagent" | "subsession";
}

/** 历史命名，保留导出以免 `client/tests` 与旧调用点断裂。 */
export type SubsessionRunState = OwnerRunState;

/** 标题里的归属线索：模型常把执行者 id 写进标题（「已派发 #465」）却没填 owner。 */
const OWNER_HINT_RE = /#(\d+)/g;

/** 从标题里挑出**真实存在**的执行者 id。
 *  只认传入的 owners：标题里的 `#123` 也可能是步骤 / 消息编号，认错会把无关步骤标成在跑。 */
function inferOwner(title: string, known: Map<number, boolean>): number | null {
  for (const match of title.matchAll(OWNER_HINT_RE)) {
    const id = Number(match[1]);
    if (known.has(id)) return id;
  }
  return null;
}

/** 按执行者运行态修正进度行。
 *
 * 背景：模型并行派发多个执行者时通常只把一项标 `in_progress`，`owner` 也常漏填
 * （只在标题里写了「已派发 #465」），于是都在跑、任务卡却只有一项转圈。
 * 这里以**执行者自己的运行态**为准：在跑的置为 running，已结束的沿用清单状态（不擅自改），
 * 已完成的步骤永不翻回进行中。推断出的 owner 写回行上，chip 与工具卡「当前动作」
 * 的归属判断才能拿到它。
 *
 * 必须在 `withRunningFallback` **之前**应用：否则兜底会先把下一项待办标成 running，
 * 之后按执行者状态就再也区分不出"是不是真的只剩一项在跑"。
 */
export function resolveOwnerRows(rows: ProgressRow[], owners: OwnerRunState[]): ProgressRow[] {
  if (owners.length === 0) return rows;
  const runById = new Map<number, boolean>();
  const kindById = new Map<number, "subagent" | "subsession">();
  for (const o of owners) {
    runById.set(o.id, Boolean(o.running));
    if (o.kind) kindById.set(o.id, o.kind);
  }
  return rows.map((r) => {
    const owner = r.owner ?? inferOwner(r.title, runById);
    if (owner == null) return r;
    const ownerKind = kindById.get(owner) ?? r.ownerKind ?? null;
    const changed = owner !== r.owner || ownerKind !== (r.ownerKind ?? null);
    const next: ProgressRow = changed ? { ...r, owner, ownerKind } : r;
    if (r.status === "done") return next;
    if (runById.get(owner) !== true) return next;
    return { ...next, status: "running" };
  });
}

/** 兼容旧名（plan-549-2281 起 owner 语义由子会话改为子代理，函数体不变）。 */
export const resolveSubsessionRows = resolveOwnerRows;
