# chatcoder 前端全面优化与重构方案

> 版本：v1.0
> 定位：本方案交付给执行型 AI 实施。所有改动点均给出**文件路径、现状行号、改法、验收标准**，执行时逐条对照，禁止自由发挥。
> 参考对象：Trae（Work/Code/Design 三栏 + 步骤流消息页）、WorkBuddy（任务列表侧栏 + 工具调用折叠摘要 + 底部多功能输入条）。

---

## 0. 执行总原则（先读这一节）

1. **不改后端**：本方案纯前端改造。不修改 `server/`、`electron/` 任何文件；不修改 REST API 与 WebSocket 事件协议。
2. **不改数据层逻辑**：`client/src/store/chat.ts`、`client/src/api/client.ts`、`client/src/api/ws.ts` 三个文件**只允许新增字段/方法，禁止删除或改写已有 action 的行为语义**（如 `sendRequirement`、`switchSession` 的刷新逻辑）。
3. **样式技术栈不变**：继续使用「`global.css` 设计 token + 组件内联 `<style>`」的既有模式，**不引入 Tailwind / styled-components / CSS Modules / 任何新依赖**（唯一例外见 §9.4 图标方案，仍然零依赖）。
4. **每完成一个阶段必须验证**：`cd client && npx tsc -b --noEmit` 无错误，`npm run build` 成功。
5. **组件拆分允许新增文件**：新组件统一放在 `client/src/components/chat/` 目录下，文件名与导出组件名保持 PascalCase 一致。
6. **禁止破坏 Electron 兼容**：`vite.config.ts` 的 `base: "./"` 不变；不要引入依赖 `http://localhost` 的硬编码。
7. **所有文案保持中文**，与现有 UI 一致。
8. 删除旧代码前先全文搜索确认无其他引用（`className` 字符串也搜一遍）。

---

## 1. 现状盘点（改造前必读）

### 1.1 技术栈

| 维度 | 现状 |
|---|---|
| 框架 | React 18.3.1 + TypeScript 5.5 + Vite 5.4 |
| 状态管理 | zustand 4.5（`store/chat.ts` 业务、`store/theme.ts` 主题） |
| 样式 | `styles/global.css`（312 行，CSS 变量 token）+ 每个组件尾部内联 `<style>` |
| Markdown | `react-markdown` + `remark-gfm` + `rehype-highlight`（**固定引入 github-dark 主题，浅色模式下代码块仍是深色，是 bug**） |
| 路由 | 无路由库，`App.tsx` 用 `useState<NavKey>` 切换视图 |

### 1.2 组件清单（全部平铺在 `client/src/components/`）

| 文件 | 职责 |
|---|---|
| `App.tsx` | 根布局：标题栏 + app-body（Sidebar + Workspace） |
| `TitleBar.tsx` | 自定义标题栏（Electron 无边框窗口） |
| `Sidebar.tsx` | 左导航：主导航 4 项 + 群聊会话列表 + 底部设置 |
| `Workspace.tsx` | 主工作区壳：header（标题/返回/工作路径）+ 视图分发 |
| `ChatPanel.tsx` | 主消息页：状态栏 + 消息列表 + 任务清单 + 产物清单 + 输入框（含 `MessageBubble`） |
| `AgentPanel.tsx` | 右侧团队面板：成员卡片 + 状态点 |
| `ThreadDetailModal.tsx` | 子会话详情弹窗：时间线 + 工具调用配对渲染 |
| `ToolCallCard.tsx` | 工具调用折叠卡片（emoji 图标 + 参数/结果 pre） |
| `TaskBoard.tsx` / `TeamPanel.tsx` / `KnowledgePanel.tsx` | 看板/团队/知识库视图 |
| `SettingsModal.tsx` / `ModelConfigPanel.tsx` | 设置弹窗 |
| `AgentHistoryModal.tsx` / `ArtifactViewer.tsx` / `Modal.tsx` / `SessionSwitcher.tsx` | 弹窗与工具组件 |
| `MarkdownContent.tsx` | Markdown 渲染 |
| `motion.ts` | 动画工具 |

### 1.3 消息数据模型（改造核心依据）

`MessageOut`（`api/client.ts:56-65`）：
- `thread_id: number | null` — `null` 为主群消息，非 null 为任务子会话消息
- `sender_type`：`user` / `system` / agent
- `msg_type`：`text` / `task_card` / `approval` / `tool_call` / `tool_result` / `artifact`
- `content`：自由 JSON。`tool_call` 含 `{tool, args, call_key, agent_name}`；`tool_result` 含 `{call_key, ok, output, error, duration_ms}`

store 分流逻辑（`store/chat.ts:329-349`）：`thread_id == null` 进 `messages`，否则进 `threads[thread_id]`。
**主消息页目前完全不展示 tool_call/tool_result**（它们都在子会话里），主群只有 text/task_card/approval —— 这是主消息页「粗糙」的根因之一：用户看不到执行过程，只能看到干巴巴的文字回复和一个任务清单表格。

子会话工具配对逻辑已存在于 `ThreadDetailModal.tsx:43-78`（按 `call_key` 把 `tool_result` 合并到 `tool_call` 上），**该逻辑要抽成公共工具函数复用**（见 §5.2）。

### 1.4 现状问题清单（本方案要解决的）

| # | 问题 | 位置 |
|---|---|---|
| P1 | 浅色主题下代码高亮仍是 github-dark 深色主题，视觉割裂 | `MarkdownContent.tsx:1-4` |
| P2 | 主消息页看不到任何执行过程（工具调用/思考），只有最终文本，不像 agent 工具 | `ChatPanel.tsx` |
| P3 | 顶部状态栏把「中断」「回滚」「确认拆解」三个不同层级、不同危险度的按钮平铺在一行；「回滚」是破坏性操作却无任何二次确认，紧挨「中断」极易误点 | `ChatPanel.tsx:47-70` |
| P4 | 「确认拆解」按钮出现在状态栏，与它作用的「任务清单」卡片分离，用户无法理解按钮和任务的关联 | `ChatPanel.tsx:67-69` |
| P5 | 任务清单、产物清单是两个独立大卡，永久占据消息流底部（即使任务已完成），刷屏后找不到对话内容 | `ChatPanel.tsx:95-157` |
| P6 | 消息气泡千人一面：用户/Leader/agent/system 都是左侧字母头像 + 灰底气泡，无层次、无时间戳 | `ChatPanel.tsx:374-442` |
| P7 | 子会话用 Modal 弹窗展示，宽度 820px，内容一多就挤压；且与主消息页渲染逻辑完全不同（Modal 内有时间线圆点，主页没有），体验不一致 | `ThreadDetailModal.tsx` |
| P8 | ToolCallCard 用 emoji 当图标（📄✏️⚡），风格廉价；参数永远是 `JSON.stringify` 原文，长命令不可读 | `ToolCallCard.tsx:19-29` |
| P9 | 新建群聊不询问名称，直接生成 `新群聊 1234`，也不让选工作目录，用户事后无法辨认 | `Sidebar.tsx:86-94` |
| P10 | 输入框只有 textarea + 发送键：无模型选择、无 @成员、无附件、发送中无停止按钮 | `ChatPanel.tsx:161-187` |
| P11 | `store.error` 写入后**全应用没有任何地方展示**，请求失败用户无感知 | `store/chat.ts` 多处 `set({ error })` |
| P12 | 消息列表任何变化都强制滚到底（`ChatPanel.tsx:27-29`），用户上翻历史时被拽回底部 | `ChatPanel.tsx` |
| P13 | 整体视觉：圆角 14px + 灰底卡片堆叠，对比度低、层级糊，离参考软件的「干净、通透、专业」差距大 | 全局 |
| P14 | 无加载骨架屏、无 WS 断连提示、无空会话引导（只有一行「发布需求」） | 全局 |
| P15 | 右侧团队面板只有成员列表，信息量低，占据 220px 常驻宽度 | `AgentPanel.tsx` |

---

## 2. 设计目标与参考语言

### 2.1 从参考软件提取的设计语言

**Trae（参考图 2）**：
- 浅色为主，大面积白底，内容区最大宽度居中，阅读密度舒适
- 侧栏是「任务列表」：空间分组（chatcoder / yipinCode / work）+ 任务条目 + 时间
- 消息页是**步骤流**：agent 的每个动作是一行紧凑摘要（如「查看 git 工具、session 创建、ws 广播、App.tsx 整体布局」「已读取 3 个文件 ›」），可展开；文末是「整改完成总结」卡片
- 底部输入条集成：附件、@引用、模型选择器（`glm-5.2 ▾`）、发送键
- 顶部有 Work / Code / Design 模式 Tab

**WorkBuddy（参考图 3）**：
- 侧栏：新建任务按钮 + 功能导航 + 任务列表（按空间分组折叠）+ 底部用户区
- 消息页：思考块（「深度思考」可折叠）、工具调用一行摘要（图标 + 动词短语 + 文件名 + chevron，如「🔍 调用子任务 扫描后端项目结构和模式」）、文档类产物卡片（标题 + 图标 + 可展开）
- 底部输入条：「+」附件、权限选择（`默认权限 ▾`）、模型选择（`Deepseek-V4-Pro ▾`）、语音、发送

### 2.2 chatcoder 改造目标（一句话）

> 把「聊天软件皮」改成「agent 执行台」：**消息流 = 步骤流**，工具调用/思考/计划/产物都是消息流里的一等公民；主消息页与子会话页共用同一套渲染管线；操作按钮各归其位、危险操作有确认。

---

## 3. 设计系统规范（Design Tokens）

全部改动落在 `client/src/styles/global.css` 的 `:root` 变量上。**只改值与新增变量，不删除旧变量名**（旧变量仍被未改造的组件引用）。

### 3.1 圆角与间距（收敛，更利落）

```css
:root {
  --radius: 10px;        /* 原 14px，收敛 */
  --radius-sm: 8px;      /* 原 10px */
  --radius-xs: 6px;      /* 不变 */
  --radius-lg: 14px;     /* 新增：弹窗/大卡片 */
}
```

### 3.2 浅色主题（主战场，全面提亮）

```css
::root[data-theme="light"] {
  --bg: #f7f8fa;           /* 原 #f3f4f7，更干净 */
  --bg-elevated: #ffffff;  /* 不变 */
  --bg-muted: #f2f3f5;     /* 原 #f5f6f8 */
  --bg-hover: #eceef1;
  --bg-active: #e4e7ec;
  --border: #e8eaee;       /* 原 #e6e8ec，更浅更隐形 */
  --border-light: #d9dce2;
  --text: #1f2329;         /* 原 #2c3036，正文加深提升对比 */
  --text-secondary: #4e5560;
  --text-muted: #8a919c;
  --accent: #4f5bd5;       /* 原 #5b63e0，稍微压深，白底上更稳 */
  --accent-dim: rgba(79, 91, 213, 0.08);
  --accent-contrast: #ffffff;
  --success: #22a06b;
  --warning: #c0822a;
  --error: #d64545;
  --info: #3574d4;
}
```

### 3.3 深色主题（微调即可）

`--bg: #16181d` 系列保持，仅把 `--text` 从 `#d4d7de` 提到 `#dcdfe5`，`--border` 从 `#262932` 提到 `#2a2e38`。

### 3.4 字体阶梯（新增变量，全组件统一引用）

```css
:root {
  --fs-xs: 11px;    /* 辅助信息/标签 */
  --fs-sm: 12px;    /* 次级内容/工具摘要 */
  --fs-md: 13px;    /* 正文 */
  --fs-lg: 14px;    /* 强调正文/卡片标题 */
  --fs-xl: 16px;    /* 区块标题 */
  --lh-body: 1.65;  /* 正文行高 */
}
```

### 3.5 新增语义色 token（步骤流用）

```css
:root {
  --tool-color: var(--accent);    /* 工具调用节点 */
  --thought-color: var(--info);   /* 思考节点 */
  --artifact-color: var(--success); /* 产物节点 */
  --plan-color: var(--warning);   /* 计划/确认节点 */
}
```

### 3.6 修复 P1：代码高亮双主题

1. `MarkdownContent.tsx` 中删除对 `highlight.js/styles/github-dark.css` 的静态 import。
2. 在 `global.css` 里手写两套最小高亮色（**不要引入两个完整 hljs 主题 css 再覆盖，体积和优先级都麻烦**）。按 `data-theme` 作用域：

```css
/* 浅色主题代码块 */
:root[data-theme="light"] pre code.hljs { background: #f6f8fa; color: #1f2329; }
:root[data-theme="light"] .hljs-keyword, :root[data-theme="light"] .hljs-selector-tag { color: #d73a49; }
:root[data-theme="light"] .hljs-string, :root[data-theme="light"] .hljs-attr { color: #032f62; }
:root[data-theme="light"] .hljs-comment { color: #6a737d; font-style: italic; }
:root[data-theme="light"] .hljs-number, :root[data-theme="light"] .hljs-literal { color: #005cc5; }
:root[data-theme="light"] .hljs-function, :root[data-theme="light"] .hljs-title { color: #6f42c1; }
/* 深色主题沿用现有 github-dark 观感，值照搬现有即可 */
:root[data-theme="dark"] pre code.hljs { background: #0d1117; color: #c9d1d9; }
:root[data-theme="dark"] .hljs-keyword, :root[data-theme="dark"] .hljs-selector-tag { color: #ff7b72; }
:root[data-theme="dark"] .hljs-string, :root[data-theme="dark"] .hljs-attr { color: #a5d6ff; }
:root[data-theme="dark"] .hljs-comment { color: #8b949e; font-style: italic; }
:root[data-theme="dark"] .hljs-number, :root[data-theme="dark"] .hljs-literal { color: #79c0ff; }
:root[data-theme="dark"] .hljs-function, :root[data-theme="dark"] .hljs-title { color: #d2a8ff; }
```

> 注意：删除静态 import 前先确认 `rehype-highlight` 生成的 class 就是 `hljs-*`（现有实现已是），上面只是换配色来源。

---

## 4. 整体布局重构

### 4.1 布局结构（改 `Workspace.tsx` + `App.tsx`）

现状：app-body 是「Sidebar 卡片 + Workspace 卡片」两个圆角卡片并排（`global.css:115-122` 的 `.app-body` 有 `padding: 8px; gap: 8px`）。

目标布局：

```
┌────────────────────────────────────────────────────────┐
│ TitleBar（不变）                                        │
├──────────┬─────────────────────────────────────────────┤
│ Sidebar  │ Workspace                                   │
│ 260px    │  ┌───────────────────────────────────────┐  │
│          │  │ 会话顶栏（新）:标题/路径/运行状态/操作   │  │
│          │  ├──────────────────────────┬────────────┤  │
│          │  │ 消息流（步骤流）           │ AgentPanel │  │
│          │  │  max-width 760px 居中     │ 240px可折叠 │  │
│          │  ├──────────────────────────┴────────────┤  │
│          │  │ Composer（新输入条）                    │  │
│          │  └───────────────────────────────────────┘  │
└──────────┴─────────────────────────────────────────────┘
```

具体改动：

1. `global.css` `.app-body`：去掉 `padding` 和 `gap`，改为 `padding: 0; gap: 0;`。卡片感交给各面板自己控制。
2. `.sidebar`：去掉 `border` 和 `border-radius`，改为 `border-right: 1px solid var(--border)`；宽度 `--sidebar-w: 260px`（原 220px，参考 Trae 侧栏密度）。
3. `.workspace`：去掉 `border` 与 `border-radius`，直接铺平。
4. `--sidebar-w-collapsed: 56px`（原 52px）。

### 4.2 视图顶栏（改 `Workspace.tsx` 的 `.ws-header`）

- 高度 40px → `48px`；`padding: 0 16px`。
- 左侧：返回按钮（非 chat 视图时保留）+ 视图标题（`font-size: var(--fs-lg); font-weight: 600`）。
- chat 视图追加：会话工作路径（保留现状，样式不变）。
- 右侧新增「运行控制区」（仅 chat 视图显示）：
  - 运行中：显示 `● 调度中 · N 个任务进行中` 徽章（沿用现有 status-badge 样式）+「停止」按钮（error 色描边样式）+「更多」图标按钮（下拉菜单：回滚到任务前）。
  - 空闲：只显示「空闲」徽章。
- **「回滚」从主按钮区移除，收进「更多」下拉，且点击后弹二次确认 Modal**（见 §8-R3）。

---

## 5. 核心：消息流渲染管线（主/子会话复用）

这是整个改造的中心。**目标：一套「MessageOut[] → 时间线条目 → React 节点」的管线，`ChatPanel`（主会话）和子会话视图都用它。**

### 5.1 新建目录与文件

```
client/src/components/chat/
├── MessageFlow.tsx        # 管线入口：接收 MessageOut[]，渲染整个时间线
├── timeline.ts            # 纯函数：MessageOut[] → TimelineEntry[]（从 ThreadDetailModal 抽出并扩展）
├── entries/
│   ├── TextEntry.tsx      # 文本消息（用户/agent/system）
│   ├── ThinkingEntry.tsx  # 思考块（可折叠）
│   ├── ToolEntry.tsx      # 工具调用一行摘要（可展开，替代 ToolCallCard 的展示层）
│   ├── PlanEntry.tsx      # 计划/任务清单卡片（含「确认拆解」按钮）
│   ├── ApprovalEntry.tsx  # 审批卡片（从 MessageBubble 的 approval 分支迁出）
│   ├── ArtifactEntry.tsx  # 产物卡片
│   └── TaskCardEntry.tsx  # task_card 消息（任务状态变更提示）
└── entries/entry.css.ts?  # 不需要，沿用内联 <style> 模式即可
```

> 现有 `ToolCallCard.tsx` **保留文件不删**（`AgentHistoryModal` 可能引用），但其展示职责由 `ToolEntry` 接管；若全文搜索确认只有 `ThreadDetailModal` 引用，则改造完成后删除 `ToolCallCard.tsx` 并把引用全部换到 `ToolEntry`。

### 5.2 `timeline.ts` 数据管线（详细规格）

从 `ThreadDetailModal.tsx:43-78` 抽出的配对逻辑，扩展为：

```ts
// client/src/components/chat/timeline.ts
import type { MessageOut } from "../../api/client";

export type TimelineEntry =
  | { kind: "text"; msg: MessageOut }
  | { kind: "thinking"; msg: MessageOut }                       // content.thinking === true 或 msg_type === "thinking"
  | { kind: "tool"; msg: MessageOut; result?: MessageOut }
  | { kind: "approval"; msg: MessageOut }
  | { kind: "artifact"; msg: MessageOut }
  | { kind: "task_card"; msg: MessageOut }
  | { kind: "divider"; label: string };                         // 日期/阶段分隔(可选,本期不实现)

export function buildTimeline(msgs: MessageOut[]): TimelineEntry[] {
  // 1. 复制 ThreadDetailModal.tsx:43-78 的 tool_result 配对逻辑,行为保持一致:
  //    - tool_result 按 content.call_key 找已入列的同 key tool 条目,挂到 .result
  //    - 找不到时存入 pendingResults,供后续 tool_call 领取
  // 2. msg_type 映射:
  //    tool_call -> tool; tool_result -> (被消费,不入列;孤儿 result 忽略)
  //    artifact -> artifact; approval -> approval; task_card -> task_card
  //    text -> 若 content.thinking 为真 -> thinking,否则 text
  //    其他未知类型 -> text(用 JSON.stringify(content) 兜底,保持 MessageBubble 现状行为)
  // 3. 过滤:content.text 为空字符串的 text 条目丢弃(保持 ThreadDetailModal.tsx:183 的行为)
}
```

**验收**：`ThreadDetailModal` 改用 `buildTimeline` 后，与改造前渲染的条目数量、顺序完全一致。

### 5.3 `MessageFlow.tsx`

```tsx
interface MessageFlowProps {
  messages: MessageOut[];
  /** 渲染上下文:main=主会话(宽松) / thread=子会话(紧凑) */
  variant?: "main" | "thread";
}
```

- 内部：`const entries = useMemo(() => buildTimeline(messages), [messages])`
- 按 `entry.kind` 分发到各 Entry 组件。
- 容器样式：`display: flex; flex-direction: column; gap: 2px;`（条目自身控制下间距）。
- **不再使用时间线圆点竖线**（废弃 `ThreadDetailModal` 的 `.tdm-timeline::before` 与 `.tdm-node-dot`），改用参考软件的「摘要行 + 缩进展开」风格。

### 5.4 各 Entry 组件规格

#### 5.4.1 `TextEntry`（文本消息）

分三种发送者形态：

| sender_type | 形态 |
|---|---|
| `user` | **右侧对齐气泡**：`background: var(--accent-dim); color: var(--text); border-radius: 12px 12px 4px 12px; padding: 10px 14px; max-width: 70%; margin-left: auto;` 无头像，发送者名不显示 |
| `system` | 居中灰色小字：`text-align: center; color: var(--text-muted); font-size: var(--fs-xs);` 无气泡 |
| agent | **左侧通栏**（参考 Trae/WorkBuddy 的 agent 消息）：无气泡底色，直接正文流式排布；头部一行显示 agent 名（`font-weight: 600; font-size: var(--fs-sm)`，名前加彩色圆点 `agent-color`，颜色由 `sender_id` hash 到 6 色板）+ 相对时间（`color: var(--text-muted); font-size: var(--fs-xs)`） |

agent 名与颜色工具函数放 `timeline.ts` 同目录 `agentMeta.ts`：

```ts
const PALETTE = ["#4f5bd5", "#22a06b", "#c0822a", "#d64545", "#3574d4", "#8b5cf6"];
export function agentColor(senderId: number | null): string {
  return PALETTE[Math.abs(senderId ?? 0) % PALETTE.length];
}
export function agentDisplayName(content: Record<string, unknown>, senderId: number | null): string {
  return (content.agent_name as string) || `agent-${senderId ?? "?"}`;
}
```

相对时间工具 `formatTime(iso: string | null)`：今天显示 `HH:mm`，否则显示 `M月d日 HH:mm`。放 `client/src/utils/time.ts`（新建）。

#### 5.4.2 `ThinkingEntry`（思考块）

参考 WorkBuddy「深度思考」：
- 折叠态：一行 `💭 深度思考`（用 §9.4 的 svg 图标，禁止 emoji）+ 右侧 chevron。
- 展开态：下方显示完整 Markdown 内容，左侧 2px `var(--thought-color)` 竖线 + 内容缩进 16px，文字色 `var(--text-secondary)`。
- 默认折叠；「正在进行中的思考」（该消息是会话最后一条且 `isRunning`）默认展开。

#### 5.4.3 `ToolEntry`（工具调用摘要行，**替换 ToolCallCard 的展示层**）

折叠态（一行，高 32px，hover 浅灰底）：

```
[chevron] [工具图标] [动词短语] [关键参数(截断)] ........ [状态徽章]
```

- **动词短语映射表**（组件内常量，参考 WorkBuddy 的「已读取 3 个文件」）：

```ts
const TOOL_VERB: Record<string, (args: Record<string, unknown>) => string> = {
  "fs.read":    (a) => `读取 ${basename(a.path)}`,
  "fs.write":   (a) => `写入 ${basename(a.path)}`,
  "fs.list":    (a) => `查看目录 ${basename(a.path) || a.path}`,
  "terminal.exec": (a) => `运行 ${truncate(String(a.command ?? ""), 40)}`,
  "editor.apply_diff": (a) => `修改 ${basename(a.path)}`,
  "web.fetch":  (a) => `访问 ${hostOf(String(a.url ?? ""))}`,
  "ci.run":     () => `运行校验`,
  "git.diff":   () => `查看变更`,
  "memory.search": (a) => `搜索记忆 ${truncate(String(a.query ?? ""), 20)}`,
};
// basename/truncate/hostOf 为本文件内小工具函数;取不到参数时回退显示工具名原文
```

- 状态徽章：
  - 无 result：`执行中` + 旋转 svg spinner（沿用 `.spinning`）
  - `ok`：✓ 图标 + `duration_ms` 格式化为 `0.8s` / `1m20s`（绿色）
  - `!ok`：✕ 图标 + `失败`（红色）
- 展开态（chevron 向下）：左侧 2px `var(--tool-color)` 竖线 + 缩进 16px 区域内显示：
  - 「参数」小节：key 参数表格化（不再是整段 JSON）：遍历 `args` 的 key/value，每行 `key`（mono, muted）+ `value`（mono, 截断 120 字符，title 显示全文）；仅当 args 嵌套对象时才 fallback 为 JSON pre。
  - 「结果」小节：复用 `MarkdownContent`（沿用 `_formatOutput` 对 `git.diff` 的 diff 包裹逻辑——把该函数从 `ToolCallCard.tsx` 原样迁入 `ToolEntry.tsx`）。
  - 「错误」小节：`pre` 红字（沿用现 `.tcc-error` 样式值）。
  - 结果区 `max-height: 320px; overflow-y: auto;`（保持现状）。

#### 5.4.4 `PlanEntry`（计划/任务清单卡片 —— 解决 P4/P5）

触发源不是消息，而是 store 的 `tasks`。因此在 `ChatPanel` 中单独渲染（不放 `MessageFlow` 内），但**插入位置改为消息流之中**：渲染在「最后一条用户需求消息之后」。实现方式：`ChatPanel` 渲染 `MessageFlow` 时不传全部 messages，而是：

1. 找到最后一条 `sender_type === "user"` 的消息索引 `lastUserIdx`；
2. `messages.slice(0, lastUserIdx + 1)` → `MessageFlow`；若 `tasks.length > 0` 则紧接着渲染 `<PlanCard />`；再渲染 `messages.slice(lastUserIdx + 1)` → 第二个 `MessageFlow`。
3. 若没有 user 消息，PlanCard 直接放消息流顶部。

`<PlanCard />` 规格（新文件 `client/src/components/chat/PlanCard.tsx`）：

- 卡片：`border: 1px solid var(--border); border-radius: var(--radius-lg); background: var(--bg-elevated); box-shadow: var(--shadow-1); overflow: hidden;`
- 头部：`📋 任务拆解 · N 项`（svg 图标）+ 右侧整体进度 `x/N 已完成`（`font-size: var(--fs-xs); color: var(--text-muted)`）。
- 任务行（复用现有 `.task-item` 的视觉，重做）：
  - 状态图标（沿用 `ChatPanel.tsx:457-484` 的 `TaskStatusIcon`，**移到 `PlanCard.tsx` 一并导出**，ChatPanel 删除原实现）
  - 标题（进行中时标题色 `var(--info)`，沿用现 `.is-running` 行为）
  - 状态文字徽章（沿用 `.task-status-text` 各色值）
  - 该行**整行可点击**打开子会话（替代现在的「详情(n)」小按钮），右侧显示子会话消息数 `n 条 ›`（有消息时）。hover 行底色 `var(--bg-hover)`。
- 底部操作区（仅 `hasPending && !planConfirmed` 时显示）：`border-top: 1px solid var(--border); padding: 10px 14px;`，内含：
  - 主按钮「确认拆解并执行」（`button.primary`）→ `confirmPlan(true)`
  - 次按钮「重新描述需求」→ 点击后聚焦 Composer（通过 store 新增 `focusComposerTick: number` 字段 + Composer 内 useEffect 监听实现，见 §8-R6）
- **任务全部完成后，PlanCard 整体可折叠**：头部右侧加 chevron，默认折叠为一行 `📋 任务拆解 · 8/8 已完成`，点击展开。是否折叠用组件内 `useState`，默认 `allDone`（`tasks.every(t => ["done","cancelled"].includes(t.status))`）。

#### 5.4.5 `ApprovalEntry`（审批卡片）

从 `MessageBubble` 的 approval 分支（`ChatPanel.tsx:384-407`）整体迁出，视觉重做：

- 卡片左缘 3px `var(--warning)` 竖条；标题行 `⚠ 审批请求 · {risk_level}`（svg 图标）；正文 `summary`；底部按钮「同意」（primary）/「拒绝」（error 色描边）。
- 逻辑（`wsClient.send("approval.response", ...)`）原样保留。
- **已处理的审批置灰**：组件挂载时若该 `approval_id` 已响应过，禁用按钮并显示「已同意/已拒绝」。实现：store 新增 `approvalDecisions: Record<string, boolean>`，在 `handleApproval` 里写入（§8-R7）。

#### 5.4.6 `ArtifactEntry`（产物卡片）

- 卡片一行：`[类型图标] {title}` + 右侧 `›`；点击打开 `ArtifactViewer`（保留）。
- 类型图标按 `type` 区分：file/code/doc 用不同 svg（§9.4）。
- 主消息页**不再渲染独立的产物清单卡**（删除 `ChatPanel.tsx:127-157` 的 `.artifact-list` 区块）；产物消息经由 `MessageFlow` 的 `ArtifactEntry` 自然流入消息流。`store.artifacts` 保留供其他视图使用。

#### 5.4.7 `TaskCardEntry`（任务状态提示）

- 居中小灰条样式（类似 system 消息）：`{agent} 将任务「{title}」标记为 {statusLabel}`，`color: var(--text-muted); font-size: var(--fs-xs); text-align: center;`。
- 状态文案映射沿用 `_statusLabel`（`ChatPanel.tsx:444-455`，该函数移到 `timeline.ts` 导出复用）。

### 5.5 主消息页 `ChatPanel.tsx` 重构（汇总）

重构后结构（自上而下）：

```tsx
<section className="chat-panel">
  {/* 1. 删除原 .chat-status-bar(47-70),运行状态移入 Workspace 顶栏(§4.2) */}

  <div className="chat-messages" ref={scrollRef} onScroll={handleScroll}>
    <div className="chat-messages-inner">            {/* max-width: 760px; padding: 24px 16px; */}
      {empty && <EmptyState />}                       {/* §8-R8 */}
      <MessageFlow messages={beforeLastUser} variant="main" />
      {tasks.length > 0 && <PlanCard />}
      <MessageFlow messages={afterLastUser} variant="main" />
      {isRunning && <RunningIndicator />}             {/* 消息流末尾的「思考中…」呼吸行 */}
    </div>
  </div>

  <ScrollToBottomFab visible={!atBottom && hasNew} />  {/* §8-R2 */}

  <ComposerBox />                                      {/* §6,替代原 composer 块 */}
  <ThreadDrawer ... />                                 {/* §7,替代 ThreadDetailModal */}
  <ArtifactViewer ... />                               {/* 保留 */}
</section>
```

删除清单：
- `.chat-status-bar` 及其内全部按钮（迁移去向见 §4.2 / §5.4.4）
- `.artifact-list` 区块
- `MessageBubble` 函数（职责拆入各 Entry；`AgentHistoryModal` 若引用 `MessageBubble`，改为引用 `MessageFlow`，见 §7.3）
- `_statusLabel`、`TaskStatusIcon`（迁移）

### 5.6 滚动行为修正（解决 P12）

```ts
const [atBottom, setAtBottom] = useState(true);
const handleScroll = () => {
  const el = scrollRef.current;
  if (!el) return;
  setAtBottom(el.scrollHeight - el.scrollTop - el.clientHeight < 48);
};
useEffect(() => {
  if (atBottom) scrollRef.current?.scrollTo(0, scrollRef.current.scrollHeight);
}, [messages, tasks]);
```

- 不在底部且新消息到达时：右下角浮出「↓ 新消息」按钮（`ScrollToBottomFab`），点击滚到底。
- 切换会话/初次加载时无条件滚到底。

---

## 6. Composer 输入条重构（解决 P10）

新建 `client/src/components/chat/ComposerBox.tsx`，替代 `ChatPanel.tsx:161-187`。

### 6.1 结构

```
┌──────────────────────────────────────────────────┐
│ [textarea 自动增高, 1~8 行]                       │
├──────────────────────────────────────────────────┤
│ [＋ 附件(占位禁用)] [@ 引用成员▾] [模型: 默认▾]      │
│                    [权限: 默认权限▾]   [发送/停止]  │
└──────────────────────────────────────────────────┘
```

- 外框：`border: 1px solid var(--border); border-radius: var(--radius-lg); background: var(--bg-elevated); box-shadow: var(--shadow-2);`，`:focus-within` 时 `border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-dim);`。
- 工具行：`display: flex; align-items: center; gap: 4px; padding: 6px 8px; border-top: 1px solid var(--border);` 右侧发送按钮 `margin-left: auto`。
- **@ 引用成员**：点击弹气泡菜单列出 `agents`（checkbox 多选），选中后以 chip 形式显示在 textarea 上方；发送时把选中的 agent id 数组传给 `sendRequirement(text, mentions)`（store 已支持第二参数，`chat.ts:227`，目前 UI 没接 —— 本次接上）。
- **模型选择**：读取设置中已配置模型（`ModelConfigPanel` 的数据源，从 `api/client.ts` 现有接口取；若接口无「当前默认模型」概念，则下拉只展示模型列表且选中项仅写入 localStorage `chatcoder.model`，不改变后端行为——**禁止发明新后端协议**）。
- **权限选择**：静态选项 `默认权限 / 只读 / 完全放行`，选中值写 localStorage `chatcoder.permission`，发送时**不传给后端**（后端无此协议），仅作 UI 占位并在 tooltip 注明「将在后续版本生效」。若执行 AI 判断该占位会造成误解，可整体隐藏该下拉（在文档中备注此决策）。
- **发送/停止切换**：`isRunning` 时发送按钮变为红色方块「停止」图标，点击调 `interrupt()`；否则为纸飞机发送（沿用现 svg）。`disabled = !input.trim() && !isRunning`。
- textarea：Enter 发送 / Shift+Enter 换行（保持）；自动增高：`onInput` 里 `el.style.height = "auto"; el.style.height = Math.min(el.scrollHeight, 160) + "px"`。

### 6.2 快捷命令占位

输入 `/` 开头时弹出命令气泡（仅一项「/init 扫描项目并生成规则文档」占位不可点亦可）——**本期可不做，若做不完就在工具行留一个隐藏的 `data-slot="slash-commands"` 注释标记**，严禁做一半弹出空气泡。

---

## 7. 子会话视图重构（解决 P7）

### 7.1 Modal → 右侧抽屉（ThreadDrawer）

新建 `client/src/components/chat/ThreadDrawer.tsx`，**删除 `ThreadDetailModal.tsx`**（先全文搜索引用点：`ChatPanel.tsx`、`TeamPanel.tsx` 若引用一并改）。

- 形态：从右向左滑出的抽屉，**覆盖在 AgentPanel 之上**（`position: absolute; right: 0; top: 0; bottom: 0; width: 480px;` 于 chat-panel 容器内），背景 `var(--bg-elevated)`，左缘 `border-left: 1px solid var(--border); box-shadow: var(--shadow-3)`，滑入动画 `transform: translateX(100%) → 0`，`transition: var(--dur-slow) var(--ease-out-expo)`。
- 头部（48px）：返回箭头（关闭抽屉）+ 状态徽章 + 任务标题（截断）+ 右侧「重新执行」按钮（仅 `status === "pending"` 可用，逻辑沿用 `ThreadDetailModal.tsx:102-110`）与「信息」图标按钮（切换显示任务描述/验收标准，即原 `mode === "info"` 的内容）。
- 进度条：`task.status === "in_progress"` 且 `agentProgress` 有数据时显示（沿用 `ThreadDetailModal.tsx:125-134` 的 DOM 与样式值，class 前缀 `.tdm-` 改为 `.tdr-`）。
- 内容区：`<MessageFlow messages={threads[task.id] || []} variant="thread" />` —— **与主消息页完全同一渲染管线**。`variant="thread"` 时 `TextEntry` 的 agent 消息也带头像行（因为子会话内可能多个 agent 交替发言，需要区分）。
- 空态/加载态：沿用现有文案「加载中...」「暂无详细过程记录」，样式改居中 + 骨架屏（§8-R9）。

### 7.2 打开方式不变

`PlanCard` 任务行点击 → `openThread(task.id)` + 本地 state 记录当前 task → 渲染 `ThreadDrawer`。多任务并行时允许反复开关，数据层不变。

### 7.3 `AgentHistoryModal` 适配

若其内部使用 `MessageBubble` 渲染历史消息，改为 `<MessageFlow messages={...} variant="thread" />`；若使用 `ToolCallCard`，改为 `ToolEntry`（通过 `MessageFlow` 间接使用则无需动）。**执行前先读 `AgentHistoryModal.tsx` 确认实际实现，按最小改动原则只换渲染层。**

---

## 8. 交互逻辑优化清单（不合逻辑处逐条修正）

| # | 问题 | 改法 | 涉及文件 |
|---|---|---|---|
| R1 | 「中断/回滚/确认拆解」挤在状态栏 | 状态栏整体删除；运行状态 + 停止按钮进 Workspace 顶栏（§4.2）；确认拆解进 PlanCard（§5.4.4）；回滚进顶栏「更多」下拉 | `ChatPanel.tsx`、`Workspace.tsx` |
| R2 | 回滚无确认 | 新增通用确认弹窗 `client/src/components/ConfirmDialog.tsx`（基于现有 `Modal.tsx`）：标题「回滚到任务前」，正文「将恢复执行前的文件快照并删除执行期间的消息，此操作不可撤销。」，按钮「取消 / 确认回滚」（error 色）。确认后调 `rollback()` | 新建、`Workspace.tsx` |
| R3 | 「中断」语义不清（用户以为是暂停） | 文案统一改为「停止执行」，tooltip：「停止所有正在运行的任务」 | `Workspace.tsx` |
| R4 | 新建群聊自动命名 | 点击「+」弹 `NewSessionDialog`（新建，基于 `Modal.tsx`）：字段「群聊名称」（必填，默认 `新群聊`）、「工作目录」（输入框 + Electron 下「浏览…」按钮调 `window.chatcoderAPI.selectDirectory()`，浏览器环境隐藏该按钮）、「关联团队」（下拉，数据源 `sessions[0]?.team_id` 及 `api` 现有团队列表接口——先读 `api/client.ts` 确认有 listTeams 类接口，没有则固定传 `sessions[0]?.team_id \|\| 1` 并隐藏下拉）。提交调 `createSession(teamId, title, workspaceRoot)` | `Sidebar.tsx`、新建 `NewSessionDialog.tsx` |
| R5 | 错误无感知（P11） | 新增 `Toast.tsx`：监听 `store.error`，非 null 时右上角浮出 error toast（4s 自动消失 + 手动关闭），同时 `set({ error: null })` 清除。挂载在 `App.tsx` | 新建 `Toast.tsx`、`App.tsx` |
| R6 | 「重新描述需求」入口 | PlanCard 底部次按钮 → store 新增 `composerFocusTick: number` 与 action `focusComposer()`（`set(s => ({ composerFocusTick: s.composerFocusTick + 1 }))`）；`ComposerBox` 内 `useEffect(() => textareaRef.current?.focus(), [composerFocusTick])` | `chat.ts`（仅新增）、`PlanCard.tsx`、`ComposerBox.tsx` |
| R7 | 审批可重复点击 | store 新增 `approvalDecisions: Record<string, boolean>`；`ApprovalEntry` 响应后记录并置灰 | `chat.ts`（仅新增）、`ApprovalEntry.tsx` |
| R8 | 空会话引导单薄 | `EmptyState` 组件：图标 + 「发布需求」+ 三行示例（「例：给当前项目补充单元测试」「例：重构 XX 模块并输出设计文档」）点击示例直接填入 Composer | `ChatPanel.tsx` |
| R9 | 无加载态 | `initSession` 的 `loading` 期间消息区显示骨架屏：3 条灰条（shimmer 动画，沿用 `@keyframes shimmer`） | `ChatPanel.tsx` |
| R10 | WS 断连无提示 | `ws.ts` 已有重连与否先读代码确认；在 `ChatPanel` 顶栏下方加一条细横幅：「连接已断开，正在重连…」（warning 底色），仅在 ws readyState 非 OPEN 超过 3s 时显示。实现：store 新增 `wsConnected: boolean`，`ws.ts` onopen/onclose 里 `useChatStore.setState({ wsConnected })` | `ws.ts`（仅新增回调）、`chat.ts`、`ChatPanel.tsx` |
| R11 | 消息无时间戳 | agent 消息头部行右侧加相对时间（§5.4.1）；user 气泡 hover 时右下浮出时间 tooltip | `TextEntry.tsx` |
| R12 | AgentPanel 信息少 | 成员卡片追加「当前任务」一行：由 `agentProgress[agent.id].taskId` 找到 task 标题截断显示；点击卡片改开 `ThreadDrawer`（若有进行中的 task）否则保持开 `AgentHistoryModal` | `AgentPanel.tsx` |
| R13 | 看板/团队/知识库风格不统一 | 三视图的卡片统一为：`background: var(--bg-elevated); border: 1px solid var(--border); border-radius: var(--radius-lg); box-shadow: var(--shadow-1);`，标题字阶 `var(--fs-lg)`。**仅调整样式值，不改这三个文件的任何逻辑** | `TaskBoard.tsx`、`TeamPanel.tsx`、`KnowledgePanel.tsx` |
| R14 | 设置入口语义 | 侧栏底部「设置」点开的是 Modal 且 `TITLES.settings = "模型配置"`（`Workspace.tsx:76`），语义错位。统一：侧栏 label 改「模型与设置」，Modal 标题栏文案与之对齐 | `Sidebar.tsx`、`SettingsModal.tsx` |
| R15 | 主会话切换后 Composer 残留草稿 | `switchSession` 后清空输入。实现：ComposerBox 的 `input` state 提升到 store？**不，保持本地 state**，改为 `useEffect(() => setInput(""), [currentSessionId])` | `ComposerBox.tsx` |

---

## 9. 视觉细节统一规范

### 9.1 消息区排版

- `.chat-messages-inner`：`max-width: 760px; margin: 0 auto; padding: 24px 16px 16px;`
- agent 正文：`font-size: var(--fs-md); line-height: var(--lh-body); color: var(--text);`
- 段落间距由 `MarkdownContent` 的 `p { margin: 0 0 8px; }` 保证（检查 `MarkdownContent.tsx` 现有样式，没有则补）。

### 9.2 按钮体系（`global.css` 补充）

新增两个变体类，不动现有 `button`/`button.primary`/`button.sm`：

```css
button.danger { color: var(--error); border: 1px solid var(--error); background: transparent; }
button.danger:hover { background: rgba(214, 69, 69, 0.08); color: var(--error); }
button.ghost { color: var(--text-secondary); }
button.ghost:hover { background: var(--bg-hover); color: var(--text); }
```

### 9.3 徽章（Badge）统一

状态徽章（任务状态/审批风险/工具结果）统一规格：`font-size: 10px; padding: 2px 8px; border-radius: 999px; font-weight: 500;`，颜色语义沿用现有 `.task-status-text` 各色值。执行时把 `.task-status-text`、`.tcc-status`、`.tdm-node-tag` 三处重复定义合并为 `global.css` 的 `.badge` + `.badge.{success|warning|error|info|muted}` 变体，旧 class 删除并全量替换引用。

### 9.4 图标规范（解决 P8）

- **全面禁用 emoji 图标**（`ToolCallCard.tsx:19-29` 的 TOOL_ICON、`TimelineNode` 的 🔧📦💬 等）。
- 方案：新建 `client/src/components/icons.tsx`，集中导出内联 SVG（stroke 风格，与 `Sidebar.tsx:22-68` 现有图标一致：`fill="none" stroke="currentColor" strokeWidth="1.8"`）。需要的图标：chevron-right/down、file-read、file-write、folder、terminal、diff、globe、ci-flask、git-branch、brain、tool-generic、check、x、spinner、plus、at、mic（占位）、send、stop、clipboard(plan)、package(artifact)、alert-triangle(approval)、arrow-left、refresh、more-horizontal、user。
- 每个导出形如 `export const IconFileRead = ({ size = 14 }: { size?: number }) => (<svg width={size} height={size} .../>);`
- **禁止引入 react-icons 等依赖**，SVG 从 feather-icons 风格手写（与现有 Sidebar 图标同源）。

### 9.5 头像规范

- 用户消息：无头像（右气泡）。
- agent 消息：不显示方块字母头像，改用「彩色圆点 + 名字」（§5.4.1）。
- AgentPanel 成员卡片保留方块头像但圆角改 `var(--radius-sm)`、去掉 border，底色用 `agentColor(agent.id)` 的 12% 透明度 + 文字用同色（与消息流圆点同色，建立视觉关联）。

---

## 10. 实施阶段划分（严格按顺序执行）

> 每阶段结束执行 `cd client && npx tsc -b --noEmit && npm run build`，通过后才进入下一阶段。任何阶段失败，修复后重验，禁止带错进入下阶段。

### 阶段 A：设计 token 与基础设施（低风险）

1. `global.css` 按 §3 更新 token 值、新增字体/语义变量、新增 hljs 双主题、新增 `.badge` 体系、`button.danger/ghost`。
2. `MarkdownContent.tsx` 删除 github-dark 静态 import（§3.6）。
3. 新建 `icons.tsx`（§9.4）、`utils/time.ts`（§5.4.1）、`agentMeta.ts`（§5.4.1）。
4. 新建 `Toast.tsx` 并接入 `App.tsx`（R5）。
5. **验收**：双主题切换正常；代码块在浅色主题为浅色；控制台无 class 未定义告警；构建通过。

### 阶段 B：消息流管线（核心）

1. 新建 `chat/timeline.ts`（§5.2），并从 `ThreadDetailModal.tsx` 复制配对逻辑。
2. 新建 `chat/entries/` 七个 Entry 组件（§5.4）。
3. 新建 `chat/MessageFlow.tsx`（§5.3）。
4. 单元自验：在 `ThreadDetailModal` 中临时引入 `MessageFlow` 替换 `timeline` 渲染段（先不删旧代码，注释保留），对比渲染一致性。
5. **验收**：子会话弹窗内条目数量/顺序与改造前一致；tool 配对正确（call_key 相同的 result 挂到 call 上）。

### 阶段 C：主消息页重构

1. 新建 `PlanCard.tsx`（§5.4.4）、`ComposerBox.tsx`（§6）、`EmptyState`、骨架屏。
2. 重写 `ChatPanel.tsx`（§5.5、§5.6）：删状态栏/产物清单/MessageBubble，接入 MessageFlow + PlanCard + ComposerBox。
3. `Workspace.tsx` 顶栏运行控制区（§4.2）+ 回滚确认（R2/R3，新建 `ConfirmDialog.tsx`）。
4. store 新增字段：`composerFocusTick`（R6）、`approvalDecisions`（R7）、`wsConnected`（R10）。**只新增，不改已有 action**。
5. **验收**：发送需求 → PlanCard 出现在用户消息之后 → 确认拆解按钮在卡片内 → 执行中顶栏出现停止按钮 → 回滚有确认弹窗 → 错误请求出现 toast。

### 阶段 D：子会话抽屉化

1. 新建 `ThreadDrawer.tsx`（§7.1），全文搜索 `ThreadDetailModal` 引用点全部切换。
2. 删除 `ThreadDetailModal.tsx`。
3. `AgentHistoryModal` 适配（§7.3）。
4. 全文搜索 `ToolCallCard`：若无引用，删除该文件；有引用则迁移到 `ToolEntry` 后删除。
5. **验收**：任务行点击开抽屉；抽屉内渲染与主消息页同风格；多任务切换正常。

### 阶段 E：侧栏与其余视图

1. `Sidebar.tsx`：宽度/边框按 §4.1；新建 `NewSessionDialog`（R4）；「设置」label 修正（R14）。
2. `AgentPanel.tsx`：宽度 240px、成员卡追加当前任务行（R12）、头像色与消息流联动（§9.5）。
3. `TaskBoard/TeamPanel/KnowledgePanel` 卡片样式统一（R13，只改样式值）。
4. 布局铺平（§4.1 的 `.app-body`/`.sidebar`/`.workspace` 改动在此阶段做，避免影响前阶段调试）。
5. **验收**：整体三栏铺平、无双重卡片边框；新建群聊弹窗可命名选目录。

### 阶段 F：打磨

1. R8~R11 的引导/骨架/断连横幅/时间戳。
2. 全面 emoji 清查：`search_content` 搜 `📄|✏️|📁|⚡|🔀|🌐|🧪|📊|🧠|🔧|📦|💬|💭`，应零残留。
3. 暗色主题走查：所有新增组件在 dark 下对比度正常。
4. 构建 + Electron 实机启动（`npm run build` 后由用户验证，AI 不要自己启动 Electron 打包流程）。

---

## 11. 禁止事项汇总（执行 AI 红线）

1. 禁止修改 `server/`、`electron/`、`packages/` 下任何文件。
2. 禁止新增 npm 依赖（图标、动画、UI 库全部手写）。
3. 禁止删除 `store/chat.ts`、`api/client.ts`、`api/ws.ts` 中的已有导出与行为。
4. 禁止引入路由库重构视图切换（保持 `App.tsx` 的 NavKey 状态切换）。
5. 禁止改动 WebSocket 事件名、REST 路径、请求参数。
6. 禁止把内联 `<style>` 模式改成 CSS Modules / styled-components。
7. 禁止在组件里写死具体业务接口 URL（保持走 `api/client.ts`）。
8. 删除文件前必须全文搜索引用；删除 `MessageBubble`、`ToolCallCard`、`ThreadDetailModal` 前尤其如此。
9. 文案保持简体中文；状态枚举值（`in_progress` 等）不得中文化——它们是协议值，仅显示层做映射。
10. 不确定的接口字段先读 `api/client.ts` 与服务端 `server/` 的 schema 确认，禁止凭命名猜测。

---

## 12. 验收总清单（交付前逐项勾选）

- [ ] `npx tsc -b --noEmit` 零错误，`npm run build` 成功
- [ ] 浅色主题代码块为浅色高亮；深色主题正常
- [ ] 主消息页能看到 agent 的工具调用摘要行，可展开看参数/结果
- [ ] 任务拆解卡片位于消息流中，含确认按钮；完成后可折叠
- [ ] 状态栏已删除；运行控制（停止/回滚）在顶栏，回滚有二次确认
- [ ] 子会话为右侧抽屉，渲染风格与主消息页一致
- [ ] 全应用无 emoji 图标残留
- [ ] 新建群聊可输入名称与选择工作目录
- [ ] 请求失败有 toast 提示
- [ ] 上翻历史时新消息不打断滚动，出现「↓ 新消息」浮钮
- [ ] @成员选择可将 mentions 传给后端
- [ ] 暗色主题全页面走查无刺眼对比/不可读文本
