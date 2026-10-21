/** plan-20261021-subq-row：子会话提问的文本解析与展示用纯函数。
 *
 * 提问是后端写入的系统消息，正文只有 content.text 一份压缩文本：
 *   [子会话汇报]「标题」提问（question_id=q_…）：问题正文
 *   答复方式：send_to_subsession(…)。……
 * 头部标记之后才是问题本身；尾部「答复方式：…」是给主代理的工具调用说明，不是提问内容，要截掉。
 * 旧消息 / 文案变动时解析不出头部，一律退化为原样展示，绝不丢内容。
 */

/** 时间线里承载「子会话提问」的伪工具名（与 bg_command_done 同属消息流伪工具，只用于展示）。 */
export const SUBSESSION_QUESTION_TOOL = "subsession_question";

/** 头部标记：可选的「[子会话汇报]」前缀 + 「标题」+ 提问（question_id=…）：。标题用惰性匹配，靠后面的固定锚点收尾。 */
const HEAD_RE = /^\s*(?:\[子会话汇报\]\s*)?(?:「([\s\S]*?)」\s*)?提问\s*（question_id=[^）]*）\s*[：:]/;

/** 工具调用说明行：本行及其后全部丢弃。 */
const REPLY_HINT_RE = /^\s*答复方式\s*[：:]/;

export interface ParsedSubsessionQuestion {
  /** 提问的子会话标题（取自头部标记，解析不出为空串） */
  title: string;
  /** 问题正文（已去掉头部标记与尾部答复说明；整段解析不出时为原文） */
  body: string;
}

export function parseSubsessionQuestion(raw: string): ParsedSubsessionQuestion {
  const text = (raw ?? "").trim();
  if (!text) return { title: "", body: "" };

  let title = "";
  let rest = text;
  const head = HEAD_RE.exec(text);
  if (head) {
    title = (head[1] ?? "").trim();
    rest = text.slice(head[0].length);
  } else {
    // 兜底：旧文案没有标准头部时，取「）：」或第一个冒号之后；都没有就整段当正文
    const cut = text.indexOf("）：");
    const colon = text.indexOf("：");
    if (cut >= 0) rest = text.slice(cut + 2);
    else if (colon >= 0) rest = text.slice(colon + 1);
  }

  const lines = rest.split(/\r?\n/);
  const hint = lines.findIndex((l) => REPLY_HINT_RE.test(l));
  if (hint >= 0) lines.length = hint;
  const body = lines.join("\n").trim();
  return { title, body: body || text };
}

/** 行内标记的切片：普通文字 / 行内代码 / 加粗。 */
export type InlineSpan = { kind: "text" | "code" | "bold"; text: string };

/** 把提问正文里最常见的两种行内标记（`代码` 与 **加粗**）切开，展开区据此渲染；
 *  其余一律保持原文——提问是自然语言，不值得为它引入整套 Markdown 渲染。 */
export function splitInlineMarks(text: string): InlineSpan[] {
  const spans: InlineSpan[] = [];
  const re = /`([^`\n]+)`|\*\*([^*\n]+)\*\*/g;
  let last = 0;
  for (let m = re.exec(text); m; m = re.exec(text)) {
    if (m.index > last) spans.push({ kind: "text", text: text.slice(last, m.index) });
    if (m[1] != null) spans.push({ kind: "code", text: m[1] });
    else spans.push({ kind: "bold", text: m[2] });
    last = m.index + m[0].length;
  }
  if (last < text.length) spans.push({ kind: "text", text: text.slice(last) });
  return spans;
}
