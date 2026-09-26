/** 展开面板（plan-73-323 阶段2 · 方案 3.5）。
 *
 * 结构自上而下：头部摘要 → 任务列表（手风琴，最多渲染 5 张、超出滚动）→ 选中任务详情 → 底部固定行。
 *
 * 交互取舍：卡片点击**只切换选中**，不触发任何任务操作；跳转必须显式点「查看会话」——
 * 避免误点导致列表乱跳或误操作（方案 3.9 的易错点对策）。
 *
 * 组件复用说明：宠物页是独立窗口/独立 chunk，不引入主窗 `components/ui/*` 组件树
 * （会把整棵依赖拖进宠物包），因此此处用语义化类名 + 设计令牌（--r-* / --dur-*）实现，
 * 视觉参数与主窗保持一致。
 */
import { useEffect } from "react";
import type { RecentDone, TaskCard, TaskStep } from "./useTaskCards";
import { formatElapsed, formatRelative } from "./useNow";

/** 列表最多同时渲染的卡片数（超出滚动；头部始终显示真实总数） */
const MAX_RENDER = 5;

const STATUS_LABEL: Record<TaskCard["status"], string> = {
  running: "执行中",
  waiting: "等待",
  failed: "失败",
};

interface Props {
  cards: TaskCard[];
  aggregate: { total: number; waiting: number; failed: number };
  recent: RecentDone[];
  selected: number | null;
  now: number;
  onSelect: (sessionId: number | null) => void;
  onOpenSession: (sessionId: number) => void;
  onLoadSteps: (sessionId: number) => void;
  onHide: () => void;
  onOpenSettings: () => void;
}

function progressOf(c: TaskCard) {
  const total = c.steps.length;
  const done = c.steps.filter((s) => s.status === "done").length;
  return { total, done, pct: total > 0 ? Math.round((done / total) * 100) : 0 };
}

function StepRow({ step }: { step: TaskStep }) {
  return (
    <div className={`pet-step is-${step.status}`}>
      <span className={`pet-step-icon is-${step.status}`} aria-hidden="true" />
      <span className="pet-step-text">{step.content}</span>
      {step.activeForm && step.status === "running" && <span className="pet-step-note">{step.activeForm}</span>}
    </div>
  );
}

export function DetailPanel({
  cards,
  aggregate,
  recent,
  selected,
  now,
  onSelect,
  onOpenSession,
  onLoadSteps,
  onHide,
  onOpenSettings,
}: Props) {
  const shown = cards.slice(0, MAX_RENDER);
  const selectedCard = cards.find((c) => c.sessionId === selected) ?? null;
  const selId = selectedCard ? selectedCard.sessionId : null;
  const selStepCount = selectedCard ? selectedCard.steps.length : 0;

  // 选中一张没有清单的卡 → 按需补齐引擎步骤（一次性；已有 AI 清单则不拉，避免无谓请求）
  useEffect(() => {
    if (selId != null && selStepCount === 0) onLoadSteps(selId);
  }, [selId, selStepCount, onLoadSteps]);

  return (
    <div className="pet-panel">
      <div className="pet-panel-head">
        <div className="pet-panel-title">
          {aggregate.total > 0 ? `${aggregate.total} 个任务运行中` : "当前无运行任务"}
        </div>
        <div className="pet-panel-sub">
          {aggregate.waiting > 0 && <span className="pet-chip is-waiting">{aggregate.waiting} 个等待确认</span>}
          {aggregate.failed > 0 && <span className="pet-chip is-failed">{aggregate.failed} 个失败</span>}
          {cards.length > MAX_RENDER && <span className="pet-chip">显示前 {MAX_RENDER} 个</span>}
        </div>
      </div>

      <div className="pet-panel-list">
        {shown.map((c) => {
          const p = progressOf(c);
          const isSel = c.sessionId === selected;
          return (
            <div key={c.sessionId} className={`pet-card is-${c.status}${isSel ? " is-selected" : ""}`}>
              <button
                type="button"
                className="pet-card-head"
                onClick={() => onSelect(isSel ? null : c.sessionId)}
              >
                <span className={`pet-card-badge is-${c.status}`}>{STATUS_LABEL[c.status]}</span>
                <span className="pet-card-title">{c.title}</span>
                {c.subagentCount > 0 && <span className="pet-card-sub">+{c.subagentCount} 子代理</span>}
                <span className="pet-card-time">{formatElapsed(c.startedAt, now)}</span>
              </button>

              <div className="pet-card-line">
                <span className="pet-card-step">
                  {p.total > 0 ? `第 ${Math.min(p.done + 1, p.total)}/${p.total} 步` : c.currentAction || "执行中"}
                </span>
                {p.total > 0 && (
                  <span className="pet-card-bar">
                    <span className="pet-card-bar-fill" style={{ width: `${p.pct}%` }} />
                  </span>
                )}
              </div>

              {isSel && (
                <div className="pet-card-detail">
                  {c.steps.length > 0 ? (
                    <div className="pet-step-list">
                      {c.steps.map((s, i) => (
                        <StepRow key={`${i}-${s.content}`} step={s} />
                      ))}
                    </div>
                  ) : (
                    <div className="pet-empty-line">暂无步骤清单（该任务未分步）</div>
                  )}

                  {c.lastResult && (
                    <div className="pet-kv">
                      <span className="pet-kv-key">最近工具</span>
                      <span className={c.lastResult.ok ? "pet-kv-ok" : "pet-kv-fail"}>
                        {c.lastResult.tool}
                        {c.lastResult.ok ? " · 成功" : " · 失败"}
                      </span>
                    </div>
                  )}
                  {c.subagentCount > 0 && (
                    <div className="pet-kv">
                      <span className="pet-kv-key">子代理</span>
                      <span>运行中 {c.subagentCount} 个</span>
                    </div>
                  )}
                  {c.goalText && (
                    <div className="pet-kv">
                      <span className="pet-kv-key">目标</span>
                      <span>
                        {c.goalText}
                        {c.goalTurnsUsed > 0 ? ` · 第 ${c.goalTurnsUsed} 轮` : ""}
                      </span>
                    </div>
                  )}

                  <button type="button" className="pet-btn-primary" onClick={() => onOpenSession(c.sessionId)}>
                    查看会话
                  </button>
                </div>
              )}
            </div>
          );
        })}

        {cards.length === 0 && (
          <div className="pet-panel-empty">
            <div className="pet-empty-line">暂无运行中的任务</div>
            {recent.length > 0 && (
              <div className="pet-recent">
                {recent.slice(0, 2).map((r) => (
                  <button
                    key={r.sessionId}
                    type="button"
                    className="pet-recent-row"
                    onClick={() => onOpenSession(r.sessionId)}
                  >
                    <span className="pet-recent-title">{r.title}</span>
                    <span className="pet-recent-time">{formatRelative(r.at, now)}</span>
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      <div className="pet-panel-foot">
        <span className="pet-foot-count">{aggregate.total} 个任务</span>
        {/* plan-73-326：隐藏改为持久（写偏好），从设置页或宠物下方角标均可恢复 */}
        <button
          type="button"
          className="pet-foot-btn"
          onClick={onHide}
        >
          隐藏宠物
        </button>
        <button type="button" className="pet-foot-btn" onClick={onOpenSettings}>
          设置
        </button>
      </div>
    </div>
  );
}
