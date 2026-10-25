/** 形变图标（plan-20261009-ui U3「多图标折叠聚焦或变化时有弹性变换」）。
 *
 * 规则：形态切换只动 transform / opacity（styles/icons.css 的 .ic-plusx / .ic-sendstop …），
 * 曲线用真弹簧 --spring-bouncy，时长受「动画效果」档位约束；两种形态的图形叠放在同一网格格子里，
 * 切换时交叉缩放，不存在「先卸载再挂载」的硬切。 */

interface MorphBase {
  size?: number;
  strokeWidth?: number;
  className?: string;
}

/** + ↔ ×：整枚旋转 135°（视觉上恰好是 ×），弹簧回弹带出「拧开」的手感 */
export function IconPlusX({ open, size = 16, strokeWidth = 1.9, className }: MorphBase & { open: boolean }) {
  return (
    <svg
      className={"ic ic-plusx" + (className ? ` ${className}` : "")}
      data-open={open}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      aria-hidden
    >
      <path d="M12 5.5v13M5.5 12h13" />
    </svg>
  );
}

/** 发送 ↔ 停止 ↔ 排队：
 *  send  —— 粗短上箭头（箭身短、箭翼开阔，32px 按钮里重心居中）；
 *  stop  —— 圆角实心方块（约占 32px 按钮的三成，太小会显得空）；
 *  queue —— 同 send 图形、同底色（排队语义只由提示文案表达：灰底会被读作「不能发送」）。 */
export function IconSendStop({ mode, size = 16, className }: MorphBase & { mode: "send" | "stop" | "queue" }) {
  return (
    <span className={"ic-morph ic-sendstop" + (className ? ` ${className}` : "")} data-mode={mode} style={{ width: size, height: size }} aria-hidden>
      <svg className="ic-ss-send" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.4} strokeLinecap="round" strokeLinejoin="round">
        <path d="M12 18.5V6.5" />
        <path d="M6.5 11.5 12 6l5.5 5.5" />
      </svg>
      <svg className="ic-ss-stop" width={size} height={size} viewBox="0 0 24 24" aria-hidden>
        <rect x="5" y="5" width="14" height="14" rx="3.4" fill="currentColor" />
      </svg>
    </span>
  );
}

/** 复制 ↔ 完成：复制双框缩小淡出，对勾描边绘入 */
export function IconCopyCheck({ done, size = 14, strokeWidth = 1.8, className }: MorphBase & { done: boolean }) {
  return (
    <span className={"ic-morph ic-copycheck" + (className ? ` ${className}` : "")} data-done={done} style={{ width: size, height: size }} aria-hidden>
      <svg className="ic-cc-copy" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round" strokeLinejoin="round">
        <rect x="9" y="9" width="12" height="12" rx="2.5" />
        <path d="M5 15H4.5A1.5 1.5 0 0 1 3 13.5v-9A1.5 1.5 0 0 1 4.5 3h9A1.5 1.5 0 0 1 15 4.5V5" />
      </svg>
      <svg className="ic-cc-check" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="var(--success)" strokeWidth={strokeWidth + 0.4} strokeLinecap="round" strokeLinejoin="round">
        <path d="M5 12.5 10 17.5 19.5 7" pathLength={1} />
      </svg>
    </span>
  );
}

/** 文件夹开合：后片不动，前盖绕底边斜切翻开并露出纸张 */
export function IconFolderMorph({ open, size = 16, strokeWidth = 1.75, className }: MorphBase & { open: boolean }) {
  return (
    <svg
      className={"ic ic-foldermorph" + (className ? ` ${className}` : "")}
      data-open={open}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="M3 17.5V6.5a2 2 0 0 1 2-2h4.2a2 2 0 0 1 1.4.6l1.8 1.8a2 2 0 0 0 1.4.6H19a2 2 0 0 1 2 2v1.5" />
      <path className="ic-fm-paper" d="M6.5 10.5V9h11v1.5" />
      <path className="ic-fm-flap" d="M3 17.5a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-6a1 1 0 0 0-1-1H4a1 1 0 0 0-1 1z" />
    </svg>
  );
}
