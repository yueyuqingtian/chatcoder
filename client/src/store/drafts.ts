/** 输入框草稿持久化（plan-546）：
 * 会话与空态首页的未发送文字、附件及输入框配置按 key（"home" | `s{sessionId}`）隔离保存，
 * 组件重挂载/切换会话后可完整恢复；localStorage 防抖落盘保证应用重启不丢。 */
import { create } from "zustand";
import type { AttachmentInfo } from "../api/client";

/** plan-230-1144 M2: 模式名外置为可配置数据，自定义模式是任意字符串。
 *  plan-75-332: 内置模式改为 3 档（agent 智能体 / plan 计划 / readonly 只读）——“计划执行”
 *  （accept_edits）已取消（它与 agent 能力相同），旧值 default 更名为 agent。
 *  旧 localStorage 里的旧值由 normalizeDraftMode 归一化，避免模式菜单选不中任何一项。 */
export const BUILTIN_DRAFT_MODES = ["agent", "plan", "readonly"] as const;
export type BuiltinDraftMode = (typeof BUILTIN_DRAFT_MODES)[number];
export type ComposerDraftMode = string;

/** plan-75-332: 权限模式（与执行模式正交）——询问审批 / 自动审批 / 完全访问。 */
export const APPROVAL_MODES = ["ask", "auto", "full"] as const;
export type ApprovalMode = (typeof APPROVAL_MODES)[number];

const _MODE_ALIASES: Record<string, ComposerDraftMode> = {
  default: "agent",
  accept_edits: "agent",
};

/** 执行模式归一化（存量草稿兼容）。
 *
 *  plan-75-332 R6 修复：空值必须回落 **agent（智能体模式）**——这是空态首页的默认档。
 *  此前写法 `_MODE_ALIASES[m] ?? m ?? "agent"` 在 m 为空串时返回 ""：`??` 只跳过
 *  null/undefined，空串会原样返回，于是「首次打开首页」与「发送后草稿被清空再回首页」
 *  两种场景下模式标签都会变成空（用户反馈首页默认档显示异常）。
 */
export function normalizeDraftMode(mode: string | null | undefined): ComposerDraftMode {
  const m = (mode || "").trim();
  if (!m) return "agent";
  if ((BUILTIN_DRAFT_MODES as readonly string[]).includes(m)) return m;
  return _MODE_ALIASES[m] ?? m; // 旧值别名映射；自定义模式名原样保留
}

/** 权限模式归一化（存量草稿兼容）。空值/未识别值均按最保守的 ask（询问审批）处理。 */
export function normalizeApprovalMode(mode: string | null | undefined): ApprovalMode {
  const m = (mode || "").trim();
  return ((APPROVAL_MODES as readonly string[]).includes(m) ? m : "ask") as ApprovalMode;
}

/** plan-238-1210 (A2): 引用 chip 的可序列化形态（与 ComposerCore.ComposerRef 同构）。
 *  plan-282-1441（#9）：新增 "mcp"（连接器引用）。 */
export interface ComposerRefDraft {
  kind: "file" | "skill" | "mcp" | "plugin";
  value: string;
  label: string;
}

export interface ComposerDraft {
  /** 未发送文字 */
  text: string;
  /** 已上传附件（含服务端 url/id，可序列化） */
  attachments: AttachmentInfo[];
  /** plan-238-1210 (A2): 引用 chips（@文件 / $技能）——不再内嵌进输入文本，
   *  按会话 key 隔离持久化，切换会话/重启不丢。 */
  refs?: ComposerRefDraft[];
  /** 思考深度（会话与首页均按 key 隔离；null=跟随全局最近值） */
  reasoningEffort: string | null;
  /** 仅 home 草稿使用：首页选中的模型（会话模型走服务端 session.model_id） */
  modelId: number | null;
  /** 仅 home 草稿使用：首页执行模式（会话模式走服务端 session.permission_mode） */
  mode: ComposerDraftMode;
  /** plan-75-332: 仅 home 草稿使用：首页权限模式（会话模式走服务端 session.approval_mode） */
  approvalMode: ApprovalMode;
  /** 仅 home 草稿使用：空态首页工作目录项目 */
  projectId: number | null;
  /** 仅 home 草稿使用：空态首页设定的目标（会话目标走服务端 session.goal_text；plan-676） */
  goalText: string | null;
  updatedAt: number;
}

const STORAGE_KEY = "chatcoder.composer-drafts";
/** localStorage 落盘防抖（ms）：高频输入只触发一次写盘 */
const SAVE_DEBOUNCE_MS = 500;
/** 本地最多保留的草稿条数（超出按 updatedAt 淘汰最旧，防无限膨胀） */
const MAX_DRAFTS = 60;

function loadAll(): Record<string, ComposerDraft> {
  if (typeof window === "undefined") return {};
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === "object") return parsed as Record<string, ComposerDraft>;
  } catch { /* 忽略损坏的本地缓存 */ }
  return {};
}

let _saveTimer: ReturnType<typeof setTimeout> | null = null;
function scheduleSave(drafts: Record<string, ComposerDraft>) {
  if (typeof window === "undefined") return;
  if (_saveTimer) clearTimeout(_saveTimer);
  _saveTimer = setTimeout(() => {
    _saveTimer = null;
    try {
      // 淘汰超期最旧条目
      const entries = Object.entries(drafts);
      if (entries.length > MAX_DRAFTS) {
        entries.sort((a, b) => b[1].updatedAt - a[1].updatedAt);
        drafts = Object.fromEntries(entries.slice(0, MAX_DRAFTS));
      }
      localStorage.setItem(STORAGE_KEY, JSON.stringify(drafts));
    } catch { /* localStorage 配额超限等静默处理 */ }
  }, SAVE_DEBOUNCE_MS);
}

interface DraftsState {
  drafts: Record<string, ComposerDraft>;
  /** 读取指定 key 的草稿（"home" 或 `s${sessionId}`） */
  getDraft: (key: string) => ComposerDraft | null;
  /** 局部更新草稿并触发防抖落盘 */
  patchDraft: (key: string, patch: Partial<Omit<ComposerDraft, "updatedAt">>) => void;
  /** 清空草稿（发送成功后调用） */
  clearDraft: (key: string) => void;
  /** 仅清空文字/附件，保留输入框配置（思考深度/模型/模式等）——发送后深度选择不丢 */
  clearDraftText: (key: string) => void;
}

export const useDraftsStore = create<DraftsState>((set, get) => ({
  drafts: loadAll(),

  getDraft: (key) => get().drafts[key] ?? null,

  patchDraft: (key, patch) => {
    const prev = get().drafts[key];
    const base: ComposerDraft = prev ?? {
      text: "",
      attachments: [],
      refs: [],
      reasoningEffort: null,
      modelId: null,
      mode: "agent",
      approvalMode: "ask",
      projectId: null,
      goalText: null,
      updatedAt: Date.now(),
    };
    const next: ComposerDraft = {
      ...base,
      ...patch,
      updatedAt: Date.now(),
    };
    const all = { ...get().drafts, [key]: next };
    set({ drafts: all });
    scheduleSave(all);
  },

  clearDraft: (key) => {
    const prev = get().drafts;
    if (!(key in prev)) return;
    const all = { ...prev };
    delete all[key];
    set({ drafts: all });
    scheduleSave(all);
  },

  /** v7: 发送成功后仅清文字/附件/引用，保留思考深度等输入框配置（会话内深度跨发送存活） */
  clearDraftText: (key) => {
    const prev = get().drafts[key];
    if (!prev) return;
    get().patchDraft(key, { text: "", attachments: [], refs: [] });
  },
}));
