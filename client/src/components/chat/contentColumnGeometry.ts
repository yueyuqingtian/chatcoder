/** 内容列几何求解（plan-156-619 S3 · 需求 C1）。
 *
 * 【为什么需要这层】
 * 此前内容列宽度是 `max-width: var(--content-max-w) + margin: 0 auto`——
 * 宽度由 CSS 百分比推导，任何「可用空间变化」都会连带改变宽度。
 * 于是右上角工具卡一出现/一收起，长回答就整体重排、阅读位置丢失。
 *
 * 现在把宽度拆成两件事：
 *   · **width（冻结值）**：只在「窗口缩放 / 拖面板 / 折叠展开面板 / 用户改内容宽度」
 *     四类事件时重算（见 useContentColumnGeometry 的几何纪元）；
 *   · **left（位置）**：每次输入变化都重算，卡片出现只改它。
 *
 * 本文件只有纯函数，不碰 DOM、不读 store —— 便于单测，也避免几何逻辑散落到组件里。
 */

/** 内容列与宿主左右边缘的最小内距（px）。左移时不得突破，否则贴边视觉异常。 */
export const COLUMN_PAD = 16;

/** 用户可设置的宽度下限（与 ContentWidthResizer.MIN_W 同源，两处必须一致）。 */
export const MIN_COL_W = 560;

/** 工具卡展开态占位 = 卡片宽 320 + 与内容列的视觉间隙 16。 */
export const CARD_RESERVE_EXPANDED = 336;

/** 卡片折叠为胶囊 / 隐藏时都不占列宽（胶囊浮在右上角空白区）。 */
export const CARD_RESERVE_NONE = 0;

export interface ColumnGeoInput {
  /** 内容列可用宽：`.app-main` 内宽 − 滚动条 gutter。 */
  hostW: number;
  /** 外观设置「内容展示宽度」；0 或负数表示不限（尽量宽）。 */
  preferredW: number;
  /** 卡片右侧占位（CARD_RESERVE_EXPANDED / CARD_RESERVE_NONE）。 */
  cardReserve: number;
  /** 最小内距，默认 COLUMN_PAD。 */
  pad?: number;
  /** 用户偏好下限，默认 MIN_COL_W。 */
  minColW?: number;
}

export interface ColumnGeo {
  /** 内容列宽度（px）——冻结语义：调用方只在几何纪元变化时采用新值。 */
  width: number;
  /** 内容列左边距（px）——卡片变化只影响它。 */
  left: number;
  /** 卡片要不到的空间（>0 表示左移已到边界，应降级卡片形态而不是压窄内容列）。 */
  shortfall: number;
}

const clampNonNegative = (n: number): number => (Number.isFinite(n) && n > 0 ? n : 0);

/**
 * 求解一次内容列几何。
 *
 * 宽度优先级：**物理可用宽 > 用户偏好 > 理想下限**。
 * 也就是说窗口窄到连 minColW 都放不下时，宽度取物理可用宽而不是死撑 minColW——
 * 定宽列一旦超出容器，`.message-flow` 的 `overflow-x: hidden` 会直接裁掉文字，
 * 这比「列比偏好窄一点」严重得多。
 */
export function contentColumnGeometry(input: ColumnGeoInput): ColumnGeo {
  const pad = input.pad ?? COLUMN_PAD;
  const minColW = input.minColW ?? MIN_COL_W;
  const hostW = clampNonNegative(input.hostW);
  const cardReserve = clampNonNegative(input.cardReserve);

  // 左右各留 pad 之后真正能用的宽度
  const avail = Math.max(0, hostW - pad * 2);
  const wanted = input.preferredW > 0 ? input.preferredW : avail;
  // 先满足「不低于理想下限」，再被物理可用宽封顶
  const width = Math.min(avail, Math.max(minColW, wanted));

  // 原本的居中位置（宿主更窄时退化为 0，不会出现负边距）
  const idealLeft = Math.max(0, (hostW - width) / 2);
  // 能左移的上限：不能突破左内距
  const maxShift = Math.max(0, idealLeft - pad);
  const shift = Math.min(cardReserve, maxShift);

  return {
    width,
    left: idealLeft - shift,
    shortfall: Math.max(0, cardReserve - maxShift),
  };
}

/** 内容列右边缘的右内距——用于判定卡片是否真的放得下（与 shortfall 同口径的辅助检查）。 */
export function rightGapOf(geo: ColumnGeo, hostW: number): number {
  return Math.max(0, hostW - (geo.left + geo.width));
}
