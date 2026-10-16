/** 流式正文里的 DSML 工具调用改写（plan-20261016-ux 问题 5）。
 *
 * DeepSeek / 智谱等模型偶尔不走结构化 tool_calls，而是把工具调用以 DSML 标签写进正文：
 *   <｜｜DSML｜｜invoke name="fs_write"><｜｜DSML｜｜parameter name="content">…代码…</｜｜DSML｜｜parameter>…
 * 服务端要等整段输出结束才把它解析成工具调用（openai_compatible._parse_dsml_tool_calls），
 * 于是流式期间用户看到的是一大段夹着标签的普通文本，「正在写的代码」直到输出完成才进入代码框——
 * 子代理写文件时最明显。这里只改写**流式显示**：DSML 之前的正文照常渲染，DSML 部分换成
 * 「正在准备 写入 `path`」+ 代码块（未闭合围栏在流式中同样按代码块渲染），落库后由工具行接管。
 */

// DSML 用全角竖线 ｜（U+FF5C），也兼容半角 |
const P2 = "[\\uFF5C|]{2}";
const MARK = new RegExp(`<${P2}DSML${P2}`);
const INVOKE = new RegExp(`<${P2}DSML${P2}invoke\\s+name="([^"]*)"[^>]*>`, "g");
const PARAM_OPEN = new RegExp(`<${P2}DSML${P2}parameter\\s+name="([^"]*)"[^>]*>`, "g");
const PARAM_CLOSE = new RegExp(`</${P2}DSML${P2}parameter\\s*>`);
const ANY_TAG = new RegExp(`</?${P2}/?DSML${P2}[^>]*>?`, "g");
/** 流式尾部可能停在半个标记上（「<」「<｜｜DS」…），显示时先去掉，避免闪出半截标签 */
const PARTIAL_TAIL = /<(?:[｜|]{0,2}(?:D(?:S(?:M(?:L(?:[｜|]{0,2})?)?)?)?)?)?$/;

const VERB: Record<string, string> = {
  fs_write: "写入",
  editor_apply_diff: "编辑",
  multi_file_edit: "批量编辑",
  terminal_exec: "运行命令",
  fs_read: "读取",
  fs_list: "列出目录",
  fs_grep: "搜索",
};
/** 作为代码块展示的参数（取第一个出现的）；其余参数按「名称: 值」简述 */
const CODE_PARAMS = ["content", "new_text", "command", "sql", "code", "script"];
/** 不在简述里展示的参数：编辑的原文（展示替换后的代码即可）与开关类参数 */
const HIDDEN_PARAMS = new Set(["path", "file_path", "old_text", "old_string", "replace_all"]);

const EXT_LANG: Record<string, string> = {
  java: "java", kt: "kotlin", ts: "ts", tsx: "tsx", js: "js", jsx: "jsx", mjs: "js", cjs: "js",
  py: "python", xml: "xml", vue: "xml", html: "html", css: "css", scss: "scss", less: "less",
  json: "json", md: "markdown", sql: "sql", yml: "yaml", yaml: "yaml", sh: "bash", ps1: "powershell",
  go: "go", rs: "rust", c: "c", cpp: "cpp", h: "c", properties: "ini", toml: "ini",
};

function langOfPath(path: string): string {
  const ext = (path.split(".").pop() || "").toLowerCase();
  return EXT_LANG[ext] ?? "";
}

/** 代码里本身含 ``` 时加长围栏，避免提前闭合 */
function fenceFor(code: string): string {
  let fence = "```";
  while (code.includes(fence)) fence += "`";
  return fence;
}

interface StreamCall {
  tool: string;
  params: Array<{ name: string; value: string; open: boolean }>;
}

function parseCalls(dsml: string): StreamCall[] {
  const calls: StreamCall[] = [];
  const invokes = [...dsml.matchAll(INVOKE)];
  invokes.forEach((m, i) => {
    const bodyStart = (m.index ?? 0) + m[0].length;
    const bodyEnd = i + 1 < invokes.length ? (invokes[i + 1].index ?? dsml.length) : dsml.length;
    const body = dsml.slice(bodyStart, bodyEnd);
    const params: StreamCall["params"] = [];
    for (const pm of body.matchAll(PARAM_OPEN)) {
      const vStart = (pm.index ?? 0) + pm[0].length;
      const rest = body.slice(vStart);
      const close = PARAM_CLOSE.exec(rest);
      const raw = close ? rest.slice(0, close.index) : rest;
      params.push({ name: pm[1], value: raw.replace(ANY_TAG, "").replace(PARTIAL_TAIL, ""), open: !close });
    }
    calls.push({ tool: m[1], params });
  });
  return calls;
}

/** 流式显示文本：不含 DSML 时原样返回（同一字符串，下游 memo 不受影响） */
export function rewriteDsmlForStream(text: string): string {
  const at = text.search(MARK);
  if (at < 0) {
    // 尾部半个标记（最多十余字符）先藏起来，下一帧补全后若确是 DSML 会走下方分支
    const tail = PARTIAL_TAIL.exec(text.slice(-12));
    return tail && tail[0].length > 0 ? text.slice(0, text.length - tail[0].length) : text;
  }
  const head = text.slice(0, at).replace(/\s+$/, "");
  const parts: string[] = head ? [head] : [];
  for (const call of parseCalls(text.slice(at))) {
    const verb = VERB[call.tool] ?? `调用 ${call.tool}`;
    const path = call.params.find((p) => p.name === "path" || p.name === "file_path")?.value.trim() ?? "";
    parts.push(`**正在准备${verb}**${path ? ` \`${path}\`` : ""}`);
    const code = CODE_PARAMS.map((n) => call.params.find((p) => p.name === n)).find(Boolean);
    const others = call.params.filter((p) => p !== code && !HIDDEN_PARAMS.has(p.name) && p.value.trim());
    if (others.length > 0) {
      parts.push(others.map((p) => `- ${p.name}：${p.value.trim().replace(/\s+/g, " ").slice(0, 120)}`).join("\n"));
    }
    if (code && code.value) {
      const lang = code.name === "command" ? "bash" : code.name === "sql" ? "sql" : langOfPath(path);
      const body = code.value.replace(/^\n/, "");
      const fence = fenceFor(body);
      // 参数仍在输出中：不写闭合围栏，未闭合的代码块在流式里同样按代码块渲染
      parts.push(`${fence}${lang}\n${body}${code.open ? "" : `\n${fence}`}`);
    }
  }
  if (parts.length === (head ? 1 : 0)) parts.push("**正在准备工具调用…**");
  return parts.join("\n\n");
}
