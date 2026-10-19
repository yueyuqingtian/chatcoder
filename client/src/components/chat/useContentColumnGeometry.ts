/** 内容列几何的运行时编排（plan-156-619 S3 · 需求 C1；plan-20261018 抖动根治）。
 *
 * 职责边界：
 *   · **何时重算** —— 宿主宽度变化（窗口缩放 / 拖面板 / 折叠展开）与用户改内容宽度；
 *   · **谁来布局** —— 只写两个输入变量，宽度与位置由 CSS 在布局阶段实时求解；
 *   · 把结果写成 `.chat-panel` 上的 CSS 变量，供 `.mf-list` 与 `.composer` 消费。
 *
 * 【plan-20261018：为什么把宽度与位置从 JS 移到 CSS】
 *   旧实现每帧在 rAF 里把 `--content-col-w` / `--content-col-left` 写成 px。
 *   实测（真实分隔条 mousedown/mousemove 序列）一次拖拽收尾里内容列左缘出现了
 *   `421 → 173 → 297` 的跳变，幅度 248px —— 就是用户看到的「左右抖动」。原因有三：
 *     1. **滞后半帧到一帧**：RO 回调给出的宿主宽要先经 rAF 才写入，写入时宿主又变成了新宽度；
 *        左移量按 `(H−W)/2` 的 0.5 斜率变化，宿主宽每帧变 8px，位置就跟着错 4px。
 *     2. **口径不一致**：RO 的 contentRect 已扣掉 `scrollbar-gutter: stable` 预留，
 *        旧代码又用 `clearanceIn()` 减了一次 gutter，于是短fall 判定与实际布局差一个 gutter，
 *        卡片形态在临界宽度上提前降级、来回切换。
 *     3. **宿主宽度骤变时写出不自洽的中间值**：面板提交宽度 / 滚动条切换时
 *        `clientWidth` 会短暂读到 0 或旧值，那一帧写下的宽度与位置组合彼此矛盾。
 *   现在宽度 = `min(100% − 32px, max(560px, 偏好))`、位置 = `居中量 − 避让量`
 *   全部由 CSS 的 min/max/calc 表达：**浏览器在布局阶段按当前包含块一次算完**，
 *   不存在「JS 算的是上一帧」这回事，拖面板时内容列与宿主同帧变化，抖动从根上消失。
 *   本 hook 只保留两件 JS 才能做的事：写用户偏好 / 卡片形态需求，以及算 shortfall 判定形态。
 *
 * 为什么仍然用 CSS 变量而不是 React state 下发：变量只影响布局，React 不参与；
 * 形态判定带滞回，且只在「放得下 / 放不下」档位真正变化时才 setState。
 */
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type RefObject } from "react";
import { useUiStore } from "../../store/ui";
import {
  CLEARANCE_EXPANDED, contentColumnGeometry, COLUMN_PAD, MIN_CAPSULE_HOST_W, MIN_COL_W,
} from "./contentColumnGeometry";

/** 宿主宽度变化小于该值视为抖动（字体 hinting 取整），不重新求解。 */
const HOST_WIDTH_EPSILON = 0.5;

/** 形态档位滞回：已判定「放得下」后，需越界超过该像素才降级。
 *  否则临界宽度上 expanded ↔ capsule 每次拖拽都反复切换，卡片与内容列一起跳。 */
const SHORTFALL_HYSTERESIS = 12;

const UNMEASURED: ContentColumnHandle = {
  expandedShortfall: Number.POSITIVE_INFINITY,
  capsuleShortfall: Number.POSITIVE_INFINITY,
};

/** 内容宽度拖拽柄的实时预览事件（ContentWidthResizer 派发，detail.width 为 px）。 */
export const CONTENT_WIDTH_PREVIEW_EVENT = "chatcoder:content-width-preview";

/** 滚动条 gutter 的缓存时长：拖拽期每帧 getComputedStyle 会触发样式重算。 */
const GUTTER_CACHE_MS = 1000;

/** 读取滚动条 gutter 预留宽（与 `--scrollbar-gutter-w` 同源，内容列与输入框靠它对齐中轴）。
 *  CSS 侧的 `--content-col-avail` 用的也是这个变量，两边必须同源，否则卡片形态判定
 *  与实际布局差一个 gutter，临界宽度上会来回切换（plan-20261018 审查发现）。 */
function gutterOf(el: HTMLElement): number {
  const v = parseFloat(getComputedStyle(el).getPropertyValue("--scrollbar-gutter-w"));
  return Number.isFinite(v) && v > 0 ? v : 12;
}

/** 形态探针滞回：判据只看符号（>0 表示放不下），越界 12px 内维持「放得下」。 */
function settleShortfall(prev: number, next: number): number {
  const value = Math.round(next);
  if (!Number.isFinite(prev)) return value;
  if (prev > 0 && value <= 0) return 0;
  if (prev <= 0 && value > 0 && value <= SHORTFALL_HYSTERESIS) return 0;
  return value;
}

export interface ContentColumnHandle {
  /** 展开卡片在最大左移后仍缺的空间（>0 → 只能降级为胶囊）。 */
  expandedShortfall: number;
  /** 宿主比胶囊的最小可用宽度还窄多少（>0 → 胶囊会盖住半个消息列，整卡隐藏）。
   *  plan-20261019-toolcard-v2：胶囊悬浮不让位，此值只由宿主宽度决定。 */
  capsuleShortfall: number;
}

/**
 * @param hostRef 宿主要素（`.chat-panel`）；变量写到它上面，子元素只读。
 * @param clearance 当前卡片形态对右侧空间的需求（CLEARANCE_EXPANDED / CAPSULE / NONE）。
 */
export function useContentColumnGeometry(
  hostRef: RefObject<HTMLElement | null>,
  clearance = 0,
): ContentColumnHandle {
  const preferredW = useUiStore((s) => s.contentMaxWidth);

  /** 最近一次观测到的宿主内容宽（**已扣掉滚动条 gutter**，RO 的 contentRect 口径）。 */
  const hostWRef = useRef(0);
  /** 拖拽内容宽度期间的预览值（松手写回偏好后清空）。 */
  const previewWidthRef = useRef<number | null>(null);
  /** 滚动条 gutter（带过期缓存，避免拖拽期每帧 getComputedStyle）。 */
  const gutterRef = useRef({ value: 0, at: 0 });
  const clearanceRef = useRef(clearance);
  clearanceRef.current = clearance;
  /** 已写入的变量值：值未变就跳过，避免样式失效引发无谓的重排。 */
  const writtenRef = useRef<{ max: string; clearance: number }>({ max: "", clearance: Number.NaN });

  /* 尚未测量时按「放不下」处理（卡片先隐藏）：初值为 0 会让卡片首帧按「放得下」渲染，
     测量完成后再降级，表现为进入会话时卡片闪一下（plan-156-620 审查发现）。 */
  const [shortfalls, setShortfalls] = useState<ContentColumnHandle>(UNMEASURED);
  const lastShortfallsRef = useRef<ContentColumnHandle>(UNMEASURED);
  const rafRef = useRef(0);

  /** 写入布局输入 + 求解形态缺口。全程只写不读（宿主宽来自 RO），拖拽期无强制同步布局。 */
  const apply = useCallback(() => {
    const host = hostRef.current;
    if (!host) return;

    let hostW = hostWRef.current;
    if (!(hostW > 0)) {
      // 首次挂载 / 折叠恢复：RO 还没给出尺寸，读一次兜底
      hostW = host.clientWidth;
      hostWRef.current = hostW;
    }
    if (!Number.isFinite(hostW) || hostW <= 0) return;

    /* 内容列可用宽 = 宿主内宽 − 滚动条 gutter：与 CSS 的 `--content-col-avail` **同一表达式**。
       `clientWidth` / RO 的 contentRect 都不含 `.message-flow` 内部那条滚动条占位，
       而内容列的实际包含块在滚动容器里，故这里必须自己扣掉，否则形态判定会偏乐观。 */
    const nowTs = performance.now();
    if (!gutterRef.current.value || nowTs - gutterRef.current.at > GUTTER_CACHE_MS) {
      gutterRef.current = { value: gutterOf(host), at: nowTs };
    }
    const avail = Math.max(0, hostW - gutterRef.current.value);

    const wanted = previewWidthRef.current ?? preferredW;

    /* 布局输入：CSS 侧用 min/max/calc 实时求解宽度与位置（见 global.css「内容列定宽」段）。
       `100cqw` 表示不限宽——与 store 的 0 语义对齐，min() 才能保持成立。 */
    const maxToken = wanted > 0 ? `${wanted}px` : "100cqw";
    const written = writtenRef.current;
    if (written.max !== maxToken) {
      host.style.setProperty("--content-col-max", maxToken);
      written.max = maxToken;
    }
    if (written.clearance !== clearanceRef.current) {
      host.style.setProperty("--content-col-clearance", `${clearanceRef.current}px`);
      written.clearance = clearanceRef.current;
    }

    /* 形态判定的两个探针都按「CSS 求解出的宽度」计算，与当前形态无关——
       否则 shortfall → 形态 → clearance → shortfall 形成回路，卡片会在两档之间来回跳。
       只在**档位（放得下 / 放不下）真正翻转**时才 setState：数值本身随宽度连续变化，
       按数值比较会让 ChatPanel 每帧重渲染，正是拖拽发涩的来源之一。 */
    const solve = (preferred: number, need: number) => contentColumnGeometry({
      hostW: avail, preferredW: preferred, clearance: need, pad: COLUMN_PAD, minColW: MIN_COL_W,
    });
    const geo = solve(wanted, clearanceRef.current);
    const prev = lastShortfallsRef.current;
    const rawExpanded = solve(geo.width, CLEARANCE_EXPANDED).shortfall;
    // plan-20261019-toolcard-v2：胶囊悬浮、不让位，「放不下」只看宿主是否窄到胶囊会盖住半个消息列
    const rawCapsule = Math.max(0, MIN_CAPSULE_HOST_W - hostW);
    const next: ContentColumnHandle = {
      expandedShortfall: settleShortfall(prev.expandedShortfall, rawExpanded),
      capsuleShortfall: settleShortfall(prev.capsuleShortfall, rawCapsule),
    };
    if ((prev.expandedShortfall > 0) !== (next.expandedShortfall > 0)
      || (prev.capsuleShortfall > 0) !== (next.capsuleShortfall > 0)) {
      lastShortfallsRef.current = next;
      setShortfalls(next);
    }
  }, [hostRef, preferredW]);

  /** 监听器只注册一次，经 ref 调最新的 apply。 */
  const applyRef = useRef(apply);
  applyRef.current = apply;

  /** 合帧：同一帧内多次触发（RO + resize）只求解一次。 */
  const schedule = useCallback(() => {
    if (rafRef.current) return;
    rafRef.current = requestAnimationFrame(() => {
      rafRef.current = 0;
      applyRef.current();
    });
  }, []);

  // 内容宽度偏好变化 = 用户显式调整（设置页滑杆 / 拖拽柄松手），预览值随之作废
  useEffect(() => {
    previewWidthRef.current = null;
    applyRef.current();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [preferredW]);

  // 卡片形态变化：只改避让量，一次布局内完成。
  // plan-20261019-toolcard-v2：用 layout effect——与卡片形变同一帧写入变量，内容列的平移过渡
  // 才会和卡片形变同时起步（passive effect 会晚一帧，正文总是慢半拍）。
  useLayoutEffect(() => { applyRef.current(); }, [clearance]);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    let lastW = host.clientWidth;
    const ro = typeof ResizeObserver !== "undefined"
      ? new ResizeObserver((entries) => {
          // 只认宽度：高度变化（流式内容、输入框增高）与内容列几何无关。
          // RO 回调读 contentRect 不触发布局，正好避开「写变量 → 读宽度」的循环。
          const w = entries[0]?.contentRect.width;
          if (typeof w !== "number" || Math.abs(w - lastW) < HOST_WIDTH_EPSILON) return;
          lastW = w;
          hostWRef.current = w;
          schedule();
        })
      : null;
    ro?.observe(host);

    const onPreview = (e: Event) => {
      const w = Number((e as CustomEvent<{ width?: number }>).detail?.width);
      if (!Number.isFinite(w) || w <= 0) return;
      previewWidthRef.current = w;
      // 拖拽柄已在自己的 rAF 里派发：同步写入偏好变量，宽度当帧生效
      applyRef.current();
    };

    window.addEventListener("resize", schedule);
    window.addEventListener(CONTENT_WIDTH_PREVIEW_EVENT, onPreview);
    // 首次挂载先求解一次
    applyRef.current();

    return () => {
      cancelAnimationFrame(rafRef.current);
      rafRef.current = 0;
      window.removeEventListener("resize", schedule);
      window.removeEventListener(CONTENT_WIDTH_PREVIEW_EVENT, onPreview);
      ro?.disconnect();
    };
  }, [hostRef, schedule]);

  return shortfalls;
}
