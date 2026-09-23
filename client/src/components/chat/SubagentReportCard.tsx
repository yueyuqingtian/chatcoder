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
import { usePanelStore } from "../../store/panel";
import { MarkdownContent } from "../MarkdownContent";
import { FileBadge, splitFilePath } from "./FileBadge";
import { IconAlertCircle, IconCheck, IconChecklist, IconFlask, IconFileRead, IconX } from "../icons";

/** 文件路径识别（与后端 subagent.py 的 _FILE_RE 同口径，仅用于展示） */
const FILE_RE =
  /[\w./\\:-]+\.(?:tsx|ts|json|jsx|js|py|go|rs|java|cs|cpp|c|h|rb|php|sql|md|yaml|yml|toml|svelte|scss|less|css|html|htm|vue|xml|txt|ini|cfg|sh|bat|ps1)(?![\w])/g;

/** 空值占位（子代理被要求写 None） */
const isEmptyItem = (s: string) => !s || /^(none|n\/a|null|-)$/i.test(s.trim());

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
  const items = (k: keyof Report): string[] =>
    (buffers[k] || [])
      .map((l) => l.replace(/^[-*·•]\s*/, "").replace(/^\d+[.)]\s*/, "").trim())
      .filter((l) => l && !isEmptyItem(l));

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
      <div className="subagent-report subagent-report-raw">
        <MarkdownContent>{text}</MarkdownContent>
      </div>
    );
  }

  return (
    <div className="subagent-report">
      {report.result && (
        <section className="sr-sec">
          <div className="sr-sec-head"><IconChecklist size={12} /><span>执行结果</span></div>
          <MarkdownContent>{report.result}</MarkdownContent>
        </section>
      )}

      {report.files.length > 0 && (
        <section className="sr-sec">
          <div className="sr-sec-head">
            <IconFileRead size={12} /><span>变更文件</span>
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
          <div className="sr-sec-head"><IconAlertCircle size={12} /><span>关键发现</span></div>
          <MarkdownContent>{report.findings}</MarkdownContent>
        </section>
      )}

      {report.verification.length > 0 && (
        <section className="sr-sec">
          <div className="sr-sec-head"><IconFlask size={12} /><span>验证</span></div>
          <ul className="sr-list">
            {report.verification.map((v, i) => (
              <li key={i}>
                <span className={"sr-mark " + (isNotMet(v) ? "no" : "plain")}>
                  <IconFlask size={11} />
                </span>
                <span>{v}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {report.acceptance.length > 0 && (
        <section className="sr-sec">
          <div className="sr-sec-head"><IconCheck size={12} /><span>验收</span></div>
          <ul className="sr-list">
            {report.acceptance.map((a, i) => {
              const ok = !isNotMet(a);
              return (
                <li key={i}>
                  <span className={"sr-mark " + (ok ? "ok" : "no")}>
                    {ok ? <IconCheck size={11} /> : <IconX size={11} />}
                  </span>
                  <span>{a}</span>
                </li>
              );
            })}
          </ul>
        </section>
      )}

      {report.risks.length > 0 && (
        <section className="sr-sec sr-risks">
          <div className="sr-sec-head"><IconAlertCircle size={12} /><span>风险 / 待确认</span></div>
          <ul className="sr-list">
            {report.risks.map((r, i) => (
              <li key={i}><span className="sr-mark warn"><IconAlertCircle size={11} /></span><span>{r}</span></li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
});
