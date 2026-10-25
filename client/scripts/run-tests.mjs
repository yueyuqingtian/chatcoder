/** 前端单测入口（plan-562-2318 C2）。
 *
 *  为什么需要这个脚本：`tests/` 下 16 个测试文件都是**自建断言**（各自维护 `assert()` 与
 *  `failures` 计数、末尾 `process.exit(failures ? 1 : 0)`），并不使用 `node:test`。
 *  因此 `node --test` 跑它们会全部报 “test failed”（它按 node:test 的协议等待用例注册）。
 *  此前也没有任何 npm script 指向这些文件，等于「写了测试但没有一键入口」。
 *
 *  这里按文件头注释里的用法逐个直跑（`node tests/xxx.test.ts`，Node ≥22 原生支持 TS），
 *  收集退出码，最后统一汇报。用法：`npm test`，或 `npm test -- progress` 只跑文件名含该词的。
 */
import { readdirSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const testsDir = join(here, "..", "tests");
const filter = process.argv[2] ?? "";

const files = readdirSync(testsDir)
  .filter((f) => f.endsWith(".test.ts"))
  .filter((f) => !filter || f.includes(filter))
  .sort();

if (files.length === 0) {
  console.error(filter ? `没有文件名包含「${filter}」的测试` : "tests/ 下没有找到 *.test.ts");
  process.exit(1);
}

const failed = [];
for (const f of files) {
  // plan-562-2318 C2：源码与测试都用无扩展名 import（交给 Vite/tsc 解析），
  // 直跑时必须挂上解析钩子，否则 7/15 个文件会 ERR_MODULE_NOT_FOUND。
  // 注意两点（都踩过）：
  //  · 入口脚本要传**相对路径**并把 cwd 指到 client/——Windows 下绝对路径会被 ESM loader
  //    当成 `d:` 协议；
  //  · `--import` 的参数同理用相对路径。
  const res = spawnSync(
    process.execPath,
    ["--experimental-transform-types", "--import", "./scripts/ts-resolve.mjs", `./tests/${f}`],
    { stdio: "inherit", cwd: join(here, "..") },
  );
  if (res.status !== 0) failed.push(f);
}

console.log("");
if (failed.length > 0) {
  console.error(`失败 ${failed.length}/${files.length}：`);
  for (const f of failed) console.error(`  - ${f}`);
  process.exit(1);
}
console.log(`全部通过：${files.length} 个测试文件`);
