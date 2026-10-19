/** 工作流通知：系统在工作流进入终态或需要用户决定时写入会话的一条进展提示。
 * 它不是用户发言，因此以可展开的工具调用行呈现（与“已调用 …”同一套样式）：
 * 行内只显示第一句，展开后是完整通知；不占用用户气泡的位置。
 */
import { useState } from "react";
import { IxWorkflow } from "../iconkit/StackIcons";
import { ChatCollapse } from "../chat/ChatCollapse";

const PREFIX = /^\[系统通知\]\s*/;

/** 标题：通知的第一句（到第一个逗号或句号为止），剥掉系统前缀与行首重复的“工作流”（行首标签已写明） */
export function noticeTitle(text: string): string {
  const body = text.replace(PREFIX, "").replace(/^工作流\s*/, "").trim();
  const cut = body.search(/[，。]/);
  return cut > 0 ? body.slice(0, cut) : body;
}

export function WorkflowNotice({ text }: { text: string }) {
  const [expanded, setExpanded] = useState(false);
  const body = text.replace(PREFIX, "").trim();
  const title = noticeTitle(text) || "工作流通知";
  return (
    <div className="tc-node wf-notice">
      <button
        type="button"
        className={"tc-row has-output wf-notice-row" + (expanded ? " expanded" : "")}
        aria-expanded={expanded}
        onClick={() => setExpanded((v) => !v)}
      >
        <span className="tc-icon"><IxWorkflow size={16} /></span>
        <span className="tc-verb">工作流</span>
        <span className="tc-query wf-notice-title">{title}</span>
      </button>
      {/* plan-20261019-motion-sweep：展开 / 收起走 ChatCollapse（此前是条件渲染，瞬间出现 / 消失） */}
      <ChatCollapse open={expanded}><div className="wf-notice-body">{body}</div></ChatCollapse>
    </div>
  );
}
