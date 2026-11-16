/** plan-20261114-message-flow：分组摘要和运行预览共用同一份按序叶子，避免预览已完成的调用。 */
import { isWriteLeaf, RUN_TOOLS, SEARCH_TOOLS, type ToolLeaf, type TurnItem } from "./timeline";
import { toolLabel } from "./toolLabels";
import { activityTarget } from "./subagentActivity";

const READ_TOOLS = new Set(["fs_read", "read_file_range", "fs_list", "view_image", "read_attachment", "read_session_context", "outline", "skill_view", "compaction_view", "compaction_index"]);

export function processGroupState(items: readonly TurnItem[], indices: readonly number[], active: boolean, results: Readonly<Record<string, unknown>> = {}) {
  const leaves: ToolLeaf[] = [];
  let thinking = 0;
  let shots = 0;
  for (const index of indices) {
    const item = items[index];
    if (item?.kind === "tools") {
      for (const node of item.nodes) leaves.push(...(node.kind === "leaf" ? [node.leaf] : node.leaves));
    } else if (item?.kind === "thinking") thinking++;
    else if (item?.kind === "shot") shots++;
  }
  const counts = { 编辑: 0, 搜索: 0, 读取: 0, 命令: 0, 调用: 0 };
  for (const leaf of leaves) {
    const result = results[leaf.callKey] as { change_stat?: ToolLeaf["changeStat"] } | undefined;
    const writes = isWriteLeaf(leaf) || result?.change_stat != null;
    const key = writes ? "编辑" : SEARCH_TOOLS.has(leaf.tool) ? "搜索" : READ_TOOLS.has(leaf.tool) ? "读取" : RUN_TOOLS.has(leaf.tool) ? "命令" : "调用";
    counts[key]++;
  }
  const browsing = leaves.length > 0 && leaves.every((leaf) => leaf.tool.startsWith("browser_"));
  const verb = counts.编辑 ? "编辑" : browsing ? "浏览" : counts.调用 ? "执行" : leaves.length ? "探索" : "思考";
  const tool = counts.编辑 ? "editor_apply_diff" : browsing ? "browser_navigate" : counts.调用 ? "cluster" : leaves.length ? "codebase_search" : "thinking";
  const parts = Object.entries(counts).filter(([, n]) => n > 0).map(([name, n]) => `${n} 次${name}`);
  if (thinking) parts.unshift(`${thinking} 段思考`);
  if (shots) parts.push(`${shots} 张截图`);
  const pending = active ? leaves.filter((leaf) => leaf.ok === null && !Object.hasOwn(results, leaf.callKey)) : [];
  const previews = pending.map((leaf) => `正在${toolLabel(leaf.tool).verb} ${activityTarget(leaf.args)}`.trim());
  const calls = leaves.map((leaf) => ({ key: leaf.callKey, text: `${toolLabel(leaf.tool).verb} ${activityTarget(leaf.args)}`.trim() }));
  return { verb, tool, summary: parts.join(" · "), previews, calls };
}
