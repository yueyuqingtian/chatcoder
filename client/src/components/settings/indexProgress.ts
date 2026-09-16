/** 索引库进度展示的纯逻辑（与组件分离，便于 Node 原生测试直接校验）。
 *
 * 背景（用户反馈）：实际正在索引时，页面徽标显示「解析中」，但下方仍是旧的
 * 「8000 文件 · 35337 符号」，看不到进度与已扫描文件数。
 *
 * 根因：前端此前仅在 status === "indexing" 时渲染进度，而 worker 实际写入的是
 * queued/scanning/parsing，永远匹配不上，于是走了 else 分支显示陈旧数据。
 */

/** 建议展示进度的状态：worker 实际写 scanning/parsing，queued 为排队，
 * indexing 为 manager 开启时写入的过渡态。 */
const BUSY_STATUSES = new Set(["queued", "scanning", "parsing", "indexing"]);

export const isIndexBusy = (status: string): boolean => BUSY_STATUSES.has(status);

export interface IndexProgressStat {
  status: string;
  progress: number;
  files: number;
  symbols: number;
  files_scanned?: number;
  files_total?: number;
}

/** 进行中状态的计数文案。
 * 两个阶段计数语义不同：扫描期总量未知（只报已发现数），
 * 解析期才有总数（此时计数从 0 重新开始，报告为「已解析 x / 共 y」）。 */
export function progressText(w: IndexProgressStat): string {
  const scanned = w.files_scanned ?? 0;
  if (w.status === "queued") return "等待启动索引进程…";
  if (w.files_total) return `已解析 ${scanned} / 共 ${w.files_total} 个文件`;
  return `已发现 ${scanned} 个文件…`;
}

/** 工作区路径等价比较：容忍分隔符方向、末尾斜杠与大小写差异。
 * 后端 workspace 经 Path.resolve() 规范化，前端项目 path 是原始字符串，
 * 直接 === 比较会漏匹配，导致"检索目标"选不中当前项目。 */
export function sameWorkspace(a?: string | null, b?: string | null): boolean {
  if (!a || !b) return false;
  const norm = (s: string) => s.replace(/\//g, "\\").replace(/\\+$/, "").toLowerCase();
  return norm(a) === norm(b);
}

/** 解析检索下拉框应选中的工作区。
 *
 * 关键约束：下拉框**只列出 enabled 项**，故选中值必须落在 enabled 集合内。
 * 否则 target 指向未启用项时，<select> 找不到匹配 option 会回落到显示第一项，
 * 造成"界面显示 A、实际检索 B"（用户反馈：选了 yipinCode 却检索到别的项目）。
 */
export function resolveSearchTarget(
  current: string | null,
  workspaces: Array<{ workspace: string; enabled: boolean }>,
  currentProjectPath?: string | null,
): string | null {
  const enabled = workspaces.filter((w) => w.enabled);
  if (enabled.length === 0) return null;
  // 用户已选且仍有效：保持不变
  if (current && enabled.some((w) => w.workspace === current)) return current;
  // 优先落到当前项目（若其索引已启用），否则第一个启用的工作区
  const preferred = enabled.find((w) => sameWorkspace(w.workspace, currentProjectPath));
  return preferred?.workspace ?? enabled[0].workspace;
}
