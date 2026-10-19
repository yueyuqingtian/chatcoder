/** 后续任务建议卡片（plan-156-619 S10）。
 *
 * 展示位置：消息流底部（`MessageFlow` 的 trailingNode），只在最新一轮之后出现。
 * 两个动作：
 *   · 在新子会话执行 —— 后端建子会话并下发建议步骤，成功后左栏出现该子会话；
 *   · 取消 —— 后端置 dismissed，本卡片消失且刷新/重启都不会再出现。
 *
 * 视觉（项目规则）：
 *  · 纯色底 + 1px 边框 + `--r-lg`，不使用彩色渐变或毛玻璃（浮层不透明原则）；
 *  · 不用左侧装饰性竖线表达层级，层次靠留白 / 字重 / 底色；
 *  · 按钮统一 `ui/Button`，提示统一 `ui/Tooltip`，圆角只用令牌。
 */
import { memo, useState } from "react";
import { useChatStore } from "../../store/chat";
import { IconSparkles, IconChevronDown, IconCheck, IconCpu } from "../icons";
import { Button, Collapse } from "../ui";
import { api } from "../../api/client";

const MAX_VISIBLE_STEPS = 3;

export const SuggestionCard = memo(function SuggestionCard() {
  const suggestion = useChatStore((s) => s.pendingSuggestion);
  const dismissSuggestion = useChatStore((s) => s.dismissSuggestion);
  const loadSubsessionIndex = useChatStore((s) => s.loadSubsessionIndex);
  const switchSession = useChatStore((s) => s.switchSession);
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

  /** 接受建议：建子会话 → 清卡片 → 刷新左栏子会话桶 → 切到新子会话。 */
  const onAccept = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await api.acceptSuggestion(suggestion.id);
      // 先清卡片：后端已置 accepted，本地保留会与「已消费」状态不一致
      useChatStore.setState({ pendingSuggestion: null });
      if (res?.subsession_id != null) {
        // plan-156-620：后端返回的 session_id 是根主会话；子会话桶整体重建，
        // 无论建议来自主会话还是子会话，新子会话都出现在左栏同一父会话下方
        await loadSubsessionIndex();
        await switchSession(res.subsession_id);
      }
    } catch (e) {
      // 失败保留卡片可重试——后端 accept 是「先建会话、成功后才置状态」，
      // 因此这里失败意味着建议仍是 pending，重试安全。
      setError("创建子会话失败：" + String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="sg-card" role="region" aria-label="后续任务建议">
      <div className="sg-head">
        <span className="sg-head-icon"><IconSparkles size={13} /></span>
        <span className="sg-head-title">下一步建议</span>
        {busy && <span className="sg-head-hint">正在创建子会话…</span>}
      </div>

      <div className="sg-body">
        <div className="sg-title">{suggestion.title}</div>
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
          <span>在新子会话执行</span>
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
      </div>
    </div>
  );
});
