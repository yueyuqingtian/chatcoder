/** 子代理展示角色判定与参数提取（供 SubagentAvatar / SubagentCard / TurnGroup 共用）。 */

export type SubagentRole = "explore" | "coder" | "operator" | "verifier";

export interface SubagentRoleArgs {
  mode?: string;
  explore?: boolean;
  readonly?: boolean;
  purpose?: string;
  agent_type?: string;
  subagent_type?: string;
}

export const SUBAGENT_ROLE_LABEL: Record<SubagentRole, string> = {
  explore: "探索智能体",
  coder: "执行智能体",
  operator: "执行智能体",
  verifier: "探索智能体",
};

const VALID_ROLES = new Set<SubagentRole>(["explore", "coder", "operator", "verifier"]);

function normStr(value?: string | null): string {
  return typeof value === "string" ? value.trim().toLowerCase() : "";
}

function looseBool(value: unknown): boolean | undefined {
  if (typeof value === "boolean") return value;
  if (typeof value === "string") {
    const v = value.trim().toLowerCase();
    if (v === "true" || v === "1" || v === "yes") return true;
    if (v === "false" || v === "0" || v === "no") return false;
  }
  return undefined;
}

/** 从 TOOL_CALL 参数与 TOOL_RESULT 输出文本中提取角色判定线索。
 *
 *  在只读/计划模式下，`spawn_subagent` 的 schema 会隐藏 mode/explore/purpose 参数，
 *  调用参数里没有模式字段；但工具结果仍带 `[运行参数] mode=readonly`，可据此还原历史记录。 */
export function extractSubagentRoleArgs(
  args: Record<string, unknown>,
  resultOutput?: string,
): SubagentRoleArgs {
  let mode = typeof args.mode === "string" ? args.mode : undefined;
  if (!mode && typeof resultOutput === "string" && resultOutput) {
    const paramMatch = /\[运行参数\]\s*mode=([a-z_-]+)/i.exec(resultOutput);
    if (paramMatch) mode = paramMatch[1];
    else if (/^Exploration subagent #\d+/i.test(resultOutput.trim())) mode = "readonly";
  }
  return {
    mode,
    explore: looseBool(args.explore),
    readonly: looseBool(args.readonly),
    purpose: typeof args.purpose === "string" ? args.purpose : undefined,
    agent_type: typeof args.agent_type === "string" ? args.agent_type : undefined,
    subagent_type: typeof args.subagent_type === "string" ? args.subagent_type : undefined,
  };
}

/** 根据后端元信息、工作流节点类型、调用参数与名称前缀解析子代理角色。 */
export function resolveSubagentRole(opts: {
  name: string;
  workflowKind?: string | null;
  subagentMode?: string | null;
  subagentRole?: string | null;
  roleArgs?: SubagentRoleArgs;
}): SubagentRole {
  const { name, workflowKind, subagentMode, subagentRole, roleArgs } = opts;

  const directRole = normStr(subagentRole) as SubagentRole;
  if (VALID_ROLES.has(directRole)) return directRole;

  if (normStr(roleArgs?.purpose) === "operator") return "operator";

  const wf = normStr(workflowKind);
  if (wf === "check" || wf === "review") return "verifier";
  if (wf === "explore") return "explore";

  const typeHint = normStr(roleArgs?.agent_type) || normStr(roleArgs?.subagent_type);
  const mode = normStr(subagentMode) || normStr(roleArgs?.mode);
  if (mode === "readonly" || mode === "read-only" || mode === "read_only" || mode === "explore") {
    return "explore";
  }
  if (mode === "agent" || mode === "default") {
    if (typeHint === "verification" || typeHint === "verifier") return "verifier";
    return "coder";
  }

  if (typeHint === "verification" || typeHint === "verifier") return "verifier";
  if (
    roleArgs?.explore === true
    || roleArgs?.readonly === true
    || typeHint === "explore"
    || typeHint === "plan"
    || /^探索[·:：\s]/.test(name)
  ) {
    return "explore";
  }
  return "coder";
}
