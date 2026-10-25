/** 排队托盘（plan-20261009-ui U2；plan-20261025-shell 需求 9 重做外观）：会话运行中继续发送的消息，排在输入卡上方。
 *
 * 旧实现是输入卡内部顶部的一串细胶囊（与正文挤在同一个卡片里、按钮 20px 难点中）。
 * 现在：托盘像从输入卡背后抽出的抽屉——左右比输入卡各收进一截、底边与输入卡顶边相接，
 * 每条一行（弱色序号 + 模式徽标 + 单行摘要 + 附件数 + 操作：立即发送 / 编辑回填 / 移除）。
 * plan-20261025-composer：去掉「排队中 N · 说明 · 清空」标题行（用户反馈太占行高），
 * 操作图标常显而不是悬停才出现——托盘只剩条目本身，精简扁平；「按顺序发送」的说明交给无障碍标签。
 * 条目不再各套一层描边卡片，层次靠留白与悬停底色；入队错峰滑入、出队高度收拢、发送中显示进度微光。 */
import { useEffect, useRef, useState } from "react";
import { Badge, Tooltip } from "../ui";
import { IconArrowUp, IconPaperclip, IconPencil, IconX } from "../icons";
import { StatusIcon } from "../iconkit/StatusIcon";
import { useI18n } from "../../store/i18n";
import type { QueuedInput } from "../../store/chat";

const MODE_TAG: Record<string, string> = { plan: "计划", readonly: "只读" };

/** 出队收拢：被移除的条目保留一个退场周期再真正卸载 */
function useExiting<T extends { id: string }>(items: T[], ms = 220): Array<T & { _leaving?: boolean }> {
  const [view, setView] = useState<Array<T & { _leaving?: boolean }>>(items);
  const timers = useRef<Map<string, number>>(new Map());
  /** 最新 items：退场定时器到点复核用——切走又切回时队列项会「复活」，到点不能再按旧账移除。 */
  const itemsRef = useRef(items);
  itemsRef.current = items;
  useEffect(() => {
    const ids = new Set(items.map((i) => i.id));
    // 复活取消：仍存在于 items 的项，先清掉它待触发的退场定时器。
    // 缺这一步时：切走（条目进入退场计时）→ 220ms 内切回（条目复活）→ 旧定时器到点
    // 仍把复活项从 view 移除；此后 items 不再变化 ⇒ effect 不重跑，托盘永久不显示。
    for (const id of ids) {
      const t = timers.current.get(id);
      if (t != null) { window.clearTimeout(t); timers.current.delete(id); }
    }
    setView((prev) => {
      const next: Array<T & { _leaving?: boolean }> = [];
      // 保持原顺序：先遍历旧列表，标记已移除项；再补入新增项
      for (const p of prev) {
        const cur = items.find((i) => i.id === p.id);
        if (cur) next.push(cur);
        else next.push(p._leaving ? p : { ...p, _leaving: true });
      }
      for (const it of items) if (!prev.some((p) => p.id === it.id)) next.push(it);
      return next;
    });
    for (const p of view) {
      if (!ids.has(p.id) && !timers.current.has(p.id)) {
        const t = window.setTimeout(() => {
          timers.current.delete(p.id);
          // 到点复核：等待期间可能已复活（切走又切回），已回来则不移除
          if (itemsRef.current.some((x) => x.id === p.id)) return;
          setView((v) => v.filter((x) => x.id !== p.id));
        }, ms);
        timers.current.set(p.id, t);
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items]);
  useEffect(() => () => { timers.current.forEach((t) => window.clearTimeout(t)); }, []);
  return view;
}

export function QueueTray({ items, onFlush, onRemove, onEdit }: {
  items: QueuedInput[];
  onFlush: (id: string) => void;
  onRemove: (id: string) => void;
  onEdit: (item: QueuedInput) => void;
}) {
  const { t } = useI18n();
  const view = useExiting(items);
  if (view.length === 0) return null;
  return (
    <div className="cc-queue" role="list" aria-label="排队消息：当前任务结束后按顺序发送">
      {view.map((q, i) => {
        const leaving = Boolean(q._leaving);
        const attach = q.attachments?.length ?? 0;
        return (
          <div key={q.id} className="mo-collapse cc-queue-slot" data-state={leaving ? "closed" : "open"}>
            <div className={"cc-queue-item" + (q.flushing ? " is-flushing" : "")} role="listitem">
              <span className="cc-queue-idx">{q.flushing ? <StatusIcon status="running" size={14} /> : i + 1}</span>
              {q.mode && MODE_TAG[q.mode] && <Badge size="xs" tone="info">{MODE_TAG[q.mode]}</Badge>}
              <span className="cc-queue-text">{q.content || (attach ? "（仅附件）" : "")}</span>
              {attach > 0 && (
                <span className="cc-queue-attach"><IconPaperclip size={12} />{attach}</span>
              )}
              <span className="cc-queue-ops">
                {q.flushing ? (
                  <span className="cc-queue-sending">{t("composer.queue_sending")}</span>
                ) : (
                  <>
                    <Tooltip content={t("composer.queue_flush_now")} side="top">
                      <button type="button" className="ui-icon-btn size-xs" aria-label={t("composer.queue_flush_now")} onClick={() => onFlush(q.id)}>
                        <IconArrowUp size={13} />
                      </button>
                    </Tooltip>
                    <Tooltip content="编辑（移回输入框）" side="top">
                      <button type="button" className="ui-icon-btn size-xs" aria-label="编辑" onClick={() => onEdit(q)}>
                        <IconPencil size={13} />
                      </button>
                    </Tooltip>
                  </>
                )}
                <Tooltip content={t("composer.queue_remove")} side="top">
                  <button type="button" className="ui-icon-btn size-xs tone-danger" aria-label={t("composer.queue_remove")} onClick={() => onRemove(q.id)}>
                    <IconX size={13} />
                  </button>
                </Tooltip>
              </span>
            </div>
          </div>
        );
      })}
    </div>
  );
}
