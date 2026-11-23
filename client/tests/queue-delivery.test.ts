/** plan-20261120-queue-message-mode：覆盖队列消息配置与 Enter / Alt+Enter 快捷键解析。 */
import assert from "node:assert/strict";
import {
  normalizeQueueMessageMode,
  resolveEnterDelivery,
  resolveSendButtonState,
} from "../src/components/composer/queueDelivery";

const baseKey = {
  key: "Enter",
  altKey: false,
  shiftKey: false,
  ctrlKey: false,
  metaKey: false,
};

// 1. 模式归一化
assert.equal(normalizeQueueMessageMode("queue"), "queue");
assert.equal(normalizeQueueMessageMode("immediate"), "immediate");
assert.equal(normalizeQueueMessageMode(undefined), "queue");
assert.equal(normalizeQueueMessageMode("invalid"), "queue");

// 2. 运行中 + 配置为「排队入列（queue）」：Enter 排队，Alt+Enter 直接发送
assert.equal(
  resolveEnterDelivery({ ...baseKey, queueMode: "queue", isTurnRunning: true }),
  "queue",
  "排队模式下运行中按 Enter 应排队入列",
);
assert.equal(
  resolveEnterDelivery({ ...baseKey, altKey: true, queueMode: "queue", isTurnRunning: true }),
  "immediate",
  "排队模式下运行中按 Alt+Enter 应直接发送",
);

// 3. 运行中 + 配置为「直接发送（immediate）」：Enter 直接发送，Alt+Enter 排队
assert.equal(
  resolveEnterDelivery({ ...baseKey, queueMode: "immediate", isTurnRunning: true }),
  "immediate",
  "直接发送模式下运行中按 Enter 应直接发送",
);
assert.equal(
  resolveEnterDelivery({ ...baseKey, altKey: true, queueMode: "immediate", isTurnRunning: true }),
  "queue",
  "直接发送模式下运行中按 Alt+Enter 应排队入列",
);

// 4. 空闲状态：无论配置为何，Enter 与 Alt+Enter 均正常发送
assert.equal(
  resolveEnterDelivery({ ...baseKey, queueMode: "queue", isTurnRunning: false }),
  "immediate",
);
assert.equal(
  resolveEnterDelivery({ ...baseKey, altKey: true, queueMode: "queue", isTurnRunning: false }),
  "immediate",
);

// 5. Shift+Enter / Ctrl+Enter / 非 Enter 不拦截
assert.equal(resolveEnterDelivery({ ...baseKey, shiftKey: true, queueMode: "queue", isTurnRunning: true }), null);
assert.equal(resolveEnterDelivery({ ...baseKey, ctrlKey: true, queueMode: "queue", isTurnRunning: true }), null);
assert.equal(resolveEnterDelivery({ ...baseKey, altKey: true, ctrlKey: true, queueMode: "queue", isTurnRunning: true }), null);
assert.equal(resolveEnterDelivery({ ...baseKey, key: "a", queueMode: "queue", isTurnRunning: true }), null);

// 6. 发送按钮三态
assert.equal(
  resolveSendButtonState({ isHome: false, busy: true, isTurnRunning: true, hasContent: false, uploading: false, queueMode: "queue" }),
  "stop",
);
assert.equal(
  resolveSendButtonState({ isHome: false, busy: true, isTurnRunning: true, hasContent: true, uploading: false, queueMode: "queue" }),
  "queue",
);
assert.equal(
  resolveSendButtonState({ isHome: false, busy: true, isTurnRunning: true, hasContent: true, uploading: false, queueMode: "immediate" }),
  "send",
);
assert.equal(
  resolveSendButtonState({ isHome: false, busy: true, isTurnRunning: false, hasContent: true, uploading: false, queueMode: "queue" }),
  "send",
);

console.log("通过：队列消息模式与 Enter / Alt+Enter 快捷键解析 17 项断言");
