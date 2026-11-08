// plan-20261107-long-paste：保护原生粘贴边界、原文件优先级及 TXT 内容保真。
import assert from "node:assert/strict";
import { getPastedFiles, PASTE_TEXT_CHAR_LIMIT, PASTE_TEXT_LINE_LIMIT } from "../src/components/chat/composerPaste";

function paste(text: string, files: File[] = []) {
  return getPastedFiles({
    items: files.map((file) => ({ kind: "file", getAsFile: () => file })) as unknown as DataTransferItemList,
    getData: (format: string) => {
      assert.equal(format, "text/plain");
      return text;
    },
  });
}

assert.deepEqual(paste(""), []);
assert.deepEqual(paste("普通文本\n保留原生插入与选区替换"), []);
assert.deepEqual(paste("a".repeat(PASTE_TEXT_CHAR_LIMIT - 1)), []);
assert.deepEqual(paste(Array(PASTE_TEXT_LINE_LIMIT - 1).fill("行").join("\n")), []);

const boundary = "a".repeat(PASTE_TEXT_CHAR_LIMIT);
const [file] = paste(boundary);
assert.ok(file instanceof File);
assert.match(file.name, /^粘贴文本-\d+-[\da-f]{8}\.txt$/);
assert.equal(file.type, "text/plain;charset=utf-8");
assert.equal(await file.text(), boundary);
assert.notEqual(paste(boundary)[0].name, file.name);

for (const newline of ["\n", "\r\n", "\r"]) {
  const text = Array(PASTE_TEXT_LINE_LIMIT).fill("行").join(newline);
  assert.equal(await paste(text)[0].text(), text);
  assert.deepEqual(paste(Array(PASTE_TEXT_LINE_LIMIT - 1).fill("行").join(newline)), []);
}

const unicode = `  中文🙂\t\r\n${"内容\n".repeat(5000)}\n `;
const [unicodeFile] = paste(unicode);
assert.equal(await unicodeFile.text(), unicode);
assert.deepEqual(new Uint8Array(await unicodeFile.arrayBuffer()), new TextEncoder().encode(unicode));
assert.equal(await paste("x".repeat(2_000_000))[0].text(), "x".repeat(2_000_000));

const image = new File(["image"], "截图.png", { type: "image/png" });
const document = new File(["document"], "文件.txt", { type: "text/plain" });
assert.deepEqual(paste(boundary, [image, document]), [image, document]);
assert.deepEqual(getPastedFiles({
  items: [{ kind: "file", getAsFile: () => null }] as unknown as DataTransferItemList,
  getData: () => "短文本",
}), []);
assert.equal(getPastedFiles({
  items: [{ kind: "string", getAsFile: () => null }] as unknown as DataTransferItemList,
  getData: () => boundary,
}).length, 1);

console.log("通过：长文本字符/行数阈值、三类换行、UTF-8 原文保真、唯一文件名、大文本及真实附件优先回归");
