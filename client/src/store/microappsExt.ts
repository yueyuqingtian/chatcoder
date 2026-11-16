/** 外置微应用清单与进程状态（plan-external-microapps · docs/app1/13-external-microapps.md）。
 *
 *  与 `store/microapps.ts`（内置库）的分工：
 *  - 内置微应用由后端 `/api/microapps` 列出，页面是 `app://` 资源，走宿主桥；
 *  - 外置微应用由**主进程**扫描 `~/.chatcoder/microapps-ext` 列出，页面是它自己的 http 地址，
 *    进程也由主进程拉起。所以要经 preload 的 `microappsExt*` 四个 IPC 取用，不能走后端。
 *
 *  为什么单独一个 store 而不是塞进 microapps.ts：两者的数据来源、失败语义、刷新时机都不同
 *  （后端不可达 vs 主进程不可用），混在一起会让「清单为空」无法归因。
 */
import { create } from "zustand";

/** 主进程不可用（网页版 / 老版本桌面端）时的统一失败原因。 */
export const EXT_UNAVAILABLE = "microapps-ext-unavailable";

function bridge() {
  if (typeof window === "undefined") return undefined;
  const api = window.chatcoderAPI;
  if (!api?.microappsExtList) return undefined;
  return api;
}

interface MicroappsExtState {
  items: MicroappsExtStatus[];
  /** 是否成功取过一次（失败不算，下次打开入口会重试） */
  loaded: boolean;
  loading: boolean;
  loadedAt: number;
  /** 最近一次失败原因（空串 = 正常）；入口据此显示「读不到」而不是「一个都没装」 */
  error: string;
  /** 是否有宿主支持（网页版没有） */
  available: boolean;
  load: (force?: boolean) => Promise<void>;
  refreshIfStale: (ttlMs?: number) => Promise<void>;
  /** 启动一个外置微应用，成功返回它的前端地址 */
  start: (id: string) => Promise<{ ok: boolean; url?: string; error?: string }>;
  stop: (id: string) => Promise<boolean>;
  /** 按 id 取一条 */
  resolve: (id: string) => MicroappsExtStatus | undefined;
}

export const useMicroappsExtStore = create<MicroappsExtState>((set, get) => ({
  items: [],
  loaded: false,
  loading: false,
  loadedAt: 0,
  error: "",
  available: Boolean(bridge()),

  load: async (force = false) => {
    const { loading, loaded } = get();
    const api = bridge();
    if (!api) {
      set({ available: false, error: EXT_UNAVAILABLE });
      return;
    }
    if (loading || (loaded && !force)) return;
    set({ loading: true, available: true });
    try {
      const res = await api.microappsExtList!();
      if (res && res.ok === false) {
        set({ loading: false, error: String(res.error || "读取失败") });
        return;
      }
      set({
        items: Array.isArray(res?.apps) ? res.apps : [],
        loaded: true,
        loading: false,
        loadedAt: Date.now(),
        error: "",
      });
    } catch (err) {
      // 失败保持 loaded=false：下次进入口还能自动重试（与内置清单同一口径）
      set({ loading: false, error: String((err as Error)?.message || err) });
    }
  },

  refreshIfStale: async (ttlMs = 15000) => {
    const { loadedAt, loading } = get();
    if (loading) return;
    if (loadedAt && Date.now() - loadedAt < ttlMs) return;
    await get().load(true);
  },

  start: async (id) => {
    const api = bridge();
    if (!api?.microappsExtStart) return { ok: false, error: EXT_UNAVAILABLE };
    try {
      const res = await api.microappsExtStart(id);
      // 起完立刻刷新一次状态，让入口上的状态点与端口立刻反映出来
      const st = await api.microappsExtStatus?.(id);
      if (st?.ok && st.status) {
        set((s) => ({
          items: s.items.some((x) => x.id === id)
            ? s.items.map((x) => (x.id === id ? st.status! : x))
            : [...s.items, st.status!],
        }));
      }
      if (res?.ok) return { ok: true, url: res.url };
      return { ok: false, error: String(res?.error || "启动失败") };
    } catch (err) {
      return { ok: false, error: String((err as Error)?.message || err) };
    }
  },

  stop: async (id) => {
    const api = bridge();
    if (!api?.microappsExtStop) return false;
    try {
      const res = await api.microappsExtStop(id);
      await get().load(true);
      return Boolean(res?.ok);
    } catch {
      return false;
    }
  },

  resolve: (id) => get().items.find((x) => x.id === id),
}));

/** 把主进程返回的错误翻成人话（错误码前缀与 external-microapps.cjs 一一对应）。 */
export function describeExtError(raw: string): string {
  const s = String(raw || "");
  const table: Array<[string, string]> = [
    [EXT_UNAVAILABLE, "当前桌面端版本不支持外置微应用"],
    ["microapp-not-found", "没有找到这个外置微应用（检查 ~/.chatcoder/microapps-ext 目录）"],
    ["microapp-invalid", "微应用声明不完整，请修正 microapp.json"],
    ["microapp-spawn-failed", "启动命令执行失败，检查 server.command 与 cwd"],
    ["microapp-not-ready", "微应用没有在超时内就绪，检查它的 health 端点与启动日志"],
    ["microapp-unreachable", "连不上微应用（进程可能已退出）"],
    ["microapp-no-methods", "该微应用没有声明任何方法"],
    ["method-not-declared", "该方法没有在 microapp.json 里声明"],
  ];
  for (const [prefix, human] of table) {
    if (s.startsWith(prefix)) return human;
  }
  return s || "未知错误";
}
