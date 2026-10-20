/** plan-474-2105：子会话提问卡。
 *
 * 为什么做成只读卡片而不是审批卡：子会话的提问是「需要主代理/用户回一句」的提示，
 * 不是对某个工具的授权确认；卡片的处置动作是「回到那个子会话看上下文」，
 * 所以这里只提供来源跳转，不提供确认/拒绝按钮。
 *
 * 正文来源只有 content.text（后端把它压缩成一行摘要），因此解析必须容错：
 * 头部标记（[子会话汇报]…提问（question_id=…）：）之后才是问题正文，
 * 尾部「答复方式：…」是工具调用说明而非提问内容，一并截断；解析不出来就原样展示。
 */
import { memo, useMemo } from "react";
import type { MessageOut } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { Button } from "../ui";
import { IconAlertTriangle } from "../icons";

/** 与 toolcard/ToolCardSections 的 useSubsessionTitle 同源：跨父会话桶按 id 查标题。 */
function useSubsessionTitle(id: number | null): string {
  return useChatStore((s) => {
    if (id == null) return "";
    for (const list of Object.values(s.subsessionsByParent)) {
      const hit = list.find((c) => c.id === id);
      if (hit) return hit.title || `子会话 #${id}`;
    }
    return `子会话 #${id}`;
  });
}

/** 从 content.text 提取提问正文：取「：」之后，截断「答复方式：」所在行及其后。 */
export function questionBodyOf(raw: string): string {
  const text = raw.trim();
  if (!text) return "";
  // 优先定位头部标记「（question_id=…）：」，避免正文里的第一个冒号抢位
  const head = text.match(/）：/);
  let body = text;
  if (head && head.index != null) body = text.slice(head.index + 2);
  else {
    const i = text.indexOf("：");
    if (i >= 0) body = text.slice(i + 1);
  }
  const lines = body.split(/\r?\n/);
  const cut = lines.findIndex((l) => /^\s*答复方式\s*[：:]/.test(l));
  if (cut >= 0) lines.length = cut;
  const joined = lines.join("\n").trim();
  return joined || text;
}

function asNumber(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

export const SubsessionQuestionCard = memo(function SubsessionQuestionCard({ msg }: { msg: MessageOut }) {
  const content = (msg.content ?? {}) as Record<string, unknown>;
  const mailbox = (content.mailbox && typeof content.mailbox === "object"
    ? content.mailbox
    : {}) as Record<string, unknown>;
  // 来源会话：优先 mailbox.source_session_id，兜底 content.subsession_id（旧数据）
  const sourceId = asNumber(mailbox.source_session_id) ?? asNumber(content.subsession_id);
  const questionId = typeof content.question_id === "string" ? content.question_id : "";
  const rawText = typeof content.text === "string" ? content.text : "";
  const body = useMemo(() => questionBodyOf(rawText), [rawText]);

  const title = useSubsessionTitle(sourceId);
  const switchSession = useChatStore((s) => s.switchSession);

  return (
    <div className="sqc-card">
      <div className="sqc-head">
        <span className="sqc-head-icon"><IconAlertTriangle size={13} /></span>
        <span className="sqc-title">子会话提问</span>
        {sourceId != null && (
          <Button variant="subtle" size="xs" onClick={() => void switchSession(sourceId)}>
            {`「${title}」`}
          </Button>
        )}
      </div>
      {/* 空正文不渲染，避免 flex gap 多占一份间距 */}
      {body ? <div className="sqc-body">{body}</div> : null}
      {questionId ? <div className="sqc-note">{`question_id：${questionId}`}</div> : null}
    </div>
  );
});
