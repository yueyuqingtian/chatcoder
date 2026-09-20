/** ContextNoticeCard —— "上下文已回收"提示（plan-282-1441）。
 *
 * 为什么需要：后端在构造 API 副本时，会把**单条超长工具结果**落盘折叠为占位符
 * （原文可在工作区 `.compact-cache/` 恢复）。这个过程此前完全静默，用户只看到
 * "占用突然降了几十 k"，很容易误以为历史丢了（用户反馈的"隐藏压缩"）。
 *
 * v16：按占用占比折叠较早工具结果的隐式压缩已移除——历史内容只在超过
 * 「设置-常规」压缩阈值后由"上下文压缩"卡片（可恢复）处理；本卡片只讲超长结果的落盘回收。
 * 它只在"确有回收"时出现，且可关闭，不打扰正常对话。
 */
import { IconCompress, IconX } from "../icons";
import { useChatStore } from "../../store/chat";

export function ContextNoticeCard({
  notice,
}: {
  notice: { savedTokens: number; foldedResults: number; at: number };
}) {
  const dismiss = () => useChatStore.setState({ contextNotice: null });
  const kTokens = notice.savedTokens >= 1000
    ? `${(notice.savedTokens / 1000).toFixed(1)}k`
    : String(notice.savedTokens);

  return (
    <div className="ctx-notice">
      <span className="ctx-notice-icon"><IconCompress size={13} /></span>
      <div className="ctx-notice-body">
        <div className="ctx-notice-title">
          上下文已回收约 {kTokens} tokens
          {notice.foldedResults > 0 && `（${notice.foldedResults} 条超长工具结果已落盘）`}
        </div>
        <div className="ctx-notice-desc">
          为避免单条超长工具输出撑爆请求，其原文已落盘到工作区
          <code>.compact-cache/</code>，需要时可用 <code>fs_read</code> 读回。
          <b>历史消息本身未被删除，也不会因占用比例被折叠。</b>
        </div>
      </div>
      <button className="ctx-notice-close" type="button" onClick={dismiss} title="知道了">
        <IconX size={12} />
      </button>
    </div>
  );
}
