/** 输入框 token 化（会话 229）：把 $技能 / @文件 从纯文本切分为可高亮片段。
 *
 * 用途：ComposerCore 输入框标签化（方案 A 高亮叠加）——标签仅是视觉高亮，
 * input 值语义仍为纯文本，发送内容与后端协议不变。
 */

export type TokenType = "text" | "skill" | "file";

export interface Token {
  type: TokenType;
  text: string;
}

/** $技能：$ 后跟非空白字符（技能引用格式 `$name`，与 EmptyState 预填一致）；
 * @文件：@ 后跟非空白字符（与 pickAtFile 插入的 `@path ` 一致）。 */
const TOKEN_RE = /(\$[^\s]+|@[^\s]+)/g;

/** 把输入文本切分为 [{type, text}]；无 token 时返回单个 text 片段（空串返回空数组）。 */
export function tokenize(text: string): Token[] {
  if (!text) return [];
  const out: Token[] = [];
  let last = 0;
  for (const m of text.matchAll(TOKEN_RE)) {
    const idx = m.index ?? 0;
    if (idx > last) out.push({ type: "text", text: text.slice(last, idx) });
    const raw = m[0];
    out.push({ type: raw.startsWith("$") ? "skill" : "file", text: raw });
    last = idx + raw.length;
  }
  if (last < text.length) out.push({ type: "text", text: text.slice(last) });
  return out;
}

/** 标签展示名：去掉前缀符（$name → name；@a/b/c.tsx → 取文件名）。 */
export function tokenDisplayName(tok: Token): string {
  const body = tok.text.slice(1);
  if (tok.type === "file") {
    const parts = body.replace(/\\/g, "/").split("/");
    return parts[parts.length - 1] || body;
  }
  return body;
}

/** 返回光标所在 token 的区间 [start, end)（会话 228-1142：Backspace/Delete 整体删除用）。
 * pos 为光标位置：pos-1 落在某个 token 内（含 token 末尾）时命中该 token；
 * pos 恰为 token 起点（或 pos<=0 / 无命中）时返回 null——此时应按普通字符删除。 */
export function tokenRangeAt(text: string, pos: number): { start: number; end: number } | null {
  if (pos <= 0) return null;
  for (const m of text.matchAll(TOKEN_RE)) {
    const start = m.index ?? 0;
    const end = start + m[0].length;
    if (pos > start && pos <= end) return { start, end };
  }
  return null;
}
