/** ClampBlock（plan-522-2209 R10）：工具输出的统一折叠块——全站只保留**一套**折叠契约。
 *
 * ── 为什么 ──
 * 此前「长输出怎么折、折了怎么标注」四处各写一套口径：`CodeView` 报「N 行 · 已省略前 X 字符」、
 * `ToolTree` 报「已省略前 N 字符」与「变更过大，已截断显示」、`ApprovalCard` 报「已省略 N 字」
 * （后者由组 E 迁移）。用户无法形成「折叠等于多少行」的稳定预期。本组件把规则收成一条：
 *
 *  1. **头尾切片，默认 16 行**：超限时保留头部 12 行 + 尾部 4 行，中间折叠。
 *     头部给上下文（这段输出在说什么），尾部给结论（报错、退出码、最后一条日志都在尾部）——
 *     此前只留尾部（`utils/tailClamp`）会把开头一起丢掉，用户看不出结果是从哪来的。
 *  2. **截断必须标注总量**：统一文案「显示 前 {head} 行 + 后 {tail} 行 / 共 {total} 行」，
 *     不再出现「已省略」「已截断」等多种句式。
 *  3. **折叠只影响显示，不影响复制**：传 `copyText` 才出现「复制完整内容」入口，
 *     它始终取调用方给的完整原文；数据源从不被截断。
 *  4. 展开/收起复用 `ChatCollapse`（grid-template-rows 真实高度过渡），
 *     禁止 `{open && <X/>}` 条件渲染——挂载即呈现会让折叠瞬间跳变。
 *
 * 未超限时原样渲染：不出现折叠按钮、不出现行数标注（避免噪音）。
 * 折叠态 DOM 里恒定只有 maxLines 行，渲染成本不随输出规模增长。
 */
import { useCallback, useMemo, useState, type ReactNode } from "react";
import { Button } from "../ui";
import { IconCopy } from "../icons";
import { notify } from "../../store/toast";
import { ChatCollapse } from "./ChatCollapse";

/** 按真实行数切行：`"a\nb\n"` 是 2 行——末尾换行会 split 出一个空串，
 *  不处理会让 total 永远多 1，标注立刻失真（plan-522-2209 R10）。 */
export function splitClampLines(text: string): string[] {
  if (!text) return [];
  const lines = text.split("\n");
  if (lines.length > 1 && lines[lines.length - 1] === "") lines.pop();
  return lines;
}

/** 片段的定位信息：折叠把头 / 中 / 尾切成三段分别渲染，
 *  调用方（如 `CodeView` 的行号槽、`ToolTree` 的 diff 行着色）靠 `startLine` 续上真实下标，
 *  不必重新数前面的行。 */
export interface ClampBlockRenderInfo {
  /** 该片段第一行在全文中的下标（0-based） */
  startLine: number;
  /** 调用方透传的语言标签（渲染提示用，不参与切片） */
  lang?: string;
}

export interface ClampBlockProps {
  /** 完整文本（按 \n 切行后切片） */
  text: string;
  /** 行数上限，默认 16（头 12 + 尾 4） */
  maxLines?: number;
  /** 头部保留行数，默认 12；超出 maxLines 时收到 maxLines */
  headLines?: number;
  /** 片段渲染方式；缺省是等宽 <pre>。
   *  折叠态调用两次（头段、尾段），展开后多一次中段——片段外面不要再包滚动容器 / 边框，
   *  否则折叠行会把内容切成几个各自滚动的盒子。 */
  render?: (visible: string, expanded: boolean, info: ClampBlockRenderInfo) => ReactNode;
  /** 复制所用的完整原文；不传则不显示复制入口 */
  copyText?: string;
  /** 外层 className（用于沿用各调用点的既有排版） */
  className?: string;
  /** 语言标签（如 'bash' / 'diff'），经 render 的 info.lang 透传 */
  lang?: string;
  /** 缺省 <pre> 额外带上的类名（如工具输出区的 tc-plain / tc-live），只在没传 render 时生效 */
  preClassName?: string;
}

const DEFAULT_MAX_LINES = 16;
const DEFAULT_HEAD_LINES = 12;

export function ClampBlock({
  text,
  maxLines = DEFAULT_MAX_LINES,
  headLines = DEFAULT_HEAD_LINES,
  render,
  copyText,
  className,
  lang,
  preClassName,
}: ClampBlockProps) {
  const [expanded, setExpanded] = useState(false);
  // 中段渲染开关：展开过一次就永久置位。折叠时不能把内容变 null 再让 ChatCollapse 收起——
  // 那会「瞬间消失」（违反禁止 0 动画切换）；真正的卸载由 ChatCollapse 的 transitionend 负责。
  // 置位必须与 setExpanded 同批提交（见 toggleOpen），否则首次展开的第一帧内容还是空的，
  // 高度过渡会从一个空盒子开始，内容随后才"涨"出来。
  const [midReady, setMidReady] = useState(false);

  const lines = useMemo(() => splitClampLines(text), [text]);
  const total = lines.length;
  // headLines 收进 [0, maxLines]：调用方给大了只折中间，给 0 就退化成「只留尾部」
  const head = Math.min(Math.max(headLines, 0), Math.max(maxLines, 0));
  const tail = Math.max(maxLines - head, 0);
  const over = total > maxLines;

  const paint = useCallback(
    (slice: string[], startLine: number, open: boolean) => (
      render
        ? render(slice.join("\n"), open, { startLine, lang })
        : (
          <pre className={preClassName ? `tc-clamp-pre ${preClassName}` : "tc-clamp-pre"}>
            {slice.join("\n")}
          </pre>
        )
    ),
    [render, lang, preClassName],
  );

  const headNode = useMemo(
    () => (over ? paint(lines.slice(0, head), 0, expanded) : paint(lines, 0, true)),
    [over, paint, lines, head, expanded],
  );
  const midNode = useMemo(
    () => (over && midReady ? paint(lines.slice(head, total - tail), head, true) : null),
    [over, midReady, paint, lines, head, tail, total],
  );
  const tailNode = useMemo(
    () => (over && tail > 0 ? paint(lines.slice(total - tail), total - tail, expanded) : null),
    [over, paint, lines, total, tail, expanded],
  );

  const copyAll = () => {
    if (copyText == null) return;
    navigator.clipboard.writeText(copyText).then(
      () => notify.success("已复制完整内容"),
      // 剪贴板不可用（非安全上下文 / 权限被拒）：明确告知，不静默假装成功
      () => notify.error("复制失败：剪贴板不可用"),
    );
  };

  /** 展开时同步置位中段开闸：两者同一批提交，第一帧就有内容可播高度过渡；
   *  收起时只关 expanded，内容留给 ChatCollapse 播完过渡再卸载。
   *  不能在 setExpanded 的 updater 里顺带置位——updater 必须是纯函数（StrictMode 会重放）。 */
  const toggleOpen = () => {
    if (!expanded) setMidReady(true);
    setExpanded(!expanded);
  };

  const rootCls = "tc-clamp" + (className ? " " + className : "");
  // 未超限：原样一整段，没有任何折叠痕迹
  if (!over) return <div className={rootCls}>{headNode}</div>;

  return (
    <div className={rootCls}>
      {headNode}
      {/* 折叠行：整行一个按钮，跨在头段与尾段之间。位置固定在头段之后——
          而不是浮在内容上，展开后尾部内容不重排，用户视线不跳。 */}
      <button
        type="button"
        className="tc-clamp-fold"
        aria-expanded={expanded}
        onClick={toggleOpen}
      >
        <span className="tc-clamp-fold-text">
          {expanded
            ? `共 ${total} 行，已全部显示`
            : `显示 前 ${head} 行 + 后 ${tail} 行 / 共 ${total} 行`}
        </span>
        <span className="tc-clamp-fold-act">{expanded ? "收起" : `展开中间 ${total - maxLines} 行`}</span>
      </button>
      <ChatCollapse open={expanded}>{midNode}</ChatCollapse>
      {tailNode}
      {copyText != null && (
        // 折叠只影响显示：复制入口始终给完整原文
        <div className="tc-clamp-foot">
          <Button variant="ghost" size="xs" icon={<IconCopy size={12} />} onClick={copyAll}>
            复制完整内容
          </Button>
        </div>
      )}
    </div>
  );
}
