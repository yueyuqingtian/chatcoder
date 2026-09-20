/** 设置中心：索引库（plan-248-1258 M3.4）。
 *
 * 对齐 codegraph 体验：每个工作目录独立开关（默认关闭），开启后后台自动建立
 * 代码符号索引；文件变更后自动增量更新。AI 会话中会感知索引状态并主动使用
 * symbol_search / outline。
 *
 * 页面能力：项目列表（状态徽标 + 文件/符号数 + 更新时间）、开关、重建、
 * 实时进度（WS symbol_index.progress）、符号检索预览。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { IconRefresh, IconSearch, IconZap } from "../icons";
import { Input, Select } from "../ui";
import { isIndexBusy, progressText, resolveSearchTarget } from "./indexProgress";

export interface WorkspaceStat {
  workspace: string;
  name: string;
  enabled: boolean;
  status: string;          // off | queued | scanning | parsing | indexing | ready | cancelled | error
  files: number;
  symbols: number;
  last_updated: number | null;
  progress: number;
  error: string | null;
  exists?: boolean;
  /** 进行中的实时计数（后端仅在索引中返回非 0） */
  files_scanned?: number;
  files_total?: number;
}

/** 进行中状态集合与计数文案见 ./indexProgress（纯逻辑，便于 Node 测试直接校验）。 */

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
  queued: "排队中",
  scanning: "扫描中",
  parsing: "解析中",
  indexing: "索引中",
  ready: "已就绪",
  cancelled: "已取消",
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
  // 当前项目路径：用于把检索目标默认落到"当前项目"而非项目列表第一项
  const projects = useChatStore((s) => s.projects);
  const currentProjectId = useChatStore((s) => s.currentProjectId);
  const currentProjectPath = projects.find((p) => p.id === currentProjectId)?.path ?? null;
  const currentProjectPathRef = useRef<string | null>(currentProjectPath);
  currentProjectPathRef.current = currentProjectPath;

  const load = useCallback(async () => {
    try {
      const r = await api.symbolIndexWorkspaces();
      setItems(r.workspaces);
      // 检索目标必须落在 enabled 项内（下拉框只列 enabled）。
      // 旧逻辑仅判断"仍在工作区列表"就保留旧值，导致先打开页面时
      // target 落到首个项目（未启用），开启其它项目后 target 仍指向它——
      // 下拉框无匹配项而显示第一项，于是"显示 A、实际检索 B"。
      setTarget((cur) => resolveSearchTarget(cur, r.workspaces, currentProjectPathRef.current));
    } catch (e) {
      useChatStore.setState({ error: String(e) });
    }
  }, []);
  useEffect(() => { void load(); }, [load]);

  // 切换当前项目（或项目列表变化）时，把检索目标同步到当前项目（若已启用索引）
  useEffect(() => {
    setTarget((cur) => resolveSearchTarget(cur, items, currentProjectPath));
    // items 变化由上方 load/setItems 驱动；此处仅在项目上下文变化时纠偏
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentProjectPath]);

  // 实时进度：直接消费 WS 广播的 payload 就地更新对应工作区，
  // 避免每帧（约 0.35s）都全量重拉一次 /workspaces。
  useEffect(() => {
    const handler = (e: Event) => {
      const p = (e as CustomEvent<Record<string, unknown>>).detail;
      const ws = typeof p?.workspace === "string" ? p.workspace : null;
      if (!ws || typeof p.status !== "string") { void load(); return; }
      setItems((prev) => {
        let hit = false;
        const next = prev.map((w) => {
          if (w.workspace !== ws) return w;
          hit = true;
          return {
            ...w,
            status: p.status as string,
            progress: Number(p.progress ?? w.progress) || 0,
            files: Number(p.files ?? w.files) || 0,
            symbols: Number(p.symbols ?? w.symbols) || 0,
            files_scanned: Number(p.files_scanned ?? 0),
            files_total: Number(p.files_total ?? 0),
            error: (p.error as string | null) ?? null,
          };
        });
        // 未命中（如新开工作区）才回退到整表刷新
        if (!hit) { void load(); return prev; }
        // 结束态（就绪/取消/异常）时补拉一次，同步 files/symbols/时间戳等终值
        if (p.status === "ready" || p.status === "cancelled" || p.status === "error") {
          window.setTimeout(() => { void load(); }, 60);
        }
        return next;
      });
    };
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

  const enabledItems = items.filter((w) => w.enabled);
  // 渲染期兜底：target 必须命中某个 enabled 选项，否则 <select> 会显示第一项
  // 而请求仍发往原 target —— 正是"显示 A、实际检索 B"的成因。
  // 此处以实际匹配到的工作区为准发起请求，保证所见即所查。
  const effectiveTarget = enabledItems.find((w) => w.workspace === target)?.workspace
    ?? enabledItems[0]?.workspace
    ?? null;

  const runSearch = async () => {
    if (!effectiveTarget || !query.trim()) return;
    setSearching(true);
    try {
      const r = await api.symbolIndexSearch(effectiveTarget, query.trim());
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
                {isIndexBusy(w.status) ? (
                <div className="idx-progress">
                  <div className="idx-progress-bar">
                    <div className="idx-progress-fill" style={{ width: `${Math.max(2, Math.min(100, w.progress || 0))}%` }} />
                  </div>
                  <div className="idx-progress-meta">
                    <span className="idx-progress-pct">{w.progress || 0}%</span>
                    {/* 两阶段计数语义不同：扫描期只报发现数（总量未知），
                        解析期才有总数（此时计数从 0 重新开始）。 */}
                    <span>{progressText(w)}</span>
                  </div>
                </div>
              ) : (
                <>
                  <span>{w.files} 文件</span>
                  <span>·</span>
                  <span>{w.symbols} 符号</span>
                  <span>·</span>
                  <span>更新于 {fmtTime(w.last_updated)}</span>
                </>
              )}
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
                <button
                  className="btn btn-ghost btn-xs"
                  disabled={busy === w.workspace || isIndexBusy(w.status)}
                  onClick={() => void rebuild(w)}
                >
                  <IconRefresh size={12} /> {isIndexBusy(w.status) ? "重建中…" : "重建"}
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
          <Select
            value={effectiveTarget ?? ""}
            onChange={(v) => { setTarget(v); setHits(null); }}
            disabled={enabledItems.length === 0}
            options={enabledItems.map((w) => ({ value: w.workspace, label: w.name || w.workspace }))}
            aria-label="检索目标索引库"
          />
          <Input
            placeholder="函数名 / 类名（支持部分匹配）"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") void runSearch(); }}
          />
          <button className="btn btn-primary btn-sm" disabled={!effectiveTarget || !query.trim() || searching} onClick={() => void runSearch()}>
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
