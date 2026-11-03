/** 用户气泡「超长折叠」判据的回归测试（plan-631-2594）。
 *
 *  背景：子代理面板里长消息有时不折叠、也看不到「展开全文」按钮——根因是折叠与否
 *  完全依赖一次像素测量，测量被跳过就永远停在「不折叠」。这里覆盖补偿用的文本判据，
 *  确保它只对「一定超过 6 行」的文本返回 true，不会把正常消息误折叠。
 *
 *  运行：client 目录下 `npm test -- clamp`（或 `npm test` 全量）。
 */
import { COLLAPSE_CHARS, COLLAPSE_LINES, isLongUserMessage } from "../src/components/chat/userBubbleClamp.ts";

let failures = 0;
function eq(actual: unknown, expected: unknown, msg: string) {
  if (actual !== expected) {
    failures++;
    console.error(`FAIL: ${msg}\n   actual:   ${String(actual)}\n   expected: ${String(expected)}`);
  } else {
    console.log(`ok: ${msg}`);
  }
}

const lines = (n: number, line = "一行内容") => Array.from({ length: n }, () => line).join("\n");

// ① 阈值常量
eq(COLLAPSE_LINES, 6, "折叠行数阈值为 6 行");
eq(COLLAPSE_CHARS, 800, "兜底字符阈值为 800");

// ② 空文本与短文本不折叠
eq(isLongUserMessage(""), false, "空文本不折叠");
eq(isLongUserMessage("你好"), false, "单行短文本不折叠");
eq(isLongUserMessage(lines(6)), false, "恰好 6 个硬换行不折叠");
eq(isLongUserMessage(lines(7)), true, "7 个硬换行必然超过 6 行，判超长");

// ③ 字符数兜底：超过阈值即判超长（不读布局）
eq(isLongUserMessage("字".repeat(COLLAPSE_CHARS)), false, "恰好 800 字符仍交给像素测量");
eq(isLongUserMessage("字".repeat(COLLAPSE_CHARS + 1)), true, "801 字符直接判超长");

// ④ 边界：阈值内且不换行的长段仍交给测量，避免误折叠（窗口很宽时它可能只占几行）
eq(isLongUserMessage("字".repeat(300)), false, "300 字符无换行时不下结论");

// ⑤ 真实回归样本：子代理任务 prompt（37 行 / 2100 字符，取自现场数据）必须命中
const realPrompt = `调研主题切换图标颜色割裂\n\n只读调研任务，**绝对不要修改任何文件**。项目 D:\\myProject\\chatcoder，前端 client/src。\n`
  + lines(34, "- 检查 styles/themes.css 里图标相关变量在两套主题下是否都有定义（深色主题缺失会导致偏色）");
eq(realPrompt.split("\n").length, 37, "样本行数符合现场数据");
eq(realPrompt.length > COLLAPSE_CHARS, true, "样本字符数超过兜底阈值");
eq(isLongUserMessage(realPrompt), true, "现场那条子代理 prompt 会被折叠");

// ⑥ 去掉 Markdown 结构后的纯文本（含 \r\n）同样按硬换行计数
eq(isLongUserMessage("a\r\nb\r\nc\r\nd\r\ne\r\nf\r\ng"), true, "CRLF 换行同样计入行数");

if (failures > 0) {
  console.error(`\n${failures} 项失败`);
  process.exit(1);
}
console.log("\n全部通过");
