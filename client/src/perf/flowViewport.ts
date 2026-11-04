import { isBusy, subscribe } from "./bus";

export interface BlockViewport {
  valid: boolean;
  busy: boolean;
  width: number;
  height: number;
  intersects: boolean;
  selected: boolean;
}

/** plan-20261103-flow-blank：进入视口不能被 busy 阻止；无有效几何时保留上次内容。 */
export function shouldMountBlock(previous: boolean, measured: boolean, frame: BlockViewport): boolean {
  if (!frame.valid) return previous;
  if (frame.selected || frame.intersects) return true;
  if (frame.busy) return previous;
  return !measured;
}

type Listener = (frame: BlockViewport) => void;
const coordinators = new WeakMap<HTMLElement, FlowViewport>();

/** 同一消息流只批量读一次布局；滑动、重排及重新显示都能推进专用窗口。 */
class FlowViewport {
  private blocks = new Map<HTMLElement, Listener>();
  private observer: ResizeObserver | null = null;
  private content: Element | null = null;
  private frame = 0;
  private disposed = false;
  private offBusy: () => void;
  constructor(private root: HTMLElement) {
    this.root.addEventListener("scroll", this.schedule, { passive: true });
    document.addEventListener("selectionchange", this.schedule);
    window.addEventListener("focus", this.schedule);
    document.addEventListener("visibilitychange", this.schedule);
    this.offBusy = subscribe((busy) => { if (!busy) this.schedule(); });
    if (typeof ResizeObserver !== "undefined") {
      this.observer = new ResizeObserver(this.schedule);
      this.observer.observe(root);
    }
  }
  add(element: HTMLElement, listener: Listener): () => void {
    this.blocks.set(element, listener);
    this.observer?.observe(element);
    this.schedule();
    return () => {
      this.blocks.delete(element); this.observer?.unobserve(element);
      if (!this.blocks.size) this.dispose();
    };
  }
  schedule = () => {
    if (!this.disposed && !this.frame) this.frame = requestAnimationFrame(this.flush);
  };
  flush = () => {
    if (this.frame) cancelAnimationFrame(this.frame);
    this.frame = 0;
    const content = this.root.querySelector(".message-flow-virtual-inner");
    if (content !== this.content) {
      if (this.content) this.observer?.unobserve(this.content);
      this.content = content;
      if (content) this.observer?.observe(content);
    }
    const rootRect = this.root.getBoundingClientRect();
    const valid = this.root.isConnected && this.root.clientWidth > 0 && this.root.clientHeight > 0;
    const busy = isBusy();
    const selection = document.getSelection();
    const range = selection && !selection.isCollapsed && selection.rangeCount ? selection.getRangeAt(0) : null;
    // 先读齐，再通知 React，防止「逐块读写」反复触发布局。
    const frames: Array<[Listener, BlockViewport]> = [];
    for (const [element, listener] of this.blocks) {
      const rect = element.getBoundingClientRect();
      let selected = false;
      try { selected = Boolean(range?.intersectsNode(element)); } catch { /* 选区可能刚随父项卸载 */ }
      frames.push([listener, { valid: valid && element.isConnected && rect.width > 0,
        busy, width: rect.width, height: rect.height, selected,
        intersects: rect.bottom > rootRect.top - 900 && rect.top < rootRect.bottom + 900 }]);
    }
    for (const [listener, frame] of frames) listener(frame);
  };
  private dispose() {
    this.disposed = true;
    if (this.frame) cancelAnimationFrame(this.frame);
    this.observer?.disconnect(); this.offBusy();
    this.root.removeEventListener("scroll", this.schedule);
    document.removeEventListener("selectionchange", this.schedule);
    window.removeEventListener("focus", this.schedule);
    document.removeEventListener("visibilitychange", this.schedule);
    coordinators.delete(this.root);
  }
}

export function observeFlowBlock(root: HTMLElement, element: HTMLElement, listener: Listener): () => void {
  let coordinator = coordinators.get(root);
  if (!coordinator) { coordinator = new FlowViewport(root); coordinators.set(root, coordinator); }
  return coordinator.add(element, listener);
}
export function updateFlowViewport(root: HTMLElement | null, immediate = false): void {
  const coordinator = root ? coordinators.get(root) : undefined;
  if (immediate) coordinator?.flush(); else coordinator?.schedule();
}

export interface FlowDiagnostic {
  session: string | number;
  epoch: number;
  cause: "outer" | "inner";
  outcome: "repair" | "unresolved";
  count: number;
  width: number;
  height: number;
  top: number;
  modelOffset: number | null;
  range: { startIndex: number; endIndex: number } | null;
  mountedItems: number;
  mountedWindows: number;
}
const diagnostics: Array<FlowDiagnostic & { at: number }> = [];
/** plan-20261103-flow-blank：每代每类只留一条，最多十六条，不记录正文也不产生恢复轮询。 */
export function recordFlowDiagnostic(value: FlowDiagnostic): void {
  if (diagnostics.some((entry) => entry.session === value.session && entry.epoch === value.epoch
    && entry.cause === value.cause && entry.outcome === value.outcome)) return;
  diagnostics.push({ ...value, at: Date.now() });
  if (diagnostics.length > 16) diagnostics.shift();
  if (value.outcome === "unresolved") console.warn("[消息流布局未恢复]", value);
}
const inspectors = new Map<string, () => Record<string, unknown>>();
export function registerFlowInspector(key: string, read: () => Record<string, unknown>): () => void {
  inspectors.set(key, read);
  return () => { if (inspectors.get(key) === read) inspectors.delete(key); };
}
export function getFlowDiagnostics() {
  // 手动读取才收集当前几何，历史有部分空白但仍有尾部输出时也能取得现场。
  return { recent: diagnostics.map((entry) => ({ ...entry })), current: Array.from(inspectors.values(), (read) => read()) };
}
if (typeof window !== "undefined") {
  (window as Window & { __chatcoderFlowDiagnostics?: typeof getFlowDiagnostics }).__chatcoderFlowDiagnostics = getFlowDiagnostics;
}
