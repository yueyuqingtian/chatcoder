import { forwardRef } from "react";
import { Button, Select, Switch, Tooltip } from "../ui";
import { IconMessageSquare, IconFileText } from "../icons";
import { type MentionItem, type useMentionCandidates } from "./useMentionCandidates";

type Props = { model: ReturnType<typeof useMentionCandidates>; index: number; listId: string;
  onIndex: (index: number) => void; onPick: (item: MentionItem) => void; onClose: () => void; onFocusInput: () => void };

/** plan-20261104-session-mentions：选择器保留输入焦点，内容分组与状态分区独立展示。 */
export const MentionMenu = forwardRef<HTMLDivElement, Props>(function MentionMenu({ model: m, index, listId, onIndex, onPick, onClose, onFocusInput }, ref) {
  const renderItem = (item: MentionItem, i: number) => {
    const session = item.kind === "session" ? item.session : null;
    const path = item.kind === "file" ? item.path : "";
    const title = session ? session.title || "未命名会话" : path.split("/").pop() || path;
    const kind = session?.side_parent_id ? session.side_kind === "side" ? "侧边会话" : "历史子会话" : "主会话";
    const detail = session ? `${session.project_name || "无项目"} · ${kind}${session.parent_title ? ` · ${session.parent_title}` : ""} · #${session.id}`
      : path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "项目根目录";
    return <Tooltip key={session ? `session-${session.id}` : `file-${path}`} content={`${title}\n${detail}`}>
      <Button variant="ghost" size="sm" block role="option" aria-selected={i === index} id={`${listId}-${i}`}
        className={`mention-option${i === index ? " active" : ""}`} onMouseEnter={() => onIndex(i)} onClick={() => onPick(item)}>
        <span className="mention-option-icon">{session ? <IconMessageSquare size={16} /> : <IconFileText size={16} />}</span>
        <span className="mention-option-copy"><strong>{title}</strong><span>{detail}</span></span>
        {session?.status === "archived" && <span className="mention-badge">已归档</span>}
        {session && <time className="mention-time">{session.last_activity_at ? new Date(session.last_activity_at.replace(" ", "T") + (/Z$|[+-]\d\d:\d\d$/.test(session.last_activity_at) ? "" : "Z")).toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" }) : ""}</time>}
      </Button>
    </Tooltip>;
  };
  const empty = (type: "session" | "file") => {
    const loading = type === "session" ? m.sessionLoading : m.fileLoading;
    const error = type === "session" ? m.sessionError : m.fileError;
    const count = type === "session" ? m.sessionItems.length : m.fileItems.length;
    return <div className="mention-status" role="status">{error ? <>{error}<Button size="xs" onClick={type === "session" ? m.retrySessions : m.retryFiles}>重试</Button></>
      : loading ? "正在查找…" : count === 0 ? `未找到匹配${type === "session" ? "会话" : "文件"}` : null}</div>;
  };
  return <div className="mention-menu" role="dialog" aria-label="引用上下文"
    onMouseDown={(e) => { if (!(e.target as HTMLElement).closest('[role="switch"], [role="combobox"]')) e.preventDefault(); }}
    onKeyDown={(e) => { if (e.key === "Escape") { e.stopPropagation(); onClose(); } }}>
    <div className="mention-heading"><strong>引用上下文</strong><span>按需读取，保持对话简洁</span></div>
    <div className="mention-filters">
      <div className="mention-categories" role="group" aria-label="引用类型">
        {([['all', '全部'], ['session', '会话'], ['file', '文件']] as const).map(([value, label]) =>
          <Button key={value} size="xs" variant={m.category === value ? "subtle" : "ghost"} aria-pressed={m.category === value}
            onClick={() => { m.setCategory(value); onIndex(0); }}>{label}</Button>)}
      </div>
      {m.category !== "file" && <Select size="sm" aria-label="会话项目范围" value={m.allProjects ? "all" : "current"}
        onChange={(v) => { m.setAllProjects(v === "all"); onIndex(0); onFocusInput(); }} options={[{ value: "current", label: "当前项目" }, { value: "all", label: "全部项目" }]} />}
    </div>
    {m.category !== "file" && <div className="mention-archive"><span>显示归档</span>
      <Switch aria-label="显示归档会话" checked={m.archived} onChange={(v) => { m.setArchived(v); onIndex(0); onFocusInput(); }} /></div>}
    <div className="mention-list" ref={ref} role="listbox" id={listId} aria-label="引用候选">
      {m.category !== "file" && <><div className="mention-group-heading">会话</div>
        {m.sessionItems.map((item, i) => renderItem(item, i))}{empty("session")}
        {m.moreSessions && <Button size="xs" className="mention-more" disabled={m.sessionLoading} onClick={m.loadSessions}>{m.category === "all" ? "查看更多会话" : "加载更多会话"}</Button>}</>}
      {m.category !== "session" && <><div className="mention-group-heading">文件</div>
        {m.fileItems.map((item, i) => renderItem(item, m.sessionItems.length + i))}{empty("file")}
        {m.moreFiles && <Button size="xs" className="mention-more" disabled={m.fileLoading} onClick={m.loadFiles}>{m.category === "all" ? "查看更多文件" : "加载更多文件"}</Button>}</>}
    </div>
    <div className="mention-footer">↑↓ 选择 <span>Enter / Tab 引用</span><span>Esc 关闭</span></div>
  </div>;
});
