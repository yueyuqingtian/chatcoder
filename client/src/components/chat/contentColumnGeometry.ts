/** 内容列几何求解（plan-156-619 S3 · 需求 C1；plan-20261018 起只用于形态判定）。
 *
 * 【为什么需要这层】
 * `--content-col-w` / `--content-col-left` 已改由 CSS 在布局阶段实时求解（见 global.css
 * 「内容列定宽」段与 useContentColumnGeometry 的说明）——拖面板时内容列与宿主同帧变化，
 * 不再有「JS 写的是上一帧」造成的左右抖动。
 * 本文件因此只剩一件事：**算出卡片形态还差多少空间**（shortfall），决定展开 / 胶囊 / 隐藏。
 *
 * 【按需左移】
 * 内容列只在右缘真的会被卡片压住时才左移，且只移需要的那一段：
 * 右侧本来放得下就一点不动。短fall 的判据与 CSS 的 `--content-col-shift` 公式保持同源，
 * 两处改一处必须同步另一处（数值来自 COLUMN_PAD / CARD_W / CARD_INSET / CARD_GAP 令牌）。
 *
 * 本文件只有纯函数，不碰 DOM、不读 store —— 便于推理，也避免几何逻辑散落到组件里。
 */

/** 内容列与宿主左右边缘的最小内距（px）。左移时不得突破，否则贴边视觉异常。 */
export const COLUMN_PAD = 16;

/** 用户可设置的宽度下限（与 ContentWidthResizer.MIN_W 同源，两处必须一致）。 */
export const MIN_COL_W = 560;

/** 工具卡展开态宽度（与 toolcard.css 的 --tcard-w 同源）。 */
export const CARD_W = 280;
/** 胶囊最大宽度（与 toolcard.css 的 --tcard-cap-max-w 同源）。 */
export const CAPSULE_MAX_W = 260;
/** 卡片 / 胶囊距宿主右缘的距离（与 toolcard.css 的 --tcard-inset 同源）。 */
export const CARD_INSET = 14;
/** 内容列右缘与卡片左缘之间至少保留的间隙。 */
export const CARD_GAP = 16;

/** 卡片形态对右侧空间的需求（px，按宿主「含滚动条 gutter 的外宽」计）。 */
export const CLEARANCE_EXPANDED = CARD_W + CARD_INSET + CARD_GAP;
export const CLEARANCE_CAPSULE = CAPSULE_MAX_W + CARD_INSET + CARD_GAP;
export const CLEARANCE_NONE = 0;

export interface ColumnGeoInput {
  /** 内容列可用宽：宿主内宽（**已扣滚动条 gutter**，与 RO 的 contentRect 同口径）。 */
  hostW: number;
  /** 外观设置「内容展示宽度」；0 或负数表示不限（尽量宽）。 */
  preferredW: number;
  /** 右侧需要让出的空间（与宿主同一口径，不要再扣 gutter）。 */
  clearance: number;
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
  /** 左移到边界后仍让不出的空间（>0 表示该形态放不下，应降级卡片而不是压窄内容列）。 */
  shortfall: number;
}

const clampNonNegative = (n: number): number => (Number.isFinite(n) && n > 0 ? n : 0);

/**
 * 求解一次内容列几何。
 *
 * 宽度优先级：**物理可用宽 > 用户偏好 > 理想下限**。
 * 窗口窄到连 minColW 都放不下时宽度取物理可用宽，而不是死撑 minColW——
 * 定宽列一旦超出容器，`.message-flow` 的 `overflow-x: hidden` 会直接裁掉文字。
 */
export function contentColumnGeometry(input: ColumnGeoInput): ColumnGeo {
  const pad = input.pad ?? COLUMN_PAD;
  const minColW = input.minColW ?? MIN_COL_W;
  const hostW = clampNonNegative(input.hostW);
  const clearance = clampNonNegative(input.clearance);

  // 左右各留 pad 之后真正能用的宽度
  const avail = Math.max(0, hostW - pad * 2);
  const wanted = input.preferredW > 0 ? input.preferredW : avail;
  // 先满足「不低于理想下限」，再被物理可用宽封顶
  const width = Math.min(avail, Math.max(minColW, wanted));

  // 原本的居中位置（宿主更窄时退化为 0，不会出现负边距）
  const idealLeft = Math.max(0, (hostW - width) / 2);
  // 只移「右缘侵入卡片区域」的那一段：右侧本来放得下就不动
  const intrusion = clearance > 0 ? Math.max(0, idealLeft + width - (hostW - clearance)) : 0;
  // 能左移的上限：不能突破左内距
  const maxShift = Math.max(0, idealLeft - pad);
  const shift = Math.min(intrusion, maxShift);

  return {
    width,
    left: idealLeft - shift,
    shortfall: Math.max(0, intrusion - shift),
  };
}
