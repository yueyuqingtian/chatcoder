/** 输入框引用 chip 的生命周期（plan-693-2868 P1 收尾修复）。
 *
 *  背景：微应用引用 chip 原先被定为「保留型」——发送成功后仍留在输入框上，本意是
 *  「接着追问同一个应用时不必重新去 / 菜单里挑一次」。实际效果反了：发完消息 chip 还挂着，
 *  看起来像**引用还没提交**，用户会以为上一轮的引用会被带进下一条消息；更糟的是后续任何
 *  输入变化都会把这枚 chip 同步回草稿，切走再切回来它又冒出来（跨会话 / 跨消息重复携带）。
 *
 *  规则（组件只做调用，判定放在纯函数里，由 tests/composer-refs-lifecycle.test.ts 守住）：
 *  - 发送成功（含运行中入队——refs 已随队列项带走）→ **全部清空**，不按 kind 留例外；
 *  - 发送失败 → **原样保留**（用户已经选好的引用不能丢，直接重试即可）；
 *  - 会话切换 / Esc 取消引用 / 发送后清空输入框 → 清空（由组件调用点负责）。
 */

/** 引用项的最小形态。
 *  这里不 import ComposerCore.ComposerRef：那是组件文件（.tsx），纯逻辑模块保持无
 *  组件依赖，测试才能直接 `node tests/xxx.test.ts` 跑。 */
interface RefLike {
  kind: string;
}

/** 发送结束后输入框应保留的 chip：送达即清空，未送达原样保留。
 *  **不按 kind 留例外**——任何「发送后保留」的特例都会让 chip 看起来像没提交（见文件头）。 */
export function refsAfterSend<T extends RefLike>(refs: T[], delivered: boolean): T[] {
  return delivered ? [] : refs;
}

/** `sendTurn` 的返回值分不清「失败」与「运行中入队」（两者都是 null，见 store/chat.ts 的
 *  sendTurn：入队分支与 catch 分支都 `return null`）。入队时 refs 已随队列项带走了，
 *  所以「队列里真的多出一条新 id」就说明本次发送已被接收（送达），否则按失败处理。 */
export function deliveredFromQueue(
  beforeIds: ReadonlySet<string>,
  queued: readonly { id: string }[],
): boolean {
  return queued.some((q) => !beforeIds.has(q.id));
}
