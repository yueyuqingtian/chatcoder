# 左右侧面板拖拽卡顿优化方案

> 目标：把「拖动左右分隔条时面板不跟手 / 掉帧 / 松手顿挫」从观感问题变成可度量、可回归的工程问题。
>
> 结论先行：**主线程已经没有 React 重渲染了，剩下的成本全在浏览器布局与绘制管线**。
> 现有实现（零 state 写入、rAF 合并、帧闸门、PerfBus 门控）已经把 JS 侧压到很低，
> 真正还没做的是 **CSS 侧的失效边界（containment）** 和 **拖拽期样式/命中的降级**。
> 这两块改动量很小、收益最大、风险可控，是本方案的重点（P0 / P1）。

---

## 一、现状盘点：已经做到位的部分

优化前先明确**不要重复投入**的地方。以下机制已在代码中落地，评审时应视为基线：

| 机制 | 位置 | 作用 |
|---|---|---|
| 拖拽中零 React 重渲染 | `components/ResizeHandle.tsx:177-182` | 直接写 `el.style.width/flexBasis`，不走 setState |
| rAF 合并每帧至多一次写入 | `ResizeHandle.tsx:198-205` | 高刷鼠标下省掉一半以上写入 |
| 手柄矩形缓存，避免强制同步布局 | `ResizeHandle.tsx:70-77, 263-265` | 去掉每次 mousemove 的 `getBoundingClientRect` |
| 三档帧闸门 realtime/balanced/frozen | `ResizeHandle.tsx:116-144` | 按实测帧间隔自适应降级 |
| PerfBus 统一运动期门控 | `perf/bus.ts` | 8~9 处重活统一由 `isBusy()` 暂停 |
| 唯一收敛序列（8 个收尾任务同帧串行） | `perf/reconcile.ts:63-106` | 消除「松手后逐帧排队归位」 |
| 虚拟列表 directDomUpdates + position 定位 | `MessageFlow.tsx:253-268` | 位置写 DOM，避免重叠帧 |
| 视口 rect 运动期节流 120ms 提交 | `MessageFlow.tsx:25-30, 278-328` | 避免虚拟器逐帧 setState → 整树重渲染 |
| 流式 flush 运动期暂停 + 结束追平 | `store/chat.ts:561-613, 670-674` | 流式内容不跟拖拽抢帧预算 |
| 右面板内容根冻结（Monaco/xterm） | `ResizeHandle.tsx:44-58`、`App.tsx:215` | 每帧不重排编辑器/终端 |
| 运动期暂停循环动画 | `styles/global.css:3485-3506` | 动画不参与合成竞争 |

**结论**：JS 侧（事件、state、I/O）已基本无优化空间，剩余瓶颈在样式重算、布局、绘制三段。

---

## 二、根因分析（结合代码）

### 根因 1：布局/绘制没有「失效边界」——全库零 `contain` / `content-visibility`

```
$ grep -rn "contain:\|content-visibility\|contain-intrinsic" client/src   →  0 命中
```

拖左分隔条时，每帧写一次宽度 → `.app-main` 内部整棵可见消息树（含 markdown、代码块 span、工具树、表格）重新折行。
没有 `contain: paint` 意味着**重绘范围也没有被裁剪**，浏览器只能保守地扩大失效区域。

- 证据：`App.tsx:195-197`（左分隔条未传 `freezeRefs`）、`MessageFlow.tsx:363-370`（注释明确「消息列不再冻结 → 文本逐帧折行」）
- 调研依据：web.dev《content-visibility》与 CSS Containment 指南指出，`contain: layout paint` 的价值在**交互期间**最大——它给浏览器一个明确的失效边界，使 layout/style/paint 只在该子树内重算；且它是渐进增强属性，**不需要 fallback**。
- 为什么现在最值得做：`.app-main` 已有 `container-type: inline-size`（`global.css:3553`），它隐含 `contain: layout style inline-size`——**layout 已经有了，只缺 paint**。

### 根因 2：`container-type: inline-size` 让每次宽度变化都触发容器查询评估

```css
/* global.css:3553 */
.app-main { container-type: inline-size; container-name: main-pane; }
/* 全部 3 处 @container 规则都依赖 .todo-card-active */
/* global.css:3569 / 4003 / 4006 */
@container main-pane (max-width: 960px) { .app-main.todo-card-active .mf-list, ... }
```

这三条规则**全部以 `.todo-card-active` 为前缀**。也就是说：**没有任务卡时，这些规则永远不会命中，但容器仍然存在**，容器尺寸每帧变化仍会让浏览器评估容器查询所覆盖的后代。
调研依据（moderncsstools / 容器查询指南）：容器尺寸变化会触发「所有带容器查询后代」的样式重算；实践建议是「只给真正需要尺寸感知子元素的元素设 `container-type`」。此处属于典型的多余声明。

### 根因 3：帧闸门的自动降级会把「掉帧」放大成「面板停住」

```ts
// ResizeHandle.tsx:117-144
const budget = Math.max(24, minDeltaRef.current * 1.6);   // 60Hz ≈ 26.7ms
if (overStreak >= 3) → balanced；again if (>= 5) → frozen（完全停止写宽度）
```

- 60Hz 下只要**连续 3 帧**超过 ~26.7ms 就降档；运行期（LLM 流式 + markdown 解析 + 中列折行并存）很容易触发。
- `frozen` 档是**完全停止写宽度**（`ResizeHandle.tsx:138`），面板会「卡住不动」。用户描述的「卡顿」很可能**有一部分正是这个安全阀本身**，而不是浏览器真的渲染不动。
- 调研依据：拖拽交互的性能实践普遍指出——**宁可降帧率也不能停止反馈**（停止反馈 = 交互断裂感），且降级判据应基于滚动窗口内的长帧比例，而非单帧尖峰（一次 GC / 一次 markdown 解析就会误判）。

### 根因 4：`.message-flow` 上的 `pointermove` 每帧读 `getBoundingClientRect`

```tsx
// MessageFlow.tsx:494-512
const onMove = (e: PointerEvent) => {
  const rect = el.getBoundingClientRect();     // ← 每次 pointermove 强制同步布局
  if (e.clientY < rect.top || e.clientY > rect.bottom) return;
  const nearRight = e.clientX >= rect.right - SCROLLBAR_BAND && ...;
  el.classList.toggle("is-scrollbar-hover", nearRight);
};
el.addEventListener("pointermove", onMove);
```

这是全库**唯一没有被 PerfBus 门控的逐帧布局读**。用途只是判断「指针是否落在右侧 12px 滚动条条带内」，却付出了「读取前先把脏布局全部算完」的代价——与拖拽每帧写入的脏布局叠加，等于每帧多付一次全量重排。

- 触发条件：用右手柄拖拽时，指针可能掠过消息面板；或窗口运动期间。
- 影响面有限但成本确定，属于「白送的优化」。

### 根因 5：拖拽期鼠标扫过内容触发大量 `:hover` 样式重算

消息项上分布着大量 hover 规则（`.turn-item-text:hover .msg-actions`、`.md-code-copy-btn:hover`、`.msg-action:hover`、`.todo-float-item:hover` 等，`global.css:597-635, 3725-3750`）。
拖拽时指针在面板上水平扫动，每进入一个新元素就会触发 style recalc + paint；这与折行重排抢同一份帧预算。

- 调研依据：accessibility/split-pane 实现指南中明确建议：拖拽期给面板加 `pointer-events: none`（原文是为了防止 iframe 抢事件，但同样消除了 hover 命中测试）。

### 根因 6：松手当帧串行做了太多事

```ts
// ResizeHandle.tsx:208-242
applyWidth(最终宽度)
unfreezeContents()          // 右面板内容根解冻 → Monaco/xterm 全量重排（最贵）
runReconcile()              // 8 个收尾任务同帧串行（含 xterm fit、虚拟器重测）
onCommit(finalWidth)        // → store.set + savePrefs(localStorage 同步写) + applyUiVars(全量写根 CSS 变量)
```

用户感受即「松手后顿一下」。其中 `localStorage` 同步写与 xterm 全缓冲 `fit()` 都不在视觉关键路径上，没必要挤在松手当帧。

### 根因 7（放大因素，非缺陷）：`.mf-list` 的 768px 上限造成「随动区 / 非随动区」两段式表现

```css
/* tokens.css:480 */ --content-max-w: 768px;
/* global.css:472 */ .mf-list { max-width: var(--content-max-w); margin: 0 auto; padding: 0 24px 4px; }
```

当 `.app-main` 宽度 > 768 + 64 ≈ 832px 时，消息列宽度**不变** → 拖拽几乎不卡；
一旦进入 < 832px，消息列宽度开始逐帧变化 → 立刻开始折行。
这解释了「空态首页不卡、某些窗口尺寸才卡」的复现差异，也说明**性能测试必须覆盖窄中列场景**（尤其是 `todo-card-active` 时宽度公式含 `calc(100% - 360px)`，随动区更宽，见 `global.css:3554-3566`）。

---

## 三、优化方案

### P0-1　给中列补上绘制失效边界（`contain: paint`）

**改哪里**：`client/src/styles/global.css`，`.app-main` 附近（第 3550 行区域）。

```css
/* 中列：拖拽期宽度逐帧变化，是唯一会逐帧重排的容器。
   layout 部分已由 container-type 提供，这里补 paint ——
   让折行后的重绘范围被裁剪在中间面板内，不向左右面板扩散。
   安全性：.app-main 已有 overflow: hidden（第 3550 行），
   paint containment 的裁剪语义与现状完全一致；
   且 .app-main 内无 position: fixed 后代（overlay/modal 都挂在 .app-shell 层）。 */
.app-main { contain: paint; }
```

**为什么有效**：把 layout + style + paint 三者都锁在 `.app-main` 内，拖拽每帧的重排/重绘不再向上/向兄弟面板传播。

**风险与规避**：
- paint containment 会让元素成为 `absolute` / `fixed` 后代的包含块，并裁剪溢出。
  - 已核实：`.todo-float { position:absolute }`（`global.css:1647-1648`）与 `.flow-scroll-bottom-btn { position:absolute }`（`global.css:4108-4109`）的**最近定位祖先是更内层的 `position:relative` 容器**（`.message-flow-wrap` / `.message-flow-outer`，`global.css:416-418`），不会被 `.app-main` 的 containment 改变。
  - `.app-main` 本身已有 `overflow: hidden`，不存在「依赖溢出到面板外」的元素。
- ⚠️ **不要**给 `.message-flow` 加 containment：`.flow-scroll-bottom-btn` 是它的直接子元素，若 `.message-flow` 变成包含块，该按钮会从「悬浮固定」变成「随内容滚动」。

**验证**：DevTools Performance 录制一次 3 秒拖拽，对比 `Recalculate Style` / `Layout` / `Paint` 三段时长。

---

### P0-2　把 `container-type` 收窄到「只有任务卡时才启用」

**改哪里**：`client/src/styles/global.css:3553`。

```css
/* 原来：无条件声明，导致每次拖拽跨断点都要重评估容器查询 */
/* .app-main { container-type: inline-size; container-name: main-pane; } */

/* 改为：仅在有任务卡（唯一使用 @container main-pane 的场景）时建立查询容器 */
.app-main.todo-card-active { container-type: inline-size; container-name: main-pane; }
```

**为什么有效**：全部 3 处 `@container main-pane` 规则都以 `.app-main.todo-card-active` 为前缀（`global.css:3554-3566, 3569-3575, 4003-4006`），无任务卡时它们本就不生效。收窄后，绝大多数拖拽场景**完全不产生容器查询评估**。

**风险**：极低。非 `todo-card-active` 时 `@container main-pane` 不再匹配 —— 与「匹配但不命中选择器」的结果一致。需回归一次「有任务卡 + 拖到窄于 960px」的场景，确认悬浮卡按预期隐藏（`global.css:3574`）。

**配套**：由于本项会一并去掉 `container-type` 隐含的 `layout` containment，P0-1 建议直接写成 `contain: layout paint` 以免回退：

```css
.app-main { contain: layout paint; }
.app-main.todo-card-active { container-type: inline-size; container-name: main-pane; }
```

---

### P0-3　消除 `.message-flow` 的逐帧 `getBoundingClientRect`

**改哪里**：`client/src/components/chat/MessageFlow.tsx:494-512`。

**方案 A（推荐，彻底去掉 rect 读取）**：用一个绝对定位的窄条元素做热区，靠 CSS `:has()` 驱动。

```tsx
// MessageFlowCore 返回结构内，紧跟滚动容器之后
<div className="mf-scrollbar-hotzone" aria-hidden="true" />
```

```css
/* 条带 = 6px 滚动条 + 6px 容差，与 JS 常量 SCROLLBAR_BAND 一致 */
.mf-scrollbar-hotzone { position: absolute; top: 0; bottom: 0; right: 0; width: 12px; z-index: 5; }
.message-flow-outer:has(.mf-scrollbar-hotzone:hover) .message-flow { /* 复用现有 .is-scrollbar-hover 的样式体 */ }
```

Electron 44（Chromium 130+）原生支持 `:has()`，无需 polyfill。

**方案 B（最小改动）**：保留 JS，但加三重收敛——`isBusy()` 时直接 return、rect 结果按「滚动容器尺寸签名」缓存、指针移动用 rAF 合并。

**收益**：去掉拖拽/窗口运动期间每帧一次强制同步布局。

---

### P1-1　把「冻结」改成「降频」——修复帧闸门的体感

**改哪里**：`ResizeHandle.tsx:116-144`。

```ts
// 1) frozen 不再是「停止写宽度」，改为「最低保证频率」（约 15fps，66ms 一拍）
//    保留跟手感，只牺牲平滑度 —— 交互断裂感远大于掉帧感。
if (dragModeRef.current === "frozen") {
  const now = performance.now();
  if (now - lastFrozenWriteRef.current < 66) return false;
  lastFrozenWriteRef.current = now;
  return true;
}

// 2) 降级判据从「单帧超预算 streak」改为「滚动窗口长帧比例」，
//    排除一次性 GC / markdown 解析尖峰的误判（最近 12 帧中长帧 > 50% 才降档）。
// 3) 补上升档：连续 N 帧合格后 balanced → realtime（当前实现只降不升，
//    一次误判会在整段拖拽中保持降级态）。
```

**为什么有效**：把用户感知的「卡住」转成「略不跟手」；同时避免一次瞬时尖峰毁掉整段拖拽的跟手性。

**验证**：拖拽时读 `document.documentElement.dataset.panelDragMode`（`ResizeHandle.tsx:93` 已在写），确认正常场景不再进入 `frozen`。

---

### P1-2　拖拽期关闭昂贵的文本 shaping 与 hover 命中

**改哪里**：`client/src/styles/global.css`，追加到 `body.panel-dragging` 规则区（第 3455-3506 行附近）。

```css
/* 折行成本 = 逐字 shaping + 断行计算。CJK 与代码混排时
   kerning / ligature 的收益在拖拽的 1~2 秒内可以忽略，关掉换帧率。 */
body.panel-dragging .turn-agent-text,
body.panel-dragging .md-body {
  text-rendering: optimizeSpeed;
  font-kerning: none;
  font-variant-ligatures: none;
}

/* 拖拽期不再做 hover 命中测试：指针扫过消息树时会连续触发
   .msg-actions / .md-code-copy-btn / .todo-float-item 等 hover 规则，
   每次都是一次 style recalc + paint，与折行重排抢同一份帧预算。
   顺带让根因 4 的 pointermove 在拖拽期自然失效。 */
body.panel-dragging .app-main { pointer-events: none; }
```

**风险**：`pointer-events: none` 期间该区域不可点击/不响应滚轮 —— 拖拽期间本就不需要（拖拽结束后 `body.panel-dragging` 立即移除，`ResizeHandle.tsx:227`）。
**注意**：若实测发现拖拽中仍需滚动，把该条限定为 `body.panel-dragging .app-main .mf-list`（不覆盖滚动容器本身）。

---

### P1-3　把非关键收尾挪出「松手当帧」

**改哪里**：`ResizeHandle.tsx:208-242`、`perf/reconcile.ts:88-106`。

| 任务 | 现状 | 建议 |
|---|---|---|
| `savePrefs()`（localStorage 同步写） | `onCommit` 内同步 | 结束后 debounce 200ms，或 `requestIdleCallback` |
| xterm `fit()`（`RECONCILE_ORDER.terminalFit = 50`） | 与其它 7 个任务同帧串行 | 拆到收敛序列的下一个 rAF（不在视觉关键路径） |
| 右面板内容根解冻 | 松手当帧（`unfreezeContents`） | 保持当帧（它决定视觉终态），但确认早于 `runReconcile` 的读任务 |

**为什么有效**：松手的第一帧只做「面板宽度落定 + 视觉归位」，把 I/O 与编辑器重排摊到后续帧。

---

### P1-4　补齐滚动路径上的布局读门控

**改哪里**：`MessageFlow.tsx:406-431`（`updateActiveEntry` 读 `scrollTop` / `clientHeight`）、`MessageFlow.tsx:589-640`（宽度锚点 effect）。

`updateActiveEntry` 经 `scheduleSpy` 做了 rAF 节流，但**没有 `isBusy()` 判断**。拖拽期若因内容折行产生 scroll 事件，仍会读布局。建议统一加：

```ts
const scheduleSpy = useCallback(() => {
  if (isBusy()) return;        // 运动期不读布局，结束由收敛序列补一次
  ...
}, [updateActiveEntry]);
```

---

### P2-1　虚拟项级别的失效边界（需逐项验证）

```css
.mf-entry { contain: layout paint; }
```

收益：每个虚拟项成为独立的重排/重绘边界。
**风险**：paint containment 会裁剪溢出到项外的内容（tooltip / 展开态浮层 / `translateY(-2px)` 的悬浮按钮），且在 `position: absolute` 的虚拟项上会改变绝对定位后代的包含块。
**做法**：先只加 `contain: layout`（不裁剪），用 DevTools 逐个确认工具卡、代码块复制按钮、消息操作行无位移；再逐步加 `paint`。

---

### P2-2　不推荐的项：给虚拟项加 `content-visibility: auto`

调研中该属性确实能跳过离屏渲染（web.dev 实测首屏 7x），**但与本项目的虚拟列表冲突**：

- `content-visibility: auto` 的离屏元素会进入 size containment，高度由 `contain-intrinsic-size` 提供 → **TanStack Virtual 的 `measureElement` 会测到估算值而非真实高度**，导致总高反复跳变；
- 官方明确警告：对被跳过的子树调用会强制渲染的 DOM API（`getBoundingClientRect` 等）会抵消收益，而虚拟列表**必然**要测量。

**替代**：真要减少 overscan 项的折行成本，正确做法是在拖拽期**下调 `overscan`**（`MessageFlow.tsx:252`，当前为 6），或对超出视口一定距离的项临时加 `.panel-dragging .mf-entry.is-far { visibility: hidden; }`。此项收益需要先测量 overscan 项的实际折行占比再决定。

---

### P2-3　Electron 渲染层（收益不确定，先测）

`electron/main.cjs:864-870` 的 `webPreferences` 只有 4 个键。可考虑：

- `backgroundThrottling: false`：避免窗口失焦再恢复后第一帧的节流抖动；
- 确认未使用任何 `--disable-gpu*` 类开关（当前未见 `commandLine` 调用）。

**注意**：不要引入 `--disable-frame-rate-limit` 之类会破坏正常帧节奏的开关。先测量再决定，不做无依据的调参。

---

## 四、验证与度量

### 4.1 现成工具（无需新建）

| 工具 | 用法 | 看什么 |
|---|---|---|
| `__chatcoderPerf` | `start()` → 拖 3~5 秒 → `report()` | 帧间隔 p95、longtask 数、收敛耗时（`perf/metrics.ts:151-176`） |
| `document.documentElement.dataset.panelDragMode` | 拖拽时观察 | 是否掉进 `balanced` / `frozen`（`ResizeHandle.tsx:261`） |
| DevTools Performance | 录制拖拽 | `Recalculate Style` / `Layout` / `Paint` 三段时长 |

### 4.2 场景矩阵（必须覆盖）

| 场景 | 中列是否折行 | 说明 |
|---|---|---|
| 空态首页 | 否 | 基线，应当始终 60fps |
| 40 条普通消息，中列宽 > 900px | 否（受 768px 上限保护） | 验证「非随动区」 |
| 40 条普通消息，中列压到 600px | **是** | 核心复现场景 |
| 220 条重内容（含代码块 + 表格） | **是** | 历史基准场景（`ResizeHandle.tsx:39` 提到的 A/B 脚本） |
| 运行中（LLM 流式输出） | **是** | 与流式渲染抢帧，最容易触发降级 |
| `todo-card-active` | **是**（随动区更宽） | 验证 P0-2 回归 |

左、右手柄都要测（右手柄有 `freezeRefs`，左手柄没有）。

### 4.3 达标线

- 帧间隔 p95 ≤ 20ms，max ≤ 50ms（拖拽期间）
- longtask 数 = 0
- 拖拽期间 `panelDragMode` 不进入 `frozen`
- 松手后「视觉归位」在 1 帧内完成

---

## 五、落地顺序建议

| 顺序 | 项 | 改动量 | 预期收益 | 风险 |
|---|---|---|---|---|
| 1 | P0-1 `contain: layout paint` | 1 行 CSS | 高 | 低（已核实定位祖先） |
| 2 | P0-2 收窄 `container-type` | 1 行 CSS | 中高 | 极低 |
| 3 | P0-3 去掉逐帧 rect 读取 | ~20 行 | 中 | 低 |
| 4 | P1-2 拖拽期 shaping / hover 降级 | ~10 行 CSS | 中高 | 低 |
| 5 | P1-1 帧闸门改「降频」+ 可升档 | ~20 行 | 高（体感） | 中（需调参） |
| 6 | P1-3 收尾任务分摊 | ~15 行 | 中 | 低 |
| 7 | P1-4 滚动路径门控 | ~5 行 | 低中 | 极低 |
| 8 | P2-* | — | 需先测量 | 中高 |

**建议做法**：1~4 一起上（都是低风险、可独立回退），**每项单独提交**以便用 `__chatcoderPerf` 做前后对比；5~7 在度量确认仍有长帧后再动。

---

## 附：调研来源

- web.dev —《content-visibility: the new CSS property that boosts your rendering performance》：containment 四类语义、`content-visibility: auto` 的 size containment 副作用与 `contain-intrinsic-size` 的必要性、对被跳过子树调用强制布局 API 会抵消收益。
- webperfclinic —《CSS Containment Guide》：`contain: layout paint` 是「失效边界」，**交互期间收益大于首屏**；无 fallback 需求；paint containment 会裁剪溢出并使元素成为 fixed 的包含块；跨树选择器（`.dark .card`）不受 containment 阻止。
- moderncsstools / 容器查询指南：容器尺寸变化触发带查询后代的样式重算；应只对真正需要尺寸感知子元素的元素设 `container-type`，优先 `inline-size` 而非 `size`。
- speedkit —《Field Testing CSS Containment》：真实站点 A/B 实测，`contain: content` + `content-visibility: auto` 对复杂 DOM 的渲染耗时改善显著。
- accessible split-pane 实现指南：拖拽期对面板加 `pointer-events: none`、`user-select: none` 的标准做法。
