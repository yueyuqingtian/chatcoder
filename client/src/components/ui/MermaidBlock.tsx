/** MermaidBlock —— Markdown `mermaid` 代码块的原生交互渲染器。
 *
 *  特点：
 *  - 懒加载 `mermaid`（不拖慢首屏加载）；`securityLevel: "strict"` 防止 SVG 脚本注入；
 *  - 随 `useThemeStore` 浅/深色主题自动重绘；
 *  - 工具条支持：图表 / 源码切换、缩放（60%~180%）、复制源码、导出 SVG、Dialog 大窗预览；
 *  - 流式未闭合期间由外层传入 `live=true` 保持代码占位，围栏封块后一次性渲染 SVG，避免半截语法报错闪烁。
 */
import { memo, useCallback, useEffect, useId, useState } from "react";
import { useThemeStore } from "../../store/theme";
import {
  IconCheck,
  IconCode,
  IconCopy,
  IconDownload,
  IconGitGraph,
  IconMaximize,
  IconMinus,
  IconNoWrap,
  IconPlus,
  IconWrapText,
} from "../icons";
import { Dialog } from "./Dialog";
import { Tooltip } from "./Tooltip";

let mermaidModulePromise: Promise<typeof import("mermaid")> | null = null;
function loadMermaid() {
  if (!mermaidModulePromise) {
    mermaidModulePromise = import("mermaid");
  }
  return mermaidModulePromise;
}

function downloadSvg(svgMarkup: string): void {
  const blob = new Blob([svgMarkup], { type: "image/svg+xml;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `diagram-${Date.now()}.svg`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 2000);
}

export const MermaidBlock = memo(function MermaidBlock({
  source,
  live = false,
}: {
  source: string;
  live?: boolean;
}) {
  const rawId = useId();
  const safeId = `mmd_${rawId.replace(/[^a-zA-Z0-9_-]/g, "")}`;
  const theme = useThemeStore((s) => s.theme);

  const [tab, setTab] = useState<"view" | "code">("view");
  const [svg, setSvg] = useState<string>("");
  const [error, setError] = useState<string>("");
  const [zoom, setZoom] = useState(1);
  const [modalOpen, setModalOpen] = useState(false);
  const [modalZoom, setModalZoom] = useState(1);
  const [copied, setCopied] = useState(false);
  const [wrapped, setWrapped] = useState(false);

  const lineCount = source ? source.replace(/\r?\n$/, "").split(/\r?\n/).length : 0;

  useEffect(() => {
    const code = source.trim();
    if (live || !code) return;
    let cancelled = false;
    setError("");

    void (async () => {
      try {
        const mod = await loadMermaid();
        const mermaid = mod.default;
        mermaid.initialize({
          startOnLoad: false,
          securityLevel: "strict",
          theme: theme === "dark" ? "dark" : "neutral",
          themeVariables: { background: "transparent" },
          fontFamily: "var(--font-sans, sans-serif)",
        });
        const renderId = `${safeId}_${Date.now().toString(36)}`;
        const result = await mermaid.render(renderId, code);
        if (!cancelled) {
          setSvg(result.svg);
          setError("");
        }
      } catch (e) {
        if (!cancelled) {
          setSvg("");
          setError(e instanceof Error ? e.message : "流程图解析失败");
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [source, live, theme, safeId]);

  const handleCopy = useCallback(() => {
    if (!source) return;
    void navigator.clipboard?.writeText(source).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    }).catch(() => {});
  }, [source]);

  const stepZoom = (delta: number, setter: (fn: (v: number) => number) => void) => {
    setter((prev) => Math.max(0.5, Math.min(2, Number((prev + delta).toFixed(2)))));
  };

  // 流式未封块期间先安静展示源码，封块后无缝切到图表
  if (live) {
    return (
      <div className="md-code-block mmd-block" role="region" aria-label="Mermaid 流程图">
        <div className="md-code-head">
          <span className="md-code-lang">
            <IconGitGraph size={14} />
            <span>mermaid</span>
            <span className="md-code-lang-meta">· 绘制中…</span>
          </span>
        </div>
        <pre className="md-code-pre"><code>{source}</code></pre>
      </div>
    );
  }

  // plan-seamless-flow-ui：图表态改用无外框透明画布 + 退让式单行眉标，直接融入消息流背景；
  // 切换查看源码或语法异常时仍以干净的代码视图展示。
  if (tab === "code" || error) {
    return (
      <div className="md-code-block mmd-block" role="region" aria-label="Mermaid 流程图源码">
        <div className="md-code-head">
          <span className="md-code-lang">
            <IconGitGraph size={14} />
            <span>mermaid</span>
            {lineCount > 0 && <span className="md-code-lang-meta">· {lineCount} 行</span>}
          </span>
          <div className="md-code-actions">
            {!error && (
              <div className="iui-seg" role="tablist" aria-label="图表视图切换">
                <button
                  type="button"
                  role="tab"
                  aria-selected={false}
                  className="iui-seg-btn"
                  onClick={() => setTab("view")}
                >
                  图表
                </button>
                <button
                  type="button"
                  role="tab"
                  aria-selected={true}
                  className="iui-seg-btn is-active"
                  onClick={() => setTab("code")}
                >
                  源码
                </button>
              </div>
            )}
            <Tooltip content={wrapped ? "当前：自动换行（点击关闭）" : "当前：不自动换行（点击开启）"} side="top">
              <button
                type="button"
                className={`md-code-action-btn${wrapped ? " is-active" : ""}`}
                aria-label={wrapped ? "关闭自动换行" : "开启自动换行"}
                aria-pressed={wrapped}
                onClick={() => setWrapped((v) => !v)}
              >
                {wrapped ? <IconWrapText size={14} /> : <IconNoWrap size={14} />}
              </button>
            </Tooltip>
            <Tooltip content={copied ? "已复制到剪贴板" : "复制源码"} side="top">
              <button
                type="button"
                className={`md-code-copy-btn${copied ? " copied" : ""}`}
                aria-label={copied ? "已复制" : "复制源码"}
                onClick={handleCopy}
              >
                {copied ? <IconCheck size={14} /> : <IconCopy size={14} />}
              </button>
            </Tooltip>
          </div>
        </div>
        {error ? (
          <div className="mmd-error-wrap">
            <div className="mmd-error-note">流程图语法暂无法解析，已保留源码供核对：{error}</div>
            <pre className={`md-code-pre${wrapped ? " is-wrapped" : ""}`}><code>{source}</code></pre>
          </div>
        ) : (
          <pre className={`md-code-pre${wrapped ? " is-wrapped" : ""}`}><code>{source}</code></pre>
        )}
      </div>
    );
  }

  return (
    <div className="iui-canvas mmd-seamless" role="region" aria-label="Mermaid 流程图">
      <div className="iui-eyebrow">
        <div className="iui-eyebrow-left">
          <span className="iui-eyebrow-icon" aria-hidden="true">
            <IconGitGraph size={14} />
          </span>
          <span className="iui-eyebrow-title">流程图</span>
          <span className="iui-eyebrow-kind">mermaid</span>
        </div>
        <div className="iui-eyebrow-tools">
          {svg && (
            <>
              <Tooltip content="缩小" side="top">
                <button
                  type="button"
                  className="md-code-action-btn"
                  aria-label="缩小图表"
                  onClick={() => stepZoom(-0.15, setZoom)}
                >
                  <IconMinus size={13} />
                </button>
              </Tooltip>
              <Tooltip content="复位缩放" side="top">
                <button
                  type="button"
                  className="mmd-zoom-reset"
                  aria-label="复位缩放"
                  onClick={() => setZoom(1)}
                >
                  {Math.round(zoom * 100)}%
                </button>
              </Tooltip>
              <Tooltip content="放大" side="top">
                <button
                  type="button"
                  className="md-code-action-btn"
                  aria-label="放大图表"
                  onClick={() => stepZoom(0.15, setZoom)}
                >
                  <IconPlus size={13} />
                </button>
              </Tooltip>
              <Tooltip content="导出 SVG" side="top">
                <button
                  type="button"
                  className="md-code-action-btn"
                  aria-label="导出 SVG"
                  onClick={() => downloadSvg(svg)}
                >
                  <IconDownload size={13} />
                </button>
              </Tooltip>
              <Tooltip content="放大查看" side="top">
                <button
                  type="button"
                  className="md-code-action-btn"
                  aria-label="大窗查看图表"
                  onClick={() => { setModalZoom(1); setModalOpen(true); }}
                >
                  <IconMaximize size={13} />
                </button>
              </Tooltip>
            </>
          )}
          <Tooltip content="查看源码" side="top">
            <button
              type="button"
              className="md-code-action-btn"
              aria-label="查看源码"
              onClick={() => setTab("code")}
            >
              <IconCode size={14} />
            </button>
          </Tooltip>
          <Tooltip content={copied ? "已复制到剪贴板" : "复制源码"} side="top">
            <button
              type="button"
              className={`md-code-copy-btn${copied ? " copied" : ""}`}
              aria-label={copied ? "已复制" : "复制源码"}
              onClick={handleCopy}
            >
              {copied ? <IconCheck size={14} /> : <IconCopy size={14} />}
            </button>
          </Tooltip>
        </div>
      </div>

      {svg ? (
        <div
          className="mmd-stage"
          onDoubleClick={() => setZoom(1)}
          dangerouslySetInnerHTML={{
            __html: `<div class="mmd-svg-inner" style="transform:scale(${zoom})">${svg}</div>`,
          }}
        />
      ) : (
        <div className="mmd-loading">正在渲染流程图…</div>
      )}

      <Dialog
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        className="iui-modal-dialog"
        bodyClassName="iui-modal-body"
        title="流程图预览"
        subtitle="双击画布复位缩放 · 支持导出 SVG"
        width={1400}
        actions={svg ? (
          <div className="mmd-modal-tools">
            <Tooltip content="缩小" side="top">
              <button
                type="button"
                className="md-code-action-btn"
                aria-label="缩小图表"
                onClick={() => stepZoom(-0.15, setModalZoom)}
              >
                <IconMinus size={14} />
              </button>
            </Tooltip>
            <span className="mmd-zoom-label">{Math.round(modalZoom * 100)}%</span>
            <Tooltip content="放大" side="top">
              <button
                type="button"
                className="md-code-action-btn"
                aria-label="放大图表"
                onClick={() => stepZoom(0.15, setModalZoom)}
              >
                <IconPlus size={14} />
              </button>
            </Tooltip>
            <Tooltip content="切换源码" side="top">
              <button
                type="button"
                className="md-code-action-btn"
                aria-label="切换源码"
                onClick={() => { setModalOpen(false); setTab("code"); }}
              >
                <IconCode size={14} />
              </button>
            </Tooltip>
            <Tooltip content="导出 SVG" side="top">
              <button
                type="button"
                className="md-code-action-btn"
                aria-label="导出 SVG"
                onClick={() => downloadSvg(svg)}
              >
                <IconDownload size={14} />
              </button>
            </Tooltip>
          </div>
        ) : undefined}
      >
        {svg && (
          <div
            className="mmd-stage mmd-stage-modal"
            onDoubleClick={() => setModalZoom(1)}
            dangerouslySetInnerHTML={{
              __html: `<div class="mmd-svg-inner" style="transform:scale(${modalZoom})">${svg}</div>`,
            }}
          />
        )}
      </Dialog>
    </div>
  );
});
