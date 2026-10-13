/** 状态图标（plan-20261009-ui U3「常规状态标识图标」+「状态变更」动画）。
 *
 * 统一取代散落的 6px 圆点 / 文字 spinner / 各自绘制的勾叉：
 *  pending   虚线环            queued    环 + 时针
 *  running   底环 + 旋转弧 + 呼吸核
 *  done      实心圆 + 对勾（弹出 + 描边绘入）
 *  failed    实心圆 + 叉       cancelled / paused / interrupted  环 + 双竖线
 *
 * 状态切换时内层 svg 以 status 为 key 重挂载，入场动画（弹出 / 绘入）即为「状态变更」反馈；
 * 颜色由 data-status 映射语义色（styles/icons.css），调用方无需传色。 */
import { memo } from "react";

export type StatusKind =
  | "pending" | "queued" | "running" | "done" | "failed" | "cancelled" | "paused" | "interrupted";

/** 后端/任务口径归一：in_progress → running，completed/success → done，error → failed */
export function normalizeStatus(raw: string | null | undefined): StatusKind {
  const s = (raw || "").toLowerCase();
  if (s === "in_progress" || s === "running" || s === "active") return "running";
  if (s === "done" || s === "completed" || s === "success" || s === "succeeded") return "done";
  if (s === "failed" || s === "error") return "failed";
  if (s === "cancelled" || s === "canceled") return "cancelled";
  if (s === "paused") return "paused";
  if (s === "interrupted") return "interrupted";
  if (s === "queued") return "queued";
  return "pending";
}

export const StatusIcon = memo(function StatusIcon({ status, size = 14, className, label }: {
  status: StatusKind;
  size?: number;
  className?: string;
  /** 无障碍名（缺省不朗读） */
  label?: string;
}) {
  const tone = status === "interrupted" ? "paused" : status;
  return (
    <span
      className={"st-icon" + (className ? ` ${className}` : "")}
      data-status={tone}
      style={{ width: size, height: size }}
      role={label ? "img" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
    >
      <svg key={status} width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round">
        {/* plan-128-518：所有环状态统一同一径同一线宽（r=8、线宽 2，外缘半径 9）——与转圈环大小完全一致，状态切换不再大小跳动；
            实心圆（完成/失败）半径同样取外缘 9 */}
        {status === "pending" && <circle className="st-ring" cx="12" cy="12" r="8" strokeWidth={2} strokeDasharray="2.4 3.1" />}
        {status === "queued" && (
          <>
            <circle className="st-ring" cx="12" cy="12" r="8" strokeWidth={2} />
            <path d="M12 8v4.2l2.6 1.6" strokeWidth={2} />
          </>
        )}
        {status === "running" && (
          <>
            {/* plan-128-518：运行态 = 左侧会话执行中同款转圈（细底环 25% + 单段实色弧，线性匀速） */}
            <circle cx="12" cy="12" r="8" strokeWidth={2} strokeOpacity={0.25} />
            <path className="st-arc" d="M12 4a8 8 0 0 1 8 8" strokeWidth={2} />
          </>
        )}
        {status === "done" && (
          <>
            <circle className="st-core" cx="12" cy="12" r="9" fill="currentColor" stroke="none" />
            <path className="st-mark" d="M8 12.3l2.7 2.7 5.4-5.6" stroke="var(--st-on, #fff)" strokeWidth={2.2} pathLength={1} />
          </>
        )}
        {status === "failed" && (
          <>
            <circle className="st-core" cx="12" cy="12" r="9" fill="currentColor" stroke="none" />
            <path className="st-mark" d="M9 9l6 6M15 9l-6 6" stroke="var(--st-on, #fff)" strokeWidth={2.2} pathLength={1} />
          </>
        )}
        {(status === "cancelled" || status === "paused" || status === "interrupted") && (
          <>
            <circle className="st-ring" cx="12" cy="12" r="8" strokeWidth={2} />
            <path d="M10 9.2v5.6M14 9.2v5.6" strokeWidth={2} />
          </>
        )}
      </svg>
    </span>
  );
});
