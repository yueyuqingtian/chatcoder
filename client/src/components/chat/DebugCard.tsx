/** DebugCard —— 消息流尾部的"调试进行中"卡片（plan-282-1441 #8）。
 *
 * 需求里最关键的一条可见性要求：**让用户在 AI 调试时看到断点进行到哪一行代码**。
 * 调试面板在右侧提供完整控制，这张卡片在消息流里给出即时的现场快照——
 * 用户读对话时就能看到"已暂停：某文件:128（handleSubmit）"，不必切面板。
 *
 * 数据来源：store.debugState（由 debug.paused 事件写入，见 store/chat.ts）。
 */
import { useState } from "react";
import type { DebugStatusOut } from "../../api/client";
import { IconBug, IconChevronRight } from "../icons";
import { Collapse } from "../ui";

export function DebugCard({ status }: { status: DebugStatusOut }) {
  const [expanded, setExpanded] = useState(true);
  const targetLabel = status.target === "java" ? "Java（JDWP）" : "Web（CDP）";
  const location = status.line != null
    ? `${status.file || "(未知文件)"}:${status.line}`
    : status.file || "";

  return (
    <div className={`debug-card${status.paused ? " paused" : ""}`}>
      <button
        type="button"
        className="debug-card-head"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
      >
        <span className={`debug-card-dot${status.paused ? " paused" : ""}`} />
        <IconBug size={12} />
        <span className="debug-card-title">
          {status.paused ? "调试已暂停" : "调试运行中"}
        </span>
        <span className="debug-card-target">{targetLabel}</span>
        {location && <code className="debug-card-loc">{location}</code>}
        {status.function && <span className="debug-card-fn">{status.function}</span>}
        <span className="debug-card-meta">
          断点 {status.breakpoints} · 命中 {status.hitCount}
        </span>
        <span className={`debug-card-caret${expanded ? " open" : ""}`}>
          <IconChevronRight size={12} />
        </span>
      </button>

      <Collapse open={expanded}>
        <div className="debug-card-body">
          {status.stack.length > 0 && (
            <div className="debug-card-block">
              <div className="debug-card-block-title">调用栈</div>
              {status.stack.slice(0, 8).map((f, i) => (
                <div className="debug-card-stack" key={i}>
                  <span className="debug-card-idx">#{i}</span>
                  <span className="debug-card-fn-name">{f.function || "(anonymous)"}</span>
                  <span className="debug-card-fn-loc">
                    {f.url || ""}{f.line != null ? `:${f.line}` : (f.index != null ? `@${f.index}` : "")}
                  </span>
                </div>
              ))}
            </div>
          )}
          {status.variables.length > 0 && (
            <div className="debug-card-block">
              <div className="debug-card-block-title">变量（{status.variables.length}）</div>
              <div className="debug-card-vars">
                {status.variables.slice(0, 24).map((v, i) => (
                  <div className="debug-card-var" key={i}>
                    <code>{v.name}</code>
                    <span className="debug-card-var-eq">=</span>
                    <span className="debug-card-var-val">{String(v.value ?? "")}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
          {status.stack.length === 0 && status.variables.length === 0 && (
            <div className="debug-card-empty">已连接调试会话，等待命中断点…</div>
          )}
          <div className="debug-card-tip">
            可在「设置 → 拓展 → 连接器 → 开发调试」或用 <code>/</code> 命令控制调试（继续 / 跳过 / 步入 / 跳出）
          </div>
        </div>
      </Collapse>
    </div>
  );
}
