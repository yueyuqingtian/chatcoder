import { lazy, Suspense, useEffect, useRef, useState } from "react";
import Editor from "@monaco-editor/react";
import {
  previewApi,
  type PreviewResource,
  type PreviewSource,
} from "../../api/client";
import { MarkdownContent } from "../MarkdownContent";
import { openGallery } from "../../store/gallery";
import { usePanelStore } from "../../store/panel";
import { ResourceLoading } from "./ResourceLoading";
import { IconX } from "../icons";
import { Button, Segmented, Tooltip } from "../ui";
const PdfPreview = lazy(() => import("./PdfPreview"));
const WordPreview = lazy(() => import("./WordPreview"));
const SpreadsheetPreview = lazy(() => import("./SpreadsheetPreview"));
const languages: Record<string, string> = {
  py: "python",
  js: "javascript",
  jsx: "javascript",
  ts: "typescript",
  tsx: "typescript",
  md: "markdown",
  json: "json",
  yml: "yaml",
  yaml: "yaml",
  sh: "shell",
  rs: "rust",
  cs: "csharp",
  h: "cpp",
  txt: "plaintext",
};

/** plan-20261103-attachment-preview：查看器生命周期绑定来源，旧请求不得覆盖新目标。 */
export default function ResourcePreview({
  source,
  line,
  onDirectory,
  showName = true,
  onClose,
}: {
  source: PreviewSource;
  line?: number | null;
  onDirectory?: (path: string) => void;
  showName?: boolean;
  onClose?: () => void;
}) {
  const [data, setData] = useState<PreviewResource | null>(null);
  const [error, setError] = useState("");
  const [mode, setMode] = useState<"preview" | "source">("preview");
  const host = useRef<HTMLDivElement>(null);
  const key = JSON.stringify(source);
  useEffect(() => {
    const ac = new AbortController();
    setData(null);
    setError("");
    setMode("preview");
    previewApi
      .resolve(source, ac.signal)
      .then((d) => {
        if (ac.signal.aborted) return;
        setData(d);
        if (d.kind === "directory") onDirectory?.(d.path);
      })
      .catch((e) => {
        if (!ac.signal.aborted) setError(String(e));
      });
    return () => ac.abort();
  }, [key]);
  const url = previewApi.contentUrl(source);
  const download = async () => {
    try {
      const response = await fetch(previewApi.contentUrl(source, true));
      if (!response.ok) throw new Error("下载失败");
      const object = URL.createObjectURL(await response.blob());
      const a = document.createElement("a");
      a.href = object;
      a.download = data?.name ?? "附件";
      a.click();
      setTimeout(() => URL.revokeObjectURL(object), 1000);
    } catch (e) {
      setError(String(e));
    }
  };
  const ext = data?.name.split(".").pop()?.toLowerCase() ?? "";
  const markdown = ext === "md" || ext === "markdown";
  const csv = ext === "csv" || ext === "tsv";
  return (
    <div className="resource-preview" ref={host}>
      <div className="resource-tools">
        {showName && (
          <span className="resource-name">{data?.name ?? "文件预览"}</span>
        )}
        <span className="resource-origin">
          {source.source === "attachment" ? "消息附件" : source.source === "upload" ? "待发送附件" : "只读预览"}
        </span>
        <div className="resource-actions">
          {(markdown || csv) && (
            <Segmented
              size="sm"
              value={mode}
              onChange={setMode}
              aria-label="查看方式"
              options={[
                { value: "preview", label: "预览" },
                { value: "source", label: "源码" },
              ]}
            />
          )}
          <Button variant="ghost" size="xs" onClick={() => void download()}>
            下载
          </Button>
          {onClose && <Tooltip content="关闭预览"><Button variant="ghost" size="xs" iconOnly icon={<IconX size={14}/>} onClick={onClose} aria-label="关闭预览"/></Tooltip>}
        </div>
      </div>
      {error ? (
        <div className="resource-state" role="alert">
          <strong>无法预览此文件</strong>
          <p>{error}</p>
        </div>
      ) : !data ? (
        <ResourceLoading>正在读取文件…</ResourceLoading>
      ) : (
        <>
          {data.truncated && (
            <div className="resource-notice">
              文件较大，当前显示前 512KB；下载可查看完整内容。
            </div>
          )}
          <Suspense
            fallback={
              <ResourceLoading>正在准备查看器…</ResourceLoading>
            }
          >
            {data.kind === "directory" ? (
              <div className="resource-state">正在定位目录…</div>
            ) : data.kind === "word" ? (
              <WordPreview url={url} />
            ) : data.kind === "spreadsheet" && mode === "preview" ? (
              <SpreadsheetPreview key={key} source={source} />
            ) : data.kind === "pdf" ? (
              <PdfPreview key={key} url={url}/>
            ) : data.kind === "image" ? (
              <div className="resource-image">
                <Button variant="ghost" className="resource-image-open" aria-label={`放大查看 ${data.name}`} onClick={() => openGallery([{ url, name: data.name }])}><img src={url} alt={data.name} /></Button>
              </div>
            ) : data.kind === "unsupported" ? (
              <div className="resource-state">
                <strong>暂不支持此格式的内置预览</strong>
                <p>
                  {ext === "doc"
                    ? "请将旧版 Word 文档另存为 DOCX 后查看。"
                    : "可下载后使用对应应用查看。"}
                </p>
              </div>
            ) : markdown && mode === "preview" ? (
              <div className="ft-md-preview resource-markdown">
                <MarkdownContent
                  components={{
                    a: ({ href, children }) => {
                      if (href && /^https?:\/\//i.test(href))
                        return (
                          <a
                            className="md-a"
                            href={href}
                            target="_blank"
                            rel="noopener noreferrer"
                          >
                            {children}
                          </a>
                        );
                      if (
                        !href ||
                        /^[a-z]+:/i.test(href) ||
                        href.startsWith("#")
                      )
                        return <span>{children}</span>;
                      return (
                        <Button
                          variant="ghost"
                          size="xs"
                          onClick={() =>
                            usePanelStore
                              .getState()
                              .openResource({ ...source, relative: href })
                          }
                        >
                          {children}
                        </Button>
                      );
                    },
                    img: ({ src, alt }) =>
                      src && !/^[a-z]+:/i.test(src) ? (
                        <img
                          className="md-img"
                          alt={alt ?? ""}
                          src={previewApi.contentUrl({
                            ...source,
                            relative: src,
                          })}
                        />
                      ) : (
                        <span className="resource-origin">
                          {alt ?? "外部图片未加载"}
                        </span>
                      ),
                  }}
                >
                  {data.content ?? ""}
                </MarkdownContent>
              </div>
            ) : (
              <TextPreview
                key={`${key}:${mode}`}
                source={source}
                content={data.content}
                language={languages[ext] ?? ext}
                line={line}
              />
            )}
          </Suspense>
        </>
      )}
    </div>
  );
}

function TextPreview({
  source,
  content,
  language,
  line,
}: {
  source: PreviewSource;
  content?: string;
  language: string;
  line?: number | null;
}) {
  const editorRef = useRef<{
    setPosition: (p: { lineNumber: number; column: number }) => void;
    revealLineInCenter: (line: number) => void;
  } | null>(null);
  useEffect(() => {
    if (line && editorRef.current) {
      editorRef.current.setPosition({ lineNumber: line, column: 1 });
      editorRef.current.revealLineInCenter(line);
    }
  }, [line]);
  const [text, setText] = useState(content ?? "");
  const [error, setError] = useState("");
  useEffect(() => {
    if (content !== undefined) return;
    const ac = new AbortController();
    previewApi
      .text(source, ac.signal)
      .then((d) => {
        if (!ac.signal.aborted) setText(d.content ?? "");
      })
      .catch((e) => {
        if (!ac.signal.aborted) setError(String(e));
      });
    return () => ac.abort();
  }, []);
  if (error) return <div className="resource-state">{error}</div>;
  return (
    <Editor
      height="100%"
      language={language}
      value={text}
      theme={
        document.documentElement.getAttribute("data-theme") === "dark"
          ? "vs-dark"
          : "light"
      }
      options={{
        readOnly: true,
        minimap: { enabled: false },
        fontSize: 12,
        automaticLayout: true,
        scrollBeyondLastLine: false,
      }}
      onMount={(editor) => {
        editorRef.current = editor;
        if (line) {
          editor.setPosition({ lineNumber: line, column: 1 });
          editor.revealLineInCenter(line);
        }
      }}
    />
  );
}
