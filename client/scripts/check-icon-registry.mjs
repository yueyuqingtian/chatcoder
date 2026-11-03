#!/usr/bin/env node
/**
 * 图标注册表守卫（plan-630-2590），接入 npm run lint:design。
 *
 * 拦两类会让图标体系悄悄失控的写法：
 *  1. 业务代码直接 import "@ant-design/icons-svg"——绕过 icons.tsx 后，图标名散落在上百个文件里，
 *     既无法统一尺寸/描边补偿，也无法审计；且很容易写成 `import * as icons` 把 3.6MB 全量打进包。
 *  2. registry.ts 里登记的图标在包里不存在——重命名或打错字时只有运行到那行才报错，
 *     构建阶段应当直接失败（`tsc` 能查出来，但这里对纯字符串键的引用也做一次兜底）。
 *
 * 用法：node scripts/check-icon-registry.mjs
 */
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = fileURLToPath(new URL("..", import.meta.url));
const SRC = join(ROOT, "src");
const REGISTRY = join(SRC, "components", "antd-icons", "registry.ts");
/** 唯一允许直接依赖图标数据包的位置 */
const ALLOWED = new Set([REGISTRY, join(SRC, "components", "antd-icons", "AntIcon.tsx")]);
const PKG = "@ant-design/icons-svg";
const ICON_PKG_DIR = join(ROOT, "node_modules", PKG, "es");

const problems = [];

function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    const st = statSync(p);
    if (st.isDirectory()) walk(p, out);
    else if (/\.(ts|tsx)$/.test(name)) out.push(p);
  }
  return out;
}

const show = (file) => relative(ROOT, file).split(sep).join("/");

// ① 业务代码不得直接依赖图标数据包
for (const file of walk(SRC)) {
  if (ALLOWED.has(file)) continue;
  const src = readFileSync(file, "utf8");
  if (src.includes(`"${PKG}"`) || src.includes(`'${PKG}'`)) {
    problems.push(`${show(file)}：请从 components/icons 引入图标，不要直接依赖 ${PKG}`);
  }
}

// ② registry.ts 登记的图标必须在包里存在
if (!existsSync(REGISTRY)) {
  problems.push("缺少 components/antd-icons/registry.ts");
} else if (!existsSync(ICON_PKG_DIR)) {
  problems.push(`未安装 ${PKG}，请先运行 npm install`);
} else {
  const registry = readFileSync(REGISTRY, "utf8");
  const names = new Set();
  for (const m of registry.matchAll(/^\s+\w+:\s*(\w+Outlined),/gm)) names.add(m[1]);
  if (names.size === 0) problems.push("registry.ts 里没有解析到任何图标登记（正则失配？）");
  for (const name of names) {
    if (!existsSync(join(ICON_PKG_DIR, "asn", `${name}.d.ts`))) {
      problems.push(`registry.ts 引用了不存在的图标：${name}`);
    }
  }
}

if (problems.length) {
  console.error("[lint:icons] 图标体系检查未通过：\n" + problems.map((p) => "  " + p).join("\n"));
  process.exit(1);
}
console.log("[lint:icons] 图标体系检查通过");
