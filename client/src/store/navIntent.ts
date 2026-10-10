/** 跨页面导航意图（plan-20261009-ui U2）：输入框「+」浮窗里的「管理插件 / 探索更多插件」
 *  需要落到目标页并**预选对应分类**。目标页挂载时读取并消费一次意图。 */
import { create } from "zustand";

export type ExtKind = "plugin" | "skill" | "connector";

interface NavIntentState {
  /** 设置 → 拓展（已安装管理）要预选的分类 */
  installedKind: ExtKind | null;
  /** 左侧「拓展」市场页要预选的标签 */
  marketKind: ExtKind | null;
  takeInstalledKind: () => ExtKind | null;
  takeMarketKind: () => ExtKind | null;
}

export const useNavIntentStore = create<NavIntentState>((set, get) => ({
  installedKind: null,
  marketKind: null,
  takeInstalledKind: () => {
    const k = get().installedKind;
    if (k) set({ installedKind: null });
    return k;
  },
  takeMarketKind: () => {
    const k = get().marketKind;
    if (k) set({ marketKind: null });
    return k;
  },
}));

/** 打开「设置 → 拓展」并预选分类 */
export function openExtensionsManager(kind: ExtKind) {
  useNavIntentStore.setState({ installedKind: kind });
  window.dispatchEvent(new CustomEvent("chatcoder:open-settings", { detail: { tab: "extensions" } }));
}

/** 打开左侧「拓展」市场页并预选标签 */
export function openExtensionMarket(kind: ExtKind) {
  useNavIntentStore.setState({ marketKind: kind });
  window.dispatchEvent(new CustomEvent("chatcoder:navigate", { detail: { nav: "skills" } }));
}
