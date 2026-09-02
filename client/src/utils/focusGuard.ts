/**
 * 全局焦点与输入保护（安全版）：
 * 避免在正常输入时激进拦截或高频调用 hardReset/readOnly 切换，
 * 绝不干扰 Windows 系统 TSF 与 IME 消息管道。
 */

export interface FocusGuard {
  dispose: () => void;
}

export function installFocusGuard(): FocusGuard {
  // 仅在 iframe 内部获焦且用户点击外部时，安全辅助 iframe 失焦
  const onMouseDownCapture = (e: MouseEvent) => {
    const target = e.target as HTMLElement | null;
    if (!target || !target.closest) return;
    const editable = target.closest("input, textarea, [contenteditable='true']") as HTMLElement | null;
    if (!editable) return;

    const active = document.activeElement;
    if (active && active.tagName === "IFRAME" && active !== editable) {
      try {
        (active as HTMLIFrameElement).contentWindow?.blur();
      } catch {
        /* 跨域 iframe 忽略 */
      }
    }
  };

  // ── 焦点事件诊断日志（一次性：定位抢焦点者后移除）──
  // main.cjs 会把 renderer console 转发到日志文件，复现时据此确认
  // focusin/focusout/window blur/focus 的时序与目标。
  const diagFocus = (ev: Event) => {
    const t = ev.target as HTMLElement | null;
    const tag = t ? `${t.tagName}.${String(t.className || "").split(/\s+/).join(".")}` : "null";
    const act = document.activeElement;
    const actTag = act ? `${act.tagName}.${String((act as HTMLElement).className || "").split(/\s+/).join(".")}` : "null";
    console.log(`[focus-diag] ${ev.type} target=${tag} active=${actTag}`);
  };
  const diagWindow = (ev: Event) => {
    console.log(`[focus-diag] window:${ev.type} hasFocus=${document.hasFocus()}`);
  };
  document.addEventListener("focusin", diagFocus, true);
  document.addEventListener("focusout", diagFocus, true);
  window.addEventListener("blur", diagWindow);
  window.addEventListener("focus", diagWindow);

  document.addEventListener("mousedown", onMouseDownCapture, true);

  return {
    dispose: () => {
      document.removeEventListener("mousedown", onMouseDownCapture, true);
      document.removeEventListener("focusin", diagFocus, true);
      document.removeEventListener("focusout", diagFocus, true);
      window.removeEventListener("blur", diagWindow);
      window.removeEventListener("focus", diagWindow);
    },
  };
}
