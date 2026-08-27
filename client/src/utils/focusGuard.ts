/**
 * 全局焦点保护（v25）：输入框"能删除、无法输入、无光标"问题的完整修复。
 *
 * 根因：Chromium/Electron 渲染层的文本输入状态（TextInputState / IME composition）
 * 与 DOM 焦点脱节（"幽灵组合"）。输入法组合输入进行中时输入框被卸载/重挂载
 * （切换页面、切换会话），compositionstart 没有配对的 compositionend，
 * Chromium 内部文本管线卡死。此后 keydown 正常（Backspace 仍可删除），
 * 但文本插入被吞（无法输入）、光标不显示、点击看似无反应。
 * 该状态挂在渲染器层而非元素层，blur+focus 无法修复，只能重启。
 *
 * 修复策略（三层）：
 * 1. 检测层：可打印字符 keydown 到达已聚焦输入框后，若短窗口内既无 input
 *    事件也无 compositionstart，判定文本管线卡死（正是"能删不能输"症状）；
 *    点击可编辑元素后焦点未落在其上，判定点击失效。两者触发硬重置。
 * 2. 恢复层：blur -> readOnly=true（强制 Chromium 丢弃并重建 TextInputState，
 *    幽灵组合的有效解法）-> 下一帧 readOnly=false + focus。
 * 3. 主进程层：window.chatcoderAPI.fixTextInput() 让主进程重新同步
 *    browser<->renderer 焦点状态，作为渲染层修复无效时的兜底。
 */

export interface FocusGuard {
  dispose: () => void;
}

/** 判定元素是否为可编辑输入目标。 */
function isEditable(el: Element | null): el is HTMLInputElement | HTMLTextAreaElement {
  if (!el) return false;
  const tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || (el as HTMLElement).isContentEditable === true;
}

/**
 * 判定输入框是否为文本类输入（用于输入失败探测）。
 * 日期/数字/颜色等原生输入会合法地吞掉部分按键（无 input 事件），排除以免误报。
 */
function isTextLikeInput(el: Element): boolean {
  if (el.tagName === "TEXTAREA") return true;
  if ((el as HTMLElement).isContentEditable === true) return true;
  if (el.tagName !== "INPUT") return false;
  const type = (el as HTMLInputElement).type;
  return ["text", "search", "url", "tel", "email", "password", ""].includes(type);
}

export function installFocusGuard(): FocusGuard {
  // IME composition 活跃计数：compositionstart 未配对的 compositionend 视为卡死
  let composing = 0;
  // 硬重置冷却时间戳：防止检测器连续触发导致输入框反复闪烁
  let lastResetAt = 0;
  const RESET_COOLDOWN_MS = 500;

  /**
   * 硬重置指定输入框的 Chromium 文本输入状态。
   * readOnly 切换会强制渲染器丢弃当前 TextInputState 并重建（对幽灵组合有效），
   * 随后恢复可编辑并重新聚焦。冷却期内跳过，避免正常输入被误伤。
   */
  const requestNativeFocus = () => {
    // 窗口已激活但 renderer 文档未激活时，DOM focus 会被 Chromium 丢弃；
    // IPC 只在 Electron 存在，浏览器模式保持纯 DOM 行为。
    try { void window.chatcoderAPI?.fixTextInput?.(); } catch { /* 浏览器模式忽略 */ }
  };

  const focusEditable = (el: HTMLElement) => {
    if (!document.contains(el)) return;
    try { el.focus({ preventScroll: true }); } catch { el.focus(); }
  };

  const hardReset = (el: HTMLElement) => {
    const now = Date.now();
    if (now - lastResetAt < RESET_COOLDOWN_MS) {
      requestNativeFocus();
      focusEditable(el);
      return;
    }
    lastResetAt = now;
    composing = 0;
    const isField = el.tagName === "INPUT" || el.tagName === "TEXTAREA";
    if (isField) {
      // 不先调用 blur：blur 会制造新的焦点事件风暴。readOnly 切换本身足以让
      // Chromium 丢弃旧 TextInputState，下一帧再恢复可编辑状态并聚焦。
      (el as HTMLInputElement).readOnly = true;
      requestNativeFocus();
      requestAnimationFrame(() => {
        (el as HTMLInputElement).readOnly = false;
        focusEditable(el);
      });
    } else {
      requestNativeFocus();
      requestAnimationFrame(() => focusEditable(el));
    }
  };

  // 1. mousedown 捕获阶段：目标为可编辑元素时的焦点/IME 兜底
  const onMouseDownCapture = (e: MouseEvent) => {
    const target = e.target as HTMLElement | null;
    if (!target || !target.closest) return;
    const editable = target.closest("input, textarea, [contenteditable='true']") as HTMLElement | null;
    if (!editable) return;
    const active = document.activeElement;
    if (!active) return;

    if (active === editable) {
      // 目标输入框已持有焦点：仅当 composition 卡死时才需要重置
      if (composing > 0) hardReset(editable);
      return;
    }
    if (active.tagName === "IFRAME") {
      // 焦点在 iframe 内部文档：先让 iframe 失焦，再聚焦目标输入框
      try { (active as HTMLIFrameElement).contentWindow?.blur(); } catch { /* 跨域 iframe：忽略 */ }
    }
    // 用户明确点击了可编辑元素时，无论 document.hasFocus() 当前值如何都恢复；
    // 第一次点击未激活窗口通常只完成 OS 激活，下一帧再补一次 DOM focus。
    requestNativeFocus();
    requestAnimationFrame(() => focusEditable(editable));
    window.setTimeout(() => {
      if (document.activeElement !== editable) {
        requestNativeFocus();
        focusEditable(editable);
      }
    }, 80);
  };
  document.addEventListener("mousedown", onMouseDownCapture, true);

  // 2. 点击有效性检测：点击可编辑元素后焦点未落在其上（点击"没反应"），
  //    说明默认聚焦被吞 -> 硬重置。仅限 mousedown 目标本身就是输入框的场景，
  //    避免误伤点击容器/标签等合法的非聚焦点击。
  const CLICK_SETTLE_MS = 150;
  const onSettleMouseDown = (e: MouseEvent) => {
    const target = e.target as HTMLElement | null;
    if (!isEditable(target)) return;
    const editable = target;
    window.setTimeout(() => {
      const active = document.activeElement;
      // 焦点已落在该输入框（或其 contenteditable 内部）：点击生效，无需处理
      if (active === editable || (active !== null && editable.contains(active))) return;
      // 焦点移到了其他正常元素（用户 150ms 内又点了别处）：非失效场景，跳过
      if (active && active !== document.body && active.tagName !== "IFRAME"
        && document.contains(active)) return;
      // 不以 document.hasFocus() 作为短路条件：这是本次问题的诊断现象，
      // 窗口激活后仍可能暂时为 false，必须继续请求原生同步并重试。
      hardReset(editable);
    }, CLICK_SETTLE_MS);
  };
  document.addEventListener("mousedown", onSettleMouseDown, true);

  // 3. 输入失败检测（核心）：可打印字符 keydown 到达已聚焦输入框，
  //    若窗口期内既无 input 事件也无 compositionstart，说明文本插入被吞
  //    （"能删不能输"的直接症状）-> 硬重置。
  //    正常输入链：keydown -> beforeinput -> input；
  //    IME 输入链：keydown -> compositionstart（被排除在检测外）。
  const KEY_SETTLE_MS = 150;
  let keyTimer: number | null = null;
  let pendingKeyEl: HTMLElement | null = null;
  let pendingKeyResolved = false;

  const clearKeyProbe = () => {
    if (keyTimer !== null) { window.clearTimeout(keyTimer); keyTimer = null; }
    pendingKeyEl = null;
  };

  const onKeyDownCapture = (e: KeyboardEvent) => {
    // 只探测可打印单字符、无修饰键、非 IME 处理中的按键
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key.length !== 1) return;
    if (e.isComposing || e.keyCode === 229) return;
    const target = e.target as HTMLElement | null;
    if (!target || !isTextLikeInput(target)) return;
    if (document.activeElement !== target) return;
    // 只读/禁用的输入框不接受输入，跳过避免误报
    const field = target as HTMLInputElement;
    if ((field.tagName === "INPUT" || field.tagName === "TEXTAREA")
      && (field.readOnly || field.disabled)) return;

    clearKeyProbe();
    pendingKeyEl = target;
    pendingKeyResolved = false;
    keyTimer = window.setTimeout(() => {
      const el = pendingKeyEl;
      clearKeyProbe();
      if (!el || pendingKeyResolved) return;
      if (!document.hasFocus()) return;
      if (document.activeElement !== el) return;
      if (composing > 0) return;
      // 字符被吞：文本管线卡死，硬重置
      hardReset(el);
    }, KEY_SETTLE_MS);
  };

  const onInput = (e: Event) => {
    // 正常插入到达：撤销检测
    if (e.target === pendingKeyEl) { pendingKeyResolved = true; clearKeyProbe(); }
  };
  const onCompositionStart = (e: CompositionEvent) => {
    composing++;
    if (e.target === pendingKeyEl) { pendingKeyResolved = true; clearKeyProbe(); }
  };
  const onCompositionEnd = () => { composing = Math.max(0, composing - 1); };

  document.addEventListener("keydown", onKeyDownCapture, true);
  document.addEventListener("input", onInput, true);
  document.addEventListener("compositionstart", onCompositionStart);
  document.addEventListener("compositionend", onCompositionEnd);

  // 4. composition 卡死兜底（focusin 路径：焦点转移到新输入框时）
  const onFocusIn = (e: FocusEvent) => {
    const target = e.target as HTMLElement | null;
    if (!target || !target.closest) return;
    if (!target.closest("input, textarea, [contenteditable='true']")) return;
    if (composing <= 0) return;
    // 输入框获得焦点时 composition 仍处于活跃态：说明上一个输入框卸载时
    // composition 被中断，IME 状态卡死。硬重置恢复输入能力。
    hardReset(target);
  };
  document.addEventListener("focusin", onFocusIn);

  // 4.5 composition 泄漏防护：焦点离开输入框时强制清零 composing。
  // 输入法组合输入进行中切换页面/卸载输入框（设置页 ↔ 会话页），compositionend
  // 可能不派发，计数泄漏后每次输入框 focusin 都会触发硬重置；若点击频率落在
  // 500ms 冷却窗口内，hardReset 直接 return（composing 不清零），输入框陷入
  // "永远无法稳定聚焦"——正是"无论怎么点击都进不去"的根因之一。
  const onFocusOut = () => {
    if (composing > 0) composing = 0;
  };
  document.addEventListener("focusout", onFocusOut);

  return {
    dispose: () => {
      clearKeyProbe();
      document.removeEventListener("mousedown", onMouseDownCapture, true);
      document.removeEventListener("mousedown", onSettleMouseDown, true);
      document.removeEventListener("keydown", onKeyDownCapture, true);
      document.removeEventListener("input", onInput, true);
      document.removeEventListener("compositionstart", onCompositionStart);
      document.removeEventListener("compositionend", onCompositionEnd);
      document.removeEventListener("focusin", onFocusIn);
      document.removeEventListener("focusout", onFocusOut);
    },
  };
}
