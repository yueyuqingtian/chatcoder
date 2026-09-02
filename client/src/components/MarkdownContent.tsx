import { memo, useState, useCallback, useRef, type ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeHighlight from "rehype-highlight";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";
import { IconCopy, IconCheck } from "./icons";
import { usePanelStore } from "../store/panel";

function CodeBlockWrapper({ children }: { children: ReactNode }) {
  const [copied, setCopied] = useState(false);
  const preRef = useRef<HTMLPreElement>(null);

  // 从子节点中提取 language-xxx 标记
  let detectedLang = "";
  if (children && typeof children === "object" && "props" in children) {
    const childProps = (children as { props?: { className?: string } }).props;
    const className = childProps?.className || "";
    const m = className.match(/language-([a-zA-Z0-9_-]+)/);
    if (m && m[1]) {
      detectedLang = m[1].toLowerCase();
    }
  }

  const handleCopy = useCallback(() => {
    const text = preRef.current?.textContent || "";
    if (!text) return;
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    });
  }, []);

  return (
    <div className="md-code-block" role="region" aria-label="代码块">
      <div className="md-code-head">
        <span className="md-code-lang">{detectedLang || "code"}</span>
        <button
          className={`md-code-copy-btn${copied ? " copied" : ""}`}
          onClick={handleCopy}
          title={copied ? "已复制到剪贴板" : "复制代码"}
          type="button"
        >
          {copied ? <IconCheck size={12} /> : <IconCopy size={12} />}
          <span>{copied ? "已复制" : "复制"}</span>
        </button>
      </div>
      <pre className="md-code-pre" ref={preRef}>{children}</pre>
    </div>
  );
}

/** v40: 共享 markdown 组件映射——MarkdownContent 与 StreamingMarkdown（流式增量）共用，保证渲染一致 */
export const markdownComponents: Components = {
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
  a({ href, children }) {
    const isExternal = href?.startsWith("http://") || href?.startsWith("https://") || href?.startsWith("mailto:");
    if (isExternal) {
      return <a className="md-a" href={href} target="_blank" rel="noopener noreferrer">{children}</a>;
    }
    return (
      <a
        className="md-a md-file-link"
        href={href}
        onClick={(e) => {
          e.preventDefault();
          if (href) {
            const clean = href.replace(/^(\.\/|\/)/, "");
            usePanelStore.getState().setPreviewPath(clean);
            usePanelStore.getState().openPanel();
            usePanelStore.getState().openTab("files");
          }
        }}
      >
        {children}
      </a>
    );
  },
  blockquote({ children }) { return <blockquote className="md-blockquote">{children}</blockquote>; },
  table({ children }) { return <table className="md-table">{children}</table>; },
  th({ children }) { return <th className="md-th">{children}</th>; },
  td({ children }) { return <td className="md-td">{children}</td>; },
};

export const MarkdownContent = memo(function MarkdownContent({ children }: { children: string }) {
  return (
    <div className="md-body">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[rehypeHighlight, rehypeKatex]}
        components={markdownComponents}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
});
