# v23 方案：输入框贴条重构 + 任务进度实时性 + 子代理治理

## 背景与问题确认

### 问题 1：输入框上方任务卡片丑陋且逻辑错误
现状：[TaskStatusPanel.tsx](../client/src/components/chat/TaskStatusPanel.tsx) 是一个普通大卡片，
展示**整个会话**的所有步骤（截图中 13/31，含历史 turn 的遗留步骤），文件变更来自 artifact 消息
且无分组/无回滚，展开无高度限制。

### 问题 2：左右面板更新不及时
- 左侧面板发送消息后不立即转圈：[chat.ts](../client/src/store/chat.ts) `sendTurn` 中
  `has_running: true` 在 `await api.createTurn()` 返回后才设置，REST 往返期间左侧无反馈。
- 任务摘要/新拆分展示不及时：`turn.started` 事件处理只重建子代理卡片，不刷新任务列表；
  新 turn 的 request 任务要等后续 `task.updated` 未知任务兜底才拉取。
- 每步进度不及时：拆分流程中步骤状态由"探索子代理完成"驱动（探索完即置 done），
  并非真实实现进度；主代理串行实现期间步骤状态不更新，直到结束统一扫尾。

### 问题 3：子代理三连
- **预览缺任务消息**：子代理的 handoff 只注入上下文 developer 消息
  （[context_manager.py:462](../server/app/orchestration/context_manager.py)），
  从不落库为 `thread_id=agent.id` 的 Message，右侧子代理面板看不到主 AI 下发的任务。
- **设置开关失效**：设置里停用 explore/general 后，[engine.py](../server/app/orchestration/engine.py)
  `execute_split_then_main` 的"阶段一"仍为每个拆分步骤无条件 spawn `探索·xxx` 子代理，
  完全绕过 SubagentProfile 开关。
- **过度依赖子代理**：根源有二——
  1. 上述自动探索：拆分 N 步就 spawn N 个探索子代理（上限 6，并行批次 3）；
  2. [prompts/main.py](../server/app/orchestration/prompts/main.py) 主提示词鼓励
     "spawn several explore subagents in ONE round"；
  3. [task_planner.py](../server/app/orchestration/task_planner.py) `_contains_explicit_list`
     只要命中 3 个动词关键词（实现/修复/优化…）就跳过 LLM 语义评估直接拆分。

### 参考项目结论（codex 源码实证）
- codex 默认 **EXPLICIT_REQUEST_ONLY**："Do not spawn sub-agents unless the user or applicable
  AGENTS.md/skill instructions explicitly ask for sub-agents, delegation, or parallel agent work."
  （codex-rs/core/src/context/multi_agent_mode_instructions.rs）
- codex spawn 工具描述含 "When to delegate vs. do the subtask yourself" 纪律；
  实验提示词："For simple or straightforward tasks, you don't need to spawn a new agent."
- codex 有 max_concurrent_threads_per_session 并发槽位限制。

## 整改方案

### A. 贴条重构（前端）

重写 TaskStatusPanel 为**紧贴输入框上沿的半弧形贴条**：

1. **形态**：与 composer-main 同宽、无缝贴合（上圆角 12px、下缘与输入框顶边共线、
   margin-bottom:-1px 合并边框），高度 30px  slim 条，背景 bg-elevated。
2. **左侧·任务进度**：仅展示**最新 turn** 的任务步骤（与任务摘要面板同源逻辑：
   visible tasks 中 max(turn_id) 的步骤）。折叠态显示 `任务进度 done/total · 当前步骤名`。
3. **右侧·文件变更**：数据源改为 `turnChanges`（FileChangeOut，含后端持久化 reviewed），
   按 turn 分组（升序，从会话开始到现在），只展示**未审核通过**的文件；
   组头显示该 turn 的用户消息摘要 + 文件数 + +增/-删 + 回滚按钮
   （复用 `requestRollbackPreview(turnId)` → RollbackConfirmModal）。
4. **分别展开**：左右两半各自独立展开；展开体在正常布局流内（把消息流往上顶，
   不遮挡），max-height 240px 内部滚动；只展开一侧时该侧独占全宽。
5. **动画**：grid-template-rows 0fr→1fr 高度过渡 + opacity；贴条入场 fade+translateY；
   chevron 旋转；`prefers-reduced-motion` 降级。
6. 会话加载时 `turnChanges` 补全范围：由"最近 10 个完成 turn"改为
   **∪(含 artifact 消息的 turnId, 最近 10 个完成 turn)**，覆盖"从开始到现在"。

### B. 实时性修复（前端）

1. `sendTurn`：乐观消息入列后、await createTurn **之前**即置
   `isRunning: true` + 该会话 `has_running: true` + 启动心跳；失败时回退。左侧立即转圈。
2. `turn.started` 处理：增加 `refreshTasks()`，新 turn 的 request 任务立刻进入任务摘要/贴条。

### C. 子代理治理（后端，对齐 codex 原则）

1. **移除无条件自动探索**：`execute_split_then_main` 删除"阶段一"每步一个探索子代理的编排；
   拆分后直接由主代理串行执行（步骤卡保留展示）。
2. **步骤进度改由主代理驱动**：拆分路径 instruction 要求主代理先用 todo_write
   按步骤标题逐条建清单、每步完成即更新；[todo.py](../server/app/orchestration/tools/todo.py)
   `_do_sync` 扩展：存在引擎 group 时不再跳过，而是按标题匹配（精确优先、包含兜底）
   把清单状态同步到引擎步骤并广播 task.updated。不新增/不隐藏引擎步骤。
3. **提示词纪律改写**（prompts/main.py）：
   - 默认"自己干"：简单/直接任务禁止 spawn；只有自己几次工具调用拿不下的、
     相互独立且并行能实质提速的调研才允许 spawn explore 子代理；
   - 实现永远在主循环串行完成，不委派；
   - 遵守每 turn 子代理硬上限；少而精优于多而碎。
   禁用时（两类全关）整块剔除的逻辑保留。
4. **spawn 工具描述**（subagent_tools.py）加入同款约束文案。
5. **拆分预筛收敛**（task_planner.py）：`_contains_explicit_list` 删除纯关键词计数触发
   （≥3 个动词即拆分），保留显式列表标记（编号/符号列表）检测；
   无列表标记的口语化多动词请求交给 LLM 语义评估。
6. **子代理任务消息落库**：`agent_loop._run_subagent_tool` spawn 时写入一条
   `thread_id=sub_agent.id, sender_type=user, msg_type=text` 的消息
   （标题+任务描述），广播 message.created；右侧子代理面板首条即主 AI 下发的任务。
   （/plan 确认路径 execute_confirmed_plan 的 per-step spawn 同样补写。）

### D. 影响面与风险

- 贴条纯前端重写，复用现有 store 数据与 rollback/review API，无协议变更。
- 移除自动探索后：拆分步骤进度依赖主代理 todo_write 合规度；已有
  todo_reminder_interval=3 提醒机制兜底；收尾统一修正状态逻辑保留。
- 拆分减少后，口语化多需求消息由 LLM 语义评估决定是否拆分，行为更稳。
- 历史数据兼容：旧会话无任务消息落库的子代理线程，面板照常渲染（仅少首条任务消息）。

## 验证

1. `cd client && npx tsc --noEmit` 前端类型检查。
2. `cd server && python -m compileall app` 后端语法检查。
3. 打包后手动验证（用户环境运行 release 版 exe）：
   - 贴条形态/动画/限高/分组回滚；
   - 发送消息左侧立即转圈；任务摘要随 turn.started 即更新；
   - 设置关闭两类子代理后，任务执行全程无子代理卡片；
   - 子代理预览首条为任务消息；简单问答不 spawn 子代理。
