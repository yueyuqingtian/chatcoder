/** 后续任务建议卡片（plan-156-619 S10 · plan-549-2281 改为主会话内执行）。
 *
 * 展示位置：消息流底部（`MessageFlow` 的 trailingNode），只在最新一轮之后出现。
 * 两个动作：
 *   · 开始执行 —— 后端在**当前会话**里开新一轮执行建议的步骤，卡片消失、消息流继续；
 *   · 取消 —— 后端置 dismissed，本卡片消失且刷新/重启都不会再出现。
 *
 * 为什么不再新建子会话（plan-549-2281）：并行执行统一收敛到子代理，子会话的创建入口下线；
 * 建议卡是「用户点一下就开工」的轻量入口，直接在主会话续跑最直观——不必再跳去另一个会话
 * 才知道任务有没有开始。
 *
 * 视觉（项目规则）：
 *  · 纯色底 + 1px 边框 + `--r-lg`，不使用彩色渐变或毛玻璃（浮层不透明原则）；
 *  · 不用左侧装饰性竖线表达层级，层次靠留白 / 字重 / 底色；
 *  · 按钮统一 `ui/Button`，提示统一 `ui/Tooltip`，圆角只用令牌。
 */
import { memo, useState } from "react";
import { useChatStore } from "../../store/chat";
import { IconChevronDown, IconCheck, IconCpu } from "../icons";
import { Button, Collapse } from "../ui";

const MAX_VISIBLE_STEPS = 2;

export const SuggestionCard = memo(function SuggestionCard() {
  const suggestion = useChatStore((s) => s.pendingSuggestion);
  const dismissSuggestion = useChatStore((s) => s.dismissSuggestion);
  const acceptSuggestion = useChatStore((s) => s.acceptSuggestion);
  const currentSessionId = useChatStore((s) => s.currentSessionId);

  const [expanded, setExpanded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!suggestion) return null;

  const steps = suggestion.steps || [];
  // plan-20261019-motion-sweep：超出的步骤放进独立的折叠容器，「还有 N 步」是高度过渡而不是整段弹出
  const headSteps = steps.slice(0, MAX_VISIBLE_STEPS);
  const restSteps = steps.slice(MAX_VISIBLE_STEPS);
  const hidden = restSteps.length;
  const stepItem = (s: string, i: number) => (
    <li key={`${i}-${s.slice(0, 12)}`}>
      <span className="sg-step-idx">{i + 1}</span>
      <span className="sg-step-text">{s}</span>
    </li>
  );

  /** 接受建议：交给 store 动作统一收尾（运行态、左栏 has_running、心跳、刷新）。
   *  plan-560-2311 E4：卡片等刷新完成后**才**清自己这张——先清会让组件卸载，
   * 之后的刷新异常写不进任何可见位置（用户只看到卡片消失、消息没变）。 */
  const onAccept = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await acceptSuggestion(suggestion.id);
      // 后端已是终态：卡片是过期残留，直接清掉（只清自己这张，期间到达的新建议不能一并抹掉）
      if (res.stale) {
        if (useChatStore.getState().pendingSuggestion?.id === suggestion.id) {
          useChatStore.setState({ pendingSuggestion: null });
        }
        return;
      }
      if (res.error) setError(res.error);
      if (res.ok) {
        // 刷新已完成（或仅刷新失败但这一轮确实开始了）：此时再移除卡片
        if (useChatStore.getState().pendingSuggestion?.id === suggestion.id) {
          useChatStore.setState({ pendingSuggestion: null });
        }
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="sg-card" role="region" aria-label="后续任务建议">
      <div className="sg-body">
        <div className="sg-title">
          <span className="sg-kicker">下一步</span>
          {suggestion.title}
        </div>
        {suggestion.summary && <div className="sg-summary">{suggestion.summary}</div>}

        {steps.length > 0 && (
          <>
            <div className="sg-steps-wrap">
              <ul className="sg-steps">{headSteps.map(stepItem)}</ul>
              {hidden > 0 && (
                <Collapse open={expanded} lazy>
                  {() => (
                    <ul className="sg-steps sg-steps-rest">
                      {restSteps.map((s, i) => stepItem(s, i + MAX_VISIBLE_STEPS))}
                    </ul>
                  )}
                </Collapse>
              )}
            </div>
            {hidden > 0 && (
              <button
                type="button"
                className="sg-steps-more"
                onClick={() => setExpanded((v) => !v)}
                aria-expanded={expanded}
              >
                <IconChevronDown size={12} className={"sg-steps-caret" + (expanded ? " is-open" : "")} />
                <span>{expanded ? "收起步骤" : `还有 ${hidden} 步`}</span>
              </button>
            )}
          </>
        )}

        {error && <div className="sg-error">{error}</div>}
      </div>

      <div className="sg-actions">
        <Button
          variant="primary"
          size="sm"
          disabled={busy || currentSessionId == null}
          onClick={() => void onAccept()}
        >
          <IconCpu size={12} />
          <span>开始执行</span>
        </Button>
        <Button
          variant="ghost"
          size="sm"
          disabled={busy}
          onClick={() => void dismissSuggestion()}
        >
          <IconCheck size={12} />
          <span>取消</span>
        </Button>
        {busy && <span className="sg-busy">正在开始执行…</span>}
      </div>
    </div>
  );
});
