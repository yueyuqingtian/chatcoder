/** 宠物窗口主组件（plan-73-342）。
 *
 * 架构（沿用上一轮确立的方向，本轮做稳定性与手感修复）：
 *  · **窗口尺寸恒定**（384×520），形态变化全用窗口内动画 —— 不 resize，故无残影。
 *  · **拖拽 / 缩放由主进程主导**：渲染层只发一次「开始」信号，之后由主进程轮询光标定位，
 *    避免跨进程坐标换算误差与合成事件干扰。
 *
 * 本轮修复（对应实测反馈）：
 *  ① **宠物任意位置可拖**：命中判定改为「宠物矩形 + 外扩容差」，不再用 alpha 网格 ——
 *     网格只覆盖精灵图部分像素，动画换了姿态、点到肢体边缘就判为"透明"而穿透，表现为"点不动"。
 *  ② **失焦后浮窗能折叠**：悬停态改由 `mousemove` **坐标推导**，并监听 `mouseleave`/`blur` 兜底清理。
 *     此前依赖 pointerenter/leave，而穿透窗口在切换穿透时会丢事件，浮窗就永远卡在展开态。
 *  ③ **缩放手柄在宠物区域内**：手柄移入宠物容器右下角，避免"点在手柄上却触发拖拽"。
 *  ④ 拖拽帧率：主进程侧已改为跨屏才重算工作区、位置未变不发 setBounds。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { DetailPanel } from "./DetailPanel";
import { IconChevron, IconHide, IconResize, IconSettings } from "./PetIcons";
import { SpritePlayer, PET_DISPLAY_BASE } from "./SpritePlayer";
import { StatusCapsule } from "./StatusCapsule";
import { TaskBadge } from "./TaskBadge";
import { petApi, type PetBoot } from "./petApi";
import { useLiveActivity } from "./useLiveActivity";
import { useTaskCards, type TaskCard } from "./useTaskCards";
import type { PetState } from "./petAnim";
import { useNow } from "./useNow";

/** 完成庆祝时长 / 挥手提醒时长 */
const CELEBRATE_MS = 6000;
const WAVING_MS = 5000;
/** 拖拽启动阈值（小于该位移视为点击） */
const DRAG_THRESHOLD = 6;
/** 宠物命中外扩容差：覆盖图像边缘的抗锯齿像素与缩放手柄区，避免"点到边上拖不动" */
const PET_HIT_PAD = 10;
/** 悬停态兜底校正间隔（ms）：透明穿透窗口被快速移出时会丢 mouseleave 与后续 mousemove，
 *  旧坐标仍停在浮窗矩形内 → 浮窗卡在展开态；定时向主进程查真实光标位置来校正。 */
const HOVER_RECONCILE_MS = 400;

function defaultPref(): PetBoot["pref"] {
  return {
    enabled: false,
    slug: "",
    displayId: null,
    rightGap: null,
    bottomY: null,
    edge: null,
    visible: true,
    floatCollapsed: false,
    blockOrder: [],
    scale: 1,
    clickThrough: true,
    showCapsule: true,
    maxCapsuleRows: 3,
    showBadge: true,
    hideOnFullscreen: false,
    hideOnMinimize: true,
  };
}

export function PetApp() {
  const [boot, setBoot] = useState<PetBoot | null>(null);
  /** 鼠标是否在宠物区域（决定缩放手柄显示，以及拖拽受击时的视觉反馈） */
  const [petHover, setPetHover] = useState(false);
  /** 鼠标是否在浮窗区（决定浮窗展开几块 —— 用户要求"聚焦浮窗才展开"） */
  const [blocksHover, setBlocksHover] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [selected, setSelected] = useState<number | null>(null);
  const [celebrate, setCelebrate] = useState(false);
  const [waving, setWaving] = useState(false);
  const [dragDir, setDragDir] = useState<"left" | "right" | null>(null);
  const [dragging, setDragging] = useState(false);
  const [reordering, setReordering] = useState(false);
  const [menuAt, setMenuAt] = useState<{ x: number; y: number } | null>(null);
  const [mainFocused, setMainFocused] = useState(true);
  const [reducedMotion, setReducedMotion] = useState(
    () => window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );

  const petBoxRef = useRef<HTMLDivElement | null>(null);
  const blocksRef = useRef<HTMLDivElement | null>(null);
  const badgeRef = useRef<HTMLSpanElement | null>(null);
  const handleRef = useRef<HTMLButtonElement | null>(null);
  const resizeRef = useRef<HTMLButtonElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const interactiveRef = useRef(false);
  /** 悬停态的镜像（供事件回调比对，避免闭包读到过期值） */
  const petHoverRef = useRef(false);
  const blocksHoverRef = useRef(false);
  const prevTotalRef = useRef(0);
  const frozenBlocksRef = useRef(0);
  /** 拖拽会话：只记录起点与抓取点偏移（后续交给主进程）。
   *  方向不在本地跟踪 —— 拖拽期间收不到 pointermove，方向由主进程判定后推送。 */
  const dragRef = useRef({ active: false, moved: false, startX: 0, startY: 0, offX: 0, offY: 0 });

  const pref = boot ? boot.pref : defaultPref();

  // 任务数据（浮窗聚焦或面板展开时提高刷新节奏；标题缓存来自主进程首屏）
  const { cards, aggregate, recent, loadStepsFor } = useTaskCards(
    boot ? boot.backendPort : null,
    expanded || blocksHover,
    pref.blockOrder,
    boot ? boot.titles : {}
  );
  const now = useNow(cards.length > 0 || expanded, 1000);

  /** 主任务 = 列表第一张 */
  const primaryId = cards.length > 0 ? cards[0].sessionId : null;

  /** 实时消息行：**连接常驻**（只要宠物窗口可见且有主任务），不随 hover 断连 ——
   *  流式增量不入服务端缓冲，断连即永久丢失（历史问题：浮窗消息卡住不动）。 */
  const live = useLiveActivity(primaryId, boot ? boot.backendPort : null, !!primaryId);

  // ── 启动数据 + 主进程事件 ──
  useEffect(() => {
    const api = petApi();
    if (!api) return;
    void api.getBoot().then(setBoot).catch(() => setBoot(null));
    const offPref = api.onPrefChanged((next) => setBoot((b) => (b ? { ...b, pref: next } : b)));
    const offAssets = api.onAssets((payload) =>
      setBoot((b) => (b ? { ...b, pet: payload.pet ?? null } : b))
    );
    const offFocus = api.onMainFocus((focused) => setMainFocused(focused));
    // 交互由主进程判定结束（按键松开），这里据此复位视觉态
    const offInteraction = api.onInteractionEnded(() => {
      setDragging(false);
      setDragDir(null);
      dragRef.current.active = false;
      dragRef.current.moved = false;
    });
    // 行走方向：拖拽期间窗口跟随光标移动，鼠标相对窗口不再移动 → 渲染层收不到 pointermove，
    // 方向只能由主进程按光标实时判定后推送（否则方向会冻结在启动值）
    const offDragDir = api.onDragDir((dir) => setDragDir(dir));
    return () => {
      offPref();
      offAssets();
      offFocus();
      offInteraction();
      offDragDir();
    };
  }, []);

  // 系统「减少动态效果」→ 降帧（可访问性，跟随系统设置）
  useEffect(() => {
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    const onChange = () => setReducedMotion(mq.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  // ── 浮窗块数：未聚焦浮窗时只显示最新一条；聚焦浮窗才展开 ──
  const maxBlocks = Math.max(1, pref.maxCapsuleRows || 3);
  const floatCollapsed = pref.floatCollapsed === true;
  const blockCountRaw = useMemo(() => {
    if (dragging) return 0; // 拖拽时收起浮窗：避免遮挡，也让命中区稳定
    if (floatCollapsed) return 0; // 折叠手柄主动隐藏浮窗（宠物本体保持显示）
    if (expanded) return 0; // 面板态由面板承载列表
    if (pref.showCapsule === false) return 0;
    if (blocksHover) return Math.min(cards.length || 1, maxBlocks);
    return 1; // 默认：只显示最新一条（空闲时也显示 1 块，保持布局稳定）
  }, [dragging, floatCollapsed, expanded, pref.showCapsule, blocksHover, cards.length, maxBlocks]);

  // 调序期间冻结块数：拖动时鼠标可能短暂离开浮窗区，块区收缩会打乱落点
  useEffect(() => {
    if (!reordering) frozenBlocksRef.current = blockCountRaw;
  }, [blockCountRaw, reordering]);
  const blockCount = reordering ? frozenBlocksRef.current : blockCountRaw;

  /** 判断坐标是否落在元素矩形内；pad 为外扩容差 */
  const inRectOf = useCallback((el: Element | null, x: number, y: number, pad = 0) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 && r.height <= 0) return false;
    return x >= r.left - pad && x <= r.right + pad && y >= r.top - pad && y <= r.bottom + pad;
  }, []);

  /** 鼠标是否落在宠物可交互区（宠物本体 + 缩放手柄 + 折叠手柄）。
   *  **两个手柄都必须算在内**：它们只在「聚焦宠物」时显示，若鼠标从宠物移到手柄上
   *  就不算"还在宠物区"，悬停态会立刻变 false → 手柄刚出现就消失，根本点不到。 */
  const overPetArea = useCallback((x: number, y: number) => {
    if (inRectOf(petBoxRef.current, x, y, PET_HIT_PAD)) return true;
    if (inRectOf(resizeRef.current, x, y, 6)) return true;
    if (inRectOf(handleRef.current, x, y, 6)) return true;
    return false;
  }, [inRectOf]);

  // ── 命中检测 + 悬停推导（同一份 mousemove，状态永远同步）──
  useEffect(() => {
    const api = petApi();
    // 面板展开 / 拖拽 / 调序中：整窗可交互（交互期间不能被穿透打断）
    if (expanded || dragging || reordering) {
      if (!interactiveRef.current) {
        interactiveRef.current = true;
        void api?.setInteractive(true);
      }
      return;
    }
    const onMove = (e: MouseEvent) => {
      const x = e.clientX;
      const y = e.clientY;

      // ① 命中：宠物区（矩形 + 容差）/ 浮窗区 / 徽标 / 折叠手柄 / 缩放 / 菜单
      const hit =
        overPetArea(x, y) ||
        inRectOf(blocksRef.current, x, y) ||
        inRectOf(badgeRef.current, x, y, 2) ||
        inRectOf(handleRef.current, x, y, 6) ||
        inRectOf(menuRef.current, x, y);
      if (hit !== interactiveRef.current) {
        interactiveRef.current = hit;
        void api?.setInteractive(hit);
      }

      // ② 悬停：按坐标推导，不依赖 enter/leave —— 穿透窗口切换时会丢事件，
      //    会导致浮窗永远卡在展开态（用户反馈"失去焦点后浮窗没有折叠"）。
      const overBlocks = inRectOf(blocksRef.current, x, y, 4);
      if (overBlocks !== blocksHoverRef.current) {
        blocksHoverRef.current = overBlocks;
        setBlocksHover(overBlocks);
      }
      const overPet = overPetArea(x, y);
      if (overPet !== petHoverRef.current) {
        petHoverRef.current = overPet;
        setPetHover(overPet);
      }
    };

    // 鼠标离开窗口 / 窗口失焦：mousemove 不再派发 → 主动清一遍悬停态，
    // 保证"失去焦点后浮窗折叠、缩放手柄隐藏"。
    const clearHover = () => {
      if (blocksHoverRef.current) { blocksHoverRef.current = false; setBlocksHover(false); }
      if (petHoverRef.current) { petHoverRef.current = false; setPetHover(false); }
    };

    // ── 悬停态定时兜底（plan-73-344）──
    // 为什么必须加：透明穿透窗口在用户**快速把鼠标移远**时会一次性丢掉 mouseleave
    // 与后续 mousemove（窗口来不及切换交互态），最后收到的坐标仍停在浮窗矩形内 →
    // 浮窗卡在展开态。用户实测路径正是"聚焦最上面那块后迅速移开鼠标"。
    // 这里不依赖任何鼠标事件，直接问主进程要真实光标位置。
    const reconcile = () => {
      const api = petApi();
      if (!api?.cursorClient) return;
      void api
        .cursorClient()
        .then((p) => {
          if (!p || document.hidden) return;
          const inside =
            p.x >= -2 && p.y >= -2 && p.x <= window.innerWidth + 2 && p.y <= window.innerHeight + 2;
          if (!inside) {
            clearHover();
            return;
          }
          const overBlocks = inRectOf(blocksRef.current, p.x, p.y, 4);
          if (overBlocks !== blocksHoverRef.current) {
            blocksHoverRef.current = overBlocks;
            setBlocksHover(overBlocks);
          }
          const overPet = overPetArea(p.x, p.y);
          if (overPet !== petHoverRef.current) {
            petHoverRef.current = overPet;
            setPetHover(overPet);
          }
        })
        .catch(() => {
          /* 查询失败不影响正常交互 */
        });
    };
    const reconcileTimer = window.setInterval(reconcile, HOVER_RECONCILE_MS);
    window.addEventListener("focus", reconcile);

    window.addEventListener("mousemove", onMove, true);
    document.addEventListener("mouseleave", clearHover);
    window.addEventListener("blur", clearHover);
    return () => {
      window.clearInterval(reconcileTimer);
      window.removeEventListener("focus", reconcile);
      window.removeEventListener("mousemove", onMove, true);
      document.removeEventListener("mouseleave", clearHover);
      window.removeEventListener("blur", clearHover);
    };
  }, [expanded, dragging, reordering, inRectOf, overPetArea]);

  // 面板/菜单在鼠标移出后收起（面板是整块区域，用坐标判定同样更可靠）
  useEffect(() => {
    if (!expanded && !menuAt) return;
    const onMove = (e: MouseEvent) => {
      // 面板展开时整窗可交互，移出面板区即收起
      if (expanded && !inRectOf(document.querySelector(".pet-panel-wrap"), e.clientX, e.clientY)) {
        setExpanded(false);
        setSelected(null);
      }
    };
    window.addEventListener("mousemove", onMove, true);
    return () => window.removeEventListener("mousemove", onMove, true);
  }, [expanded, menuAt, inRectOf]);

  // ── Esc 收起面板 / 菜单 ──
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      setExpanded(false);
      setSelected(null);
      setMenuAt(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // ── 右键菜单：点击外部 / 再次右键 / Esc 关闭 ──
  useEffect(() => {
    if (!menuAt) return;
    const close = () => setMenuAt(null);
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") close(); };
    window.addEventListener("pointerdown", close, true);
    window.addEventListener("contextmenu", close, true);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("pointerdown", close, true);
      window.removeEventListener("contextmenu", close, true);
      window.removeEventListener("keydown", onKey);
    };
  }, [menuAt]);

  // ── 完成庆祝 / 未聚焦时的挥手提醒 ──
  useEffect(() => {
    const prev = prevTotalRef.current;
    prevTotalRef.current = cards.length;
    if (prev > 0 && cards.length === 0) {
      setCelebrate(true);
      const timer = window.setTimeout(() => setCelebrate(false), CELEBRATE_MS);
      if (!mainFocused) {
        setWaving(true);
        window.setTimeout(() => setWaving(false), WAVING_MS);
      }
      return () => window.clearTimeout(timer);
    }
    return undefined;
  }, [cards.length, mainFocused]);

  // ── 拖拽：只发「开始」信号（抓取点偏移），之后由主进程轮询光标定位 ──
  const onPetPointerDown = useCallback((e: React.PointerEvent) => {
    if (e.button !== 0) return;
    const d = dragRef.current;
    d.active = true;
    d.moved = false;
    d.startX = e.screenX;
    d.startY = e.screenY;
    // clientX/clientY = 鼠标相对窗口客户区左上角的位置；
    // 保持它不变即「抓取点跟手」，不会出现首次移动时宠物瞬移到鼠标的跳变
    d.offX = e.clientX;
    d.offY = e.clientY;
    try { (e.currentTarget as Element).setPointerCapture(e.pointerId); } catch { /* 忽略 */ }
  }, []);

  const onPetPointerMove = useCallback((e: React.PointerEvent) => {
    const d = dragRef.current;
    if (!d.active) return;
    // 按键已释放（事件可能丢失）→ 立即结束，杜绝"无点击也跟随鼠标"
    if (e.buttons === 0) {
      d.active = false;
      d.moved = false;
      setDragging(false);
      setDragDir(null);
      return;
    }
    if (!d.moved) {
      const dx = e.screenX - d.startX;
      const dy = e.screenY - d.startY;
      if (Math.abs(dx) + Math.abs(dy) < DRAG_THRESHOLD) return;
      d.moved = true;
      setDragging(true);
      setExpanded(false);
      // 启动帧的位移方向作为初值：此刻窗口尚未跟随移动，screenX 仍然可信；
      // 之后的换向由主进程按光标实时判定并推送 —— 拖拽期间窗口跟着光标走，
      // 鼠标相对窗口不再移动，本地判定会把方向冻结在启动值。
      const initialDir: "left" | "right" | null = Math.abs(dx) >= 2 ? (dx > 0 ? "right" : "left") : null;
      setDragDir(initialDir);
      void petApi()?.dragBegin(d.offX, d.offY, initialDir);
    }
  }, []);

  const onPetPointerUp = useCallback(() => {
    const d = dragRef.current;
    if (!d.active) return;
    const moved = d.moved;
    d.active = false;
    d.moved = false;
    if (moved) {
      // 交互由主进程判定按键松开后收尾；这里只做本地视觉复位
      setDragging(false);
      setDragDir(null);
      void petApi()?.interactionEnd();
      return;
    }
    // 未发生位移 → 视为点击：展开/收起面板
    setExpanded((v) => {
      if (v) setSelected(null);
      return !v;
    });
  }, []);

  // 窗口级兜底复位（指针在窗口外松开、被取消、失焦、页面隐藏）
  useEffect(() => {
    const reset = () => {
      if (!dragRef.current.active) return;
      dragRef.current.active = false;
      dragRef.current.moved = false;
      setDragging(false);
      setDragDir(null);
    };
    window.addEventListener("pointerup", reset, true);
    window.addEventListener("pointercancel", reset, true);
    window.addEventListener("blur", reset, true);
    document.addEventListener("visibilitychange", reset);
    return () => {
      window.removeEventListener("pointerup", reset, true);
      window.removeEventListener("pointercancel", reset, true);
      window.removeEventListener("blur", reset, true);
      document.removeEventListener("visibilitychange", reset);
    };
  }, []);

  // ── 缩放：只发开始信号（主进程按光标纵向位移 1:1 调整宠物尺寸）──
  const onResizeDown = useCallback((e: React.PointerEvent) => {
    if (e.button !== 0) return;
    e.stopPropagation(); // 关键：阻止冒泡到宠物的拖拽处理，否则会变成"移动宠物"
    e.preventDefault();
    try { (e.currentTarget as Element).setPointerCapture(e.pointerId); } catch { /* 忽略 */ }
    void petApi()?.scaleBegin(pref.scale || 1);
  }, [pref.scale]);

  const onResizeUp = useCallback((e: React.PointerEvent) => {
    e.stopPropagation();
    void petApi()?.interactionEnd();
  }, []);

  const openSession = useCallback((sessionId: number) => {
    void petApi()?.focusSession(sessionId);
  }, []);

  const stopRun = useCallback((card: TaskCard) => {
    if (card.turnId) void petApi()?.cancelTurn(card.turnId);
  }, []);

  const hideTemporarily = useCallback(() => {
    setExpanded(false);
    setSelected(null);
    setMenuAt(null);
    void petApi()?.hideTemporarily();
  }, []);

  const toggleFloat = useCallback(() => {
    const next = !floatCollapsed;
    if (next) {
      setExpanded(false);
      setSelected(null);
      blocksHoverRef.current = false;
      setBlocksHover(false);
    }
    void petApi()?.setPref({ floatCollapsed: next });
  }, [floatCollapsed]);

  const openSettings = useCallback(() => {
    setMenuAt(null);
    void petApi()?.openSettings();
  }, []);

  const onPetContextMenu = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    setMenuAt({ x: e.clientX, y: e.clientY });
  }, []);

  const reorderBlocks = useCallback((ids: number[]) => {
    void petApi()?.setPref({ blockOrder: ids.slice(0, 20) });
  }, []);

  // ── 动画状态（优先级：拖拽 > 庆祝 > 挥动 > 任务态 > 空闲）──
  const petState: PetState = dragDir
    ? dragDir === "left" ? "running-left" : "running-right"
    : celebrate
      ? "jumping"
      : waving
        ? "waving"
        : aggregate.status === "failed"
          ? "failed"
          : aggregate.status === "waiting"
            ? "waiting"
            : aggregate.status === "running"
              ? "running"
              : "idle";
  useEffect(() => {
    if (petState !== "jumping" && celebrate) setCelebrate(false);
    if (petState !== "waving" && waving) setWaving(false);
  }, [petState, celebrate, waving]);

  const motionLevel: "full" | "reduced" = reducedMotion ? "reduced" : "full";

  // 宠物显示尺寸注入 CSS 变量：与主进程越界钳制用的换算保持一致
  const petW = Math.round(PET_DISPLAY_BASE.w * (pref.scale || 1));
  const petH = Math.round(PET_DISPLAY_BASE.h * (pref.scale || 1));

  return (
    <div
      className={`pet-root is-${expanded ? "expanded" : blockCount > 0 ? "capsule" : "collapsed"}`
        + (petHover ? " is-pet-hover" : "")
        + (dragging ? " is-dragging" : "")}
      style={{ "--pet-w": `${petW}px`, "--pet-h": `${petH}px` } as React.CSSProperties}
    >
      {/* 浮窗块区：默认只显示最新一条，鼠标移到浮窗上才展开 */}
      <div className={`pet-blocks-wrap${blockCount > 0 ? " is-open" : ""}`} ref={blocksRef}>
        {blockCount > 0 && (
          <StatusCapsule
            cards={cards}
            live={live}
            primaryId={primaryId}
            recent={recent}
            maxBlocks={maxBlocks}
            expanded={blocksHover}
            onFocusSession={openSession}
            onStop={stopRun}
            onReorder={reorderBlocks}
            onDragActive={setReordering}
          />
        )}
      </div>

      {/* 展开面板：窗口内区域，带淡入位移（窗口尺寸不变） */}
      <div className={`pet-panel-wrap${expanded ? " is-open" : ""}`}>
        {expanded && (
          <DetailPanel
            cards={cards}
            aggregate={aggregate}
            recent={recent}
            selected={selected}
            now={now}
            onSelect={setSelected}
            onOpenSession={openSession}
            onLoadSteps={(sid) => void loadStepsFor(sid)}
            onHide={hideTemporarily}
            onOpenSettings={openSettings}
          />
        )}
      </div>

      <div className="pet-body">
        {/* 宠物本体 + 缩放手柄同属一个容器：手柄在容器右下角，
            既保证「点到手柄」不会冒泡成拖拽，也让命中区能统一外扩 */}
        <div
          className={`pet-anchor${dragging ? " is-dragging" : ""}`}
          ref={petBoxRef}
          onPointerDown={onPetPointerDown}
          onPointerMove={onPetPointerMove}
          onPointerUp={onPetPointerUp}
          onPointerCancel={onPetPointerUp}
          onContextMenu={onPetContextMenu}
        >
          {pref.showBadge && <TaskBadge ref={badgeRef} count={aggregate.total} status={aggregate.status} />}
          <SpritePlayer
            pet={boot ? boot.pet : null}
            state={petState}
            scale={pref.scale || 1}
            motionLevel={motionLevel}
          />
          {/* 缩放手柄：仅鼠标聚焦宠物时显示；按下交给主进程按光标位移 1:1 调整 */}
          <button
            ref={resizeRef}
            type="button"
            className="pet-resize"
            onPointerDown={onResizeDown}
            onPointerMove={(e) => e.stopPropagation()}
            onPointerUp={onResizeUp}
            onPointerCancel={onResizeUp}
            aria-label="拖拽调整宠物大小"
          >
            <IconResize size={13} />
          </button>
        </div>
      </div>

      {/* 折叠手柄：只控制「上面的浮窗」显隐；居中于宠物正下方 */}
      <button
        ref={handleRef}
        type="button"
        className={`pet-handle${floatCollapsed ? " is-collapsed" : ""}`}
        onClick={toggleFloat}
        aria-label={floatCollapsed ? "显示任务浮窗" : "隐藏任务浮窗"}
      >
        <IconChevron size={12} />
      </button>

      {/* 宠物右键菜单：隐藏宠物 / 打开设置 */}
      {menuAt && (
        <div
          ref={menuRef}
          className="pet-menu"
          role="menu"
          style={{
            left: Math.max(4, Math.min(menuAt.x, window.innerWidth - 116)),
            top: Math.max(4, Math.min(menuAt.y, Math.max(4, window.innerHeight - 72))),
          }}
        >
          <button type="button" className="pet-menu-item" role="menuitem" onClick={hideTemporarily}>
            <IconHide size={13} />
            <span>隐藏宠物</span>
          </button>
          <button type="button" className="pet-menu-item" role="menuitem" onClick={openSettings}>
            <IconSettings size={13} />
            <span>打开设置</span>
          </button>
        </div>
      )}
    </div>
  );
}
