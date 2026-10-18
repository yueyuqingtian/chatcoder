/** 全局提示（plan-20261017-ux-overhaul 阶段 A）。
 *
 * - 位置从右上角改到窗口正上方居中（标题栏下方）：右上角与右侧面板工具条重叠，且视线不在那里；
 * - 四种语气共用同一张卡片，只有图标色区分，不用彩色边框或色条；
 * - 悬停暂停计时，移开后按剩余时长继续；Esc 关闭最新一条；
 * - 离场先播退出动画再卸载，堆叠的其余条目用 FLIP 平移补位，不跳动。
 */
import { memo, useEffect, useLayoutEffect, useRef, useState } from "react";
import { useChatStore } from "../store/chat";
import { useToastStore, type ToastItem } from "../store/toast";
import { IconAlertCircle, IconAlertTriangle, IconCheckCircle, IconInfo, IconX } from "./icons";
import { IconButton } from "./ui/IconButton";

const TONE_ICON = {
  info: <IconInfo size={16} />,
  success: <IconCheckCircle size={16} />,
  warning: <IconAlertTriangle size={16} />,
  error: <IconAlertCircle size={16} />,
} as const;

/** 退出动画时长，需与 .toast-out 的 --dur-normal 对齐（含 motion-scale 时由 animationend 兜底） */
const LEAVE_MS = 220;

const ToastCard = memo(function ToastCard({ item }: { item: ToastItem }) {
  const dismiss = useToastStore((s) => s.dismiss);
  const [leaving, setLeaving] = useState(false);
  const remaining = useRef(item.duration);
  const startedAt = useRef(0);
  const timer = useRef<number | undefined>(undefined);

  const start = () => {
    if (item.duration <= 0) return;
    startedAt.current = Date.now();
    timer.current = window.setTimeout(() => setLeaving(true), remaining.current);
  };
  const pause = () => {
    if (timer.current === undefined) return;
    window.clearTimeout(timer.current);
    timer.current = undefined;
    remaining.current = Math.max(800, remaining.current - (Date.now() - startedAt.current));
  };

  useEffect(() => {
    start();
    return () => window.clearTimeout(timer.current);
    // 仅挂载时启动计时；悬停暂停 / 恢复由事件驱动
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!leaving) return;
    const t = window.setTimeout(() => dismiss(item.id), LEAVE_MS + 40);
    return () => window.clearTimeout(t);
  }, [leaving, dismiss, item.id]);

  return (
    <div
      className={`toast toast-${item.tone}${leaving ? " toast-out" : ""}`}
      role={item.tone === "error" || item.tone === "warning" ? "alert" : "status"}
      data-toast-id={item.id}
      onMouseEnter={pause}
      onMouseLeave={() => { if (!leaving) start(); }}
      onAnimationEnd={(e) => { if (leaving && e.animationName === "toast-leave") dismiss(item.id); }}
    >
      <span className="toast-icon">{TONE_ICON[item.tone]}</span>
      <div className="toast-body">
        {item.title && <div className="toast-title">{item.title}</div>}
        <div className="toast-text">{item.text}</div>
      </div>
      <IconButton
        size="xs"
        className="toast-close"
        icon={<IconX size={12} />}
        title="关闭"
        onClick={() => setLeaving(true)}
      />
    </div>
  );
});

export function Toast() {
  const items = useToastStore((s) => s.items);
  const listRef = useRef<HTMLDivElement>(null);
  const prevTops = useRef(new Map<string, number>());

  // 兼容存量：chat.error 写入即转发为错误提示并清空，64 处 setState({ error }) 无需改动
  useEffect(() => {
    const forward = (err: string | null) => {
      if (!err) return;
      useToastStore.getState().push("error", err);
      useChatStore.setState({ error: null });
    };
    forward(useChatStore.getState().error);
    return useChatStore.subscribe((s, prev) => { if (s.error !== prev.error) forward(s.error); });
  }, []);

  // Esc 关闭最新一条（不拦截事件：弹窗等其他 Esc 行为照常）
  useEffect(() => {
    if (items.length === 0) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      const last = listRef.current?.lastElementChild?.querySelector<HTMLButtonElement>(".toast-close");
      last?.click();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [items.length]);

  // FLIP：有条目离开后，其余条目从旧位置平滑移到新位置，避免整列瞬跳
  useLayoutEffect(() => {
    const list = listRef.current;
    if (!list) return;
    const next = new Map<string, number>();
    list.querySelectorAll<HTMLElement>(".toast").forEach((el) => {
      const id = el.dataset.toastId!;
      const top = el.offsetTop;
      next.set(id, top);
      const before = prevTops.current.get(id);
      if (before !== undefined && before !== top) {
        el.animate(
          [{ transform: `translateY(${before - top}px)` }, { transform: "translateY(0)" }],
          { duration: 200, easing: "cubic-bezier(0.2, 0, 0, 1)" },
        );
      }
    });
    prevTops.current = next;
  }, [items]);

  if (items.length === 0) return null;
  return (
    <div className="toast-stack" ref={listRef} aria-live="polite">
      {items.map((t) => <ToastCard key={t.id} item={t} />)}
    </div>
  );
}
