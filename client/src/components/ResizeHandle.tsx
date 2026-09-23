/**
 * 可拖拽调整宽度的面板分隔条 (v3 — 零 React 重渲染方案)
 *
 * 性能策略:
 * - 拖拽中: 直接操作 DOM element.style.width, 不触发任何 React state 更新
 * - 拖拽结束: 一次性 commit 最终宽度到 store
 *
 * 这样每帧只有一次 style 写入, 没有 React reconciliation, 没有 localStorage I/O。
 */
import { useCallback, useRef } from "react";
import { acquire, release } from "../perf/bus";
import { runReconcile } from "../perf/reconcile";
import { useUiStore, type PanelDragLayout } from "../store/ui";

interface Props {
  side: "left" | "right";
  /** 拖拽开始时的基准宽度(px) */
  baseWidth: number;
  /** 最小宽度(px) */
  minWidth: number;
  /** 最大宽度(px) */
  maxWidth: number;
  /** plan-95: 拖拽方向上其余区域需保留的最小空间(px)，动态上限 = 容器宽 - reservePx */
  reservePx?: number;
  /** 拖拽过程中直接操作的 DOM 元素 ref */
  panelEl: React.RefObject<HTMLElement | null>;
  /** 拖拽结束,提交最终宽度 */
  onCommit: (width: number) => void;
  /** 拖拽期内容冻结（plan-329-1647 S4 收窄语义）。
   *
   *  传入需要**钉住宽度**的面板容器 ref（内部取其 firstElementChild = 内容根）：
   *  拖开始时把内容根 inline width 钉为当前值，拖拽期间面板每帧变宽/变窄都不再传入
   *  该内容树，松手时解冻并按终宽一次性重排。
   *
   *  ⚠ 现状只用于**右面板内容根**（Monaco / xterm 每帧 layout 代价极高）。
   *  消息列不再冻结：用户要求「拖拽时消息流排版实时更新」——中列宽度实时跟随、
   *  文本逐帧折行；其重排成本由 PerfBus 零读预算与 ResizeHandle 的帧闸门（RFL-5）兜住。
   *
   *  历史收益记录（冻结方案本来的动机，保留备查）：tmp_scroll_repro/ab_drag.mjs，
   *  220 条重内容下每帧强制布局读 2.37ms → 1.53ms。 */
  freezeRefs?: Array<React.RefObject<HTMLElement | null>>;
}

/** 内容根跟随记录：el=内容根，prev=拖拽前的 inline width，offset=面板宽 − 内容根宽。 */
export interface PaneFollowEntry {
  el: HTMLElement;
  prev: string;
  offset: number;
}

/** 钉住内容根宽度（拖拽起点调用），并记录 offset 供拖拽期跟随换算。
 *
 *  本轮修复「拖拉右侧面板宽度时内容不实时自适应，停下才刷新」：
 *  旧实现只钉住宽度、拖拽期完全不更新，内容要等松手解冻才重排。
 *  现在配合 followPaneContents：拖动过程中按帧闸门节拍把内容根宽度同步到当前面板宽，
 *  即「跟随」——默认 realtime 档每帧跟随（所见即所得），降档后按节拍跟随（不会停住）。 */
export function freezePaneContents(refs: Array<React.RefObject<HTMLElement | null>>): PaneFollowEntry[] {
  const list: PaneFollowEntry[] = [];
  for (const ref of refs) {
    const pane = ref.current;
    const el = pane?.firstElementChild as HTMLElement | null;
    if (!pane || !el) continue;
    const w = Math.round(el.getBoundingClientRect().width);
    const paneW = Math.round(pane.getBoundingClientRect().width);
    list.push({ el, prev: el.style.width, offset: Math.max(0, paneW - w) });
    el.style.width = w + "px";
  }
  return list;
}

/** 拖拽期把内容根宽度同步到当前面板宽（按 offset 反推）。
 *  必须与 applyWidth 同拍调用：面板宽与内容宽同帧一致，不会出现内容留白 / 裁切。 */
export function followPaneContents(list: PaneFollowEntry[], panelWidth: number) {
  for (const it of list) {
    const next = Math.max(0, Math.round(panelWidth - it.offset)) + "px";
    if (it.el.style.width !== next) it.el.style.width = next;
  }
}

export function unfreezePaneContents(list: PaneFollowEntry[]) {
  for (const it of list) it.el.style.width = it.prev;
}

export function ResizeHandle({ side, baseWidth, minWidth, maxWidth, reservePx = 0, panelEl, onCommit, freezeRefs }: Props) {
  const startXRef = useRef(0);
  const baseWRef = useRef(baseWidth);
  const draggingRef = useRef(false);
  const handleRef = useRef<HTMLDivElement>(null);
  // plan-95: 动态实际上限——静态 maxWidth 不考虑窗口可用空间，窄窗口下拖到
  // "拖不动"后 DOM 宽度仍超布局，溢出部分被裁剪（面板右缘图标不可见）。
  // 上限取 min(maxWidth, 容器宽 - reservePx)，到达上限后宽度与视觉完全静止。
  const effectiveMaxRef = useRef(maxWidth);

  /** 手柄矩形缓存（拖拽开始时取一次）。
   *
   *  为何必须缓存：拖拽中每次 mousemove 都调 getBoundingClientRect 会**强制同步布局**
   *  （浏览器必须先把此前写入的 width 脏布局全部重算才能给出准确矩形）。
   *  面板宽度写入 + 读取交替，每次 mousemove 都多付一次全量重排——
   *  这正是"拖左右侧面板时（尤其会话运行时）非常卡顿"的直接来源之一。
   *  手柄在拖拽期间不会移动（只有面板宽变），矩形完全可以复用。 */
  const rectRef = useRef<{ top: number; height: number } | null>(null);
  // rAF 句柄：一帧内多次 mousemove 只写一次宽度（高刷鼠标/慢拖时事件可达 200+Hz）
  const rafRef = useRef(0);
  const pendingWRef = useRef(0);
  // 手柄 Y 也并入同一帧：直接在 mousemove 里写 --handle-y 会触发 style recalc，
  // 高刷鼠标下一帧多个事件就多次 recalc；与宽度写入合并到每帧一次。
  const pendingYRef = useRef<number | null>(null);

  // ── RFL-5 帧闸门（plan-329-1647 S6，本轮重写判据）──
  // 中列不再冻结（RFL-1）后，实时排版的主要成本变成“浏览器文本折行”，它随可见内容量增长。
  // 为同时守住「实时排版」与「绝不卡死」，按实测帧间隔做三档自适应：
  //   realtime —— 每帧写宽度 + 跟随（默认；用户要的实时折行）
  //   balanced —— 宽度隔帧写一次（把重排预算摊到两帧上）
  //   frozen   —— 最低保证频率：约 15fps（66ms 一拍）继续写宽度与跟随。
  //               （旧行为是“暂停写入”，面板会停住不动 → 用户感知成卡死；
  //                本轮改为降频而不停止：交互断裂感远大于掉帧感。）
  // 判据改用滑动窗口长帧比例 + 可升档（见 gateAllowWrite）；
  // 档位写入 documentElement.dataset.panelDragMode，便于观测与自动化验证。
  const dragModeRef = useRef<PanelDragLayout>("realtime");
  const autoDegradeRef = useRef(true);
  const prevFrameTsRef = useRef(0);
  const minDeltaRef = useRef(Number.POSITIVE_INFINITY);
  const skipFrameRef = useRef(false);
  // 最近 12 帧的帧间隔滑动窗口（升降档判据）+ 降档冷却 + 升档连好计数 + frozen 档节拍计时
  const frameSamplesRef = useRef<number[]>([]);
  const degradeCooldownRef = useRef(0);
  const goodStreakRef = useRef(0);
  const lastFrozenWriteRef = useRef(0);

  /** 读取设置里的拖拽排版档位（含 reduced-motion 降档）。 */
  const readDragLayoutPref = useCallback((): { layout: PanelDragLayout; autoDegrade: boolean } => {
    let layout: PanelDragLayout = "realtime";
    let autoDegrade = true;
    try {
      const p = useUiStore.getState();
      layout = p.panelDragLayout ?? "realtime";
      autoDegrade = p.panelDragAutoDegrade !== false;
      const reduceMotion = p.motionLevel === "reduced" || p.motionLevel === "off"
        || (typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
      if (reduceMotion && layout === "realtime") layout = "balanced";
    } catch { /* 读不到偏好时用默认档 */ }
    return { layout, autoDegrade };
  }, []);

  /** 本帧是否允许写宽度；同时按实测帧间隔推进档位（见上方三档说明）。
   *
   *  本轮两处修复：
   *   ① frozen 档不再是「停止写宽度」（旧行为会让面板停在原地，用户感知成"卡死"），
   *      改为**最低保证频率**：约 15fps（66ms 一拍）——宁可降频也不能停止反馈；
   *   ② 判据由「连续超预算帧数」改为「最近 12 帧的长帧比例」，并补上**升档**：
   *      旧实现只降不升，一次瞬时尖峰（GC / markdown 解析）会让整段拖拽保持降级。 */
  const gateAllowWrite = useCallback((): boolean => {
    const now = performance.now();
    const prev = prevFrameTsRef.current;
    prevFrameTsRef.current = now;
    if (!prev) {
      // 本次拖拽首帧：必须放行（否则"按下不动"）；同时重置 frozen 节拍计时
      lastFrozenWriteRef.current = now;
      return true;
    }
    const delta = now - prev;
    // 自适应刷新间隔：取本次拖拽观察到的最小帧间隔（≈显示器刷新间隔）
    if (delta < minDeltaRef.current) minDeltaRef.current = delta;
    const budget = Math.max(24, minDeltaRef.current * 1.6);
    const samples = frameSamplesRef.current;
    samples.push(delta);
    if (samples.length > 12) samples.shift();

    if (autoDegradeRef.current && samples.length >= 8) {
      const longRatio = samples.filter((d) => d > budget).length / samples.length;
      if (degradeCooldownRef.current > 0) {
        degradeCooldownRef.current -= 1;
      } else if (longRatio > 0.5 && dragModeRef.current !== "frozen") {
        dragModeRef.current = dragModeRef.current === "realtime" ? "balanced" : "frozen";
        degradeCooldownRef.current = 24; // 降档冷却：避免连续跳档
        goodStreakRef.current = 0;
        samples.length = 0;
        document.documentElement.dataset.panelDragMode = dragModeRef.current;
      } else if (longRatio < 0.25 && dragModeRef.current !== "realtime") {
        goodStreakRef.current += 1;
        if (goodStreakRef.current >= 24) {
          dragModeRef.current = dragModeRef.current === "frozen" ? "balanced" : "realtime";
          goodStreakRef.current = 0;
          samples.length = 0;
          document.documentElement.dataset.panelDragMode = dragModeRef.current;
        }
      } else {
        goodStreakRef.current = 0;
      }
    }

    if (dragModeRef.current === "frozen") {
      if (now - lastFrozenWriteRef.current < 66) return false;
      lastFrozenWriteRef.current = now;
      return true;
    }
    if (dragModeRef.current === "balanced") {
      skipFrameRef.current = !skipFrameRef.current;
      return !skipFrameRef.current;
    }
    return true;
  }, []);
  // 内容根跟随记录（拖拽起点建立；松手恢复原 inline width。正常情况下内容根无 inline width）
  const frozenRef = useRef<PaneFollowEntry[]>([]);

  /** 面板展开/折叠与拖分隔条共用：把容器内容根钉成当前像素宽（并记录 offset，供拖拽期跟随）。 */
  const freezeContents = useCallback(() => {
    if (!freezeRefs || freezeRefs.length === 0) return;
    frozenRef.current = freezePaneContents(freezeRefs);
  }, [freezeRefs]);

  /** 内容根跟随（本轮修复"拖拽时内容不实时自适应，停下才刷新"）：
   *  与 applyWidth 同拍调用，保证「面板宽 → 内容根宽」同帧一致（不出现留白/裁切）。
   *  跟随频率由帧闸门决定：realtime 每帧、balanced 隔帧、frozen 约 15fps 一拍。 */
  const followContents = useCallback((panelWidth: number) => {
    followPaneContents(frozenRef.current, panelWidth);
  }, []);

  /** 拖结束：恢复原 inline width。必须在 applyWidth（面板已到终宽）**之后**调用，
   *  解冻即按面板终宽一次性重排，与随后的 onCommit 合并到同一帧。 */
  const unfreezeContents = useCallback(() => {
    unfreezePaneContents(frozenRef.current);
    frozenRef.current = [];
  }, []);

  // plan-246-1236 S6: 把鼠标相对手柄的 Y 写成 CSS 变量，供渐隐细线定位（不走 React state）。
  // 用缓存的矩形计算，不再每次调用 getBoundingClientRect（避免强制同步布局）。
  const setHandleY = useCallback((clientY: number) => {
    const el = handleRef.current;
    if (!el) return;
    const r = rectRef.current;
    const y = r ? Math.max(0, Math.min(r.height, clientY - r.top)) : clientY;
    el.style.setProperty("--handle-y", `${y}px`);
  }, []);

  const clampMax = useCallback(() => {
    const parent = panelEl.current?.parentElement;
    const avail = parent ? parent.clientWidth - reservePx : Number.POSITIVE_INFINITY;
    effectiveMaxRef.current = Math.max(minWidth, Math.min(maxWidth, avail));
  }, [maxWidth, minWidth, panelEl, reservePx]);

  const applyWidth = useCallback((w: number) => {
    const el = panelEl.current;
    if (!el) return;
    el.style.width = w + "px";
    el.style.flexBasis = w + "px";
  }, [panelEl]);

  const handleMouseMove = useCallback((e: MouseEvent) => {
    if (!draggingRef.current) return;
    e.preventDefault();
    pendingYRef.current = e.clientY; // 与宽度合并，rAF 内统一写入（见 pendingYRef 声明处）

    const rawDelta = e.clientX - startXRef.current;
    const effective = side === "right" ? -rawDelta : rawDelta;
    const newWidth = Math.round(Math.max(minWidth, Math.min(effectiveMaxRef.current, baseWRef.current + effective)));

    // 直接操作 DOM — 零 React 重渲染。
    // 再用 rAF 合并：本帧内后续事件只更新目标值，实际写入每帧至多一次，
    // 减少"写宽度 → 重排"的频率（高刷鼠标下一次拖拽可省掉一半以上写入）。
    pendingWRef.current = newWidth;
    if (rafRef.current) return;
    rafRef.current = requestAnimationFrame(() => {
      rafRef.current = 0;
      if (!draggingRef.current) return; // 帧间已结束拖拽：不再写
      if (pendingYRef.current != null) { setHandleY(pendingYRef.current); pendingYRef.current = null; }
      // RFL-5 帧闸门：balanced 档隔帧写、frozen 档按约 15fps 节拍写（见 gateAllowWrite）
      if (!gateAllowWrite()) return;
      applyWidth(pendingWRef.current);
      // 内容根与面板宽同拍跟随（本轮修复：此前拖拽期内容被完全钉住，松手才刷新）
      followContents(pendingWRef.current);
    });
  }, [side, minWidth, applyWidth, setHandleY, gateAllowWrite, followContents]);

  const handleMouseUp = useCallback(() => {
    if (!draggingRef.current) return;
    draggingRef.current = false;
    handleRef.current?.classList.remove("is-dragging");

    // 取消未执行的那一帧，并**用最终目标值同步落一次**：
    // 否则最后一次移动可能停在上一帧的宽度上，提交值与视觉不一致。
    if (rafRef.current) { cancelAnimationFrame(rafRef.current); rafRef.current = 0; }
    pendingYRef.current = null;
    applyWidth(pendingWRef.current);
    followContents(pendingWRef.current); // 松手当帧内容先跟到终宽，再解冻（避免解冻瞬间回退到旧宽）
    unfreezeContents(); // 面板已到终宽 → 解冻后内容按新宽一次性重排（见 unfreezeContents 注释）
    rectRef.current = null;

    document.removeEventListener("mousemove", handleMouseMove);
    document.removeEventListener("mouseup", handleMouseUp);
    window.removeEventListener("blur", handleMouseUp);
    window.removeEventListener("resize", handleMouseUp);
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
    document.body.classList.remove("panel-dragging");
    release("panel-drag"); // PerfBus：先摘 DOM 类再归还，避免总线读到残留状态
    // 所有运动期门控统一在类移除后收尾；必须晚于 body 类更新，监听方此时可确认静止。
    window.dispatchEvent(new CustomEvent("chatcoder:panel-drag-end"));
    // RFL-6：触发唯一收敛序列——解冻 / 虚拟器尺寸提交 / 锚点还原 / 重测 / 终端 fit /
    // 浏览器框架尺寸 / 输入框 remeasure / 流式追平，同一帧内按序执行（不再各自排队）。
    runReconcile();

    // 读取最终宽度,一次性提交（plan-95: 提交前按动态上限钳制，不持久化越界值）
    const el = panelEl.current;
    const finalWidth = Math.min(
      effectiveMaxRef.current,
      el ? parseInt(el.style.width, 10) || baseWRef.current : baseWRef.current,
    );
    onCommit(finalWidth);
  }, [handleMouseMove, panelEl, onCommit, applyWidth, unfreezeContents, followContents]);

  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    draggingRef.current = true;
    handleRef.current?.classList.add("is-dragging");
    startXRef.current = e.clientX;
    baseWRef.current = baseWidth;
    pendingWRef.current = baseWidth;
    clampMax();
    // RFL-5：本次拖拽从设置档位起步（reduced-motion 自动降一档），并重置闸门统计
    const pref = readDragLayoutPref();
    dragModeRef.current = pref.layout;
    autoDegradeRef.current = pref.autoDegrade;
    prevFrameTsRef.current = 0;
    minDeltaRef.current = Number.POSITIVE_INFINITY;
    skipFrameRef.current = false;
    frameSamplesRef.current = [];
    degradeCooldownRef.current = 0;
    goodStreakRef.current = 0;
    lastFrozenWriteRef.current = 0;
    document.documentElement.dataset.panelDragMode = pref.layout;
    freezeContents(); // 钉住内容根宽度（现仅右面板内容根，见 Props.freezeRefs 注释）
    // 拖拽开始取一次手柄矩形（此时无脏布局，成本最低），全程复用
    const r = handleRef.current?.getBoundingClientRect();
    rectRef.current = r ? { top: r.top, height: r.height } : null;
    setHandleY(e.clientY);

    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    document.body.classList.add("panel-dragging");
    acquire("panel-drag"); // PerfBus：分隔条拖拽期（S3 统一门控口径）
    // plan-31-151 S4：拖拽开始前派发事件——MessageFlow 在此时捕获滚动锚点
    //   （布局尚未被拖拽改写，锚点不被污染）。
    window.dispatchEvent(new CustomEvent("chatcoder:panel-drag-start"));

    document.addEventListener("mousemove", handleMouseMove);
    document.addEventListener("mouseup", handleMouseUp);
    // 冻结兜底：拖拽中窗口失焦（Alt+Tab）或被系统 resize（屏变/DPI/分屏）时
    // document mouseup 可能永不到达——若不收尾，内容根的 inline width 会被
    // 永久钉死，此后窗口怎么变内容都不跟随。两类事件都按"拖拽中断"处理：
    // 取消拖拽、解冻、按当前宽度收尾。
    window.addEventListener("blur", handleMouseUp);
    window.addEventListener("resize", handleMouseUp);
  }, [baseWidth, clampMax, freezeContents, handleMouseMove, handleMouseUp, setHandleY, readDragLayoutPref]);

  return (
    <div
      ref={handleRef}
      className={`resize-handle resize-handle-${side}`}
      onMouseDown={handleMouseDown}
      onMouseMove={(e) => { if (!draggingRef.current) setHandleY(e.clientY); }}
      role="separator"
      aria-orientation="vertical"
    />
  );
}
