import { api, resolveFileUrl, type AttachmentInfo } from "../../api/client";
import { openGallery } from "../../store/gallery";
import { usePanelStore } from "../../store/panel";
import { IconPaperclip, IconX } from "../icons";
import { Tooltip } from "../ui";

export async function uploadComposerFiles(files: File[]) {
  const uploaded: AttachmentInfo[] = [];
  const failures: string[] = [];
  for (const file of files) {
    try {
      uploaded.push(await api.uploadFile(file));
    } catch (error) {
      failures.push(`${file.name}：${error instanceof Error ? error.message : "上传失败，请重试"}`);
    }
  }
  return { uploaded, failures };
}

export function ComposerAttachments({ files, sessionId, uploading, onRemove }: {
  files: AttachmentInfo[];
  sessionId: number | null;
  uploading?: boolean;
  onRemove?: (index: number) => void;
}) {
  const show = (file: AttachmentInfo) => {
    if (file.type !== "image" && !file.mime_type.startsWith("image/")) {
      usePanelStore.getState().openResource({ source: "upload", file_id: file.file_id, filename: file.filename }, sessionId);
      return;
    }
    const images = files.filter((item) => item.type === "image" || item.mime_type.startsWith("image/"));
    openGallery(images.map((item) => ({ url: resolveFileUrl(item.url), name: item.filename, size: item.size })),
      images.indexOf(file));
  };

  if (!files.length && !uploading) return null;
  return (
    <div className="composer-attachments" data-question-control>
      {files.map((file, index) => (
        <div key={file.file_id} className={`composer-attach-chip${file.type === "image" ? " image" : ""}`}>
          <Tooltip content={file.filename}>
            <button className="btn-ghost attach-chip-name" type="button" onClick={() => show(file)}>
              {file.type === "image" ? <img src={resolveFileUrl(file.url)} alt="" className="attach-chip-thumb" />
                : <IconPaperclip size={11} />}
              <span>{file.filename}</span>
            </button>
          </Tooltip>
          {onRemove && (
            <Tooltip content="移除附件">
              <button className="remove" type="button" aria-label={`移除附件 ${file.filename}`}
                onClick={() => onRemove(index)}>
                <IconX size={10} />
              </button>
            </Tooltip>
          )}
        </div>
      ))}
      {uploading && <span className="composer-attach-uploading" role="status">附件上传中…</span>}
    </div>
  );
}
