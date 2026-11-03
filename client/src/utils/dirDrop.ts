/** plan-633-2604（R3）：拖入文件夹的共享工具。
 *
 * 用途：让「把文件夹拖进输入框 / 项目弹窗」这类操作拿到**可用于定位的目录路径**，
 * 从而把目录作为一条引用交给 AI，AI 侧用已有的 `fs_list`（别名 list_dir / glob）自行列目录内容。
 *
 * 为什么后端零改动：引用只随消息文本（引用行）与 content.refs 落库，`refs` 在服务端是
 * `list[dict]`，新增 kind 不需要迁移；列目录能力早已由 `server/app/orchestration/tools/fs_list.py`
 * 提供（工作区外目录走审批策略），所以这里只负责「把目录换成路径」，不需要任何上传接口。
 *
 * 两条取路径的路子（与 project/ProjectDialog 一致）：
 *   · 桌面版（Electron）：preload 暴露的 `webUtils.getPathForFile` 直接给出绝对路径；
 *   · 浏览器：页面拿不到绝对路径，只能读「目录名 + 首层子项名」，请本机后端按特征定位
 *     （`GET /utils/resolve-dir`）。
 */
import { api } from "../api/client";

/** 拖入项：drop 事件结束后 DataTransfer 会被清空，必须在事件内同步取出 entry 与 File */
export interface DroppedEntry {
  entry: FileSystemEntry | null;
  file: File | null;
}

/** 同步拆分拖入的目录项与文件项（必须在 drop 事件回调内调用） */
export function splitDroppedEntries(dt: DataTransfer | null): { dirs: DroppedEntry[]; files: File[] } {
  if (!dt) return { dirs: [], files: [] };
  const items = dt.items ? Array.from(dt.items).filter((it) => it.kind === "file") : [];
  // 退化路径：不支持 items / 不支持 webkitGetAsEntry（老浏览器、部分合成事件）时，
  // 全部按普通文件处理——保证「拖文件上传」这条原有行为一个字都不变。
  const noEntryApi = items.length > 0 && !items.some((it) => typeof it.webkitGetAsEntry === "function");
  if (items.length === 0 || noEntryApi) {
    return { dirs: [], files: Array.from(dt.files ?? []) };
  }
  const dirs: DroppedEntry[] = [];
  const files: File[] = [];
  for (const it of items) {
    const entry = it.webkitGetAsEntry?.() ?? null;
    const file = it.getAsFile();
    if (entry?.isDirectory) dirs.push({ entry, file });
    else if (file) files.push(file);
  }
  return { dirs, files };
}

/** 目录 → 绝对路径：桌面版取真实路径，浏览器版用名称 + 首层子项让后端定位 */
export async function resolveDroppedDir(entry: FileSystemDirectoryEntry, file: File | null): Promise<string> {
  // 桌面版：preload 直接给出绝对路径（Electron 32+ 已移除 File.path）
  if (file) {
    const abs = window.chatcoderAPI?.getPathForFile?.(file) ?? "";
    if (abs) return abs;
  }
  // 浏览器：读不到路径，按「名称 + 子项」让后端在本机定位
  const kids = await readChildNames(entry);
  const res = await api.resolveDir(entry.name, kids);
  const list = res.candidates ?? [];
  if (list.length === 1) return list[0].path;
  if (list.length === 0) throw new Error(`没能在本机找到「${entry.name}」`);
  throw new Error(`本机有多个名为「${entry.name}」的文件夹，无法确定是哪一个`);
}

/** 读出拖入文件夹的第一层子项名称（浏览器定位同名文件夹时用来消歧）；readEntries 每次最多回 100 项 */
export async function readChildNames(entry: FileSystemDirectoryEntry): Promise<string[]> {
  const reader = entry.createReader();
  const names: string[] = [];
  for (let i = 0; i < 3; i++) {
    const batch = await new Promise<FileSystemEntry[]>((resolve) => reader.readEntries(resolve, () => resolve([])));
    if (batch.length === 0) break;
    names.push(...batch.map((e) => e.name));
  }
  return names;
}
