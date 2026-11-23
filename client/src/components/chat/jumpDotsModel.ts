/** JumpDots 纯数据模型与分区求解（plan-20261121-jumpdots-v2）。
 *  将大纲/本地消息合并、scrollspy 匹配、分区容量与区块窗口抽离为纯函数，便于单测验证。 */
import type { MessageOut, OutlineAttachment, OutlineRef, SessionOutlineItem } from "../../api/client";
import { userMessagePreview, type TimelineEntry } from "./timeline";

export const DEFAULT_JUMP_BLOCK_SIZE = 15;
export const MIN_JUMP_BLOCK_SIZE = 8;
export const JUMP_TICK_PITCH_PX = 11;

export interface JumpDotItem {
  key: string;
  messageId: number;
  turnId: number | null;
  createdAt: string | null;
  text: string;
  attachments: OutlineAttachment[];
  refs: OutlineRef[];
  /** 在当前已加载 entries 中的下标；未加载分页为 -1 */
  entryIndex: number;
  loaded: boolean;
}

export interface JumpBlockWindow {
  blockCount: number;
  focusBlock: number;
  currentBlock: number;
  start: number;
  end: number;
  showPager: boolean;
}

/** 合并服务端轻量大纲与前端已加载 entries（保留乐观消息与未同步尾部消息）。 */
export function buildJumpDotItems(
  entries: readonly TimelineEntry[],
  outline: readonly SessionOutlineItem[] | null,
): JumpDotItem[] {
  const loadedByMsgId = new Map<number, { entryIndex: number; msg: MessageOut }>();
  const localOrder: Array<{ entryIndex: number; msg: MessageOut }> = [];

  entries.forEach((entry, entryIndex) => {
    if (entry.kind !== "turn") return;
    for (const item of entry.items) {
      if (item.kind !== "user") continue;
      const msgId = Number(item.msg.id);
      if (Number.isFinite(msgId)) {
        loadedByMsgId.set(msgId, { entryIndex, msg: item.msg });
      }
      localOrder.push({ entryIndex, msg: item.msg });
    }
  });

  if (outline == null) {
    return localOrder.map(({ entryIndex, msg }, idx) => {
      const preview = userMessagePreview(msg);
      const msgId = Number(msg.id);
      return {
        key: Number.isFinite(msgId) && msgId > 0 && msgId < 1e12 ? `msg-${msgId}` : `local-${entryIndex}-${idx}`,
        messageId: msgId,
        turnId: msg.turn_id ?? null,
        createdAt: msg.created_at ?? null,
        text: preview.text,
        attachments: preview.attachments,
        refs: preview.refs,
        entryIndex,
        loaded: true,
      };
    });
  }

  const out: JumpDotItem[] = [];
  const seen = new Set<number>();

  for (const item of outline) {
    seen.add(item.message_id);
    const local = loadedByMsgId.get(item.message_id);
    if (local) {
      const preview = userMessagePreview(local.msg);
      out.push({
        key: `msg-${item.message_id}`,
        messageId: item.message_id,
        turnId: local.msg.turn_id ?? item.turn_id,
        createdAt: local.msg.created_at ?? item.created_at,
        text: preview.text || item.text,
        attachments: preview.attachments.length > 0 ? preview.attachments : item.attachments,
        refs: preview.refs.length > 0 ? preview.refs : item.refs,
        entryIndex: local.entryIndex,
        loaded: true,
      });
    } else {
      out.push({
        key: `msg-${item.message_id}`,
        messageId: item.message_id,
        turnId: item.turn_id,
        createdAt: item.created_at,
        text: item.text,
        attachments: item.attachments,
        refs: item.refs,
        entryIndex: -1,
        loaded: false,
      });
    }
  }

  // 补上刚发送但大纲尚未刷新的本地/乐观用户消息
  localOrder.forEach(({ entryIndex, msg }, idx) => {
    const msgId = Number(msg.id);
    if (seen.has(msgId)) return;
    const preview = userMessagePreview(msg);
    out.push({
      key: Number.isFinite(msgId) && msgId > 0 && msgId < 1e12 ? `msg-${msgId}` : `local-${entryIndex}-${idx}`,
      messageId: msgId,
      turnId: msg.turn_id ?? null,
      createdAt: msg.created_at ?? null,
      text: preview.text,
      attachments: preview.attachments,
      refs: preview.refs,
      entryIndex,
      loaded: true,
    });
  });

  return out;
}

/** 计算当前视口焦点对应的刻度下标；跳过未加载项，避免跨页空洞误命中。 */
export function findActiveDot(items: readonly JumpDotItem[], activeIndex?: number): number {
  if (activeIndex == null) return -1;
  let active = -1;
  for (let i = 0; i < items.length; i++) {
    const ei = items[i].entryIndex;
    if (ei >= 0 && ei <= activeIndex) active = i;
  }
  return active;
}

/** 按容器可用高度求解单区最大刻度数：常规 15 条，矮窗口自适应收缩到 8~15 条。 */
export function computeBlockSize(containerHeight: number | null, defaultSize = DEFAULT_JUMP_BLOCK_SIZE): number {
  if (containerHeight == null || !Number.isFinite(containerHeight) || containerHeight <= 0) {
    return defaultSize;
  }
  // 预留上下切区按钮与边距约 72px，避免溢出定位容器
  const fit = Math.floor((containerHeight - 72) / JUMP_TICK_PITCH_PX);
  return Math.min(defaultSize, Math.max(MIN_JUMP_BLOCK_SIZE, fit));
}

/** 求解当前应展示的区块范围（默认落在焦点所在区块，未确定焦点时落最后一区）。 */
export function resolveBlockWindow(
  total: number,
  blockSize: number,
  activeDot: number,
  manualBlock: number | null,
): JumpBlockWindow {
  const safeSize = Math.max(1, blockSize);
  const blockCount = Math.max(1, Math.ceil(total / safeSize));
  const focusBlock = activeDot >= 0 ? Math.min(blockCount - 1, Math.floor(activeDot / safeSize)) : blockCount - 1;
  const currentBlock = Math.max(0, Math.min(blockCount - 1, manualBlock ?? focusBlock));
  const start = currentBlock * safeSize;
  const end = Math.min(total, start + safeSize);
  return {
    blockCount,
    focusBlock,
    currentBlock,
    start,
    end,
    showPager: total > safeSize,
  };
}

export function isImageAttachment(att: Pick<OutlineAttachment, "type" | "mime_type">): boolean {
  return att.type === "image" || att.mime_type.startsWith("image/");
}

export function formatBytesCompact(n: number): string {
  if (!n || n <= 0) return "";
  if (n >= 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)}MB`;
  if (n >= 1024) return `${(n / 1024).toFixed(0)}KB`;
  return `${n}B`;
}

export function formatOutlineTime(raw: string | null | undefined): string {
  if (!raw) return "";
  const normalized = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}/.test(raw) && !/[zZ]|[+-]\d{2}:?\d{2}$/.test(raw)
    ? `${raw.replace(" ", "T")}Z`
    : raw;
  const d = new Date(normalized);
  if (Number.isNaN(d.getTime())) return "";
  const now = new Date();
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  if (d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate()) {
    return `${hh}:${mm}`;
  }
  const mon = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${mon}-${day} ${hh}:${mm}`;
}
