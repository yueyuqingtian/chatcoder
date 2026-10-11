/** 工具调用展开区的代码视图（plan-91-393）：类 VSCode 编辑器外观——
 * 顶部文件头（文件类型图标 + 文件名 + 行数）、左侧行号槽、语法高亮、等宽字体。
 * 高亮复用已有依赖 highlight.js（按需注册常用语言，不引入全量包）；超长内容仍走 tailClamp 尾部截断。 */
import { memo, useMemo } from "react";
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
import { FileIcon, baseName } from "../iconkit/FileIcon";
import { tailClamp } from "../../utils/tailClamp";

const LANGS: Record<string, unknown> = { typescript, javascript, python, json, css, xml, markdown, bash, go, rust, java, cpp, yaml };
for (const [k, v] of Object.entries(LANGS)) if (!hljs.getLanguage(k)) hljs.registerLanguage(k, v as never);

const EXT_LANG: Record<string, string> = {
  ts: "typescript", tsx: "typescript", js: "javascript", jsx: "javascript", mjs: "javascript", cjs: "javascript",
  py: "python", json: "json", css: "css", scss: "css", html: "xml", vue: "xml", xml: "xml", svg: "xml",
  md: "markdown", sh: "bash", ps1: "bash", go: "go", rs: "rust", java: "java", c: "cpp", cpp: "cpp", h: "cpp",
  yml: "yaml", yaml: "yaml",
};

/** 读文件工具常见输出形如「   12→code」或「12: code」，带行号时剥离并沿用原行号 */
const NUMBERED = /^\s*(\d+)(?:→|\t|: |\| )(.*)$/;

export function langOf(path: string): string | null {
  return EXT_LANG[(path.split(".").pop() || "").toLowerCase()] ?? null;
}

export const CodeView = memo(function CodeView({ path, text }: { path: string; text: string }) {
  const { text: shown, omitted } = useMemo(() => tailClamp(text), [text]);
  const { lines, start } = useMemo(() => {
    const raw = shown.replace(/\n$/, "").split("\n");
    const m0 = NUMBERED.exec(raw[0] ?? "");
    const allNumbered = m0 && raw.every((l) => l === "" || NUMBERED.test(l));
    if (allNumbered) return { lines: raw.map((l) => NUMBERED.exec(l)?.[2] ?? ""), start: Number(m0![1]) };
    return { lines: raw, start: 1 };
  }, [shown]);
  const html = useMemo(() => {
    const lang = langOf(path);
    const src = lines.join("\n");
    try {
      return lang ? hljs.highlight(src, { language: lang, ignoreIllegals: true }).value : hljs.highlightAuto(src).value;
    } catch { return null; }
  }, [lines, path]);
  // 高亮结果按行切分：hljs 的 span 不会跨行闭合问题在单文件内可接受，逐行渲染保证行号对齐
  const htmlLines = html ? html.split("\n") : null;
  const gutterW = String(start + lines.length).length;

  return (
    <div className="cv">
      <div className="cv-head">
        <FileIcon name={path} size={14} />
        <span className="cv-name">{baseName(path)}</span>
        <span className="cv-meta">{lines.length} 行{omitted > 0 ? ` · 已省略前 ${omitted.toLocaleString()} 字符` : ""}</span>
      </div>
      <div className="cv-body" style={{ ["--cv-gutter" as string]: `${gutterW + 2}ch` }}>
        {lines.map((l, i) => (
          <div className="cv-line" key={i}>
            <span className="cv-ln">{start + i}</span>
            {htmlLines
              ? <code className="cv-code hljs" dangerouslySetInnerHTML={{ __html: htmlLines[i] || " " }} />
              : <code className="cv-code">{l || " "}</code>}
          </div>
        ))}
      </div>
    </div>
  );
});
