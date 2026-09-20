/** 发布说明规范化：把 HTML 形态的更新说明转成 Markdown（问题2）。
 *
 * 背景：更新说明有两条来源，格式并不统一——
 *  - electron-updater 的 releaseNotes 来自 electron-builder 生成的 latest.yml，
 *    内容是 **HTML**（`<p>…</p><ul><li>…</li></ul>`）；
 *  - GitHub Releases API 的 body 与本地 CHANGELOG 是 Markdown。
 * 展示端统一用 react-markdown 渲染（未启用 rehype-raw），HTML 便以源码形式裸露：
 * 侧栏更新浮窗表现为「<p><ul><li> 全裸」，与设置页正常的 Markdown 排版不一致。
 *
 * 这里把 HTML 结构就地转成等价的 Markdown 记号——文本内容原样保留（`<li>` 内
 * 混写的 `**加粗**` 等 Markdown 语法不受影响），使四个展示入口（侧栏浮窗 /
 * 设置页折叠区 / 更新历史 / 首启「本次更新」弹窗）排版完全一致。
 */

/** 常见的命名实体（数字实体由 decodeEntities 兜底处理） */
const NAMED_ENTITIES: Record<string, string> = {
  amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " ",
  hellip: "…", mdash: "—", ndash: "–", times: "×", middot: "·",
};

export function decodeEntities(s: string): string {
  return s.replace(/&(#x?[0-9a-fA-F]+|[a-zA-Z]+);/g, (m, code: string) => {
    if (code[0] === "#") {
      const hex = code[1] === "x" || code[1] === "X";
      const num = hex ? parseInt(code.slice(2), 16) : parseInt(code.slice(1), 10);
      return Number.isFinite(num) && num > 0 && num <= 0x10ffff ? String.fromCodePoint(num) : m;
    }
    return NAMED_ENTITIES[code.toLowerCase()] ?? m;
  });
}

/** 块级标签探测：命中才走转换，纯 Markdown 原样返回 */
const HTML_BLOCK_RE = /<\s*(?:p|ul|ol|li|div|br|h[1-6]|pre|blockquote|table)\b[^>]*>/i;

export function htmlReleaseNotesToMarkdown(input: string): string {
  if (!input || !HTML_BLOCK_RE.test(input)) return input;
  let s = input;

  // 0) 代码块先摘出（占位保护，避免内容被后续的标签清理规则破坏）
  const blocks: string[] = [];
  s = s.replace(/<\s*pre[^>]*>([\s\S]*?)<\s*\/\s*pre\s*>/gi, (_m, inner: string) => {
    const code = decodeEntities(String(inner).replace(/<\s*\/?\s*code[^>]*>/gi, ""));
    blocks.push("```\n" + code.replace(/^\s*\n+|\n+\s*$/g, "") + "\n```");
    return `\n\n\u0000BLOCK${blocks.length - 1}\u0000\n\n`;
  });

  // 1) 标题
  s = s.replace(/<\s*h([1-6])[^>]*>([\s\S]*?)<\s*\/\s*h\1\s*>/gi,
    (_m, lvl: string, text: string) => `\n\n${"#".repeat(Number(lvl))} ${text.trim()}\n\n`);

  // 2) 列表：li →「- 」（有序列表同样用「- 」，release notes 场景无需区分）
  s = s.replace(/<\s*li[^>]*>/gi, "\n- ").replace(/<\s*\/\s*li\s*>/gi, "");
  s = s.replace(/<\s*\/?\s*[uo]l[^>]*>/gi, "\n");

  // 3) 段落与换行
  s = s.replace(/<\s*br\s*\/?>/gi, "\n");
  s = s.replace(/<\s*p[^>]*>/gi, "\n\n").replace(/<\s*\/\s*p\s*>/gi, "\n\n");
  s = s.replace(/<\s*div[^>]*>/gi, "\n").replace(/<\s*\/\s*div\s*>/gi, "\n");
  s = s.replace(/<\s*blockquote[^>]*>/gi, "\n\n> ").replace(/<\s*\/\s*blockquote\s*>/gi, "\n\n");
  s = s.replace(/<\s*hr\s*\/?>/gi, "\n\n---\n\n");
  // 表格：去掉结构标签、保留单元格文本（行内以制表符分隔），避免整段挤成一行
  s = s.replace(/<\s*\/?\s*(thead|tbody|tr|th|td|table)\b[^>]*>/gi, "\n");

  // 4) 内联元素
  s = s.replace(/<\s*(strong|b)[^>]*>([\s\S]*?)<\s*\/\s*\1\s*>/gi, "**$2**");
  s = s.replace(/<\s*(em|i)[^>]*>([\s\S]*?)<\s*\/\s*\1\s*>/gi, "*$2*");
  s = s.replace(/<\s*code[^>]*>([\s\S]*?)<\s*\/\s*code\s*>/gi, "`$1`");
  s = s.replace(/<\s*a\b[^>]*href\s*=\s*["']([^"']*)["'][^>]*>([\s\S]*?)<\s*\/\s*a\s*>/gi,
    (_m, href: string, text: string) => `[${text.trim() || href}](${href})`);

  // 5) 其余标签（span/font/img 等）直接去掉
  s = s.replace(/<[^>]*>/g, "");

  // 6) 实体解码 + 还原代码块
  s = decodeEntities(s);
  s = s.replace(/\u0000BLOCK(\d+)\u0000/g, (_m, i: string) => blocks[Number(i)] ?? "");

  // 7) 空白整理：行尾空格清除、连续空行压缩
  return s.replace(/[ \t]+\n/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
}
