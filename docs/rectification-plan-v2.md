# chatcoder v2 前后端联合整改与优化方案

> 版本：v2.0（在 `docs/frontend-ui-rebuild-plan.md` 已执行的基础上）
> 执行者：低级 AI。所有改动均给出**文件路径、现状行号、根因、改法、验收标准**，逐条执行，禁止自由发挥。
> 本轮范围：**前端 + 后端一起改**。后端在 `server/`（FastAPI + SQLAlchemy async），前端在 `client/`（React 18 + TS + Vite + zustand + 内联 `<style>`）。

---

## 0. 执行总原则（红线）

1. 不引入任何新 npm / pip 依赖。
2. 前端样式模式不变：`global.css` token + 组件内联 `<style>`。
3. 数据库变更只走 `server/app/persistence/migrations.py` 的幂等补列机制（参照现有 `("sessions", "rules_doc", "VARCHAR(512)")` 的写法），**禁止删列、禁止改已有列**。
4. 已有 API 路径不删不改语义，只允许**新增字段/新增端点**；`rules_doc` 旧字段保留兼容。
5. 每阶段结束：前端 `cd client && npx tsc -b --noEmit && npm run build`；后端 `cd server && python -m pytest tests -q`（若有）+ 确认 `python -c "from app.main import create_app; create_app()"` 无导入错误。
6. 删除文件前先全文搜索引用。
7. 文案简体中文；协议枚举值（`in_progress` 等）保持英文原文，仅显示层映射。

---

## 1. Bug 修复清单（先修 bug，再做重构）

### B1 【主消息页】「agent-? 将任务「」标记为；」幽灵提示

**根因**（已定位，两条代码路径）：
- 后端拆解需求时发一条 **计划卡消息**：`server/app/orchestration/orchestrator.py:100-112`，`msg_type=task_card`、`sender_id=None`、content 为 `{understanding, tasks: [...]}` —— **没有 `title`/`status`/`agent_name` 字段**。
- 前端 `client/src/components/chat/timeline.ts` 把**所有** `task_card` 都分给 `TaskCardEntry`；`TaskCardEntry.tsx:11-18` 读 `title/status/agent_name` 全部落空，于是渲染出 `agent-? 将任务「」标记为；`。
- 而状态卡消息（`agent_runtime.py:355-367`、`review.py:262-274` 的 `_emit_main_card`）content 才是 `{task_id, title, status, assignee, note, agent_name}`。

**改法**：
1. `client/src/components/chat/timeline.ts` 的 `buildTimeline` 中，`task_card` 分支增加区分：
   ```ts
   if (m.msg_type === "task_card") {
     const c = m.content as Record<string, unknown>;
     if (Array.isArray(c.tasks)) { items.push({ kind: "plan_summary", msg: m }); continue; } // 计划卡
     if (!c.status && !c.title) { continue; } // 字段全空的畸形卡直接丢弃
     items.push({ kind: "task_card", msg: m }); continue;
   }
   ```
   `TimelineEntry` 联合类型新增 `{ kind: "plan_summary"; msg: MessageOut }`。
2. 新建 `client/src/components/chat/entries/PlanSummaryEntry.tsx`：渲染为一张「需求理解」卡——标题行 `📋 需求理解`（用 `icons.tsx` 的 IconClipboard，禁 emoji）+ 正文 `content.understanding`（走 `MarkdownContent`）+ 底部一行小字 `共拆解 {content.tasks.length} 项任务，见下方任务清单`。样式：左缘 3px `var(--plan-color)` 竖条、`background: var(--bg-muted)`、`border-radius: var(--radius-sm)`、`padding: 10px 14px`。
3. `MessageFlow.tsx` 分发表加 `plan_summary → PlanSummaryEntry`。
4. `TaskCardEntry.tsx` 兜底加固：`agent_name` 缺失时，从 `useChatStore.getState().agents` 按 `sender_id` 查名字；仍无则显示 `系统`。`status` 为空时不渲染该行（返回 null）。
5. **验收**：发一个新需求，消息流中不再出现 `agent-? 将任务「」标记为；`，拆解后显示「需求理解」卡；任务状态流转提示（`XX 将任务「YY」标记为 执行中`）正常显示 agent 名。

### B2 【运行状态】任务全完成后仍显示「调度中」+「思考中…」

**根因**（三个叠加，缺一不可修）：
1. `client/src/store/chat.ts:414-444` `pollTasksUntilDone`：轮询发现没有 pending/in_progress/in_review 任务后只 `clearInterval`，**从不把 `isRunning` 置回 false**。
2. 后端任务全部完成时，`server/app/orchestration/scheduler.py:105-165` `_maybe_announce_completion` 只写了一条总结消息入库，**没有广播任何「会话已结束」事件**；前端 `isRunning` 完全依赖 `agent.status` 事件逐个减员（`chat.ts:195-223`），事件一旦漏收/迟到就永远卡死。
3. `chat.ts:164-180` 的 `task.step` 处理会无条件 `runningAgents.add(aid)` 且 `isRunning=true`，但没有对应的移除路径；若 `agent.status=done` 之后再收到迟到的 `task.step`，该 agent 永远留在 runningAgents。

**改法**：
1. **后端新增完成事件**：`scheduler.py` `_maybe_announce_completion` 在 `await db.commit()`（:164）之后追加广播：
   ```python
   from app.gateway.ws import manager  # 与现有 ws 广播同一 manager,先读 app/gateway/ws.py 确认导出名
   await manager.broadcast(self.session_id, {
       "event": "session.completed",
       "payload": {"session_id": self.session_id},
   })
   ```
   （若 `manager.broadcast` 签名不同，先读 `ws.py` 与 `agent_runtime.py:375-386` 的 `_broadcast_event` 实现对齐写法；也可直接复用 `agent_runtime._broadcast_event(session_id, {...})` 的同款逻辑在 scheduler 内写一个本地辅助函数。）
2. **前端监听**：`chat.ts` `initSession` 的 `wsClient.on` 回调链中，`session.interrupted` 分支旁新增：
   ```ts
   } else if (ev === "session.completed") {
     set({ isRunning: false, runningAgents: new Set(), agentProgress: {} });
     _debouncedRefresh(sessionId);
   }
   ```
3. **轮询兜底修正**：`pollTasksUntilDone` 的 `tick` 中，`useChatStore.setState` 改为同时根据任务推导：
   ```ts
   const stillRunning = tasks.some((t) => ["pending", "in_progress", "in_review"].includes(t.status));
   useChatStore.setState({
     tasks, messages, artifacts, planConfirmed: true,
     ...(stillRunning ? {} : { isRunning: false, runningAgents: new Set<number>(), agentProgress: {} }),
   });
   ```
   （原有的 stillRunning 判断上移到 setState 之前，勿重复计算。）
4. **`task.step` 迟到防护**：`chat.ts:164-180` 分支中，add 到 runningAgents 前先判断该 task 是否已完结：
   ```ts
   const t = s.tasks.find((x) => x.id === Number(payload.task_id));
   if (t && ["done", "cancelled", "blocked", "rejected"].includes(t.status)) return {};
   ```
   注意原代码是 `set((s) => ({...}))` 形式，把该判断放进 updater 内部，命中时 `return {}`。
5. **验收**：跑一个完整需求，最后一条「全部任务完成」总结出现后：顶栏「调度中」徽章消失变为「空闲」，消息流末尾「思考中…」消失；断网 30s 重连后状态依然正确（轮询兜底生效）。

### B3 【子会话】工具结果输出二进制乱码刷屏

**根因**：截图中 `fs.read` 读取了二进制/无换行日志文件（compile.log），`ToolEntry` 把 `result.output` 原文渲染，满屏 `�`。
**改法**（前端防御即可，不动后端）：
1. `client/src/components/chat/entries/ToolEntry.tsx` 新增工具函数：
   ```ts
   function sanitizeOutput(raw: string): { text: string; truncated: boolean; binary: boolean } {
     if (!raw) return { text: "", truncated: false, binary: false };
     const sample = raw.slice(0, 2000);
     const bad = (sample.match(/[\uFFFD\u0000-\u0008\u000E-\u001F]/g) || []).length;
     if (bad > sample.length * 0.05) return { text: "(二进制或不可读内容,已省略)", truncated: false, binary: true };
     const MAX = 4000;
     return raw.length > MAX
       ? { text: raw.slice(0, MAX), truncated: true, binary: false }
       : { text: raw, truncated: false, binary: false };
   }
   ```
2. 结果区渲染处使用 `sanitizeOutput(result.output)`；`truncated` 时末尾追加一行 muted 小字 `… 已截断,完整内容见产物文件`。
3. 参数区 value 字符串同样截断 300 字符。
4. **验收**：再读该日志文件，结果显示「(二进制或不可读内容,已省略)」，不再刷屏。

### B4 【交付总结】Leader 总结消息满屏 emoji

**根因**：`server/app/orchestration/chat_handler.py:338-360` `_generate_completion_summary` 硬编码 🎉📋✅⚠️📁💡。
**改法**（后端文案去 emoji，改 Markdown 结构）：
- `lines` 改为：
  ```python
  lines = ["**全部任务完成,交付总结:**", ""]
  lines.append("**各成员产出:**")
  # mark 改为 "- [x]" / "- [ ]" 形式
  lines.append(f"  {'- [x]' if t.status == 'done' else '- [ ]'} @{name} — {t.title}")
  # "📁 成品文件:" 改 "**成品文件:**";"💡 ..." 提示行去掉 emoji 保留文字
  ```
- 不改消息协议，仅改字符串。
- **验收**：完成总结以 Markdown 标题/复选列表渲染，无 emoji。

### B5 【任务清单】右侧信息不对齐

**根因**：`PlanCard.tsx:46-52` 任务行 DOM 顺序为 `图标 | 标题(flex:1) | 状态徽章 | 「N 条 ›」`，条数文本只在有子会话时出现，导致状态徽章位置左右漂移。
**改法**：
1. DOM 顺序改为：`图标 | 标题(flex:1) | 「N 条 ›」(固定槽位) | 状态徽章(固定槽位)`。
2. 样式：
   ```css
   .plan-task-count { width: 48px; text-align: right; font-size: var(--fs-xs); color: var(--text-muted); flex-shrink: 0; }
   .plan-task-status { width: 56px; text-align: center; flex-shrink: 0; }  /* badge 本体保持 capsule,槽位定宽 */
   ```
   无条数时也渲染空槽 `<span className="plan-task-count" />`。
3. **验收**：所有任务行的「N 条 ›」与状态徽章在两列固定位置垂直对齐。

### B6 【成员历史弹窗】状态英文原文 + 内容重复

**根因**：截图中 `AgentHistoryModal` 直接显示 `done`/`in_review` 英文，且「参与的任务」与「群聊发言」两区重复展示同一任务。
**改法**：
1. 读 `client/src/components/AgentHistoryModal.tsx`，把所有任务状态文本替换为 `statusLabel()`（从 `chat/timeline.ts` 导入，已是中文映射）。
2. 「群聊发言」区过滤掉 `msg_type === "task_card"` 的消息（这些已在「参与的任务」展示），只保留 `text`。
3. 列表项加 `title` 截断（`overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 100%`）。
4. **验收**：弹窗内无英文状态、无重复条目。

### B7 【协议健壮性】其他顺手修的小问题

| # | 位置 | 问题 | 改法 |
|---|---|---|---|
| B7-1 | `client/src/components/NewSessionDialog.tsx:16` | 团队永远取 `sessions[0].team_id`，多团队时建错群 | 改为团队下拉选择（数据源 `api.listTeams()`，见 §5.2），无 sessions 时兜底 `1` |
| B7-2 | `client/src/components/SessionSwitcher.tsx` | 已无人引用的遗留文件 | 全文搜索确认零引用后删除 |
| B7-3 | `client/src/components/chat/ComposerBox.tsx:58` | `disabled = !input.trim() \|\| isRunning`，运行中无法输入（连排队都不行） | 本期保持禁止发送，但 **textarea 不禁用**（仅发送按钮禁用），用户可先打字 |
| B7-4 | `server/app/orchestration/context.py:78-86` | `_load_session_rules` 找到第一个文件即 return，多项目目录只能加载一份规范 | 见 §4.3 多文件规则文档改造 |
| B7-5 | `client/src/components/Workspace.tsx` 顶栏 | 标题文字垂直未居中（截图可见标题偏上） | `.ws-title` 确认 `align-items: center; height: 100%`；`.ws-header` 加 `align-items: center`；标题 `line-height: 1`，`font-size: var(--fs-lg)`；运行徽章/按钮 `height: 26px` 统一 |
| B7-6 | 后端 `review.py` / `agent_runtime.py` 两处 `_emit_main_card` 重复定义 | 逻辑重复易改错 | 保持现状（不重构），但修改 task_card content 结构时**两处必须同步改** |

---

## 2. 子会话展示重构（废除抽屉，改内联展开 + 成员弹窗）

### 2.1 交互新模型

| 入口 | 现状 | 改为 |
|---|---|---|
| PlanCard 任务行点击 | 打开右侧 ThreadDrawer | **行内手风琴展开**：任务行下方展开子会话内容区，`max-height: 360px; overflow-y: auto`，再点收起；同时只允许一个任务展开 |
| AgentPanel 成员卡点击 | 打开 AgentHistoryModal | 保持弹窗，但弹窗内「参与的任务」每行可点击 → **打开二级任务会话弹窗（Modal）** 展示该任务子会话 |
| ThreadDrawer | 存在 | **删除** `client/src/components/chat/ThreadDrawer.tsx`（先全文搜索引用：`ChatPanel.tsx`、`AgentHistoryModal.tsx`，全部切换后再删） |

### 2.2 PlanCard 内联展开实现

`PlanCard.tsx` 改造：
1. 新增 state：`const [expandedId, setExpandedId] = useState<number | null>(null);`；`onTaskClick` prop 删除（不再通知父级开抽屉）。
2. 任务行点击：`setExpandedId(v => v === t.id ? null : t.id)`，并 `openThread(t.id)`（确保 `threads[t.id]` 有数据，逻辑已在 store）。
3. 展开区渲染在**该任务行正下方**：
   ```tsx
   {expandedId === t.id && (
     <div className="plan-thread-inline">
       <ThreadInlineView taskId={t.id} />
     </div>
   )}
   ```
   ```css
   .plan-thread-inline {
     max-height: 360px; overflow-y: auto;
     margin: 2px 4px 8px; padding: 8px 10px;
     background: var(--bg-muted); border: 1px solid var(--border);
     border-radius: var(--radius-sm);
     animation: slideUpFade var(--dur-normal) var(--ease-out-expo) both;
   }
   ```
4. 新建 `client/src/components/chat/ThreadInlineView.tsx`：
   ```tsx
   export function ThreadInlineView({ taskId }: { taskId: number }) {
     const msgs = useChatStore((s) => s.threads[taskId] || []);
     if (msgs.length === 0) return <div className="tiv-empty">暂无详细过程记录</div>;
     return <MessageFlow messages={msgs} variant="thread" />;
   }
   ```
   （`tiv-empty`：居中 muted 小字，padding 24px。）
5. `ChatPanel.tsx`：移除 `ThreadDrawer` 的 import、state（`threadTask`）与渲染；`PlanCard` 不再传 `onTaskClick`。

### 2.3 任务会话弹窗（成员历史二级）

新建 `client/src/components/chat/TaskThreadModal.tsx`：
- 基于 `Modal.tsx`，`width={760} height={560}`，标题 = 状态徽章 + 任务标题，副标题 `子会话 #id · N 条记录`。
- 头部右侧保留「重新执行」按钮（仅 `status === "pending"` 可用，调 `executeTask`，逻辑从 ThreadDrawer 平移）。
- 进行中任务显示进度条（DOM/样式平移 ThreadDrawer 的 `.tdr-progress` 段，class 前缀改 `.ttm-`）。
- 内容区：`<MessageFlow messages={threads[task.id] || []} variant="thread" />`。
- `AgentHistoryModal` 的「参与的任务」行点击 → 打开 `TaskThreadModal`（组件内局部 state 控制，`openThread(task.id)` 预取数据）。

### 2.4 子会话内容 Codex 风工具调用优化（主/子会话共用）

现状子会话里工具调用是「调用工具 fs.list 参数{...}」「工具结果 [fs.list] ok ...」这种**文字行**，信息密度低且丑。参考 Codex：连续同工具调用聚合成一行摘要（如「已读取 3 个文件 ›」）。

**改法**：
1. `timeline.ts` 的 `buildTimeline` 末尾新增 **tool 聚合 pass**：
   ```ts
   // 把相邻的、同 agent(sender_id 相同)、同 tool 的 tool 条目合并为 tool_group
   export type TimelineEntry = ... | { kind: "tool_group"; tool: string; agentKey: string; items: { msg: MessageOut; result?: MessageOut }[] };
   ```
   规则：遍历结果数组，当前条目为 `tool` 且与前一 `tool`/`tool_group` 条目的 `tool` 相同、`sender_id` 相同、中间无其他类型条目 → 并入 group；否则单独保留。**只有同一 tool 连续出现 ≥2 次才聚合**。
2. 新建 `entries/ToolGroupEntry.tsx`：
   - 折叠态一行：`[chevron] [工具图标] {聚合短语}` + 右侧全部成功时绿色 `✓ N`，有失败时红色 `✓ N-1 ✕ 1`。
   - 聚合短语映射（复用 ToolEntry 的 `TOOL_VERB`，扩展加聚合版）：
     ```ts
     const GROUP_VERB: Record<string, (n: number) => string> = {
       "fs.read": (n) => `已读取 ${n} 个文件`,
       "fs.list": (n) => `已查看 ${n} 个目录`,
       "fs.write": (n) => `已写入 ${n} 个文件`,
       "terminal.exec": (n) => `已执行 ${n} 条命令`,
       "editor.apply_diff": (n) => `已修改 ${n} 个文件`,
       "web.fetch": (n) => `已访问 ${n} 个页面`,
       "git.diff": () => `已查看变更`,
       "ci.run": (n) => `已运行 ${n} 次校验`,
       "memory.search": (n) => `已搜索 ${n} 次记忆`,
     };
     ```
   - 展开态：组内每个调用渲染为一行迷你摘要（图标 + 动词短语 + 状态），每行可再点击展开完整参数/结果（复用 `ToolEntry` 的展开区，把 ToolEntry 的展开体抽成 `ToolDetailBody` 子组件供两处复用——抽在 `ToolEntry.tsx` 内导出即可）。
3. 单个 `ToolEntry` 视觉同步改紧凑：行高 30px、参数/结果区左侧 2px `var(--tool-color)` 竖线 + 缩进 14px（替代现在的整卡边框）。
4. **验收**：子会话内连续 3 次 `fs.list` 显示为「已查看 3 个目录 ›」一行；展开可见每次调用的目录与结果；乱码输出按 B3 处理；主会话若出现连续工具调用同样聚合。

---

## 3. Composer 修正

`client/src/components/chat/ComposerBox.tsx`：
1. **删除模型选择下拉**（:107-116 的 `showModel` 区块与对应 state/icon import）——多 agent 群聊每个 agent 有自己的模型，输入框选模型是错误语义。
2. **删除「默认权限」禁用占位按钮**（:117-119）——占位控件造成误解，等后端有协议再加。
3. 保留：附件（disabled 占位）、@成员、语音（disabled 占位）、发送/停止。
4. 删除后工具行左侧只剩 3 个图标按钮，样式不变。
5. **验收**：工具行无「模型: 默认」「默认权限」。

---

## 4. 规则文档多文件化 + 工作目录不可变（前后端）

### 4.1 后端：sessions 新增 `rules_docs` JSON 列

1. `server/app/persistence/migrations.py` 的迁移表中追加一行：
   ```python
   ("sessions", "rules_docs", "JSON"),
   ```
   （先读该文件确认迁移机制对 JSON 类型的写法是否与现有列一致；若 SQLite 不支持 JSON 原生类型则用 `TEXT`，service 层 `json.dumps/loads`——**按现有 migrations.py 的实际机制执行**。）
2. `server/app/persistence/models/message.py` Session 模型追加：
   ```python
   rules_docs: Mapped[list | None] = mapped_column(JSON, nullable=True)
   ```
   （import 参照同文件 `knowledge_base_ids` 字段的写法，保持一致。）
3. `server/app/gateway/schemas.py`：
   - `SessionCreate` 追加 `rules_docs: list[str] | None = None`
   - `SessionConfigUpdate` 追加 `rules_docs: list[str] | None = None`
   - `SessionOut` 追加 `rules_docs: list[str] | None = None`
4. `server/app/gateway/routers/sessions.py` 的 SessionOut 组装处（:37 附近）追加 `rules_docs=getattr(session, "rules_docs", None)`；创建（:53 附近）与更新（:297 附近）透传。
5. `server/app/services/session_service.py`：`create_session` 与 `update_session_config` 接受并落库 `rules_docs`。

### 4.2 后端：工作目录不可变

`session_service.update_session_config`（:49 起）开头追加：
```python
if data.get("workspace_root") is not None:
    current = getattr(session, "workspace_root", None)
    if current and data["workspace_root"] != current:
        raise ValueError("工作目录创建后不可更改")
```
（先读该函数现状，确认 `data` 形态与 `session` 获取方式后按现有风格插入；路由层 `sessions.py:288-303` 若未捕获 ValueError 返回 400，补上 `except ValueError as e: raise HTTPException(400, str(e))`，参照同文件其他路由的错误处理。）

### 4.3 后端：多规则文档加载

`server/app/orchestration/context.py:58-86` `_load_session_rules` 重写：
```python
async def _load_session_rules(session) -> str:
    """加载群规则文档(支持多文件)。

    优先级:session.rules_docs(多文件) > session.rules_doc(旧单文件兼容) > 自动探测
    (工作目录根 + 一级子目录的 AGENTS.md/.cursorrules/CLAUDE.md)。
    多个文件拼接,各自带 (文件名) 头,总量上限 8000 字符。
    """
    from pathlib import Path
    from app.core.config import resolve_workspace_root

    workspace = resolve_workspace_root(getattr(session, "workspace_root", None))
    candidates: list[Path] = []
    for rel in (getattr(session, "rules_docs", None) or []):
        p = Path(rel)
        candidates.append(p if p.is_absolute() else Path(workspace) / rel)
    legacy = getattr(session, "rules_doc", None)
    if legacy:
        p = Path(legacy)
        candidates.append(p if p.is_absolute() else Path(workspace) / legacy)
    # 自动探测:根目录 + 一级子目录(前后端多项目场景)
    roots = [Path(workspace)]
    try:
        roots += [d for d in Path(workspace).iterdir() if d.is_dir() and not d.name.startswith(".")][:8]
    except OSError:
        pass
    for root in roots:
        for name in ("AGENTS.md", ".cursorrules", "CLAUDE.md"):
            candidates.append(root / name)

    parts: list[str] = []
    seen: set[str] = set()
    total = 0
    for p in candidates:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        try:
            if p.is_file():
                text = p.read_text(encoding="utf-8", errors="replace").strip()
                if not text:
                    continue
                chunk = f"({p.name})\n{text[:4000]}"
                if total + len(chunk) > 8000:
                    break
                parts.append(chunk)
                total += len(chunk)
        except OSError:
            continue
    return "\n\n".join(parts)
```

### 4.4 后端：规则文档智能扫描端点（新建群聊用）

1. 新建 `server/app/gateway/routers/workspace.py`：
   ```python
   """工作目录辅助:扫描规范文档候选。"""
   from pathlib import Path
   from fastapi import APIRouter, Query
   from app.core.config import resolve_workspace_root

   router = APIRouter(prefix="/utils", tags=["utils"])

   _RULE_NAMES = ("AGENTS.md", ".cursorrules", "CLAUDE.md")

   @router.get("/scan-rules-docs", response_model=list[str])
   async def scan_rules_docs(path: str = Query(...)) -> list[str]:
       """扫描指定目录(根+一级子目录)下的规范文档,返回相对路径列表。"""
       root = Path(path)
       if not root.is_dir():
           return []
       found: list[str] = []
       dirs = [root]
       try:
           dirs += [d for d in root.iterdir() if d.is_dir() and not d.name.startswith(".")][:8]
       except OSError:
           pass
       for d in dirs:
           for name in _RULE_NAMES:
               p = d / name
               if p.is_file():
                   found.append(str(p.relative_to(root)))
       return found
   ```
2. `server/app/main.py` 注册该 router（参照现有 router 注册行追加 `app.include_router(workspace_router)`，注意 import 与 `/api` 前缀的挂载方式与现有路由一致）。
3. 前端 `api/client.ts` 追加：
   ```ts
   scanRulesDocs: (path: string) => req<string[]>(`/utils/scan-rules-docs?path=${encodeURIComponent(path)}`),
   ```
   （`req` 用该文件现有的 GET 封装函数名，先读文件确认。）

### 4.5 前端：新建群聊弹窗重做（拉人 + 目录 + 规则文档）

`NewSessionDialog.tsx` 重写为三区块表单：
1. **群聊名称**：必填，默认 `新群聊`。
2. **选择团队（拉人进群）**：下拉列出 `api.listTeams()` 全部团队；选中后下方以 chip 形式预览该团队成员（`api.listAgents(teamId)`，头像色点 + 名字，只读展示）。默认选中当前会话的团队（`currentTeamId`），无则第一个。
3. **工作目录**：输入框 + 「浏览…」（Electron）；**必填校验**（为空时禁用创建按钮，提示「工作目录创建后不可更改，请谨慎选择」）。
4. **规则文档**：
   - 输入工作目录后自动调 `api.scanRulesDocs(dir)`（防抖 500ms），把扫描结果渲染为 checkbox 列表，**默认全部勾选**；扫描结果为空时显示 muted 小字「未扫描到规范文档，将在创建后自动探测 AGENTS.md 等文件」。
   - 追加一行「手动添加」：一个输入框 + 「添加」按钮，把输入的相对路径加入勾选列表（去重）。
   - 勾选项最终作为 `rules_docs: string[]` 传给 `createSession`。
5. `store/chat.ts` 的 `createSession` action 签名扩展为：
   ```ts
   createSession: (teamId: number, title?: string, workspaceRoot?: string, rulesDocs?: string[]) => Promise<void>;
   ```
   内部 `api.createSession({ team_id, title, workspace_root, rules_docs })`。`api.createSession` 的 body 类型同步加 `rules_docs?: string[]`。
6. **验收**：新建群聊可选团队、选目录、自动扫描出前后端两个子项目的 AGENTS.md 并默认勾选；创建后 `GET /api/sessions/{id}` 返回 `rules_docs` 数组；agent 执行时上下文包含多份规范。

### 4.6 前端：设置弹窗移除「当前群聊配置」

- `SettingsModal.tsx` 的 `GeneralTab`：**整块删除**「当前群聊配置」区（:96-129 工作目录/规则文档/保存群配置按钮）与相关 state、import（`useChatStore`、`updateSessionConfig` 等）。群级配置入口改到 §5.4 的「群设置弹窗」。
- `GeneralTab` 保留：主题切换、全局默认工作目录。
- **验收**：设置-通用里不再有群聊工作目录/规则文档表单；`PUT /sessions/{id}/config` 传不同 `workspace_root` 返回 400。

---

## 5. 左侧栏重构（工作目录分组 + 新建入口 + 运行动画 + 色彩）

### 5.1 结构新模型

```
┌─────────────────────┐
│ ＋ 新建群聊          │  ← 置顶主按钮(accent 实心)
├─────────────────────┤
│ 群聊工作台           │  ← 主导航(保持 4 项,样式升级见 5.5)
│ 任务看板             │
│ 团队管理             │
│ 知识库               │
├─────────────────────┤
│ 群聊            (+)  │  ← (+) 同样打开新建弹窗
│ ▾ F:\project\yipinCode      │  ← 工作目录分组头(可折叠,默认展开)
│   ● 待办清单项目·默认会话     │  ← 会话项;运行中显示脉冲点
│ ▸ F:\project\work           │
│ ▾ 未分组                     │  ← workspace_root 为空的归此组
├─────────────────────┤
│ ⚙ 模型与设置          │
└─────────────────────┘
```

### 5.2 分组逻辑

`Sidebar.tsx` 会话列表区重写：
1. 分组计算：
   ```ts
   const groups = useMemo(() => {
     const map = new Map<string, SessionOut[]>();
     for (const s of sessions.filter((x) => x.status !== "archived")) {
       const key = s.workspace_root?.trim() || "";
       if (!map.has(key)) map.set(key, []);
       map.get(key)!.push(s);
     }
     return [...map.entries()].sort((a, b) => (a[0] === "" ? 1 : b[0] === "" ? -1 : a[0].localeCompare(b[0])));
   }, [sessions]);
   ```
2. 每组：组头（chevron + 文件夹图标 + 路径末段加粗显示、完整路径 `title` 提示 + 组内会话数）+ 会话项列表。折叠状态 `useState<Record<string, boolean>>`，默认全展开。
3. 组头路径显示规则：`F:\project\yipinCode` → 加粗 `yipinCode`，前面路径 muted 小字。
4. 会话项：脉冲点（见 5.3）+ 名称；active 样式保持。

### 5.3 运行中会话动画

1. **后端**：`sessions.py` 的 `GET /sessions`（:41）响应中每个 session 追加 `has_running: bool`。实现：在 `session_service.list_sessions` 返回后，路由层一次性查询：
   ```python
   # 伪代码,先读 tasks 表模型确认字段
   from app.persistence.models.task import Task
   from sqlalchemy import select
   res = await db.execute(
       select(Task.session_id).where(Task.status.in_(["pending", "in_progress", "in_review"]))
   )
   running_ids = {r[0] for r in res.all()}
   # 组装 SessionOut 时 has_running = s.id in running_ids
   ```
   `schemas.py` 的 `SessionOut` 追加 `has_running: bool = False`。
2. **前端**：`api/client.ts` 的 `SessionOut` 追加 `has_running?: boolean`；会话项圆点：
   ```tsx
   <span className={`session-dot${session.has_running ? " running" : ""}`} />
   ```
   ```css
   .session-dot.running { background: var(--info); animation: pulse 1.4s ease-in-out infinite; box-shadow: 0 0 0 3px rgba(53,116,212,0.15); }
   ```
3. 切换进某会话后其运行状态由 ws 实时驱动，但**其他会话**的 `has_running` 只在 `loadSessions` 时刷新——在 `session.completed` / 发送新需求成功后顺手调一次 `loadSessions()` 刷新列表（注意 `loadSessions` 会触发自动切换逻辑，先读现有代码确认不会误切；现有实现只在 `currentSessionId` 为空时自动选，安全）。

### 5.4 会话项右键/悬停操作

- 会话项 hover 显示右侧「⋯」图标按钮，点击弹出小菜单：「群设置」「归档」。
- 「归档」：ConfirmDialog 确认后调 `api` 的 `DELETE /sessions/{id}`（已存在，`sessions.py:306`），然后 `loadSessions()`。
- 「群设置」：打开新建的 `SessionSettingsDialog.tsx`：
  - 显示工作目录（**只读**，muted 说明「创建后不可更改」）；
  - 规则文档多文件编辑：与新建弹窗同款的 checkbox 列表 + 手动添加（初始值 `session.rules_docs || []`，目录已知可直接 `scanRulesDocs` 补候选，把「已扫描到但未勾选」的列在下方可补勾）；
  - 群名称编辑；
  - 保存调 `updateSessionConfig(sessionId, { title, rules_docs })`（`updateSessionConfig` 的 data 类型追加 `rules_docs?: string[]`，**禁止再传 workspace_root**）。
- 菜单组件：若无现成下拉，用绝对定位小卡实现（参照 `ComposerBox` 的 `.composer-dropdown` 样式），点击外部关闭（`useEffect` 挂 document mousedown 监听）。

### 5.5 侧栏视觉升级（解决「全局就一个颜色」）

1. 侧栏底色与主区拉开层次：`global.css` 新增：
   ```css
   :root[data-theme="light"] { --bg-sidebar: #f0f2f6; }
   :root[data-theme="dark"]  { --bg-sidebar: #14161b; }
   ```
   `.sidebar { background: var(--bg-sidebar); }`。
2. 「＋ 新建群聊」置顶主按钮：`background: var(--accent); color: var(--accent-contrast); border-radius: var(--radius-sm); padding: 8px; font-weight: 500; width: 100%;`，hover `filter: brightness(1.05)`。
3. 主导航 active 项：左侧 3px accent 竖条 + `var(--accent-dim)` 底（替代现在仅变色）。
4. 工作目录组头：`font-size: var(--fs-xs); color: var(--text-secondary); font-weight: 600;` + 文件夹图标用 `var(--warning)` 色（让侧栏出现第二个色彩点）。
5. 会话运行点 `var(--info)`、active 竖条 `var(--accent)`——侧栏色彩体系：accent（选中）/ warning（目录）/ info（运行）/ muted（常规），告别单色。
6. 「群聊」标题与主导航之间加 `border-top: 1px solid var(--border)` 分隔。

---

## 6. 团队管理页重做（TeamPanel.tsx）

现状问题：团队是顶部一排 chip、成员卡信息挤、删除按钮通红刺眼、大面积留白。

新结构（两栏）：
```
┌────────────┬──────────────────────────────────┐
│ 团队(3)     │  全栈开发小组          [+ 添加成员] │
│ ┌────────┐ │ ┌─────┐ ┌─────┐ ┌─────┐ ┌─────┐ │
│ │全栈开发 │ │ │成员卡 │ │ ...                          │
│ │小组 10人│ │ └─────┘                              │
│ └────────┘ │                                      │
│ + 新建团队  │                                      │
│ ✨ 智能组队  │                                      │
└────────────┴──────────────────────────────────┘
```
1. 左栏（200px）：团队列表卡（名称 + 成员数 + active 高亮）+ 底部「新建团队」「智能组队」两个 ghost 按钮（功能沿用现有 `api`，逻辑不变只挪位置）。
2. 右栏：选中团队名（`--fs-lg` 加粗）+ Leader 标记 + 「+ 添加成员」主按钮。
3. 成员卡重设计：`width: 200px; border: 1px solid var(--border); border-radius: var(--radius-lg); padding: 14px;`：
   - 顶部：方块头像（底色 `agentColor(a.id)` 12% 透明度、文字同色，`agentColor` 从 `chat/agentMeta.ts` 导入）+ 右侧角色徽章（Leader = accent 色 badge，其余 muted）。
   - 名称（`--fs-md` 600）+ 模型一行（muted `fs-xs`，icon + 模型名）。
   - 底部操作：「编辑」「删除」改为两个 icon-btn（铅笔/垃圾桶），删除用 `var(--error)` 色图标、hover 浅红底，点击弹 ConfirmDialog（复用现有组件）。
4. 布局：`display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 12px;`。
5. 编辑/添加成员的表单弹窗逻辑**完全保留现有代码**，只把触发按钮换成新样式。
6. 空团队：右栏居中空态（图标 + 「暂无成员，点击右上角添加」）。

## 7. 知识库页重做（KnowledgePanel.tsx）

新结构（两栏）：
```
┌────────────┬──────────────────────────────────┐
│ 知识库(2)   │  项目规范库      [搜索____] [+ 文档] │
│ ┌────────┐ │ ┌──────────────────────────────┐ │
│ │项目规范库│ │ │ 文档行:标题 | 类型badge | 时间  │ │
│ │ N 篇文档 │ │ │ (点击展开右侧预览/或行内展开)  │ │
│ └────────┘ │ └──────────────────────────────┘ │
│ + 新建知识库 │ 空态:选择一个知识库查看文档          │
└────────────┴──────────────────────────────────┘
```
1. 左栏（220px）：KB 列表卡（名称 + 文档数 + active 高亮 + hover 出现删除 icon-btn，ConfirmDialog 确认）+ 底部「+ 新建知识库」。
2. 右栏：KB 名 + 副标题 `N 篇文档 · 知识库为全局共享,所有群聊可用`；工具行右侧：搜索框（调现有 `GET /knowledge-bases/{id}/docs/search`）+「+ 添加文档」主按钮。
3. 文档行：`border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 10px 14px;`：标题 + 类型 badge + 创建时间（`formatTime`）+ hover 出现「预览/删除」icon 按钮。
4. 预览：点击文档行在下方行内展开内容区（`max-height: 320px; overflow: auto;` MarkdownContent 渲染）——不再右侧大片空白。
5. 空态统一：左栏空「暂无知识库」、右栏未选「← 选择一个知识库查看文档」、选中无文档「暂无文档，点击右上角添加」。
6. 所有增删查改 API 调用沿用现有实现（`knowledge.py` 11 个端点已齐），**只重构展示层**。

---

## 8. 设置弹窗重做（SettingsModal.tsx）

### 8.1 结构：左侧固定 Tab 栏 + 右侧滚动内容

解决「上下滑动 tab 页不固定」：把横向 tab 条改为**左侧竖排 tab 栏**（独立列、不随内容滚动）：
```
┌────────────────────────────────────┐
│ 模型与设置                       ✕ │
├──────────┬─────────────────────────┤
│ 通用      │                         │
│ 模型      │   (仅此列滚动)           │
│ 关于      │                         │
└──────────┴─────────────────────────┘
```
1. `Modal` 的 children 改为：
   ```tsx
   <div className="settings-layout">
     <div className="settings-rail"> {/* width: 140px; flex-shrink: 0; border-right: 1px solid var(--border); padding: 12px 8px; */}
       {tabs.map(...)}
     </div>
     <div className="settings-body"> {/* flex: 1; overflow-y: auto; padding: 4px 4px 4px 16px; */}
       ...
     </div>
   </div>
   ```
   `.settings-layout { display: flex; height: 100%; min-height: 0; }`。
2. Rail tab 项：`display: block; width: 100%; text-align: left; padding: 8px 12px; border-radius: var(--radius-sm);`，active：`background: var(--accent-dim); color: var(--accent);`。
3. **Tab 从 4 个减为 3 个**：通用 / 模型 / 关于。
   - 「团队」「知识库」tab **删除**（与主导航的独立页重复，且弹窗里嵌整页是反模式）。
   - 通用：主题切换 + 全局默认工作目录（群级配置已按 §4.6 移除）。
   - 模型：保留 `ModelConfigPanel`。
   - 关于：应用名 chatcoder、版本号（读 `client/package.json` 的 version，`import pkg from "../../package.json"`，vite 默认支持 JSON import）、一句简介。
4. **验收**：内容区滚动时左侧 tab 固定；设置里不再出现群聊工作目录表单与团队/知识库 tab。

---

## 9. 顶栏与全局细节

1. **顶栏对齐**（B7-5 已列，汇总于此）：`.ws-header` 高 48px、`display: flex; align-items: center;`；左区（返回 + 标题 + 路径 chip）`display: flex; align-items: center; gap: 10px; min-width: 0;`；路径 chip：`font-family: var(--font-mono); font-size: var(--fs-xs); color: var(--text-muted); background: var(--bg-muted); padding: 3px 8px; border-radius: 999px;`；右区运行控制 `display: flex; align-items: center; gap: 8px;`。
2. **空闲态徽章**：`isRunning === false` 时右区显示 muted 小字 `空闲`（不要占大徽章）。
3. **全局滚动条**：现有 6px 滚动条在浅色下几乎看不见，`--border-light` → 拇指色加深一档（light 用 `#c9ced6`）。
4. **消息区最大宽度**：`variant="thread"` 时 MessageFlow 容器不加 max-width 限制（抽屉/弹窗内自然铺满）。

---

## 10. 实施阶段（严格按序）

### 阶段 1：Bug 修复（B1~B7）
- 顺序：B1 → B2（后端先加 `session.completed` 广播，前端再接）→ B3 → B4 → B5 → B6 → B7。
- 验收：§1 各条验收标准逐项过。

### 阶段 2：规则文档多文件 + 工作目录不可变（§4.1~4.4 后端）
- 先迁移与模型，再 service/router，最后 `_load_session_rules` 与扫描端点。
- 后端自验：起服务后 `PUT /sessions/{id}/config` 传 `rules_docs` 能落库；传不同 `workspace_root` 返回 400；`GET /api/utils/scan-rules-docs?path=...` 返回数组。

### 阶段 3：子会话重构（§2）+ Composer（§3）
- ToolGroupEntry 聚合 → PlanCard 内联展开 → TaskThreadModal → 删 ThreadDrawer → Composer 删模型/权限。
- 验收：任务行内联展开高度受限可滚动；连续同工具聚合显示；成员弹窗二级打开任务会话。

### 阶段 4：新建群聊 + 群设置（§4.5、§4.6、§5.4）
- NewSessionDialog 重写、SessionSettingsDialog 新建、设置弹窗移除群配置。

### 阶段 5：侧栏重构（§5）+ 顶栏/全局细节（§9）
- 分组、动画、色彩、归档菜单。

### 阶段 6：团队页 + 知识库页 + 设置弹窗（§6、§7、§8）

### 阶段 7：全面回归
- 新发需求全流程：拆解 → PlanSummaryEntry → 确认/自动执行 → 内联展开看工具聚合 → 完成总结（无 emoji）→ 状态正确归零。
- 暗色主题走查。
- `client` 构建 + `server` 启动冒烟。

---

## 11. 风险与备注

1. `rules_doc`（旧单文件）保留读写兼容：后端 `_load_session_rules` 仍读它；前端任何界面不再编辑它。新数据以 `rules_docs` 为准。
2. `session.completed` 广播前必须确认 `ws.py` 的 broadcast 签名（`agent_runtime.py:375-386` 有现成 `_broadcast_event` 参考）；若 broadcast 在无连接时抛错，按现有 `except Exception` 静默处理。
3. PlanCard 内联展开与 `activeThreadId` store 字段无耦合，`closeThread` 若只剩删除后的 ThreadDrawer 引用，确认零引用后可从 store 移除该 action（**可选，不强制**；保守做法是保留）。
4. 多团队时 `switchSession` 依赖 `s.team_id`，分组渲染不得过滤掉 `team_id` 为空的会话（显示但点击 toast 提示「该群未关联团队」）。
5. 后端 migration 若用 TEXT 存 JSON：service 层读取时 `json.loads(session.rules_docs or "[]")`，写入时 `json.dumps`，schemas 出参保持 `list[str]`。
6. 扫描端点有路径遍历面：仅做只读 `is_file` 探测，不读取内容、不执行写操作，风险可接受；无需鉴权（桌面应用，与现有端点一致）。

---

## 12. 验收总清单

- [ ] 主消息页不再出现 `agent-? 将任务「」标记为；`
- [ ] 任务全部完成后「调度中」「思考中…」正确消失（含断网恢复场景）
- [ ] 二进制文件读取结果不刷屏
- [ ] 交付总结无 emoji
- [ ] 任务清单右侧「N 条 ›」与状态徽章两列对齐
- [ ] 成员历史弹窗无英文状态、无重复
- [ ] 子会话为任务行内联展开（≤360px 可滚动），成员弹窗可二级打开任务会话
- [ ] 连续同工具调用聚合为「已读取 N 个文件 ›」样式
- [ ] 输入框无模型选择、无权限占位
- [ ] 新建群聊：可选团队（预览成员）、必选目录、自动扫描勾选规则文档
- [ ] 工作目录修改被后端拒绝（400），前端入口只读
- [ ] 多规则文档注入 agent 上下文（日志或上下文验证）
- [ ] 侧栏按工作目录分组、运行中会话脉冲动画、有「＋ 新建群聊」置顶按钮
- [ ] 会话可归档（确认弹窗）
- [ ] 群设置弹窗可编辑名称与多规则文档
- [ ] 设置弹窗左栏 tab 固定，仅 通用/模型/关于 三项
- [ ] 团队管理、知识库两栏新布局落地，交互功能不缺失
- [ ] 前端 `tsc` + `build` 通过；后端冒烟通过
