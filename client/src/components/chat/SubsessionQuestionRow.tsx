/** plan-20261021-subq-row：子会话提问行。
 *
 * 为什么是一行工具行而不是卡片：提问只是子会话与主代理之间的一次对话，对用户而言与「已调用工具」同属执行过程。
 * 此前做成常显卡片，任务进行中夹在工具行之间很突兀，完成后又整张留在汇报上方——工作过程折叠不收它。
 * 现在复用工具行的版式（图标 + 动词 + 来源标题 + 展开箭头），展开看提问全文，
 * 时间线里它是一条普通的 tools 条目（见 timeline.subsessionQuestionItem），完成后随工作过程一起折叠。
 *
 * 处置动作只有「前往子会话」——提问不是审批，没有确认 / 拒绝。
 * 展开状态按调用身份记在 flowExpand（消息流虚拟化与重新分组都会重挂载本组件）。
 *
 * plan-20261022-release-ci：子会话的进度汇报（subsession_report）与一轮结论（subsession_result）也走这一行——
 * 此前它们在主会话里按普通正文平铺，结论原文一铺就是一屏。结论按 Markdown 渲染（子会话的最终回复本身就是 Markdown），
 * 行内动词带上结束状态（子会话已完成 / 执行失败 / 被中断）。
 */
import { memo, useMemo, type ReactNode } from "react";
import type { ToolLeaf } from "./timeline";
import { useChatStore } from "../../store/chat";
import { usePanelStore } from "../../store/panel";
import { useFlowExpand } from "../../store/flowExpand";
import { ToolGlyph } from "../iconkit/ToolGlyphs";
import { IconChevronRight } from "../icons";
import { Button } from "../ui";
import { Markdown } from "../ui/Markdown";
import { ChatCollapse } from "./ChatCollapse";
import { toolLabel } from "./toolLabels";
import { SUBSESSION_RESULT_TOOL, SUBAGENT_COMPLETION_TOOL, SUBAGENT_NOTICE_TOOLS, splitInlineMarks, type InlineSpan } from "../../utils/subsessionQuestion";

/** 来源标题的统一取值：子会话取会话桶里的最新标题，子代理取 subagentMeta 的任务名。
 *  plan-549-2281：两者 id 语义不同（session id / agent id），但展示需求一致，
 *  因此合并成一个 hook——**不能按类型分别调用不同的 hook**（React 规则要求 hook 调用顺序稳定）。 */
function useNoticeTitle(
  kind: "subsession" | "subagent", id: number | null, asked: string,
): string {
  return useChatStore((s) => {
    if (id != null) {
      if (kind === "subsession") {
        for (const list of Object.values(s.subsessionsByParent)) {
          const hit = list.find((c) => c.id === id);
          if (hit?.title) return hit.title;
        }
      } else {
        const meta = s.subagentMeta[id];
        const name = String((meta as { name?: unknown } | undefined)?.name ?? "").trim();
        if (name) return name;
      }
    }
    const fallback = kind === "subsession" ? "子会话" : "子代理";
    return asked || (id != null ? `${fallback} #${id}` : fallback);
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
  const openSubagent = usePanelStore((s) => s.openSubagent);
  // plan-549-2281：同一行还要承载子代理的提问 / 进度上报。
  // 两类来源的 id 语义不同：子会话是 session id（可切换会话），子代理是 agent id（开右面板看执行过程）。
  const isSubagent = SUBAGENT_NOTICE_TOOLS.has(leaf.tool);
  const sourceId = isSubagent
    ? (typeof leaf.args.agent_id === "number" ? leaf.args.agent_id : null)
    : (typeof leaf.args.source_session_id === "number" ? leaf.args.source_session_id : null);
  const asked = typeof leaf.args.title === "string" ? leaf.args.title : "";
  // hook 必须无条件调用，因此把类型作为参数传进去（不能按分支调用不同 hook）
  const title = useNoticeTitle(isSubagent ? "subagent" : "subsession", sourceId, asked);
  // plan-560-2311 E2：子代理完成通知与子会话结论同属「有结果的结束」，展开区都按 Markdown 渲染——
  // 此前仅认 SUBSESSION_RESULT_TOOL，子代理失败 / 取消的正文被当成提问走 QuestionBody，代码块与列表全丢。
  const isResult = leaf.tool === SUBSESSION_RESULT_TOOL || leaf.tool === SUBAGENT_COMPLETION_TOOL;
  const status = typeof leaf.args.status === "string" ? leaf.args.status : "";
  // plan-20261025-shell（需求 1）：与工具行同一写法——两字动作词（结束行取状态：完成 / 失败 / 取消 / 中断）
  // + 弱色来源（子代理 / 子会话，不能共用，否则子代理的通知会显示成「子会话…」）+ 来源标题
  const verb = isResult && status
    ? (status.replace(/^(已|被|执行)/, "") || status)
    : toolLabel(leaf.tool).verb;
  const sourceName = isSubagent ? "子代理" : "子会话";
  // 头部命中但确实没有正文时给用户可读占位（解析层已不再回退整段原文，见 parseSubagentNotice）
  const body = leaf.output.trim() || "（无正文）";

  return (
    <div className={"tc-node tc-sq" + (isResult ? " is-result" : "")}>
      <button
        type="button"
        className={"tc-row has-output tc-sq-row" + (expanded ? " expanded" : "")}
        data-tool-state={leaf.ok === false ? "fail" : "done"}
        aria-expanded={expanded}
        onClick={() => setExpanded(!expanded)}
      >
        <span className="tc-icon"><ToolGlyph tool={leaf.tool} size={16} /></span>
        <span className="tc-verb">{verb}</span>
        <span className="tc-tool-name">{sourceName}</span>
        <span className="tc-sq-title">{title}</span>
        <span className={"tc-chevron" + (expanded ? " open" : "")}><IconChevronRight size={11} /></span>
      </button>
      <ChatCollapse open={expanded}>
        <div className="tc-output">
          {isResult
            ? <div className="tc-sq-md"><Markdown>{body}</Markdown></div>
            : <QuestionBody text={body} />}
          {sourceId != null && (
            <div className="tc-sq-foot">
              {isSubagent
                ? <Button variant="subtle" size="xs" onClick={() => openSubagent(sourceId, title)}>打开执行过程</Button>
                : <Button variant="subtle" size="xs" onClick={() => void switchSession(sourceId)}>前往子会话</Button>}
            </div>
          )}
        </div>
      </ChatCollapse>
    </div>
  );
});
