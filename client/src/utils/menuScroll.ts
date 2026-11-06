/** plan-20261105-minimal-panels：吸顶分组会遮住视口顶部，只调整候选列表自身的滚动位置。 */
export function scrollActiveMenuOption(menu: HTMLElement | null): void {
  if (!menu) return;
  const active = menu.querySelector<HTMLElement>("button.active");
  if (!active) return;
  if (active === menu.querySelector("button[role='option']")) {
    menu.scrollTop = 0;
    return;
  }
  const heading = menu.querySelector<HTMLElement>(".mention-group-heading");
  const viewport = menu.getBoundingClientRect();
  const item = active.getBoundingClientRect();
  const top = viewport.top + menu.clientTop + (heading?.getBoundingClientRect().height ?? 0);
  const bottom = viewport.top + menu.clientTop + menu.clientHeight;
  if (item.top < top) menu.scrollTop -= top - item.top;
  else if (item.bottom > bottom) menu.scrollTop += item.bottom - bottom;
}
