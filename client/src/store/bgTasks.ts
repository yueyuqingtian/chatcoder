/** 后台任务（右上角工具卡「后台任务」栏的数据；plan-20261020-bgtask）。
 *
 * 数据口径：
 *  · 只列**仍在运行**的后台命令（terminal_exec 以 waitForCompletion=false 启动、或前台命令被转后台）——
 *    退出（自然结束 / 被停止 / 会话删除）即从列表消失，没有「已完成」历史；
 *  · 来源 = REST 快照（切会话 / 重连时拉取）+ WS 事件 `bg.process.started / exited`（增量），
 *    事件按 payload.session_id 归属，不依赖「当前会话」；
 *  · 运行时间由服务端给出「收到时已运行多少秒」，前端用本机时钟累加——不依赖两端系统时间一致。
 */
import { create } from "zustand";
import { api, type BgProcessOut } from "../api/client";

export interface BgTask {
  shellId: string;
  /** 完整命令（界面按首行省略展示，悬停给全文） */
  command: string;
  cwd: string;
  /** 收到数据时已运行的秒数（服务端口径） */
  baseElapsed: number;
  /** 收到数据的本机时刻（Date.now） */
  receivedAt: number;
  /** 正在停止：按钮置灰，避免重复点击 */
  stopping: boolean;
}

interface BgTaskState {
  /** 会话 id → 运行中的后台命令（按启动先后） */
  bySession: Record<number, BgTask[]>;
  /** 拉取某会话的运行中后台命令（切会话 / 重连 / 一轮结束时调用）。 */
  load: (sessionId: number) => Promise<void>;
  /** WS 事件入口：bg.process.started 新增一行，bg.process.exited 撤掉一行。 */
  applyEvent: (event: string, payload: Record<string, unknown>) => void;
  /** 用户点「停止」：成功后立即撤掉这一行（服务端随后会广播 exited，幂等）。 */
  stop: (sessionId: number, shellId: string) => Promise<void>;
}

const taskOf = (row: { shell_id: string; command: string; cwd: string; elapsed: number }, now: number): BgTask => ({
  shellId: row.shell_id,
  command: row.command ?? "",
  cwd: row.cwd ?? "",
  baseElapsed: Number.isFinite(row.elapsed) ? Math.max(0, row.elapsed) : 0,
  receivedAt: now,
  stopping: false,
});

/** 已运行秒数（按本机时钟累加到 nowMs）。 */
export function bgTaskElapsedSec(task: BgTask, nowMs: number): number {
  return Math.max(0, task.baseElapsed + (nowMs - task.receivedAt) / 1000);
}

/** 每个会话只认最新一次快照响应：切会话 / 并发刷新时旧响应作废。 */
const loadSeq = new Map<number, number>();

export const useBgTaskStore = create<BgTaskState>((set, get) => ({
  bySession: {},

  load: async (sessionId) => {
    const mine = (loadSeq.get(sessionId) ?? 0) + 1;
    loadSeq.set(sessionId, mine);
    let rows: BgProcessOut[];
    try {
      rows = (await api.listBgProcesses(sessionId)).items ?? [];
    } catch {
      return; // 旧后端没有这个接口 / 暂时取不到：保持现状，栏目不出现
    }
    if (loadSeq.get(sessionId) !== mine) return;
    const now = Date.now();
    set((s) => {
      const prev = s.bySession[sessionId] ?? [];
      const next = rows.map((r) => {
        const t = taskOf(r, now);
        const old = prev.find((x) => x.shellId === t.shellId);
        return old?.stopping ? { ...t, stopping: true } : t;
      });
      if (next.length === 0 && prev.length === 0) return {};
      return { bySession: { ...s.bySession, [sessionId]: next } };
    });
  },

  applyEvent: (event, payload) => {
    const sid = Number(payload.session_id ?? 0);
    const shellId = typeof payload.shell_id === "string" ? payload.shell_id : "";
    if (!sid || !shellId) return;
    if (event === "bg.process.started") {
      set((s) => {
        const list = s.bySession[sid] ?? [];
        if (list.some((t) => t.shellId === shellId)) return {};
        const row = taskOf({
          shell_id: shellId,
          command: typeof payload.command === "string" ? payload.command : "",
          cwd: typeof payload.cwd === "string" ? payload.cwd : "",
          elapsed: Number(payload.elapsed ?? 0),
        }, Date.now());
        return { bySession: { ...s.bySession, [sid]: [...list, row] } };
      });
    } else if (event === "bg.process.exited") {
      set((s) => {
        const list = s.bySession[sid];
        if (!list || !list.some((t) => t.shellId === shellId)) return {};
        return { bySession: { ...s.bySession, [sid]: list.filter((t) => t.shellId !== shellId) } };
      });
    }
  },

  stop: async (sessionId, shellId) => {
    const mark = (stopping: boolean) => set((s) => {
      const list = s.bySession[sessionId];
      if (!list) return {};
      return { bySession: { ...s.bySession, [sessionId]: list.map((t) => (t.shellId === shellId ? { ...t, stopping } : t)) } };
    });
    mark(true);
    try {
      await api.stopBgProcess(sessionId, shellId);
    } catch {
      // 404 = 已经自己结束了；其它失败也以服务端现状为准重新拉取，不留下假的「运行中」
      mark(false);
      await get().load(sessionId);
      return;
    }
    set((s) => {
      const list = s.bySession[sessionId];
      if (!list) return {};
      return { bySession: { ...s.bySession, [sessionId]: list.filter((t) => t.shellId !== shellId) } };
    });
  },
}));
