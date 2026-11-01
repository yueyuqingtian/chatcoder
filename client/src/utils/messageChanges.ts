/** plan-20261101-performance：只信任写入端登记的追加关系，跳过重复的全历史前缀校验。 */
type Chain = { latest: number };
const appends = new WeakMap<readonly unknown[], { chain: Chain; version: number }>();
type Indexed = { positions: Map<number, number>; length: number };
const indexes = new WeakMap<readonly { id: number }[], Indexed>();

function indexOf(messages: readonly { id: number }[]): Indexed {
  let index = indexes.get(messages);
  if (!index) {
    index = { positions: new Map(messages.map((message, position) => [message.id, position])), length: messages.length };
    indexes.set(messages, index);
  }
  return index;
}
export function hasMessageId(messages: readonly { id: number }[], id: number): boolean {
  const position = indexOf(messages).positions.get(id);
  return position != null && position < messages.length;
}
export function indexMessageAppend<T extends { id: number }>(previous: T[], next: T[]): T[] {
  let index = indexOf(previous);
  if (index.length !== previous.length) index = { positions: new Map(previous.map((message, position) => [message.id, position])), length: previous.length };
  for (let position = previous.length; position < next.length; position++) {
    if (!index.positions.has(next[position].id)) index.positions.set(next[position].id, position);
  }
  index.length = next.length;
  indexes.set(next, index);
  return markMessageAppend(previous, next);
}
export function markMessageAppend<T>(previous: T[], next: T[]): T[] {
  let hint = appends.get(previous);
  if (!hint) { hint = { chain: { latest: 0 }, version: 0 }; appends.set(previous, hint); }
  // 分叉从新链开始，不误认两条不同追加历史；元信息不强引用旧数组。
  if (hint.version !== hint.chain.latest) appends.set(next, { chain: { latest: 0 }, version: 0 });
  else appends.set(next, { chain: hint.chain, version: ++hint.chain.latest });
  return next;
}
export function isMessageAppend(previous: readonly unknown[], next: readonly unknown[]): boolean {
  const before = appends.get(previous), after = appends.get(next);
  return Boolean(before && after && before.chain === after.chain && before.version < after.version && previous.length <= next.length);
}
