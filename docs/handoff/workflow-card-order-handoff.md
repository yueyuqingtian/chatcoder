# 交接：工作流卡与节点子代理卡按创建序固化

- 日期：2026-10-13
- 分支：`fix/workflow-card-order`（从 `master` 切出）
- 计划文档：`ai/chatcoder-plan-20261013-wf-order.md`。**仅存在于本机**：`ai/` 被 `.gitignore` 忽略，未入库。本文档 §二、§三已包含根因与改动要点，回家后可不依赖该文件。

## 一、当前状态

- 代码改动已完成并提交到上述分支（见 §三）。
- 已在本机通过：客户端 `typecheck`、`build`，客户端单测 5/5，服务端相关 pytest 43 项。
- **未完成：真实界面的视觉验证**（见 §五）。
- 同一分支还包含另一个会话的界面改动（第二个提交），未经本人单独验证。

## 二、问题与根因

现象：工作流运行期间，主消息流中的工作流卡与节点子代理卡（如 `audit•explore_ops`）位置漂移，节点卡会排到最终汇报之后。

根因（均已在代码中核对）：

1. `TurnGroup` 把工作流卡放到过程折叠之后、最终汇报之前，与它在时间线中的创建位置脱节；最终汇报落库时结构整体切换。
2. 节点子代理不经 `spawn_subagent` 工具调用，消息流里没有可定位的条目，只能走兜底位置（最后一个 AI 内容项之后）。每新增一项，这组卡就被推到新的末尾。
3. `agent.started` 不携带程序归属，前端只能等 REST 回填才知道节点属于哪个工作流。
4. 节点的 `turn_id` 取 `root.last_turn_id`，而它只在创建程序时写入（规划轮）。后续轮次启动或续跑的工作流，节点仍归属规划轮。
5. 合并轮（唤醒并入的续跑轮）只取基础轮的子代理，续跑轮派发的子代理在合并轮中取不到。

## 三、改动

服务端

| 文件 | 改动 |
|---|---|
| `server/app/orchestration/agent_loop.py` | 新增 `_agent_started_extra()`：工作流节点子代理的 `agent.started` 附带 `workflow_program_id`；查询失败只记日志，不阻塞启动 |
| `server/app/orchestration/workflow/runtime.py` | `start` / `resume` 新增可选参数 `turn_id`，随状态更新写入 `last_turn_id`；无轮次入口（自动续跑、界面按钮）保持原值 |
| `server/app/orchestration/workflow/tools.py` | `start_workflow` / `resume_workflow` 传入当前轮 |

客户端

| 文件 | 改动 |
|---|---|
| `client/src/store/chat.ts` | `SubagentMeta.programId`；启动事件写入，REST 回填时取自 `workflow.program_id` |
| `client/src/components/chat/timeline.ts` | 工作流项带 `turnId`；导出 `isOutsideProcess`；新增 `splitFlowSegments`（分段）与 `placeWorkflowNodeCards`（节点卡挂靠） |
| `client/src/components/chat/TurnGroup.tsx` | 执行段改为分段渲染（`renderFlowSegments`）。工作流卡及其节点卡常显、原位留在段间；兜底槽位只承接挂不到的卡 |
| `client/src/components/chat/MessageFlow.tsx` | 子代理映射带 `turnId` / `programId`；合并轮取各轮子代理并集（`subagentsOfRound`） |
| `client/src/components/chat/SubagentCard.tsx` | `SubagentMetaLite` 增加可选的 `turnId` / `programId` 字段 |

测试与文档

| 文件 | 内容 |
|---|---|
| `client/tests/workflow-card-order.test.ts` | 5 个用例：时间线 turnId、分段、挂靠（两例）、漂移回归 |
| `server/tests/test_plan_20261013_wf_order.py` | 6 个用例：启动事件附加程序编号（含普通子代理、无程序、查询失败），start / resume 写入轮次 |
| `CHANGELOG.md` | 0.8.1「修复」组追加一条（面向用户） |
| `docs/handoff/workflow-card-order-handoff.md` | 本文档 |

## 四、验证记录

在本机完成：

- `client`：`npm run typecheck` 通过；`npm run build` 通过（只有既有的 chunk 体积告警）。
- `client/tests/workflow-card-order.test.ts`：esbuild 打包后用 node 运行，5/5 通过（运行方式见文件头注释）。
- 对照：用改动前的渲染顺序复刻同一场景，最终汇报落库时工作流卡与节点卡移到了后续项之后（漂移）；改动后保持稳定。说明该回归用例能区分新旧行为。
- `server`：在 `server/.venv` 中运行 `tests/test_plan_20261013_wf_order.py`、`tests/test_workflow_runtime.py`、`tests/test_workflow_tools_api.py`、`tests/test_plan_330_1648_subagent.py`，共 43 项通过。

注意：`SubagentCard.tsx` 的类型字段补充发生在最后一次 `typecheck` / `build` 之后，只增加了可选字段，但仍需在家里重跑一次。

## 五、未完成 / 未验证

1. **真实界面视觉验证未完成。** `preview_start`（`.claude/launch.json` 的 `client-dev`，端口 5173）失败：端口被另一个会话的 dev server 占用。未修改 `.claude/launch.json`，因为它是共享配置，需先确认。
2. **SSR 结构验证未完成。** 尝试用 `renderToStaticMarkup` 渲染真实 `TurnGroup`，节点子代理卡没有输出：harness 中 `PluginSlot` 取不到已注册的插件，原因尚未确认。因此**不能据此判断渲染顺序是否正确**，以视觉验证为准。
3. 计划步骤子代理（`server/app/orchestration/engine.py` 派发，不经 spawn 工具）仍走兜底位置。非工作流运行中，它们仍可能漂移。
4. 界面按钮启动（REST，无轮次）的工作流，节点仍归属规划轮。
5. 第二个提交（侧栏运行指示、首页徽章浮签、设置页间距、首页居中）来自另一个会话，未经本人验证。若构建或界面出现问题，先排查该提交。
6. `last_turn_id` 的写入会改变节点子代理的 `turn_id`，影响按轮次过滤子代理的接口（`server/app/gateway/routers/turns.py` 中的 `turn_id` 过滤）。

## 六、回家后继续

1. `git fetch origin && git checkout fix/workflow-card-order`
2. 安装依赖：在 `client` 下执行 `npm install`；`server` 按 `server/pyproject.toml` 准备 `.venv`。
3. 在最终状态上重新验证（代码改动之后，尚未完整跑过一遍）：
   - `cd client`，然后 `npm run typecheck`、`npm run build`
   - 客户端单测：`npx esbuild tests/workflow-card-order.test.ts --bundle --format=esm --platform=node --alias:@chatcoder/shared=../packages/shared/src/index.ts --outfile=<临时目录>/wf-order.test.mjs`，再执行 `node <临时目录>/wf-order.test.mjs`
   - `cd server`，然后 `.venv\Scripts\python.exe -m pytest tests/test_plan_20261013_wf_order.py tests/test_workflow_runtime.py tests/test_workflow_tools_api.py tests/test_plan_330_1648_subagent.py -q`
4. 界面验证：启动 dev server，打开一个带工作流运行的会话，确认：
   - 节点子代理卡位于工作流卡之后，按创建顺序排列；
   - 工作流运行中新增消息、节点陆续完成、最终汇报落库后，已显示的卡片位置不变；
   - 普通 `spawn_subagent` 子代理卡仍在其工具调用的位置。

   若 5173 被占用，先关闭占用它的 dev server。
5. 若要继续排查 SSR 结构验证（§五第 2 项），harness 不在仓库内。做法：用 esbuild 的 JS API 打包为 cjs；`define` 设置 `import.meta.env`；`inject` 一个 preamble，提供 `window` / `document` / `localStorage` 等桩，并为 `react` 的 `useSyncExternalStore` 补上 `getServerSnapshot`；入口用 `renderToStaticMarkup` 渲染 `TurnGroup`，数据用 `buildTimeline` 构造。卡点在插件激活，见 §五第 2 项。
