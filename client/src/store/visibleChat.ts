import { useCallback, useRef, useSyncExternalStore } from "react";
import { useChatStore } from "./chat";

/** plan-20261101-performance：保活标签只保留快照，隐藏期间不为后台数据推进 React。 */
export function useVisibleChat<T>(selector: (state: ReturnType<typeof useChatStore.getState>) => T, visible: boolean): T {
  const selectorRef = useRef(selector);
  selectorRef.current = selector;
  const snapshot = useRef(selector(useChatStore.getState()));
  const subscribe = useCallback((listener: () => void) => visible
    ? useChatStore.subscribe(listener) : () => {}, [visible]);
  const read = useCallback(() => {
    if (visible) snapshot.current = selectorRef.current(useChatStore.getState());
    return snapshot.current;
  }, [visible]);
  return useSyncExternalStore(subscribe, read, read);
}
