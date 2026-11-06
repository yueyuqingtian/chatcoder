/** plan-20261105-question-custom：覆盖切题保留焦点与显式离开输入行的回车目标。 */
import assert from "node:assert/strict";
import { questionEnterIndex } from "../src/components/chat/questionKeyboard";

const options = ["代码和文档一起改", "只改文档"];
const custom = "先讨论方案，暂时不改代码";
const customIdx = options.length;
const count = customIdx + 1;
const submit = (highlight: number, focused: boolean, text: string) => {
  const target = questionEnterIndex(highlight, count, customIdx, focused);
  return target < customIdx ? options[target] : text.trim() || null;
};

assert.equal(submit(0, true, custom), custom, "切到下一题高亮归零但输入仍聚焦时，应提交自定义内容");
assert.equal(submit(1, true, custom), custom, "返回已有选项的题目后，输入框回车不能提交旧选项");
assert.equal(submit(0, true, "  "), null, "聚焦空输入框时不能误提交首个选项");
assert.equal(submit(0, false, custom), options[0], "显式导航离开输入框后，回车仍选择高亮选项");
assert.equal(submit(1, false, ""), options[1], "普通选项键盘确认保持不变");
assert.equal(submit(customIdx, false, custom), custom, "键盘导航至自定义行可提交已有输入");
assert.equal(questionEnterIndex(0, 1, 0, true), 0, "无选项开放题仍定位输入行");
assert.equal(questionEnterIndex(0, count, customIdx, true), customIdx, "多选补充输入仍进入合并提交分支");
assert.equal(questionEnterIndex(9, count, customIdx, false), customIdx, "非聚焦状态仍限制高亮在当前题范围内");
console.log("通过：自定义回答焦点与键盘目标 9 项回归");
