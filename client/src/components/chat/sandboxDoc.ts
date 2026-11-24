/** sandboxDoc —— 沙箱 iframe HTML 文档包装、CSP 注入与宿主主题变量提取（纯 TS 逻辑，无 React 依赖）。 */

export const HOST_VAR_KEYS = [
  "--bg-panel", "--bg-main", "--bg-elevated", "--bg-hover", "--bg-active",
  "--surface", "--surface-inset", "--surface-inset-border",
  "--color-card", "--color-panel", "--color-brand", "--color-primary", "--color-primary-foreground",
  "--text-strong", "--text-prose", "--text-1", "--text-2", "--text-3",
  "--md-code-fg", "--md-code-bg", "--md-code-hover-bg",
  "--border", "--border-light", "--border-hover", "--divider",
  "--success", "--success-soft", "--success-ring",
  "--warning", "--warning-soft", "--warning-ring",
  "--error", "--error-soft", "--error-ring",
  "--info", "--info-soft", "--info-ring",
  "--important", "--important-soft", "--important-ring",
  "--fs-md", "--r-sm", "--r-md", "--r-lg", "--r-pill",
  "--shadow-xs", "--shadow-sm", "--font-sans", "--font-mono",
];

export function readHostThemeVars(): Record<string, string> {
  if (typeof document === "undefined") return {};
  const style = getComputedStyle(document.documentElement);
  const vars: Record<string, string> = {};
  for (const k of HOST_VAR_KEYS) {
    const val = style.getPropertyValue(k).trim();
    if (val) vars[k] = val;
  }
  // 语义别名：兼容生成内容里常见的简写变量名
  if (!vars["--bg-card"] && vars["--color-card"]) vars["--bg-card"] = vars["--color-card"];
  if (!vars["--bg-inset"] && vars["--bg-panel"]) vars["--bg-inset"] = vars["--bg-panel"];
  if (!vars["--bg-surface"] && vars["--surface"]) vars["--bg-surface"] = vars["--surface"];
  if (!vars["--code-fg"] && vars["--md-code-fg"]) vars["--code-fg"] = vars["--md-code-fg"];
  if (!vars["--code-bg"] && vars["--md-code-bg"]) vars["--code-bg"] = vars["--md-code-bg"];
  if (!vars["--brand"] && vars["--color-brand"]) vars["--brand"] = vars["--color-brand"];
  if (!vars["--primary"] && vars["--color-primary"]) vars["--primary"] = vars["--color-primary"];
  if (!vars["--primary-fg"] && vars["--color-primary-foreground"]) vars["--primary-fg"] = vars["--color-primary-foreground"];
  return vars;
}

const CSP_META = `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval'; style-src 'unsafe-inline'; img-src data: blob:; font-src data:; connect-src 'none';">`;

function buildRuntimeHead(theme: string, vars: Record<string, string>, flush: boolean): string {
  const cssVars = Object.entries(vars).map(([k, v]) => `${k}:${v};`).join("");
  const pad = flush ? "0" : "16px";
  return `${CSP_META}
<style id="__chatcoder_host_theme">
  :root { ${cssVars} color-scheme: ${theme}; }
  *, *::before, *::after { box-sizing: border-box; }
  html, body {
    margin: 0;
    padding: 0;
    background: transparent;
    color: var(--text-prose, var(--text-1, #1f2329));
    font-family: var(--font-sans, -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif);
    font-size: var(--fs-md, 14px);
    line-height: 1.62;
    -webkit-font-smoothing: antialiased;
  }
  #__cc_root {
    padding: ${pad};
    width: 100%;
    background: transparent;
  }
  button, input, select, textarea { font-family: inherit; }
  .tabular-nums { font-variant-numeric: tabular-nums; }
</style>`;
}

function buildRuntimeBridge(instanceId: string): string {
  return `<script>
(function(){
  var __id = ${JSON.stringify(instanceId)};
  window.chatcoder = {
    emit: function(action, payload) {
      window.parent.postMessage({ channel: "chatcoder-ui", id: __id, type: "action", action: String(action || ""), payload: payload }, "*");
    },
    select: function(payload) {
      window.parent.postMessage({ channel: "chatcoder-ui", id: __id, type: "action", action: "select", payload: payload }, "*");
    }
  };
  window.addEventListener("message", function(e) {
    if (!e.data || e.data.channel !== "chatcoder-host" || e.data.type !== "theme") return;
    document.documentElement.setAttribute("data-theme", e.data.theme);
    document.documentElement.style.colorScheme = e.data.theme;
    var rootStyle = document.documentElement.style;
    var vars = e.data.vars || {};
    for (var k in vars) {
      if (Object.prototype.hasOwnProperty.call(vars, k)) rootStyle.setProperty(k, vars[k]);
    }
    window.dispatchEvent(new CustomEvent("chatcoder:themechange", { detail: { theme: e.data.theme } }));
  });
  var lastH = 0;
  var timer = 0;
  function measure() {
    var root = document.getElementById("__cc_root") || document.body;
    if (!root) return;
    var rectH = Math.ceil(root.getBoundingClientRect().height);
    var scrollH = Math.ceil(root.scrollHeight || 0);
    var h = Math.max(rectH, scrollH);
    if (h > 0 && Math.abs(h - lastH) >= 2) {
      lastH = h;
      window.parent.postMessage({ channel: "chatcoder-ui", id: __id, type: "resize", height: h }, "*");
    }
  }
  function scheduleMeasure() {
    clearTimeout(timer);
    timer = setTimeout(measure, 35);
  }
  if (typeof ResizeObserver !== "undefined") {
    var ro = new ResizeObserver(scheduleMeasure);
    var target = document.getElementById("__cc_root") || document.body;
    if (target) ro.observe(target);
  }
  window.addEventListener("load", measure);
  setTimeout(measure, 40);
  setTimeout(measure, 180);
})();
</script>`;
}

/** 将片段 HTML 或完整 HTML 文档统一包装为注入了 CSP、主题变量与通信桥的安全文档。
 *  plan-seamless-flow-ui：默认 flush=true（零外边距 + 透明背景），直接与消息流正文左缘齐平融入画布。 */
export function buildSandboxDocument(rawHtml: string, instanceId: string, theme: string, vars: Record<string, string>, flush = true): string {
  const html = String(rawHtml ?? "").trim();
  const headInsert = buildRuntimeHead(theme, vars, flush);
  const bridge = buildRuntimeBridge(instanceId);
  const isFullDoc = /<!doctype\s+html/i.test(html) || /<html[\s>]/i.test(html);

  if (!isFullDoc) {
    return `<!doctype html>
<html lang="zh-CN" data-theme="${theme}">
<head>
<meta charset="UTF-8">
${headInsert}
</head>
<body>
<div id="__cc_root">${html}</div>
${bridge}
</body>
</html>`;
  }

  let out = html;
  if (/<head[\s>]/i.test(out)) {
    out = out.replace(/<head([^>]*)>/i, `<head$1>\n${headInsert}`);
  } else if (/<html[\s>]/i.test(out)) {
    out = out.replace(/<html([^>]*)>/i, `<html$1>\n<head>${headInsert}</head>`);
  } else {
    out = `<head>${headInsert}</head>\n${out}`;
  }

  if (/<\/body>/i.test(out)) {
    out = out.replace(/<\/body>/i, `${bridge}\n</body>`);
  } else {
    out = `${out}\n${bridge}`;
  }
  return out;
}
