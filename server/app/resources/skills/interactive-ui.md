# 交互界面设计（Interactive UI）

用于在消息流内直接生成、对比并呈现**可交互 HTML 组件、数据可视化图表、流程图与原型页面**。本能力**不往工作区写入任何磁盘文件**，在只读模式、计划模式和智能体模式下均可直接使用。

## 触发与选用策略

### 何时应该使用
- 用户要求**设计或对比多个版式**（如“设计 3 个卡片风格/布局方案给我选”）：用 `present_ui` 传入 `variants[]`，让用户在消息流直接体验、勾选并继续。
- 用户需要**交互式组件、动态模拟器、可筛选表格、自定义图表**（如“做个并发与延迟交互曲线”、“画个可切换维度的看板”）：用 `present_ui` 生成自包含交互界面。
- 用户需要**架构图、流程图、时序图、状态机**：优先在正文中直接输出 ````mermaid` 围栏代码块（系统会自动渲染为可缩放/导出 SVG 的交互流程图）；如需带点击回调或自定义交互节点，改用 `present_ui`。
- 若模型通道无法调用 `present_ui` 或只需在正文中附带轻量单页演示，可使用 ````html-preview`（或 ````interactive-html`）围栏代码块，消息流会在闭合后自动转换为沙箱交互预览。

### 何时不该使用
- 纯代码讲解、命令行排错、普通问答、或仅需展示一小段静态表格/Markdown 说明时，不要强行封装成交互卡片。

## `present_ui` 参数规范

1. **多版式比选**：
   - `kind`: `"comparison"`，`layout`: 信息较少/窄卡片用 `"grid"`，结构较复杂用 `"tabs"`。
   - `variants`: 提供 2~4 个差异明显的设计方案，每项包含 `id`、`label`（简短清晰，如 `A · 极简标准`）、`summary`（一句话说明亮点与适用场景）以及 `html`。
   - 默认单选（`selection: { enabled: true, mode: "single" }`）；若允许组合多个模块特性，设 `mode: "multiple"`。
2. **单界面 / 交互图表**：
   - `kind`: `"chart" | "diagram" | "component" | "page"`，可在顶层直接传 `html`（或传 1 项 `variants`）。
   - 如果页面里有「把当前参数/点击节点回传给 AI」的按钮，调用宿主注入的桥接方法：
     `window.chatcoder.emit("select", { summary: "...", ...data })`

## 沙箱界面编写约束（必须遵守）

1. **完全离线自包含**：沙箱启用严格 CSP（`connect-src 'none'`），**禁止**任何外网 `<script src>` / `<link href>` / `fetch()` / CDN（如 cdn.jsdelivr.net）。样式与脚本全部内联写在 `<style>` 与 `<script>` 中，图表使用原生 `<svg>` 或 `<canvas>` 绘制。
2. **使用宿主主题语义变量**（自动跟随浅/深色切换）：
   - 背景：`var(--bg-card)`、`var(--bg-panel)`、`var(--surface)`、`var(--bg-hover)`
   - 文字：`var(--text-1)`、`var(--text-2)`、`var(--text-3)`
   - 边框与点缀：`var(--border)`、`var(--border-hover)`、`var(--color-brand)`、`var(--primary)` / `var(--primary-fg)`、`var(--success)`、`var(--warning)`、`var(--error)`
   - 圆角：控件 `8px`（`var(--r-md)`）、卡片 `12px`（`var(--r-lg)`），**禁止**左侧彩色装饰竖线。
3. **Canvas 主题重绘**：若页面使用 `<canvas>`，监听 `window.addEventListener("chatcoder:themechange", draw)` 在主题切换时读取 `getComputedStyle(document.documentElement)` 重绘。
4. **禁止视口自锁高度**：外层容器高度由内部内容自适应决定（上限约 500px），**严禁**在顶层容器写 `100vh`、`min-height: 100vh`、`h-screen` 或 `height: 100%`，否则容器会塌缩。
5. **与微应用（`microapp-create`）的分工**：
   - 消息流即时展示、多方案比选、图表与一次性原型 → 用本技能（`present_ui`，零写盘、跨模式通用）；
   - 需要长期保存到个人应用库、挂到右侧面板且持续读写项目数据 → 用 `microapp-create`。
