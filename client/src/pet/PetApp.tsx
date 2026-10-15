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
 *
 * 本轮调整（plan-344-1706）：
 *  · **没有执行中的任务时不渲染浮窗**（会话全部停止后，连同「空闲」占位块一起退场）；
 *  · **折叠语义升级**：折叠后默认隐藏，聚焦宠物显示最新一条、移到浮窗上展开、失去焦点隐藏；
 *  · 修正隐藏态「幽灵命中带」：浮窗高度为 0 时不再参与命中，鼠标路过不会误触发。
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
/** 存活心跳间隔（ms）：常驻运行，主进程连续 3s 收不到才判定渲染层冻结（约 3 个周期的余量）。 */
const HEARTBEAT_MS = 1000;

/** 光标（窗口客户区坐标）是否仍在宠物窗口内；2px 容差吸收 DIP 取整误差 */
function insideWindow(p: { x: number; y: number }): boolean {
  return p.x >= -2 && p.y >= -2 && p.x <= window.innerWidth + 2 && p.y <= window.innerHeight + 2;
}

/* plan-387-1825 A14：菜单根节点的原生捕获 stopPropagation 兜底已删除（它确实会截断
 * React 18 挂在 #pet-root 上的事件委托，删除是对的）。
 * 「重新显示后右键菜单高亮正常、点击无反应、拖不动」的真因不在渲染层：隐藏 → 显示后 Chromium 不再恢复
 * 输入子窗口，左键按下被不可激活的顶层窗口丢弃（主进程补显，见 pet.cjs ensureInputChild，plan-20261009 P2）。
 * 另一条独立成因：菜单停留超过 3s 被看门狗误判冻结、重放成穿透（见下方「存活心跳」，plan-20261009 P1）。 */

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
    blockTheme: "dark",
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
  /** 交互保持期（面板展开 / 拖拽 / 调序 / 右键菜单）：整窗必须可交互，
   *  任何命中回调都不得把交互态改回穿透（plan-20261009 P1） */
  const holdRef = useRef(false);
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

  /** 执行中的任务（plan-344-1706）：以「会话未停止」为准（对齐主界面的转圈 + 停止按钮），
   *  失败即停止 —— 失败卡不再参与浮窗展示（用户确认口径）。 */
  const activeCards = useMemo(() => cards.filter((c) => c.status !== "failed"), [cards]);

  /** 主任务 = 执行中列表的第一张 */
  const primaryId = activeCards.length > 0 ? activeCards[0].sessionId : null;

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
    if (expanded) return 0; // 面板态由面板承载列表
    if (pref.showCapsule === false) return 0;
    // 没有执行中的任务 → 不展示浮窗（plan-344-1706：空闲时连同「空闲」占位块一起退场）
    if (activeCards.length === 0) return 0;
    // 折叠态（plan-344-1706）：默认隐藏；聚焦宠物显示最新一条，鼠标移到浮窗上展开，
    // 失去焦点（离开宠物与浮窗）即隐藏 —— 折叠不再是「彻底隐藏」
    if (floatCollapsed) {
      if (blocksHover) return Math.min(activeCards.length, maxBlocks);
      return petHover ? 1 : 0;
    }
    if (blocksHover) return Math.min(activeCards.length, maxBlocks);
    return 1; // 默认：只显示最新一条
  }, [dragging, expanded, pref.showCapsule, activeCards.length, floatCollapsed, blocksHover, petHover, maxBlocks]);

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

  /** 鼠标是否落在浮窗块区（plan-344-1706）。
   *  要求 wrap 有实际高度：浮窗隐藏时它高度为 0，仅凭 pad 外扩会在宠物上方留下
   *  一条「幽灵命中带」——鼠标路过即误判为聚焦浮窗，折叠态浮窗会无端闪现。 */
  const inBlocksArea = useCallback((x: number, y: number, pad = 4) => {
    const el = blocksRef.current;
    if (!el || el.offsetHeight <= 0) return false;
    return inRectOf(el, x, y, pad);
  }, [inRectOf]);

  /** 命中结果统一落位：交互态（穿透开关）+ 悬停视觉态。
   *  plan-365-1770：鼠标事件与兜底轮询必须走同一份逻辑 —— 唤醒后事件转发可能中断，
   *  只有这条不依赖事件的兜底路径能把窗口从穿透态里拉回来。 */
  const syncAt = useCallback((x: number, y: number) => {
    const api = petApi();
    // ① 命中：宠物区（矩形 + 容差）/ 浮窗区 / 徽标 / 折叠手柄 / 缩放 / 菜单。
    //    保持期恒为命中：唤醒重放会在保持期内调用本函数，而面板区不在下列矩形里，
    //    不加这条会把「面板打开中」的窗口误切回穿透（plan-20261009 P1）。
    const hit =
      holdRef.current ||
      overPetArea(x, y) ||
      inBlocksArea(x, y, 0) ||
      inRectOf(badgeRef.current, x, y, 2) ||
      inRectOf(handleRef.current, x, y, 6) ||
      inRectOf(menuRef.current, x, y);
    if (hit !== interactiveRef.current) {
      interactiveRef.current = hit;
      void api?.setInteractive(hit);
    }

    // ② 悬停：按坐标推导，不依赖 enter/leave —— 穿透窗口切换时会丢事件，
    //    会导致浮窗永远卡在展开态（用户反馈"失去焦点后浮窗没有折叠"）。
    const overBlocks = inBlocksArea(x, y);
    if (overBlocks !== blocksHoverRef.current) {
      blocksHoverRef.current = overBlocks;
      setBlocksHover(overBlocks);
    }
    const overPet = overPetArea(x, y);
    if (overPet !== petHoverRef.current) {
      petHoverRef.current = overPet;
      setPetHover(overPet);
    }
  }, [inRectOf, overPetArea, inBlocksArea]);

  // 鼠标离开窗口 / 窗口失焦：mousemove 不再派发 → 主动清一遍悬停态与交互态，
  // 保证"失去焦点后浮窗折叠、缩放手柄隐藏、透明区域恢复穿透"。
  const clearHover = useCallback(() => {
    if (blocksHoverRef.current) { blocksHoverRef.current = false; setBlocksHover(false); }
    if (petHoverRef.current) { petHoverRef.current = false; setPetHover(false); }
    if (interactiveRef.current && !holdRef.current) {
      interactiveRef.current = false;
      void petApi()?.setInteractive(false);
    }
  }, []);

  // ── 命中/悬停定时兜底（plan-73-344 起源；plan-365-1770 扩展为「交互态同步」）──
  // 为什么必须加：透明穿透窗口在用户**快速把鼠标移远**时会一次性丢掉 mouseleave
  // 与后续 mousemove（窗口来不及切换交互态），最后收到的坐标仍停在浮窗矩形内 →
  // 浮窗卡在展开态；系统唤醒后事件转发甚至可能整体中断（用户反馈"唤醒后无法
  // 拖动/折叠/右键无反应"）。这里不依赖任何鼠标事件，直接问主进程要真实光标
  // 位置，命中态与交互态一并校正 —— 400ms 内自动恢复。
  const reconcileNow = useCallback(() => {
    const api = petApi();
    if (!api?.cursorClient) return;
    void api
      .cursorClient()
      .then((p) => {
        if (!p || document.hidden) return;
        if (!insideWindow(p)) {
          clearHover();
          return;
        }
        syncAt(p.x, p.y);
      })
      .catch(() => {
        /* 查询失败不影响正常交互 */
      });
  }, [clearHover, syncAt]);

  // ── 存活心跳（plan-387-1825 A3 起源；plan-20261009 P1 改为常驻）──
  // 必须独立常驻：此前心跳挂在上面的兜底轮询里，而该轮询在「菜单 / 面板 / 拖拽 / 调序」
  // 保持期整体停摆 —— 主进程看门狗把这种刻意暂停误判为「渲染层冻结」，每 3s 按宠物矩形
  // 重放一次穿透。右键菜单不在宠物矩形内 → 窗口转为穿透：菜单项悬停高亮照常
  // （穿透窗口仍转发 mousemove），点击却落到下层窗口 —— 即「右键菜单高亮正常、点击无反应」
  // （main.log 中菜单打开期间每 3s 一条「渲染层心跳停滞」即此误判）。
  // 心跳同时上报期望交互态：主进程侧任何路径（显示 / 唤醒重放 / 看门狗）改写后，一个周期内被纠正。
  useEffect(() => {
    const api = petApi();
    if (!api?.heartbeat) return;
    const beat = () => void api.heartbeat?.(interactiveRef.current);
    beat();
    const timer = window.setInterval(beat, HEARTBEAT_MS);
    return () => window.clearInterval(timer);
  }, []);

  // ── 命中检测 + 悬停推导（同一份 mousemove，状态永远同步）──
  useEffect(() => {
    const api = petApi();
    // 面板展开 / 拖拽 / 调序 / **右键菜单打开** 时：整窗可交互（交互期间不能被穿透打断）
    //
    // plan-387-1825 A5：此前漏了 `menuAt` —— 菜单弹出后只要鼠标移出菜单矩形，
    // syncAt 立刻把交互态置 false → 主进程 setIgnoreMouseEvents(true) 让窗口转为穿透。
    // 穿透窗口收不到 pointerdown / contextmenu，于是「点击外部关闭」永远不触发、
    // 菜单里两个按钮也点不动、宠物也拖不动（用户反馈：菜单关不掉、重新右键也还在）。
    const held = expanded || dragging || reordering || !!menuAt;
    holdRef.current = held;
    if (held) {
      // plan-20261009 P1：无条件重申。本地镜像为 true 不代表窗口真的可交互 ——
      // 主进程的显示重放 / 唤醒重放会绕过本层改写穿透态，按镜像跳过就会一直错下去。
      interactiveRef.current = true;
      void api?.setInteractive(true);
      if (!menuAt && !expanded) return;
      // 菜单 / 面板：光标离开宠物窗口即收起。保持期整窗可交互，不收起的话
      // 整块透明区域会变成看不见的「挡点击」区域（窗口不可聚焦，Esc 也到不了这里）。
      // 用光标轮询而非 mouseleave：窗口被主窗最小化连带隐藏时根本没有离开事件。
      const timer = window.setInterval(() => {
        void api
          ?.cursorClient?.()
          .then((p) => {
            if (!p || insideWindow(p)) return;
            setMenuAt(null);
            setExpanded(false);
            setSelected(null);
          })
          .catch(() => {
            /* 查询失败不影响正常交互 */
          });
      }, HOVER_RECONCILE_MS);
      return () => window.clearInterval(timer);
    }
    const onMove = (e: MouseEvent) => syncAt(e.clientX, e.clientY);
    const reconcileTimer = window.setInterval(reconcileNow, HOVER_RECONCILE_MS);
    window.addEventListener("focus", reconcileNow);

    window.addEventListener("mousemove", onMove, true);
    document.addEventListener("mouseleave", clearHover);
    window.addEventListener("blur", clearHover);
    return () => {
      window.clearInterval(reconcileTimer);
      window.removeEventListener("focus", reconcileNow);
      window.removeEventListener("mousemove", onMove, true);
      document.removeEventListener("mouseleave", clearHover);
      window.removeEventListener("blur", clearHover);
    };
  }, [expanded, dragging, reordering, menuAt, syncAt, clearHover, reconcileNow]);

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
  // plan-387-1825 A1：此前是无差别 `pointerdown → close`（捕获阶段），
  // 菜单内按下时它先于按钮 onClick 执行 → 菜单被自己卸载，两个按钮全是「假按钮」。
  // 修法只有一条：判定事件目标是否落在菜单 DOM 内，是则不关（contains 判定与时序无关）。
  //
  // A14：上一版在菜单根节点加的原生捕获阶段 stopPropagation 会截断 React 委托链，已删除。
  // （「高亮正常、点击无反应」的成因在窗口层而非事件链：输入子窗口缺席 P2 / 看门狗误判穿透 P1。）
  useEffect(() => {
    if (!menuAt) return;
    const close = () => setMenuAt(null);
    const onOutside = (e: Event) => {
      if (menuRef.current?.contains(e.target as Node)) return;
      close();
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") close(); };
    window.addEventListener("pointerdown", onOutside, true);
    window.addEventListener("contextmenu", onOutside, true);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("pointerdown", onOutside, true);
      window.removeEventListener("contextmenu", onOutside, true);
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
    // 按键已释放（事件可能丢失）→ 立即结束，杜绝"无点击也跟随鼠标"。
    // 拖拽已交给主进程后不在此收尾：失去捕获时 buttons 可能误报 0（见 onPetPointerCancel）
    if (e.buttons === 0 && !d.moved) {
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
      void petApi()
        ?.dragBegin(d.offX, d.offY, initialDir)
        .then((r) => {
          if (r && r.ok) return;
          // 主进程未接管（窗口销毁竞态）：本地复位，避免视觉态卡在拖拽中
          dragRef.current.active = false;
          dragRef.current.moved = false;
          setDragging(false);
          setDragDir(null);
        })
        .catch(() => { /* 忽略：收尾由主进程 interactionEnded 兜底 */ });
    }
  }, []);

  const onPetPointerUp = useCallback(() => {
    const d = dragRef.current;
    if (!d.active) return;
    const moved = d.moved;
    d.active = false;
    d.moved = false;
    if (moved) {
      // plan-20261016 D1：只请求收尾，视觉复位等主进程 interactionEnded 回推 ——
      // 主进程会核对左键真实状态，误报的抬起不会把拖拽掐断，本地也就不能先复位
      void petApi()?.interactionEnd();
      return;
    }
    // 未发生位移 → 视为点击：展开/收起面板
    setExpanded((v) => {
      if (v) setSelected(null);
      return !v;
    });
  }, []);

  /** 指针被取消：拖拽已交给主进程时忽略。
   *  plan-20261016 D1：主窗最小化后宠物窗口拿不到全局鼠标捕获，光标一越出窗口 Chromium 就派发
   *  pointercancel —— 此前它被当成「松手」收尾，表现为「只能拖一点点就拖不动」。
   *  拖拽的结束只认主进程的按键轮询（interactionEnded）。 */
  const onPetPointerCancel = useCallback(() => {
    const d = dragRef.current;
    if (!d.active || d.moved) return;
    d.active = false;
  }, []);

  // 窗口级兜底复位（指针在窗口外松开、被取消、失焦、页面隐藏）。
  // 已进入拖拽的会话不在此复位：拖拽由主进程按左键真实状态收尾并回推 interactionEnded（plan-20261016 D1）
  useEffect(() => {
    const reset = () => {
      if (!dragRef.current.active || dragRef.current.moved) return;
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

  // ── 系统唤醒/解锁：主进程已重放窗口状态（穿透/置顶），这里复位本地交互残留并立即校准 ──
  useEffect(() => {
    const api = petApi();
    if (!api?.onSystemResume) return;
    const off = api.onSystemResume(() => {
      // 唤醒时不可能还按着鼠标：清掉可能卡住的拖拽会话与视觉态
      dragRef.current.active = false;
      dragRef.current.moved = false;
      setDragging(false);
      setDragDir(null);
      reconcileNow();
    });
    return off;
  }, [reconcileNow]);

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
    // plan-365-1770：turnId 可能缺失（快照补建卡错过 turn.started）——
    // 携带 sessionId，由主进程补查运行中的 turn 再取消，点击不再被静默吞掉
    void petApi()?.cancelTurn(card.turnId, card.sessionId);
  }, []);

  const hideTemporarily = useCallback(() => {
    setExpanded(false);
    setSelected(null);
    setMenuAt(null);
    void petApi()?.hideTemporarily();
  }, []);

  /** 折叠/展开浮窗（plan-344-1706）：折叠后不再彻底隐藏——聚焦宠物或浮窗时仍会临时
   *  展示（见 blockCountRaw），失去焦点即隐藏；展开则恢复常显。 */
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
      <div
        className={`pet-blocks-wrap${blockCount > 0 ? " is-open" : ""} is-${pref.blockTheme === "light" ? "light" : "dark"}`}
        ref={blocksRef}
      >
        {blockCount > 0 && (
          <StatusCapsule
            cards={activeCards}
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
          onPointerCancel={onPetPointerCancel}
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
