/** AttachmentCard（v14）：消息中的附件卡片——图片显示缩略图，其他文件显示图标+文件名。
 * v15: 图片点击在应用内大图预览（lightbox），不再跳转浏览器下载；
 * 其他文件点击新窗口打开（后端已改 inline 预览，PDF/文本直接展示而非下载）。
 */
import { useState } from "react";
import { createPortal } from "react-dom";
import { IconPaperclip } from "../icons";
import { type AttachmentInfo, resolveFileUrl } from "../../api/client";

function fmtSize(n: number): string {
  if (!n || n <= 0) return "";
  if (n >= 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)}MB`;
  if (n >= 1024) return `${(n / 1024).toFixed(0)}KB`;
  return `${n}B`;
}

export function AttachmentCard({ att }: { att: AttachmentInfo }) {
  const url = resolveFileUrl(att.url);
  const isImage = att.type === "image" || att.mime_type.startsWith("image/");
  const [preview, setPreview] = useState(false);
  const open = () => {
    if (isImage) setPreview(true);
    else window.open(url, "_blank", "noopener");
  };
  return (
    <>
      <div className="attach-card" onClick={open} title={`${att.filename}（点击预览）`}>
        {isImage ? (
          <img className="attach-card-thumb" src={url} alt={att.filename} loading="lazy" />
        ) : (
          <span className="attach-card-icon"><IconPaperclip size={13} /></span>
        )}
        <span className="attach-card-name">{att.filename}</span>
        <span className="attach-card-size">{fmtSize(att.size)}</span>
      </div>
      {preview && createPortal(
        <div
          onClick={() => setPreview(false)}
          style={{
            position: "fixed", inset: 0, zIndex: 9999,
            background: "rgba(0,0,0,0.75)", display: "flex",
            alignItems: "center", justifyContent: "center", cursor: "zoom-out",
          }}
        >
          <img
            src={url}
            alt={att.filename}
            onClick={(e) => e.stopPropagation()}
            style={{
              maxWidth: "92vw", maxHeight: "92vh", objectFit: "contain",
              borderRadius: 6, boxShadow: "0 8px 40px rgba(0,0,0,0.5)", cursor: "default",
            }}
          />
          <div style={{
            position: "absolute", bottom: 24, left: 0, right: 0, textAlign: "center",
            color: "rgba(255,255,255,0.85)", fontSize: 13, pointerEvents: "none",
          }}>
            {att.filename}（{fmtSize(att.size)}）· 点击空白处关闭
          </div>
        </div>,
        document.body,
      )}
    </>
  );
}

/** 从消息 content 中解析附件数组（v14: content.attachments）。 */
export function attachmentsOf(content: Record<string, unknown> | undefined): AttachmentInfo[] {
  const atts = content?.attachments;
  if (!Array.isArray(atts)) return [];
  return atts.filter((a): a is AttachmentInfo => Boolean(a && typeof a === "object" && (a as AttachmentInfo).path && (a as AttachmentInfo).url));
}
