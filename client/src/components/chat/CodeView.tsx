/** 工具调用展开区的代码视图（plan-91-393）：类 VSCode 编辑器外观——
 * 顶部文件头（文件类型图标 + 文件名 + 行数）、左侧行号槽、语法高亮、等宽字体。
 * 高亮复用已有依赖 highlight.js（按需注册常用语言，不引入全量包）。
 *
 * plan-522-2209 R10：超长内容的折叠与截断标注统一交给 `ClampBlock`——
 * 此前这里自己算 `tailClamp` 再拼「N 行 · 已省略前 X 字符」，与工具树 / 审批卡是三套口径。
 * 本文件只保留「怎么解析成行」：`READ_HEADER` / `READ_TRUNC` 用于剥离 fs_read 的文件头与截断提示
 * （它们混进正文会让「整段带行号」判定失败、行号错位），`NUMBERED` 决定行号起点。 */
import { memo, useCallback, useMemo } from "react";
import { Tooltip } from "../ui/Tooltip";
import hljs from "highlight.js/lib/core";
import typescript from "highlight.js/lib/languages/typescript";
import javascript from "highlight.js/lib/languages/javascript";
import python from "highlight.js/lib/languages/python";
import json from "highlight.js/lib/languages/json";
import css from "highlight.js/lib/languages/css";
import xml from "highlight.js/lib/languages/xml";
import markdown from "highlight.js/lib/languages/markdown";
import bash from "highlight.js/lib/languages/bash";
import go from "highlight.js/lib/languages/go";
import rust from "highlight.js/lib/languages/rust";
import java from "highlight.js/lib/languages/java";
import cpp from "highlight.js/lib/languages/cpp";
import yaml from "highlight.js/lib/languages/yaml";
import sql from "highlight.js/lib/languages/sql";
import ini from "highlight.js/lib/languages/ini";
import kotlin from "highlight.js/lib/languages/kotlin";
import { FileIcon, baseName } from "../iconkit/FileIcon";
import { ClampBlock, splitClampLines, type ClampBlockRenderInfo } from "./ClampBlock";

// plan-20261016-ux（问题 5）：补 sql / ini（properties、toml、conf）/ kotlin——Java 项目调研时最常读的
// MyBatis 映射、配置文件此前全部按纯文本展示
const LANGS: Record<string, unknown> = { typescript, javascript, python, json, css, xml, markdown, bash, go, rust, java, cpp, yaml, sql, ini, kotlin };
for (const [k, v] of Object.entries(LANGS)) if (!hljs.getLanguage(k)) hljs.registerLanguage(k, v as never);

const EXT_LANG: Record<string, string> = {
  ts: "typescript", tsx: "typescript", mts: "typescript", cts: "typescript",
  js: "javascript", jsx: "javascript", mjs: "javascript", cjs: "javascript",
  py: "python", json: "json", jsonc: "json", css: "css", scss: "css", less: "css",
  html: "xml", htm: "xml", vue: "xml", xml: "xml", svg: "xml", xsd: "xml", wsdl: "xml", fxml: "xml",
  md: "markdown", sh: "bash", bash: "bash", ps1: "bash", bat: "bash", cmd: "bash",
  go: "go", rs: "rust", java: "java", kt: "kotlin", kts: "kotlin", gradle: "kotlin",
  c: "cpp", cpp: "cpp", cc: "cpp", h: "cpp", hpp: "cpp",
  yml: "yaml", yaml: "yaml", sql: "sql",
  properties: "ini", ini: "ini", toml: "ini", conf: "ini", cfg: "ini", env: "ini",
};

/** 读文件工具常见输出形如「   12→code」或「12: code」，带行号时剥离并沿用原行号。
 *  plan-20261016-ux（问题 5）：fs_read 的空行是「  13: 」，尾空格可能被裁掉，冒号后允许直接结束 */
const NUMBERED = /^\s*(\d+)(?:→|\t|:(?: |$)|\| )(.*)$/;
/** fs_read 输出首行的文件头（「文件: path (共 N 行) | 显示第 a-b 行」）与末尾截断提示——不是文件内容。
 *  此前它们混在正文里：首行不带行号导致「整段带行号」判定失败，文件自带的行号被当成代码显示、高亮也错乱 */
const READ_HEADER = /^文件: .+\(共 \d+ 行\)/;
const READ_TRUNC = /^\.\.\.\(已截断/;

export function langOf(path: string): string | null {
  return EXT_LANG[(path.split(".").pop() || "").toLowerCase()] ?? null;
}

/** 供其它视图（如 Git 差异）复用同一套语言注册与高亮口径。
 *  整块高亮后按行切分：同一段连续代码一起送进 hljs，跨行的块注释、模板字符串也能正确着色；
 *  未知扩展名返回 null，调用方退回纯文本。 */
export function highlightLines(path: string, lines: string[]): string[] | null {
  const lang = langOf(path);
  if (!lang || lines.length === 0) return null;
  try {
    const html = hljs.highlight(lines.join("\n"), { language: lang, ignoreIllegals: true }).value;
    return html.split("\n");
  } catch {
    return null;
  }
}

export const CodeView = memo(function CodeView({ path, text }: { path: string; text: string }) {
  // 解析成「正文行 + 行号起点」：剥离文件头 / 截断提示后再判「整段带行号」，
  // 保证行号槽与文件真实行号对齐（plan-522-2209 R10 保留了这段逻辑）
  const { body, start, total } = useMemo(() => {
    const raw = splitClampLines(text);
    if (raw.length > 1 && READ_HEADER.test(raw[0])) raw.shift();
    if (raw.length > 1 && READ_TRUNC.test(raw[raw.length - 1])) raw.pop();
    const m0 = NUMBERED.exec(raw[0] ?? "");
    const allNumbered = m0 && raw.every((l) => l === "" || NUMBERED.test(l));
    if (allNumbered) {
      return { body: raw.map((l) => NUMBERED.exec(l)?.[2] ?? "").join("\n"), start: Number(m0![1]), total: raw.length };
    }
    return { body: raw.join("\n"), start: 1, total: raw.length };
  }, [text]);
  // 行号槽宽度按**总行数**算，不随折叠态变化——否则展开 / 收起时整块内容会横向跳一格
  const gutterW = String(start + total).length;

  /** 片段渲染：ClampBlock 把内容切成头 / 中 / 尾三段分别调用，
   *  这里只负责把每段按行号槽渲染出来。片段各自高亮（跨片段的多行注释着色可能不连续），
   *  这是折叠本身造成的内容断裂，展开后仍是同一份被夹着折叠行的文本。 */
  const renderLines = useCallback((visible: string, _expanded: boolean, info: ClampBlockRenderInfo) => {
    const lines = visible.length > 0 ? visible.split("\n") : [];
    const htmlLines = highlightLines(path, lines);
    return (
      <>
        {lines.map((l, i) => (
          <div className="cv-line" key={info.startLine + i}>
            <span className="cv-ln">{start + info.startLine + i}</span>
            {htmlLines
              ? <code className="cv-code hljs" dangerouslySetInnerHTML={{ __html: htmlLines[i] || " " }} />
              : <code className="cv-code">{l || " "}</code>}
          </div>
        ))}
      </>
    );
  }, [path, start]);

  return (
    <div className="cv">
      <div className="cv-head">
        <FileIcon name={path} size={14} />
        <Tooltip content={path} side="top"><span className="cv-name">{baseName(path)}</span></Tooltip>
        {/* 文件属性（共 N 行）：截断说明由 ClampBlock 的折叠行统一给出，这里不再拼「已省略…」 */}
        <span className="cv-meta">{total} 行</span>
      </div>
      <div className="cv-body" style={{ ["--cv-gutter" as string]: `${gutterW + 2}ch` }}>
        <ClampBlock text={body} render={renderLines} lang={langOf(path) ?? undefined} copyText={text} />
      </div>
    </div>
  );
});
