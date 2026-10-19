# 交接：会话工具卡重做 · 变更清单净状态与撤销校验

- 初版：2026-10-19；分支：`master`（直接提交，未新建分支）
- 计划文档：`ai/chatcoder-plan-20261019-toolcard-v2.md`——**仅存在于本机**（`ai/` 被 `.gitignore` 忽略）。本文已含要点；计划文档里还没补「补充 C～G」，见 §二。
- **状态：开发暂停，按用户要求未跑构建与全量测试就提交推送。** 最后一次 `npx tsc --noEmit` 通过（提交前最后一次改动 `store/toolCard.ts` 之后跑的）；`npm run build`、全量 pytest 未重跑。

## 一、已完成（代码已在仓库中）

**服务端**
- 变更清单按「净状态」折叠：同一文件取本轮首条记录的前态、末条记录的后态；本轮新建又删除的文件（净零）不再出现；本轮删除的既有文件显示并标 `action="deleted"`。`server/app/services/rollback_service.py`（`_net_state`、`visible_change_turns`、`list_turn_changes`、`get_file_diff`）。
- 写入记录新增 `op`（create/modify/delete）与 `after_hash` 两列：`persistence/models/rollback.py`、`migrations.py`（启动时 ALTER，向后兼容；旧记录这两列为空，走 `_plan_path_legacy` 兼容分支）、`write_behind.py`。
- 命令行删除/移动识别：`extract_shell_delete_paths`、`extract_shell_move_dests`、目录展开、glob、`cd`；执行前检查点 `agent_loop.py::_register_pre_checkpoint`；轮次收尾对账 `reconcile_turn_writes`（`engine.py` 两处收尾点调用）。
- 撤销改为**严格拒绝**（取代三方合并）：文件在本轮之后被改动（用户手改或后续轮次）、被重建、或写入链不连续，则整次撤销取消、不动任何文件，409 的 `detail.conflicts[].reason` 给出原因。`build_plan(strict=True)`、`revert_turn_files`、`_conflict_result`。
- Git：`GET /git/diff-stat`（未提交 +N −M，含未跟踪文件行数）；`git_service.diff_stat`。
- 设置：`tool_card_default_form`（capsule / expanded）、`tool_card_section_defaults`（五栏布尔）；`core/config.py`、`routers/settings.py`；旧布尔 `tool_card_sections_expanded` 作回落。
- 类型：`gateway/schemas.py` 的 `FileDiffOut.action`；`packages/shared/src/index.ts`。

**客户端**（`client/src/components/chat/toolcard/` 为主）
- 工具卡重做：`SessionToolCard.tsx`、`ToolCardSections.tsx`、`GitToolSection.tsx`、`useToolCardData.ts`、`processFold.ts`（进程动态折叠，纯函数）、`gitGraph.ts`（提交图谱布局）、`store/toolCard.ts`、`styles/toolcard.css`。
- 通用组件：`ui/Floating.tsx`（portal + 定位）、`ui/HoverCard.tsx`；样式 `overlay.css`；图标 `icons.tsx`（IconFileDiff / IconGitCommit / IconGitGraph）。
- 胶囊悬浮右上角、不影响消息流居中：`ChatPanel.tsx`、`contentColumnGeometry.ts`、`useContentColumnGeometry.ts`；胶囊 ↔ 卡片形变动画（`tcard-morph`）；阴影变淡；展开时的滚动条闪现已修（卡片 `flex: 0 0 auto`，正文 `max-height` 由 JS 以 px 写入 `--tcard-max-h`）。
- 底色：消息流 / 顶栏 / 右侧面板不随世界主题变——浅色 `--color-panel: #f8f8f8`（`tokens.css`），深色 `neutral-900`（我的取值，未经用户确认），`themes.css` 去掉了覆盖。
- 老后端兼容：后端没有 `/git/diff-stat`（404/405）时回落到 `status-all` / `status`，Git 栏显示「分支 + N 个文件」而非 +N −M；设置接口缺新字段时保留本机缓存的选择。**用户当时「没展示 Git 栏」的根因**就是 dev 前端（5173）连着已安装的旧后端（12973）。
- 单测：`client/tests/process-fold.test.ts`、`git-graph.test.ts`；`server/tests/test_turn_changes_net.py`、`test_git_diff_stat.py`、`test_tool_card_settings.py`。

## 二、用户需求（按提出顺序）与完成度

1. 工具卡：浅色底 `#FFFFFF`；Git 栏（更改 +N −M / 分支弹窗：搜索、切换、创建并检出新分支对话框、Git 图谱对话框 / 提交或推送→打开右侧 Git 面板）；计划图标样式；进程每项一行 + hover 浮窗看全文 + 优先级动态折叠（进行中 > 即将执行 > 靠后 > 已完成，折叠刚好够用，未折叠至少 5 项高度，折叠行 hover 看被折叠项）；子代理、工作流已完成合并一行；整卡最大高度；设置里可配默认形态与逐栏默认展开。→ **除「设置页入口」外均已实现**，见 §四。
2. 变更卡片（`TurnChangesCard`）排版 / 间距 / 样式；文件名被截断时 hover 显示全名。→ **未做**。
3. 补充 A：消息流 / 顶栏 / 右侧面板底色不随主题：浅 `#F8F8F8`，深取反转色。→ 已做。
4. 补充 B：变更清单逻辑（建后又删不展示、删除既有文件标已删除、撤销校验后续手动改动并说明原因）。→ **后端已做**；前端展示与错误提示未做（§四）。
5. 补充 C：胶囊悬浮右上角、不影响居中，切换动画，胶囊样式。→ 已做，部分场景未验（§三）。
6. 补充 D：展开时滚动条闪现跳变。→ 已修并验证。
7. 补充 E：阴影太深太大。→ 已改淡。
8. 补充 F：项目有 Git 为何没有 Git 栏。→ 见 §一「老后端兼容」。
9. 补充 G（最新）：**Git 面板层级关系没做好**。→ **未做**（§四第 1 项）。

## 三、验证记录

已在浏览器（5176 + 临时后端 8000）核对：Git 栏三行与分支弹窗样式；有本地改动时切分支的拦截对话框；「暂存并切换」流程（冲突时提示并打开 Git 面板）；切换后卡片的分支与更改数刷新；形变过程中无滚动条。后端单测：新增三个文件通过；既有回滚 / 审查相关 87 项通过（`test_rollback_*`、`test_plan_rollback`、`test_revert_turn_files`、`test_review`、`test_file_reviews`）。

**未验证**：Git 图谱对话框、新建分支对话框、卡片 ⋯ 设置菜单；计划 / 进程 / 子代理 / 工作流各栏在真实数据下的表现与折叠行 hover；深色与各世界主题下的胶囊 / 卡片；1.5× 缩放；老后端降级在浏览器里的实际显示（只过了类型检查）；`npm run build`。

## 四、未完成清单（建议顺序）

1. **Git 面板层级关系**（用户最新反馈，截图：「更改 1」分组标题与其下的 `b.txt M` 文件行勾选框在同一 x 位置，看不出分组 → 文件的从属；「未跟踪文件」同理）。入口：`client/src/components/panel/GitPanel.tsx` 约 598–700 行（`changeRow`、`gp-section` 渲染），样式 `client/src/styles/git-panel.css` 约 472–630 行（`.gp-section-head` / `.gp-section-body` / `.gp-row`）。**尚未读代码、未改动。** 方向：文件行整体右移一级，让文件勾选框与图标对齐到分组标题文字起点之后；分组标题用字号 / 字重 / 底色区分；遵守设计语言——**禁止左侧装饰竖线**。冲突区、历史视图里的文件行（`gp-cv-file`）一并检查。
2. 设置页「会话工具卡」分组：`components/settings/AppearancePanel.tsx` 增加默认形态（胶囊 / 展开）与五栏默认展开，「修改即保存」，用 `saveToolCardForm` / `saveToolCardSection` / `hydrateToolCardPrefs`。
3. `GitPanel.tsx` 监听 `window` 事件 `chatcoder:git-changed`（工具卡切换分支 / 暂存后已派发）并刷新。
4. 变更卡片：`components/chat/TurnChangesCard.tsx` + `styles/turn-changes.css` 排版、间距、样式；文件名截断时用 `ui/Tooltip` 显示全名 / 路径；已删除文件的「打开」置灰或隐藏。
5. 撤销被拒绝时的展示：`store/chat.ts::revertTurnFiles` 目前只 `setFlowError`，需把 409 的 `detail.conflicts[]`（文件 + reason）带回，在确认对话框里逐文件说明原因（`RollbackConfirmModal.tsx` 可复用）。
6. 审查预览对已删除文件：`FileTreePanel.tsx` / `store/panel.ts` 的 `DiffPreview` 增加 `deleted`，隐藏「当前内容」、头部显示「已删除」、不读磁盘；老后端无 `action` 字段时按现状显示。
7. 补 §三「未验证」项；然后 `CHANGELOG.md`（§六）、更新计划文档（补充 C～G）、`npm run typecheck` + `npm run build`、pytest（**单进程**，见下）。

## 五、环境与注意

- **后端必须重启 / 重新打包才有新能力**：5173 的 dev 前端连的是 12973 上已安装的旧后端，缺 `/git/diff-stat`、设置新字段、变更清单修复与撤销严格校验。前端对旧后端已降级，不会报错。不要结束承载会话的 Electron 进程 / 后端，也不要重置数据库（新增列均为向后兼容的 ALTER）。
- 临时验证环境：`.claude/launch.json` 的 `client-dev-verify`（端口 5176，`VITE_API_PORT=8000`）+ 在 `server/` 下用 `.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000`，并设置 `DATABASE_URL`（`sqlite+aiosqlite:///<临时目录>/verify.db`）、`UPLOADS_DIR`、`WORKSPACE_ROOT`、`CHATCODER_USER_CONFIG` 指向临时目录。临时库与工作区（含 `feat/login` 分支和暂存冲突的测试仓库）在会话临时目录，可能已被清理，需要重建一个有几次提交与多个分支的 Git 仓库。
- 卡片只在内容列能让出足够宽度时才展开，否则回落为胶囊（右侧面板打开、窗口窄时都是胶囊）。测试展开形态要加宽窗口，并把「内容最大宽度」调小（本次用 700）。
- 面板隐藏时 `requestAnimationFrame` 不触发：验证动画用 `getAnimations()` 暂停并按 `currentTime` 取样。多次热更新后 zustand store 可能有多个 `?t=` 实例，异常时重启 dev server。
- pytest 只能单进程跑（共用临时库，两个进程同跑会互相 drop 表）：`cd server && .venv\Scripts\python.exe -m pytest tests/test_turn_changes_net.py tests/test_git_diff_stat.py tests/test_tool_card_settings.py tests/test_revert_turn_files.py tests/test_rollback_safety.py tests/test_plan_rollback.py -q`。客户端纯函数单测：`node client/tests/process-fold.test.ts`、`node client/tests/git-graph.test.ts`。
- 设计假设（要改请先确认）：进程栏未折叠最多 5 项 + 最多 2 个折叠行（已完成 / 待执行）；只含 1 项的折叠组还原成原条目；Git 栏只要项目是 Git 仓库就显示。

## 六、CHANGELOG 草稿（**未写入**——设置页与变更卡片未交付，不写未交付的项；写入时只追加到 `## v0.8.3`）

新增
- 右上角工具卡新增 Git 栏：看未提交行数、切换与新建分支、查看提交图谱
- 工具卡可设置默认形态（胶囊或展开）与各栏目默认展开状态（设置页完成后再写）

优化
- 工具卡进程栏每项一行、悬停看全文，步骤多时按优先级自动折叠
- 工具卡里已完成的子代理与工作流合并为一行，运行中的不折叠
- 工具卡收起时悬浮在右上角不挤占消息流，展开与收起有形变过渡
- 工具卡阴影更轻；消息流、顶栏与右侧面板底色不再随主题变化

修复
- 展开工具卡时先出现滚动条再消失、画面跳动的问题
- 本轮新建又删除的文件仍出现在变更清单、审查时报文件不存在的问题
- 本轮删除的已有文件没有出现在变更清单的问题，现在会标注已删除
- 撤销本轮改动时文件在之后被手动改过会拒绝并说明原因，不再覆盖（前端提示完成后再写）

## 七、本次提交一并带入的其他未提交改动（非本会话开发，未逐项复核）

电脑操控的元素优先定位与 OCR 精简（`tools/desktop-core/`、`server/app/core/desktop_*.py`、`desktop` 路由与工具、相关测试）、折叠展开动画（`ui/Collapse.tsx`、侧栏、变更清单、Git 面板等）、设置页拓展 / 插件 / 工具白名单、工作流卡与下一步建议卡、`AGENTS.md` / `CLAUDE.md` / `CHANGELOG.md` 的既有改动。出问题时先从这些排查，对应 CHANGELOG 条目在 `## v0.8.3` 中。
