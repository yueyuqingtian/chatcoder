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

// ── plan-549-2281：子代理提问 / 汇报 ──────────────────────────────
// 子会话下线后，提问与汇报全部来自子代理。它们与子会话那套共用同一行版式，
// 但来源是「子代理 #N「任务标题」」，动作是「打开执行过程」（右面板）而不是「前往子会话」。
/** 子代理提问的伪工具名 */
export const SUBAGENT_QUESTION_TOOL = "subagent_question";
/** 子代理进度上报的伪工具名 */
export const SUBAGENT_REPORT_TOOL = "subagent_report";
/** 子代理完成通知的伪工具名 */
export const SUBAGENT_COMPLETION_TOOL = "subagent_completion";
/** 三类子代理通知共用一行渲染 */
export const SUBAGENT_NOTICE_TOOLS: ReadonlySet<string> = new Set([
  SUBAGENT_QUESTION_TOOL, SUBAGENT_REPORT_TOOL, SUBAGENT_COMPLETION_TOOL,
]);

/** 子代理头部标记：「【子代理提问 subagent#12「标题」】问题」；完成通知是
 *  「【子代理已完成/失败/已取消 subagent#12「标题」】…」（plan-549-2281 补：后端
 *  format_completion_row 落库成行）。标题可缺省，编号必须有。 */
const SUBAGENT_HEAD_RE = /^\s*【子代理(提问|进度上报|已完成|失败|已取消)\s*subagent#(\d+)(?:「([\s\S]*?)」)?】\s*/;

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

// ── plan-20261022-release-ci：子会话汇报 / 结论 ──────────────────────────
// 此前这两类系统消息在主会话里按普通正文平铺——结论原文动辄几千字，一个子会话结束就在主会话里铺满一屏。
// 现在与提问同一套行版式：行内「子会话已完成「标题」」，展开看结论原文，可一键前往子会话，随工作过程折叠。

/** 进度汇报的伪工具名 */
export const SUBSESSION_REPORT_TOOL = "subsession_report";
/** 一轮结束的结论的伪工具名 */
export const SUBSESSION_RESULT_TOOL = "subsession_result";
/** 三类子会话通知（提问 / 汇报 / 结论）共用 SubsessionQuestionRow 渲染 */
export const SUBSESSION_NOTICE_TOOLS: ReadonlySet<string> = new Set([
  SUBSESSION_QUESTION_TOOL, SUBSESSION_REPORT_TOOL, SUBSESSION_RESULT_TOOL,
]);

/** 头部：可选「[子会话汇报]」前缀 +「标题」+ 汇报 / 结束 + 冒号 */
const NOTICE_HEAD_RE = /^\s*(?:\[子会话汇报\]\s*)?(?:「([\s\S]*?)」\s*)?(汇报|结束)\s*[：:]/;
/** 结论正文前的「turn 12 已完成。结论原文：」——取出状态文字，正文从冒号之后开始 */
const RESULT_LEAD_RE = /^\s*turn\s*\d+\s*([^。\n]*)。\s*结论原文\s*[：:]\s*/;

export interface ParsedSubsessionNotice {
  /** 来源子会话标题（解析不出为空串） */
  title: string;
  kind: "report" | "result";
  /** 结论的结束状态（已完成 / 执行失败 / 被中断 …），汇报为空串 */
  status: string;
  /** 汇报正文 / 结论原文（解析不出时为原文，绝不丢内容） */
  body: string;
}

export function parseSubsessionNotice(raw: string): ParsedSubsessionNotice {
  const text = (raw ?? "").trim();
  const head = NOTICE_HEAD_RE.exec(text);
  if (!head) return { title: "", kind: "report", status: "", body: text };
  const kind = head[2] === "结束" ? "result" : "report";
  let body = text.slice(head[0].length).trim();
  let status = "";
  if (kind === "result") {
    const lead = RESULT_LEAD_RE.exec(body);
    if (lead) {
      status = lead[1].trim();
      body = body.slice(lead[0].length).trim();
    }
  }
  return { title: (head[1] ?? "").trim(), kind, status, body: body || text };
}

// ── plan-549-2281：子代理提问 / 汇报 / 完成通知的解析 ──────────────────────
// 后端格式（见 server/app/orchestration/subagent_comm.publish_question、
// engine._on_agent_report 与 subagent.format_completion_row）：
//   【子代理提问 subagent#12「修 bug」】要改 A 还是 B？
//   （答复：send_to_subagent 12）
//   【子代理进度上报 subagent#12】…
//   【子代理已完成 subagent#12「修 bug」】结论首段
// 尾部「（答复：…）」是给主代理的工具调用说明，不属于提问内容，要截掉。
export interface ParsedSubagentNotice {
  /** 来源子代理 id（解析不出为 null） */
  agentId: number | null;
  /** 提问 / 进度上报 / 完成通知 */
  kind: "question" | "report" | "completion";
  /** 派发时的任务标题（解析不出为空串） */
  title: string;
  /** 结束状态（已完成 / 失败 / 已取消）；提问与进度上报为空串，语义与 ParsedSubsessionNotice.status 对齐。
   *  plan-560-2311 E2：此前头部捕获组被丢弃，渲染层拿不到状态，失败 / 取消的行与成功行长得一样。 */
  status: string;
  /** 正文（已去掉头部标记与尾部答复说明；头部解析不出时为原文） */
  body: string;
}

/** 尾部答复说明行：本行及其后全部丢弃。 */
const SUBAGENT_REPLY_HINT_RE = /^\s*（?答复[：:]/;

export function parseSubagentNotice(raw: string): ParsedSubagentNotice {
  const text = (raw ?? "").trim();
  const head = SUBAGENT_HEAD_RE.exec(text);
  if (!head) {
    // 兜底：解析不出头部时整段当正文，绝不丢内容
    return { agentId: null, kind: "report", title: "", status: "", body: text };
  }
  const kind = head[1] === "提问" ? "question"
    : head[1] === "进度上报" ? "report" : "completion";
  const agentId = Number(head[2]) || null;
  const title = (head[3] ?? "").trim();
  // 状态词只对完成通知有意义（提问 / 进度上报的「提问」「进度上报」本身就是 kind，
  // 拿到动词位置上会变成「子代理提问」这种读不通的句子）。
  const status = kind === "completion" ? head[1].trim() : "";
  const lines = text.slice(head[0].length).split(/\r?\n/);
  const hint = lines.findIndex((l) => SUBAGENT_REPLY_HINT_RE.test(l));
  if (hint >= 0) lines.length = hint;
  // plan-560-2311 E2：头部命中却无正文时返回空串，**绝不**回退整段原文——
  // 回退会把「【子代理已取消 subagent#12「修 bug」】」这种内部协议头直接摆给用户看。
  // 界面层对空正文给出用户可读占位（见 SubsessionQuestionRow）。
  return { agentId, kind, title, status, body: lines.join("\n").trim() };
}
