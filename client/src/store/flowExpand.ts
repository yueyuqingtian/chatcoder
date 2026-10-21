/** 消息流展开块的展开状态（工具行 / 聚合行 / 思考块）。
 *
 *  这些块会随新消息到达被重新分组（单行并入聚合行、同文件连写合并），或随所在段落的切分
 *  换到另一个父节点下；组件因此重挂载，本地 state 随之丢失，用户刚展开的内容就被收起了。
 *  所以展开状态按稳定身份记在这里（工具调用 callKey / 思考消息 id），重挂载后仍按用户的选择呈现。
 *  只记用户操作过的块，未记录即默认态。
 *
 *  流式思考尚未落库，没有消息 id，它的展开态记在「流式键」下（主会话一个，子代理按线程区分）。
 *  思考段落库时，消息落库处（store/chat.ts 的 message.created）调用 handOffThinkingExpand，
 *  把展开态移交给落库后的思考块——同一段思考，展开的内容不会因为落库而收起。 */
import { useCallback, useEffect } from "react";
import { create } from "zustand";

interface FlowExpandState {
  /** key = 稳定身份；true = 展开，false = 用户收起（覆盖聚合行的默认展开） */
  open: Record<string, boolean>;
  setOpen: (key: string, open: boolean) => void;
  /** 段落落库：from 的展开态移交给 to（仅 from 为展开时写入 to），并清掉 from */
  handOff: (from: string, to: string) => void;
  drop: (key: string) => void;
}

const useFlowExpandStore = create<FlowExpandState>((set, get) => ({
  open: {},
  setOpen: (key, open) => {
    if (get().open[key] === open) return;
    set({ open: { ...get().open, [key]: open } });
  },
  handOff: (from, to) => {
    const { open } = get();
    if (open[from] === undefined) return;
    const next = { ...open };
    delete next[from];
    if (open[from]) next[to] = true;
    set({ open: next });
  },
  drop: (key) => {
    if (get().open[key] === undefined) return;
    const next = { ...get().open };
    delete next[key];
    set({ open: next });
  },
}));

/** 非 hook 读写入口：事件处理与测试直接使用。 */
export function readFlowExpand(key: string): boolean | undefined {
  return useFlowExpandStore.getState().open[key];
}

export function writeFlowExpand(key: string, open: boolean): void {
  useFlowExpandStore.getState().setOpen(key, open);
}

export function dropFlowExpand(key: string): void {
  useFlowExpandStore.getState().drop(key);
}

/** 落库后思考块的展开键。 */
export function thinkingKey(msgId: number): string {
  return `think:${msgId}`;
}

/** 流式思考（尚未落库）的展开键：主会话为 main，子代理按线程 id 区分。 */
export function liveThinkingKey(threadId?: number | null): string {
  return threadId == null ? "live:think:main" : `live:think:${threadId}`;
}

/** 流式思考段落落库：展开态移交给落库后的思考块。 */
export function handOffThinkingExpand(msgId: number, threadId?: number | null): void {
  useFlowExpandStore.getState().handOff(liveThinkingKey(threadId), thinkingKey(msgId));
}

/** 单个块的展开状态；未操作过时取 fallback（默认态）。 */
export function useFlowExpand(key: string, fallback = false): [boolean, (open: boolean) => void] {
  const stored = useFlowExpandStore((s) => s.open[key]);
  const setOpen = useCallback((open: boolean) => writeFlowExpand(key, open), [key]);
  return [stored ?? fallback, setOpen];
}

/** 流式思考行的展开状态：段落结束（落库移交，或中断后卸载）即清掉，下一段从收起开始。 */
export function useLiveThinkingExpand(threadId?: number): [boolean, (open: boolean) => void] {
  const key = liveThinkingKey(threadId);
  const [open, setOpen] = useFlowExpand(key);
  useEffect(() => () => dropFlowExpand(key), [key]);
  return [open, setOpen];
}

/** 聚合行（工具簇 / 同文件连写）的展开状态：内含已展开的单行时默认展开。
 *  这一默认会被记下，之后收起其中某一行不会连带把聚合行收起。 */
export function useFlowGroupExpand(key: string, memberKeys: string[]): [boolean, (open: boolean) => void] {
  const memberOpen = useFlowExpandStore((s) => memberKeys.some((k) => s.open[k] === true));
  const [open, setOpen] = useFlowExpand(key, memberOpen);
  useEffect(() => {
    if (memberOpen && readFlowExpand(key) === undefined) writeFlowExpand(key, true);
  }, [key, memberOpen]);
  return [open, setOpen];
}
