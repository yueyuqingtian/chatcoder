/** 审批卡（plan-75-332）。
 *
 * 替换旧的全屏遮罩弹窗（.approval-overlay + .approval-card）：需要审批时直接
 * 占用输入框位置，与 AI 提问卡（QuestionWizardBox）同构——用户的视线与操作位置
 * 始终停留在输入区，不再被一层遮罩打断。
 *
 * 交互（对齐设计图）：
 * - 左下「解释」：先渲染骨架占位，AI 分析完成后把文字填进骨架（用途 / 影响范围 / 风险）；
 * - 中「更多选项」：向下展开——完整参数详情 + 「本会话始终允许 / 所有会话始终允许」；
 * - 右下「拒绝 / 允许一次」：与旧弹窗的「取消 / 仅本次执行」语义一一对应。
 *
 * 旧弹窗的四个动作全部保留，只是把低频的两个（始终允许）收进展开面板，
 * 让常规判断路径只有「拒绝 / 允许一次」两个按钮。
 */
import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useChatStore } from "../../store/chat";
import {
  IconAlertTriangle,
  IconChevronDown,
  IconChevronUp,
  IconClock,
  IconFileText,
  IconGlobe,
  IconInfo,
  IconTerminal,
  IconWand,
} from "../icons";
import { Button, Tooltip } from "../ui";

/** 审批卡字段文案对照（工具参数名 → 中文标签）。未知字段回落原名。 */
const FIELD_LABELS: Record<string, string> = {
  command: "命令", cwd: "工作目录", timeout: "超时", is_background: "后台运行",
  path: "路径", paths: "路径", file: "文件", files: "文件列表",
  pattern: "搜索内容", query: "关键词", include: "文件过滤",
  offset: "起始行", limit: "行数上限", recursive: "递归", max_depth: "递归深度",
  content: "写入内容", old_text: "原文本", new_text: "新文本", replace_all: "替换全部",
  edits: "批量编辑", url: "地址", method: "请求方法", body: "请求体", headers: "请求头",
  sql: "SQL", goal: "目标", question: "问题", task_title: "子任务",
};

/** 动作类别 → 图标（后端 approval_policy 的 action 字段）。 */
function actionIcon(action: string): ReactNode {
  if (action.startsWith("exec_")) return <IconTerminal size={15} />;
  if (action.startsWith("write") || action === "delete") return <IconFileText size={15} />;
  return <IconAlertTriangle size={15} />;
}

function clipText(text: string, max = 600): string {
  return text.length > max ? `${text.slice(0, max)}\n…（已省略 ${text.length - max} 字）` : text;
}

function jsonText(value: unknown): string {
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function formatValue(key: string, value: unknown): string {
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "number") return String(value);
  if (typeof value === "string") return clipText(value);
  if (Array.isArray(value)) {
    if (key === "edits") {
      const paths = value
        .map((e) => (e && typeof e === "object" ? String((e as Record<string, unknown>).path ?? "") : ""))
        .filter(Boolean);
      const head = `${value.length} 处修改`;
      return paths.length ? `${head}\n${paths.join("\n")}` : head;
    }
    return clipText(value.map((x) => (typeof x === "string" ? x : jsonText(x))).join("\n"));
  }
  return clipText(jsonText(value));
}

/** 参数 → 标签/值行（常用字段优先，其余按原顺序追加；空值跳过）。 */
function argRows(args: unknown): Array<{ label: string; value: string }> {
  if (args == null) return [];
  if (typeof args === "string") return [{ label: "参数", value: clipText(args) }];
  if (typeof args !== "object" || Array.isArray(args)) {
    return [{ label: "参数", value: clipText(jsonText(args)) }];
  }
  const obj = args as Record<string, unknown>;
  const ordered = [
    ...Object.keys(FIELD_LABELS).filter((k) => k in obj),
    ...Object.keys(obj).filter((k) => !(k in FIELD_LABELS)),
  ];
  const rows: Array<{ label: string; value: string }> = [];
  for (const k of ordered) {
    const v = obj[k];
    if (v === null || v === undefined || v === "") continue;
    rows.push({ label: FIELD_LABELS[k] || k, value: formatValue(k, v) });
  }
  return rows;
}

/** 主展示块：命令类给等宽命令原文，其余给结构化字段行。 */
function PrimaryArgs({ tool, args }: { tool: string; args: unknown }) {
  const rows = argRows(args);
  if (rows.length === 0) return null;

  // 命令类：命令原文是判断的核心，用等宽块独占展示，其余参数走字段区
  const isCommand = tool === "terminal_exec" || tool === "ci_run";
  const cmdRow = isCommand ? rows.find((r) => r.label === "命令") : undefined;
  const rest = cmdRow ? rows.filter((r) => r !== cmdRow) : rows;

  return (
    <>
      {cmdRow && (
        <div className="approval-cmd-block">
          <code>{cmdRow.value}</code>
        </div>
      )}
      {rest.length > 0 && (
        <div className="approval-arg-rows">
          {rest.map((r, i) => (
            <div key={`${i}-${r.label}`} className="approval-arg-row">
              <span className="approval-arg-label">{r.label}</span>
              <span className="approval-arg-value">{r.value}</span>
            </div>
          ))}
        </div>
      )}
    </>
  );
}

/** 解释区：骨架态 → 完成态。 */
function ExplainSection({ approvalId }: { approvalId: string }) {
  const explain = useChatStore((s) => s.approvalExplain);
  const active = explain && explain.approvalId === approvalId ? explain : null;
  if (!active || active.status === "idle") return null;

  if (active.status === "loading" || (active.status === "streaming" && !active.text)) {
    return (
      <div className="approval-explain">
        <div className="approval-explain-skeleton" aria-label="正在分析命令">
          <span className="approval-explain-line" />
          <span className="approval-explain-line short" />
          <span className="approval-explain-line" />
          <span className="approval-explain-line mid" />
        </div>
      </div>
    );
  }

  if (active.status === "error") {
    return (
      <div className="approval-explain">
        <div className="approval-explain-error">
          <IconInfo size={13} />
          <span>{active.error || "解释失败，请重试"}</span>
        </div>
      </div>
    );
  }

  // 逐条渲染要点：模型输出约定为「- 」开头的行
  const lines = active.text
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean)
    .map((l) => l.replace(/^[-*·]\s*/, ""));

  return (
    <div className="approval-explain">
      <ul className="approval-explain-list">
        {lines.map((l, i) => (
          <li key={i}>{l}</li>
        ))}
      </ul>
      {active.status === "streaming" && <span className="approval-explain-pulse" />}
    </div>
  );
}

export interface ApprovalCardProps {
  approvalId: string;
  detail: Record<string, unknown>;
  /** 拒绝 */
  onDeny: () => void;
  /** 允许一次 */
  onAllowOnce: () => void;
  /** 始终允许（scope: 本会话 / 所有会话） */
  onAlwaysAllow: (scope: "session" | "global") => void;
}

export function ApprovalCard({
  approvalId, detail, onDeny, onAllowOnce, onAlwaysAllow,
}: ApprovalCardProps) {
  const [showMore, setShowMore] = useState(false);
  const explain = useChatStore((s) => s.approvalExplain);
  const explainApproval = useChatStore((s) => s.explainApproval);
  const resetApprovalExplain = useChatStore((s) => s.resetApprovalExplain);
  const cardRef = useRef<HTMLDivElement>(null);

  const tool = String(detail.tool ?? "");
  const action = String(detail.action ?? "");
  const actionLabel = String(detail.action_label ?? "") || "执行一个操作";
  const agentName = String(detail.agent_name ?? "");
  const riskNote = String(detail.risk_note ?? "");
  const summary = typeof detail.summary === "string" ? detail.summary : "";
  const isOutsideRead = detail.outside_read === true;

  const explaining =
    explain != null && explain.approvalId === approvalId &&
    (explain.status === "loading" || explain.status === "streaming");

  /** 审批生命周期结束时清掉解释内容，避免下一个审批卡复用残留文本。 */
  useEffect(() => () => { resetApprovalExplain(); }, [approvalId, resetApprovalExplain]);

  /** Esc 关闭「更多选项」展开面板（浮层纪律：点击外部 + Esc 关闭）。 */
  useEffect(() => {
    if (!showMore) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setShowMore(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [showMore]);

  return (
    <div className="composer-approval-card" ref={cardRef}>
      <div className="approval-head">
        <span className="approval-head-icon">{actionIcon(action)}</span>
        <span className="approval-head-text">
          <span className="approval-head-tool">{tool || "工具"}</span>
          <span className="approval-head-action">想要{actionLabel}</span>
        </span>
      </div>

      {isOutsideRead && (
        <div className="approval-outside-hint">
          <IconInfo size={13} />
          <span>该路径在当前工作目录之外</span>
        </div>
      )}

      <PrimaryArgs tool={tool} args={detail.args} />

      {riskNote && (
        <div className="approval-risk-note">
          <IconAlertTriangle size={13} />
          <span>{riskNote}</span>
        </div>
      )}

      <ExplainSection approvalId={approvalId} />

      {showMore && (
        <div className="approval-more-panel">
          {/* plan-75-332 R2（用户反馈）：始终允许改为列表式选项——图标 + 标题 + 作用范围说明，
              用户一眼能分辨「本会话 / 所有会话」的区别，不再靠两个并排长文字按钮去猜。 */}
          <button
            type="button"
            className="approval-more-opt"
            onClick={() => onAlwaysAllow("session")}
          >
            <span className="approval-more-opt-icon"><IconClock size={14} /></span>
            <span className="approval-more-opt-body">
              <span className="approval-more-opt-title">本会话内始终允许</span>
              <span className="approval-more-opt-desc">当前对话中，同类操作不再询问</span>
            </span>
          </button>
          <button
            type="button"
            className="approval-more-opt"
            onClick={() => onAlwaysAllow("global")}
          >
            <span className="approval-more-opt-icon"><IconGlobe size={14} /></span>
            <span className="approval-more-opt-body">
              <span className="approval-more-opt-title">所有会话始终允许</span>
              <span className="approval-more-opt-desc">所有项目与对话中，同类操作不再询问</span>
            </span>
          </button>
          {(agentName || summary) && (
            <div className="approval-more-meta">
              {agentName && <span>发起者：{agentName}</span>}
              {summary && <span>{summary}</span>}
            </div>
          )}
          <div className="approval-more-tip">
            始终允许仅对「自动审批」与「完全访问」生效；询问审批模式下每次仍会询问。
          </div>
        </div>
      )}

      <div className="approval-footer">
        <Tooltip content="由 AI 分析这条操作的用途与风险">
          <Button
            variant="ghost"
            size="sm"
            icon={<IconWand size={13} />}
            disabled={explaining}
            onClick={() => explainApproval(approvalId)}
          >
            {explaining ? "正在分析…" : "解释"}
          </Button>
        </Tooltip>

        {/* plan-75-332 R2（用户反馈）：主决策「拒绝 / 允许一次」在右，「更多选项」置于其右端——
            低频入口不挤占主判断路径的视觉重心；展开时按钮转为按下态（底色 + 上指箭头）。 */}
        <div className="approval-footer-actions">
          <Button variant="secondary" size="sm" onClick={onDeny}>拒绝</Button>
          <Button variant="danger" size="sm" onClick={onAllowOnce}>允许一次</Button>
          <Tooltip content={showMore ? "收起更多选项" : "更多选项：始终允许等"}>
            <Button
              variant={showMore ? "subtle" : "outline"}
              size="sm"
              iconOnly
              aria-expanded={showMore}
              icon={showMore ? <IconChevronUp size={13} /> : <IconChevronDown size={13} />}
              onClick={() => setShowMore((v) => !v)}
            />
          </Tooltip>
        </div>
      </div>
    </div>
  );
}
