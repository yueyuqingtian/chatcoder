/** 用户消息气泡（plan-128-518）：超长文本自动折叠。
 *  · 文本区超过约 6 行才折叠，短消息零控件、零开销；
 *  · 折叠态底部渐隐（mask，不叠色块，深浅主题通吃）+ 居中圆形箭头按钮，点击展开 / 收起；
 *  · 展开/收起只改单个气泡高度，由消息流虚拟器的尺寸测量补偿滚动位置；
 *  · 展开状态按消息 id 存在模块级 Map（会话内保持，不落库）。附件卡 / 引用芯片不参与折叠。 */
import { useLayoutEffect, useMemo, useRef, useState } from "react";
import { IconChevronDown } from "../icons";
import { MessageFileCards, RefChips, TokenText, attachmentsOf, refsOf, stripRefLines } from "./AttachmentCard";
import { msgText } from "./timeline";
import type { MessageOut } from "../../api/client";
import { Button, Tooltip } from "../ui";
import { isBusy, subscribe } from "../../perf/bus";
import { COLLAPSE_LINES, isLongUserMessage } from "./userBubbleClamp";

const expandedIds = new Map<number, boolean>();

export function UserBubble({ msg }: { msg: MessageOut }) {
  const content = msg.content as Record<string, unknown>;
  const refs = refsOf(content);
  const text = stripRefLines(msgText(content), refs.length > 0);
  /** plan-631-2594：不依赖布局的下限判据（见 userBubbleClamp）——明细见下方测量注释。
   *  只作兜底，真正的边界判定仍交给像素测量（宽度变化时它会重新量）。 */
  const longByText = useMemo(() => isLongUserMessage(text), [text]);
  const textRef = useRef<HTMLDivElement>(null);
  // plan-631-2594：初值直接取文本判据——超长消息首帧即折叠，不依赖测量结果先后
  const [overflow, setOverflow] = useState(longByText);
  const [expanded, setExpanded] = useState(() => expandedIds.get(msg.id) ?? false);
  /** plan-20261019-motion-sweep：只有用户点过展开 / 收起之后才挂高度过渡——
   *  首次测出「超长」时的那次折叠（挂载即发生）若也走过渡，每条长消息出现时都会从全高「缩」到 6 行。 */
  const [animate, setAnimate] = useState(false);

  // 判定是否超长：用「行高 × 行数」换算像素阈值，宽度变化（窗口缩放、右面板折叠）时重新判定
  useLayoutEffect(() => {
    const el = textRef.current;
    if (!el) { setOverflow(longByText); return; }
    let frame = 0;
    let retryTimer = 0;
    let retries = 0;
    let disposed = false;
    const measure = () => {
      const lh = parseFloat(getComputedStyle(el).lineHeight) || 24;
      const limit = lh * COLLAPSE_LINES;
      // 折叠态 scrollHeight 即全文高度；展开态 clientHeight 也是全文高度，二者取大
      const tall = Math.max(el.scrollHeight, el.clientHeight) > limit + 2;
      setOverflow(longByText || tall);
    };
    // plan-631-2594：首次判定**无条件**执行。
    // 此前首次测量与 ResizeObserver 回调共用 isBusy() 门控——面板折叠过渡、窗口运动期间
    // 挂载的长消息会把这次唯一的初始测量整个跳过；若此后不再出现 busy 翻转（或翻转发生在
    // 订阅之前），overflow 就永久停在 false：长消息不折叠、「展开全文」按钮也不渲染，
    // 正是用户反馈的「有时候长消息没用折叠」。首次读一次 scrollHeight 只是一次布局读，
    // 代价远小于让整条长消息裸奔。
    measure();

    /** 运动期（拖拽 / 面板过渡 / 窗口移动）不测量，避免强制同步布局；
     *  但必须**自己**排重试——只依赖 busy 翻转通知会漏掉「测量被跳过之后再无翻转」的情况。 */
    let missed = false;
    const schedule = () => {
      if (disposed) return;
      if (isBusy()) {
        missed = true;
        if (!retryTimer && retries < 40) {
          retries++;
          retryTimer = window.setTimeout(() => { retryTimer = 0; schedule(); }, 120);
        }
        return;
      }
      missed = false;
      if (!frame) frame = requestAnimationFrame(() => { frame = 0; measure(); });
    };

    const ro = new ResizeObserver(schedule);
    ro.observe(el);
    // busy 结束的瞬间补测一次（只补「确实被跳过」的那次，避免运动结束后整屏气泡重复测量）
    const off = subscribe((busy) => { if (!busy && missed) schedule(); });

    return () => {
      disposed = true;
      ro.disconnect();
      off();
      if (retryTimer) window.clearTimeout(retryTimer);
      if (frame) cancelAnimationFrame(frame);
    };
  }, [text, longByText]);

  const toggle = () => {
    // 只是单个气泡的高度变化：由虚拟器的尺寸测量（ResizeObserver）自行补偿滚动位置；
    // 不派发 pane-change 事件——那组事件需要 applyPaneUpdate 的收尾配对，单独派发会让锚点状态悬挂。
    const next = !expanded;
    expandedIds.set(msg.id, next);
    setAnimate(true);
    setExpanded(next);
  };

  const collapsed = overflow && !expanded;
  return (
    <div className="turn-user-bubble" data-message-id={msg.id}>
      <MessageFileCards atts={attachmentsOf(content)} sessionId={msg.session_id} messageId={msg.id} />
      {/* plan-308-1542 需求2：引用芯片 */}
      <RefChips refs={refs} />
      {text && (
        <div
          className={"turn-user-text ub-text" + (overflow ? " is-overflow" : "") + (collapsed ? " is-collapsed" : "") + (animate ? " is-animating" : "")}
          ref={textRef}
        >
          <TokenText text={text} />
        </div>
      )}
      {overflow && (
        <Tooltip content={expanded ? "收起长消息" : "展开完整消息"}><Button
          type="button"
          size="xs"
          variant="subtle"
          pill
          className={"ub-toggle" + (expanded ? " is-open" : "")}
          aria-expanded={expanded}
          aria-label={expanded ? "收起" : "展开全文"}
          onClick={toggle}
        >
          <IconChevronDown size={14} />
          <span>{expanded ? "收起" : "展开全文"}</span>
        </Button></Tooltip>
      )}
    </div>
  );
}
