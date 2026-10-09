/** v36 (plan-321-1600 M2): 子代理结构化汇报卡片——仅右面板启用，主流消息渲染不变。
 *
 * 子代理最终汇报按固定六节输出（Result / Files Touched / Key Findings /
 * Verification / Acceptance / Risks）。这里把分节渲染为结构化视图：
 *  - 变更文件 → 文件芯片（点击在右面板打开预览）；
 *  - 验证 / 验收 → 勾/叉列表（验收按"未满足/not met"措辞判叉）；
 *  - 风险 → 警示块（无风险不渲染）；
 *  - 结果 / 关键发现 → 保留 markdown 排版。
 * 解析不到任何分节时**原样渲染 markdown**（兜底，绝不吞内容）。
 */
import { memo, useMemo } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { usePanelStore } from "../../store/panel";
import { MarkdownContent, markdownComponents } from "../MarkdownContent";
import { FileBadge, splitFilePath } from "./FileBadge";
import { IconAlertCircle, IconCheck, IconChecklist, IconFlask, IconFileRead, IconX } from "../icons";

/** 文件路径识别（与后端 subagent.py 的 _FILE_RE 同口径，仅用于展示） */
const FILE_RE =
  /[\w./\\:-]+\.(?:tsx|ts|json|jsx|js|py|go|rs|java|cs|cpp|c|h|rb|php|sql|md|yaml|yml|toml|svelte|scss|less|css|html|htm|vue|xml|txt|ini|cfg|sh|bat|ps1)(?![\w])/g;

/** 空值占位（子代理被要求写 None） */
const isEmptyItem = (s: string) => !s || /^(none|n\/a|null|-)$/i.test(s.trim());

/** 行内 Markdown（v42）：「图标 + 文本」的行结构专用——
 *  复用主消息流的 markdown 组件映射，只把块级容器（p / ul / ol / li）内联化。
 *  目的：条目里的 `代码`、**加粗**、链接按主消息流样式渲染；
 *  此前这些条目是纯文本，反引号原样裸露（终态汇报"丑陋"的来源之一）。
 *
 *  plan-387-1825 D1-2：**必须同时接管 `pre`**。此前只内联化 p/ul/ol/li，
 *  `pre` 仍是块级元素（CodeBlockWrapper 内含 div 头部 + pre），被塞进 <li> 的
 *  内联流后布局裂开——语言标签框与代码框分离、代码行落在两框之间（用户截图实况）。 */
const inlineComponents: Components = {
  ...markdownComponents,
  p: ({ children }) => <>{children}</>,
  ul: ({ children }) => <>{children}</>,
  ol: ({ children }) => <>{children}</>,
  li: ({ children }) => <>{children}</>,
  // plan-387-1825 D1-2：**刻意不覆盖 `pre`**。
  // 覆盖成 Fragment 会剥掉 CodeBlockWrapper（div.md-code-block > 头部 + pre），
  // 只剩一个裸 <pre> —— 那正是「语言标签框与代码框分离、内容散落」的成因。
  // 正确做法是让 pre 沿用 markdownComponents 的 CodeBlockWrapper（块级完整渲染），
  // 由 items() 的围栏感知拆分保证围栏成对、不会散架（见下方 items 实现）。
};

const InlineMd = memo(function InlineMd({ text }: { text: string }) {
  return (
    <div className="md-body">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={inlineComponents}>{text}</ReactMarkdown>
    </div>
  );
});

interface Report {
  result: string;
  findings: string;
  files: string[];
  verification: string[];
  acceptance: string[];
  risks: string[];
}

/** 分节标题识别——顺序敏感（先匹配更具体的节，避免被泛化规则吃掉）。 */
const HEAD_PATTERNS: Array<[keyof Report, RegExp]> = [
  ["files", /(变更文件|改动文件|files?\s*(?:touched|changed|modified)|files\s*[:：])/i],
  ["risks", /(风险|未决|待确认|risks?|blockers?|open\s+questions?)/i],
  ["verification", /(验证|校验|verification|verified)/i],
  ["acceptance", /(验收|acceptance)/i],
  ["findings", /(关键发现|key\s+findings|findings)/i],
  ["result", /(结果|结论|results?|summary)/i],
];

/** 把汇报文本切成结构化字段；未识别到任何分节时返回 null（触发兜底原样渲染）。 */
function parseReport(text: string): Report | null {
  const out: Report = {
    result: "", findings: "", files: [], verification: [], acceptance: [], risks: [],
  };
  const buffers: Record<string, string[]> = {};
  let section: keyof Report | "" = "";
  let matched = 0;

  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    const isHead = line.startsWith("#") || (line.length <= 72 && /[:：]$/.test(line));
    if (isHead) {
      const headText = line.replace(/^#+/, "").trim();
      if (headText.length <= 72) {
        const hit = HEAD_PATTERNS.find(([, re]) => re.test(headText));
        if (hit) {
          section = hit[0];
          matched += 1;
          continue;
        }
      }
    }
    if (!section) continue;
    (buffers[section] ||= []).push(line);
  }

  if (matched === 0) return null;

  const text_ = (k: keyof Report) => (buffers[k] || []).join("\n").trim();
  /**
   * plan-387-1825 D1-1：**围栏感知**的条目拆分。
   * 此前按行拆分，多行代码围栏被切成散行、``` 标记丢失，
   * 每行各自走一次 InlineMd → 围栏结构彻底破坏（用户截图：TS 空框 + 代码行散落 + CODE 空框）。
   * 现在按 ``` 成对扫描：围栏内的所有行（含首尾标记）合并为**一个条目**，原样保留。
   */
  const items = (k: keyof Report): string[] => {
    const out: string[] = [];
    let fence: string[] | null = null; // 非 null 表示正处于围栏内
    for (const raw of buffers[k] || []) {
      const t = raw.trim();
      if (/^```/.test(t)) {
        if (fence) { fence.push(raw); out.push(fence.join("\n")); fence = null; }
        else fence = [raw];
        continue;
      }
      if (fence) { fence.push(raw); continue; }
      const line = t.replace(/^[-*·•]\s*/, "").replace(/^\d+[.)]\s*/, "").trim();
      if (line && !isEmptyItem(line)) out.push(line);
    }
    // 围栏未闭合（子代理输出被截断）：整段并入最后一个条目，绝不丢内容
    if (fence) out.push(fence.join("\n"));
    return out.filter((l) => l && !isEmptyItem(l));
  };

  out.result = text_("result");
  out.findings = text_("findings");
  out.risks = items("risks");
  out.verification = items("verification");
  out.acceptance = items("acceptance");
  out.files = [...new Set((buffers["files"] || []).flatMap((l) => l.match(FILE_RE) || []))];
  return out;
}

/** 验收条目是否"未达成"——按常见否定措辞判叉（判错时仍显示原文，不影响可读性）。 */
const isNotMet = (s: string) =>
  /(not\s+met|unsatisfied|unmet|fail(ed|ure)?|未(满足|达标|通过|完成)|不(满足|达标|通过))/i.test(s);

export const SubagentReportCard = memo(function SubagentReportCard({ text }: { text: string }) {
  const report = useMemo(() => parseReport(text), [text]);
  const setPreviewPath = usePanelStore((s) => s.setPreviewPath);
  const openTab = usePanelStore((s) => s.openTab);

  // 兜底：解析不出分节（非结构化汇报）时保持原样 markdown，绝不吞内容
  if (!report) {
    return (
      <div className="subagent-report subagent-report-raw turn-agent-text">
        <MarkdownContent>{text}</MarkdownContent>
      </div>
    );
  }

  return (
    <div className="subagent-report turn-agent-text">
      {report.result && (
        <section className="sr-sec">
          <div className="sr-sec-head"><IconChecklist size={13} /><span>执行结果</span></div>
          <MarkdownContent>{report.result}</MarkdownContent>
        </section>
      )}

      {report.files.length > 0 && (
        <section className="sr-sec">
          <div className="sr-sec-head">
            <IconFileRead size={13} /><span>变更文件</span>
            <span className="sr-count">{report.files.length}</span>
          </div>
          <div className="sr-files">
            {report.files.map((f) => (
              <button
                key={f}
                type="button"
                className="sr-file"
                title={`${f}（点击查看文件）`}
                onClick={() => { setPreviewPath(f); openTab("files"); }}
              >
                <FileBadge path={f} size={14} />
                <span className="sr-file-name">{splitFilePath(f).name}</span>
              </button>
            ))}
          </div>
        </section>
      )}

      {report.findings && (
        <section className="sr-sec">
          <div className="sr-sec-head"><IconAlertCircle size={13} /><span>关键发现</span></div>
          <MarkdownContent>{report.findings}</MarkdownContent>
        </section>
      )}

      {report.verification.length > 0 && (
        <section className="sr-sec">
          <div className="sr-sec-head"><IconFlask size={13} /><span>验证</span></div>
          <ul className="sr-list">
            {report.verification.map((v, i) => (
              <li key={i}>
                <span className={"sr-mark " + (isNotMet(v) ? "no" : "plain")}>
                  <IconFlask size={12} />
                </span>
                <InlineMd text={v} />
              </li>
            ))}
          </ul>
        </section>
      )}

      {report.acceptance.length > 0 && (
        <section className="sr-sec">
          <div className="sr-sec-head"><IconCheck size={13} /><span>验收</span></div>
          <ul className="sr-list">
            {report.acceptance.map((a, i) => {
              const ok = !isNotMet(a);
              return (
                <li key={i}>
                  <span className={"sr-mark " + (ok ? "ok" : "no")}>
                    {ok ? <IconCheck size={12} /> : <IconX size={12} />}
                  </span>
                  <InlineMd text={a} />
                </li>
              );
            })}
          </ul>
        </section>
      )}

      {report.risks.length > 0 && (
        <section className="sr-sec sr-risks">
          <div className="sr-sec-head"><IconAlertCircle size={13} /><span>风险 / 待确认</span></div>
          <ul className="sr-list">
            {report.risks.map((r, i) => (
              <li key={i}><span className="sr-mark warn"><IconAlertCircle size={12} /></span><InlineMd text={r} /></li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
});
