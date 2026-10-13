# 工作流运行时（Workflow Runtime）交接文档

> 分支 `feat/workflow-runtime`，远端 `origin`。本次提交包含全部本地改动，本文件与代码一起推送，路径 `docs/handoff/workflow-runtime-handoff.md`。
> 本机的 `ai/` 目录（实施计划与留痕）与 Claude 计划文件只在本机，按 AGENTS.md 约定不入库；验收矩阵已复制到第 7 节。

## 1. 一句话状态

功能主体已实现；服务端全量测试 **1318 项通过**，客户端类型检查与构建通过。浏览器里只核对了“草稿 → 闸门 → 检查失败”的主路径，其余界面尚未走查。**这是阶段性快照，不是可发布版本。**

## 2. 当前问题

1. **界面走查未完成**：调整、差异弹层、模型不可用提示、新建模板、设置页概览与模板编辑器、补充说明、尝试分隔线、浅色 / 深色、窄屏，详见第 5.1 节。
2. **设计与实现有偏差或缺口**：
   - 冲突事件：设计中的 `workflow.conflict.detected` / `workflow.conflict.resolved` 在代码中未找到。冲突相关逻辑集中在守卫模块 `server/app/orchestration/workflow/guard.py`（范围检查、读账本、自动三方合并 `_try_merge`、冲突处理 `_conflict`、读集失效通知、shell 越界检测），但与设计的 L1–L4 分级、冲突分区、三栏决定卡的对应关系未逐项核对。
   - `request_write_scope`：守卫模块有 `_request_scope` 实现，但 `tools.py` 的工具定义列表中没有它的 schema，是否对模型开放待核实。
   - 归档暂停类型为 `archived`（设计写 `restart`）；列表筛选为“进行中 / 已暂停 / 全部”（设计写“已结束”）。
3. **CHANGELOG 未更新**：0.8.0 区块尚未补入本轮条目。
4. **界面文案**：只有简体中文，设计要求的英文键未补（见第 6 节）。
5. **测试警告**：78 条。可见的多为 Windows 下 pytest 清理临时目录时的 `PytestWarning (rm_rf)` / `WinError 145`，属于清理噪声，不影响结果；其余警告未逐条核对。
6. **测试不可并行**：所有测试共用同一个临时 SQLite 文件 `%TEMP%/chatcoder_pytest.db`，同时跑多个 pytest 会出现假失败。
7. **开发端口陷阱**：开发模式下前端默认直连 `127.0.0.1:12973`（桌面版后端端口）。必须设置 `VITE_API_PORT` 指向开发后端，见第 3 节。
8. **进程显示**：Windows 上 `server\.venv\Scripts\python.exe` 会再拉起一个基础解释器子进程，一次 pytest 在任务管理器里显示为两个 python 进程，属正常现象。

## 3. 在新电脑上继续

1. 获取代码：`git fetch origin`，然后 `git checkout feat/workflow-runtime`。
2. 前端（仓库根目录）：

```powershell
npm --prefix client install
npm --prefix client run typecheck
npm --prefix client run build
```

3. 后端（`server` 目录）。项目要求 Python ≥ 3.10（见 `server/pyproject.toml`），本机使用 3.12：

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
```

4. 后端测试（`server` 目录，**一次只跑一个**）：

```powershell
python -m pytest tests -q
```

上次结果：1318 通过，78 条警告，约 252 秒。

5. 本地开发（与 README 一致，端口见第 2 节第 7 条）：

```powershell
# 终端 1：后端（server 目录）
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

```powershell
# 终端 2：前端（仓库根目录）
$env:VITE_API_PORT = "8000"
npm --prefix client run dev
```

前端开发地址 http://localhost:5173 。不要占用 12973 端口，它是桌面版后端的默认端口。

## 4. 已完成内容

### 4.1 服务端（`server/app/orchestration/workflow/`，接口在 `server/app/gateway/routers/workflows.py`）

- **运行时**：程序 → 子工作流 → 步骤；状态机；返工（评估未通过则回到实现，次数有上限）；闸门节点（等待用户“继续 / 中止”）；检查节点（不调用模型，只运行白名单命令：`npm run typecheck|build|test|lint`、`npx tsc --noEmit`、`npm test`、`pytest`、`python -m pytest`、`ruff check`；白名单外的命令不执行，直接记为未通过）。
- **续跑与重试**：从断点继续 / 从头重做（失败步骤）；续跑前做模型可达性预检（12 秒超时）；不可达时保持暂停、不消耗重试次数；自动续跑不可达时保持暂停。
- **工作区漂移**：暂停时记录写入范围内小文件（单个 ≤ 32 KB，合计 ≤ 512 KB）的内容与 Git HEAD；续跑前列出改动文件；HEAD 变化只作提示，不阻止续跑；提供单文件差异接口。
- **草稿编辑**：换模板（替换子工作流的步骤，经与 `plan_workflow` 相同的校验，不通过则不落库，不产生修订记录）；调整（发送给主代理后取消旧草稿，标记“已调整”）；新建模板（从一个探索步骤起步）；从预设开始。
- **节点补充说明**：运行中的步骤经子代理指令通道送达，下一步生效；未在运行则拒绝。
- **尝试记录**：`…/nodes/{flow}/{node}/attempts` 接口；续跑与返工时在子代理线程中插入“第 N 次尝试”的系统消息。
- **会话生命周期**：归档 → 运行暂停（`archived`，界面显示“会话已归档”）；永久删除 → 未结束的运行取消。
- **其它**：步骤按计划顺序展示；每个运行的事件保留最近 1000 条；提示词补充三条续跑规则（无关新任务不自动续跑并询问；多个暂停时列出或“全部”；“算了”不续跑）；共享事件类型 `ServerWsEvent` 去重。
- **工具定义**（`tools.py`）：`plan_workflow`、`start_workflow`、`workflow_status`、`resume_workflow`、`cancel_workflow`、`amend_workflow`。
- **并发、隔离与写入保护**：并发池（`scheduler.py`）、写入租约与热点文件（`conflicts.py`）、守卫（`guard.py`）、工作树隔离（`isolation.py`）；对应测试文件与本次提交一并推送。

### 4.2 客户端（`client/src/components/workflow/`、`client/src/store/workflow.ts`）

- **聊天卡**：草稿计划正文（子工作流、角色链、写入范围、依赖、预计并发、换模板、开始执行 / 调整 / 取消）；运行中默认折叠为子工作流轨道，展开看步骤与评估问题；返工虚线弧；网络重试倒计时；模型暂不可用的就地提示；闸门卡。
- **弹层**：漂移弹层（查看差异、仍然继续 / 取消）；调整弹层；开始前确认弹层。
- **输入框上方胶囊**：状态文案、冲突徽标、点击钉住浮层（Esc / 点外部关闭）。
- **右侧“工作流”标签**：列表（进度环、子工作流数、耗时）；空态两个入口（让嗒嗒规划 / 从预设开始）；详情（目标、开始时间、预算、并发）；时间线（50 条，可加载更多）；步骤的尝试记录。
- **子代理面板**：节点头（计划、验证、评估问题、尝试记录、补充说明输入框）；总结面板中工作流子代理标注“工作流 · 节点名”。
- **设置 → 工作流**：概览四项；网络重试只读行（跳转子智能体设置）；模板列表与新建；模板编辑器（执行角色、输入来源、迷你流程图、字段内联错误）。

### 4.3 已在浏览器中核对的主路径（沙箱 e2e）

- 草稿计划卡渲染；换模板（合法替换）后卡片更新；胶囊钉住浮层；闸门等待决定 → 点“继续” → 检查节点失败 → 失败卡片显示原因“检查未通过：pytest -q”和“从断点继续 / 从头重做该步骤 / 放弃”；右侧面板的步骤与尝试记录。
- 沙箱仓库没有测试用例，所以检查节点失败是预期结果。

### 4.4 验证结果

- 服务端：`python -m pytest tests -q` → **1318 passed, 78 warnings**，退出码 0，约 252 秒。
- 客户端：`npm --prefix client run typecheck` 退出码 0；`npm --prefix client run build` 退出码 0（分包体积警告是既有的）。
- 端到端 harness：已从新位置（`docs/handoff/e2e/`）启动，并确认能播种项目与两个会话。

## 5. 未完成内容

### 5.1 浏览器尚未走查（代码已实现）

- 调整：发送给主代理 → 旧草稿标记“已调整”的完整路径（沙箱没有模型，主代理回合会失败，但发送与取消的界面需要走一遍）。
- 漂移弹层：“查看差异”，以及“仍然继续 / 取消”。
- 模型不可用时的就地提示。
- 新建模板；设置页概览与网络重试行；模板编辑器的执行角色、输入来源与字段错误；换模板的非法替换错误路径。
- 节点补充说明（运行中的子代理）；子代理线程内的尝试分隔线。
- 浅色 / 深色与窄面板（360px）逐界面截图（矩阵 F27）。

### 5.2 设计项未实现或与设计不同

- 冲突事件与界面：事件名 `workflow.conflict.*` 未找到；L2 重做、L3 合并节点、L4 三栏决定卡的界面未核对。
- `request_write_scope` 是否对模型开放，待核实（见第 2 节）。
- 英文键未补（矩阵 F26）。
- 归档暂停类型与列表筛选文案与设计不同（见第 2 节）。
- 其它与设计的偏差，以 `ai/chatcoder-plan-workflow-runtime.md` 第 3 节“剩余偏差（相对设计）”为准（本机文件）。

### 5.3 文档与发布

- `CHANGELOG.md` 的 0.8.0 区块未补入本轮条目。
- 未发版、未合并。版本号只在正式发布时调整（AGENTS.md §3）。

## 6. 需要你决定

1. **界面英文**：AGENTS.md §4 规定 UI 文案为简体中文；设计 §7.11 要求英文键。当前按 AGENTS 执行。
2. **`ai/` 与设计草案**：按 AGENTS 约定不入库。如需在新电脑上保留，请自行拷贝。
3. **冲突处理范围**：是否按设计补齐 L2–L4 的界面与事件名，还是以现有守卫实现为准。

## 7. 验收矩阵（摘要）

图例：✅ 已实现并验证 · ⚠️ 部分实现 · ❌ 未实现 · 🔍 待核实。

#### 7.1 服务端

| 编号 | 设计依据 | 内容 | 状态 |
|---|---|---|---|
| B1 | §6.3 第 2 步 | 续跑前模型可达性预检：不可达返回 `model_unavailable`，保持暂停，不消耗重试次数 | ✅ |
| B2 | §5.9 | 续跑结果码（resumed / already_running / needs_drift_confirm / model_unavailable / not_resumable）以结构化 409 返回 | ✅ |
| B3 | §6.6、§7.7 | 漂移：HEAD 变化单独提示；“查看差异”接口 | ✅ 服务端与界面（F7） |
| B4 | §7.7 | 草稿中替换子工作流模板（A → B），仅 draft，重新校验 | ✅ 服务端与界面（F1） |
| B5 | §7.7 | 调整：发送给主代理后取消旧草稿，标记“已调整” | ✅ 服务端与界面（F2） |
| B6 | §7.8 | 模板页“新建模板” | ✅ 服务端与界面（F23） |
| B7 | §7.6 | 节点详情的尝试历史（时间、模式、结果、原因） | ✅ 服务端（`GET …/attempts`）与界面（F17） |
| B8 | §5.5 | 续跑 / 返工在同一线程内以分隔线标注“第 N 次尝试” | ✅ 续跑 / 返工的分隔线（系统消息） |
| B9 | §5.3 | 会话归档 → 运行暂停（restart）；会话删除 → 运行取消 | ✅ 服务端（归档 → 暂停 archived；永久删除 → 取消） |
| B10 | §4.5 第 8 点 | 计划预览的成本估算（LLM 节点数与预计调用次数） | ✅ 计划预览含“预计代理调用约 N 次” |
| B11 | §6.7、§6.9 | 提示词：换需求时提示暂停运行；多个暂停运行时列出 / “全部” | ✅ 提示词三条规则 + 测试 |
| B12 | §5.2 | 每个运行保留最近 1000 条事件 | ✅ 每个运行保留最近 1000 条（测试以 5 条验证） |
| B13 | §7.2、§6.9 | 网络重试：显示 第 N/M 次 与倒计时（绝对时间本地计算） | ✅ 服务端 reason = 网络重试 n/m；界面倒计时（F6） |
| B14 | 设计 Phase 0 | `packages/shared` 两份 `ServerWsEvent` 的权威来源 | ✅ `index.ts` 重复声明已移除，`events.ts` 为唯一定义 |
| B15 | §4.5 §2 | `check` / `gate` 节点类型的执行语义 | ✅ `check` 只运行命令，`gate` 等待决定，两者不调用模型 |
| B16 | §4.5 §4.4 | 计划模式下的权限天花板 | ✅ 只读计划模式拒绝 execute / merge（graph 校验） |

#### 7.2 客户端：聊天卡、胶囊、侧栏、宠物

| 编号 | 设计依据 | 内容 | 状态 |
|---|---|---|---|
| F1 | §7.7 | 草稿计划卡（WorkflowPlanCard）：子工作流列表（名称、角色链、写入范围摘要）、预计并发、说明文案、按钮 开始执行 / 调整 / 取消 | ✅ 草稿计划正文（角色链、写入范围、并发、换模板） |
| F2 | §7.7 | “调整”对话框 → 发送给主代理 → 旧草稿“已调整” | ✅ 调整对话框 → 发送 → 旧草稿“已调整” |
| F3 | §7.3 | 卡片头部：已完成 X/Y 个子工作流；折叠态只显示子工作流级轨道，展开才列步骤 | ✅ 卡片头部 X/Y 个子工作流；折叠态子工作流轨道 |
| F4 | §7.1 第 4 点 | “评估未通过 · 返工 N/M · 子工作流 · 步骤”，展开看问题（文件:行、问题、修复建议） | ✅ 评估未通过状态 + 问题清单（展开） |
| F5 | §7.3 | 返工：评估与实现之间的虚线弧 + 角标 | ✅ 返工虚线弧（按实际排版测量）+ n/m 角标 |
| F6 | §7.2 | 网络重试状态文案：“网络重试 2/3 · 20 秒后”（固定槽位） | ✅ 网络重试 n/m 与固定槽位倒计时 |
| F7 | §6.9、§7.7 | 续跑漂移弹层：列出文件、查看差异、仍然继续 / 取消；文案按 §6.9 | ✅ 漂移弹层：HEAD 变化、逐个文件“查看差异”、仍然继续 / 取消 |
| F8 | §6.9 | 续跑时模型不可达：显示“模型暂时不可用，已保留进度” | ✅ 模型暂不可用：就地提示，暂停保留 |
| F9 | §7.4、§5A.3 | 胶囊：点击钉住浮层（子工作流状态与按钮）；有待决定冲突时显示“冲突 N” | ✅ 胶囊：状态文案、冲突徽标、钉住浮层（Esc / 外部点击关闭） |
| F10 | §7.3 | 聊天卡：每个子工作流的节点展开（展开态的步骤与节点列表） | ✅ 展开态：子工作流步骤与评估问题 |
| F11 | §7.1 | “找历史”筛选：进行中 / 已暂停 / 已结束（与 §7.5 的全部并列） | ⚠️ 列表筛选为 进行中 / 已暂停 / 全部（§7.5）；§7.1 的“已结束”文案未采用 |
| F12 | §7.2 | 节点与运行状态映射（图标、文案、固定槽位） | ✅ 状态映射（图标、文案、固定槽位） |

#### 7.3 客户端：右侧“工作流”标签、子代理面板、设置、模板编辑器

| 编号 | 设计依据 | 内容 | 状态 |
|---|---|---|---|
| F13 | §7.5 列表页 | 进度环、状态徽标、子工作流数量、耗时；空态文案与入口（让嗒嗒规划 / 从预设开始） | ✅ 列表：进度环、子工作流数、耗时；空态两个入口（让嗒嗒规划 / 从预设开始） |
| F14 | §7.5 详情页 | 头部：目标（可折叠）、状态、开始时间、预算细条、池占用“并发 2/4 · 排队 1” | ✅ 详情：目标（可折叠）、开始时间、预算细条、池占用 |
| F15 | §7.5 详情页 | 底部事件时间线：倒序、最近 50 条、加载更多 | ✅ 时间线：最近 50 条，加载更多 |
| F16 | §7.5 | 总结面板“子代理”分区：按工作流分组，标注“工作流 · 节点名”，运行中置顶 | ✅ 总结面板子代理：工作流 · 节点名，运行中置顶，按程序分组 |
| F17 | §7.6 | 节点头：节点名、角色、尝试次数（第 N 次及原因）、本次模式、输出摘要（explorer 计划 / implementer 验证 / reviewer 结论与问题） | ✅ 节点头：计划 / 验证 / 评估问题、尝试记录（展开） |
| F18 | §7.6 | 节点运行中补充说明（复用子代理的指令注入通道） | ✅ 运行中补充说明：经子代理指令通道，下一步生效 |
| F19 | §7.7 | 闸门卡：原因一句、问题前 3 条可展开、三个决定按钮、可选说明 | ✅ 闸门：问题前 3 条可展开、剩余返工次数、说明输入 |
| F20 | §7.8 概览 | 四个统计：运行中工作流 · 占用并发 · 排队节点 · 已暂停 | ✅ 概览四项：运行中工作流 · 占用并发 · 排队节点 · 已暂停工作流 |
| F21 | §7.8 | 资源与并发：StepSlider 档位（1/2/3/4/6/8/12/16/不限）；各项即时保存 | ✅ 档位与设计一致（全局 1–16 / 不限；单会话 1–8 / 不限） |
| F22 | §7.8 执行与恢复 | 网络瞬时故障重试：只读行 + “前往子智能体设置” | ✅ 网络瞬时故障的重试：只读说明 + 前往子智能体设置 |
| F23 | §7.8 模板 | 页头“新建模板”；ListRow（名称、描述、角色链、标签、启用、编辑、复制、删除） | ✅ 模板：新建模板、内置只读、复制、编辑、启停、删除 |
| F24 | §7.8 模板编辑器 | 节点表单：角色 Select、输入来源多选、返工回到 Select、顶部迷你流程图、字段级内联校验 | ✅ 节点表单：执行角色、输入来源、返工回到、迷你流程图、字段级错误 |
| F25 | §7.11 | 无障碍：步骤轨道 role=list；节点可 Tab 聚焦、Enter 打开详情 | ✅ 步骤轨道为列表；步骤按钮可聚焦并回车打开 |
| F26 | §7.11 | i18n：UI 文案为简体中文；英文键 | ⚠️ AGENTS.md §4 要求 UI 文案为简体中文，未补英文键（需你决定） |
| F27 | §10 UI 走查 | 浅色与深色：每个界面（三态卡片、胶囊、列表、详情、子代理头、闸门、确认弹层、设置各区、模板编辑器）；窄面板 360px | 🔍 待浏览器逐项截图（浅色 / 深色 / 窄面板） |

#### 7.4 本轮新增的测试与验证（留痕）

（每完成一项，在此追加：项目编号 → 测试名 / E2E 步骤 / 截图说明。）

- B1 模型可达性预检：`test_workflow_recovery.py` 中 `test_resume_is_refused_while_the_model_is_unreachable_and_keeps_the_pause`、`test_resume_goes_ahead_once_the_model_answers_again`、`test_automatic_resume_stays_paused_when_the_model_is_unreachable`、`test_retry_is_refused_while_the_model_is_unreachable_and_the_node_stays_failed`；预检实现见 `orchestration/workflow/probe.py`（与代理循环相同的供应商解析，一次 max_tokens=1 的请求，12 秒超时）。
- B2 结构化结果：`test_resume_route_answers_an_unreachable_model_with_a_structured_409`（409，detail.code = model_unavailable，暂停状态不变）。
- B3 漂移（服务端）：`test_workflow_drift_report.py`（6 个用例：暂停记录含 HEAD 与小文件内容；暂停期间的提交单独报告 HEAD 变化；差异内容；拒绝清单外的路径；超大文件给出说明；旧版记录仍可比较）。
- B4 草稿换模板：`test_workflow_draft_edit.py`（替换后保留标识与标题、不占修订次数；替换后校验不通过则不落库；非草稿拒绝；集成验收不可替换；路由 409 结构化诊断、404 模板不存在）。
- B5 调整取消：`test_a_draft_cancelled_for_an_adjustment_is_recorded_as_adjusted`（closed_reason = adjusted）。
- B6 新建模板：`test_new_template_starts_with_one_explore_step_and_is_listed`。
- B9 会话生命周期：`test_workflow_session_lifecycle.py`（归档 → 运行中的程序暂停、进度保留、他人会话不受影响；永久删除 → 未结束的程序取消、已成功的保留）；归档的暂停类型为 archived（界面显示“会话已归档”）。
- B11 提示词：`test_the_prompt_tells_the_main_agent_not_to_resume_on_unrelated_requests`（无关新任务不自动续跑并询问；多个暂停时列出或“全部”；“算了”不续跑）。
- B12 事件保留：`test_the_event_log_keeps_only_the_latest_entries_of_a_run`。

## 8. 端到端验证环境（`docs/handoff/e2e/e2e_serve.py`）

- **用途**：启动真实的 FastAPI 应用、SQLite 数据库与真实的 git 仓库；模型调用由确定性执行器替代（沙箱没有模型）。
- **数据目录请用空目录**：脚本会在其中创建仓库与数据库并写入种子数据；重复运行会在已有数据上追加会话。
- **模拟用户编辑**：实现节点运行时，脚本会在主工作区写入 `docs/notes.md`。
- **失败注入**：在数据目录里写一个 `fail_once.txt`，内容为步骤键（如 `impl`），该步骤的下一次执行会失败一次。
- 脚本通过自身位置定位 `server` 目录，换电脑后无需改路径。

启动后端（仓库根目录）：

```powershell
$env:CORS_ORIGINS = "http://localhost:5173,http://localhost:5174"
server\.venv\Scripts\python.exe docs\handoff\e2e\e2e_serve.py <空数据目录> 12980
```

启动前端（另开一个终端）：

```powershell
$env:VITE_API_PORT = "12980"
npm --prefix client run dev -- --port 5174 --strictPort
```

用完后停止这两个进程（端口 12980 与 5174）。

## 9. 建议的下一步

1. 补齐第 5.1 节的界面走查，保留截图（浅色 / 深色、窄屏）。
2. 更新日志：远端已有 `v0.8.0` 标签（bdcbe4d，已发布），本分支的条目不写入该区块；版本号由维护者决定（见第 10 节）。
3. 决定第 6 节的三项。
4. 在新电脑上先跑一次 `python -m pytest tests -q` 与 `npm --prefix client run build`，确认环境一致。

## 10. 2026-10-13 本机复验记录

- 环境：`client/node_modules` 与锁文件不同步（缺 `three`），已执行 `npm --prefix client install`，未改动跟踪文件。
- 客户端：`npm --prefix client run typecheck` 与 `npm --prefix client run build` 均退出码 0（分包体积警告为既有）。
- 服务端：首次全量 `python -m pytest tests -q` 为 1318 通过（640 秒，1 条警告）；修复后复跑为 1320 通过（新增 2 项，547 秒，1 条警告），退出码 0。
- 本次修复（已提交，见 git log）：
  1. 新建模板的计划 `goal` 为空，导致之后的保存必然失败。`server/app/orchestration/workflow/presets.py` 的 `blank_plan` 改为沿用模板名。
  2. 校验错误路径改为 `flows[i].nodes[j].字段`，与其它诊断一致，编辑器才能把错误挂到对应字段旁；文案译为中文（此前直接显示 Pydantic 英文原文）。`server/app/orchestration/workflow/graph.py`，新增两条用例（`server/tests/test_workflow_graph.py`）。
  3. `client/src/components/chat/JumpDots.tsx`：乐观消息（turnId 为 null）与真实 turn 的 React key 相撞，改为带前缀的 key。
- 已在浏览器（e2e 沙箱）中核对：调整流程（程序 `cancelled` / `adjusted`，草稿卡显示「已调整」）；设置页工作流概览、并发档位、预设与「我的模板」；模板编辑器保存与中文校验提示。
- 未核对：浅色 / 深色、窄面板、漂移弹层、节点补充说明、尝试分隔线。另外，发送后界面要重新打开会话才会更新，这一点需要在页面可见时复验：测试时内置浏览器页面处于隐藏状态，`requestAnimationFrame` 不触发，聊天状态的批处理更新因此停住。
- `request_write_scope`（更正）：已对可写工作流节点开放（`server/app/orchestration/workflow/executor_subagent.py` 在可写节点的工具清单中追加 `REQUEST_SCOPE_SCHEMA`），主代理不暴露。上面「未开放」的判断有误。
- 冲突事件 `workflow.conflict.*`（更正）：已实现，见第 11 节。上文第 2、5.2 节的相关描述已过时。
- 远端已有 `v0.8.0` 标签（bdcbe4d，已发布）：本分支的更新条目不写入该区块；版本号已由维护者决定为 0.8.1（见第 11 节）。

## 11. 2026-10-13 第二轮整改记录

- 决定（来自维护者）：版本 0.8.1（`package.json` 与 `package-lock.json` 根版本号已更新，CHANGELOG 新增 `## v0.8.1`）；界面英文键不做，工作流文案保持简体中文（AGENTS.md §4）；冲突按设计补齐；启动网页版后由维护者在页面配置模型。
- 胶囊：工作流胶囊与任务胶囊共用 `client/src/components/ui/useCapsulePopover.ts`，交互一致（聚焦或悬停展开、点击钉住、Esc 与点外部关闭）；浮层居中于胶囊正上方。
- 设置：「新建模板」移到「模板」区块页头；模板编辑弹窗增加「模板名称」「说明」，保存时一并提交。
- 深色（设置 → 外观 → 深色）：DOM 层面核对对比度，修正胶囊状态与计数、编辑弹窗标签与说明、共享对话框副标题。遗留：共享危险按钮「删除这个步骤」为 4.42:1（全局配色，未改）。布局的视觉核对待页面可见时补做。
- 冲突事件：`workflow.conflict.detected / resolved` 已实现。L1 守卫自动合并（resolved）；L2 硬冲突（detected），重做写入成功（resolved）；L3 合并步骤（创建时 detected，合并完成 resolved）；L4 闸门（创建时 detected，决定后 resolved，载荷含 decision）。测试：`server/tests/test_workflow_conflict_events.py`，3 项通过。
- 调研：搜索工具两次返回模型访问错误，未取得外部资料；依据改为代码（`git merge-file --diff3` 返回码分级，见 `server/app/services/git_merge_engine.py`）。
- 测试与留痕：见计划文档 `ai/chatcoder-plan-20261013-workflow-round2.md` 的 W8 条目。

## 12. 2026-10-13 第三轮整改记录（真实模型联调与界面问题）

- 真实模型联调（牛码 / deepseek-flash）：程序 16 完整跑完（计划 → 8 节点 → 集成 → 评估闸门 → 返工 → 采纳 → succeeded）。联调中发现并修复三处：
  1. 闸门没有返工目标（集成阶段的评估）时，节点静默回到「等待人工」却没有闸门与选项。现由 `_open_review_gate` 打开闸门，无返工目标时只提供采纳 / 中止，`decide_gate` 拒绝返工并保留闸门（`server/app/orchestration/workflow/runtime.py`；测试 `server/tests/test_workflow_review_gate.py`，2 项）。
  2. 工作流结束的系统消息被显示为用户气泡：实时消息的 turn_id 为空，增量时间线漏判。`client/src/components/chat/timeline.ts` 的 `userItemOf` 统一分类；通知改为可展开的行（`client/src/components/workflow/WorkflowNotice.tsx`）。
  3. 同一程序的工作流卡片在一轮内重复（计划与启动分属两簇工具调用）。`buildTurnItems` 的 `cardedPrograms` 保证每轮每程序只出一张卡。在真实会话 `real-model-workflow-2` 的 DOM 中核对：仅 1 张卡（succeeded）。
- 胶囊合并（取代第 11 节的“两个胶囊并存”）：任务进度、工作流、未审核变更合并为一个 `.tcap`（`client/src/components/chat/ComposerCapsule.tsx`，取代已删除的 `TaskProgressCapsule.tsx`；工作流数据与浮层内容在 `client/src/components/workflow/WorkflowCapsule.tsx`）。段按有无内容显示，顺序固定为 任务 → 工作流 → 产物。
  - 浮层由单一状态机 `client/src/components/ui/useCapsulePopover.ts` 管理：同一时间只展开一段，划过即切换（修复：任务浮层在划到产物时不收起）；点击钉住，钉住后划过其它段不切换；点外部或 Esc 解除；段消失时清空状态。
  - 样式：段样式集中在 `composer.css` 的 `.tcap-*`；`flow.css` 中与之重复的旧覆盖块已删除（其值并入 `composer.css`，视觉口径不变）。
- 窄宽：`.tcap-wrap` 为 size 容器，宽度收窄时依次隐去段名“工作流”与“+N”、增删统计、状态文案；只有任务段的标题可以省略，其它段整段保留（360px 面板下三段内容完整）。
- 工作流面板运行行（窄宽重叠）：`.wf-run-foot` 两行网格，进度与时间定位在第一行，标记在第二行（`workflow.css`）。240 / 320 / 420px 下以同结构 DOM 探针核对，无重叠。
- 子代理补充说明：改为主输入卡同款（圆角卡、文本域、`cc-send` 圆形发送；Enter 发送，Shift+Enter 换行，输入法组字中不发送），只保留输入与发送（`SubagentPanel.tsx`、`workflow.css` 的 `.subagent-wf-note-*`）。
- 验证方法：用临时验证页（Vite 下挂载真实组件，store 为可控的种子数据）核对胶囊的悬停 / 钉住 / 切换、三段动态显示、360px 窄宽与浅色主题、通知展开、补充说明输入与发送按钮状态；验证页已删除，不入库。
- 测试：服务端串行 `python -m pytest tests -q` 1325 通过、0 失败（521 秒，退出码 0）。定向 6 个工作流测试文件 63 通过。第二轮的 3 失败 / 5 错误是并发运行共用测试库导致，串行后消失。客户端 `typecheck` 与 `build` 退出码 0。
- 未做 / 待你确认：`0.7.3copy/` 空目录未删（需你确认）；界面英文键不做（第 11 节）；搜索工具本轮未使用。提交与合并：本分支已提交，并合并到 master，记录见 git log。
- 进程：已停止沙箱用的 8000、8123、8088 服务；保留 12980 后端与 5174 前端（你在用）。

## 13. 2026-10-13 第四轮整改记录（右侧面板与子代理头部）

- 「查看详情」没有弹出右侧工作流面板：工作流卡片与输入框胶囊的「打开详情」都只切换了标签，面板是收起的，所以看不到变化。两处现在先展开面板，再切到工作流标签并聚焦程序（`client/src/components/workflow/WorkflowCard.tsx`、`WorkflowCapsule.tsx`）。
- 开发环境的坑（已遇到）：Vite 热更新后，同一个 store 模块可能被不同的导入方加载成多份实例。资源列表里 `store/workflow.ts` 会出现多个 `?t=` 时间戳。症状：面板已展开，却仍停在列表，「查看详情」写入的 store 与面板读取的不是同一份。修复办法：重启 dev server（保留 `VITE_API_PORT=12980`）后刷新页面；之后若再出现，先看资源列表里 `store/workflow.ts` 的 URL 是否只有一个。
- 子代理面板头部改为三行：名称行；所属路径（完整显示）；尝试次数、评估结论与「详情」开关。摘要、计划、验证、评估问题与尝试记录收在详情里，展开后高度上限 300px、内部滚动。补充说明移到面板底部，与主输入框同位置。头部高度由约 435px 降到约 100px（`SubagentPanel.tsx`、`workflow.css` 的 `.subagent-wf-*`、`global.css` 的 `.subagent-panel-top`）。
- 验证（真实会话 real-model-workflow-2）：查看详情后面板展开到 418px 并进入详情视图；子代理头部、详情的展开与收起、运行态下的补充说明输入卡，均用 DOM 测量核对。浏览器面板处于隐藏状态时 CSS 过渡不会推进，因此测量时临时禁用了过渡，未截图。
- 未做：胶囊「打开详情」需要一个进行中的工作流，沙箱会话里没有，未实际点击验证；它与卡片走的是同一段代码。
