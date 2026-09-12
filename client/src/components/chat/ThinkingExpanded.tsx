/** ThinkingExpanded（v41）：思考内容展开块——ThinkingBlock 与 StreamingText 思考尾部共用。
 * - 内容实时更新（流式期间随 delta 增长）；
 * - useAutoScroll：默认贴底自动跟随；用户在块内上滑即取消自动滚动（以用户滚动为准），
 *   滚回底部后自动恢复跟随。展开块随 open 卸载/重挂，每次展开都从贴底状态开始。
 */
import { useEffect, useRef } from "react";

/** 用户优先的贴底滚动 hook：内容增长时若处于"吸附"状态则跟到最新；
 * 与 MessageFlow 主消息流同一策略（本轮优化）：
 * - 贴底状态下检测到向上滑动（wheel，当帧生效）→ 立即取消吸附并进入"用户接管"；
 * - 接管期间内容增长一律不打扰；用户滚回贴底（< 8px）→ 清除接管、恢复吸附；
 * - 未接管时离底 > 24px 仍按用户主动上滑处理。 */
function useAutoScroll<T extends HTMLElement>(dep: unknown) {
  const ref = useRef<T>(null);
  const stickRef = useRef(true);
  /** 用户接管标记：接管期间内容增长不贴底，直到滚回底部 */
  const userOverrideRef = useRef(false);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const onScroll = () => {
      const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
      if (userOverrideRef.current) {
        // 只有真正滚回贴底才恢复吸附（避免"还差几像素"被 24px 阈值判回跟随 → 反复拉底抖动）
        if (distance < 8) {
          userOverrideRef.current = false;
          stickRef.current = true;
        }
        return;
      }
      stickRef.current = distance < 24;
    };
    const onWheel = (e: WheelEvent) => {
      if (e.deltaY < 0 && !userOverrideRef.current) {
        userOverrideRef.current = true;
        stickRef.current = false;
      }
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    el.addEventListener("wheel", onWheel, { passive: true, capture: true });
    return () => {
      el.removeEventListener("scroll", onScroll);
      el.removeEventListener("wheel", onWheel, { capture: true });
    };
  }, []);

  useEffect(() => {
    const el = ref.current;
    if (el && stickRef.current) el.scrollTop = el.scrollHeight;
  }, [dep]);

  return ref;
}

export function ThinkingExpanded({ text }: { text: string }) {
  const ref = useAutoScroll<HTMLDivElement>(text);
  return (
    <div className="tc-output thinking-output" ref={ref}>
      <pre className="tc-plain thinking-pre">{text}</pre>
    </div>
  );
}
