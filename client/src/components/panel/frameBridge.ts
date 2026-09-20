/**
 * <webview> / <iframe> 安全调用桥（BrowserPanel 专用）
 *
 * 为什么需要它：
 * Electron 的 <webview> 在「未 attach 到 DOM」或「dom-ready 尚未触发」时，
 * executeJavaScript / capturePage / openDevTools 等方法会**同步抛出**
 *   The WebView must be attached to the DOM and the dom-ready event emitted
 *   before this method can be called.
 * 同步异常无法被 .catch() 捕获。这类调用常发生在 React effect（尤其是依赖变化
 * 或卸载时的 cleanup）里，异常冒泡到顶层 ErrorBoundary 就会把整个应用打成白屏
 * （典型复现：右侧浏览器面板折叠/展开时 BrowserPanel 重挂载，标注探针 cleanup
 *  仍对已脱离 DOM 的 webview 调 executeJavaScript）。
 *
 * 本模块统一收口 guest 调用：
 * 1. 按标签页记录宿主元素与就绪状态（webview: dom-ready / iframe: load）；
 * 2. 未就绪时挂起调用，就绪后补执行；元素卸载或超时（该次交互已过期）则放弃；
 * 3. 同步异常与 promise rejection 一律转为结果对象/空值，绝不外溢到 React 渲染流程。
 */
import { useRef } from "react";

export type FrameEl = any;

/**
 * 是否为 Electron <webview> guest 宿主（可直接调用 executeJavaScript / capturePage）。
 * iframe 无此方法，需走 contentDocument 同源直查。
 */
export function isGuestFrame(el: FrameEl | HTMLIFrameElement | null | undefined): boolean {
  return Boolean(el) && typeof (el as FrameEl).executeJavaScript === "function";
}

/** guest 调用结果：notReady 表示宿主尚未 attach（调用被安全丢弃） */
export interface FrameResult<T = unknown> {
  ok: boolean;
  value?: T;
  error?: string;
  notReady?: boolean;
}

interface PendingCall {
  run: () => void;
  /** 作废该调用时必须调用，否则调用方的 promise 永不 settle（永久挂起） */
  cancel: () => void;
  timer: ReturnType<typeof setTimeout>;
}

interface FrameEntry {
  el: FrameEl;
  /** webview: 已触发 dom-ready；iframe: 已触发 load / readyState=complete */
  ready: boolean;
  /** 就绪前挂起的调用（元素卸载或超时后统一丢弃） */
  pending: PendingCall[];
}

/** 未就绪时的最大等待时长；超时视为该次交互已过期（面板已折叠 / 用户已切走） */
const READY_TIMEOUT_MS = 4000;

export interface FrameBridge {
  /** 标记宿主元素（React ref 回调）；元素被替换时作废旧挂起调用 */
  attach: (tabId: string, el: FrameEl) => void;
  /** 注销宿主元素（React ref 回调传 null / 面板卸载） */
  detach: (tabId: string) => void;
  /** 标记 guest 已就绪（dom-ready / load 事件回调） */
  markReady: (tabId: string, el?: FrameEl) => void;
  /** 元素其实已 attach 完成（能取到 guest id / iframe readyState=complete）时立即就绪 */
  markReadyIfAttached: (tabId: string, el: FrameEl) => boolean;
  get: (tabId: string) => FrameEl | null;
  isReady: (tabId: string) => boolean;
  /** 在 guest 内执行 JS；waitReady=false 时未就绪立即返回 notReady（悬停查询等即时场景） */
  invoke: <T = unknown>(tabId: string, code: string, opts?: { waitReady?: boolean }) => Promise<FrameResult<T>>;
  /** invoke 的简化版：失败/未就绪返回 null */
  call: <T = unknown>(tabId: string, code: string, opts?: { waitReady?: boolean }) => Promise<T | null>;
  /** 截取 guest 页面，返回 dataURL；未就绪或失败返回空串 */
  capture: (tabId: string) => Promise<string>;
  /** 打开 guest 开发者工具；未就绪返回 false（调用方可降级到主进程 IPC） */
  openDevTools: (tabId: string) => boolean;
}

export function useFrameBridge(): FrameBridge {
  const ref = useRef<FrameBridge | null>(null);
  if (!ref.current) ref.current = createFrameBridge();
  return ref.current;
}

/**
 * 桥接核心（不含 React）：独立成纯函数以便单测"同步抛错不外溢"这一关键不变量。
 */
export function createFrameBridge(): FrameBridge {
  const map = new Map<string, FrameEntry>();

  const dropPending = (entry: FrameEntry) => {
    for (const p of entry.pending.splice(0)) {
      clearTimeout(p.timer);
      // 必须 settle：否则调用方 await 永久挂起（面板折叠/元素替换就是这样发生的）
      p.cancel();
    }
  };

  /** 元素是否仍挂在文档上（已脱离 DOM 的元素调用 guest 方法同样会抛错） */
  const usable = (el: FrameEl) => Boolean(el) && el.isConnected !== false;

  const markReady = (tabId: string, el?: FrameEl) => {
    const entry = map.get(tabId);
    if (!entry || entry.ready) return;
    if (el && entry.el !== el) return;
    entry.ready = true;
    for (const p of entry.pending.splice(0)) {
      clearTimeout(p.timer);
      p.run();
    }
  };

  /**
   * 挂起一个待就绪执行的调用。
   * onAbort 在「超时」与「元素被卸载/替换」两种作废路径上都会被调用，
   * 从而保证调用方的 promise 一定 settle（否则 await 会永久挂起）。
   */
  const defer = (entry: FrameEntry, run: () => void, onAbort: () => void) => {
    const cancel = () => onAbort();
    const timer = setTimeout(() => {
      const idx = entry.pending.findIndex((p) => p.run === run);
      if (idx >= 0) entry.pending.splice(idx, 1);
      cancel();
    }, READY_TIMEOUT_MS);
    entry.pending.push({ run, cancel, timer });
  };

  const invoke = <T = unknown,>(tabId: string, code: string, opts?: { waitReady?: boolean }) =>
    new Promise<FrameResult<T>>((resolve) => {
      const entry = map.get(tabId);
      if (!entry) {
        resolve({ ok: false, notReady: true });
        return;
      }
      const run = () => {
        const el = entry.el;
        if (!usable(el) || typeof el.executeJavaScript !== "function") {
          resolve({ ok: false, notReady: true });
          return;
        }
        try {
          // 未 attach 时这里是同步抛错，必须就地兜住，否则会冒泡到 ErrorBoundary
          Promise.resolve(el.executeJavaScript(code)).then(
            (value: T) => resolve({ ok: true, value }),
            (e: unknown) => resolve({ ok: false, error: e instanceof Error ? e.message : String(e) }),
          );
        } catch (e) {
          resolve({ ok: false, error: e instanceof Error ? e.message : String(e) });
        }
      };
      if (entry.ready || opts?.waitReady === false) {
        run();
        return;
      }
      defer(entry, run, () => resolve({ ok: false, notReady: true }));
    });

  return {
    attach: (tabId, el) => {
      const prev = map.get(tabId);
      if (prev && prev.el !== el) {
        // 元素被替换（面板重挂载 / 标签重建）：旧的挂起调用立即作废
        dropPending(prev);
        map.set(tabId, { el, ready: false, pending: [] });
        return;
      }
      // React ref 回调每次渲染都会执行：同一元素只刷新引用，保留就绪状态与挂起队列
      map.set(tabId, { el, ready: prev ? prev.ready : false, pending: prev ? prev.pending : [] });
    },

    detach: (tabId) => {
      const entry = map.get(tabId);
      if (!entry) return;
      dropPending(entry);
      map.delete(tabId);
    },

    markReady,

    markReadyIfAttached: (tabId, el) => {
      if (!el) return false;
      // webview：能取到 guest id 即说明已 attach 且 dom-ready 已触发
      if (typeof el.getWebContentsId === "function") {
        try {
          if (el.getWebContentsId() > 0) {
            markReady(tabId, el);
            return true;
          }
        } catch {
          /* 未 attach：等待 dom-ready 事件 */
        }
        return false;
      }
      // iframe：同源可直接看 readyState（跨域取不到 document，交给 load 事件）
      try {
        const doc = el.contentDocument;
        if (doc && doc.readyState === "complete") {
          markReady(tabId, el);
          return true;
        }
      } catch {
        /* 跨域 iframe：忽略 */
      }
      return false;
    },

    get: (tabId) => map.get(tabId)?.el ?? null,

    isReady: (tabId) => Boolean(map.get(tabId)?.ready),

    invoke,

    call: <T = unknown,>(tabId: string, code: string, opts?: { waitReady?: boolean }) =>
      invoke<T>(tabId, code, opts).then((r) => (r.ok ? (r.value as T) : null)),

    capture: (tabId) =>
      new Promise<string>((resolve) => {
        const entry = map.get(tabId);
        if (!entry) {
          resolve("");
          return;
        }
        const run = () => {
          const el = entry.el;
          if (!usable(el) || typeof el.capturePage !== "function") {
            resolve("");
            return;
          }
          try {
            Promise.resolve(el.capturePage()).then(
              (img: { toDataURL?: () => string } | null | undefined) => resolve(img?.toDataURL?.() || ""),
              () => resolve(""),
            );
          } catch {
            resolve("");
          }
        };
        if (entry.ready) {
          run();
          return;
        }
        defer(entry, run, () => resolve(""));
      }),

    openDevTools: (tabId) => {
      const entry = map.get(tabId);
      if (!entry || !entry.ready) return false;
      const el = entry.el;
      if (!usable(el) || typeof el.openDevTools !== "function") return false;
      try {
        el.openDevTools();
        return true;
      } catch {
        return false;
      }
    },
  };
}
