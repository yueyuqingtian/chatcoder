/** plan-20261107-message-meta-implementation：服务端 UTC 与本地日期边界不能相差一个时区。 */
import assert from "node:assert/strict";
import { formatMessageTime, parseUtc } from "../src/utils/time";

const utc = parseUtc("2026-11-07 23:45:12.123456");
assert.equal(utc, Date.parse("2026-11-07T23:45:12.123Z"));
assert.equal(parseUtc("2026-11-08T07:45:12.123+08:00"), utc);
assert.equal(parseUtc("2026-11-07T18:45:12.123-05:00"), utc);
assert.equal(parseUtc(null), 0);
assert.equal(parseUtc("无效时间"), 0);
for (const timestamp of [0, NaN, Infinity, -1, 9e15]) assert.equal(formatMessageTime(timestamp), "");

const year = new Date().getFullYear();
assert.equal(formatMessageTime(new Date(year, 10, 7, 4, 5).getTime()), "11月7日 04:05");
assert.equal(formatMessageTime(new Date(year - 1, 11, 31, 23, 59).getTime()), `${year - 1}年12月31日 23:59`);
assert.equal(formatMessageTime(new Date(year, 0, 1, 0, 0).getTime()), "1月1日 00:00");

// 显示必须遵从本地时区，即使 UTC 日期与本地日期不同。
const d = new Date(utc);
const prefix = d.getFullYear() === year ? "" : `${d.getFullYear()}年`;
assert.equal(formatMessageTime(utc), `${prefix}${d.getMonth() + 1}月${d.getDate()}日 ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`);
console.log("通过：UTC、显式时区、跨日、跨年、分钟补零与缺失时间回归");
