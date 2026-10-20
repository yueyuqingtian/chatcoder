/**
 * 贴底跟随内核（plan-20261020-pts）：主消息流、子代理消息流与对话小窗共用。
 *
 * ── 为什么重写 ──
 * 旧实现在**每个滚动事件里按「距底距离」重算是否跟随**。可滚动事件并不都来自用户：
 * 虚拟列表测量后的位置补偿、浏览器滚动锚定、异步内容（变更卡片、图片）撑高后的位置钳制、
 * 窗口最小化时视口塌缩为 0……只要其中一个先于贴底执行，「贴底」就被误判成「用户离开了底部」，
 * 之后所有贴底都被跳过——首次进入会话、最小化再还原、展开右侧面板后偶发不贴底都是这一个问题。
 * 此前的程序滚动宽限、抑制标记、宽松阈值、运动期补滚都是在给它打补丁。
 *
 * ── 现在 ──
 * 「跟随」是**锁存状态**，只由用户意图改变：
 *  · 解除：用户向上滚轮 / 键盘上翻 / 拖滚动条 / 触摸 / 拖选文字，且滚动容器**确实向上移动**；
 *    或调用方主动跳转到某条消息前调用 release()。
 *  · 恢复：用户滚回底部；向下滚动途中进入近底区；尺寸变化后已处于底部；
 *    调用方 stick()（发送消息、切换会话、开始运行、点「回到底部」）。
 *  · 跟随期间：视口、内容、尾项任一尺寸变化都在同一帧贴底；非用户造成的位移立即补回。
 *  · 容器尺寸为 0（窗口最小化、隐藏的标签页）时不做任何判定，恢复尺寸即贴底。
 */

/** 距底不超过此值视为「在底部」（高 DPI 下 scrollTop 是小数，留 2px 舍入余量） */
const AT_BOTTOM_PX = 2;
/** 用户向下滚动途中进入此范围即恢复跟随（不必精确滚到最后一像素） */
const NEAR_BOTTOM_PX = 48;
/** 滚轮 / 键盘输入之后多久内的滚动算作用户滚动 */
const INTENT_MS = 350;
/** 向上滚轮 / 上翻键之后暂停贴底的时长：浏览器在合成线程执行滚动，若这期间内容增长触发贴底，
 *  主线程写入的 scrollTop 会覆盖用户这一下滚动（表现为「往上划不动」） */
const PAUSE_MS = 260;
/** 平滑回底的最长时长：期间不强行贴底，交给平滑滚动自己到达 */
const SMOOTH_MS = 600;

export interface StickyBottomOptions {
  /** 「回到底部」按钮显隐变化（只在变化时回调） */
  onJumpChange?: (show: boolean) => void;
  /** 跟随状态变化（只在变化时回调） */
  onFollowChange?: (following: boolean) => void;
  /** 不跟随且距底超过多少像素时显示「回到底部」，默认 120 */
  jumpThreshold?: number;
}

export class StickyBottom {
  private el: HTMLElement | null = null;
  private content: Element | null = null;
  private tail: Element | null = null;
  private ro: ResizeObserver | null = null;
  private following = true;
  private showJump = false;
  private lastTop = 0;
  private lastWheelUpAt = -Infinity;
  private lastWheelDownAt = -Infinity;
  private keyIntentUntil = 0;
  private pauseUntil = 0;
  private smoothUntil = 0;
  private pointerDown = false;
  private scrollbarDrag = false;
  private touching = false;
  private pinTimer = 0;
  private pinQueued = false;
  private readonly opts: StickyBottomOptions;
  private readonly jumpThreshold: number;

  constructor(opts: StickyBottomOptions = {}) {
    this.opts = opts;
    this.jumpThreshold = opts.jumpThreshold ?? 120;
  }

  /** 绑定滚动容器（同一元素重复绑定无副作用）。 */
  attach(el: HTMLElement): void {
    if (this.el === el) return;
    this.detach();
    this.el = el;
    this.lastTop = el.scrollTop;
    el.addEventListener("scroll", this.onScroll, { passive: true });
    // 捕获阶段：内嵌元素即使阻止冒泡，也能先记下用户的滚动意图
    el.addEventListener("wheel", this.onWheel, { passive: true, capture: true });
    el.addEventListener("pointerdown", this.onPointerDown, { passive: true });
    el.addEventListener("touchstart", this.onTouchStart, { passive: true });
    window.addEventListener("pointerup", this.onPointerUp, true);
    window.addEventListener("pointercancel", this.onPointerUp, true);
    window.addEventListener("touchend", this.onTouchEnd, true);
    window.addEventListener("touchcancel", this.onTouchEnd, true);
    window.addEventListener("keydown", this.onKeyDown, true);
    // 窗口从最小化 / 后台回来：尺寸未变时 ResizeObserver 不会回调，这里补一次
    window.addEventListener("focus", this.onWake);
    document.addEventListener("visibilitychange", this.onWake);
    this.rebuildObserver();
  }

  detach(): void {
    const el = this.el;
    if (el) {
      el.removeEventListener("scroll", this.onScroll);
      el.removeEventListener("wheel", this.onWheel, { capture: true });
      el.removeEventListener("pointerdown", this.onPointerDown);
      el.removeEventListener("touchstart", this.onTouchStart);
    }
    window.removeEventListener("pointerup", this.onPointerUp, true);
    window.removeEventListener("pointercancel", this.onPointerUp, true);
    window.removeEventListener("touchend", this.onTouchEnd, true);
    window.removeEventListener("touchcancel", this.onTouchEnd, true);
    window.removeEventListener("keydown", this.onKeyDown, true);
    window.removeEventListener("focus", this.onWake);
    document.removeEventListener("visibilitychange", this.onWake);
    this.ro?.disconnect();
    this.ro = null;
    window.clearTimeout(this.pinTimer);
    this.pinTimer = 0;
    this.el = null;
  }

  /** 内容容器（其高度变化即内容增长）。
   *  每次更换都重建观察者：观察者按创建顺序回调，排在虚拟列表的测量观察者之后，
   *  同一轮回调里先完成测量、再贴底，新内容不会有「先露出一帧未贴底」的抖动。 */
  setContent(node: Element | null): void {
    if (node === this.content) return;
    this.content = node;
    this.rebuildObserver();
  }

  /** 尾项（最后一条）：流式输出、异步卡片都在这里长高，直接观察可在同一帧贴底。 */
  setTail(node: Element | null): void {
    if (node === this.tail) return;
    if (this.tail) this.ro?.unobserve(this.tail);
    this.tail = node;
    if (node) this.ro?.observe(node);
  }

  isFollowing(): boolean {
    return this.following;
  }

  /** 恢复跟随并贴底：发送消息、切换会话、开始运行、点「回到底部」。 */
  stick(smooth = false): void {
    this.pauseUntil = 0;
    this.setFollowing(true);
    const el = this.el;
    if (!el || this.collapsed(el)) return;
    if (smooth && this.distance(el) > AT_BOTTOM_PX) {
      // 离得很远时先跳到距底一屏处：长会话里平滑滚过整段历史既慢，又让虚拟列表逐段渲染
      const max = el.scrollHeight - el.clientHeight;
      if (max - el.scrollTop > el.clientHeight * 2) el.scrollTop = max - el.clientHeight;
      this.smoothUntil = performance.now() + SMOOTH_MS;
      el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
      this.schedulePin(SMOOTH_MS + 20);
    } else {
      this.smoothUntil = 0;
      this.pin();
    }
    this.updateJump();
  }

  /** 解除跟随：调用方要把视图带离底部（跳到某条消息、搜索命中）之前调用。 */
  release(): void {
    this.smoothUntil = 0;
    this.pauseUntil = 0;
    this.setFollowing(false);
    this.updateJump();
  }

  /** 请求贴底：同一任务内的多次请求合并成一次，在微任务里执行（仍在本帧绘制之前）。
   *  用于虚拟列表的变更回调——一次测量回调会逐项改尺寸并重排位置，逐项贴底会逐项强制重排；
   *  上方条目测量变高、尾项尺寸不变时，也只有这条路径能在同一帧补上贴底。 */
  requestPin(): void {
    if (!this.following || this.pinQueued) return;
    this.pinQueued = true;
    queueMicrotask(() => {
      this.pinQueued = false;
      this.pin();
    });
  }

  /** 跟随中则贴底（外部触发点：条目变化、整表刷新、首屏定位逐帧收敛）。 */
  pin(): void {
    const el = this.el;
    if (!el || !this.following || this.collapsed(el)) return;
    const now = performance.now();
    if (now < this.smoothUntil) return;
    if (now < this.pauseUntil || this.scrollbarDrag || this.touching) {
      // 用户正在操作滚动：先不抢，操作结束后补一次
      this.schedulePin(Math.max(16, this.pauseUntil - now));
      return;
    }
    if (this.pointerDown && this.selectingInside(el)) {
      // 拖选文字时内容若继续上移，选区会从鼠标下被拖走：视为用户接管
      this.setFollowing(false);
      this.updateJump();
      return;
    }
    if (el.scrollHeight - el.clientHeight - el.scrollTop > 0.5) {
      // 写 scrollHeight 而不是算出的最大值：由浏览器钳制到真实上限（含小数像素）
      el.scrollTop = el.scrollHeight;
    }
    this.lastTop = el.scrollTop;
  }

  // ───────────────────────── 内部 ─────────────────────────

  private rebuildObserver(): void {
    this.ro?.disconnect();
    this.ro = null;
    if (!this.el || typeof ResizeObserver === "undefined") return;
    this.ro = new ResizeObserver(this.onResize);
    this.ro.observe(this.el);
    if (this.content) this.ro.observe(this.content);
    if (this.tail) this.ro.observe(this.tail);
  }

  private setFollowing(v: boolean): void {
    if (v === this.following) return;
    this.following = v;
    if (v && this.showJump) {
      this.showJump = false;
      this.opts.onJumpChange?.(false);
    }
    this.opts.onFollowChange?.(v);
  }

  /** 尺寸塌缩（最小化、隐藏标签、尚未布局）：此时的几何不代表任何东西，一律不判定。 */
  private collapsed(el: HTMLElement): boolean {
    return !el.isConnected || el.clientHeight < 1 || el.clientWidth < 1;
  }

  private distance(el: HTMLElement): number {
    return Math.max(0, el.scrollHeight - el.clientHeight - el.scrollTop);
  }

  private schedulePin(delay: number): void {
    window.clearTimeout(this.pinTimer);
    this.pinTimer = window.setTimeout(() => {
      this.pinTimer = 0;
      this.pin();
      this.updateJump();
    }, delay);
  }

  private updateJump(): void {
    const el = this.el;
    if (!el || this.collapsed(el)) return;
    const show = !this.following && this.distance(el) > this.jumpThreshold;
    if (show === this.showJump) return;
    this.showJump = show;
    this.opts.onJumpChange?.(show);
  }

  /** 最近的输入是否表明「用户正在朝该方向滚动」。 */
  private userScrolling(now: number, dir: "up" | "down"): boolean {
    if (this.pointerDown || this.touching || now < this.keyIntentUntil) return true;
    return now - (dir === "up" ? this.lastWheelUpAt : this.lastWheelDownAt) < INTENT_MS;
  }

  private selectingInside(el: HTMLElement): boolean {
    const sel = typeof document.getSelection === "function" ? document.getSelection() : null;
    if (!sel || sel.isCollapsed || sel.rangeCount === 0) return false;
    const node = sel.anchorNode;
    return !!node && el.contains(node);
  }

  /** 滚轮落在内嵌滚动区（思考块、工具输出、代码块）上且它还能朝该方向滚：由它消费，主消息流不动。 */
  private innerConsumes(target: EventTarget | null, dir: -1 | 1): boolean {
    const root = this.el;
    let node = target instanceof Element ? target : null;
    while (node && node !== root) {
      if (node instanceof HTMLElement && node.scrollHeight > node.clientHeight + 1) {
        const oy = getComputedStyle(node).overflowY;
        if (oy === "auto" || oy === "scroll" || oy === "overlay") {
          const canScroll = dir < 0
            ? node.scrollTop > 0.5
            : node.scrollTop + node.clientHeight < node.scrollHeight - 0.5;
          if (canScroll) return true;
        }
      }
      node = node.parentElement;
    }
    return false;
  }

  private onScroll = (): void => {
    const el = this.el;
    if (!el) return;
    const top = el.scrollTop;
    const prev = this.lastTop;
    this.lastTop = top;
    if (this.collapsed(el)) return;
    const now = performance.now();
    const dist = this.distance(el);
    const movedUp = top < prev - 0.5;
    const movedDown = top > prev + 0.5;
    if (this.following) {
      if (movedUp && dist > AT_BOTTOM_PX && this.userScrolling(now, "up")) {
        this.smoothUntil = 0;
        this.setFollowing(false);
      } else if (dist > AT_BOTTOM_PX) {
        // 非用户造成的位移（测量补偿、滚动锚定、位置钳制）：补回底部
        this.pin();
      }
    } else if (dist <= AT_BOTTOM_PX) {
      // 回到底部即恢复跟随；正在上滑经过底部附近的那一下除外
      if (!(movedUp && this.userScrolling(now, "up"))) this.setFollowing(true);
    } else if (movedDown && dist <= NEAR_BOTTOM_PX && this.userScrolling(now, "down")) {
      this.setFollowing(true);
      this.pin();
    }
    this.updateJump();
  };

  private onResize = (): void => {
    const el = this.el;
    if (!el || this.collapsed(el)) return;
    if (this.following) {
      this.pin();
    } else if (
      this.distance(el) <= AT_BOTTOM_PX
      && !this.pointerDown && !this.touching
      && performance.now() - this.lastWheelUpAt > INTENT_MS
    ) {
      // 内容变短 / 视口变高后已经在底部：用户此刻看到的就是末尾，恢复跟随
      this.setFollowing(true);
    }
    this.updateJump();
  };

  private onWheel = (e: WheelEvent): void => {
    if (e.ctrlKey) return; // 缩放手势
    if (Math.abs(e.deltaY) <= Math.abs(e.deltaX)) return; // 横向滚动（宽代码块、表格）
    const now = performance.now();
    if (e.deltaY < 0) {
      if (this.innerConsumes(e.target, -1)) return;
      this.lastWheelUpAt = now;
      if (this.following) {
        this.pauseUntil = now + PAUSE_MS;
        this.smoothUntil = 0;
      }
    } else if (!this.innerConsumes(e.target, 1)) {
      this.lastWheelDownAt = now;
    }
  };

  private onPointerDown = (e: PointerEvent): void => {
    const el = this.el;
    if (!el) return;
    this.pointerDown = true;
    // 指针落在滚动条上（内容盒右侧）：拖动期间暂停贴底，滚动交给用户
    if (e.target === el) {
      const r = el.getBoundingClientRect();
      if (e.clientX > r.left + el.clientLeft + el.clientWidth) this.scrollbarDrag = true;
    }
  };

  private onPointerUp = (): void => {
    if (!this.pointerDown) return;
    const wasDrag = this.scrollbarDrag;
    this.pointerDown = false;
    this.scrollbarDrag = false;
    if (wasDrag) {
      this.pin();
      this.updateJump();
    }
  };

  private onTouchStart = (): void => {
    this.touching = true;
  };

  private onTouchEnd = (): void => {
    if (!this.touching) return;
    this.touching = false;
    this.schedulePin(16);
  };

  private onKeyDown = (e: KeyboardEvent): void => {
    const k = e.key;
    // Tab 切换焦点时浏览器会把焦点元素滚进视口，方向不定：两个方向都算用户意图
    const up = k === "PageUp" || k === "ArrowUp" || k === "Home" || k === "Tab" || (k === " " && e.shiftKey);
    const down = k === "PageDown" || k === "ArrowDown" || k === "End" || k === "Tab" || (k === " " && !e.shiftKey);
    if (!up && !down) return;
    const t = e.target as HTMLElement | null;
    const el = this.el;
    if (!el || !t) return;
    // 输入框里的翻页键滚的是输入框；焦点在别的面板时滚的也不是消息流
    if (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)) return;
    if (t !== document.body && !el.contains(t)) return;
    const now = performance.now();
    this.keyIntentUntil = now + INTENT_MS + 150;
    if (up && this.following) this.pauseUntil = now + PAUSE_MS;
  };

  private onWake = (): void => {
    if (document.visibilityState === "hidden") return;
    this.schedulePin(0);
  };
}
