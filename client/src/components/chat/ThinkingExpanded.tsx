/** ThinkingExpanded（v41）：思考内容展开块——ThinkingBlock 与 StreamingText 思考尾部共用。
 * - 内容实时更新（流式期间随 delta 增长）；
 * - useAutoScroll：默认贴底自动跟随；用户在块内上滑即取消自动滚动（以用户滚动为准），
 *   滚回底部后自动恢复跟随。展开块随 open 卸载/重挂，每次展开都从贴底状态开始。
 */
import { useEffect, useRef } from "react";

/** 用户优先的贴底滚动 hook：内容增长时若处于"吸附"状态则跟到最新；
 * scroll 事件里离底 > 24px 视为用户主动上滑 → 取消吸附；滚回底部 → 恢复吸附。 */
function useAutoScroll<T extends HTMLElement>(dep: unknown) {
  const ref = useRef<T>(null);
  const stickRef = useRef(true);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const onScroll = () => {
      stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
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
