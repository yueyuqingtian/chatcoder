/** MarkdownContent —— 兼容桥（plan-282-1416）
 *
 * 实现已迁入组件库 `components/ui/Markdown.tsx`，此文件仅做转发，
 * 保证既有 23 处 `from "../MarkdownContent"` 调用与
 * `StreamingMarkdown` 的 `markdownComponents` 引用零改动。
 *
 * 新代码请直接从组件库导入：`import { Markdown } from "./ui"`。
 */
export { Markdown as MarkdownContent, markdownComponents } from "./ui/Markdown";
