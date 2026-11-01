import hljs from "highlight.js/lib/common";

/** plan-20261101-performance：高亮移出渲染线程，按行平衡 span，窗口切片不破坏标记。 */
self.onmessage = (event: MessageEvent<{ id: number; source: string; language: string }>) => {
  const { id, source, language } = event.data;
  try {
    const html = hljs.getLanguage(language)
      ? hljs.highlight(source, { language, ignoreIllegals: true }).value
      : source.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    const stack: string[] = [];
    const lines = html.split("\n").map((line) => {
      const prefix = stack.join("");
      for (const match of line.matchAll(/<span\b[^>]*>|<\/span>/g)) {
        if (match[0] === "</span>") stack.pop();
        else stack.push(match[0]);
      }
      return prefix + line + "</span>".repeat(stack.length);
    });
    self.postMessage({ id, lines });
  } catch { self.postMessage({ id, lines: null }); }
};
