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
import { IconCheck, IconChevron, IconCopy, IconToneBadge, IconX } from "../icons";
import { Button } from "../ui/Button";
import { Collapse } from "../ui/Collapse";
import { IconButton } from "../ui/IconButton";
import { errorKey, useDismissedErrors } from "../../store/dismissedErrors";

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

/** plan-20261017-ux-overhaul 阶段 A：按报错特征归类，标题直接说清「是哪类问题」，
 *  并给一句可执行的建议——用户不必读懂 HTTP 原文也知道下一步做什么。
 *  只在调用方未显式传 title 时生效；匹配不到时沿用「执行出错」。 */
const ERROR_KINDS: Array<{ re: RegExp; title: string; hint: string }> = [
  // plan-565-2323 G4：账号被逐个停用是一种「用户自己能修」的状态，必须排在鉴权失败之前——
  // 它的原因码里带 credential，若不前置会被下面的鉴权分支当成 key 失效，把用户引向错误方向。
  { re: /provider_credentials_all_disabled|全部处于停用状态/i,
    title: "账号已全部停用",
    hint: "该供应商的账号全部处于停用状态，请在模型管理中启用至少一个账号" },
  { re: /\b(401|403)\b|unauthori[sz]ed|forbidden|invalid[_ ]?api[_ ]?key|authentication|鉴权|认证失败/i,
    title: "鉴权失败", hint: "检查该供应商的 API Key 是否有效或已过期" },
  { re: /\b429\b|rate[_ ]?limit|too many requests|请求过于频繁|限流/i,
    title: "请求过于频繁", hint: "供应商限流，稍等片刻后重试" },
  { re: /quota|insufficient|balance|余额|额度|billing/i,
    title: "额度不足", hint: "检查账户余额或套餐额度" },
  { re: /context[_ ]?length|maximum context|too many tokens|prompt is too long|上下文.*(超|过)长/i,
    title: "上下文超长", hint: "压缩会话或新开会话后重试" },
  { re: /timed? ?out|timeout|超时/i,
    title: "请求超时", hint: "网络较慢或服务繁忙，可直接重试" },
  { re: /ECONN|ENOTFOUND|EAI_AGAIN|network|fetch failed|socket|连接(失败|被拒|中断)|网络/i,
    title: "网络连接异常", hint: "检查网络或代理设置后重试" },
  { re: /\b5\d\d\b|internal server error|bad gateway|service unavailable|overloaded/i,
    title: "服务端错误", hint: "供应商服务暂时不可用，稍后重试" },
];

function classify(text: string): { title: string; hint: string } | null {
  for (const k of ERROR_KINDS) if (k.re.test(text)) return { title: k.title, hint: k.hint };
  return null;
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
  text, title, onRetry, onClose, className, variant = "inline",
}: Props) {
  const [showDetail, setShowDetail] = useState(false);
  const [copied, setCopied] = useState(false);
  const split = splitErrorText(text);
  const kind = title ? null : classify(text);
  const heading = title ?? kind?.title ?? "执行出错";
  // plan-20261025-shell（需求 9）：「鉴权失败：Request failed…」这类报错，冒号前的原因与归类标题相同，
  // 此前标题与摘要会连着出现两遍「鉴权失败」——此时摘要改取冒号后的细节首行，详情只在比摘要长时保留
  let { summary, detail } = split;
  if (detail && summary.trim() === heading.trim()) {
    const lead = clip(detail.split("\n")[0] ?? "");
    summary = lead;
    if (lead === detail.trim()) detail = null;
  }
  const copy = () => {
    void navigator.clipboard?.writeText(text).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    });
  };
  return (
    <div
      className={`turn-item turn-item-error${variant === "trailing" ? " flow-error-card" : ""}${className ? ` ${className}` : ""}`}
    >
      <div className="err-head">
        {/* plan-20261025-shell（需求 9）：实心语气徽标代替线框圆，浅红底上一眼可辨 */}
        <IconToneBadge tone="error" size={16} className="err-icon" />
        <div className="err-text">
          <div className="err-title">{heading}</div>
          <div className="err-summary">{summary || text}</div>
          {kind && <div className="err-hint">{kind.hint}</div>}
        </div>
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
            {/* 复制图标固定槽位：复制后原位换成对勾，不改变按钮宽度 */}
            <IconButton
              size="xs"
              icon={copied ? <IconCheck size={14} /> : <IconCopy size={14} />}
              title={copied ? "已复制" : "复制报错"}
              onClick={copy}
            />
            {onRetry && (
              <Button type="button" size="xs" variant="ghost" onClick={onRetry}>
                重试
              </Button>
            )}
            {onClose && <IconButton size="xs" icon={<IconX size={14} />} title="关闭" onClick={onClose} />}
        </div>
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

/** 轮次内的报错（数据库 ERROR 消息）：可关闭，关闭记在本机（plan-20261025-shell 需求 9：消息流所有报错都支持叉掉）。
 *  只订阅自己这一条的关闭态，关掉一条不会让整轮重渲染。 */
export const DismissibleErrorCard = memo(function DismissibleErrorCard({ msgId, text }: { msgId: number | string; text: string }) {
  const key = errorKey(msgId, text);
  const dismissed = useDismissedErrors((s) => s.keys.has(key));
  const dismiss = useDismissedErrors((s) => s.dismiss);
  if (dismissed) return null;
  return (
    <div className="turn-item-wrap">
      <ErrorCard text={text} onClose={() => dismiss(key)} />
    </div>
  );
});
