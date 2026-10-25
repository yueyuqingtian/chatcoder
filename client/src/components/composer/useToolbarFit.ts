/** 输入框工具条的收窄档位：观察工具条自身宽度，在 data-fit 上写入已触发的收起项（空格分隔）。
 *
 *  收起顺序（从最不重要到最重要）：
 *  模型名限宽 → 思考深度档位文字 → 执行模式文字 → 权限文字与箭头 → 紧凑内边距（模型名再限宽）
 *  → 执行模式整枚、上下文圆环、模型触发器箭头（ultra）。
 *  plan-20261025-composer：模型与思考深度合并为无图标的文字触发器，模型名只能逐档限宽省略、不能整段隐藏。
 *
 *  ultra 档为什么可以整枚收起执行模式：它打开的就是「+」菜单（PlusMenu 内含模式切换），
 *  且 Shift+Tab 可切换——最小宽度下把入口让给附件与发送，功能没有丢失。
 *
 *  收起后名称与当前值由悬停提示与 aria-label 承接。
 *
 *  为什么不用容器查询：原先的 `@container main-pane` 只在旧任务卡存在时才建立容器（该状态已不再出现），
 *  规则从不命中，窄面板下右侧按钮会压到左侧文字上；而给工具条加 container-type 会让它成为层叠上下文，
 *  把内部 z-index:1100 的浮层困住，被上方 z-index:6 的任务胶囊盖住。直接观察宽度没有这两个副作用。
 *
 *  阈值是「工具条内容宽度」，按中文文案留了余量；实测右面板 280px（最小宽度）时工具条只有 209px，
 *  全部收起后左右两组刚好填满——因此阈值比早期版本更保守，并多留一档 ultra 兜底。 */
import { useLayoutEffect } from "react";

const STEPS: ReadonlyArray<readonly [string, number]> = [
  ["model", 560],
  ["effort", 500],
  ["mode", 460],
  ["approval", 420],
  ["tight", 360],
  ["ultra", 300],
];

export function useToolbarFit(el: HTMLElement | null) {
  useLayoutEffect(() => {
    if (!el) return;
    const apply = (width: number) => {
      const next = STEPS.filter(([, max]) => width <= max).map(([key]) => key).join(" ");
      if (el.dataset.fit !== next) el.dataset.fit = next;
    };
    apply(el.clientWidth);
    const ro = new ResizeObserver((entries) => apply(entries[0].contentRect.width));
    ro.observe(el);
    return () => ro.disconnect();
  }, [el]);
}
