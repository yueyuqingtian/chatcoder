/** plan-20261105-minimal-panels：覆盖首尾循环、吸顶遮挡与非引用菜单，滚动不影响祖先。 */
import assert from "node:assert/strict";
import { scrollActiveMenuOption } from "../src/utils/menuScroll";

function fixture({ scroll = 200, top = 100, bottom = 138, first = false, heading = 23, options = true } = {}) {
  const active = { getBoundingClientRect: () => ({ top, bottom }) };
  const menu = {
    scrollTop: scroll, clientTop: 0, clientHeight: 180,
    getBoundingClientRect: () => ({ top: 100, bottom: 280 }),
    querySelector: (selector: string) => selector === "button.active" ? active
      : selector === "button[role='option']" ? options ? first ? active : {} : null
      : heading ? { getBoundingClientRect: () => ({ height: heading }) } : null,
  };
  scrollActiveMenuOption(menu as unknown as HTMLElement);
  return menu.scrollTop;
}

assert.equal(fixture({ scroll: 700, top: -600, first: true }), 0, "末项循环到首项应恢复顶部与分组标题");
assert.equal(fixture({ scroll: 0, top: 750, bottom: 788 }), 508, "首项向上循环到末项应完整露出末项");
assert.equal(fixture({ top: 106, bottom: 144 }), 183, "视口内但被吸顶标题遮挡时仍需上滚");
assert.equal(fixture({ top: 116, bottom: 154, heading: 28 }), 188, "跨分组时应按实际标题高度预留空间");
assert.equal(fixture({ top: 135, bottom: 173 }), 200, "已完整可见的项不滚动");
assert.equal(fixture({ top: 90, bottom: 128, heading: 0, options: false }), 190, "旧技能菜单不应被当作首项强制归零");
assert.equal(fixture({ top: 250, bottom: 288 }), 208, "底部部分可见时完整露出");
scrollActiveMenuOption(null);
scrollActiveMenuOption({ querySelector: () => null } as unknown as HTMLElement);
console.log("通过：浮层首尾循环、吸顶遮挡、跨分组与普通菜单滚动 9 项回归");
