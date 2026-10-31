/** plan-20261112-loading-home-sidebar：去重仅作用于正文，不能影响轮次仍在执行时的等待标记。 */
export function visibleStreamingText(text: string, persistedText?: string): string {
  return text && persistedText && text.trim() === persistedText.trim() ? "" : text;
}
