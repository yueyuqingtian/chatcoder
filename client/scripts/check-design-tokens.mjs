#!/usr/bin/env node
/**
 * 设计规范守卫（lint:design，接入 npm run build）。
 *
 * 只检查 CLAUDE.md「设计语言」里能机器判定、且一旦回潮就会被用户一眼看出来的几条：
 *  1. 圆角只用令牌：禁止 5/6/7/9/10px 这类不在令牌阶梯上的字面量
 *     （令牌阶梯 2/4/8/12/14/16px + 999px 胶囊；1~4px 的细圆角视为细线条件，放行）；
 *  2. 过渡时长只用 --dur-*：transition 里禁止非零的 ms / s 字面量
 *     （var() 回退值、乘以 --motion-scale 的 calc()、!important 的减弱动效覆盖除外）；
 *  3. 禁止左侧装饰竖线：border-left 彩色实线、inset 1~4px 的左侧阴影色条；
 *  4. 禁止原生 title 悬停提示：小写 JSX 元素上的 title 属性一律改用 ui/Tooltip（iframe 的无障碍名称除外）。
 *
 * 宠物浮窗（src/pet/）是独立的视觉体系，不在检查范围内。
 *
 * 存量口径：规范允许存量逐步替换（如原生 title），所以按「文件 × 规则」记一份基线
 * （scripts/design-baseline.json）——只拦新增，某个文件的违规数超过基线才算失败；
 * 改掉存量后用 --update-baseline 收紧基线，基线只会越来越小。
 *
 * 用法：
 *   node scripts/check-design-tokens.mjs                    检查（超出基线时列出 文件:行，退出码 1）
 *   node scripts/check-design-tokens.mjs --update-baseline  按当前代码重写基线
 */
import { existsSync, readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { join, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = fileURLToPath(new URL("..", import.meta.url));
const SRC = join(ROOT, "src");
const EXCLUDE_DIRS = new Set([join(SRC, "pet")]);
const BASELINE_FILE = join(ROOT, "scripts", "design-baseline.json");

/** 允许出现的圆角字面量（px） */
const RADIUS_OK = new Set([0, 1, 2, 3, 4, 8, 12, 14, 16, 999, 9999]);
/** 非中性色（出现在左侧竖线上即视为装饰色条） */
const COLORED = /accent|primary|success|warn|error|danger|info|brand|#[0-9a-f]{3,8}\b|rgba?\(|hsla?\(|oklch\(|oklab\(|color-mix\(/i;

function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (EXCLUDE_DIRS.has(p)) continue;
    const st = statSync(p);
    if (st.isDirectory()) walk(p, out);
    else if (/\.(css|tsx)$/.test(name)) out.push(p);
  }
  return out;
}

/** 把注释替换成等长空白（保留换行），行号不变 */
function blankComments(src, lineComments) {
  const re = lineComments ? /\/\*[\s\S]*?\*\/|(^|[^:\\])\/\/[^\n]*/g : /\/\*[\s\S]*?\*\//g;
  return src.replace(re, (m, lead) => {
    const keep = typeof lead === "string" ? lead : "";
    return keep + m.slice(keep.length).replace(/[^\n]/g, " ");
  });
}

const lineOf = (src, index) => src.slice(0, index).split("\n").length;

/** 删除 name( … ) 形式的平衡括号片段；filter 返回 false 的片段保留 */
function stripCalls(value, name, filter = () => true) {
  let out = "";
  let i = 0;
  while (i < value.length) {
    const at = value.indexOf(name + "(", i);
    if (at < 0) { out += value.slice(i); break; }
    let depth = 0;
    let j = at + name.length;
    for (; j < value.length; j++) {
      if (value[j] === "(") depth++;
      else if (value[j] === ")" && --depth === 0) break;
    }
    const seg = value.slice(at, j + 1);
    out += value.slice(i, at) + (filter(seg) ? " " : seg);
    i = j + 1;
  }
  return out;
}

function checkCss(file, raw, report) {
  const src = blankComments(raw, false);
  const decl = /([a-z-]+)\s*:\s*([^;{}]+)/gi;
  for (let m = decl.exec(src); m; m = decl.exec(src)) {
    const prop = m[1].toLowerCase();
    const value = m[2];
    const line = lineOf(src, m.index);
    if (/^border(-[a-z]+)*-radius$/.test(prop)) {
      const bare = stripCalls(stripCalls(value, "var"), "calc");
      for (const px of bare.matchAll(/(-?\d*\.?\d+)px/g)) {
        const n = Number(px[1]);
        if (!RADIUS_OK.has(n)) {
          report(file, line, "radius", `圆角 ${px[0]} 不在令牌阶梯上，请改用 --r-xs/sm/md/lg/pop 或 999px`);
        }
      }
    } else if (prop === "transition" || prop === "transition-duration") {
      if (/!important/.test(value)) continue; // 减弱动效的全局覆盖
      // 先剥「× --motion-scale」的 calc（剥 var 会把判据一起剥掉），再剥 var() 回退值
      const bare = stripCalls(stripCalls(value, "calc", (seg) => seg.includes("--motion-scale")), "var");
      for (const d of bare.matchAll(/(^|[\s,(])(\d*\.?\d+)(ms|s)\b/g)) {
        if (Number(d[2]) !== 0) {
          report(file, line, "duration", `过渡时长 ${d[2]}${d[3]} 是字面量，请改用 var(--dur-*)（已含 --motion-scale）`);
        }
      }
    } else if (prop === "border-left" || prop === "border-left-color" || prop === "border-inline-start") {
      if (/\bsolid\b|^\s*(var|#|rgb|hsl|oklch)/i.test(value) && COLORED.test(value) && !/transparent/.test(value)) {
        report(file, line, "left-bar", "禁止用左侧彩色竖线表达状态 / 分组，请改用留白、底色、图标或角标");
      }
    } else if (prop === "box-shadow") {
      if (/inset\s+[1-4](\.\d+)?px\s+0(px)?\s+0/.test(value)) {
        report(file, line, "left-bar", "禁止用 inset 左侧色条表达状态，请改用底色、图标或角标");
      }
    }
  }
}

/** 小写 JSX 元素上的原生 title 属性（跳过字符串与 {} 表达式，支持跨行标签） */
function checkTsx(file, raw, report) {
  const src = blankComments(raw, true);
  const open = /<([a-z][a-z0-9-]*)(?=[\s/>])/g;
  for (let m = open.exec(src); m; m = open.exec(src)) {
    // 紧贴标识符的 < 是泛型实参（useState<string>），不是 JSX
    if (m.index > 0 && /[\w$.)\]]/.test(src[m.index - 1])) continue;
    const tag = m[1];
    let attrs = "";
    let depth = 0;
    let quote = "";
    for (let i = open.lastIndex; i < src.length; i++) {
      const c = src[i];
      if (quote) {
        if (c === quote && src[i - 1] !== "\\") quote = "";
        continue;
      }
      if (depth === 0 && (c === '"' || c === "'")) { quote = c; attrs += " "; continue; }
      if (c === "`") { quote = c; continue; }
      if (c === "{") { depth++; continue; }
      if (c === "}") { depth--; continue; }
      if (depth > 0) continue;
      if (c === ">") break;
      attrs += c;
    }
    if (tag !== "iframe" && /(^|\s)title\s*=/.test(attrs)) {
      report(file, lineOf(src, m.index), "native-title", `<${tag}> 使用了原生 title 悬停提示，请改用 ui/Tooltip`);
    }
  }
}

/** 「文件|规则」→ 该文件该规则下的全部违规 */
const found = new Map();
const report = (file, line, rule, msg) => {
  const key = `${relative(ROOT, file).split(sep).join("/")}|${rule}`;
  if (!found.has(key)) found.set(key, []);
  found.get(key).push({ line, msg });
};

for (const file of walk(SRC)) {
  const raw = readFileSync(file, "utf8");
  if (file.endsWith(".css")) checkCss(file, raw, report);
  else checkTsx(file, raw, report);
}

const counts = Object.fromEntries([...found].sort(([a], [b]) => a.localeCompare(b)).map(([k, v]) => [k, v.length]));

if (process.argv.includes("--update-baseline")) {
  writeFileSync(BASELINE_FILE, JSON.stringify(counts, null, 2) + "\n", "utf8");
  const total = Object.values(counts).reduce((s, n) => s + n, 0);
  console.log(`[lint:design] 基线已更新：${Object.keys(counts).length} 个文件 × 规则，共 ${total} 处存量`);
  process.exit(0);
}

const baseline = existsSync(BASELINE_FILE) ? JSON.parse(readFileSync(BASELINE_FILE, "utf8")) : {};
const problems = [];
let shrinkable = 0;
for (const [key, list] of found) {
  const allowed = Number(baseline[key] ?? 0);
  if (list.length > allowed) {
    const [file] = key.split("|");
    problems.push(`${file}（${list.length} 处，基线 ${allowed}）`);
    for (const p of list) problems.push(`    ${file}:${p.line}  ${p.msg}`);
  }
}
for (const [key, allowed] of Object.entries(baseline)) {
  if ((found.get(key)?.length ?? 0) < allowed) shrinkable++;
}

if (problems.length) {
  console.error("[lint:design] 新增了违反设计规范的写法（同一文件里的存量也一并列出，便于定位）：\n"
    + problems.map((p) => "  " + p).join("\n"));
  process.exit(1);
}
const tip = shrinkable ? `；${shrinkable} 项存量已减少，可运行 --update-baseline 收紧基线` : "";
console.log(`[lint:design] 设计规范检查通过${tip}`);
