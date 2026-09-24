/** Markdown —— 统一 Markdown 渲染（plan-282-1416：实现迁入组件库）
 *
 * 迁移说明：原先实现在 `components/MarkdownContent.tsx`，现移入组件库统一维护，
 * 旧路径保留 re-export 桥（23 处调用与 StreamingMarkdown 的 markdownComponents 共用零改动）。
 *
 * 相对路径修正：本文件位于 components/ui/，故对 store/api/icons 引用上移一级。
 *
 * 排版：元素级样式由 global.css 的 `.md-*` 规则承载（聊天作用域 / release-notes
 * 各自覆盖），本文件只负责渲染管线：
 *  - remark-gfm + remark-math、rehype-highlight + rehype-katex；
 *  - 代码块头部「语言 + 复制」——复制为图标按钮（去文字噪音，保留 aria-label）；
 *  - 文件链接先校验存在性再允许点击，避免点空。
 */
import { memo, useState, useCallback, useRef, useEffect, type ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeHighlight from "rehype-highlight";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";
import { IconCopy, IconCheck } from "../icons";
import { usePanelStore } from "../../store/panel";
import { useChatStore } from "../../store/chat";
import { api, resolveFileUrl } from "../../api/client";
import { openGallery } from "../../store/gallery";

/** 相对链接存在性校验缓存（path → 存在且为文件） */
const statCache = new Map<string, boolean>();

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
        {/* plan-282-1416：复制按钮图标化（原「复制」文字改为纯图标 + aria-label），
            头部更安静，同时保留已复制反馈（图标换勾 + copied 态着色） */}
        <button
          className={`md-code-copy-btn${copied ? " copied" : ""}`}
          onClick={handleCopy}
          title={copied ? "已复制到剪贴板" : "复制代码"}
          aria-label={copied ? "已复制" : "复制代码"}
          type="button"
        >
          {copied ? <IconCheck size={12} /> : <IconCopy size={12} />}
        </button>
      </div>
      <pre className="md-code-pre" ref={preRef}>{children}</pre>
    </div>
  );
}

/** 文件链接——先校验项目内文件存在性；仅真实存在且为文件时允许点击打开预览，
 * 否则渲染为普通文本（不可点击），避免点击后右侧面板报错/空白。 */
function FileLink({ href, children }: { href?: string; children?: ReactNode }) {
  const [stat, setStat] = useState<boolean | null>(() => {
    if (!href) return null;
    const clean = href.replace(/^(\.\/|\/)/, "");
    return statCache.has(clean) ? statCache.get(clean)! : null;
  });
  const currentProjectId = useChatStore((s) => s.currentProjectId);

  useEffect(() => {
    if (!href) return;
    const clean = href.replace(/^(\.\/|\/)/, "");
    // 外部链接/绝对路径不校验（保持默认行为）
    if (/^(https?:|mailto:|[A-Za-z]:[\\/]|\/)/i.test(clean)) { setStat(null); return; }
    if (statCache.has(clean)) { setStat(statCache.get(clean)!); return; }
    if (currentProjectId == null) { setStat(null); return; }
    let dead = false;
    api.projectStat(currentProjectId, clean)
      .then((r) => {
        if (dead) return;
        const ok = r.exists && !r.is_dir;
        statCache.set(clean, ok);
        setStat(ok);
      })
      .catch(() => { if (!dead) setStat(null); });
    return () => { dead = true; };
  }, [href, currentProjectId]);

  if (stat === false) {
    // 不存在/目录：纯文本展示，不可点击
    return <span className="md-a md-file-link-disabled" title="文件不存在">{children}</span>;
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
}

/** 共享 markdown 组件映射——MarkdownContent 与 StreamingMarkdown（流式增量）共用，保证渲染一致 */
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
    return <FileLink href={href}>{children}</FileLink>;
  },
  /** markdown 图片点击打开全局查看器（与消息附件/输入框附件共用） */
  img({ src, alt }) {
    const url = src ? resolveFileUrl(src) : "";
    return (
      <img
        className="md-img"
        src={url}
        alt={alt || ""}
        loading="lazy"
        onClick={() => {
          if (url) openGallery([{ url, name: alt || "image" }], 0);
        }}
      />
    );
  },
  blockquote({ children }) { return <blockquote className="md-blockquote">{children}</blockquote>; },
  table({ children }) { return <table className="md-table">{children}</table>; },
  th({ children }) { return <th className="md-th">{children}</th>; },
  td({ children }) { return <td className="md-td">{children}</td>; },
};

/** plan-334-1661 S4（本轮修复）：落库 Markdown 的跨挂载渲染缓存。
 *
 * ── 为什么 ──
 * 消息流是虚拟列表，历史条目在滚动中反复挂载/卸载；而 `Markdown` 的 memo 只按 children
 * 值比较，卸载即丢缓存 ⇒ 每次回翻都要重跑一遍 unified 解析 + rehype-highlight/rehype-katex。
 *
 * ── 做法与上一版的两处修正 ──
 * ① 缓存的是 **ReactMarkdown 的内层渲染结果**，外层 `<div className="md-body">` 每次新建——
 *    上一版把含 `.md-body` 的整棵树塞进缓存，命中后外层再包一层，出现 `.md-body > .md-body`
 *    双层结构（该轮"进入会话卡顿"的嫌疑点之一：后代选择器双重命中 + DOM 结构随命中与否变化）。
 * ② 新增 `cache` 开关：流式中间态（PlanCard 的节流预览文本）每次都产生新内容、永远不会命中，
 *    不应写入 LRU 挤掉真正可复用的历史条目（上一版未区分，属缓存污染）。
 *
 * ── 边界 ──
 * · 只缓存元素树（React 元素是不可变数据，可被重复渲染）；交互子组件（FileLink /
 *   CodeBlockWrapper）在挂载时各自跑自己的 hook（订阅 / 存在性校验），行为与未命中一致。
 * · 单条超长文本（> MD_CACHE_MAX_ONE）不入缓存，避免大对象长期滞留。
 * · 写入发生在渲染阶段：内容只由 children 决定，幂等（并发渲染被丢弃也不影响正确性）。
 */
const MD_CACHE_MAX_ENTRIES = 64;
/** 缓存文本总量上限（近似内存上限，按 key 字符数计） */
const MD_CACHE_MAX_CHARS = 2 * 1024 * 1024;
/** 单条文本上限：超过则不缓存 */
const MD_CACHE_MAX_ONE = 128 * 1024;

const mdCache = new Map<string, ReactNode>();
let mdCacheChars = 0;

function mdCacheGet(key: string): ReactNode | undefined {
  if (!mdCache.has(key)) return undefined;
  const hit = mdCache.get(key)!;
  // 命中后移到队尾：Map 保持插入顺序，即天然 LRU
  mdCache.delete(key);
  mdCache.set(key, hit);
  return hit;
}

function mdCacheSet(key: string, node: ReactNode): void {
  if (key.length > MD_CACHE_MAX_ONE) return;
  if (mdCache.has(key)) return; // 已存在（并发双渲染）：保留先入的元素树
  mdCache.set(key, node);
  mdCacheChars += key.length;
  while (mdCache.size > MD_CACHE_MAX_ENTRIES || mdCacheChars > MD_CACHE_MAX_CHARS) {
    const oldest = mdCache.keys().next();
    if (oldest.done) break;
    mdCache.delete(oldest.value);
    mdCacheChars -= oldest.value.length;
  }
}

export const Markdown = memo(function Markdown({ children, cache = true }: { children: string; cache?: boolean }) {
  // 命中缓存则直接复用内层元素树（跳过解析与 highlight/katex）；外层 .md-body 每次新建，
  // 保证无论是否命中缓存，DOM 结构恒为单层 .md-body。
  const cached = cache ? mdCacheGet(children) : undefined;
  const inner = cached !== undefined ? cached : (
    <ReactMarkdown
      remarkPlugins={[remarkGfm, remarkMath]}
      rehypePlugins={[rehypeHighlight, rehypeKatex]}
      components={markdownComponents}
    >
      {children}
    </ReactMarkdown>
  );
  if (cache && cached === undefined) mdCacheSet(children, inner);
  return <div className="md-body">{inner}</div>;
});
