import assert from "node:assert/strict";
import { parseRefLines } from "../src/utils/refLines.ts";

// plan-20261104-session-mentions：撤回与排队编辑必须还原标题中的标点、换行和同名会话。
const sessions = [{ id: 17, title: '排查、登录「分支」\n第二行' }, { id: 23, title: '排查、登录「分支」\n第二行' }];
const suffix = `引用会话：${JSON.stringify(sessions)}（可通过会话上下文读取工具分页或按关键词查询）`;
const parsed = parseRefLines(`请继续分析\n\n${suffix}\n引用文件：@src/a.ts`);
assert.equal(parsed.text, "请继续分析");
assert.deepEqual(parsed.refs.slice(0, 2), sessions.map((s) => ({ kind: "session", value: String(s.id), label: s.title })));
assert.deepEqual(parsed.refs[2], { kind: "file", value: "src/a.ts", label: "src/a.ts" });
for (const malformed of ['引用会话：不是JSON', '引用会话：[{"id":-1,"title":"非法"}]', '引用会话：[{"id":"17","title":"非法"}]']) {
  assert.equal(parseRefLines(malformed).text, malformed);
  assert.deepEqual(parseRefLines(malformed).refs, []);
}
console.log("会话引用回填、特殊标题、同名目标与无效引用回归通过");
