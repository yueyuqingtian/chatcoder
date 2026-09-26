/** Dialog —— 统一弹窗基座（Radix Dialog，plan-282-1416）
 *
 * 集中承载此前 5 套并行弹窗实现（Modal / ConfirmDialog / FormDialog /
 * RollbackConfirmModal / WhatsNewModal）的共同职责：
 *  - Esc 关闭、点击遮罩关闭、焦点陷阱与关闭后焦点还原（Radix 原生）；
 *  - **真实退场动画**（由 Presence 驱动，data-state=closed 时播放后卸载）——
 *    旧实现是 `if (!open) return null` 直接卸载，没有任何关闭过渡；
 *  - 宽度、标题/副标题、头部操作区、底部 footer 统一排版。
 *
 * 页签顺序：Content 内 head → body → footer，body 独立滚动（scrollbar-gutter: stable）。
 *
 * plan-41-227：额外暴露 `DialogContentHostContext`（弹窗内容节点）——弹窗内的 Radix
 * 浮层（级联菜单等）必须把 Portal 容器指向该节点，否则浮层挂在 body 下会被 Dialog 的
 * FocusScope 判为「弹窗外部」，焦点一进入浮层就被抢回，级联子菜单随即关闭。
 */
import * as RadixDialog from "@radix-ui/react-dialog";
import { createContext, useCallback, useContext, useRef, useState, type ReactNode } from "react";
import { IconX } from "../icons";

/** 弹窗内容节点（浮层容器）。null = 当前不在弹窗内，调用方回落到默认 body */
const DialogContentHostContext = createContext<HTMLElement | null>(null);

/** plan-41-227: 供弹窗内的浮层组件读取浮层容器 */
export function useDialogContentHost(): HTMLElement | null {
  return useContext(DialogContentHostContext);
}

/* ── plan-41-230：关闭弹窗后回收 body 上的 pointer-events 残留 ──────────────
 * 背景：Radix 的 DismissableLayer 把「禁用外部指针」的原始值记在**模块级变量**里，
 * 而 react-dismissable-layer 在依赖树中存在多份副本（react-dialog→1.1.5，
 * react-dropdown-menu→1.1.19，select/tooltip 各自还有一份），副本之间互不知情：
 *   菜单打开（副本 B）写入 none → 菜单项打开弹窗（副本 A）把「读到的 none」当成原值
 *   → 菜单先关（body 被恢复成 ""）→ 弹窗后关（body 被恢复成 none）← 残留
 * 结果是整个应用点不动，只能重启。这里在弹窗内容卸载后短暂盯住 body.style：一旦被写回
 * pointer-events:none 且已无任何打开中的 Radix 浮层，就清掉。不改变任何浮层自身语义。 */

/** 打开中的 Radix 浮层（有它们在场时 body 的禁用是合理的，交给它们自己恢复） */
const OPEN_LAYER_SELECTOR =
  '[data-state="open"][role="dialog"], [data-state="open"][role="menu"], [data-state="open"][role="listbox"]';

function reclaimBodyPointerEventsIfStale(): void {
  if (typeof document === "undefined") return;
  if (document.body.style.pointerEvents !== "none") return;
  if (document.querySelector(OPEN_LAYER_SELECTOR)) return;
  document.body.style.pointerEvents = "";
}

function watchBodyPointerEvents(): void {
  reclaimBodyPointerEventsIfStale();
  // 残留由 Radix 的 effect cleanup 写入，可能晚于本次调用；观察一小段时间以捕获
  const observer = new MutationObserver(reclaimBodyPointerEventsIfStale);
  observer.observe(document.body, { attributes: true, attributeFilter: ["style"] });
  window.setTimeout(() => observer.disconnect(), 1000);
}

interface DialogProps {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  subtitle?: ReactNode;
  /** 头部右侧操作区（关闭按钮之前） */
  actions?: ReactNode;
  /** 底部操作区（表单提交/确认按钮） */
  footer?: ReactNode;
  /** 最大宽度（px），默认 720 */
  width?: number;
  /** 是否显示右上角关闭按钮，默认 true */
  closable?: boolean;
  /** 点击遮罩关闭，默认 true */
  dismissOnOverlay?: boolean;
  bodyClassName?: string;
  className?: string;
  children: ReactNode;
}

export function Dialog({
  open,
  onClose,
  title,
  subtitle,
  actions,
  footer,
  width = 720,
  closable = true,
  dismissOnOverlay = true,
  bodyClassName = "",
  className = "",
  children,
}: DialogProps) {
  // plan-41-227: 用回调 ref 收集内容节点（React.useRef + useEffect 在部分挂载时序下会拿到 null）
  const [contentHost, setContentHost] = useState<HTMLElement | null>(null);
  // plan-41-230：回调 ref 需固定引用——内联箭头函数每次渲染都变，React 会反复 detach/attach，
  // 既会误触发指针事件兜底，也白跑一轮 setState；引用稳定后只在真正挂载/卸载时回调。
  const contentNodeRef = useRef<HTMLDivElement | null>(null);
  const setContentNode = useCallback((el: HTMLDivElement | null) => {
    if (el) {
      contentNodeRef.current = el;
    } else if (contentNodeRef.current) {
      contentNodeRef.current = null;
      watchBodyPointerEvents();
    }
    setContentHost(el);
  }, []);
  return (
    <RadixDialog.Root open={open} onOpenChange={(v) => { if (!v) onClose(); }}>
      <RadixDialog.Portal>
        <RadixDialog.Overlay className="ui-dialog-overlay" />
        <RadixDialog.Content
          ref={setContentNode}
          className={`ui-dialog-content ${className}`.trim()}
          style={{ ["--ui-dialog-w" as string]: `${width}px` }}
          onPointerDownOutside={(e) => { if (!dismissOnOverlay) e.preventDefault(); }}
          onInteractOutside={(e) => { if (!dismissOnOverlay) e.preventDefault(); }}
        >
          <DialogContentHostContext.Provider value={contentHost}>
            <div className="ui-dialog-head">
              <div className="ui-dialog-title-wrap">
                <RadixDialog.Title className="ui-dialog-title">{title}</RadixDialog.Title>
                {subtitle ? <RadixDialog.Description className="ui-dialog-subtitle">{subtitle}</RadixDialog.Description> : null}
              </div>
              <div className="ui-dialog-actions">
                {actions}
                {closable && (
                  <button type="button" className="ui-icon-btn" title="关闭 (Esc)" aria-label="关闭" onClick={onClose}>
                    <IconX size={15} />
                  </button>
                )}
              </div>
            </div>
            <div className={`ui-dialog-body ${bodyClassName}`.trim()}>{children}</div>
            {footer ? <div className="ui-dialog-footer">{footer}</div> : null}
          </DialogContentHostContext.Provider>
        </RadixDialog.Content>
      </RadixDialog.Portal>
    </RadixDialog.Root>
  );
}
