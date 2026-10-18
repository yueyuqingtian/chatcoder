/** 工具卡形态判定（plan-156-619 S7b）。
 *
 * 三档形态，由宿主（`.chat-panel`）宽度决定：
 *   · expanded：≥900px，可展开卡片（是否真的展开由会话记忆决定）
 *   · capsule ：640~900px，强制折叠为右上角胶囊（只显示优先级最高的一项）
 *   · hidden  ：<640px，不渲染（数据仍在右面板，不挤占消息流）
 *
 * 另外，即使宿主够宽，若「左移已到左边界仍让不出卡片占位」，也降一级——
 * 这件事由 `useContentColumnGeometry` 的 `shortfall` 反馈，避免卡片把内容列压窄。
 * 两者取更保守的一档（详见 pickForm）。
 *
 * 阈值来自设计约定（plan-156-619 D10），不是随手填的魔数：
 * 卡片 320 + 内容列理想宽 560 + 两侧内距 → 900 才放得下。
 */
import { useEffect, useRef, useState } from "react";
import { isBusy } from "../../../perf/bus";

/** 宿主宽度 ≥ 该值才允许展开卡片。 */
export const CARD_MIN_HOST_W = 900;
/** 宿主宽度 < 该值时连胶囊也不显示。 */
export const CAPSULE_MIN_HOST_W = 640;
/** 尺寸抖动过滤阈值（px）：小于它视为滚动条/取整抖动，不重算形态。 */
const HOST_EPSILON = 8;

export type ToolCardForm = "expanded" | "capsule" | "hidden";

/** 纯函数：按宿主宽度与左移缺口判定形态（便于单测与推理）。 */
export function pickForm(hostW: number, shortfall: number, wantExpanded: boolean): ToolCardForm {
  if (hostW < CAPSULE_MIN_HOST_W) return "hidden";
  // 让不出卡片占位 → 直接降为胶囊（保内容列宽度，绝不压窄消息流）
  if (hostW < CARD_MIN_HOST_W || shortfall > 0) return "capsule";
  return wantExpanded ? "expanded" : "capsule";
}

export function useToolCardFit(hostRef: React.RefObject<HTMLElement | null>, shortfall: number): number {
  const [hostW, setHostW] = useState(0);
  const lastRef = useRef(0);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const read = () => {
      const w = host.clientWidth;
      if (!Number.isFinite(w) || w <= 0) return;
      if (Math.abs(w - lastRef.current) <= HOST_EPSILON) return;
      lastRef.current = w;
      setHostW(w);
    };
    read();
    const ro = typeof ResizeObserver !== "undefined" ? new ResizeObserver(read) : null;
    ro?.observe(host);
    const onResize = () => read();
    window.addEventListener("resize", onResize);
    return () => {
      ro?.disconnect();
      window.removeEventListener("resize", onResize);
    };
  }, [hostRef]);

  // shortfall 只影响形态判定，不参与尺寸测量：由调用方每次渲染把它传进 pickForm
  void shortfall;
  return hostW;
}

/** 面板自身是否处于几何运动期（拖分隔条 / 面板过渡）——运动期卡片暂不切换形态，
 *  避免拖拽过程中卡片在「展开/胶囊」之间来回跳。 */
export function isPaneMoving(): boolean {
  return isBusy();
}
