/** Node 侧的 TS 解析钩子（plan-562-2318 C2）。
 *
 *  【为什么需要】
 *  本仓源码与测试都写 `import { x } from "./foo"`（不带扩展名），并用 tsconfig 的
 *  `paths` 别名 `@chatcoder/shared` / `@/*`——这两件事平时由 Vite 与 tsc 负责。
 *  但 `node tests/xxx.test.ts` 直跑时，Node 的 ESM 解析器只认显式扩展名与真实包名，
 *  于是 16 个测试文件里有 7 个从来就跑不起来（`ERR_MODULE_NOT_FOUND`）。
 *  仓里也没有 tsx / ts-node 可用。
 *
 *  这里用 Node 内置的 module.register 注册一个 resolve 钩子（不引入任何依赖），
 *  在默认解析失败时依次尝试：
 *    1. 相对 / 绝对路径补 `.ts` / `.tsx` / `/index.ts` / `/index.tsx` / `.js`；
 *    2. tsconfig 里的两个别名：`@chatcoder/shared` → `packages/shared/src/index.ts`，
 *       `@/*` → `client/src/*`。
 *
 *  用法：`node --import ./scripts/ts-resolve.mjs tests/xxx.test.ts`
 *  由 `scripts/run-tests.mjs` 自动加上，`npm test` 即可。
 */
import { register } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const clientRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const sharedIndex = resolve(clientRoot, "..", "packages", "shared", "src", "index.ts");

// 子模块在独立的 hooks 线程里执行：把算好的路径以常量形式带进去
const hooksSource = `
import { existsSync } from "node:fs";
import { fileURLToPath, pathToFileURL } from "node:url";

const CLIENT_ROOT = ${JSON.stringify(clientRoot)};
const SHARED_INDEX = ${JSON.stringify(sharedIndex)};
const SUFFIXES = [".ts", ".tsx", "/index.ts", "/index.tsx", ".js"];
const HAS_EXT = /\\.(ts|tsx|js|mjs|cjs|json)$/;

/** tsconfig paths 里声明的别名（只这两个，新增别名时同步这里） */
function mapAlias(specifier) {
  if (specifier === "@chatcoder/shared") return SHARED_INDEX;
  if (specifier.startsWith("@chatcoder/shared/")) {
    return CLIENT_ROOT + "/../packages/shared/src/" + specifier.slice("@chatcoder/shared/".length);
  }
  if (specifier.startsWith("@/")) return CLIENT_ROOT + "/src/" + specifier.slice(2);
  return null;
}

export async function resolve(specifier, context, nextResolve) {
  try {
    return await nextResolve(specifier, context);
  } catch (err) {
    // ① 别名 → 真实文件
    const mapped = mapAlias(specifier);
    if (mapped) {
      for (const candidate of HAS_EXT.test(mapped) ? [mapped] : [mapped, ...SUFFIXES.map((s) => mapped + s)]) {
        if (existsSync(candidate)) {
          return nextResolve(pathToFileURL(candidate).href, context);
        }
      }
      throw err;
    }
    // ② 相对 / 绝对路径补扩展名（node: 内置模块与第三方包名直接放弃）
    if (!specifier.startsWith(".") && !specifier.startsWith("/") && !specifier.startsWith("file:")) {
      throw err;
    }
    if (HAS_EXT.test(specifier)) throw err;
    const base = specifier.startsWith("file:")
      ? fileURLToPath(specifier)
      : fileURLToPath(new URL(specifier, context.parentURL));
    for (const suffix of SUFFIXES) {
      const candidate = base + suffix;
      if (existsSync(candidate)) {
        return nextResolve(pathToFileURL(candidate).href, context);
      }
    }
    throw err;
  }
}
`;

// 用 data: URL 注册，避免额外落一个 hooks 文件（保留 .ts 扩展名让 Node 直接剥类型）
register(`data:text/javascript,${encodeURIComponent(hooksSource)}`, pathToFileURL(import.meta.filename));

// 关键：Node 22+ 默认的 TS 支持是 strip-only（只剥类型），遇到 `enum` / `namespace` 等
// 需要真正转换的语法会抛 ERR_UNSUPPORTED_TYPESCRIPT_SYNTAX。本仓源码用了 enum，
// 因此把类型转换级别提到 transform（Node 内置 transpile，不引入依赖）。
process.env.NODE_OPTIONS = [process.env.NODE_OPTIONS, "--experimental-transform-types"]
  .filter(Boolean).join(" ");
