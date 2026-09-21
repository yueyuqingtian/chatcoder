/** AttachmentCard（v14）：消息中的附件卡片——图片显示缩略图，其他文件显示图标+文件名。
 * v15: 图片点击在应用内大图预览（lightbox）。
 * 会话 229: 统一改用全局 ImageGallery（左右切换/缩放/下载）；
 * 并新增 MessageAttachmentList——消息内图片改为约 150px 缩略图并排展示，非图片附件保持卡片。
 */
import { IconPaperclip, IconFileText, IconBox, IconPlug, IconPackage } from "../icons";
import { type AttachmentInfo, type ComposerRefOut, resolveFileUrl } from "../../api/client";
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

/** plan-308-1542 需求2：消息内引用芯片（与输入框 .composer-ref-chip 同视觉）。
 *
 * 数据源：用户消息 content.refs（结构化落库）。历史消息无该字段时，
 * 调用方回退用 TokenText 解析文本（见 stripRefLines + TokenText）。
 */
const REF_KIND_LABEL: Record<string, string> = {
  file: "文件", skill: "技能", mcp: "连接器", plugin: "插件",
};

export function RefChips({ refs }: { refs: ComposerRefOut[] }) {
  if (!refs || refs.length === 0) return null;
  return (
    <div className="msg-ref-chips">
      {refs.map((r, i) => (
        <span
          key={`${r.kind}-${r.value}-${i}`}
          className={`msg-ref-chip msg-ref-${r.kind}`}
          title={`${REF_KIND_LABEL[r.kind] ?? r.kind}：${r.value}`}
        >
          <span className="msg-ref-icon">
            {r.kind === "file" ? <IconFileText size={11} />
              : r.kind === "mcp" ? <IconPlug size={11} />
              : r.kind === "plugin" ? <IconPackage size={11} />
              : <IconBox size={11} />}
          </span>
          {r.label || r.value}
        </span>
      ))}
    </div>
  );
}

/** 读取用户消息里的结构化引用（v14 content.refs；非法项过滤）。 */
export function refsOf(content: Record<string, unknown> | undefined): ComposerRefOut[] {
  const raw = content?.refs;
  if (!Array.isArray(raw)) return [];
  return raw.filter((r): r is ComposerRefOut => {
    const it = r as { kind?: unknown; value?: unknown };
    return Boolean(it && typeof it === "object" && typeof it.kind === "string" && typeof it.value === "string");
  });
}

/** plan-308-1542 需求2：拼在消息文本里的引用行（buildRefsSuffix 产物）在有了芯片后
 *  应从气泡文本中剔除，避免「芯片 + 同一段文字」重复展示。
 *  只在消息确实带结构化 refs 时才剔除（历史消息保留原文，不丢信息）。 */
export function stripRefLines(text: string, hasRefs: boolean): string {
  if (!hasRefs || !text) return text;
  return text
    .split("\n")
    .filter((ln) => !/^(引用文件|使用技能|使用连接器|使用插件)：/.test(ln.trim()))
    .join("\n")
    .trim();
}

/** 从消息 content 中解析附件数组（v14: content.attachments）。 */
export function attachmentsOf(content: Record<string, unknown> | undefined): AttachmentInfo[] {
  const atts = content?.attachments;
  if (!Array.isArray(atts)) return [];
  return atts.filter((a): a is AttachmentInfo => Boolean(a && typeof a === "object" && (a as AttachmentInfo).path && (a as AttachmentInfo).url));
}
