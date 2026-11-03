/** 引用行解析（plan-128-518）。
 *  发送时引用 chips 会被拼成「引用文件：/ 引用文件夹：/ 使用技能：/ 使用连接器：/ 使用插件：」可读行追加在正文后（见 buildRefsSuffix）。
 *  撤回回填、排队消息编辑都需要把这些行还原成结构化 refs，这里抽成唯一实现，避免多处各写一份正则。
 *  plan-633-2604（R3）：新增「引用文件夹」（拖入的文件夹引用）。 */

export interface ParsedRef { kind: "file" | "dir" | "skill" | "mcp" | "plugin"; value: string; label: string }

const KIND_MAP: Record<string, ParsedRef["kind"]> = {
  "引用文件": "file", "引用文件夹": "dir", "使用技能": "skill", "使用连接器": "mcp", "使用插件": "plugin",
};

/** 把正文拆成「纯正文」与「结构化 refs」；没有引用行时 refs 为空、text 仅规整空行 */
export function parseRefLines(rawText: string): { text: string; refs: ParsedRef[] } {
  const refs: ParsedRef[] = [];
  const bodyLines: string[] = [];
  for (const line of (rawText ?? "").split("\n")) {
    const m = line.match(/^(引用文件|引用文件夹|使用技能|使用连接器|使用插件)：(.*)$/);
    if (!m) { bodyLines.push(line); continue; }
    const kind = KIND_MAP[m[1]];
    for (const part of m[2].split("、")) {
      // plan-633-2604（R3）：目录引用行末尾带「（可用 fs_list 查看其中内容）」说明，还原 chip 时剥掉
      const v = part.trim().replace(/（(?:该目录在工作区外，读取需按审批流程|可用 fs_list 查看其中内容)）/g, "").trim();
      if (!v) continue;
      const value = kind === "file" ? v.replace(/^@/, "") : kind === "skill" ? v.replace(/^\$/, "") : v;
      refs.push({ kind, value, label: value });
    }
  }
  return { text: bodyLines.join("\n").replace(/\n{3,}/g, "\n\n").trim(), refs };
}
