/** 用户消息气泡（plan-128-518）：超长文本自动折叠。
 *  · 文本区超过约 6 行才折叠，短消息零控件、零开销；
 *  · 折叠态底部渐隐（mask，不叠色块，深浅主题通吃）+ 居中圆形箭头按钮，点击展开 / 收起；
 *  · 展开/收起只改单个气泡高度，由消息流虚拟器的尺寸测量补偿滚动位置；
 *  · 展开状态按消息 id 存在模块级 Map（会话内保持，不落库）。附件卡 / 引用芯片不参与折叠。 */
import { useLayoutEffect, useRef, useState } from "react";
import { IconChevronDown } from "../icons";
import { MessageFileCards, RefChips, TokenText, attachmentsOf, refsOf, stripRefLines } from "./AttachmentCard";
import { msgText } from "./timeline";
import type { MessageOut } from "../../api/client";

/** 折叠行数阈值：超过 6 行才出现控件 */
const COLLAPSE_LINES = 6;
const expandedIds = new Map<number, boolean>();

export function UserBubble({ msg }: { msg: MessageOut }) {
  const content = msg.content as Record<string, unknown>;
  const refs = refsOf(content);
  const text = stripRefLines(msgText(content), refs.length > 0);
  const textRef = useRef<HTMLDivElement>(null);
  const [overflow, setOverflow] = useState(false);
  const [expanded, setExpanded] = useState(() => expandedIds.get(msg.id) ?? false);
  /** plan-20261019-motion-sweep：只有用户点过展开 / 收起之后才挂高度过渡——
   *  首次测出「超长」时的那次折叠（挂载即发生）若也走过渡，每条长消息出现时都会从全高「缩」到 6 行。 */
  const [animate, setAnimate] = useState(false);

  // 判定是否超长：用「行高 × 行数」换算像素阈值，宽度变化（窗口缩放、右面板折叠）时重新判定
  useLayoutEffect(() => {
    const el = textRef.current;
    if (!el) { setOverflow(false); return; }
    const measure = () => {
      const lh = parseFloat(getComputedStyle(el).lineHeight) || 24;
      const limit = lh * COLLAPSE_LINES;
      // 折叠态 scrollHeight 即全文高度；展开态 clientHeight 也是全文高度，二者取大
      setOverflow(Math.max(el.scrollHeight, el.clientHeight) > limit + 2);
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [text]);

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
    <div className="turn-user-bubble">
      <MessageFileCards atts={attachmentsOf(content)} />
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
        <button
          type="button"
          className={"ub-toggle" + (expanded ? " is-open" : "")}
          aria-expanded={expanded}
          aria-label={expanded ? "收起" : "展开全文"}
          onClick={toggle}
        >
          <IconChevronDown size={14} />
        </button>
      )}
    </div>
  );
}
