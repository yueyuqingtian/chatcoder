/**
 * Ant Design 图标渲染器（plan-630-2590）。
 *
 * 为什么自己渲染、不用 @ant-design/icons 的 React 组件：官方组件输出
 * `<span role="img" class="anticon"><svg/></span>` 包裹层，styles/icons.css 里
 * 以 `svg.ic` 为根的三态选择器会全部失效，需要重写约 290 行 CSS；数据包
 * `@ant-design/icons-svg` 体积还只有它的三分之一，且没有运行时依赖。
 *
 * 为什么要做小尺寸描边补偿：antd 图标是 1024 网格的**实心路径模拟线条**，
 * 线宽写死在路径里、无法通过属性调整。13px 渲染时视觉线宽约 0.5 物理像素，
 * 叠加抗锯齿后发灰发虚（与 plan-20261014-fix4 修的是同一类问题），而项目里
 * 11~13px 的图标调用共 323 处，必须补偿。
 *
 * 类名为什么要显式传：CSS 的整枚旋转动效挂在 `.ic-settings`（45°）、
 * `.ic-refresh`（180°）、`.ic-rotate-ccw`（-120°）、`.ic-globe`（-18°）上，
 * 而 antd 图标的 name 是 `setting` / `reload` / `undo` / `global`，与类名不同名，
 * 从 glyph.name 推导会让这些动效静默失效，所以由桥接层写死原类名。
 */

import { createElement, type CSSProperties, type ReactNode, type SVGProps } from "react";
import type { AbstractNode, IconDefinition } from "@ant-design/icons-svg/es/types";
import type { GlyphKey } from "./registry";
import { opticalScaleFor } from "./opticalScale";

/** 尺寸档位：名称档位或任意数字（兼容旧调用） */
export type AntIconSize = "xs" | "sm" | "md" | "lg" | number;

const SIZE_TIERS: Record<string, number> = { xs: 12, sm: 14, md: 16, lg: 20 };

/** 档位名 → px；数字原样返回 */
export function resolveIconSize(size: AntIconSize): number {
  if (typeof size === "number") return size;
  return SIZE_TIERS[size] ?? 14;
}

/** 小尺寸描边补偿：只在 ≤13px 生效，避免大尺寸变「胖」。视觉走查后可单点调整。 */
const SMALL_SIZE_STROKE_BOOST = 0.18;
const BOOST_MAX_PX = 13;

/** 1024 网格下按目标视觉线宽反算补偿值；阈值以上不补偿 */
function strokeBoost(px: number): number {
  if (px > BOOST_MAX_PX) return 0;
  return (1024 / px) * SMALL_SIZE_STROKE_BOOST;
}

/** SVG 属性名 → React 属性名（antd 图标数据用的是连字符写法） */
const ATTR_MAP: Record<string, string> = {
  "fill-rule": "fillRule",
  "clip-rule": "clipRule",
  "fill-opacity": "fillOpacity",
  "stroke-width": "strokeWidth",
  "stroke-linecap": "strokeLinecap",
  "stroke-linejoin": "strokeLinejoin",
  class: "className",
};

/** AbstractNode → React 节点；attrs 的连字符键统一转驼峰 */
function renderNode(node: AbstractNode, key: number): ReactNode {
  const props: Record<string, unknown> = { key };
  for (const [k, v] of Object.entries(node.attrs ?? {})) {
    props[ATTR_MAP[k] ?? k] = v;
  }
  const children = node.children;
  if (!children?.length) return createElement(node.tag, props);
  return createElement(node.tag, props, children.map(renderNode));
}

/** icon 字段可能是节点，也可能是 (primaryColor, secondaryColor) => 节点 的函数（twotone 用） */
function resolveIconNode(glyph: IconDefinition): AbstractNode {
  const icon = glyph.icon;
  return typeof icon === "function" ? icon("#1890ff", "#e6f7ff") : icon;
}

export interface AntIconProps extends Omit<SVGProps<SVGSVGElement>, "size" | "color"> {
  /** @ant-design/icons-svg 的图标定义 */
  glyph: IconDefinition;
  /** registry 里的图标键，用于查光学尺寸表（见 opticalScale.ts） */
  glyphKey?: GlyphKey;
  /** 类名后缀，与 styles/icons.css 的 `.ic-<name>` 选择器对应（不可从 glyph.name 推导） */
  name: string;
  size?: AntIconSize;
  color?: string;
  className?: string;
  style?: CSSProperties;
}

export function AntIcon({ glyph, glyphKey, name, size = 14, color, className, style, ...rest }: AntIconProps) {
  const node = resolveIconNode(glyph);
  // antd 图标的 viewBox 不统一（多层图标是 "64 64 896 896"），必须从数据里读
  const viewBox = (node.attrs?.viewBox as string | undefined) ?? "0 0 1024 1024";
  const px = resolveIconSize(size);
  const boost = strokeBoost(px);
  const children = node.children ?? [];
  /* plan-630-2590：光学尺寸归一。围绕画布中心 (512,512) 缩放——两种 viewBox 的中心都是它。
   * 缩放 1 时完全不生成 <g>，避免给绝大多数图标多包一层节点。 */
  const scale = opticalScaleFor(glyphKey);

  return (
    <svg
      width={px}
      height={px}
      viewBox={viewBox}
      fill={color || "currentColor"}
      aria-hidden
      {...rest}
      className={["ic", `ic-${name}`, className ?? ""].filter(Boolean).join(" ")}
      style={style}
    >
      {/* paintOrder="stroke" 让描边画在填充下层，只向外扩半个线宽，不吞掉内部镂空细节 */}
      <g
        fill={color || "currentColor"}
        stroke={boost ? color || "currentColor" : "none"}
        strokeWidth={boost}
        strokeLinejoin="round"
        paintOrder="stroke"
        transform={scale === 1 ? undefined : `translate(512 512) scale(${scale}) translate(-512 -512)`}
      >
        {children.map(renderNode)}
      </g>
    </svg>
  );
}
