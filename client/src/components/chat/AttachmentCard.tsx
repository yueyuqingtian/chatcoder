/** AttachmentCard（v14）：消息中的附件卡片——图片显示缩略图，其他文件显示图标+文件名。
 * v15: 图片点击在应用内大图预览（lightbox）。
 * 会话 229: 统一改用全局 ImageGallery（左右切换/缩放/下载）；
 * 并新增 MessageAttachmentList——消息内图片改为约 150px 缩略图并排展示，非图片附件保持卡片。
 */
import { IconPaperclip } from "../icons";
import { type AttachmentInfo, resolveFileUrl } from "../../api/client";
import { openGallery } from "../../store/gallery";
import { tokenize, tokenDisplayName } from "../../utils/tokens";

function fmtSize(n: number): string {
  if (!n || n <= 0) return "";
  if (n >= 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)}MB`;
  if (n >= 1024) return `${(n / 1024).toFixed(0)}KB`;
  return `${n}B`;
}

function isImageAtt(att: AttachmentInfo): boolean {
  return att.type === "image" || att.mime_type.startsWith("image/");
}

export function AttachmentCard({ att }: { att: AttachmentInfo }) {
  const url = resolveFileUrl(att.url);
  const open = () => {
    if (isImageAtt(att)) openGallery([{ url, name: att.filename, size: att.size }], 0);
    else window.open(url, "_blank", "noopener");
  };
  return (
    <div className="attach-card" onClick={open} title={`${att.filename}（点击预览）`}>
      {isImageAtt(att) ? (
        <img className="attach-card-thumb" src={url} alt={att.filename} loading="lazy" />
      ) : (
        <span className="attach-card-icon"><IconPaperclip size={13} /></span>
      )}
      <span className="attach-card-name">{att.filename}</span>
      <span className="attach-card-size">{fmtSize(att.size)}</span>
    </div>
  );
}

/** 消息图片网格（会话 228-1142）：渲染在用户气泡**外部**、靠右（对齐参考图 1）；
 * 点击进全局查看器（多图可左右切换）。无图片时不渲染。 */
export function MessageImageGrid({ atts }: { atts: AttachmentInfo[] }) {
  const images = atts.filter(isImageAtt);
  if (images.length === 0) return null;
  const openImage = (att: AttachmentInfo) => {
    const list = images.map((a) => ({
      url: resolveFileUrl(a.url),
      name: a.filename,
      size: a.size,
    }));
    const idx = images.indexOf(att);
    openGallery(list, idx < 0 ? 0 : idx);
  };
  return (
    <div className="msg-image-grid">
      {images.map((a) => (
        <img
          key={a.file_id || a.url}
          className="msg-image-thumb"
          src={resolveFileUrl(a.url)}
          alt={a.filename}
          title={`${a.filename}（点击查看大图）`}
          loading="lazy"
          draggable={false}
          onClick={() => openImage(a)}
        />
      ))}
    </div>
  );
}

/** 消息内非图片附件卡（保留在气泡内，点击新窗口打开）。 */
export function MessageFileCards({ atts }: { atts: AttachmentInfo[] }) {
  const files = atts.filter((a) => !isImageAtt(a));
  return (
    <>
      {files.map((a) => (
        <AttachmentCard key={a.file_id || a.url} att={a} />
      ))}
    </>
  );
}

/** 会话 228-1142: 用户消息文本中的 $技能/@文件 标签化渲染（纯展示，与输入框标签层同视觉）。 */
export function TokenText({ text }: { text: string }) {
  return (
    <>
      {tokenize(text).map((tk, i) =>
        tk.type === "text" ? (
          <span key={i}>{tk.text}</span>
        ) : (
          <span key={i} className={`tok-chip ${tk.type === "skill" ? "tok-skill" : "tok-file"}`}>
            {tokenDisplayName(tk)}
          </span>
        ),
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
