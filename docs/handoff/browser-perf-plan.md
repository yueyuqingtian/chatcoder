<!-- plan-status: proposed @ 2026-10-15（调查与基线测量完成，代码尚未改动；用户已要求按三项问题做优化，换电脑继续，开工前按 §七 确认取舍即可） -->

# chatcoder 计划：内置浏览器操作提速与点击准确性（plan-20261015-browser-perf）

> 来源：用户实测 0.8.2 内置浏览器 AI 操作后的三项反馈（功能可用，体验有缺陷）。
> 现状证据、基线数据与复测方法见交接文档 [browser-perf-handoff.md](browser-perf-handoff.md)（§2 有链路速览与关键代码位置）；
> 原有架构的计划 `ai/chatcoder-plan-20261015-browser-agent.md`、`ai/chatcoder-plan-20261015-082-polish.md` 只在原电脑本地（`ai/` 不入库），要点已并入交接文档 §2。
> 代码注释引用本计划编号 `plan-20261015-browser-perf`；开工时把本文件复制为 `ai/chatcoder-plan-20261015-browser-perf.md`。

## 一、目标

1. **速度**：降低每次浏览器动作的延迟；窗口在后台（被遮挡 / 最小化）时不再变慢、截图不再超时；减少模型为「看结果」额外发起的往返。
2. **截图及时**：点击后出现的报错提示（哪怕一闪而过）、接口错误、控制台报错，随该次动作的结果直接回报，必要时在动作完成的同一时刻附截图。
3. **准确**：点击落在正确的元素上；无法保证准确时宁可报错说明，也不静默点错。

## 二、现状与根因（证据见交接文档 §3、§4）

### 2.1 慢

| 编号 | 根因 | 位置 |
|---|---|---|
| R1 | **宿主渲染进程的定时器被后台节流**：主窗口没关 `backgroundThrottling`，Windows 原生遮挡检测把被遮住的窗口判为 hidden，host.ts 里所有 `sleep()` / 轮询（`window.setTimeout`）被对齐到约 1 秒。实测窗口被遮挡时点击 ≈2000 ms、输入/按键 ≈1000 ms、滚动 ≈2000 ms，而纯页面工具包调用只要 4–6 ms | `client/src/browserAgent/host.ts` `sleep` / `settleAfter` / `waitCondition` / `navigateTab`；`frames.ts` `waitNavigationSettled`；`electron/main.cjs` 主窗口 webPreferences |
| R2 | 动作后「稳定」判定串行固定等待：先 `waitNavigationSettled`（无跳转也要等满 120 ms），再轮询 DOM 静默 120 ms；页面有持续 DOM 变化（计时器、JS 动画）时一路等到预算上限（点击 1500 ms） | `host.ts` `settleAfter`、`frames.ts` `waitNavigationSettled`、`kit.py` `state.idle_ms` |
| R3 | 可视化与准备步骤串行：`aiWindow` IPC → `watch` → 礼让 → 光标 `sleep(150)` → 派发 → `sleep(30)`，可见时每次点击固定多 180 ms | `host.ts` mouse 分支 |
| R4 | 导航等完整 `load`（图片、字体、第三方资源全部加载完），`wait_until` 参数在桌面端被忽略；之后再单独发一次 `kit text` 取摘要 | `host.ts` `navigateTab`、`driver.py` `navigate` |
| R5 | 往返次数多：动作结果不含新弹出的菜单/对话框元素、页面提示、接口错误，模型只能再调 snapshot / screenshot / logs | `driver.py` `_settle_lines` |
| R6 | 截图用 `capturePage(stayHidden)`：宿主窗口被遮挡/最小化时合成器不出帧，截图卡满 15 秒后超时失败 | `electron/browser-agent.cjs` `capture` |

### 2.2 截图滞后（报错一闪而过）

| 编号 | 根因 | 位置 |
|---|---|---|
| R7 | 稳定判定不感知网络：点击触发的请求还没返回，settle 已按「DOM 静默」结束，结果写「页面没有可见变化」；报错提示在请求返回后才出现，模型下一轮再截图时提示已消失 | `host.ts` `settleAfter`、`browser-agent.cjs` `onCdp`（只记请求，不记完成时刻） |
| R8 | 结果文案不含页面提示（toast / 表单校验错误）、动作期间的接口错误（HTTP ≥400、请求失败、200 但业务码报错）与控制台报错 | `driver.py` `_settle_lines`、`kit.py` MutationObserver 只计数 |
| R9 | 截图只能单独调用，必然落在下一次模型往返（秒级）之后 | `driver.py` / `tools/browser.py` |

### 2.3 误点击

| 编号 | 根因 | 位置 |
|---|---|---|
| R10 | **未换算网页缩放**：`sendInputEvent` 坐标是 DIP，页面坐标是 CSS 像素，缩放≠100% 时点击整体偏移；结果仍报「已点击」。实测 125% 缩放点「远处按钮」命中 0 次 | `host.ts` `dispatchMouse` |
| R11 | 定位（resolve）与点击分两次往返，中间无稳定性检查：下拉/弹窗展开动画（CSS transition 不产生 DOM 变化，settle 早已结束）期间算出的坐标，派发时元素已移动 | `driver.py` `click` → `host.ts` mouse 分支 |
| R12 | 旧 ref 指向「就地重渲染」后的另一行：ref 绑定 DOM 节点，表格/虚拟列表翻页复用节点只换文字，旧 ref 仍然有效但内容已变。实测点「张三」行的旧 ref 删掉了「李四」 | `kit.py` `refOf` / `byRef` |
| R13 | `text` 定位有多个同分候选时静默取第一个（实测「删除」直接删了第一行）；`selector` 多匹配同样取第一个且不提示 | `kit.py` `findByText` / `pick` / `queryAny` |
| R14 | 截图坐标不校验截图后的变化：页面滚动、视口尺寸变化（面板拖动/展开）、跳转后仍按旧比例换算；整页/元素截图后仍沿用上一次视口截图的比例 | `driver.py` `_to_css` / `screenshot` |
| R15 | 坐标点在小按钮边缘外几像素时直接点在容器上，不吸附、不提示 | `kit.py` `resolve`（坐标模式） |
| R16 | 命中检测把「命中元素是目标的祖先」也算命中，包括 body / html（目标 `pointer-events:none` 等情况会点空却报成功） | `kit.py` `resolve` |
| R17 | （待确认）面板从未显示时，停泊的 webview 可能还没被赋尺寸（1×1 视口），所有点击报「不在可视区域内且无法滚入」、截图超时。首轮基线出现过，第二轮未复现 | `client/src/components/panel/BrowserPanel.tsx` `syncFrameSize`（`viewportRef` 为空时直接 return） |

## 三、方案

### 3.1 速度

1. **宿主等待不受节流（R1）**：browser-agent.cjs 的 `exec` 在查 guest 之前处理新 op `sleep`（主进程 `setTimeout`，上限 2 s）；host.ts 的 `sleep()`、frames.ts 的轮询改走它（无 API 时退回 `window.setTimeout`）。IPC 回包是消息任务，不受 hidden 页面的定时器节流。`withTimeout` 的超时计时同样改造。用 `probe_host.py` 在遮挡/最小化下复测。
2. **截图兜底（R6）**：宿主 `document.visibilityState === "hidden"` 时不带 `stayHidden` 调 `capturePage`（让 guest 临时出帧），或改走 CDP `Page.captureScreenshot`；先实测两条路在「遮挡」「最小化」下哪条可用，并把超时从 15 s 降到 3–5 s、失败时给出可读原因。是否额外加 `--disable-features=CalculateNativeWinOcclusion` 见 §七。
3. **稳定判定重写（R2/R7）**，宿主侧单循环（约 30 ms 一轮，每轮一次 `kit state` + 一次主进程 `netSince`）：
   - 导航检测与 DOM 静默并行，不再先等 120 ms；
   - **网络感知**：只统计动作起点之后发起的 XHR / Fetch（主进程 CDP 记 `startedAt`、`loadingFinished` / `loadingFailed`），在途数为 0 才算稳定；
   - **噪声过滤**：MutationObserver 常驻，按目标节点记录近 2 秒变化频次，高频变化的节点（时钟、JS 动画）不刷新「最近变化时间」；
   - 最短观察 60 ms、静默阈值 100 ms；预算：点击/提交 3000 ms、按键 2000 ms、悬停 400 ms、滚轮 250 ms、纯输入 300 ms；到预算仍有在途请求时在结果里说明。
4. **准备步骤并行（R3）**：`aiWindow` 与 `watch` 并行；光标动画与点击前校准（见 3.3-2）重叠，光标等待 150 → 约 100 ms；光标过渡改用 `--dur-2`。`finally` 里的 `sleep(30)` 改为焦点事件驱动的即时还原。
5. **导航（R4）**：等 `dom-ready`（frames.ts 记录每次导航的 DOM 就绪序号）+ 网络感知稳定判定即返回；显式 `wait_until=load` 才等完整加载。摘要与「视口内可交互元素」合并为一次 kit 调用。
6. **结果增强、少往返（R5）**：动作结果附带「新出现的可操作元素」（新增子树、新显示的绝对/固定定位浮层、新打开的对话框内的元素，带 ref，上限 20 项 / 1200 字）；`navigate` 附带视口内可交互元素（`snapshot` 的 `viewport_only`，上限 1500 字）。提示词同步说明「结果已含新元素，无需每步重新 snapshot」。

### 3.2 截图及时性

1. **提示捕获（R8）**：kit 的 MutationObserver 在节点插入的当下记录提示类节点的文字（之后被移除也保留，标记「已消失」）：`role=alert/alertdialog/status`、`aria-live`、类名含 toast / snackbar / notification / notify / notice / message / msg / alert 且自身或 3 层内祖先为 fixed/absolute 定位，以及 error / invalid / explain 类的表单校验文字；含 `data-sonner-toast` 等属性。也覆盖「常驻容器改 class/style 后显示」的情况。按类名与文字判严重级别（错误 / 警告 / 成功 / 提示）。回调里只做字符串判断和 textContent 快照，定位/可见性判断放到动作结束时。
2. **接口与控制台错误（R8）**：动作窗口内 HTTP ≥400 / 请求失败的 XHR、Fetch，附响应体摘要（≤300 字，最多 2 条，CDP `Network.getResponseBody`）与 `request_id`；JSON 响应里 `success === false` 或 `code` 不在 0/200 且带 msg/message 的视为业务错误；宿主 `rec.console` 中动作起点之后的 error 级消息（最多 3 条）。
3. **自动截图（R9）**：检测到错误级提示 / 接口错误 / 新对话框时，在同一次宿主调用里、稳定判定结束的当下截图并附在结果中（注意 agent_loop 只在 `result.ok` 时把图片送进多模态对话，失败结果不附图）。点击、输入、按键、批量新增 `screenshot: true` 参数：动作完成即截，免去一次往返。

### 3.3 准确性

1. **缩放换算（R10）**：派发坐标 = CSS 像素 × `webview.getZoomFactor()`（拖拽、滚轮、悬停同样处理）；Playwright 宿主无需换算。
2. **点击前最终校准（R11）**：mouse 原语带上 `ref` 时，宿主在派发前调用新 kit 函数 `aim`：等元素矩形稳定（连续两帧不变，上限约 500 ms，rAF 不触发时退回定时采样）、重新算命中点并复核命中元素，用最终坐标派发；目标此刻被遮挡/消失则报错不点。两种宿主都实现。
3. **旧 ref 内容校验（R12）**：ref 分配时记录「所在行/列表项」的上下文文字（`tr` / `li` / `role=row|listitem|option|treeitem`，或「父元素下有 ≥2 个同 tag+class 兄弟」的最近祖先）；resolve 时上下文明显变化即报错「ref_x 所在行已从「张三…」变为「李四…」，请重新 browser_snapshot」。
4. **歧义定位（R13）**：`text` 多个同分候选（去掉嵌套重复，优先打开的对话框内）时报错并列出前 8 个候选（ref + 行上下文）；`selector` 多匹配时在结果里说明「匹配 N 个，已用第 1 个」。
5. **截图坐标守卫（R14）**：截图时记录视口尺寸、滚动位置、URL 与截图类型。坐标动作时：页面滚动则按滚动差补偿；视口尺寸或 URL 变化则拒绝并提示重新截图；整页截图坐标按文档坐标换算并滚入视口；元素截图坐标拒绝。
6. **就近吸附（R15）**：坐标命中的不是可交互元素（且不在可交互祖先内、不是画布/编辑区/跨域框架）时，10 px 内唯一或明显最近的可交互元素作为目标，结果写明「已对准 按钮「保存」」。
7. **命中判定收紧（R16）**：祖先命中只在非 body/html 且为可交互祖先时接受，否则视为未命中。另：`resolve` / `activate` 的 `scrollIntoView` 横向由 `inline:'center'` 改为 `'nearest'`——窄视口（面板 418 px 宽）下现在只要纵向不在视口内就会把整页横向挪开（E2E 实测按钮 left 变成 −264），白白让上一张截图的坐标失效。
8. **Set-of-Mark 截图**：`browser_screenshot` 新增 `marks: true`：kit `marks` 返回视口内可交互元素的 ref 与矩形，渲染进程用 OffscreenCanvas 在截图上画编号框（不改页面 DOM；Playwright 宿主用 PIL 画），结果列出「编号 → 元素」，模型按 ref 点击。
9. **停泊尺寸兜底（R17，确认后再做）**：`syncFrameSize` 在 `viewportRef` 为空时仍按估算尺寸给 webview 赋值。

### 3.4 原语与协议变更（两种宿主必须同步，driver.py 顶部原语表一并更新）

| 原语 | 变更 | 桌面端 | Playwright 兜底 |
|---|---|---|---|
| `sleep`（主进程，新） | 不受节流的等待 | browser-agent.cjs `exec` | 不需要 |
| `aiWindow` | 返回 `net_seq`（动作起点） | browser-agent.cjs | `_before_input` 记起点 |
| `netSince`（主进程，新） | 起点后的在途 XHR/Fetch 数、错误请求与响应体摘要 | browser-agent.cjs（`onCdp` 增加 `loadingFinished`、`startedAt`） | `request` / `requestfinished` / `requestfailed` 事件 |
| `mouse` / `keys` / `text` | 新稳定判定；`settle` 增加 `notices` / `added` / `net_errors` / `console_errors`，可带 `shot`；mouse 增加 `aim` 校准与缩放换算 | host.ts | playwright_host.py |
| `capture` | 隐藏时兜底出帧；返回 `scroll`、`viewport`；支持 `marks` | host.ts + browser-agent.cjs | playwright_host.py |
| `nav` | DOM 就绪 + 稳定判定 | host.ts + frames.ts | `goto(wait_until=domcontentloaded)` + 稳定判定 |
| kit（`KIT_VERSION` 5 → 6） | `aim` / `changes` / `marks`、噪声过滤 idle、ref 上下文指纹、歧义报错、吸附、滚动补偿 | 共用 kit.py | 共用 kit.py |

宿主协议版本 `PROTOCOL_VERSION`（bridge.py 与 host.ts）：新 op 只增不改时可保持 1；若改变既有 op 的返回语义，两边同步升到 2。

## 四、分阶段计划

1. **阶段 1 · 后台不变慢（R1、R6）**：主进程 `sleep`、host.ts / frames.ts 等待改造、截图兜底。验证：E2E 测试窗口分别在「可见」「被遮挡」「最小化」下跑 `probe_host.py` 与 `bench.py`，三种状态延迟同量级、截图成功。
2. **阶段 2 · 稳定判定与错误回报（R2、R7、R8、R9）**：网络感知 + 噪声过滤的 settle、提示捕获、接口/控制台错误、自动截图与 `screenshot` 参数。验证：lab 页「保存（500 + 1.2 s 提示）」「提交订单（200 业务错误）」「慢接口（450 ms）」三个场景结果均含错误/变化，失败场景附图。
3. **阶段 3 · 点击准确性（R10–R16）**：缩放换算、`aim` 校准、ref 上下文校验、歧义报错、截图坐标守卫、吸附、命中收紧（kit v6）。验证：lab 页动画菜单、旧 ref、歧义文字、坐标偏出 5 px、截图后滚动、125% 缩放全部符合验收。
4. **阶段 4 · 少往返（R3、R4、R5）**：准备步骤并行与光标等待、导航 DOM 就绪、新元素回报、导航附视口元素、`marks` 截图、提示词更新。
5. **阶段 5 · 收尾**：单测与真实 Chromium 测试补齐；`client` typecheck + build、`node --check`；E2E 复测并与基线对比写入交接文档；CHANGELOG 0.8.2 区块；R17 复现确认后处理或记录。

## 五、涉及文件

- 修改：`client/src/browserAgent/host.ts`、`client/src/browserAgent/frames.ts`、`client/src/components/panel/BrowserPanel.tsx`（R17）、`client/src/styles/browser.css`（光标过渡）、`electron/browser-agent.cjs`、`electron/main.cjs`（仅当 §七 决定改主窗口节流/遮挡策略）、`server/app/orchestration/browser/kit.py`、`server/app/orchestration/browser/driver.py`、`server/app/orchestration/browser/playwright_host.py`、`server/app/orchestration/tools/browser.py`（`screenshot` / `marks` 参数与描述）、`server/app/orchestration/prompts/main.py`（`BROWSER_TASK_PROMPT`）、`server/tests/test_browser_agent.py`、`server/tests/test_browser_kit_frames.py`、`CHANGELOG.md`。
- 新增：`server/tests/test_browser_kit_accuracy.py`（真实 Chromium：动画稳定、提示捕获、旧 ref、歧义、吸附、滚动补偿、marks、噪声过滤）。
- 已新增（本次交接）：本文件、交接文档、`docs/handoff/browser-perf-e2e/`（E2E 基准脚手架与基线数据）。

## 六、验收标准

1. 宿主窗口可见时，无异步副作用的点击 / 悬停 / 按键在 150 ms 内返回（现状理论值约 300–450 ms）；窗口被遮挡或最小化时延迟与可见时同量级（现状量化到 1–2 s），截图成功或在 5 s 内给出明确失败原因（现状卡 15 s）。
2. 点击触发请求时，结果等到请求返回（上限 3 s）再报告页面变化；请求失败时结果含 HTTP 状态与错误信息；显示 ≥300 ms 的报错提示即使已消失也带文字回报；错误场景自动附截图。
3. 网页缩放 125% 时，按 ref / selector / 坐标点击均命中目标。
4. 打开带 500 ms 展开动画的下拉菜单后立即点选项，10 次全部选中正确项。
5. 列表就地重渲染后使用旧 ref：报错要求重新读取，不发生点错行。
6. `text` 有歧义时报错并列出候选；唯一匹配照常点击。
7. 截图后页面滚动再按截图坐标点击：点中截图里的那个元素；视口尺寸变化或页面跳转后按旧坐标点击：拒绝并提示重新截图。
8. 坐标落在可交互元素外 ≤10 px：吸附并在结果中说明；空白处（附近无可交互元素）照常点击。
9. `marks: true` 截图带编号框，结果列出编号与元素，按这些 ref 点击可用。
10. 两种宿主对同一 kit 测试结果一致；`server` 相关 pytest、`client` typecheck 与 build、`node --check electron/main.cjs electron/browser-agent.cjs electron/preload.cjs` 全部通过。
11. （R17 确认存在时）面板从未展开过，AI 首次打开网页后的点击与截图即可成功。

## 七、风险与取舍（开工前请用户拍板）

1. **主窗口后台策略**：方案默认只把「浏览器宿主的等待」挪到主进程计时，不动主窗口节流；若截图兜底在「被遮挡」下仍不可靠，可选 `app.commandLine.appendSwitch("disable-features", "CalculateNativeWinOcclusion")`（被遮挡时继续渲染，代价是被遮挡时多耗 CPU；最小化时仍按隐藏处理）。
2. **自动截图的 token 成本**：只在错误/对话框时自动附图，其余靠模型显式 `screenshot: true`；如用户希望更省，可只回报文字。
3. **歧义与旧 ref 报错会多一次往返**：换来不点错；如用户更在意速度，可改为「点击但在结果里显著提示」。
4. **网络感知的上限预算**：长轮询或持续请求的页面会等满 3 s，结果里会说明「仍有 N 个请求未完成」。
5. **提示捕获的误报**：聊天类页面的新消息可能被识别为提示；用定位方式与长度限制压低误报，最多回报 3 条。
