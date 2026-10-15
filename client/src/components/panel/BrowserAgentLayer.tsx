/**
 * 浏览器面板 · AI 操作可视化层（plan-20261015-browser-agent）。
 *
 * 叠在网页视口之上、不拦截指针（只有胶囊里的按钮可点），不挤占页面空间——
 * 出现 / 消失都不会让网页重新排版（AI 正在用的坐标不会因此失效）。
 *
 * - AI 光标：指针图形 + “AI”签，平移到目标点；点击时一次性波纹；
 * - 目标高亮：被点击 / 输入的元素矩形浅色底，无描边；
 * - 截图：一次性闪白；
 * - 状态胶囊（视口底部居中）：正在做什么 · 第几步 · 停止；结束后短暂显示「已完成 N 步」。
 * 设计约束：无装饰描边、无循环动画，进出场与位移走 --dur-* 令牌（含 --motion-scale）。
 */
import { useEffect, useState } from "react";
import { useBrowserStore, type AgentActivity, type PageDialog } from "../../store/browser";
import { useChatStore } from "../../store/chat";
import { Button, Input } from "../ui";
import { IconSparkles, IconStop, IconCheck } from "../icons";

/** 指针图形：16px / 24 网格 / 整数坐标（用户屏幕 1.5 倍缩放下保持锐利） */
function CursorGlyph() {
  return (
    <svg className="bpa-cursor-glyph" width="18" height="18" viewBox="0 0 24 24" aria-hidden>
      <path d="M5 3l14 8-6 2-3 7z" fill="var(--accent)" stroke="var(--bg-elevated)" strokeWidth="1.6" strokeLinejoin="round" />
    </svg>
  );
}

export function BrowserAgentOverlay({ agent, tabId, shown }: { agent: AgentActivity; tabId: string; shown: boolean }) {
  const mine = shown && agent.tabId === tabId && agent.phase !== "idle";
  // 波纹 / 闪白按序号播放一次：序号变化时重挂载元素即可触发 CSS 动画
  const [rippleKey, setRippleKey] = useState(0);
  const [flashKey, setFlashKey] = useState(0);
  useEffect(() => { if (agent.ripple) setRippleKey(agent.ripple.seq); }, [agent.ripple]);
  useEffect(() => { if (agent.flash) setFlashKey(agent.flash); }, [agent.flash]);
  if (!mine) return null;
  const working = agent.phase === "working";
  return (
    <div className="bpa-layer" aria-hidden>
      {agent.highlight && working && (
        <div
          key={agent.highlight.seq}
          className="bpa-highlight"
          style={{ transform: `translate(${agent.highlight.x}px, ${agent.highlight.y}px)`, width: agent.highlight.width, height: agent.highlight.height }}
        />
      )}
      {agent.ripple && rippleKey === agent.ripple.seq && (
        <span key={`r${rippleKey}`} className="bpa-ripple" style={{ left: agent.ripple.x, top: agent.ripple.y }} />
      )}
      {agent.cursor && (
        <div className={`bpa-cursor${working ? "" : " is-resting"}`} style={{ transform: `translate(${agent.cursor.x}px, ${agent.cursor.y}px)` }}>
          <CursorGlyph />
          <span className="bpa-cursor-tag">AI</span>
        </div>
      )}
      {flashKey > 0 && agent.flash === flashKey && <span key={`f${flashKey}`} className="bpa-flash" />}
    </div>
  );
}

/** 视口底部状态胶囊：AI 正在做什么、第几步、停止。 */
export function BrowserAgentCapsule({ agent, onReveal }: { agent: AgentActivity; onReveal?: () => void }) {
  const running = useChatStore((s) => s.isRunning);
  const cancelTurn = useChatStore((s) => s.cancelTurn);
  if (agent.phase === "idle") return null;
  const working = agent.phase === "working";
  return (
    <div className={`bpa-capsule${working ? " is-working" : " is-done"}`} role="status" aria-live="polite">
      <span className="bpa-capsule-ic">{working ? <IconSparkles size={14} /> : <IconCheck size={14} />}</span>
      <span className="bpa-capsule-main" onClick={onReveal}>
        <span className="bpa-capsule-who">{working ? "AI 正在操作" : "AI 已完成操作"}</span>
        <span className="bpa-capsule-label">{working ? agent.label : `共 ${agent.steps} 步`}</span>
      </span>
      {working && agent.steps > 0 && <span className="bpa-capsule-step">第 {agent.steps} 步</span>}
      {working && running && (
        <Button size="xs" variant="ghost" className="bpa-capsule-stop" onClick={() => void cancelTurn()}>
          <IconStop size={12} />停止
        </Button>
      )}
    </div>
  );
}

/** AI 在另一个标签里工作（用户正在看自己的标签，没有被强行切走）时的提示条。 */
export function BrowserAgentElsewhere({ title, onView, onDismiss }: { title: string; onView: () => void; onDismiss: () => void }) {
  return (
    <div className="bpa-elsewhere" role="status">
      <IconSparkles size={13} />
      <span className="bpa-elsewhere-text">AI 正在「{title || "新标签"}」中操作</span>
      <Button size="xs" variant="ghost" onClick={onView}>查看</Button>
      <Button size="xs" variant="ghost" onClick={onDismiss}>忽略</Button>
    </div>
  );
}

/** 网页弹窗（alert / confirm / prompt / 离开确认）：以应用内对话框询问用户，不弹系统原生窗口。 */
export function BrowserPageDialog({ sessionId, dialog }: { sessionId: number | null; dialog: PageDialog }) {
  const setDialog = useBrowserStore((s) => s.setDialog);
  const [text, setText] = useState(dialog.defaultPrompt);
  const reply = (accept: boolean) => {
    setDialog(sessionId, null);
    void window.chatcoderAPI?.browserAgentExec?.(dialog.wcId, "dialogReply", { accept, prompt_text: text });
  };
  const title = dialog.type === "confirm" ? "网页请求确认"
    : dialog.type === "prompt" ? "网页请求输入"
      : dialog.type === "beforeunload" ? "离开此页面？"
        : "网页提示";
  return (
    <div className="bpa-dialog-scrim">
      <div className="bpa-dialog" role="alertdialog" aria-label={title}>
        <div className="bpa-dialog-title">{title}</div>
        <div className="bpa-dialog-msg">{dialog.message || (dialog.type === "beforeunload" ? "页面上的修改可能不会保存。" : "")}</div>
        {dialog.type === "prompt" && (
          <Input
            size="sm"
            className="bpa-dialog-input"
            value={text}
            autoFocus
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") reply(true); if (e.key === "Escape") reply(false); }}
          />
        )}
        <div className="bpa-dialog-actions">
          {dialog.type !== "alert" && <Button size="sm" variant="ghost" onClick={() => reply(false)}>取消</Button>}
          <Button size="sm" variant="primary" onClick={() => reply(true)} autoFocus={dialog.type !== "prompt"}>
            {dialog.type === "beforeunload" ? "离开" : "确定"}
          </Button>
        </div>
      </div>
    </div>
  );
}
