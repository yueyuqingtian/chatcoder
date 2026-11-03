# 图标映射表（plan-630-2590）

本项目图标已统一为 Ant Design 图标体系，数据源 `@ant-design/icons-svg`，由
`components/antd-icons/AntIcon.tsx` 渲染，经 `components/icons.tsx` 对外暴露。
**业务代码只从 `components/icons` 引入**（由 `npm run lint:design` 强制）。

本文件记录「为什么这个旧图标对应这个 antd 图标」，便于日后审阅与替换。

## 一、重要约定

1. **旧导出名与 props 签名永不改变**：`IconXxx` 名字、`{ size, color, strokeWidth, className }`
   全部保留，`strokeWidth` 收下但不参与渲染（antd 图标线宽写死在路径里）。
2. **默认 `size` 逐个保留**：13/14/16/18px 等原默认值必须一致，否则界面尺寸整体偏移。
3. **CSS 类名显式传入**：`ic-<name>` 由桥接层写死，不从 `glyph.name` 推导。
   例：`IconRefresh` 的 CSS 类名必须是 `ic-refresh`（`.ic-refresh:hover{rotate:180deg}`），
   而 antd 的 `ReloadOutlined.name === "reload"`，推导会得到错误的类名、让动效静默失效。
4. **不要给 antd 图标补内部部件 class**：`.ic-fill` / `.ic-ac` / `.ic-hand` 依赖手绘 SVG 的
   内部路径节点，antd 图标里没有也不应伪造。

## 二、低置信映射（语义近似，日后若要调整优先看这张表）

| 旧图标 | 现用 antd | 为什么不是更贴近的名字 |
| --- | --- | --- |
| `IconTerminal` / `IconSquareTerminal` | `CodeOutlined` | antd 没有终端图形，代码符号最接近 |
| `IconCpu` / `IconGitGraph` | `DeploymentUnitOutlined` | antd 无 CPU / 分支图，用节点拓扑表达「算力 / 图」 |
| `IconBox` / `IconPackage` | `CodeSandboxOutlined` | antd 无「工具包」；沙箱盒子语义最近 |
| `IconLayers` / `IconHash` | `BlockOutlined` | antd 无层叠图形 |
| `IconLayoutGrid` / `IconBlocks` / `IconBoard` / `IconAppWindow` | `AppstoreOutlined` | antd 只有应用宫格 |
| `IconStop` | `BorderOutlined` | ⚠ 不能用 `StopOutlined`，那是「禁止」符号；实心方块用 Border 描边表达 |
| `IconPlay` | `CaretRightOutlined` | antd 的 `PlayCircle` 带圆环，行内偏重 |
| `IconSparkles` | `HighlightOutlined` | antd 无星芒；`StarOutlined` 是收藏语义，易误解 |
| `IconWand` | `BulbOutlined` | antd 无魔杖；`MagicOutlined` 在该版本包内不存在（已实测） |
| `IconBrain` / `IconMemory` | `BulbOutlined` | antd 无大脑图形 |
| `IconAnchor` | `LinkOutlined` | antd 无锚点图形 |
| `IconAt` | `MailOutlined` | antd 无 @ 图形 |
| `IconSliders` | `ControlOutlined` | antd 无推杆图形 |
| `IconPalette` / `IconMultimodal` | `BgColorsOutlined` | antd 无调色盘 |
| `IconPet` | `TagOutlined` | antd 无爪印；徽标语义 |
| `IconTelescope` | `ReadOutlined` | antd 无望远镜 |
| `IconFolderTree` | `ApartmentOutlined` | antd 无目录树；组织架构最接近 |
| `IconGitCommit` | `NodeIndexOutlined` | antd 无提交节点图形 |
| `IconFileRead` / `IconFileWrite` / `IconFilePatch` | `FileTextOutlined` | antd 的文件动作图形只有通用文本 |
| `IconOperationCluster` | `ClusterOutlined` | 语义直接对应 |
| `IconClipboard`（已移除） | 改用 `FileTextOutlined` | ⚠ `SnippetsOutlined` 的图形是「两块重叠的纸」，即复制图标；计划卡左边用它会被读成「复制」，还与右侧复制按钮撞图。antd 无剪贴板图形，而两处调用（计划卡、Git 站会摘要）语义都是「文档 / 摘要」，故统一用文档图形并删掉这个导出 |

## 三、保留自绘（不要替换）

| 图标 | 保留原因 |
| --- | --- |
| `IconRingProgress` | 按 `value` 运行时算 `strokeDasharray` 画进度弧，是数据可视件 |
| `IconBrandExplorer` / `IconBrandVSCode` / `IconBrandIdea` / `IconBrandWindowsTerminal` | 品牌标识，antd 无对应，替换丢识别度 |
| `IconPanelLeft` / `IconPanelRight` | 「左右面板开合」语义，antd 的 `LayoutOutlined` 是通用布局，用户更难分辨 |
| `IconToneBadge` | 实心圆底 + 反白符号的语气徽标，是容器不是单图标 |
| `iconkit/FileIcon` | Material 主题多色文件类型字典（400+ 后缀），antd 只有单色 `FileOutlined` |
| `iconkit/StatusIcon` / `WaitingIcon` | 任务状态机表现层（8 态 + 旋转 + 描边绘入 + 严格尺寸契约） |
| `iconkit/ToolGlyph` | 20 类工具语义 + 运行态循环动效（扫描/书写/光标/轨道/气泡），antd 无对应 |
| `iconkit/SettingsGlyph` | 彩色磁贴 + 白线稿 + 专属微动三位一体 |
| `iconkit/MorphIcons` | 弹簧形变动画 + 双 SVG 交叉切换，是交互组件 |
| `iconkit/ToneTile` | 图标容器（灰化→彩色过渡） |

### 已从「保留自绘」改为 antd 的项（用户明确要求）

| 图标 | 现用 antd | 说明 |
| --- | --- | --- |
| `IconChevronRight` / `IconChevronDown` / `IconChevronLeft` / `IconChevronUp` | — | 恢复为原 24 网格细线自绘。这是全应用最高频的层级操作符号，antd 箭头更粗更短，与周边细线图标同屏不协调；且形态本身是操作肌肉记忆 |
| `IconChevron` / `IconArrowToggle` | — | 同上，保留自绘 + 外层 `transform` 旋转过渡 |
| `IconArchive` | — | 恢复自绘「封箱」造型（横梁盖子 + 箱体 + 抽手）。antd 的 `InboxOutlined` 是「收件箱」开口托盘，语义不符 |
| `iconkit/ModeGlyphs` 的 `MgAgent` / `MgWorkflow` / `MgPlan` / `MgReadonly` / `MgGoal` | `RobotOutlined` / `PartitionOutlined` / `ProfileOutlined` / `EyeOutlined` / `AimOutlined` | 执行模式；改为 antd 后不再带 `lx` 类名，见下节「颜色割裂」 |
| `iconkit/ModeGlyphs` 的 `MgAsk` / `MgAuto` / `MgFull` | `QuestionCircleOutlined` / `ThunderboltOutlined` / `UnlockOutlined` | 权限模式，同上 |

### 颜色割裂（已修）

`styles/icons.css` 曾有一条祖先态规则：
`:is(... , [data-state="open"]) svg.lx:not(.ic-duo) { color: var(--ic-accent) }`。
浮层根节点带 `data-state="open"`，于是**只要菜单打开，里面所有 `svg.lx` 就被染成主题强调色**
（暖色主题下偏红偏橙），而同一菜单里的 antd 图标只继承 `--text-2` 的灰 —— 这就是
「一部分红、一部分黑、切换主题后颜色割裂」的根因。

处理：模式图标改用 antd（不再有 `svg.lx`），并整组删除 `svg.lx` / `.lx-*` 规则
（已无任何生成点）。**注意 `@keyframes lx-blink` 必须保留** —— `styles/flow.css` 里
工具运行态 `[data-tool-state="running"] svg.tg .tg-blink` 仍在复用它。

## 四、光学尺寸归一（plan-630-2590）

antd 图标虽同处 1024 网格，但**实际绘制跨度差异极大**（实测 102 个图标落在 53.5%~102.7%）。
原生存在两套设计尺寸：89.3%（多数）与 100%（圆形，靠画大补偿观感面积）；而箭头、加号、
更多、关闭这类符号只画到 75%~85%，与满格图标并排时明显偏小。

`components/antd-icons/opticalScale.ts` 按「目标跨度 89.3% / 自身跨度」为每个图标算缩放，
两端收敛（上限 1.20、下限 0.95、|缩放-1|<0.015 视为无需调整），在 `AntIcon` 里围绕
viewBox 中心 `(512, 512)` 施加，**不改变布局占位尺寸**。

数值来自浏览器 `getBBox()` 实测。改动图标集后必须重新实测，不要凭印象调。
`loading`（LoadingOutlined）固定为 1 —— 它只画 1/4 圆弧，包围盒中心偏离画布中心 238px，
按弧的包围盒缩放会把旋转中心放大错位。

### 4.1 自绘图标也必须归一（返工教训）

第一轮只给 antd 图标建了缩放表，**漏掉了自绘图标**——它们的坐标是各自手写的，
从未参与归一。全量实测后发现偏离 89.3% 的自绘图标：

| 图标 | 修复前跨度 | 原因 |
| --- | --- | --- |
| `IconArchive` / `IconPanelLeft` / `IconPanelRight` | **75%** | 占 18 单位 |
| `IconShield`（`safetyCertificate`） | 97.6% | antd 原生偏大，表里给 0.95 不够 |

修法：在 `icons.tsx` 里用 `OptScale` 统一包裹自绘图形，`scale` 取 `89.3 / 当前跨度`。

**必须走 `OptScale`，不要直接写 `<g transform="scale(...)">`**：SVG 的 transform 会把
**描边一起放大**，1.19 倍的图标线宽也变 1.19 倍，比同排图标粗一圈——那是把
「尺寸不一」换成了「粗细不一」。`OptScale` 会按缩放比例把 `strokeWidth` 回缩，
保证放大后线宽与未缩放图标完全相同。

**例外：折叠 / 展开箭头不参与归一**（用户决定）。`IconChevron` / `ChevronDown` / `Left` /
`Right` / `Up` / `ArrowToggle` 只占 24 网格的 50%，但这就是它们在 Git 历史版本里的原尺寸，
用户明确要求换回原形态。**不要给它们补 `OptScale` 放大**，否则就是这次被回退掉的那个改动，
连带会让消息流、工具树、下拉胶囊里的箭头全部变大一圈。

### 4.2 以后怎么验

不要靠推断「应该一致了」。起一个临时探针页，遍历 `components/icons` 的全部导出，
用 `getBBox()` 量实际跨度并换算成 `占外框百分比`，偏离 89.3% 超过 8 个百分点即为异常
（指应参与归一的图标；箭头系量到 50% 属正常）。

## 五、动效现状（plan-630-2590 D3 已确认降级）

**仍生效**（依赖外层 svg 的类名或 inline style，与内部结构无关）：

- 整枚旋转：`.ic-settings`（45°）、`.ic-refresh`（180°）、`.ic-rotate-ccw`（-120°）、`.ic-globe`（-18°）
- `IconChevron` / `IconArrowToggle` 的展开旋转（inline `transform`）
- `IconSpinner` 的持续旋转（`session-spin`）
- `IconRingProgress` 的进度过渡与加载弧

**已失效**（依赖手绘 SVG 内部路径节点，`styles/icons.css` 里对应规则保留但不再匹配）：

- `.ic-fill` 悬停双色填充淡入
- `.ic-ac` / `.ic-ac-fill` 选中强调色点亮
- `.ic-spin` / `.ic-up` / `.ic-lid` / `.ic-hand` 等部件微动
- `data-open` + `.ic-pane` 的面板开合底色

## 六、新增图标的正确做法

1. 在 `components/antd-icons/registry.ts` 的 import 与 `GLYPHS` 里各加一行（具名引入，禁止 `import *`）。
2. 在 `components/icons.tsx` 用 `defineIcon("IconXxx", "<glyphKey>", "<css-name>", <默认尺寸>)` 导出。
3. 若要新动效，挂在**外层 svg** 上（inline style 或外层类名），不要指望改内部路径。
4. 跑 `npm run lint:design`、`npm run typecheck`。
