/** 微应用宿主桥 · 渲染层中枢（plan-693-2868 · docs/app1/11-host-bridge-protocol.md §4-§7）。
 *
 *  这条链路一共四跳，本模块只负责**中间那一跳**（渲染层）：
 *
 *    页面 window.microapp.invoke
 *      → preload（sendToHost "microapp:invoke"） → 主进程
 *      → **本模块**（IPC "microapp:hostRequest" 收上⬆行 / WS microapp.host.request 发后端）
 *      → server/app/orchestration/microapp_host.py
 *      ← 后端回 microapp.host.response / microapp.host.push（全局通道）
 *      ← **本模块**（IPC "microapp:hostReply" 回执 / "microapp:hostPush" 下行） → 主进程 → 页面
 *
 *  三条必须守住的约定：
 *  ① **单例 + 可重入**：IPC 与 WS 的订阅都只在本模块里挂一次。面板会随标签开关反复挂载，
 *     若每个 MicroAppPanel 各自订阅，同一请求会被转发 N 次（页面收到 N 份回执、命令被执行 N 遍）。
 *     所以这里用引用计数：`start()` 返回停止函数，只有计数归零才真正摘监听。
 *  ② **sessionId 取标签所属会话桶**（不是当前活动会话）：后端拿它定位工作区根目录。
 *     面板挂载时把自己的 `uid → sessionId` 交给 `bind()`，上行请求按 uid 查这张表。
 *     取当前活动会话会让后台会话里的微应用把命令跑进用户正在看的项目里（协议 §7）。
 *  ③ **WS 未连接不静默丢弃**：给页面一条明确的 `microapp-host-offline`，而不是让它干等 30 秒宿主超时。
 *
 *  故意不做的事：
 *  - 不发通知、不弹审批：页面起的命令后端已 mark_observed，不会唤醒 AI 会话（契约 §4 执行语义）；
 *  - 不校验 / 不改写 streamId（后端就是后台命令注册表的 `bg_…` id，与 terminal_bg_* 同一套）；
 *  - 不拆 stdout/stderr 双栏：后端把两路合并成一路，`stream` 字段恒为 "stdout"（契约 §5）；
 *  - 不假装支持 `exec.start` 的 `env`：后端在结果里回 `warnings`，原样透传给页面就够了。
 */
import { globalWsClient } from "../api/ws";

// ── WS 事件名（与 server/app/orchestration/microapp_host.py 的 EVENT_* 逐字一致）──
export const EV_HOST_REQUEST = "microapp.host.request";
export const EV_HOST_RESPONSE = "microapp.host.response";
export const EV_HOST_PUSH = "microapp.host.push";

// ── 渲染层自己产生的失败原因（契约 §3：页面侧 Error.message 以 microapp-host-<reason> 开头）──
/** 全局 WS 未连接：请求根本没发出去 */
export const ERR_OFFLINE = "microapp-host-offline";
/** 找不到发起方 uid 所属的会话桶（界面已关、或挂在无会话的首页面板上） */
export const ERR_UNKNOWN_INSTANCE = "microapp-host-unknown-instance";

/** 主进程发来的上行请求（electron/browser-agent.cjs 的 `mw.webContents.send(MICROAPP_REQ_CHANNEL, …)`）。 */
export interface HostRequestFromMain {
  uid?: string;
  appId?: string;
  version?: string;
  wcId?: number;
  reqId?: string;
  op?: string;
  args?: Record<string, unknown>;
}

/** 一个微应用界面的身份绑定：uid 是标签的唯一标识（`store/panel.ts` 的 `microapp#3`）。 */
export interface MicroappHostInstance {
  uid: string;
  /** 标签所属会话（`bucketSessionId(桶key)`）；首页面板没有会话时为 null */
  sessionId: number | null;
  appId?: string;
  version?: string;
}

/** 回执给主进程的形状（主进程按 `uid|reqId` 找到发起方 guest，再投给页面的 Promise）。 */
export interface MicroappHostReply {
  reqId: string;
  uid?: string;
  ok: boolean;
  result?: unknown;
  /** 失败原因全串（`microapp-host-<reason>`）；preload 会以它开头拼 Error.message */
  reason?: string;
  message?: string;
}

/** 下行推送给主进程的形状：内层 `event`（stream.log / stream.state）提到外层，其余原样交给主进程聚批。 */
export interface MicroappHostPushMessage {
  uid: string;
  wcId?: number;
  event: string;
  payload?: Record<string, unknown>;
}

/** 主进程侧的三个频道（与 electron/preload.cjs 的 onMicroappHostRequest / microappHostReply / microappHostPush 对应）。 */
export interface MicroappHostIpc {
  onMicroappHostRequest: (cb: (payload: HostRequestFromMain) => void) => () => void;
  microappHostReply: (payload: MicroappHostReply) => void;
  microappHostPush: (payload: MicroappHostPushMessage) => Promise<{ ok: boolean; result?: unknown; error?: string }>;
}

/** 只用得到 on / send 两个能力，便于单测注入假通道（不引真 WebSocket）。 */
export interface MicroappHostWs {
  on(cb: (ev: { event: string; payload: Record<string, unknown> }) => void): () => void;
  send(event: string, payload: Record<string, unknown>): boolean;
}

export interface MicroappHostDeps {
  ws: MicroappHostWs;
  /** 主进程 IPC；非桌面端返回 undefined（此时不订阅 IPC，收到请求一律 offline） */
  ipc: () => MicroappHostIpc | undefined;
}

/** 排障用：下行推送投递失败的次数与最后一条原因（界面已关时属正常，不弹任何提示）。 */
export interface MicroappHostDiagnostics {
  /** 已登记身份绑定的微应用数量 */
  instances: number;
  /** 转发给后端的上行请求数 / 被本地拒绝的数量 */
  forwarded: number;
  rejected: number;
  pushFailures: number;
  lastPushError: string;
}

export interface MicroappHostBridge {
  /** 幂等启动（引用计数）；返回的停止函数反复调用只生效一次。 */
  start: () => () => void;
  /** 登记 / 更新一个界面的身份绑定；返回解绑函数（只解绑自己那一次）。 */
  bind: (instance: MicroappHostInstance) => () => void;
  /** 按所属会话定位窗口实例，不能借用其它会话同名应用。 */
  instanceFor: (sessionId: number | null, appId: string, version: string) => MicroappHostInstance | undefined;
  /** 处理一条主进程转来的上行请求（导出的测试与诊断入口）。 */
  forward: (raw: HostRequestFromMain) => void;
  diagnostics: () => MicroappHostDiagnostics;
}

function text(v: unknown): string {
  return typeof v === "string" ? v.trim() : (typeof v === "number" ? String(v) : "");
}

/** 服务端 message 形如 `"<code>: <细节>"`，而页面 preload 会再拼一次 `code` 当前缀。
 *  这里去掉重复的那一段，免得页面里看到 `microapp-host-exec-failed: microapp-host-exec-failed: …`。 */
function trimCode(message: string, code: string): string {
  if (!message || !code) return message;
  if (message === code) return "";
  const head = code + ":";
  return message.startsWith(head) ? message.slice(head.length).trim() : message;
}

/** 建一台桥。单例见文件末尾的 `microappHost`；单测用 `createMicroappHostBridge` 注入假通道。 */
export function createMicroappHostBridge(deps: MicroappHostDeps): MicroappHostBridge {
  /** uid → 身份绑定（面板挂载时写入，卸载时摘掉） */
  const instances = new Map<string, MicroappHostInstance>();
  let refs = 0;
  let offWs: (() => void) | null = null;
  let offIpc: (() => void) | null = null;
  const stats: MicroappHostDiagnostics = {
    instances: 0, forwarded: 0, rejected: 0, pushFailures: 0, lastPushError: "",
  };

  /** 回执给主进程：主进程按 `uid|reqId` 找到发起方 guest，再投给页面的 Promise。 */
  function reply(payload: MicroappHostReply): void {
    try {
      deps.ipc()?.microappHostReply(payload);
    } catch {
      /* 主进程不可用：回执丢了就丢了，页面侧 preload 30 秒后自己超时（协议 §4 的双向兜底） */
    }
  }

  function reject(reqId: string, uid: string, reason: string, message: string): void {
    stats.rejected += 1;
    reply({ reqId, uid, ok: false, reason, message: `${reason}: ${message}` });
  }

  /** 下行：后端 / 主进程 → 目标微应用界面（协议 §5）。 */
  function pushDownstream(message: MicroappHostPushMessage): void {
    const ipc = deps.ipc();
    if (!ipc?.microappHostPush) return;
    try {
      void Promise.resolve(ipc.microappHostPush(message)).then(
        (res) => {
          // 界面已关 / 还在加载时主进程回 ok:false。流式日志丢了就丢了：
          // 页面自己会按 stream.state 收尾，用户不需要被打扰（协议 §2 不弹卡）。
          if (res && res.ok === false) {
            stats.pushFailures += 1;
            stats.lastPushError = String(res.error || "");
          }
        },
        () => { /* invoke 失败同样忽略 */ },
      );
    } catch { /* 主进程不可用 */ }
  }

  function onWsEvent(ev: { event: string; payload: Record<string, unknown> }): void {
    const p = ev.payload || {};
    if (ev.event === EV_HOST_RESPONSE) {
      const reqId = text(p.reqId);
      if (!reqId) return; // 没有 reqId 无法配对，主进程也没有等待者
      const uid = text(p.uid);
      if (p.ok === true) {
        reply({ reqId, uid, ok: true, result: p.result === undefined ? null : p.result });
        return;
      }
      const reason = text(p.reason) || "microapp-host-failed";
      reply({ reqId, uid, ok: false, reason, message: trimCode(text(p.message), reason) });
      return;
    }
    if (ev.event === EV_HOST_PUSH) {
      // 服务端形状：{ uid, streamId, seq, event: "stream.log" | "stream.state", lines/state/code }
      // 主进程形状：{ uid, event, payload }——内层 event 提到外层，其余原样交给主进程聚批 / 投递。
      const uid = text(p.uid);
      const inner = text(p.event);
      if (!uid || !inner) return;
      const { uid: _uid, event: _event, ...rest } = p;
      void _uid;
      void _event;
      pushDownstream({ uid, event: inner, payload: rest });
    }
  }

  /** 上行：主进程 → 后端（协议 §4 / §7）。 */
  function forward(raw: HostRequestFromMain): void {
    const reqId = text(raw.reqId);
    const op = text(raw.op);
    const uid = text(raw.uid);
    if (!reqId || !op) return; // 主进程已校验过 reqId / op 形状，这里只挡残报
    if (!uid) {
      reject(reqId, "", ERR_UNKNOWN_INSTANCE, "上行请求没有带 uid，无法定位发起方界面（协议 §6）");
      return;
    }
    const bound = instances.get(uid);
    if (!bound) {
      reject(reqId, uid, ERR_UNKNOWN_INSTANCE, `找不到 uid=${uid} 对应的微应用标签：界面可能已关闭`);
      return;
    }
    if (bound.sessionId == null) {
      // 首页（无会话）面板里的微应用没有工作区可定位；后端会把 sessionId<=0 当成 bad-request，
      // 这里提前给出可读原因，省一跳往返。
      reject(reqId, uid, ERR_UNKNOWN_INSTANCE, "微应用标签挂在无会话的首页面板上，没有可用的工作区");
      return;
    }
    const sent = deps.ws.send(EV_HOST_REQUEST, {
      reqId,
      uid,
      appId: text(raw.appId) || bound.appId || "",
      version: text(raw.version) || bound.version || "",
      // 关键：该标签**所属会话桶**，不是当前活动会话（协议 §7）
      sessionId: bound.sessionId,
      op,
      args: raw.args && typeof raw.args === "object" ? raw.args : {},
    });
    if (!sent) {
      // 全局 WS 断开：直接回绝，让页面立刻拿到原因（而不是等宿主 30 秒超时）
      reject(reqId, uid, ERR_OFFLINE, "与后端的连接已断开，请等连接恢复后重试");
      return;
    }
    stats.forwarded += 1;
  }

  function attach(): void {
    if (!offWs) offWs = deps.ws.on(onWsEvent);
    if (!offIpc) {
      const ipc = deps.ipc();
      offIpc = ipc?.onMicroappHostRequest
        ? ipc.onMicroappHostRequest((payload) => forward(payload as HostRequestFromMain))
        : null;
    }
  }

  function detach(): void {
    if (offWs) { try { offWs(); } catch { /* 忽略 */ } offWs = null; }
    if (offIpc) { try { offIpc(); } catch { /* 忽略 */ } offIpc = null; }
  }

  return {
    start: () => {
      refs += 1;
      attach(); // 幂等：监听已在就不再挂（组件重挂载不会叠加）
      let stopped = false;
      return () => {
        if (stopped) return;
        stopped = true;
        refs = Math.max(0, refs - 1);
        if (refs === 0) detach();
      };
    },
    bind: (instance) => {
      const uid = text(instance.uid);
      if (!uid) return () => {};
      const record: MicroappHostInstance = {
        uid,
        sessionId: instance.sessionId == null ? null : Number(instance.sessionId),
        appId: instance.appId,
        version: instance.version,
      };
      instances.set(uid, record);
      stats.instances = instances.size;
      return () => {
        // 只摘自己那一次：React 严格模式下 effect 会「挂载 → 清理 → 再挂载」，
        // 用记录同一性判断，避免后一次绑定的表项被前一次的清理删掉。
        if (instances.get(uid) === record) {
          instances.delete(uid);
          stats.instances = instances.size;
        }
      };
    },
    instanceFor: (sessionId, appId, version) => {
      const instance = Array.from(instances.values()).find((item) =>
        item.sessionId === sessionId && item.appId === appId && item.version === version);
      return instance ? { ...instance } : undefined;
    },
    forward,
    diagnostics: () => ({ ...stats }),
  };
}

/** 桌面端三个 IPC 频道（electron/preload.cjs:63-69）；网页版没有主进程 → undefined。 */
function desktopIpc(): MicroappHostIpc | undefined {
  if (typeof window === "undefined") return undefined;
  const api = window.chatcoderAPI;
  if (!api?.microappHostReply || !api?.microappHostPush) return undefined;
  return {
    onMicroappHostRequest: api.onMicroappHostRequest ?? (() => () => {}),
    microappHostReply: (payload) => api.microappHostReply!(payload),
    microappHostPush: (payload) => api.microappHostPush!(payload),
  };
}

/** 单例：整个渲染层只有这一台桥（IPC / WS 监听都挂在它身上）。 */
export const microappHost = createMicroappHostBridge({
  ws: globalWsClient,
  ipc: desktopIpc,
});
