// plan-20261107-long-paste：在进入受控输入框前分流，避免大段文本触发排版、引用搜索和草稿序列化。
export const PASTE_TEXT_CHAR_LIMIT = 10_000;
export const PASTE_TEXT_LINE_LIMIT = 200;

function isLongText(text: string): boolean {
  if (text.length >= PASTE_TEXT_CHAR_LIMIT) return true;
  let lines = 1;
  for (let i = 0; i < text.length; i++) {
    if (text[i] === "\r" || text[i] === "\n") {
      if (text[i] === "\r" && text[i + 1] === "\n") i++;
      if (++lines >= PASTE_TEXT_LINE_LIMIT) return true;
    }
  }
  return false;
}

export function getPastedFiles(clipboard: Pick<DataTransfer, "items" | "getData">): File[] {
  const files: File[] = [];
  for (const item of Array.from(clipboard.items)) {
    if (item.kind !== "file") continue;
    const file = item.getAsFile();
    if (file) files.push(file);
  }
  if (files.length > 0) return files;

  const text = clipboard.getData("text/plain");
  if (!isLongText(text)) return [];
  const filename = `粘贴文本-${Date.now()}-${crypto.randomUUID().slice(0, 8)}.txt`;
  return [new File([text], filename, { type: "text/plain;charset=utf-8" })];
}
