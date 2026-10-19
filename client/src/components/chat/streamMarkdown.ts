/** streamMarkdown —— 流式 Markdown 的切块与「半截结构」处理（纯函数，无 React 依赖）。
 *
 * 主消息流 / 子代理 / 右侧「侧边对话」的流式正文都经 StreamingMarkdown 渲染，三处共用本模块，
 * 不各修各的。解决两类「流式中显示成纯文本、结束后才跳成表格/代码块」的问题：
 *
 * ① 切块判定把「末尾没有换行的最后一行」当成了空行：
 *    "| a | b |\n" 末尾的空串被误判为顶层空行，表头行被永久封成独立块，
 *    后续分隔行、数据行落进别的块，整张表一直是竖线纯文本，直到落库整段重渲染才变成表格。
 *    现在只有「已以换行结束的行」才参与判定，最后一行视为未完成。
 * ② 续扫时把「上次扫描结束的围栏状态」当成「尾块起点的围栏状态」：
 *    尾块里已含 ``` 开栏行时，重扫该行会把围栏状态翻回去，后续代码行被当成正文。
 *    现在记录的是尾块起点的状态，重扫幂等。
 *
 * 另外把「还没长成结构的半截内容」先藏起来（只影响流式显示，落库后以完整文本渲染）：
 *  - 表头行出现但分隔行（|---|）还没写完：不显示成竖线文本，分隔行成立的那一刻整张表带表头出现；
 *  - 尾部的半截围栏标记（「``」「```py」还没换行）：等换行后直接以代码块出现，语言标签不闪变。
 */

/** 围栏状态：字符、长度、开栏行缩进是否 ≤ 3（顶层围栏，闭合后可立即封块） */
export interface Fence {
  ch: string;
  len: number;
  top: boolean;
}

/** 增量切块状态：已封块 + 尾块（含最后一行未完成的内容）+ 尾块起点的围栏状态 */
export interface SplitState {
  /** 上次输入文本（用于判断是否只是「追加」） */
  src: string;
  /** 已封块（顶层空行 / 顶层围栏闭合处封块，之后不再变化） */
  sealed: string[];
  /** 尾块原文（可能以半行结尾；围栏内的空行不封块） */
  pending: string;
  /** 尾块第一个字符处的围栏状态（重扫尾块时的起点） */
  pendingFence: Fence | null;
}

export type TailKind = "text" | "fence" | "table";

export interface PreparedTail {
  /** 用于渲染的尾块文本（半截结构已藏起来；可能为空串） */
  text: string;
  /** 尾块类别：fence 才值得做语法高亮，其余走轻量渲染 */
  kind: TailKind;
}

const FENCE_LINE = /^(\s*)(`{3,}|~{3,})(.*)$/;
const FENCE_CLOSE = /^\s*(`{3,}|~{3,})\s*$/;
/** 末行是围栏标记的开头（1~2 个反引号/波浪线）：可能是下一帧才写全的围栏 */
const FENCE_PREFIX = /^\s*(`{1,2}|~{1,2})$/;

/** 该行若是围栏开栏行则返回围栏信息（反引号围栏的信息串不得含反引号，否则是行内代码） */
function openFence(line: string): Fence | null {
  const m = FENCE_LINE.exec(line);
  if (!m) return null;
  if (m[2][0] === "`" && m[3].includes("`")) return null;
  return { ch: m[2][0], len: m[2].length, top: m[1].length <= 3 };
}

function closesFence(line: string, f: Fence): boolean {
  const m = FENCE_CLOSE.exec(line);
  return !!m && m[1][0] === f.ch && m[1].length >= f.len;
}

/** 去掉块尾换行（块内部原样保留） */
function trimBlock(s: string): string {
  return s.replace(/\s+$/, "");
}

/** 对「尾块原文 + 新增」重新扫描：已完成的行才参与判定，封好的块推入 sealed，其余留作新尾块。 */
function rescan(state: SplitState, text: string): void {
  let fence = state.pendingFence;
  let blockStart = 0;
  let pos = 0;
  for (;;) {
    const nl = text.indexOf("\n", pos);
    if (nl < 0) break; // 其后是尚未换行的最后一行：不参与封块判定
    const line = text.slice(pos, nl);
    const lineEnd = nl + 1;
    if (fence) {
      if (closesFence(line, fence)) {
        const seal = fence.top;
        fence = null;
        // 顶层围栏闭合即封块：代码块立刻获得完整插件渲染，闭合后紧跟的文字也不会拖着它不封
        if (seal) {
          state.sealed.push(trimBlock(text.slice(blockStart, nl)));
          blockStart = lineEnd;
        }
      }
    } else {
      const f = openFence(line);
      if (f) {
        fence = f;
      } else if (line.trim() === "") {
        if (blockStart < pos) state.sealed.push(trimBlock(text.slice(blockStart, pos)));
        blockStart = lineEnd;
      }
    }
    pos = lineEnd;
  }
  // 起点仍在原尾块开头 → 沿用原起点围栏状态；否则新块必然起于围栏之外
  state.pendingFence = blockStart === 0 ? state.pendingFence : null;
  state.pending = text.slice(blockStart);
}

export function createSplitState(src = ""): SplitState {
  const st: SplitState = { src, sealed: [], pending: "", pendingFence: null };
  rescan(st, src);
  return st;
}

/** 增量切块入口：追加场景只扫「尾块 + 新增」（O(新增)）；文本不再以上次为前缀时全量重算。 */
export function advanceSplit(prev: SplitState | null, src: string): SplitState {
  if (!prev || !src.startsWith(prev.src)) return createSplitState(src);
  if (src !== prev.src) {
    const delta = src.slice(prev.src.length);
    prev.src = src;
    rescan(prev, prev.pending + delta);
  }
  return prev;
}

/** 表格行：去首尾竖线后按未转义竖线切分 */
function cellsOf(line: string): string[] {
  let s = line.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|") && !s.endsWith("\\|")) s = s.slice(0, -1);
  return s.split(/(?<!\\)\|/);
}

/** 分隔行成立：每格形如 `---` / `:--` / `--:` / `:-:`，且格数与表头一致（与 GFM 解析口径一致） */
function isDelimiterRow(line: string, headCells: number): boolean {
  const cells = cellsOf(line);
  return cells.length === headCells && cells.every((c) => /^\s*:?-+:?\s*$/.test(c));
}

/** 分隔行还没写完时的样子（只含竖线、冒号、短横线与空白） */
const DELIMITER_PREFIX = /^[\s|:-]*$/;

/** 流式显示用：处理尾块里「还没长成结构」的半截内容，并判定尾块类别。 */
export function prepareTail(pending: string, pendingFence: Fence | null): PreparedTail {
  if (pending.trim() === "") return { text: "", kind: "text" };
  const lastNl = pending.lastIndexOf("\n");
  const complete = pending.slice(0, lastNl + 1);
  const partial = pending.slice(lastNl + 1);

  // 已完成行推出的围栏状态
  let fence = pendingFence;
  let from = 0;
  for (;;) {
    const nl = complete.indexOf("\n", from);
    if (nl < 0) break;
    const line = complete.slice(from, nl);
    if (fence) { if (closesFence(line, fence)) fence = null; }
    else { const f = openFence(line); if (f) fence = f; }
    from = nl + 1;
  }

  if (fence) {
    // 围栏内：末行若是半截闭合标记（「``」）先不显示，免得闪一下代码正文再消失
    return { text: FENCE_PREFIX.test(partial) ? complete : pending, kind: "fence" };
  }
  // 围栏外：末行是半截开栏标记（「``」「```py」）→ 等换行后直接以代码块出现
  let text = pending;
  if (FENCE_PREFIX.test(partial) || openFence(partial)) text = complete;

  // 表格：取末尾连续的「竖线开头」行
  const lines = text.split("\n");
  const endsOpen = !text.endsWith("\n"); // 最后一行是否未完成
  const last = endsOpen ? lines.length - 1 : lines.length - 2;
  let first = last + 1;
  while (first - 1 >= 0 && lines[first - 1].trimStart().startsWith("|")) first--;
  const runLen = last + 1 - first;
  if (runLen <= 0) return { text, kind: "text" };

  const before = lines.slice(0, first).join("\n");
  if (runLen === 1) {
    // 只有表头候选行：藏起来，等分隔行
    return { text: before, kind: "text" };
  }
  const head = cellsOf(lines[first]).length;
  const delim = lines[first + 1];
  if (isDelimiterRow(delim, head)) {
    // 已成表：末行若只有一根竖线（空行骨架）先不追加，免得闪一行空行
    if (endsOpen && /^\s*\|?\s*$/.test(lines[last]) && runLen > 2) {
      return { text: lines.slice(0, last).join("\n"), kind: "table" };
    }
    return { text, kind: "table" };
  }
  // 分隔行还没写完（它是最后一行且仍可能长成分隔行）→ 表头与分隔行一起藏起来
  if (runLen === 2 && endsOpen && DELIMITER_PREFIX.test(delim)) {
    return { text: before, kind: "text" };
  }
  return { text, kind: "text" };
}
