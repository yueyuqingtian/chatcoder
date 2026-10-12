/** 引用行解析（plan-128-518）。
 *  发送时引用 chips 会被拼成「引用文件：/ 使用技能：/ 使用连接器：/ 使用插件：」可读行追加在正文后（见 buildRefsSuffix）。
 *  撤回回填、排队消息编辑都需要把这些行还原成结构化 refs，这里抽成唯一实现，避免多处各写一份正则。 */

export interface ParsedRef { kind: "file" | "skill" | "mcp" | "plugin"; value: string; label: string }

const KIND_MAP: Record<string, ParsedRef["kind"]> = {
  "引用文件": "file", "使用技能": "skill", "使用连接器": "mcp", "使用插件": "plugin",
};

/** 把正文拆成「纯正文」与「结构化 refs」；没有引用行时 refs 为空、text 仅规整空行 */
export function parseRefLines(rawText: string): { text: string; refs: ParsedRef[] } {
  const refs: ParsedRef[] = [];
  const bodyLines: string[] = [];
  for (const line of (rawText ?? "").split("\n")) {
    const m = line.match(/^(引用文件|使用技能|使用连接器|使用插件)：(.*)$/);
    if (!m) { bodyLines.push(line); continue; }
    const kind = KIND_MAP[m[1]];
    for (const part of m[2].split("、")) {
      const v = part.trim();
      if (!v) continue;
      const value = kind === "file" ? v.replace(/^@/, "") : kind === "skill" ? v.replace(/^\$/, "") : v;
      refs.push({ kind, value, label: value });
    }
  }
  return { text: bodyLines.join("\n").replace(/\n{3,}/g, "\n\n").trim(), refs };
}
