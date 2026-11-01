export interface UserSendSignal { sessionId: number | null; confirmed: number; optimistic: number; sequence: number }
/** plan-20261101-performance：回填历史里的用户消息不是新发送，不能抢走阅读位置。 */
export function advanceUserSendSignal(previous: UserSendSignal, sessionId: number | null,
  messages: readonly { id: number; sender_type: string }[]): UserSendSignal {
  let confirmed = 0, user = 0;
  for (let index = messages.length - 1; index >= 0; index--) {
    const message = messages[index];
    if (!confirmed && message.id > 0 && message.id < 1e12) confirmed = message.id;
    if (!user && message.sender_type === "user") user = message.id;
    if (confirmed && user) break;
  }
  if (previous.sessionId !== sessionId) return { sessionId, confirmed, optimistic: user >= 1e12 ? user : 0, sequence: 0 };
  const sent = user >= 1e12 ? user > previous.optimistic : user > previous.confirmed;
  return { sessionId, confirmed: Math.max(confirmed, previous.confirmed),
    optimistic: Math.max(previous.optimistic, user >= 1e12 ? user : 0), sequence: previous.sequence + (sent ? 1 : 0) };
}
