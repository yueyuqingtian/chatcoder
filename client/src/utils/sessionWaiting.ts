/** plan-522-2209：等待态的唯一判定入口。
 *  两种等待生命周期不同，不能一套逻辑套：
 *  - 审批/提问是「活体等待」——协程挂在运行中的 turn 内，进程重启即消失，所以必须绑 has_running；
 *  - 计划待确认是「持久等待」——awaiting_confirmation 已落库且启动修复不扫它，所以不看运行态。
 */

/** 判定所需的最小会话形状：不绑 SessionOut，侧栏传子会话行、组件传局部对象都能用。 */
export interface WaitingSessionLike {
  id: number;
  /** 有停在「计划待确认」的 turn（来自后端 SessionOut，或由 turn.updated 事件实时同步） */
  plan_awaiting?: boolean;
  /** 有运行中的 turn —— 活体等待只在运行期间成立 */
  has_running?: boolean;
}

/**
 * 会话是否处于「等待用户处理」。
 *
 * `hasPending` 由调用方注入（store 的 hasPending 选择器），让本函数保持纯折算、不持有数据，
 * 侧栏与输入区可以共用同一口径而不必各自写一套 if。
 */
export function waitingOf(
  session: WaitingSessionLike,
  hasPending: (id: number) => boolean,
): boolean {
  if (session.plan_awaiting === true) return true;
  // 活体等待必须绑运行态：进程重启后审批协程已消失（启动修复把 running 改成 interrupted），
  // 此时索引里即便还有残留条目也不能继续显示等待，否则会出现「一直转问号却点不出审批」。
  return session.has_running === true && hasPending(session.id);
}
