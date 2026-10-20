/**
 * 贴底跟随内核测试（plan-20261020-pts）。
 * 纯逻辑，Node 原生 TS 运行：`node tests/sticky-bottom.test.ts`（client 目录）。
 *
 * 用可控的假滚动容器驱动 StickyBottom，固化这些规则：
 *  - 布局 / 程序滚动造成的位移（测量补偿、位置钳制）不解除跟随，跟随中被挪离底部立即补回；
 *  - 只有用户意图（向上滚轮 + 容器确实上移）才解除跟随；
 *  - 内嵌滚动区还能上滚时，滚轮由它消费，不影响主消息流；
 *  - 容器尺寸为 0（最小化）时不做判定，恢复尺寸即贴底；
 *  - 回到底部即恢复跟随；release() / stick() 显式切换；拖选文字时解除跟随。
 */

type Listener = (e: any) => void;

class FakeTarget {
  private map = new Map<string, Set<Listener>>();
  addEventListener(type: string, fn: Listener) {
    if (!this.map.has(type)) this.map.set(type, new Set());
    this.map.get(type)!.add(fn);
  }
  removeEventListener(type: string, fn: Listener) { this.map.get(type)?.delete(fn); }
  dispatch(type: string, e: any = {}) { for (const fn of [...(this.map.get(type) ?? [])]) fn(e); }
}

/** 假滚动容器：scrollTop 写入按浏览器规则钳制到 [0, scrollHeight - clientHeight] */
class FakeScroller extends FakeTarget {
  isConnected = true;
  clientWidth = 800;
  clientHeight = 600;
  scrollHeight = 2000;
  clientLeft = 0;
  private _top = 0;
  get scrollTop() { return this._top; }
  set scrollTop(v: number) { this._top = Math.max(0, Math.min(v, this.scrollHeight - this.clientHeight)); }
  contains(n: unknown) { return n === this || n === innerNode; }
  getBoundingClientRect() { return { left: 0, top: 0, right: 800, bottom: 600 }; }
  scrollTo(opts: { top: number }) { this.scrollTop = opts.top; }
  /** 模拟浏览器：滚动位置变化后派发 scroll 事件 */
  scrollBy(dy: number) { this.scrollTop = this.scrollTop + dy; this.dispatch("scroll"); }
}

// ── 全局桩：window / document / ResizeObserver / Element ──
const win = new FakeTarget() as any;
win.setTimeout = (fn: () => void, ms: number) => setTimeout(fn, ms);
win.clearTimeout = (id: any) => clearTimeout(id);
const doc = new FakeTarget() as any;
doc.visibilityState = "visible";
doc.body = {};
let selectionText = "";
doc.getSelection = () => ({
  isCollapsed: selectionText === "",
  rangeCount: selectionText ? 1 : 0,
  anchorNode: innerNode,
  toString: () => selectionText,
});
const roCallbacks: Array<() => void> = [];
class FakeRO {
  cb: () => void;
  constructor(cb: () => void) { this.cb = cb; roCallbacks.push(cb); }
  observe() {}
  unobserve() {}
  disconnect() {}
}
(globalThis as any).window = win;
(globalThis as any).document = doc;
(globalThis as any).ResizeObserver = FakeRO;
(globalThis as any).Element = class {};
(globalThis as any).HTMLElement = class {};
(globalThis as any).getComputedStyle = () => ({ overflowY: "auto" });
const innerNode = { id: "inner" };

const { StickyBottom } = await import("../src/components/chat/stickyBottom.ts");

let failures = 0;
function assert(cond: boolean, msg: string) {
  if (!cond) { failures++; console.error(`FAIL: ${msg}`); }
  else { console.log(`ok: ${msg}`); }
}
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const resize = () => { for (const cb of roCallbacks) cb(); };
const dist = (el: FakeScroller) => el.scrollHeight - el.clientHeight - el.scrollTop;

function setup() {
  roCallbacks.length = 0;
  selectionText = "";
  const el = new FakeScroller();
  const jumps: boolean[] = [];
  const s = new StickyBottom({ onJumpChange: (v) => jumps.push(v) });
  s.attach(el as unknown as HTMLElement);
  s.stick();
  return { el, s, jumps };
}

// 1. 内容增长：跟随中贴底
{
  const { el, s } = setup();
  assert(dist(el) === 0, "stick() 后位于底部");
  el.scrollHeight += 300;
  resize();
  assert(s.isFollowing() && dist(el) === 0, "内容增长后仍贴底");
  s.detach();
}

// 2. 非用户位移（测量补偿 / 位置钳制）不解除跟随，并立即补回
{
  const { el, s } = setup();
  el.scrollHeight += 400;          // 内容先变高
  el.scrollBy(-120);               // 虚拟列表的位置补偿把视图往上挪（无任何用户输入）
  assert(s.isFollowing(), "无用户意图的上移不解除跟随");
  assert(dist(el) === 0, "跟随中被挪离底部会立即补回");
  s.detach();
}

// 3. 用户向上滚轮 + 容器确实上移 → 解除跟随；之后内容增长不拉回
{
  const { el, s, jumps } = setup();
  el.dispatch("wheel", { deltaY: -120, deltaX: 0, ctrlKey: false, target: el });
  el.scrollBy(-200);
  assert(!s.isFollowing(), "用户上滑解除跟随");
  el.scrollHeight += 500;
  resize();
  assert(dist(el) === 700, "解除跟随后内容增长不把视图拉回");
  assert(jumps[jumps.length - 1] === true, "离底较远时显示回到底部");
  s.detach();
}

// 4. 回到底部即恢复跟随
{
  const { el, s } = setup();
  el.dispatch("wheel", { deltaY: -120, deltaX: 0, target: el });
  el.scrollBy(-300);
  assert(!s.isFollowing(), "上滑后不跟随");
  await sleep(400);                // 越过上滑意图窗口
  el.dispatch("wheel", { deltaY: 120, deltaX: 0, target: el });
  el.scrollBy(300);
  assert(s.isFollowing(), "滚回底部恢复跟随");
  s.detach();
}

// 5. 内嵌滚动区还能上滚时，滚轮由它消费：主消息流不暂停、不解除
{
  const { el, s } = setup();
  const inner: any = { scrollHeight: 500, clientHeight: 100, scrollTop: 50, parentElement: el };
  Object.setPrototypeOf(inner, (globalThis as any).HTMLElement.prototype);
  (globalThis as any).Element = (globalThis as any).HTMLElement;
  el.dispatch("wheel", { deltaY: -100, deltaX: 0, target: inner });
  el.scrollHeight += 200;
  resize();
  assert(s.isFollowing() && dist(el) === 0, "内嵌区上滚不影响主消息流跟随");
  (globalThis as any).Element = class {};
  s.detach();
}

// 6. 容器尺寸为 0（最小化）期间不判定；恢复后贴底
{
  const { el, s } = setup();
  el.clientHeight = 0;
  el.clientWidth = 0;
  el.scrollHeight += 800;          // 最小化期间内容继续增长
  el.scrollBy(-1000);              // 浏览器钳制等引起的滚动
  resize();
  assert(s.isFollowing(), "尺寸为 0 时不改变跟随状态");
  el.clientHeight = 600;
  el.clientWidth = 800;
  resize();
  assert(dist(el) === 0, "恢复尺寸后贴底");
  s.detach();
}

// 7. release() 后不再贴底；stick() 恢复
{
  const { el, s } = setup();
  s.release();
  el.scrollHeight += 300;
  resize();
  assert(!s.isFollowing() && dist(el) === 300, "release() 后内容增长不贴底");
  s.stick();
  assert(s.isFollowing() && dist(el) === 0, "stick() 恢复跟随并贴底");
  s.detach();
}

// 8. 拖选文字期间内容增长：解除跟随，选区不被拖走
{
  const { el, s } = setup();
  el.dispatch("pointerdown", { target: innerNode, clientX: 100 });
  selectionText = "选中的文字";
  el.scrollHeight += 300;
  resize();
  assert(!s.isFollowing() && dist(el) === 300, "拖选时解除跟随");
  win.dispatch("pointerup", {});
  s.detach();
}

// 9. requestPin() 合并为一次微任务内贴底
{
  const { el, s } = setup();
  el.scrollHeight += 100;
  s.requestPin();
  s.requestPin();
  assert(dist(el) === 100, "requestPin() 不同步执行");
  await Promise.resolve();
  assert(dist(el) === 0, "微任务里完成贴底");
  s.detach();
}

if (failures > 0) {
  console.error(`\n${failures} 项失败`);
  process.exit(1);
}
console.log("\n全部通过");
