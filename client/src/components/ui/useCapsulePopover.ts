import { useCallback, useEffect, useRef, useState } from "react";
import { usePresence } from "./usePresence";

/** 输入框上方胶囊的浮层开合（任务进度 / 工作流 / 产物共用一个胶囊、一个浮层）。
 *
 *  - 同一时间只展示一段的浮层：指针或焦点划到哪一段，浮层就切到哪一段，旧浮层立即让位，不会残留；
 *  - hover / 聚焦即展开；离开整个胶囊后留 160ms 宽限期：胶囊与浮层之间有间隙，指针穿过时先离开胶囊；
 *  - 点击分段即钉住该段浮层：钉住后划过其它分段不切换（便于逐个审核），点击另一段则改为钉住它；
 *    再点同一段、点外部或 Esc 解除；
 *  - 当前分段消失（如任务全部完成）时收起并解除钉住，避免残留状态在分段回归时自动弹出。
 *
 *  segments 是当前可见的分段；浮层内容按 active 段渲染，退场动画期间保留上一段的内容。
 *  浮层必须渲染在 wrapRef 所在元素之内（胶囊内部），点击浮层不算“点外部”。 */
export function useCapsulePopover<K extends string>(segments: readonly K[]) {
  const [active, setActive] = useState<K | null>(null);
  const [pinned, setPinned] = useState(false);
  const [retained, setRetained] = useState<K | null>(null);
  const timerRef = useRef<number | null>(null);
  const wrapRef = useRef<HTMLDivElement>(null);

  const cancelClose = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  // 当前段已经不在可见分段里（如任务做完、审核完）：这里算作“没有浮层”
  const shown = active !== null && segments.includes(active) ? active : null;
  const stale = active !== null && shown === null;

  // 段消失：同步清空 active 与钉住状态。两者是组件级 state，不随段一起消失；
  // 若残留，段回归时浮层会带着旧状态自动弹出。
  useEffect(() => {
    if (stale) {
      setActive(null);
      setPinned(false);
    }
  }, [stale]);

  useEffect(() => {
    if (shown !== null) setRetained(shown);
  }, [shown]);

  const presence = usePresence(shown !== null);

  const scheduleClose = useCallback(() => {
    cancelClose();
    timerRef.current = window.setTimeout(() => {
      timerRef.current = null;
      setActive(null);
    }, 160);
  }, [cancelClose]);

  /** 指针或焦点进入某一段：未钉住时切换到该段的浮层 */
  const enter = useCallback((seg: K) => {
    cancelClose();
    if (!pinned) setActive(seg);
  }, [cancelClose, pinned]);

  /** 指针或焦点离开整个胶囊（或浮层）：未钉住时延迟收起 */
  const leave = useCallback(() => {
    if (!pinned) scheduleClose();
  }, [pinned, scheduleClose]);

  /** 点击分段：钉住该段；再点已钉住的同一段则只解除钉住（浮层随指针离开而收起） */
  const toggle = useCallback((seg: K) => {
    cancelClose();
    if (pinned && active === seg) {
      setPinned(false);
      return;
    }
    setActive(seg);
    setPinned(true);
  }, [cancelClose, pinned, active]);

  /** 收起并解除钉住（点外部、Esc，或浮层里的操作已经把用户带到别处） */
  const dismiss = useCallback(() => {
    cancelClose();
    setActive(null);
    setPinned(false);
  }, [cancelClose]);

  useEffect(() => () => cancelClose(), [cancelClose]);

  useEffect(() => {
    if (!pinned) return;
    const onDown = (e: MouseEvent) => {
      // wrapRef 为 null（胶囊已卸载）时也视为点击外部，避免钉住状态残留
      if (!wrapRef.current?.contains(e.target as Node)) dismiss();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") dismiss();
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [pinned, dismiss]);

  return {
    wrapRef,
    /** 当前展示浮层的段（null = 不展示） */
    active: shown,
    pinned,
    enter,
    leave,
    toggle,
    dismiss,
    cancelClose,
    /** 浮层挂载 / 退场状态，以及要渲染的段（退场期间保留上一段内容） */
    popover: { mounted: presence.mounted, state: presence.state, seg: shown ?? retained },
  };
}
