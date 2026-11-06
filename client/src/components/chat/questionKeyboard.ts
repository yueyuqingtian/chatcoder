/** plan-20261105-question-custom：切题可能保留输入焦点，回车目标不能仅依赖已重置的高亮。 */
export function questionEnterIndex(kbIndex: number, optionCount: number, customIdx: number, customFocused: boolean): number {
  return customFocused ? customIdx : Math.min(kbIndex, optionCount - 1);
}
