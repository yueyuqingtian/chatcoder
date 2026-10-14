# 交接：工作流卡与节点子代理卡按创建序固化

- 初版：2026-10-13；更新：2026-10-14（改为按派发时刻定位，见 §二第 6 点）
- 分支：`fix/workflow-card-order`（从 `master` 切出）
- 计划文档：`ai/chatcoder-plan-20261013-wf-order.md`。**仅存在于本机**：`ai/` 被 `.gitignore` 忽略，未入库。本文档已包含根因与改动要点，回家后可不依赖该文件。

## 一、当前状态

- 代码改动已完成，并在本机通过：客户端 `typecheck`、`build`、工作流卡定位单测 9/9、既有时间线回归 12/12；服务端新增单测 9/9、相关套件 45 项通过。
- 真实会话（`测试工作流是否能正常使用`，会话 416）已核对：节点卡位置与消息时间线一致（见 §四）。但后端仍是旧代码，核对用的派发时刻是近似值（§五第 2 项）。
- **未完成：后端重启后的端到端确认**（§五第 1 项）。
- 同一分支还包含另一个会话的界面改动（第二个提交），未经本人单独验证。

## 二、问题与根因

现象：工作流运行期间，主消息流中的工作流卡与节点子代理卡（如 `audit•explore_ops`）位置漂移，节点卡会排到最终汇报之后。

根因（均已在代码中核对）：

1. `TurnGroup` 把工作流卡放到过程折叠之后、最终汇报之前，与它在时间线中的创建位置脱节；最终汇报落库时结构整体切换。
2. 节点子代理不经 `spawn_subagent` 工具调用，消息流里没有可定位的条目，只能走兜底位置（最后一个 AI 内容项之后）。每新增一项，这组卡就被推到新的末尾。
3. `agent.started` 不携带程序归属，前端只能等 REST 回填才知道节点属于哪个工作流。
4. 节点的 `turn_id` 取 `root.last_turn_id`，而它只在创建程序时写入（规划轮）。后续轮次启动或续跑的工作流，节点仍归属规划轮。
5. 合并轮（唤醒并入的续跑轮）只取基础轮的子代理，续跑轮派发的子代理在合并轮中取不到。
6. （本轮新发现）第一版把节点卡统一挂到工作流卡之后，**不是创建序**：真实会话中节点 `audit•review_report`（11:23:09）与 `integration•verify`（11:24:57）被排在 11:20–11:23 的多条文字之前。节点卡必须按派发时刻定位。

## 三、改动

服务端

| 文件 | 改动 |
|---|---|
| `server/app/orchestration/workflow/executor_subagent.py` | `_spawn` 在打开数据库会话前调用 `write_behind.flush_all()`（屏障）。派发前已入队的主线程消息因此都已落库、id 均小于派发时刻 |
| `server/app/orchestration/agent_loop.py` | `_agent_started_extra()` 附带 `workflow_program_id`（程序编号）与 `spawn_message_id`（派发时刻 = 线程首条消息的最小 id）；查询失败只记日志，不阻塞启动 |
| `server/app/gateway/routers/turns.py` | 子代理列表对工作流节点返回 `spawn_message_id`（分组查询，与实时事件同口径） |
| `server/app/orchestration/workflow/runtime.py` | `start` / `resume` 新增可选参数 `turn_id`，随状态更新写入 `last_turn_id`；无轮次入口（自动续跑、界面按钮）保持原值 |
| `server/app/orchestration/workflow/tools.py` | `start_workflow` / `resume_workflow` 传入当前轮 |

客户端

| 文件 | 改动 |
|---|---|
| `client/src/api/client.ts` | 子代理列表类型增加 `spawn_message_id` |
| `client/src/store/chat.ts` | `SubagentMeta.programId` / `spawnMessageId`；启动事件与 REST 回填写入 |
| `client/src/components/chat/timeline.ts` | 工具簇与工作流卡记录 `firstId`（首条调用的 id）；`firstIdOf`；`placeWorkflowNodeCards` 先按派发时刻锚定到派发前最后一个条目（首条用户消息不作锚点），无派发时刻时回退到同轮同程序工作流卡；`splitFlowSegments` 支持挂靠段（`attached`） |
| `client/src/components/chat/TurnGroup.tsx` | 挂靠的节点卡独立成常显段，紧随其锚点条目；锚点条目的尾部（兜底卡、AI 操作行）改由挂靠段渲染，操作行位于节点卡之后；执行段统一由 `renderFlowSegments({ collapsed, wrap })` 渲染 |
| `client/src/components/chat/MessageFlow.tsx` | 子代理映射带 `turnId` / `programId` / `spawnMessageId`；合并轮取各轮子代理并集（`subagentsOfRound`） |
| `client/src/components/chat/SubagentCard.tsx` | `SubagentMetaLite` 增加可选字段 `turnId` / `programId` / `spawnMessageId` |

测试与文档

| 文件 | 内容 |
|---|---|
| `client/tests/workflow-card-order.test.ts` | 9 个用例：时间线 `turnId` / `firstId`、分段、挂靠段、派发时刻定位（与真实会话同形）、派发时刻回退、两例旧数据回退、两例漂移回归 |
| `server/tests/test_plan_20261013_wf_order.py` | 9 个用例：启动事件的程序编号与派发时刻（含无线程消息、普通子代理、解析失败）、start / resume 写入轮次、REST 列表带派发时刻 |
| `CHANGELOG.md` | 0.8.1「修复」组追加一条（面向用户） |
| `docs/handoff/workflow-card-order-handoff.md` | 本文档 |

## 四、验证记录

在本机完成：

- `client`：`npm run typecheck` 通过；`npm run build` 通过（只有既有的 chunk 体积告警）。
- `client/tests/workflow-card-order.test.ts`：9/9 通过；既有 `client/tests/timeline-performance.test.ts`：12/12 通过。
- `server`：`py_compile` 通过；`tests/test_plan_20261013_wf_order.py` 9/9 通过；`test_workflow_runtime.py`、`test_workflow_tools_api.py`、`test_plan_330_1648_subagent.py` 与之合计 45 项通过。
- SSR 结构验证（真实 `TurnGroup`，`renderToStaticMarkup`，派发时刻为模拟值）：三个状态（已启动、唤醒后、最终汇报落库后）的输出顺序都符合创建序；最终汇报落库后过程段折叠，工作流卡与节点卡位置不变。
- 实时页面（会话 416）：把各节点的派发时刻近似为 `startedAt` 之前最后一条消息的 id + 0.5（秒级精度，仅用于核对）后，DOM 顺序为：节点 `audit·explore_ops`、`audit·write_report` 位于启动工具簇之后、「工作流已启动」文字之前；`audit·review_report`、`integration·verify` 位于「工作流 11 正在正常运行中」文字之后。控制台无错误。

## 五、未完成 / 未验证

1. **后端需要重启。** 端口 12973 上运行的后端仍是旧代码，子代理列表没有 `spawn_message_id`。在重启之前，前端找不到派发时刻，会回退到第一版的程序卡定位（节点卡排在工作流卡之后，可能与创建序不符）。重启的做法见 §六，我没有擅自重启它，因为它可能是桌面应用的后端，影响其他会话。
2. 上面的实时核对使用了**近似派发时刻**，不是真实字段。重启后请用真实数据复核（§六第 4 步）。
3. 计划步骤子代理（`server/app/orchestration/engine.py` 派发，不经 spawn 工具）仍走兜底位置。非工作流运行中，它们仍可能漂移。
4. 界面按钮启动（REST，无轮次）的工作流，节点仍归属规划轮。
5. 第二个提交（侧栏运行指示、首页徽章浮签、设置页间距、首页居中）来自另一个会话，未经本人验证。若构建或界面出现问题，先排查该提交。
6. `last_turn_id` 的写入会改变节点子代理的 `turn_id`，影响按轮次过滤子代理的接口（`server/app/gateway/routers/turns.py` 中的 `turn_id` 过滤）。
7. 派发时刻依赖 `_spawn` 中的 `flush_all()` 屏障：若将来出现绕过屏障的消息写入路径，派发时刻与消息 id 的先后关系会失真。修改 `_spawn` 时请保留该屏障，并放在创建线程首条消息之前。

## 六、回家后继续

1. `git fetch origin && git checkout fix/workflow-card-order`
2. 安装依赖：在 `client` 下执行 `npm install`；`server` 按 `server/pyproject.toml` 准备 `.venv`。
3. 重启后端，让服务端改动生效。然后确认子代理列表返回派发时刻：`GET /api/turns/sessions/<会话 id>/subagents`，工作流节点的条目应带整数 `spawn_message_id`，普通子代理为 `null`。
4. 重新验证（代码改动之后，最终状态的检查需要在家里再跑一遍）：
   - `cd client`，然后 `npm run typecheck`、`npm run build`
   - 客户端单测：`npx esbuild tests/workflow-card-order.test.ts --bundle --format=esm --platform=node --alias:@chatcoder/shared=../packages/shared/src/index.ts --outfile=<临时目录>/wf-order.test.mjs`，再执行 `node <临时目录>/wf-order.test.mjs`；既有的 `tests/timeline-performance.test.ts` 用同样方式运行
   - `cd server`，然后 `.venv\Scripts\python.exe -m pytest tests/test_plan_20261013_wf_order.py tests/test_workflow_runtime.py tests/test_workflow_tools_api.py tests/test_plan_330_1648_subagent.py -q`
5. 界面验证：启动 dev server，打开一个带工作流运行的会话，确认：
   - 每个节点子代理卡位于它被派发时刻之前最后一条消息之后，即与消息时间线的先后一致；
   - 工作流运行中新增消息、节点陆续完成、最终汇报落库后，已显示的卡片位置不变；
   - 普通 `spawn_subagent` 子代理卡仍在其工具调用的位置。

   若 5173 被占用，先关闭占用它的 dev server。
6. 若要继续排查 SSR 结构验证，harness 不在仓库内。做法：用 esbuild 的 JS API 打包为 cjs；`define` 设置 `import.meta.env`；`inject` 一个 preamble，提供 `window` / `document` / `localStorage` 等桩，并为 `react` 的 `useSyncExternalStore` 补上 `getServerSnapshot`；入口先调用 `registerBuiltinPlugins()`，再用 `renderToStaticMarkup` 渲染 `TurnGroup`，数据用 `buildTimeline` 构造。
