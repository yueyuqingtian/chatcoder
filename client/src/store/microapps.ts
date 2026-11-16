/** 已安装微应用缓存（plan-693-2868 P1 / P4）。
 *
 *  为什么要有这一层，而不是各调用点自己 fetch：
 *  ① `/` 菜单的「微应用」分组与工具卡的「查看界面」要的是**同一份**库内清单；
 *  ② `microapp_call` 的参数里只有 appId + method，**没有 version**，
 *     而右侧面板的标签要求 `meta.version` 非空才肯渲染（见 MicroAppPanel）——
 *     工具卡点「查看界面」时必须能从清单里把 version / entry 补出来；
 *  ③ 只改一处（`api.listMicroApps`）就能让 P4 接上真实 `/api/microapps` 路由。
 *
 *  失败即空列表：后端不可用时不该在输入框里弹错，菜单照常展示「还没有微应用」空态。
 */
import { create } from "zustand";
import { api, type MicroAppOut } from "../api/client";

interface MicroAppsState {
  items: MicroAppOut[];
  /** 是否已成功取过一次（失败不算，下次进菜单会重试） */
  loaded: boolean;
  loading: boolean;
  /** 最近一次**成功**拉取的时间戳（0 = 还没成功过）：菜单据此判断要不要补一次 */
  loadedAt: number;
  /** 拉取清单；force=true 时忽略 loaded（安装 / 删除微应用后刷新用） */
  load: (force?: boolean) => Promise<void>;
  /** 菜单打开时调用：距上次成功拉取超过 ttlMs 才补一次（store 侧去重，热路径不进击键） */
  refreshIfStale: (ttlMs?: number) => Promise<void>;
  /** 按 appId 取一条（含 version / entry）——「查看界面」用它补齐参数 */
  resolve: (appId: string) => MicroAppOut | undefined;
}

export const useMicroAppsStore = create<MicroAppsState>((set, get) => ({
  items: [],
  loaded: false,
  loading: false,
  loadedAt: 0,

  load: async (force = false) => {
    const { loaded, loading } = get();
    if (loading || (loaded && !force)) return;
    set({ loading: true });
    try {
      // plan-693-2868（P4）：`GET /api/microapps` 返回 `{apps, count, root}` 而不是裸数组。
      // 旧写法按数组取用，`Array.isArray(apps)` 恒为 false → 清单永远是空（静默假空态）。
      const res = await api.listMicroApps();
      set({
        items: Array.isArray(res?.apps) ? res.apps : [],
        loaded: true, loading: false, loadedAt: Date.now(),
      });
    } catch {
      // 后端不可用：**不记 loaded**（plan-693-2868 回归修复）——桌面端冷启动时首次挂载常常
      // 撞上后端还没就绪，此前在这里写 loaded: true 等于把「拉取失败」记成「取过且为空」，
      // 之后新建 / 安装的微应用要重启应用才出现在菜单里。保持 false，下次进菜单自动重试；
      // 清单也清空：与「取过但确实没有应用」保持同一份口径，避免残留旧列表。
      set({ items: [], loaded: false, loading: false });
    }
  },

  refreshIfStale: async (ttlMs = 3000) => {
    const { loaded, loadedAt } = get();
    // 刚拉过就不重复：`/` 菜单一开一关很轻，但后端每次都要扫一遍磁盘库（§6.5 的 list 实现）
    if (loaded && Date.now() - loadedAt < ttlMs) return;
    await get().load(true);
  },

  resolve: (appId) => get().items.find((m) => m.appId === appId),
}));
