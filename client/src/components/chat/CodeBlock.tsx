/** 代码块（v2）：语言徽章 + 复制按钮 + 超长块折叠。 */
import { useMemo, useState } from "react";
import { IconCopy, IconArrowToggle } from "../icons";

const MAX_LINES = 200;

function detectLang(lang?: string): string {
  if (!lang) return "";
  const l = lang.toLowerCase();
  const map: Record<string, string> = {
    js: "javascript", jsx: "javascript", ts: "typescript", tsx: "typescript",
    py: "python", rb: "ruby", sh: "bash", shell: "bash", yml: "yaml",
    md: "markdown", htm: "html", html: "html", css: "css", json: "json",
  };
  return map[l] || l;
}

export function CodeBlock({ code, lang, maxLines = MAX_LINES }: {
  code: string;
  lang?: string;
  maxLines?: number;
}) {
  const [copied, setCopied] = useState(false);
  const [folded, setFolded] = useState(false);

  const lines = useMemo(() => code.split("\n"), [code]);
  const language = detectLang(lang);
  const tooLong = lines.length > maxLines;
  const showLines = folded ? lines.slice(0, maxLines) : lines;

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch { /* ignore */ }
  };

  return (
    <div className="code-block">
      <div className="code-block-head">
        {language && <span className="code-block-lang">{language}</span>}
        <button className="code-block-copy" onClick={handleCopy} title="复制代码">
          <IconCopy size={12} />
          {copied ? "已复制" : "复制"}
        </button>
      </div>
      <pre className="code-block-body">
        <code>{showLines.join("\n")}</code>
      </pre>
      {tooLong && (
        <button className="code-block-fold" onClick={() => setFolded((v) => !v)}>
          <IconArrowToggle open={!folded} size={11} />
          {folded ? `展开全部 ${lines.length} 行` : `已折叠（${lines.length} 行）`}
        </button>
      )}
    </div>
  );
}
