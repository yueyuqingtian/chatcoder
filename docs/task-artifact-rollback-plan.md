# chatcoder 任务产物展示 / 任务回滚 / 任务拆解与展示 完善规划

> 版本：v1.0（基于 git HEAD `85e12fa`（v10 任务卡重构）与在途 v11 变更审核）
> 范围：`server/`（FastAPI + SQLAlchemy async）+ `client/`（React 18 + TS + Vite + zustand）+ `packages/shared`
> 说明：所有改动须给出**文件路径、现状、根因、改法、验收标准**，逐条执行；同等能力不重复建。

---

## 0. 现状盘点（已具备，规划的地基）

### 0.1 任务拆解与展示

| 能力 | 位置 |
| --- | --- |
| turn 启动即建主任务记录（步骤载体） | `server/app/orchestration/engine.py:103` `create_task` |
| 子代理任务创建即入库并广播（task.created/task.step，任务卡实时刷新） | `server/app/orchestration/agent_loop.py:691`、`subagent.py`、v10 commit |
| 右上角任务卡（codex 风格，只展示「最新 turn」任务） | `client/src/components/chat/TodoFloat.tsx` |
| 右侧任务摘要面板（最新 turn 步骤 + 产物计数 + 待审查文件 + 浏览文件） | `client/src/components/panel/TaskSummaryPanel.tsx` |
| 拆解计划卡（任务数组消息）**已在 v10 重构中移除**，现无「需求理解/计划」消息流展示 | ——（旧 `orchestrator.py` 已删） |

### 0.2 任务产物展示

| 能力 | 位置 |
| --- | --- |
| Artifact 表 + ArtifactOut schema 已就绪 | `server/app/persistence/models/task.py:28`、`server/app/gateway/schemas.py:134` |
| 产物抽取落库（抽取 final_text 中写盘文件 → Artifact 行 + 挂 task.artifact_ids） | `server/app/orchestration/artifacts.py`、`agent_loop.py:505`、`task_service.attach_artifacts` |
| turn 产物 message（artifact_ids + 写盘 files 清单） | `agent_loop.py:512-523` |
| turn 内产物条（文件清单/展开/diff/审查/回滚入口） | `client/src/components/chat/ArtifactList.tsx` |
| 产物聚合查询服务（**无路由，前端零消费**） | `server/app/services/task_service.py:74` `list_artifacts` |

### 0.3 任务回滚

| 能力 | 位置 |
| --- | --- |
| 精确回滚（仅撤销 AI 写盘部分，冲突自动跳过，绝不全局 git 恢复） | `server/app/services/rollback_service.py:120` `rollback_turn` |
| 回滚预览端点（文件级 before/after + conflict） | `server/app/gateway/routers/turns.py:127` |
| 回滚确认弹窗（LCS 行级 diff、冲突标注、执行 loading） | `client/src/components/chat/RollbackConfirmModal.tsx` |
| 回滚副作用：先 cancel 并等待 agent 退出、任务置 cancelled、子代理线程消息随 turn 软删、Agent 置 terminated、磁盘 checkpoint 双保险 | v10 commit + `rollback_service.py` |
| 前端事件 `turn.rolled_back` → 刷新任务/消息 | `client/src/store/chat.ts:735` |

---

## 1. 差距与关键问题

### A. 产物展示线

- **G-A1**：`list_artifacts` 服务无路由、无 client API、store 无状态 —— `ArtifactOut`（title/summary/type/files）全链路未接线，前端唯一可见的是 `TaskSummaryPanel.tsx:105-107` 的「N 个产物文件」计数。
- **G-A2**：产物与任务步骤归属展示弱 —— 只显示计数，看不到每个产物文件的标题/摘要，文件无法从任务卡直接打开。
- **G-A3**：`ArtifactList.tsx:43-46` `openAll` 仅打开第一个文件，无「全部产物」浏览入口（产物浏览可复用 `setPreviewPath` + files tab）。

### B. 回滚线

- **G-R1**：进度统计把 `cancelled` 计入已完成后端无 `blocked` 状态但前端渲染了 `blocked` 分支（`TodoFloat.tsx:30`、`TaskSummaryPanel.tsx:61`、`TodoFloat.tsx:81`）—— 状态口径与后端 `enums.py:33-39` 不一致，回滚后任务卡显示「全部完成」的假象。
- **G-R2**：回滚后消息被软删（`message_service.py:65-66` 已过滤 deleted），时间线上该 turn 变成空白段，无「已回滚」占位与产物灰置标识，用户无法分辨「回滚了」与「没执行」。
- **G-R3**：预览弹窗不展示连带影响 —— 回滚会一并撤销「该 turn 及其之后」的写盘/任务/消息（`list_turn_writes` 按 `turn_id >=` 查询），但 `RollbackPreviewOut` 只有文件清单，无任务/消息计数提示。
- **G-R4**：回滚不清扫产物关联 —— `Task.artifact_ids`、`Artifact` 行、`FileReview` 审核状态在回滚后残留，产物区仍显示已撤销的文件（可被误点开看「已恢复」的假内容）。

### C. 拆解展示线

- **G-D1**：`TaskOut` 携带 `description / acceptance_criteria / priority / note`，但任务卡/面板只渲染 title；失败原因 note 也只在面板可见。
- **G-D2**：面板只展示「最新 turn」任务，无法切换查看历史 turn 任务（tasks 全量已拉取，只差切换 UI）。
- **G-D3**：拆解信息无消息流呈现 —— 旧 task_card「需求理解」在上版本重构中消失；现在拆解 = 主任务记录 + 子代理任务记录，「为什么拆、怎么拆」不可见。
- **G-D4**：拆解步骤与子代理任务无 `parent_task_id` 强关联（子任务 `parent_task_id` 均为空），任务卡步骤序号与执行子代理对应关系无法从数据层推断。

---

## 2. 分阶段工作项

### P0 结清在途 v11（前置，1 次提交）

现状：工作区已有未提交的 v11 变更审核（`ReviewCard.tsx`、`server/app/persistence/models/review.py`、`turns.py` 的 changes/diff/reviews 端点、`FileChangeOut` 等）与若干样式改动。

- [ ] 跑第 4 节验证命令，确认前后端编译与后端导入全绿
- [ ] 提交 v11（变更审核），锁定基线，后续工作基于该提交推进

### P1 任务产物展示打通（后端 1 端点 + 前端 3 处）

**P1.1 产物聚合端点**
- 改 `server/app/gateway/routers/turns.py`：新增 `GET /turns/sessions/{session_id}/artifacts`，返回 `list[ArtifactOut]`，复用 `task_service.list_artifacts`（`task_service.py:74`），按 `created_at desc`。
- 改 `client/src/api/client.ts`：`listSessionArtifacts(sessionId)`（挂到「会话数据查询」分组，与 tasks/snapshots/audit 并列，`client.ts:263-265`）。
- 改 `client/src/store/chat.ts`：`artifacts` 状态 + `refreshArtifacts()`（在 `refreshTasks` 后同批拉取/`agent.completed` 后刷新）。
- `packages/shared` 已验证 `ArtifactOut` 与后端一致，无需改动。
- **验收**：调接口返回全部 Artifact 行（含 title/summary/files/type）；新会话发需求后前端 `artifacts` 状态有数据。

**P1.2 右侧面板产物区按任务分组展示**
- 改 `client/src/components/panel/TaskSummaryPanel.tsx`：产物区从「N 个产物文件」升级为按 `artifact.task_id` 分组挂在任务步骤下；每个产物展示 title + summary（截断）+ 文件数；点击文件调用 `openFile`（`setPreviewPath` + files tab，复用现有 `TaskSummaryPanel.tsx:67-70`）；无归属任务（task_id 为 null）的产物进「其他产物」分组。
- 样式沿用现有 token（`ts-*` 类 + 内联 `<style>`），文案简体中文。
- **验收**：完成一个含多文件的 turn，面板每个任务步骤下能看到产物标题/摘要，点击文件名右侧 files 面板打开对应文件。

**P1.3 消息流产物条增强 + 全部产物浏览**
- 改 `client/src/components/chat/ArtifactList.tsx`：
  1. 用 store `artifacts` 按消息 `content.artifact_ids` 反查 title/summary，在文件行上方渲染产物标题（查不到时降级现状，仅 files）；
  2. `openAll` 改为「查看」按钮打开文件列表（`expanded=true` 同时打开面板 files tab），不再只打开第一个文件。
- **验收**：产物条展示产物标题；点击「查看」打开完整文件清单而非单个文件。

### P2 任务回滚完善（后端 2 处 + 前端 4 处）

**P2.1 状态口径统一（G-R1）**
- 改 `client/src/components/chat/TodoFloat.tsx:29-33` 与 `TaskSummaryPanel.tsx:59-65`：`done` 单独计数，`cancelled` 独立为「已取消」展示（`X/Y 完成 · Z 已取消`），不再并入完成。
- 后端 `enums.py:33-39` 维持现状（cancelled 为合法状态），前端删除 `blocked` 死分支渲染（`TodoFloat.tsx:81`），`running` 分支的 `in_progress→running` 映射保留。
- **验收**：回滚一次后任务卡显示「已取消 Z」而非全完成；无任何状态渲染为 blocked。

**P2.2 回滚连带影响提示（G-R3）**
- 改 `server/app/gateway/schemas.py` `RollbackPreviewOut`：新增 `affected: { tasks: int; messages: int }`（tasks = 该 turn 之后将置 cancelled 的任务数，复用 `cancel_turn_tasks`/`list_turn_writes` 的 `turn_id >=` 口径统计；messages = 将被软删的消息数）。
- 改 `server/app/gateway/routers/turns.py:127` `rollback_preview` 组装 affected。
- 改 `client/src/store/chat.ts`（`rollbackPending` 携带 affected）与 `RollbackConfirmModal.tsx`：弹窗描述补充「将连带撤销 N 条消息、M 个任务步骤」。
- **验收**：连续两个 turn 后回滚 turn1，弹窗显示连带影响数量与 files 一致；回滚后任务/消息确实同步消失。

**P2.3 回滚清扫产物与审核（G-R4）**
- 改 `server/app/services/rollback_service.py`（`rollback_turn` 内、任务置 cancelled 之后）：
  1. 该 turn 及其之后任务的 `artifact_ids` 置空；
  2. 对应 `Artifact` 行置 `task_id = NULL`（保留行，前端按任务过滤不再显示）；
  3. 该 turn 的 `FileReview` 记录删除（该 turn 的 review 状态作废）。
- 数据库只做幂等补列/不动表结构，此改动纯 ORM 操作。
- **验收**：回滚后 `GET .../tasks` 的 artifact_ids 为空、artifacts 聚合结果不再包含已撤销文件。

**P2.4 时间线「已回滚」标识（G-R2）**
- 改 `client/src/components/chat/TurnGroup.tsx`：新增 prop `rolledBack`（由上层按 turn 状态传入，`timeline.ts`/`MessageFlow.tsx` 透传）；为 true 时在该 turn 顶部渲染「已回滚」pill（复用 `rc-*` 或 `ts-*` 样式 token），不渲染回滚操作行（`MessageActions` 已随 onRollback 隐藏）。
- 改 `client/src/components/chat/ArtifactList.tsx`：`rolledBack` 为 true 时整体灰置并显示「该 turn 产物已随回滚撤销」，回滚按钮隐藏。
- **验收**：已回滚 turn 显示灰色「已回滚」标识，无回滚入口，产物条不可再操作。

### P3 拆解与展示增强（纯前端为主）

**P3.1 任务步骤详情展开（G-D1）**
- 改 `TodoFloat.tsx` 与 `TaskSummaryPanel.tsx`：任务行可点击展开 `description / acceptance_criteria / note`（note 置顶展示失败原因）；`blocked`/`cancelled` 语义由 P2.1 统一。
- **验收**：点击任务行展开完整步骤说明；失败任务直接可见原因（note）。

**P3.2 历史 turn 任务切换（G-D2）**
- 改 `TaskSummaryPanel.tsx`：顶部增加 turn 下拉（含最新 turn + 前 N 个产生过任务的 turn），切换后展示该 turn 的任务与产物；`TodoFloat` 维持只展示最新 turn 的定位不变。
- **验收**：多 turn 会话可下拉查看任一历史 turn 的步骤与产物。

**P3.3 拆解步骤与子代理任务强关联（G-D4）**
- 改 `server/app/orchestration/agent_loop.py:691` 建子任务处：主 turn 下无 plan 步骤记录时，按任务创建顺序补建步骤任务（title/description 取自子代理任务）并给子任务 `parent_task_id` 挂到步骤任务，使「任务卡步骤 ↔ 执行子代理」可从数据层对齐（改造前先评估对现有轮询/状态机的破坏面，向后兼容：缺步骤记录时按现状降级）。
- **验收**：一个需求拆出 N 个步骤时，`GET .../tasks` 返回 N 条步骤任务 + 对应子任务（parent_task_id 非空），任务卡序号与子代理 agent 一一对应。

### P4 远期候选（不进本期排期）

- 回滚撤销（undo）：回滚前对每次覆盖面做 checkpoint，支持恢复已撤销内容（需新增 rollback 历史表，破坏面大，先不排）。
- 任务级回滚/单任务重跑：`POST /tasks/{id}/rerun`（复用 `turns/{id}/resume` 思路），失败任务一键重跑。
- 拆解计划卡回归：恢复「需求理解」消息流卡（understanding + 任务清单），增强拆解可读性。

---

## 3. 执行红线（沿用 rectification-plan-v2 约定）

1. 不引入任何新 npm / pip 依赖。
2. 前端样式模式不变：`global.css`/`tokens.css` token + 组件内联 `<style>`。
3. 数据库变更只走 `server/app/persistence/migrations.py` 幂等补列机制，禁止删列、禁改已有列；P2.3 为纯 ORM 操作。
4. 已有 API 路径不删不改语义，仅允许新增字段/新增端点；`RollbackPreviewOut` 只加字段。
5. 每阶段完成跑第 4 节验证；删除文件前全文搜索引用。
6. 文案简体中文；协议枚举值（`cancelled`/`in_progress` 等）保持英文，仅显示层映射。

## 4. 验证命令

```powershell
# 前端（client/）
npx tsc -b --noEmit
npm run build

# 后端（server/）
python -m pytest tests -q
python -c "from app.main import create_app; create_app()"
```

手动冒烟：
1. 新需求 → 任务卡/面板实时出现步骤与状态流转；
2. 完成含多文件 turn → 面板产物区显示产物标题/摘要，文件可打开；
3. 连续两个 turn 后回滚 turn1 → 弹窗提示连带数量，确认后任务卡「已取消」、时间线显示「已回滚」、产物灰置、任务列表 artifact_ids 清空。

## 5. 里程碑

| 阶段 | 交付 | 依赖 |
| --- | --- | --- |
| P0 | v11 变更审核提交 | 无 |
| P1 | 产物展示全链路（端点 + 面板 + 消息条） | P0 |
| P2 | 回滚口径/连带提示/清扫/时间线标识 | P1（复用 artifacts 状态做灰置判定） |
| P3 | 步骤详情、历史 turn 切换、拆解强关联 | P2 |
| P4 | 远期候选 | 不排期 |