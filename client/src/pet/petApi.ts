/** 宠物窗口与主进程的桥接类型（plan-73-323 / plan-73-326 整改）。
 *
 * preload 暴露的 window.petAPI 是宠物页访问主进程的唯一入口；
 * 此处集中声明类型与访问器，避免各组件各自用 any。
 * 注意：宠物页**不引用主窗的 api/client.ts 与 store/** ——
 * 它是独立入口，保持零耦合才能保证独立 chunk 与独立生命周期。
 */

export interface PetInfo {
  slug: string;
  displayName: string;
  description?: string;
  /** 精灵图 canvas 尺寸（应为 1536×1872 或 1536×2288） */
  width: number;
  height: number;
  /** 网格行数（9 = v1，11 = v2） */
  rows: number;
  alpha: boolean;
  bytes: number;
  /** data URL：canvas 需读像素做命中检测，file:// 跨源会污染画布，故由主进程下发 base64 */
  spriteDataUrl?: string;
}

export interface PetPref {
  enabled: boolean;
  slug: string;
  displayId: number | null;
  /** 距工作区右边缘的间距（edge=left 时忽略）；与 bottomY 共同构成位置锚点 */
  rightGap: number | null;
  bottomY: number | null;
  edge: "left" | "right" | null;
  /** 当前是否显示（手柄一键隐藏 → false，可从设置页恢复） */
  visible: boolean;
  /** 浮窗（任务块）是否折叠——宠物下方的折叠手柄只控制这一个语义 */
  floatCollapsed: boolean;
  /** 浮窗块的显式顺序（会话 id 数组，索引 0 = 最外层/最靠近宠物）。
   *  用户在浮窗里拖拽调序后写入；为空则按自动排序（状态优先级 + 开始时间）。 */
  blockOrder: number[];
  scale: number;
  clickThrough: boolean;
  showCapsule: boolean;
  /** 悬停展开后最多显示的任务块数（未悬停时固定只显示最新一条） */
  maxCapsuleRows: number;
  showBadge: boolean;
  hideOnFullscreen: boolean;
  hideOnMinimize: boolean;
}

export interface InstalledPet {
  slug: string;
  displayName: string;
  rows: number;
  bytes: number;
  broken?: boolean;
}

export interface PetBoot {
  pref: PetPref;
  pet: PetInfo | null;
  installed: InstalledPet[];
  backendPort: number;
  /** 会话标题持久缓存（id → title）：冷启动即可显示真实标题，不依赖标题事件时序 */
  titles: Record<string, string>;
}

export type PetForm = "collapsed" | "capsule" | "expanded";

/** 会话快照（对应后端 SessionOut 的运行态字段子集） */
export interface SessionSnapshot {
  id: number;
  title: string | null;
  project_id: number | null;
  has_running: boolean;
  running_started_at: string | null;
  last_activity_at: string | null;
  goal_text: string | null;
  goal_status: string | null;
  goal_turns_used: number | null;
}

/** 引擎步骤行（对应后端 TaskOut 的子集；仅当任务卡无 AI 清单时按需拉取） */
export interface TaskRow {
  id: number;
  turn_id: number | null;
  parent_task_id: number | null;
  kind: string | null;
  title: string;
  note: string | null;
  status: string;
  agent_id: number | null;
  is_hidden: boolean;
}

export interface PetAssetsPayload {
  pet: PetInfo | null;
  error?: string;
}

export interface PetAPI {
  getBoot(): Promise<PetBoot>;
  /** 写入偏好（与设置页共用主进程同一份真相） */
  setPref(patch: Partial<PetPref>): Promise<PetPref>;
  setInteractive(on: boolean): Promise<boolean>;
  /** 开始拖拽：传「鼠标在窗口客户区内的偏移」（抓取点语义）与启动帧初判的行走方向。
   *  此后主进程自行轮询光标定位窗口**并持续判定行走方向**，渲染层不再上报坐标。 */
  dragBegin(offsetX: number, offsetY: number, initialDir?: "left" | "right" | null): Promise<{ ok: boolean }>;
  /** 开始缩放：传当前 scale，主进程按光标纵向位移 1:1 调整（窗口尺寸不变） */
  scaleBegin(startScale: number): Promise<{ ok: boolean }>;
  /** 交互提前收尾（渲染层监听 pointerup 时调用；主进程也会自行判定按键松开） */
  interactionEnd(): Promise<boolean>;
  /** 光标在窗口客户区内的坐标（DIP）；窗口已销毁返回 null。
   *  用于悬停态兜底：透明穿透窗口快速移出时会丢 mouseleave，需按真实光标位置校正。 */
  cursorClient(): Promise<{ x: number; y: number } | null>;
  focusSession(sessionId: number): Promise<boolean>;
  /** 停止会话运行（块右侧角标） */
  cancelTurn(turnId: number): Promise<{ ok: boolean; error?: string }>;
  hideTemporarily(): Promise<boolean>;
  showPet(): Promise<boolean>;
  openPetPage(slug: string): Promise<boolean>;
  openSettings(): Promise<boolean>;
  onPrefChanged(cb: (pref: PetPref) => void): () => void;
  onAssets(cb: (payload: PetAssetsPayload) => void): () => void;
  /** 主窗焦点态：未聚焦且任务完成时播 waving 提醒 */
  onMainFocus(cb: (focused: boolean) => void): () => void;
  /** 交互结束（主进程判定按键松开后通知）：渲染层据此复位拖拽/缩放的视觉态 */
  onInteractionEnded(cb: (payload: { kind?: string }) => void): () => void;
  /** 拖拽行走方向（主进程按光标水平位移判定，仅方向变化时推送） */
  onDragDir(cb: (dir: "left" | "right" | null) => void): () => void;
  /** 会话快照（主进程代取，规避 file:// 跨源限制） */
  snapshot(): Promise<SessionSnapshot[]>;
  /** 引擎步骤（仅选中无清单的任务时按需拉取一次） */
  tasks(sessionId: number): Promise<TaskRow[]>;
}

declare global {
  interface Window {
    petAPI?: PetAPI;
  }
}

/** 取 petAPI；非宠物窗口环境（如浏览器直接打开）返回 null，调用方需做空值保护 */
export function petApi(): PetAPI | null {
  if (typeof window === "undefined") return null;
  return window.petAPI ?? null;
}
