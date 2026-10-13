/** 工作流状态（设计 §7.9）：会话内程序列表、程序快照与右侧面板当前聚焦的程序。
 *
 * 数据以服务端快照为准：事件只作为“该刷新了”的信号，按程序去抖后重新拉取快照，
 * 因此断线重连、乱序与重复事件都不会让界面偏离数据库。
 */
import { create } from "zustand";
import {
  ApiError,
  api,
  type WorkflowDriftDiffOut,
  type WorkflowDriftOut,
  type WorkflowPoolOut,
  type WorkflowProgramOut,
  type WorkflowRunOut,
  type WorkflowTemplateOut,
} from "../api/client";

export type WorkflowAction = "start" | "pause" | "resume" | "cancel";
/** approve 采纳 / 保留；rework 再试或再返工；abort 中止；include 把越界改动的文件纳入本步骤范围 */
export type GateDecision = "approve" | "rework" | "abort" | "include";

const ACTION_LABEL: Record<WorkflowAction, string> = {
  start: "开始执行", pause: "暂停", resume: "继续", cancel: "取消",
};

interface WorkflowState {
  /** 会话 id → 程序列表（摘要；每次写入快照时同步其中的对应行） */
  bySession: Record<number, WorkflowRunOut[]>;
  /** 程序 id → 程序快照（含子工作流与节点） */
  snapshots: Record<number, WorkflowProgramOut>;
  /** 正在执行动作的程序 id，用于按钮禁用 */
  busy: Record<number, boolean>;
  /** 右侧面板当前聚焦的程序（null = 显示列表） */
  focusId: number | null;
  /** 续跑前的工作区报告：外部改动的文件与 HEAD 变化；等待用户确认（null = 无待确认项） */
  drift: { programId: number; files: string[]; head: WorkflowDriftOut["head"] } | null;
  /** 漂移弹层里正在查看的文件差异 */
  driftDiff: { path: string; data: WorkflowDriftDiffOut | null; loading: boolean } | null;
  /** 续跑 / 重试被模型可达性预检拦下时的提示（按程序记录，下一次成功的操作时清除） */
  notices: Record<number, string>;
  /** 草稿计划换模板后未通过校验的诊断（按程序记录） */
  planErrors: Record<number, Array<{ message: string; suggestion?: string | null }>>;
  /** 开始执行前主工作区有未提交改动：需用户确认（独立工作树看不到这些改动） */
  localChanges: { programId: number; files: string[] } | null;
  error: string | null;
  /** 全局并发池占用（右侧面板与设置页共用） */
  pool: WorkflowPoolOut | null;
  /** 模板清单：null 表示尚未加载 */
  templates: WorkflowTemplateOut[] | null;
  loadPool: () => Promise<void>;
  loadTemplates: () => Promise<void>;
  saveTemplate: (programId: number, name: string, description?: string) => Promise<WorkflowTemplateOut | null>;
  copyPreset: (key: string) => Promise<WorkflowTemplateOut | null>;
  patchTemplate: (key: string, body: { name?: string; description?: string; enabled?: boolean }) => Promise<void>;
  deleteTemplate: (key: string) => Promise<void>;
  focus: (programId: number | null) => void;
  loadSession: (sessionId: number) => Promise<void>;
  loadProgram: (programId: number) => Promise<void>;
  act: (programId: number, action: WorkflowAction, note?: string, acceptDrift?: boolean) => Promise<void>;
  startProgram: (programId: number, acceptLocalChanges?: boolean, sharedWorkspace?: boolean) => Promise<void>;
  /** 主工作区有未提交改动时的确认：仍然开始（独立工作树）或改用共享工作区 */
  confirmLocalChanges: (sharedWorkspace: boolean) => Promise<void>;
  cancelLocalChanges: () => void;
  /** 继续：先预览工作区漂移，有改动则转入确认，无改动直接续跑 */
  requestResume: (programId: number) => Promise<void>;
  confirmDrift: () => Promise<void>;
  cancelDrift: () => void;
  decideGate: (
    programId: number, flowKey: string, nodeKey: string, decision: GateDecision, feedback?: string,
  ) => Promise<void>;
  /** 失败节点的重试：checkpoint 从断点继续，fresh 从头重做该步骤 */
  retryNode: (programId: number, flowKey: string, nodeKey: string, mode: "checkpoint" | "fresh") => Promise<void>;
  /** 草稿计划的“调整”：把要求发给主代理，成功后取消旧草稿（标记为已调整） */
  adjustDraft: (programId: number, text: string) => Promise<boolean>;
  /** 草稿计划中替换某个子工作流的模板（A → B） */
  swapTemplate: (programId: number, flowKey: string, template: string, flow: string) => Promise<void>;
  /** 新建模板（从一个探索步骤起步），返回新模板 */
  createTemplate: (name: string) => Promise<WorkflowTemplateOut | null>;
  /** 从预设骨架开始：生成待确认的工作流并聚焦到它 */
  startFromPreset: (sessionId: number, preset: string) => Promise<number | null>;
  openDriftDiff: (path: string) => Promise<void>;
  closeDriftDiff: () => void;
  applyEvent: (payload: Record<string, unknown>) => void;
  clearError: () => void;
}

const _reloadTimers = new Map<number, number>();

/** 同一程序的连续事件在 250ms 内合并为一次快照刷新 */
function scheduleReload(programId: number, reload: () => void) {
  const prev = _reloadTimers.get(programId);
  if (prev !== undefined) window.clearTimeout(prev);
  _reloadTimers.set(programId, window.setTimeout(() => {
    _reloadTimers.delete(programId);
    reload();
  }, 250));
}

const errorText = (action: string, e: unknown) =>
  `${action}失败：${e instanceof Error ? e.message : String(e)}`;

export const useWorkflowStore = create<WorkflowState>((set, get) => {
  /** 写入快照，并同步会话列表中的摘要行；列表里查不到该程序时补拉一次会话列表 */
  const commit = (snap: WorkflowProgramOut) => {
    const sid = snap.run.session_id;
    const list = get().bySession[sid];
    const listed = !!list && list.some((r) => r.id === snap.run.id);
    const nodes = snap.flows.flatMap((f) => f.nodes);
    const progress = {
      done: nodes.filter((n) => n.status === "done" || n.status === "skipped").length,
      total: nodes.length,
    };
    set((s) => ({
      snapshots: { ...s.snapshots, [snap.run.id]: snap },
      bySession: listed && list
        ? { ...s.bySession, [sid]: list.map((r) => (r.id === snap.run.id ? { ...snap.run, progress } : r)) }
        : s.bySession,
      error: null,
    }));
    if (!listed) void get().loadSession(sid);
  };

  const runAction = async (programId: number, label: string, task: () => Promise<WorkflowProgramOut>) => {
    set((s) => ({ busy: { ...s.busy, [programId]: true } }));
    try {
      commit(await task());
      set((s) => ({ notices: { ...s.notices, [programId]: "" } }));
    } catch (e) {
      const code = e instanceof ApiError ? (e.detail as { code?: string } | undefined)?.code : undefined;
      if (code === "model_unavailable" && e instanceof ApiError) {
        // 模型暂不可用：保持暂停、不消耗重试次数，界面就地提示（§6.9）
        set((s) => ({ notices: { ...s.notices, [programId]: e.message } }));
      } else {
        set({ error: errorText(label, e) });
      }
    } finally {
      set((s) => ({ busy: { ...s.busy, [programId]: false } }));
    }
  };

  return {
    bySession: {},
    snapshots: {},
    busy: {},
    focusId: null,
    drift: null,
    driftDiff: null,
    notices: {},
    planErrors: {},
    localChanges: null,
    pool: null,
    templates: null,
    error: null,

    focus: (programId) => set({ focusId: programId }),

    loadPool: async () => {
      try {
        set({ pool: await api.workflowPool() });
      } catch {
        /* 占用信息是辅助展示，失败不打扰用户 */
      }
    },

    loadTemplates: async () => {
      try {
        set({ templates: await api.listWorkflowTemplates() });
      } catch (e) {
        set({ error: errorText("加载模板", e) });
      }
    },

    saveTemplate: async (programId, name, description = "") => {
      try {
        const saved = await api.saveWorkflowTemplate(programId, name, description);
        await get().loadTemplates();
        return saved;
      } catch (e) {
        set({ error: errorText("保存模板", e) });
        return null;
      }
    },

    copyPreset: async (key) => {
      try {
        const copied = await api.copyWorkflowPreset(key);
        await get().loadTemplates();
        return copied;
      } catch (e) {
        set({ error: errorText("复制模板", e) });
        return null;
      }
    },

    patchTemplate: async (key, body) => {
      try {
        await api.patchWorkflowTemplate(key, body);
        await get().loadTemplates();
      } catch (e) {
        set({ error: errorText("更新模板", e) });
      }
    },

    deleteTemplate: async (key) => {
      try {
        await api.deleteWorkflowTemplate(key);
        await get().loadTemplates();
      } catch (e) {
        set({ error: errorText("删除模板", e) });
      }
    },

    requestResume: async (programId) => {
      try {
        const report = await api.workflowDrift(programId);
        if (report.files.length > 0) {
          set({ drift: { programId, files: report.files, head: report.head } });
          return;
        }
        if (report.head) {
          set((s) => ({
            notices: {
              ...s.notices,
              [programId]: `暂停期间仓库有新的提交（${report.head?.from ?? "?"} → ${report.head?.to ?? "?"}），继续时以当前工作区为准`,
            },
          }));
        }
      } catch (e) {
        set({ error: errorText("继续", e) });
        return;
      }
      await get().act(programId, "resume");
    },

    confirmDrift: async () => {
      const pending = get().drift;
      if (!pending) return;
      set({ drift: null });
      await get().act(pending.programId, "resume", undefined, true);
    },

    cancelDrift: () => set({ drift: null, driftDiff: null }),

    openDriftDiff: async (path) => {
      const pending = get().drift;
      if (!pending) return;
      set({ driftDiff: { path, data: null, loading: true } });
      try {
        const data = await api.workflowDriftDiff(pending.programId, path);
        set({ driftDiff: { path, data, loading: false } });
      } catch (e) {
        set({ driftDiff: null, error: errorText("查看差异", e) });
      }
    },

    closeDriftDiff: () => set({ driftDiff: null }),

    adjustDraft: async (programId, text) => {
      const trimmed = text.trim();
      if (!trimmed) return false;
      // 与聊天发送共用同一条路径（排队、乐观消息、运行态）；惰性加载避免与聊天模块循环依赖
      const { useChatStore } = await import("./chat");
      const chat = useChatStore.getState();
      const wasBusy = chat.isRunning;
      const turnId = await chat.sendTurn(`请调整刚才的工作流计划：${trimmed}`);
      // 空闲时发送失败（返回 null）：草稿保留，由聊天里的错误卡说明；忙碌时消息已排队
      if (turnId == null && !wasBusy) return false;
      await runAction(programId, "调整计划", () => api.cancelWorkflowRun(programId, "adjusted"));
      return true;
    },

    swapTemplate: async (programId, flowKey, template, flow) => {
      set((s) => ({ planErrors: { ...s.planErrors, [programId]: [] } }));
      try {
        commit(await api.swapWorkflowFlowTemplate(programId, flowKey, template, flow));
      } catch (e) {
        const detail = e instanceof ApiError
          ? (e.detail as { code?: string; errors?: Array<{ message: string; suggestion?: string | null }> } | undefined)
          : undefined;
        if (detail?.code === "plan_invalid") {
          set((s) => ({ planErrors: { ...s.planErrors, [programId]: detail.errors ?? [] } }));
        } else {
          set({ error: errorText("换模板", e) });
        }
      }
    },

    startFromPreset: async (sessionId, preset) => {
      try {
        const snap = await api.startWorkflowFromPreset(sessionId, preset);
        commit(snap);
        set({ focusId: snap.run.id });
        void get().loadSession(sessionId);
        return snap.run.id;
      } catch (e) {
        set({ error: errorText("从预设开始", e) });
        return null;
      }
    },

    createTemplate: async (name) => {
      try {
        const created = await api.createWorkflowTemplate(name);
        await get().loadTemplates();
        return created;
      } catch (e) {
        set({ error: errorText("新建模板", e) });
        return null;
      }
    },

    startProgram: async (programId, acceptLocalChanges = false, sharedWorkspace = false) => {
      set((s) => ({ busy: { ...s.busy, [programId]: true } }));
      try {
        commit(
          await api.workflowAction(programId, "start", undefined, false, acceptLocalChanges, sharedWorkspace),
        );
      } catch (e) {
        // 结构化的 409：列出改动文件，由用户确认后再开始
        const detail = e instanceof ApiError ? (e.detail as { code?: string; files?: string[] } | undefined) : undefined;
        if (detail?.code === "local_changes") {
          set({ localChanges: { programId, files: detail.files ?? [] } });
        } else {
          set({ error: errorText("开始执行", e) });
        }
      } finally {
        set((s) => ({ busy: { ...s.busy, [programId]: false } }));
      }
    },

    confirmLocalChanges: async (sharedWorkspace) => {
      const pending = get().localChanges;
      if (!pending) return;
      set({ localChanges: null });
      await get().startProgram(pending.programId, true, sharedWorkspace);
    },

    cancelLocalChanges: () => set({ localChanges: null }),

    loadSession: async (sessionId) => {
      try {
        const list = await api.listWorkflows(sessionId);
        void get().loadPool();
        set((s) => ({ bySession: { ...s.bySession, [sessionId]: list } }));
      } catch (e) {
        set({ error: errorText("加载工作流", e) });
      }
    },

    loadProgram: async (programId) => {
      try {
        commit(await api.getWorkflow(programId));
      } catch (e) {
        set({ error: errorText("加载工作流", e) });
      }
    },

    act: (programId, action, note, acceptDrift) =>
      action === "start"
        ? get().startProgram(programId)
        : runAction(programId, ACTION_LABEL[action], () => api.workflowAction(programId, action, note, acceptDrift)),

    retryNode: (programId, flowKey, nodeKey, mode) =>
      runAction(programId, mode === "fresh" ? "从头重做" : "从断点继续", () =>
        api.retryWorkflowNode(programId, flowKey, nodeKey, mode),
      ),

    decideGate: (programId, flowKey, nodeKey, decision, feedback) =>
      runAction(programId, "提交决定", () =>
        api.decideWorkflowGate(programId, flowKey, nodeKey, decision, feedback ?? ""),
      ),

    applyEvent: (payload) => {
      const runId = Number(payload.run_id ?? 0);
      if (!runId) return;
      scheduleReload(runId, () => {
        void get().loadProgram(runId);
        void get().loadPool();
      });
    },

    clearError: () => set({ error: null }),
  };
});
