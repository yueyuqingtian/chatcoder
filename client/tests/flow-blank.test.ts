import assert from "node:assert/strict";
import { mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { buildSync } from "esbuild";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { Virtualizer } from "@tanstack/react-virtual";
import { shouldMountBlock, observeFlowBlock, updateFlowViewport, type BlockViewport } from "../src/perf/flowViewport.ts";
import { refreshFlowMeasurements, resolveFlowItemSize } from "../src/perf/flowGeometry.ts";
import { acquire, release } from "../src/perf/bus.ts";

// 用已安装的 Vite 编译器加载真实 TSX，验证首屏输出，不用源码字符串断言代替渲染。
mkdirSync(".cache", { recursive: true });
const target = resolve(".cache/flow-blank-component.mjs");
buildSync({ entryPoints: ["src/components/ui/ViewportBlock.tsx"], bundle: true, platform: "node", format: "esm", packages: "external", outfile: target });
const { ViewportBlock } = await import(pathToFileURL(target).href);
const ordinary = renderToStaticMarkup(createElement(ViewportBlock, { estimate: 2000 }, createElement("p", null, "首屏正文立即可见")));
assert.ok(ordinary.includes("首屏正文立即可见"));
const originalError = console.error;
console.error = (...args: unknown[]) => { if (!String(args[0]).includes("useLayoutEffect does nothing on the server")) originalError(...args); };
try {
  const long = renderToStaticMarkup(createElement(ViewportBlock, { windowed: true }, "长内容首次挂载"));
  assert.ok(long.includes("长内容首次挂载"));
} finally { console.error = originalError; }

const frames = new Map<number, FrameRequestCallback>();
let frameId = 0;
Object.defineProperty(globalThis, "requestAnimationFrame", { value: (fn: FrameRequestCallback) => { frames.set(++frameId, fn); return frameId; }, configurable: true });
Object.defineProperty(globalThis, "cancelAnimationFrame", { value: (id: number) => frames.delete(id), configurable: true });
const events = { addEventListener() {}, removeEventListener() {} };
const root = { ...events, isConnected: true, clientWidth: 400, clientHeight: 300,
  querySelector: () => null, getBoundingClientRect: () => ({ top: 0, bottom: 300 }) };
let top = 4000;
const block = { isConnected: true, getBoundingClientRect: () => ({ top, bottom: top + 200, width: 400, height: 200 }) };
Object.defineProperty(globalThis, "window", { value: events, configurable: true });
Object.defineProperty(globalThis, "document", { value: { ...events, getSelection: () => null,
  body: { classList: { contains: () => false } }, documentElement: { getAttribute: () => null } }, configurable: true });
let latest: BlockViewport | null = null;
const off = observeFlowBlock(root as unknown as HTMLElement, block as unknown as HTMLElement, (value) => { latest = value; });
updateFlowViewport(root as unknown as HTMLElement, true);
assert.equal(shouldMountBlock(true, true, latest!), false);
// 拖拽期进入视口必须挂载；隐藏时不清空已显示内容，恢复不要求触发滚动。
acquire("panel-drag"); top = 20; updateFlowViewport(root as unknown as HTMLElement, true);
assert.equal(shouldMountBlock(false, true, latest!), true);
assert.equal(shouldMountBlock(false, false, { ...latest!, intersects: false }), false);
root.clientHeight = 0; updateFlowViewport(root as unknown as HTMLElement, true);
assert.equal(shouldMountBlock(true, true, latest!), true);
root.clientHeight = 300; release("panel-drag"); updateFlowViewport(root as unknown as HTMLElement, true);
assert.equal(shouldMountBlock(false, true, latest!), true);
off(); assert.equal(frames.size, 0);

let notifications = 0;
const virtualizer = new Virtualizer<HTMLDivElement, Element>({ count: 2, getScrollElement: () => null,
  estimateSize: () => 100, scrollToFn() {}, observeElementRect() {}, observeElementOffset() {}, onChange: () => notifications++ });
assert.equal(virtualizer.getTotalSize(), 200);
virtualizer.resizeItem(0, 900); assert.equal(virtualizer.getTotalSize(), 1000);
virtualizer.elementsCache.set(0, { isConnected: true, getAttribute: () => "0", getBoundingClientRect: () => ({ height: 240 }) } as unknown as Element);
root.clientHeight = 0;
assert.equal(refreshFlowMeasurements(root as unknown as HTMLElement, virtualizer), false);
assert.equal(virtualizer.getTotalSize(), 1000);
root.clientHeight = 300; const before = notifications;
assert.equal(refreshFlowMeasurements(root as unknown as HTMLElement, virtualizer), true);
assert.equal(virtualizer.getTotalSize(), 340);
assert.equal(virtualizer.measurementsCache[1].start, 240);
assert.ok(notifications > before);
// 空尾槽在有效布局中必须归零；隐藏造成的零尺寸仍应保留旧高度。
assert.equal(resolveFlowItemSize(0, 240, 140, true), 0);
assert.equal(resolveFlowItemSize(0, 240, 140, false), 240);
assert.equal(resolveFlowItemSize(0, undefined, 140, false), 140);
assert.equal(resolveFlowItemSize(NaN, 240, 140, true), 240);
virtualizer.elementsCache.set(1, { isConnected: true, getAttribute: () => "1", getBoundingClientRect: () => ({ height: 0 }) } as unknown as Element);
refreshFlowMeasurements(root as unknown as HTMLElement, virtualizer);
assert.equal(virtualizer.getTotalSize(), 240);
assert.equal(virtualizer.itemSizeCache.get(1), 0);
console.log("普通正文首屏、长内容首屏、busy 进入可见区、隐藏恢复及公开缓存失效回归通过");
