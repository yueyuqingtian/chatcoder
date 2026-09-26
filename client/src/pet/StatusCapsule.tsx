/** 浮窗任务块（plan-73-341 复刻参考图 / plan-73-342 补拖拽动画）。
 *
 * 结构（两行，对齐参考图）：
 *   第一行　会话标题（白、14px、单行省略） + 右侧角标区
 *           角标区：等待处理 → 绿色问号（点击回到会话）；
 *                   常规执行 → 两个 28×28 方块按钮：回到会话 / 停止运行
 *   第二行　六点抓取手柄（提示可拖动调序） + 活动图标 + 固定动词 + 滚动内容
 *
 * 滚动内容用 `LiveTicker`（与主窗消息流的思考块同构）：单行视口内横向追逐行尾、
 * 换行时上滚切行 —— 因此**思考与消息的完整文本不会被省略**。
 *
 * 拖拽调序（plan-73-342）：此前拖拽时块完全不动，只在松手瞬间换位，观感是"瞬间跳变"。
 * 现补三层动画：
 *   ① **跟手**：被拖块 `translateY` 实时跟随指针（无过渡，保证跟手）；
 *   ② **让位**：其余块按被拖块跨越的格数反向平移，带过渡（拖到哪、谁让位一眼可见）；
 *   ③ **落位**：松手用 FLIP——先记录各块视觉位置，顺序变更后补偿回原位再过渡到新位置，
 *      因此不会出现"跳一下"。
 *
 * 为什么需要本地乐观顺序：顺序若等主进程广播回来才更新，松手瞬间块会先弹回旧位置，
 * 等回包再跳走（中间闪一下）。这里松手即用本地顺序渲染，动画与顺序同帧生效。
 */
import { useCallback, useLayoutEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { IconActivity, IconAsk, IconGoSession, IconGrip, IconStopRun, type ActivityKind } from "./PetIcons";
import { LiveTicker } from "./LiveTicker";
import { parseArgsPreview, toolDisplay } from "./petToolDisplay";
import type { LiveItem } from "./useLiveActivity";
import type { TaskCard } from "./useTaskCards";

/** 拖拽调序的启动阈值（小于该位移视为误触） */
const REORDER_THRESHOLD = 6;
/** 让位 / 落位动画时长（与 CSS 的 --pet-ease 搭配） */
const REORDER_MS = 200;

/** 第二行的内容：动词为固定前缀，目标进滚动区 */
interface Line {
  kind: ActivityKind;
  /** 固定前缀（不滚动，保持稳定可读） */
  verb: string;
  /** 滚动内容（可含换行；空则不滚动） */
  target: string;
}

/** 拖拽中的实时状态：跟手位移 + 跨越格数 */
interface DragState {
  id: number;
  /** 原始索引（column-reverse 下索引 0 在最下方） */
  index: number;
  /** 跟手位移（已按可移动范围钳制，负 = 向上） */
  dy: number;
  /** 跨越格数（正 = 向上、索引增大） */
  slots: number;
  /** 相邻块中心距（按下时实测，避免硬编码与 CSS 脱节） */
  step: number;
}

interface Props {
  cards: TaskCard[];
  /** 主任务的实时活动（仅主任务块使用；其他块回退到步骤/工具摘要） */
  live: LiveItem[];
  primaryId: number | null;
  recent: { sessionId: number; title: string; at: number }[];
  maxBlocks: number;
  /** 是否已展开（浮窗被聚焦时展开到 maxBlocks 块） */
  expanded: boolean;
  onFocusSession: (sessionId: number) => void;
  onStop: (card: TaskCard) => void;
  /** 拖拽调序提交：新的会话顺序（索引 0 = 最外层/最靠近宠物） */
  onReorder: (ids: number[]) => void;
  /** 拖拽调序进行中（上层据此冻结块数，避免拖拽中块区收缩） */
  onDragActive: (active: boolean) => void;
}

/** 把一条实时活动转成展示行 */
function liveToLine(item: LiveItem): Line {
  if (item.kind === "thinking") return { kind: "thinking", verb: "思考中", target: item.text };
  if (item.kind === "message") return { kind: "message", verb: "回复中", target: item.text };
  // 工具/结果项：用可读化模块把工具名与入参转成「动词 + 目标」
  const d = toolDisplay(item.tool || "", parseArgsPreview(item.argsPreview || ""));
  const done = item.kind === "result";
  return {
    kind: item.kind === "result" && item.ok === false ? "fail" : d.kind,
    verb: done ? d.doneVerb : d.verb,
    target: d.target,
  };
}

/** 第二行内容：等待/失败优先表意，其次实时活动，最后回退到步骤或工具摘要 */
function secondLine(card: TaskCard, live: LiveItem[] | null): Line {
  if (card.status === "waiting") {
    const pending = card.pendingAction;
    if (pending) {
      const d = toolDisplay(pending.tool, parseArgsPreview(pending.argsPreview));
      return { kind: "other", verb: `等待确认 · ${d.verb}`, target: d.target };
    }
    return { kind: "other", verb: "等待确认", target: card.waitingReason || "需要你的决定" };
  }
  if (card.status === "failed") {
    return { kind: "fail", verb: "执行失败", target: card.errorText || "执行异常" };
  }
  if (live && live.length > 0) return liveToLine(live[0]);
  if (card.steps.length > 0) {
    const done = card.steps.filter((s) => s.status === "done").length;
    const current = card.steps.find((s) => s.status === "running");
    const label = current?.activeForm || current?.content || "";
    return {
      kind: "other",
      verb: `第 ${Math.min(done + 1, card.steps.length)}/${card.steps.length} 步`,
      target: label,
    };
  }
  if (card.currentAction) return { kind: "other", verb: "正在执行", target: card.currentAction };
  return { kind: "other", verb: "正在执行", target: "" };
}

export function StatusCapsule({
  cards,
  live,
  primaryId,
  recent,
  maxBlocks,
  expanded,
  onFocusSession,
  onStop,
  onReorder,
  onDragActive,
}: Props) {
  const listRef = useRef<HTMLDivElement | null>(null);
  const rafRef = useRef(0);
  /** 拖拽会话：起点与可移动范围（均为视口坐标） */
  const reorderRef = useRef({
    id: null as number | null,
    startY: 0,
    moved: false,
    index: -1,
    count: 0,
    step: 64,
  });
  const [drag, setDrag] = useState<DragState | null>(null);
  /** 乐观顺序：松手瞬间生效，让动画与顺序变更落在同一次渲染 */
  const [localOrder, setLocalOrder] = useState<number[] | null>(null);
  /** FLIP 快照：sid → 变更前的视口 top（含当时的 transform 影响） */
  const flipRef = useRef<Map<number, number>>(new Map());
  /** FLIP 补偿位移：一次性，下一帧清零并交给过渡 */
  const [flipDeltas, setFlipDeltas] = useState<Map<number, number>>(new Map());

  /** 渲染顺序：本地乐观顺序优先（索引 0 = 最外层/最靠近宠物） */
  const ordered = useMemo(() => {
    if (!localOrder || localOrder.length === 0) return cards;
    const rank = new Map(localOrder.map((id, i) => [id, i]));
    const pinned = cards.filter((c) => rank.has(c.sessionId));
    pinned.sort((a, b) => (rank.get(a.sessionId) as number) - (rank.get(b.sessionId) as number));
    const rest = cards.filter((c) => !rank.has(c.sessionId)); // 新任务回落自动排序
    return [...pinned, ...rest];
  }, [cards, localOrder]);

  // ── FLIP 落位：顺序变化后先把块"留在原视觉位置"，再过渡到新位置 ──
  useLayoutEffect(() => {
    const prev = flipRef.current;
    if (prev.size === 0) return;
    flipRef.current = new Map();
    const list = listRef.current;
    if (!list) return;
    const deltas = new Map<number, number>();
    list.querySelectorAll<HTMLElement>("[data-sid]").forEach((el) => {
      const sid = Number(el.dataset.sid);
      const before = prev.get(sid);
      if (before == null) return;
      const after = el.getBoundingClientRect().top;
      const d = before - after;
      if (Math.abs(d) >= 0.5) deltas.set(sid, d);
    });
    if (deltas.size === 0) return;
    // ① 先以"无过渡"回到变更前的位置（视觉上不动）
    setFlipDeltas(deltas);
    // ② 两帧后清零并恢复过渡 → 平滑滑向新位置（两帧确保上一步样式已应用）
    rafRef.current = requestAnimationFrame(() => {
      rafRef.current = requestAnimationFrame(() => setFlipDeltas(new Map()));
    });
  }, [ordered]);

  // 卸载时清掉在途 rAF
  useLayoutEffect(() => () => { if (rafRef.current) cancelAnimationFrame(rafRef.current); }, []);

  /** 统一的块样式：FLIP 补偿 > 跟手 > 让位 > 静态 */
  const blockStyle = useCallback((sid: number, idx: number): CSSProperties => {
    // ① FLIP 补偿期：一次性，优先
    const fd = flipDeltas.get(sid);
    if (fd != null) return { transform: `translateY(${fd}px)`, transition: "none" };

    if (drag) {
      // ② 被拖块：跟手（无过渡，否则会拖尾）+ 轻微放大表达"抬起"
      if (drag.id === sid) {
        return { transform: `translateY(${drag.dy}px) scale(1.015)`, transition: "none", zIndex: 3 };
      }
      // ③ 其余块按跨越格数让位（带过渡，让"谁被挤开"一目了然）
      if (drag.slots !== 0) {
        const srcIdx = drag.index;
        const target = srcIdx + drag.slots;
        const lo = Math.min(srcIdx, target);
        const hi = Math.max(srcIdx, target);
        if (idx > lo && idx <= hi) {
          // 被拖块向上（索引增大）→ 让位块向下；反之向上
          const dir = drag.slots > 0 ? 1 : -1;
          return {
            transform: `translateY(${dir * drag.step}px)`,
            transition: `transform ${REORDER_MS}ms var(--pet-ease)`,
          };
        }
      }
    }
    return {};
  }, [drag, flipDeltas]);

  /** 结束拖拽：commit=true 时提交新顺序（并走 FLIP 落位） */
  const endReorder = useCallback((commit: boolean) => {
    const d = reorderRef.current;
    const srcId = d.id;
    const moved = d.moved;
    const index = d.index;
    const count = d.count;
    const cur = drag;
    d.id = null;
    d.moved = false;
    if (srcId == null) return;
    onDragActive(false);

    if (!moved || !commit || count < 2 || !cur) {
      setDrag(null);
      return;
    }
    const slots = Math.max(-index, Math.min(count - 1 - index, cur.slots));
    if (slots === 0) {
      setDrag(null);
      return;
    }

    // FLIP First：记录各块**当前视觉位置**（含跟手与让位的 transform 影响）
    const list = listRef.current;
    if (list) {
      const snap = new Map<number, number>();
      list.querySelectorAll<HTMLElement>("[data-sid]").forEach((el) => {
        snap.set(Number(el.dataset.sid), el.getBoundingClientRect().top);
      });
      flipRef.current = snap;
    }

    // 计算新顺序：把 srcId 从 index 移到 index + slots（索引 0 = 最外层）
    const ids = ordered.map((c) => c.sessionId);
    const next = ids.filter((id) => id !== srcId);
    next.splice(Math.max(0, Math.min(next.length, index + slots)), 0, srcId);

    // 同帧：清拖拽态（transform 归零）+ 应用新顺序 → useLayoutEffect 做 FLIP 补偿
    setDrag(null);
    if (next.join(",") !== ids.join(",")) {
      setLocalOrder(next);
      onReorder(next);
    }
  }, [drag, ordered, onDragActive, onReorder]);

  const onBlockPointerDown = useCallback((e: React.PointerEvent, id: number) => {
    if (e.button !== 0) return;
    // 角标按钮上的按下不启动调序（否则点「回会话 / 停止」会带出拖拽）
    if ((e.target as HTMLElement).closest("button")) return;
    const list = listRef.current;
    if (!list) return;
    const els = Array.from(list.querySelectorAll<HTMLElement>("[data-sid]"));
    if (els.length < 2) return;
    const index = els.findIndex((el) => Number(el.dataset.sid) === id);
    if (index < 0) return;
    // 相邻块中心距（= 块高 + 间距）：实测而非硬编码，避免与 CSS 脱节
    const rects = els.map((el) => el.getBoundingClientRect());
    const step = rects.length >= 2
      ? Math.max(8, Math.abs(rects[0].top - rects[1].top))
      : rects[0].height + 8;
    reorderRef.current = { id, startY: e.clientY, moved: false, index, count: els.length, step };
    try { (e.currentTarget as Element).setPointerCapture(e.pointerId); } catch { /* 忽略 */ }
  }, []);

  const onBlockPointerMove = useCallback((e: React.PointerEvent) => {
    const d = reorderRef.current;
    if (d.id == null) return;
    if (e.buttons === 0) { endReorder(false); return; } // 按键已释放 → 收尾
    const dy = e.clientY - d.startY;
    if (!d.moved) {
      if (Math.abs(dy) < REORDER_THRESHOLD) return;
      d.moved = true;
      onDragActive(true);
    }
    // 可移动范围（格）：向下最多 index 格、向上最多 count-1-index 格
    const minSlots = -d.index;
    const maxSlots = d.count - 1 - d.index;
    const rawSlots = Math.round(-dy / d.step); // 向上（dy<0）→ 索引增大
    const slots = Math.max(minSlots, Math.min(maxSlots, rawSlots));
    // 跟手位移按同一范围钳制，保证"块跟手"与"落点格数"始终一致
    const clampedDy = Math.max(-maxSlots * d.step, Math.min(-minSlots * d.step, dy));
    setDrag({ id: d.id, index: d.index, dy: clampedDy, slots, step: d.step });
  }, [endReorder, onDragActive]);

  const onBlockPointerUp = useCallback(() => {
    if (reorderRef.current.id == null) return;
    endReorder(true);
  }, [endReorder]);

  const onBlockPointerCancel = useCallback(() => {
    if (reorderRef.current.id == null) return;
    endReorder(false);
  }, [endReorder]);

  // 无运行任务：一块「空闲」提示（保持浮窗位置稳定，不突然消失）
  if (ordered.length === 0) {
    const r = recent[0];
    return (
      <div className="pet-blocks" ref={listRef}>
        <div className="pet-block is-idle">
          <div className="pet-block-row1">
            <span className="pet-block-title">空闲</span>
          </div>
          <div className="pet-block-row2">
            <span className="pet-block-grip" aria-hidden="true"><IconGrip size={13} /></span>
            <span className="pet-block-act is-done"><IconActivity kind="done" size={13} /></span>
            <span className="pet-block-verb">{r ? "最近完成" : "暂无任务"}</span>
            {r && <LiveTicker text={r.title} />}
          </div>
        </div>
      </div>
    );
  }

  const limit = expanded ? Math.max(1, maxBlocks) : 1;
  const shown = ordered.slice(0, limit);
  const rest = Math.max(0, ordered.length - shown.length);

  return (
    <div className="pet-blocks" ref={listRef}>
      {shown.map((c, idx) => {
        const isPrimary = c.sessionId === primaryId;
        const line = secondLine(c, isPrimary ? live : null);
        const waiting = c.status === "waiting";
        const isDragging = drag?.id === c.sessionId;
        return (
          <div
            key={c.sessionId}
            data-sid={c.sessionId}
            className={`pet-block is-${c.status}${isDragging ? " is-reordering" : ""}`}
            style={blockStyle(c.sessionId, idx)}
            onPointerDown={(e) => onBlockPointerDown(e, c.sessionId)}
            onPointerMove={onBlockPointerMove}
            onPointerUp={onBlockPointerUp}
            onPointerCancel={onBlockPointerCancel}
          >
            <div className="pet-block-row1">
              <span className="pet-block-title">{c.title}</span>
              {/* 超出展示数量的任务数并入标题行（不单独占行，避免超出窗口高度） */}
              {isPrimary && rest > 0 && <span className="pet-block-sub">+{rest}</span>}
              {c.subagentCount > 0 && <span className="pet-block-sub">+{c.subagentCount} 子</span>}

              {waiting ? (
                <button
                  type="button"
                  className="pet-block-ask"
                  onClick={() => onFocusSession(c.sessionId)}
                  aria-label="有待处理的事项，点击回到会话"
                >
                  <IconAsk size={14} />
                </button>
              ) : (
                <span className="pet-block-actions">
                  <button
                    type="button"
                    className="pet-block-btn"
                    onClick={() => onFocusSession(c.sessionId)}
                    aria-label="回到会话"
                  >
                    <IconGoSession size={14} />
                  </button>
                  <button
                    type="button"
                    className="pet-block-btn is-stop"
                    onClick={() => onStop(c)}
                    aria-label="停止本次运行"
                  >
                    <IconStopRun size={13} />
                  </button>
                </span>
              )}
            </div>
            <div className="pet-block-row2">
              {/* 抓取手柄：参考图第二行开头的六点图标，同时提示"可拖动调序" */}
              <span className="pet-block-grip" aria-hidden="true"><IconGrip size={13} /></span>
              <span className={`pet-block-act is-${line.kind}`}>
                <IconActivity kind={line.kind} size={13} />
              </span>
              <span className="pet-block-verb">{line.verb}</span>
              {line.target ? <LiveTicker text={line.target} /> : <span className="live-ticker" />}
            </div>
          </div>
        );
      })}
    </div>
  );
}
