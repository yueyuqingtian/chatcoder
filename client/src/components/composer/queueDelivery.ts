/** plan-20261120-queue-message-mode：任务执行中消息投递方式与快捷键解析。
 *
 *  纯粹函数模块（无 DOM / React 依赖），供 ComposerCore、MiniChat 与单测共用：
 *  - 当配置为「排队入列（queue）」时：Enter 排队，Alt+Enter 直接发送；
 *  - 当配置为「直接发送（immediate）」时：Enter 直接发送，Alt+Enter 排队；
 *  - 空闲状态下 Enter / Alt+Enter 均正常发送。 */

export type QueueMessageMode = "queue" | "immediate";
export type MessageDelivery = "queue" | "immediate";

export function normalizeQueueMessageMode(value: unknown): QueueMessageMode {
  return value === "immediate" ? "immediate" : "queue";
}

export interface EnterDeliveryInput {
  key: string;
  altKey: boolean;
  shiftKey: boolean;
  ctrlKey: boolean;
  metaKey: boolean;
  queueMode: QueueMessageMode;
  isTurnRunning: boolean;
}

/** 解析 Enter / Alt+Enter 对应的投递动作；非发送快捷键返回 null（交回 textarea 原生行为）。 */
export function resolveEnterDelivery(input: EnterDeliveryInput): MessageDelivery | null {
  if (input.key !== "Enter" || input.shiftKey || input.ctrlKey || input.metaKey) return null;
  if (!input.isTurnRunning) return "immediate";
  if (input.altKey) return input.queueMode === "queue" ? "immediate" : "queue";
  return input.queueMode;
}

export interface SendButtonStateInput {
  isHome: boolean;
  busy: boolean;
  isTurnRunning: boolean;
  hasContent: boolean;
  uploading: boolean;
  queueMode: QueueMessageMode;
}

/** 计算输入卡右下主按钮的形态（发送 / 停止 / 排队）。 */
export function resolveSendButtonState(input: SendButtonStateInput): "send" | "stop" | "queue" {
  if (input.isHome || !input.busy) return "send";
  if (!input.hasContent && !input.uploading) return "stop";
  if (input.isTurnRunning) return input.queueMode === "immediate" ? "send" : "queue";
  return "send";
}
