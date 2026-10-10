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
        {status === "pending" && <circle className="st-ring" cx="12" cy="12" r="8.25" strokeWidth={1.9} strokeDasharray="2.6 3.3" />}
        {status === "queued" && (
          <>
            <circle className="st-ring" cx="12" cy="12" r="8.25" strokeWidth={1.9} />
            <path d="M12 8v4.2l2.6 1.6" strokeWidth={1.9} />
          </>
        )}
        {status === "running" && (
          <>
            <circle cx="12" cy="12" r="8.25" strokeWidth={2} opacity={0.22} />
            <circle className="st-arc" cx="12" cy="12" r="8.25" strokeWidth={2.2} strokeDasharray="13 52" />
            <circle className="st-pulse" cx="12" cy="12" r="2.6" fill="currentColor" stroke="none" />
          </>
        )}
        {status === "done" && (
          <>
            <circle className="st-core" cx="12" cy="12" r="9.5" fill="currentColor" stroke="none" />
            <path className="st-mark" d="M7.8 12.4l2.8 2.8 5.6-5.8" stroke="var(--st-on, #fff)" strokeWidth={2.3} pathLength={1} />
          </>
        )}
        {status === "failed" && (
          <>
            <circle className="st-core" cx="12" cy="12" r="9.5" fill="currentColor" stroke="none" />
            <path className="st-mark" d="M9 9l6 6M15 9l-6 6" stroke="var(--st-on, #fff)" strokeWidth={2.3} pathLength={1} />
          </>
        )}
        {(status === "cancelled" || status === "paused" || status === "interrupted") && (
          <>
            <circle className="st-ring" cx="12" cy="12" r="8.25" strokeWidth={1.9} />
            <path d="M10 9.2v5.6M14 9.2v5.6" strokeWidth={2.1} />
          </>
        )}
      </svg>
    </span>
  );
});
