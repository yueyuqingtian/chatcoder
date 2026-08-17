/** v19 插件化架构：统一组件/插件注册中心。
 * 「一切都是组件/插件」：侧栏、标题栏、消息流、输入框、右面板、思考块、工具树、子代理卡片
 * 均注册为 slot 插件；用户可在设置页「插件」中像拼积木一样替换任一 slot 的生效组件，
 * 也可通过外挂插件协议（~/.chatcoder/plugins）注入自定义组件替换系统组件。
 */
import { useSyncExternalStore, type ComponentType } from "react";

export type SlotId =
  | "sidebar"
  | "settings-sidebar"
  | "titlebar"
  | "message-flow"
  | "composer"
  | "empty-state"
  | "right-panel"
  | "thinking-block"
  | "tool-tree"
  | "subagent-card";

export interface PluginDescriptor {
  id: string;
  name: string;
  slot: SlotId;
  description: string;
  builtin: boolean;
  replaceable: boolean;
  /** v20: 插件默认 props 元数据（宿主可注入数据契约，如 message-flow 的 source/threadId），供设置页展示能力 */
  props?: Record<string, unknown>;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  component: ComponentType<any>;
}

const STORAGE_KEY = "chatcoder.plugin-overrides";

/** slot -> 插件列表 */
const pluginsBySlot = new Map<SlotId, PluginDescriptor[]>();
/** slot -> 生效插件 id */
const activeBySlot = new Map<SlotId, string>();
/** 内置默认插件 id（slot -> id） */
const builtinBySlot = new Map<SlotId, string>();

function loadOverrides(): Record<string, string> {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}") as Record<string, string>;
  } catch {
    return {};
  }
}
function saveOverrides() {
  const obj: Record<string, string> = {};
  for (const [slot, id] of activeBySlot) {
    if (builtinBySlot.get(slot) !== id) obj[slot] = id;
  }
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(obj)); } catch { /* ignore */ }
}

let _version = 0;
const _listeners = new Set<() => void>();
function _emit() {
  _version++;
  for (const l of _listeners) l();
}
export function subscribeRegistry(l: () => void): () => void {
  _listeners.add(l);
  return () => { _listeners.delete(l); };
}
export function registryVersion(): number {
  return _version;
}

export function registerPlugin(desc: PluginDescriptor): void {
  const list = pluginsBySlot.get(desc.slot) ?? [];
  if (!list.some((p) => p.id === desc.id)) list.push(desc);
  pluginsBySlot.set(desc.slot, list);
  if (desc.builtin) {
    builtinBySlot.set(desc.slot, desc.id);
    if (!activeBySlot.has(desc.slot)) activeBySlot.set(desc.slot, desc.id);
  }
  _emit();
}

export function listPlugins(): PluginDescriptor[] {
  const out: PluginDescriptor[] = [];
  for (const list of pluginsBySlot.values()) out.push(...list);
  return out;
}

export function listSlotPlugins(slot: SlotId): PluginDescriptor[] {
  return pluginsBySlot.get(slot) ?? [];
}

export function getActivePlugin(slot: SlotId): PluginDescriptor | null {
  const list = pluginsBySlot.get(slot) ?? [];
  const id = activeBySlot.get(slot);
  return list.find((p) => p.id === id) ?? list.find((p) => p.builtin) ?? null;
}

export function replaceSlot(slot: SlotId, pluginId: string): void {
  const list = pluginsBySlot.get(slot) ?? [];
  if (!list.some((p) => p.id === pluginId)) return;
  activeBySlot.set(slot, pluginId);
  saveOverrides();
  _emit();
}

export function resetSlot(slot: SlotId): void {
  const builtin = builtinBySlot.get(slot);
  if (builtin) activeBySlot.set(slot, builtin);
  saveOverrides();
  _emit();
}

export function unregisterExternal(pluginId: string): void {
  for (const [slot, list] of pluginsBySlot) {
    const next = list.filter((p) => p.id !== pluginId || p.builtin);
    pluginsBySlot.set(slot, next);
    if (activeBySlot.get(slot) === pluginId) {
      activeBySlot.set(slot, builtinBySlot.get(slot) ?? (next[0]?.id ?? ""));
    }
  }
  saveOverrides();
  _emit();
}

/** 启动时应用持久化替换 */
export function applyStoredOverrides(): void {
  const overrides = loadOverrides();
  for (const [slot, id] of Object.entries(overrides)) {
    const list = pluginsBySlot.get(slot as SlotId) ?? [];
    if (list.some((p) => p.id === id)) activeBySlot.set(slot as SlotId, id);
  }
  _emit();
}

/** 渲染某 slot 当前生效组件（内置默认 or 用户替换） */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function PluginSlot<P = any>({ slot, ...props }: P & { slot: SlotId }) {
  useSyncExternalStore(subscribeRegistry, registryVersion);
  const active = getActivePlugin(slot);
  if (!active) return null;
  const C = active.component;
  return <C {...(props as object)} />;
}
