// 毛玻璃诊断的渲染层入口（plan-308-1555 M0）
//
// 设计取舍（重要）：**实际采样逻辑不在这里**。
//   历史教训是「改了代码 → 看不到效果 → 再改别的」，其中一类陷阱正是
//   "运行的仍是旧前端构建产物"。因此真正的逐层 alpha 采样被实现为
//   **主进程注入的自包含脚本**（`electron/glass-diagnostics.cjs` 的 `probeScript()`），
//   与前端构建产物解耦——即使前端是旧构建，诊断依然给出正确结论。
//
// 本文件只做两件事：
//   1) 类型定义与薄封装（前端组件统一从这里取诊断结果）；
//   2) 把诊断结论映射为可展示的文案，供设置页「玻璃诊断」区渲染。

/** 单层采样结果 */
export interface GlassLayerSample {
  selector: string;
  backgroundColor: string;
  backdropFilter: string;
  backgroundImage: string;
  /** 决策 B：属"按设计保持不透明"的层（消息流/主内容区），不参与 blocked 判定 */
  isExpectedOpaque: boolean;
}

/** 渲染层探针结果 */
export interface GlassProbeResult {
  ok: boolean;
  /** 'ready' 或 'blocked@<selector>' */
  alphaPath: string;
  blockedSelector: string | null;
  opaqueUnexpected: string[];
  samples: GlassLayerSample[];
}

/** DWM 回读结果（系统侧**实际**生效的 backdrop） */
export interface GlassDwmReadBack {
  ok: boolean;
  value: number | null;
  name: string;
  hresult?: number;
  reason?: string;
}

/** 完整诊断结果（主进程合成） */
export interface GlassDiagnostics {
  ok: boolean;
  env: {
    platform: string;
    release: string;
    windows: { build: number; isWin11: boolean; supportsBackdrop: boolean } | null;
    electron: string;
    chrome: string;
    node: string;
    modules: string;
    isPackaged: boolean;
  };
  window: Record<string, unknown>;
  dwm: GlassDwmReadBack;
  probe: GlassProbeResult | null;
  backend: string;
  backendReason: string;
  conclusion: { verdict: GlassVerdict; reason: string; line: string };
  /** 可直接复制的一行摘要 */
  line: string;
}

/** 结论分类：决定"下一步该修什么" */
export type GlassVerdict =
  | "material-live"   // 材质已生效（若看不见属对比度问题）
  | "material-dead"   // 材质未生效（需走 FFI 兼容路径）
  | "blocked"         // 材质生效但被不透明层遮挡
  | "unknown";        // 无法回读（FFI 不可用）

export const GLASS_VERDICT_LABEL: Record<GlassVerdict, string> = {
  "material-live": "材质已生效",
  "material-dead": "材质未生效",
  blocked: "被不透明层遮挡",
  unknown: "无法判定",
};

export const GLASS_VERDICT_TONE: Record<GlassVerdict, "ok" | "warn" | "bad"> = {
  "material-live": "ok",
  "material-dead": "bad",
  blocked: "warn",
  unknown: "warn",
};

/** 毛玻璃诊断 IPC 的返回类型（主进程合成的完整诊断结果） */
export type GlassDiagnosticsIpc = Record<string, unknown>;

/**
 * 取一次完整诊断（主进程回读 DWM + 注入探针采样渲染层）。
 * Web 模式（无 chatcoderAPI）返回 null，调用方据此隐藏诊断区。
 */
export async function fetchGlassDiagnostics(): Promise<GlassDiagnostics | null> {
  const api = typeof window !== "undefined" ? window.chatcoderAPI : undefined;
  if (!api?.glassDiagnostics) return null;
  try {
    const raw = (await api.glassDiagnostics()) as GlassDiagnosticsIpc;
    // 主进程与渲染层的字段是约定好的（见 electron/glass-diagnostics.cjs 的 buildConclusion），
    // 这里经 unknown 中转做结构断言——IPC 无编译期类型保障，属必要的一处收敛点。
    return raw as unknown as GlassDiagnostics;
  } catch {
    return null;
  }
}

/** 切换玻璃自检模式（临时调淡面板，肉眼判定桌面是否混入）。 */
export async function setGlassSelfCheck(on: boolean): Promise<boolean> {
  const api = typeof window !== "undefined" ? window.chatcoderAPI : undefined;
  if (!api?.setGlassSelfCheck) return false;
  try {
    const res = await api.setGlassSelfCheck(on);
    return Boolean(res?.ok);
  } catch {
    return false;
  }
}
