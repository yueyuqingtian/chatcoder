# 图标规范（Iconography）

> plan-230-1144 M5.2 建立。适用于 `client/src/components/icons.tsx` 及所有使用方。

## 1. 设计基调

- Feather / Lucide 风格：24×24 viewBox、线条描边、round 端点、`currentColor`。
- 默认 `strokeWidth = 1.75`；发送/方向类主图标可用 `2`。
- 图标颜色由父容器 `color` 决定，禁止在图标组件内写死颜色。

## 2. 尺寸档位

`IconSize` 支持名称档位（推荐）与任意数字（兼容旧调用）：

| 档位 | px | 使用场景 |
|---|---|---|
| `xs` | 12 | 徽标/内联标记、行内小图标 |
| `sm` | 14 | 工具栏按钮、列表行内（默认值） |
| `md` | 16 | 主操作按钮、导航项 |
| `lg` | 20 | 面板标题、空态插画级图标 |

用法：`<IconPlus size="sm" />`。避免继续散落 `size={13}` 这类无档位数字。

## 3. 状态协议

`IconState` 声明可交互图标的状态集合：`idle | hover | active | focus | disabled | open | loading`。

约定：
- 图标本身只画"形态"，状态切换优先由 CSS（父容器类名）驱动；
- 需要按状态换形态的（如文件夹 open/closed、面板展开/收起），组件暴露 `open` 属性；
- 折叠/展开一律使用统一组件 `<IconChevron open={...} />`（见下），
  不要用"切换两个不同图标组件"实现——那是硬切，不是过渡。

## 4. IconChevron（统一折叠箭头）

```tsx
import { IconChevron } from "./icons";
<IconChevron open={expanded} />              // right → 展开时旋转 90°（指下）
<IconChevron direction="down" open={expanded} /> // down → 展开时旋转 180°（指上）
```

实现：单一右向 chevron + `transform: rotate()` 过渡（`--dur-3` / `--ease-out`），
展开/折叠是连续旋转动画。旧代码中的 `IconChevronRight` / `IconChevronDown` /
`IconChevronLeft` 保留（大量静态用法），新代码优先用 `IconChevron`。

## 5. 可访问性（aria）

- 默认 `aria-hidden: true`（纯装饰图标）。
- 当图标是按钮内唯一内容时，调用方可显式覆盖：
  `<IconX aria-hidden={undefined} aria-label="关闭" />`，或给按钮加 `title`/`aria-label`。
- `baseProps` 中 `...rest` 在任何默认属性之后展开，因此调用方始终可覆盖。

## 6. 动画与焦点

- 图标过渡时长/曲线一律引用 token（`--dur-*` / `--ease-*`），禁止硬编码。
- 可交互元素的键盘焦点环由 `styles/focus.css` 统一提供（`:focus-visible`），
  组件不要自行声明 focus 样式。
- 折叠标（如 `.tc-chevron`、`.rp-tab-close`）必须"常显低对比、hover 提亮"，
  不得默认 `opacity: 0` 隐藏（触屏设备不可见、可发现性差）。
