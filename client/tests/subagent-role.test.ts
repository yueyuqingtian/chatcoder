/** 子代理角色解析回归：只读/计划模式缺省参数、运行参数结果文本、续跑元信息与别名统一。 */
import assert from "node:assert/strict";
import { extractSubagentRoleArgs, resolveSubagentRole } from "../src/components/chat/subagentRole";

// 1. 只读/计划模式隐藏 mode/explore 入参时，从 TOOL_RESULT 的 [运行参数] mode=readonly 恢复为探索嗒
{
  const roleArgs = extractSubagentRoleArgs(
    { task_title: "调研文件管理与 Git 面板现状", task_description: "只读分析" },
    "Subagent #42 dispatched in the background for task: 调研文件管理与 Git 面板现状. [运行参数] mode=readonly model=#1 effort=default.",
  );
  assert.equal(roleArgs.mode, "readonly");
  assert.equal(
    resolveSubagentRole({ name: "调研文件管理与 Git 面板现状", roleArgs }),
    "explore",
  );
}

// 2. 兼容旧版 Exploration subagent 结果前缀与宽松布尔/大小写入参
{
  const legacyArgs = extractSubagentRoleArgs({}, "Exploration subagent #7 finished");
  assert.equal(resolveSubagentRole({ name: "结构核对", roleArgs: legacyArgs }), "explore");

  const stringExplore = extractSubagentRoleArgs({ explore: "true" });
  assert.equal(resolveSubagentRole({ name: "代码巡检", roleArgs: stringExplore }), "explore");

  const upperMode = extractSubagentRoleArgs({ mode: "ReadOnly" });
  assert.equal(resolveSubagentRole({ name: "状态排查", roleArgs: upperMode }), "explore");

  const subTypeExplore = extractSubagentRoleArgs({ subagent_type: "Explore" });
  assert.equal(resolveSubagentRole({ name: "调研浏览器面板", roleArgs: subTypeExplore }), "explore");
}

// 3. 后端直接下发的 subagentRole / subagentMode 优先（覆盖 resume_subagent 与未落位兜底卡片）
{
  assert.equal(
    resolveSubagentRole({
      name: "调研文件管理与 Git 面板现状",
      subagentMode: "readonly",
      subagentRole: "explore",
      roleArgs: extractSubagentRoleArgs({ agent_id: 42 }),
    }),
    "explore",
  );
  assert.equal(
    resolveSubagentRole({
      name: "点按保存并导出文件",
      subagentMode: "agent",
      subagentRole: "operator",
      roleArgs: extractSubagentRoleArgs({ agent_id: 43 }),
    }),
    "operator",
  );
  assert.equal(
    resolveSubagentRole({
      name: "复核设置保存逻辑",
      workflowKind: "review",
      subagentMode: "readonly",
    }),
    "verifier",
  );
}

// 4. 明确 mode="agent"（且非 operator/verifier）仍显示为编码嗒，即使曾带 legacy explore=true
{
  const coderArgs = extractSubagentRoleArgs({ mode: "agent", explore: true });
  assert.equal(resolveSubagentRole({ name: "修改样式", roleArgs: coderArgs }), "coder");
}

console.log("通过：子代理角色解析（只读/计划模式、结果还原、后端元信息、续跑与别名）");
