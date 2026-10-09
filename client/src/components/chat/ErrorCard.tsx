/** 统一错误卡片（plan-387-1825 C1；plan-20261009 E1 重做为紧凑单行）。
 *
 * 结构：一行头部（图标 +「标题 摘要」+ 右侧操作）+ 可折叠详情。
 * 两个渲染位共用本组件：turn 内 ERROR 消息（inline）与消息流末尾错误卡（trailing，带重试 / 关闭）。
 *
 * plan-20261009 E1（用户反馈：报错很丑、一条报错占位太高、展开后出现异色色块、「查看详情」悬停色怪）：
 *  · 标题 / 摘要 / 开关 / 操作行四段纵向堆叠 → 合并为一行，操作按钮右对齐；
 *  · 中性表面，只有图标与标题用错误色 —— 卡内控件直接用标准按钮，悬停色与全局一致；
 *  · 详情与卡片同底，不再套独立色块；单行「原因：细节」的详情只放细节，不再重复摘要；
 *  · 自造的 .err-toggle 文本按钮改为 ui/Button（AGENTS：禁止页面私有按钮类）；
 *    展开走 ui/Collapse 高度过渡（AGENTS：禁止 0 动画切换）。
 *
 * 设计约束（AGENTS.md）：层次只用留白 / 字号字重 / 颜色表达，不引入左侧彩色竖线。
 */
import { memo, useState } from "react";
import { IconAlertCircle, IconChevron, IconX } from "../icons";
import { Button } from "../ui/Button";
import { Collapse } from "../ui/Collapse";
import { IconButton } from "../ui/IconButton";

/** 摘要展示上限：超出即截断，完整原文进详情 */
const SUMMARY_MAX = 96;
/** 摘要之外仍剩余这么多字符时才值得折叠（否则整句直出更省一次点击） */
const DETAIL_MIN_EXTRA = 24;

function clip(text: string, max = SUMMARY_MAX): string {
  const t = text.trim();
  return t.length > max ? `${t.slice(0, max).trimEnd()}…` : t;
}

/** 把错误文本拆成摘要与详情；无需折叠时 detail 为 null（不渲染详情开关）。 */
function splitErrorText(text: string): { summary: string; detail: string | null } {
  const raw = text.trim();
  if (!raw) return { summary: "", detail: null };

  // 多行（堆栈 / 逐行报错）：首行作摘要；首行完整可见时详情只放其余行（保留原缩进），被截断时保留全文
  const nl = raw.indexOf("\n");
  if (nl > 0) {
    const first = raw.slice(0, nl).trim();
    const summary = clip(first);
    const rest = raw.slice(nl + 1).replace(/^\s*\n/, "").trimEnd();
    return { summary, detail: summary === first ? rest || null : raw };
  }

  // 单行「原因：细节」——首个中/英文冒号前的原因短语作摘要，冒号后的细节进详情（不再重复原因）
  const zh = raw.indexOf("：");
  const en = raw.indexOf(": ");
  const sep = zh < 0 ? en : en < 0 ? zh : Math.min(zh, en);
  if (sep > 0 && sep <= SUMMARY_MAX) {
    const tail = raw.slice(sep + 1).trim();
    // 细节够长才折叠，避免「重试失败：Network Error」这类短句也要多点一下
    if (tail.length > DETAIL_MIN_EXTRA) return { summary: clip(raw.slice(0, sep)), detail: tail };
    return { summary: clip(raw), detail: null };
  }

  const clipped = clip(raw);
  return { summary: clipped, detail: clipped === raw ? null : raw };
}

interface Props {
  /** 错误正文（store 的 flowError.text 或消息 content 的 text） */
  text: string;
  /** 卡片标题，默认「执行出错」 */
  title?: string;
  /** trailing 变体才提供的操作 */
  onRetry?: () => void;
  onClose?: () => void;
  /** 额外的类名（用于挂载点定位，如 .flow-error-card） */
  className?: string;
  /** trailing 变体：挂在消息流末尾（默认 inline = turn 内） */
  variant?: "inline" | "trailing";
}

export const ErrorCard = memo(function ErrorCard({
  text, title = "执行出错", onRetry, onClose, className, variant = "inline",
}: Props) {
  const [showDetail, setShowDetail] = useState(false);
  const { summary, detail } = splitErrorText(text);
  const hasActions = Boolean(detail || onRetry || onClose);
  return (
    <div
      className={`turn-item turn-item-error${variant === "trailing" ? " flow-error-card" : ""}${className ? ` ${className}` : ""}`}
    >
      <div className="err-head">
        <IconAlertCircle size={14} className="err-icon" />
        <div className="err-text">
          <span className="err-title">{title}</span>
          <span className="err-summary">{summary || text}</span>
        </div>
        {hasActions && (
          <div className="err-actions">
            {detail && (
              <Button
                type="button"
                size="xs"
                variant="ghost"
                aria-expanded={showDetail}
                onClick={() => setShowDetail((v) => !v)}
              >
                详情
                <IconChevron direction="down" open={showDetail} size={12} />
              </Button>
            )}
            {onRetry && (
              <Button type="button" size="xs" variant="ghost" onClick={onRetry}>
                重试
              </Button>
            )}
            {onClose && <IconButton size="xs" icon={<IconX size={14} />} title="关闭" onClick={onClose} />}
          </div>
        )}
      </div>
      {/* 长报错原文（HTTP/JSON/堆栈）默认收起；展开走高度过渡，详情与卡片同底 */}
      {detail && (
        <Collapse open={showDetail}>
          <div className="err-detail">{detail}</div>
        </Collapse>
      )}
    </div>
  );
});
