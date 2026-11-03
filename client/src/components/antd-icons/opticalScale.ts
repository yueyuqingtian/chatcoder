/**
 * antd 图标的光学尺寸归一（plan-630-2590）。
 *
 * 为什么需要这张表：antd 图标虽然是同一套 1024 网格，但各图标的**实际绘制跨度**差异很大
 * （实测 108 个图标落在 53.5%~102.7% 之间）。原生的两套设计尺寸是 89.3%（多数）与 100%
 * （圆形的，靠画大一点补偿观感面积）；而箭头、加号、更多、关闭这类符号只画到 75%~85%，
 * 与满格图标并排时明显偏小——用户反馈的「各个图标有的大有的小、不协调」就是它。
 *
 * 归一策略：把每个图标按「目标跨度 89.3% / 自身跨度」缩放到同一个视觉跨度，
 * 并做两端收敛——
 *  · 上限 1.20：小符号最多放大 20%，避免箭头被放大到糊边；
 *  · 下限 0.95：满格图标最多缩小 5%，保留 antd 对圆形的补偿意图（圆形不该和方块一样大）；
 *  · |缩放-1| < 0.015 视为无需调整，直接写 1 让编译器走无变换分支。
 *
 * 缩放围绕 viewBox 中心 (512,512) 做，不改变图标在布局中的占位尺寸，
 * 因此不会引入任何布局位移。
 *
 * 数值来源：在浏览器里用 `getBBox()` 逐个实测（含 path/rect/circle/ellipse 的合并包围盒），
 * 相对 viewBox 取较长边占比。改动图标集时请重新实测，不要凭印象调。
 *
 * 例外：`loading`（LoadingOutlined）只画 1/4 圆弧，包围盒中心偏离画布中心 238px，
 * 按弧的包围盒缩放会把旋转中心放大错位，故固定为 1。
 */

import type { GlyphKey } from "./registry";

export const OPTICAL_SCALE: Record<GlyphKey, number> = {
  aim: 0.95,
  apartment: 0.95,
  api: 0.98,
  appstore: 1.088,
  appstoreAdd: 1,
  arrowDown: 1.111,
  arrowLeft: 1.088,
  arrowRight: 1.088,
  arrowUp: 1.111,
  audio: 0.95,
  barChart: 1.042,
  bgColors: 0.962,
  block: 1.111,
  border: 1,
  branches: 0.971,
  bug: 0.95,
  bulb: 0.95,
  calendar: 1,
  camera: 0.95,
  caretRight: 1.2,
  check: 0.98,
  checkCircle: 0.95,
  checkSquare: 1,
  clockCircle: 0.95,
  close: 1.157,
  cluster: 1.042,
  code: 1,
  codeSandbox: 0.961,
  control: 1,
  copy: 0.95,
  dashboard: 0.95,
  database: 0.95,
  delete: 1,
  deploymentUnit: 0.95,
  desktop: 0.95,
  diff: 0.95,
  dislike: 0.971,
  download: 1.069,
  edit: 1,
  enter: 1.111,
  exclamationCircle: 0.95,
  expandAlt: 1.136,
  experiment: 1,
  export: 1,
  eye: 0.95,
  eyeInvisible: 0.95,
  fileMarkdown: 0.95,
  fileSearch: 0.95,
  fileText: 0.95,
  filter: 1,
  fire: 0.95,
  folder: 1,
  folderAdd: 1,
  folderOpen: 0.95,
  fullscreen: 1.136,
  fullscreenExit: 1.088,
  global: 0.95,
  highlight: 0.95,
  history: 0.95,
  holder: 1.177,
  infoCircle: 0.95,
  like: 0.971,
  link: 1.042,
  loading: 1,
  lock: 1,
  mail: 0.95,
  message: 0.95,
  minus: 1.088,
  moon: 1,
  more: 1.191,
  nodeIndex: 1,
  orderedList: 0.961,
  paperClip: 1.036,
  partition: 1,
  pauseCircle: 0.95,
  picture: 0.95,
  plus: 1.111,
  profile: 1,
  pushpin: 1.064,
  questionCircle: 0.95,
  read: 0.95,
  reload: 0.952,
  robot: 0.95,
  rollback: 1.136,
  safetyCertificate: 0.87,
  schedule: 0.95,
  search: 1,
  setting: 0.95,
  shrink: 1.076,
  solution: 0.95,
  sortAscending: 1.088,
  sortDescending: 1.088,
  sun: 0.95,
  tag: 0.95,
  team: 1,
  thunderbolt: 0.962,
  tool: 1,
  undo: 1,
  unlock: 1,
  unorderedList: 0.98,
  up: 1.053,
  user: 1,
  warning: 0.95,
};

/** 取某图标的光学缩放；未登记的一律 1（新增图标忘了登记时表现为「原始尺寸」，不会错位） */
export function opticalScaleFor(key: GlyphKey | undefined): number {
  if (!key) return 1;
  return OPTICAL_SCALE[key] ?? 1;
}
