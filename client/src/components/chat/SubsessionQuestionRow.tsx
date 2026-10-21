/** plan-20261021-subq-row：子会话提问行。
 *
 * 为什么是一行工具行而不是卡片：提问只是子会话与主代理之间的一次对话，对用户而言与「已调用工具」同属执行过程。
 * 此前做成常显卡片，任务进行中夹在工具行之间很突兀，完成后又整张留在汇报上方——工作过程折叠不收它。
 * 现在复用工具行的版式（图标 + 动词 + 来源标题 + 展开箭头），展开看提问全文，
 * 时间线里它是一条普通的 tools 条目（见 timeline.subsessionQuestionItem），完成后随工作过程一起折叠。
 *
 * 处置动作只有「前往子会话」——提问不是审批，没有确认 / 拒绝。
 * 展开状态按调用身份记在 flowExpand（消息流虚拟化与重新分组都会重挂载本组件）。
 */
import { memo, useMemo, type ReactNode } from "react";
import type { ToolLeaf } from "./timeline";
import { useChatStore } from "../../store/chat";
import { useFlowExpand } from "../../store/flowExpand";
import { ToolGlyph } from "../iconkit/ToolGlyphs";
import { IconChevronRight } from "../icons";
import { Button } from "../ui";
import { ChatCollapse } from "./ChatCollapse";
import { toolVerb } from "./toolLabels";
import { SUBSESSION_QUESTION_TOOL, splitInlineMarks, type InlineSpan } from "../../utils/subsessionQuestion";

/** 来源子会话的标题：取最新的（子会话可能在提问之后被重命名），查不到时用提问当时的标题，再退到编号。
 *  返回原始字符串，selector 值不变就不重渲染。 */
function useSourceTitle(id: number | null, asked: string): string {
  return useChatStore((s) => {
    if (id != null) {
      for (const list of Object.values(s.subsessionsByParent)) {
        const hit = list.find((c) => c.id === id);
        if (hit?.title) return hit.title;
      }
    }
    return asked || (id != null ? `子会话 #${id}` : "子会话");
  });
}

/** 行内片段 → 节点；加粗里还可能嵌着代码（**没有 `memberPrice`**），递归一层即可（加粗内部不会再出现加粗）。 */
function renderSpans(spans: InlineSpan[]): ReactNode[] {
  return spans.map((sp, i) =>
    sp.kind === "code" ? <code key={i} className="tc-sq-code">{sp.text}</code>
      : sp.kind === "bold" ? <strong key={i}>{renderSpans(splitInlineMarks(sp.text))}</strong>
        : <span key={i}>{sp.text}</span>);
}

/** 提问正文：保留换行，识别 `代码` 与 **加粗** 两种行内标记，其余原样。 */
function QuestionBody({ text }: { text: string }) {
  const nodes = useMemo(() => renderSpans(splitInlineMarks(text)), [text]);
  return <div className="tc-sq-body">{nodes}</div>;
}

export const SubsessionQuestionRow = memo(function SubsessionQuestionRow({ leaf }: { leaf: ToolLeaf }) {
  const [expanded, setExpanded] = useFlowExpand(leaf.callKey);
  const switchSession = useChatStore((s) => s.switchSession);
  const sourceId = typeof leaf.args.source_session_id === "number" ? leaf.args.source_session_id : null;
  const asked = typeof leaf.args.title === "string" ? leaf.args.title : "";
  const title = useSourceTitle(sourceId, asked);

  return (
    <div className="tc-node tc-sq">
      <button
        type="button"
        className={"tc-row has-output tc-sq-row" + (expanded ? " expanded" : "")}
        data-tool-state="done"
        aria-expanded={expanded}
        onClick={() => setExpanded(!expanded)}
      >
        <span className="tc-icon"><ToolGlyph tool={SUBSESSION_QUESTION_TOOL} size={16} /></span>
        <span className="tc-verb">{toolVerb(SUBSESSION_QUESTION_TOOL)}</span>
        <span className="tc-sq-title">{`「${title}」`}</span>
        <span className={"tc-chevron" + (expanded ? " open" : "")}><IconChevronRight size={11} /></span>
      </button>
      <ChatCollapse open={expanded}>
        <div className="tc-output">
          <QuestionBody text={leaf.output} />
          {sourceId != null && (
            <div className="tc-sq-foot">
              <Button variant="subtle" size="xs" onClick={() => void switchSession(sourceId)}>前往子会话</Button>
            </div>
          )}
        </div>
      </ChatCollapse>
    </div>
  );
});
