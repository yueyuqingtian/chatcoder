/**
 * 流式 Markdown 切块与半截结构处理测试。
 * 纯逻辑，Node 原生 TS 运行：`node tests/stream-markdown.test.ts`（client 目录）。
 *
 * 口径：只有已换行的行参与封块判定；表头行等分隔行成立后整表出现；未闭合围栏立即成代码块；
 * 逐字符喂入的增量结果必须与一次性全量切块一致（增量幂等）。
 */
import { advanceSplit, createSplitState, prepareTail, type SplitState } from "../src/components/chat/streamMarkdown.ts";

let failures = 0;
function eq(actual: unknown, expected: unknown, msg: string) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) { failures++; console.error(`FAIL: ${msg}\n   actual:   ${a}\n   expected: ${e}`); }
  else { console.log(`ok: ${msg}`); }
}

const tail = (s: string) => { const st = createSplitState(s); return prepareTail(st.pending, st.pendingFence); };

// —— 根因 1：末尾换行后的空串不是空行 ——
{
  const st = createSplitState("| a | b |\n");
  eq(st.sealed, [], "表头行以换行结尾时不被封块");
  eq(st.pending, "| a | b |\n", "表头行保留在尾块");
  const st2 = advanceSplit(st, "| a | b |\n|---|---|\n| 1 | 2 |");
  eq(st2.sealed, [], "分隔行与数据行继续并入同一块");
  eq(st2.pending, "| a | b |\n|---|---|\n| 1 | 2 |", "整张表在同一尾块");
  eq(createSplitState("line1\n").sealed, [], "段落内软换行不被拆块");
}

// —— 顶层空行封块 ——
{
  const st = createSplitState("第一段\n\n第二段\n\n");
  eq(st.sealed, ["第一段", "第二段"], "顶层空行封块");
  eq(st.pending, "", "空行后尾块为空");
  eq(createSplitState("a\n\n\n\nb").sealed, ["a"], "连续空行只封一次");
}

// —— 根因 2：尾块内围栏状态重扫幂等 ——
{
  let st = createSplitState("```py\nx = 1\n");
  eq(st.pendingFence, null, "尾块起点在围栏之外");
  st = advanceSplit(st, "```py\nx = 1\n\ny = 2\n");
  eq(st.sealed, [], "围栏内空行不封块（逐帧续扫不翻转围栏状态）");
  st = advanceSplit(st, "```py\nx = 1\n\ny = 2\n```\n后续文字");
  eq(st.sealed, ["```py\nx = 1\n\ny = 2\n```"], "顶层围栏闭合即封块");
  eq(st.pending, "后续文字", "闭合后的文字进入新尾块");
}

// —— 增量幂等：逐字符喂入 == 一次性切块 ——
{
  const doc = [
    "# 标题", "", "说明文字", "第二行", "",
    "| 项 | 结果 |", "|---|---|", "| a | 1 |", "| b | 2 |", "",
    "```ts", "const a = 1;", "", "const b = 2;", "```", "尾巴", "",
    "~~~", "tilde", "~~~", "", "- x", "- y", "",
  ].join("\n");
  let inc: SplitState | null = null;
  for (let i = 1; i <= doc.length; i++) inc = advanceSplit(inc, doc.slice(0, i));
  const full = createSplitState(doc);
  eq(inc!.sealed, full.sealed, "逐字符增量与全量切块的封块一致");
  eq(inc!.pending, full.pending, "逐字符增量与全量切块的尾块一致");
  eq(full.sealed.length, 6, "样例文档封 6 块，末尾列表留在尾块");
}

// —— 非追加变化：全量重算 ——
{
  let st = createSplitState("abc\n\ndef");
  st = advanceSplit(st, "xyz");
  eq([st.sealed, st.pending], [[], "xyz"], "非追加变化时重算");
}

// —— 表格：分隔行成立前藏起表头 ——
eq(tail("| 项 | 结果 |"), { text: "", kind: "text" }, "表头写到一半：不显示竖线纯文本");
eq(tail("| 项 | 结果 |\n"), { text: "", kind: "text" }, "表头写完等分隔行：继续藏起");
eq(tail("| 项 | 结果 |\n|"), { text: "", kind: "text" }, "分隔行只写了竖线：继续藏起");
eq(tail("| 项 | 结果 |\n|---|"), { text: "", kind: "text" }, "分隔行格数不够：继续藏起");
eq(tail("| 项 | 结果 |\n|---|--").kind, "table", "分隔行格数够即成表");
eq(tail("前言\n| 项 | 结果 |"), { text: "前言", kind: "text" }, "前言文字照常显示，仅藏表头");
{
  const t = tail("| 项 | 结果 |\n|---|---|\n| a | 1 |\n| b");
  eq(t.kind, "table", "数据行逐行追加仍是表格");
  eq(t.text, "| 项 | 结果 |\n|---|---|\n| a | 1 |\n| b", "半截数据行随内容增长");
  eq(tail("| 项 | 结果 |\n|---|---|\n| a | 1 |\n|").text, "| 项 | 结果 |\n|---|---|\n| a | 1 |", "只有一根竖线的空行骨架不追加");
  eq(tail("| 项 | 结果 |\n|---|---|\n|").kind, "table", "只有表头与分隔行也是表格");
}
eq(tail("| a |\nhello").kind, "text", "竖线行后跟普通文字不是表格");
eq(tail("| a | b |\n| x | y |\n").kind, "text", "第二行不是分隔行：按普通文本显示");
eq(tail("| a | b |\n| x | y |\n").text, "| a | b |\n| x | y |\n", "非表格的竖线文本原样显示");

// —— 围栏：未闭合立即成代码块，半截标记等换行 ——
eq(tail("```py\nx = 1"), { text: "```py\nx = 1", kind: "fence" }, "未闭合围栏立即按代码块");
eq(tail("```py\n"), { text: "```py\n", kind: "fence" }, "围栏开栏行换行后即出现空代码块");
eq(tail("说明\n``"), { text: "说明\n", kind: "text" }, "半截开栏标记（两个反引号）先藏起");
eq(tail("说明\n```py").text, "说明\n", "开栏行语言标签未换行时先藏起");
eq(tail("```py\nx = 1\n``").text, "```py\nx = 1\n", "围栏内半截闭合标记先藏起");
eq(tail("```py\nx = 1\n```").kind, "fence", "闭合标记写全即闭合");
eq(tail("用 ```code``` 表示").kind, "text", "行内三反引号不当作围栏");
{
  const t = tail("````md\n```js\nx\n```\n还在外层");
  eq(t.kind, "fence", "四反引号围栏不被内层三反引号提前闭合");
}

// —— 空文本 ——
eq(tail(""), { text: "", kind: "text" }, "空文本");

if (failures > 0) { console.error(`\n${failures} 项失败`); process.exit(1); }
console.log("\n全部通过");
