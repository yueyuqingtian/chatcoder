/**
 * 停泊网页的视口守卫（plan-20261018-operator-boost）。
 *
 * 停泊容器（.browser-tab-frame-wrapper.is-parked）靠 `position:fixed` 铺满视口，让面板不可见时网页照常出帧。
 * 但只要某个祖先带 transform / filter / perspective / backdrop-filter / will-change / contain，它就会成为
 * fixed 后代的包含块：停泊容器被带到祖先的位置，并被祖先（例如宽度为 0 的折叠面板）的 overflow 裁掉，
 * 网页落到视口外、完全不出帧——截图超时、点击变慢。入场动画以 `both` 填充残留的单位矩阵 transform 就是实例。
 *
 * CSS 源头已修；这里是兜底：AI 动作 / 截图前检查停泊容器是否在视口原点，不在就给造成包含块的祖先临时加
 * `!important` 的中性值（transform:none 等），网页恢复为相对视口定位；停泊结束时还原。
 */

const CONTAINING_PROPS: Array<[string, string]> = [
  ["transform", "none"],
  ["filter", "none"],
  ["perspective", "none"],
  ["backdrop-filter", "none"],
  ["will-change", "auto"],
  ["contain", "none"],
];

/** 已覆盖的祖先 → 覆盖前的内联值（属性名 → [值, 优先级]） */
const healed = new Map<HTMLElement, Map<string, [string, string]>>();

function createsContainingBlock(cs: CSSStyleDeclaration): boolean {
  return cs.transform !== "none"
    || cs.filter !== "none"
    || cs.perspective !== "none"
    || ((cs as unknown as Record<string, string>).backdropFilter || "none") !== "none"
    || /transform|filter|perspective/.test(cs.willChange || "")
    || /paint|layout|strict|content/.test(cs.contain || "");
}

/** 停泊容器不在视口原点时修正祖先；返回是否做了修正。非停泊容器直接忽略。 */
export function healParkedWrapper(wrapper: HTMLElement | null | undefined): boolean {
  if (!wrapper || !wrapper.classList.contains("is-parked")) return false;
  const r = wrapper.getBoundingClientRect();
  if (Math.abs(r.left) < 1 && Math.abs(r.top) < 1) return false;
  let fixed = false;
  for (let el = wrapper.parentElement; el && el !== document.body; el = el.parentElement) {
    if (!createsContainingBlock(getComputedStyle(el))) continue;
    let saved = healed.get(el);
    if (!saved) { saved = new Map(); healed.set(el, saved); }
    for (const [prop, neutral] of CONTAINING_PROPS) {
      if (!saved.has(prop)) saved.set(prop, [el.style.getPropertyValue(prop), el.style.getPropertyPriority(prop)]);
      el.style.setProperty(prop, neutral, "important");
    }
    fixed = true;
  }
  if (fixed) console.warn("[browser] 停泊网页被祖先包含块带离视口，已临时解除（截图 / 操作恢复正常）");
  return fixed;
}

/** 由 webview 元素找到其停泊容器并修正 */
export function healParkedFrame(frame: Element | null | undefined): boolean {
  const wrapper = frame?.closest?.(".browser-tab-frame-wrapper") as HTMLElement | null | undefined;
  return healParkedWrapper(wrapper);
}

/** 祖先内已没有停泊网页时还原它被覆盖的样式（让面板 / 标签入场动画照常播放） */
export function releaseParkHeal(): void {
  for (const [el, saved] of Array.from(healed)) {
    if (el.isConnected && el.querySelector(".browser-tab-frame-wrapper.is-parked")) continue;
    for (const [prop, [value, priority]] of saved) {
      if (value) el.style.setProperty(prop, value, priority);
      else el.style.removeProperty(prop);
    }
    healed.delete(el);
  }
}
