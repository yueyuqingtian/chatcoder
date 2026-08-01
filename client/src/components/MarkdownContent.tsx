import { memo, useState, useCallback, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeHighlight from "rehype-highlight";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";

function CodeBlockWrapper({ children }: { children: ReactNode }) {
  const [copied, setCopied] = useState(false);
  const handleCopy = useCallback(() => {
    const el = document.activeElement?.closest("pre") || null;
    const codeEl = el?.querySelector("code");
    const text = codeEl?.textContent || "";
    navigator.clipboard.writeText(text).then(() => { setCopied(true); setTimeout(() => setCopied(false), 2000); });
  }, []);
  return (
    <div className="md-code-block" role="region" aria-label="代码块">
      <button className="md-code-copy" onClick={handleCopy} aria-label="复制代码">
        {copied ? "✓ 已复制" : "复制"}
      </button>
      <pre className="md-code-pre">{children}</pre>
    </div>
  );
}

export const MarkdownContent = memo(function MarkdownContent({ children }: { children: string }) {
  return (
    <div className="md-body">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[rehypeHighlight, rehypeKatex]}
        components={{
          code({ className, children }) {
            const lang = className?.replace("language-", "") || "";
            return <code className={`md-inline-code language-${lang}`}>{children}</code>;
          },
          pre({ children }) { return <CodeBlockWrapper>{children}</CodeBlockWrapper>; },
          h1({ children }) { return <h1 className="md-h1">{children}</h1>; },
          h2({ children }) { return <h2 className="md-h2">{children}</h2>; },
          h3({ children }) { return <h3 className="md-h3">{children}</h3>; },
          p({ children }) { return <p className="md-p">{children}</p>; },
          ul({ children }) { return <ul className="md-ul">{children}</ul>; },
          ol({ children }) { return <ol className="md-ol">{children}</ol>; },
          li({ children }) { return <li className="md-li">{children}</li>; },
          strong({ children }) { return <strong className="md-strong">{children}</strong>; },
          a({ href, children }) { return <a className="md-a" href={href} target="_blank" rel="noopener noreferrer">{children}</a>; },
          blockquote({ children }) { return <blockquote className="md-blockquote">{children}</blockquote>; },
          table({ children }) { return <table className="md-table">{children}</table>; },
          th({ children }) { return <th className="md-th">{children}</th>; },
          td({ children }) { return <td className="md-td">{children}</td>; },
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
});
