/** 唤醒消息的本地归轮（纯函数，无 store / 网络依赖，便于单测）。 */
import type { MessageOut } from "../api/client";
import { SenderType } from "@chatcoder/shared";

/** 把 turn_id 尚未回填的唤醒消息归入刚创建的唤醒轮（plan-20261014-actions-wait）。
 *
 *  服务端先广播唤醒消息、后创建轮次，回填 turn_id 只写库不广播：不处理的话，要等该轮结束后的整表刷新，
 *  前端才会把唤醒消息与回复并入上一轮——唤醒期间上一轮的操作行、计时条都停在中间，结束后才挪到末尾。
 *  这里按唤醒事件携带的 turn_id 就地回填，与服务端落库一致。
 *  plan-20261022-release-ci：除工作流唤醒外，子代理完成唤醒（subagent.wakeup）与邮箱唤醒（session.wakeup，
 *  子会话提问 / 子会话全部结束 / 后台命令完成）的消息同样先于轮次广播，一并在这里归轮。
 *
 *  只认「此刻最新的一条用户侧消息」：唤醒消息刚创建、会话空闲，它必然是最新的；若最新的用户侧消息不是未归属的
 *  唤醒消息，就不回填（避免把遗留的孤儿消息错归到新轮）。没有可回填的消息时原样返回（引用不变）。 */
export function adoptWakeupTurn(messages: MessageOut[], turnId: number): MessageOut[] {
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (m.sender_type !== SenderType.User) continue;
    const c = m.content as Record<string, unknown> | null;
    const orphanWakeup = m.turn_id == null && (c?.workflow_wakeup === true || c?.subagent_wakeup === true);
    if (!orphanWakeup) return messages;
    const next = messages.slice();
    next[i] = { ...m, turn_id: turnId };
    return next;
  }
  return messages;
}
