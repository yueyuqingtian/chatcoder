/** 文件 / 文件夹类型图标（plan-20261009-ui U3/U8，plan-771-3177 扩充）。
 *
 * 取代此前两套并行口径：文件树的文字徽标（TS/JS…）与 chat/FileBadge 的字母色块。
 * 解析顺序：完整文件名 → 文件名前缀（tsconfig.* / vite.config.* …）→ 复合后缀（.d.ts / .test.ts / .spec.tsx / .config.ts / .module.css …）→ 单扩展名 → 兜底文件。
 * 文件夹：按名称映射专用文件夹图标，展开态自动取 `-open` 版本，未知名称用通用文件夹。
 * 路径数据为静态内置子集（见 fileIconData.ts），以 innerHTML 注入是安全的。 */
import { memo } from "react";
import {
  FILE_EXT_MAP, FILE_ICON_BODIES, FILE_NAME_MAP, FILE_PREFIX_MAP, FOLDER_NAME_MAP,
} from "./fileIconData";

/** 取路径末段（兼容 \\ 与 /） */
export function baseName(path: string): string {
  const clean = path.replace(/\\/g, "/").replace(/\/+$/, "");
  const i = clean.lastIndexOf("/");
  return i < 0 ? clean : clean.slice(i + 1);
}

/** 复合后缀 → 图标 key（优先精确匹配 .d.ts / 测试 / 配置 / CSS Module 等常见复合后缀） */
const COMPOUND_SUFFIX_MAP: Array<[string, string]> = [
  [".d.ts", "typescript-def"],
  [".d.mts", "typescript-def"],
  [".d.cts", "typescript-def"],
  [".test.tsx", "test-ts"],
  [".spec.tsx", "test-ts"],
  [".test.ts", "test-ts"],
  [".spec.ts", "test-ts"],
  [".test.jsx", "test-js"],
  [".spec.jsx", "test-js"],
  [".test.js", "test-js"],
  [".spec.js", "test-js"],
  [".test.mjs", "test-js"],
  [".spec.mjs", "test-js"],
  [".test.cjs", "test-js"],
  [".spec.cjs", "test-js"],
  [".module.scss", "sass"],
  [".module.sass", "sass"],
  [".module.less", "less"],
  [".module.css", "css"],
  [".config.ts", "settings"],
  [".config.mts", "settings"],
  [".config.cts", "settings"],
  [".config.js", "settings"],
  [".config.mjs", "settings"],
  [".config.cjs", "settings"],
  [".config.json", "settings"],
];

export function fileIconKey(nameOrPath: string): string {
  const name = baseName(nameOrPath).toLowerCase();
  const exact = FILE_NAME_MAP[name];
  if (exact) return exact;
  for (const [prefix, key] of FILE_PREFIX_MAP) {
    if (name.startsWith(prefix)) return key;
  }
  for (const [suffix, key] of COMPOUND_SUFFIX_MAP) {
    if (name.endsWith(suffix)) return key;
  }
  const dot = name.lastIndexOf(".");
  if (dot >= 0) {
    const ext = name.slice(dot + 1);
    const byExt = FILE_EXT_MAP[ext];
    if (byExt) return byExt;
  }
  return "file";
}

export function folderIconKey(nameOrPath: string, open: boolean): string {
  const base = FOLDER_NAME_MAP[baseName(nameOrPath).toLowerCase()] ?? "folder-base";
  return open ? `${base}-open` : base;
}

interface FileIconProps {
  /** 文件名或路径（只看末段） */
  name: string;
  size?: number;
  /** 作为文件夹渲染 */
  folder?: boolean;
  /** 文件夹展开态 */
  open?: boolean;
  className?: string;
}

export const FileIcon = memo(function FileIcon({ name, size = 16, folder = false, open = false, className }: FileIconProps) {
  const key = folder ? folderIconKey(name, open) : fileIconKey(name);
  const [box, body] = FILE_ICON_BODIES[key] ?? FILE_ICON_BODIES.file;
  return (
    <span className={"file-ic" + (className ? ` ${className}` : "")} style={{ width: size, height: size }} aria-hidden>
      <svg viewBox={`0 0 ${box} ${box}`} width={size} height={size} dangerouslySetInnerHTML={{ __html: body }} />
    </span>
  );
});
