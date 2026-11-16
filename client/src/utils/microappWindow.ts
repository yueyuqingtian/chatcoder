/** 在**独立窗口**里打开一个微应用（拓展页 / 已安装列表 / 右侧面板「在新窗口打开」）。
 *
 *  为什么必须由主窗口渲染层来做这件事（而不是让新窗口自己申请身份）：
 *  宿主桥的上行链路是「微应用页面 → 主进程 → **渲染层** → 后端」，而「uid → 属于哪个会话」
 *  这张表在渲染层的 `microappHost` 单例里。会话上下文（工作区根目录）就是靠它传给后端的。
 *  若新窗口自建一份绑定与 WS 连接：① 多一条 WS、② 同一条回包会被两个窗口各答一次。
 *  所以这里在主窗口渲染层给**窗口实例**登记绑定（uid = `<标签uid>-w`，与面板标签互不干扰），
 *  新窗口只负责承载界面。
 *
 *  两条生命周期约定：
 *  ① 绑定与 `microappHost.start()` 的引用计数必须活到窗口关闭——面板标签被用户关掉时
 *     窗口里的微应用仍要能被 AI 调用（这正是「独立窗口」的意义）；因此由本模块持有，
 *     并在收到 `microapp:windowClosed` 时释放。
 *  ② 同一 appId 已有窗口时主进程只聚焦（`reused`）：此时本次调用**不**再持有绑定，
 *     立刻释放自己那份，避免重复计数。
 */
import { microappHost } from "../browserAgent/microappHost";
import { notify } from "../store/toast";

export interface MicroAppWindowTarget {
  appId: string;
  version?: string;
  entry?: string;
  /** 面板标签的 uid：窗口实例派生为 `<uid>-w`，同 appId 的面板标签与窗口因此是两个实例 */
  uid?: string;
  /** 标签**所属会话**（`bucketSessionId(桶key)`）：宿主桥据此定位工作区，不能传当前活动会话 */
  sessionId?: number | null;
  /** 外置微应用的前端地址；有值即外置（它有自己的后端，不经宿主桥） */
  externalUrl?: string;
  /** 窗口标题 / 提示里的应用名 */
  name?: string;
}

function bridge() {
  return typeof window === "undefined" ? undefined : window.chatcoderAPI;
}

/** 桌面端才有独立窗口能力（网页版没有 preload 桥）。 */
export function microAppWindowAvailable(): boolean {
  return Boolean(bridge()?.openMicroAppWindow);
}

/** 窗口实例的会话 id 兜底：调用方没给就当作「无会话」（页面照常显示，但 AI 侧无法定位工作区）。 */
function snapshot(value: number | null | undefined): number | null {
  return value == null || !Number.isFinite(Number(value)) ? null : Number(value);
}

/** key → 释放函数（解绑 + 引用计数 -1）；窗口关闭事件按 key 回收 */
const held = new Map<string, () => void>();
/** 没有面板标签可借 uid 时（拓展页 / 已安装列表）的窗口实例序号，保证每次都是新实例 */
let windowSeq = 0;
let subscribed = false;

function release(key: string): void {
  const fn = held.get(key);
  if (!fn) return;
  held.delete(key);
  try { fn(); } catch { /* 解绑失败不影响其它窗口 */ }
}

/** 全局订阅一次「窗口关闭 / 窗口加载失败」：前者释放绑定，后者给用户一条看得见的提示。
 *
 *  为什么加载失败也要提示：外置微应用的前端地址来自一份短 TTL 的清单，进程在 TTL 内退出时
 *  窗口会打开一个已经死掉的地址——主进程 `did-fail-load` 是唯一能发现这件事的地方，
 *  不提示就只剩一个白窗口，用户会以为应用坏了。
 */
function ensureSubscribed(): void {
  if (subscribed) return;
  const api = bridge();
  if (!api?.onMicroAppWindowClosed) return;
  subscribed = true;
  api.onMicroAppWindowClosed((payload) => {
    const key = String(payload?.key || "");
    if (key) release(key);
  });
  api.onMicroAppWindowLoadFailed?.((payload) => {
    const label = String(payload?.name || payload?.appId || "微应用");
    const reason = String(payload?.desc || "").trim();
    notify.error(
      `「${label}」界面没能加载出来：微应用进程可能已退出，请重新打开。`
      + (reason ? `（${reason}）` : ""),
    );
  });
}

/**
 * 打开（或聚焦）微应用的独立窗口。返回 true = 已经在窗口里呈现给用户了。
 * 失败时已用 notify.error 提示原因，调用方不需要再提示一遍（但可据此决定是否回退到面板）。
 */
export async function openMicroAppWindow(target: MicroAppWindowTarget): Promise<boolean> {
  const api = bridge();
  if (!api?.openMicroAppWindow) return false; // 网页版 / 老桌面端：交给调用方决定怎么降级

  const appId = String(target.appId || "").trim();
  if (!appId) return false;
  const name = String(target.name || appId);
  const version = String(target.version || "1.0.0");
  ensureSubscribed();

  // ── 外置微应用：主进程直接加载它自己的 http 地址，不需要宿主桥绑定 ──
  if (target.externalUrl) {
    const res = await api.openMicroAppWindow({ kind: "external", appId, url: target.externalUrl, name, version });
    if (!res?.ok) {
      notify.error(`打开「${name}」失败：${res?.error || "未知错误"}`);
      return false;
    }
    return true;
  }

  // ── 内置微应用：派生窗口实例 uid，并主窗口侧登记绑定（会话上下文随窗口一起保留）──
  windowSeq += 1;
  const windowUid = target.uid ? `${target.uid}-w` : `microapp#w${windowSeq}`;
  const sessionId = snapshot(target.sessionId);
  const stopHost = microappHost.start(); // 引用计数：面板标签全关掉后桥仍在线
  const unbind = microappHost.bind({ uid: windowUid, sessionId, appId, version });
  const releaseSelf = () => { unbind(); stopHost(); };

  let res: MicroAppWindowResult | undefined;
  try {
    res = await api.openMicroAppWindow({
      kind: "builtin",
      appId,
      version,
      entry: target.entry || "",
      uid: windowUid,
      sessionId,
      name,
    });
  } catch (err) {
    releaseSelf();
    notify.error(`打开「${name}」失败：${String(err)}`);
    return false;
  }
  if (!res?.ok) {
    releaseSelf();
    notify.error(`打开「${name}」失败：${res?.error || "未知错误"}`);
    return false;
  }
  if (res.reused) {
    // 已有窗口：本次不持有新绑定（那份属于第一次打开的调用）
    releaseSelf();
    return true;
  }
  if (res.key) held.set(res.key, releaseSelf);
  else releaseSelf();
  return true;
}
