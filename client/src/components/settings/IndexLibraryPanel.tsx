/** 设置中心：索引库（plan-248-1258 M3.4）。
 *
 * 对齐 codegraph 体验：每个工作目录独立开关（默认关闭），开启后后台自动建立
 * 代码符号索引；文件变更后自动增量更新。AI 会话中会感知索引状态并主动使用
 * symbol_search / outline。
 *
 * 页面能力：项目列表（状态徽标 + 文件/符号数 + 更新时间）、开关、重建、
 * 实时进度（WS symbol_index.progress）、符号检索预览。
 */
import { useCallback, useEffect, useState } from "react";
import { api } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { IconRefresh, IconSearch, IconZap } from "../icons";

interface WorkspaceStat {
  workspace: string;
  name: string;
  enabled: boolean;
  status: string;          // off | indexing | ready | error
  files: number;
  symbols: number;
  last_updated: number | null;
  progress: number;
  error: string | null;
  exists?: boolean;
}

interface SearchHit {
  file_path: string;
  kind: string;
  name: string;
  qualified_name?: string | null;
  line_start: number;
  line_end: number;
  signature?: string | null;
}

const STATUS_LABEL: Record<string, string> = {
  off: "未启用",
  indexing: "索引中",
  ready: "已就绪",
  error: "异常",
};

function fmtTime(ts: number | null): string {
  if (!ts) return "-";
  try {
    return new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false });
  } catch {
    return "-";
  }
}

export function IndexLibraryPanel() {
  const [items, setItems] = useState<WorkspaceStat[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [target, setTarget] = useState<string | null>(null);
  const [hits, setHits] = useState<SearchHit[] | null>(null);
  const [searching, setSearching] = useState(false);

  const load = useCallback(async () => {
    try {
      const r = await api.symbolIndexWorkspaces();
      setItems(r.workspaces);
      setTarget((cur) => (cur && r.workspaces.some((w) => w.workspace === cur)
        ? cur
        : (r.workspaces.find((w) => w.enabled)?.workspace ?? r.workspaces[0]?.workspace ?? null)));
    } catch (e) {
      useChatStore.setState({ error: String(e) });
    }
  }, []);
  useEffect(() => { void load(); }, [load]);

  // plan-248-1258 M3.4: 索引进度实时刷新（索引是后台任务，完成后需刷新状态）
  useEffect(() => {
    const handler = () => { void load(); };
    window.addEventListener("chatcoder:symbol-index-progress", handler);
    return () => window.removeEventListener("chatcoder:symbol-index-progress", handler);
  }, [load]);

  const toggle = async (w: WorkspaceStat, on: boolean) => {
    setBusy(w.workspace);
    try {
      if (on) await api.symbolIndexEnable(w.workspace);
      else await api.symbolIndexDisable(w.workspace);
      await load();
    } catch (e) { useChatStore.setState({ error: String(e) }); }
    finally { setBusy(null); }
  };

  const rebuild = async (w: WorkspaceStat) => {
    setBusy(w.workspace);
    try {
      await api.symbolIndexRebuild(w.workspace);
      await load();
    } catch (e) { useChatStore.setState({ error: String(e) }); }
    finally { setBusy(null); }
  };

  const runSearch = async () => {
    if (!target || !query.trim()) return;
    setSearching(true);
    try {
      const r = await api.symbolIndexSearch(target, query.trim());
      setHits(r.hits);
    } catch (e) { useChatStore.setState({ error: String(e) }); }
    finally { setSearching(false); }
  };

  return (
    <div>
      <div className="sched-note">
        为每个工作目录单独开启代码符号索引（默认关闭）。开启后会自动建立并持续更新，
        AI 在会话中即可用 symbol_search / outline 快速定位函数、类与文件结构；
        改动文件后索引会自动增量更新。
      </div>

      <div className="idx-list">
        {items.map((w) => (
          <div key={w.workspace} className="idx-item">
            <div className="idx-item-main">
              <div className="idx-item-title">
                <span className={"idx-dot " + w.status} />
                <span className="idx-name" title={w.workspace}>{w.name || w.workspace}</span>
                <span className={"idx-badge " + w.status}>{STATUS_LABEL[w.status] || w.status}</span>
                {w.exists === false && <span className="idx-badge error">目录不存在</span>}
              </div>
              <div className="idx-item-desc">
                <span className="idx-path" title={w.workspace}>{w.workspace}</span>
              </div>
              <div className="idx-item-desc">
                {w.status === "indexing"
                  ? <span>正在索引… {w.progress}%</span>
                  : <>
                      <span>{w.files} 文件</span>
                      <span>·</span>
                      <span>{w.symbols} 符号</span>
                      <span>·</span>
                      <span>更新于 {fmtTime(w.last_updated)}</span>
                    </>}
                {w.error && <span className="idx-err">{w.error}</span>}
              </div>
            </div>
            <div className="idx-item-actions">
              <button
                className="btn btn-ghost btn-xs"
                disabled={busy === w.workspace || w.exists === false}
                onClick={() => void toggle(w, !w.enabled)}
              >
                {busy === w.workspace ? "处理中…" : w.enabled ? "关闭索引" : "开启索引"}
              </button>
              {w.enabled && (
                <button className="btn btn-ghost btn-xs" disabled={busy === w.workspace} onClick={() => void rebuild(w)}>
                  <IconRefresh size={12} /> 重建
                </button>
              )}
            </div>
          </div>
        ))}
        {items.length === 0 && <div className="navpage-empty">暂无项目。创建项目后即可在此开启索引。</div>}
      </div>

      <div className="idx-search">
        <div className="idx-search-title">符号检索预览</div>
        <div className="idx-search-row">
          <select className="ui-select" value={target ?? ""} onChange={(e) => { setTarget(e.target.value); setHits(null); }}>
            {items.filter((w) => w.enabled).map((w) => (
              <option key={w.workspace} value={w.workspace}>{w.name || w.workspace}</option>
            ))}
          </select>
          <input
            className="ui-input"
            placeholder="函数名 / 类名（支持部分匹配）"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") void runSearch(); }}
          />
          <button className="btn btn-primary btn-sm" disabled={!target || !query.trim() || searching} onClick={() => void runSearch()}>
            <IconSearch size={13} /> {searching ? "检索中…" : "检索"}
          </button>
        </div>
        {hits && (
          <div className="idx-hits">
            {hits.length === 0 && <div className="navpage-empty">未找到匹配符号</div>}
            {hits.map((h, i) => (
              <div key={i} className="idx-hit">
                <span className="idx-hit-kind">{h.kind}</span>
                <span className="idx-hit-name">{h.name}</span>
                <span className="idx-hit-loc">{h.file_path}:{h.line_start}-{h.line_end}</span>
              </div>
            ))}
          </div>
        )}
        {!items.some((w) => w.enabled) && (
          <div className="idx-hint"><IconZap size={12} /> 先开启至少一个项目的索引，然后即可检索符号。</div>
        )}
      </div>
    </div>
  );
}
