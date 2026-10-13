/** 设置中心：记忆（v2.2 对齐 zcode 3.18；plan-230-1144 M4.1 三层视图；S8 重构 plan-41-197）。
 *
 * 三层化后记忆分 global（跨项目规范）/ project（项目约定）/ session（会话事实），
 * 本面板按作用域分组展示，支持：
 * - scope 页签过滤（全部/全局/项目/会话）；
 * - 候选区标记（低置信、未注入 prompt，可被 memory_search 检索）；
 * - 提升/降级（把会话记忆升级为项目/全局，或反向）；
 * - 过期时间与使用次数展示。
 *
 * S8（plan-41-197）重构：
 * - 列表每项只展示**一行摘要**（此前全文直出，满版文字难以浏览）；
 * - 顶部工具栏（作用域筛选/候选开关/批量操作）**吸顶固定**，列表可独立滑动；
 * - 点击行打开详情弹窗：查看全文、编辑内容（编辑即人工确认）、提升/降级、删除；
 * - 批量管理：多选 + 全选 + 批量删除（破坏性操作走确认弹窗）。
 */
import { useCallback, useEffect, useState } from "react";
import { api, type MemoryEntryOut } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { parseServerTime } from "../../utils/time";
import { IconArrowUp, IconPencil, IconRefresh, IconSearch, IconTrash } from "../icons";
import { ConfirmDialog } from "../ConfirmDialog";
import { Modal } from "../Modal";
import { Button, CardRow, Checkbox, Empty, Input, PageActions, Section, Segmented, Tooltip } from "../ui";
import { Sw } from "./shared";

type ScopeFilter = "all" | "global" | "project" | "session";

const SCOPE_META: Record<string, { label: string; color: string; desc: string }> = {
  global: { label: "全局", color: "var(--info)", desc: "跨项目通用规范/偏好，永不过期" },
  project: { label: "项目", color: "var(--success)", desc: "本项目约定/坑点，永不过期" },
  session: { label: "会话", color: "var(--text-2)", desc: "本会话事实，30 天过期" },
};

export function MemoryPanel() {
  const [memoryEnabled, setMemoryEnabled] = useState(true);
  const [items, setItems] = useState<MemoryEntryOut[]>([]);
  const [scopeFilter, setScopeFilter] = useState<ScopeFilter>("all");
  const [includeCandidate, setIncludeCandidate] = useState(true);
  const [confirmTarget, setConfirmTarget] = useState<MemoryEntryOut | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [err, setErr] = useState<string | null>(null);
  // S8：批量管理
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [confirmBatch, setConfirmBatch] = useState(false);
  // S8：详情弹窗（查看 + 编辑）
  const [detail, setDetail] = useState<MemoryEntryOut | null>(null);
  const [detailText, setDetailText] = useState("");
  const [detailSaving, setDetailSaving] = useState(false);
  // plan-91-393：本地关键词过滤（纯前端，不改变后端查询口径）
  const [query, setQuery] = useState("");

  const loadSettings = useCallback(async () => {
    try {
      const g = await api.getGlobalSettings();
      setMemoryEnabled(g.memory_enabled !== false);
    } catch {}
  }, []);

  const load = useCallback(async () => {
    try {
      const list = await api.listMemories({
        scope: scopeFilter === "all" ? undefined : scopeFilter,
        includeCandidate,
      });
      setItems(list);
      // 清理已不存在项的选中态（换作用域/刷新后避免"幽灵选中"）
      setSelected((prev) => {
        if (prev.size === 0) return prev;
        const alive = new Set(list.map((m) => m.id));
        const next = new Set([...prev].filter((id) => alive.has(id)));
        return next.size === prev.size ? prev : next;
      });
    } catch (e) { setErr(String(e)); }
  }, [scopeFilter, includeCandidate]);

  useEffect(() => { loadSettings(); }, [loadSettings]);
  useEffect(() => { void load(); }, [load]);

  const handleToggle = async (val: boolean) => {
    setMemoryEnabled(val);
    try {
      await api.setGlobalSettings({ memory_enabled: val });
    } catch (e) {
      useChatStore.setState({ error: "保存失败: " + String(e) });
      setMemoryEnabled(!val);
    }
  };

  const promote = async (m: MemoryEntryOut, target: "session" | "project" | "global") => {
    setBusyId(m.id); setErr(null);
    try {
      await api.promoteMemory(m.id, target);
      await load();
    } catch (e) { setErr(String(e)); }
    finally { setBusyId(null); }
  };

  const doDelete = async () => {
    const t = confirmTarget;
    setConfirmTarget(null);
    if (!t) return;
    try { await api.deleteMemory(t.id); await load(); }
    catch (e) { setErr(String(e)); }
  };

  /** S8：批量删除（逐条调用后端，保持与单项删除同一语义与审计口径） */
  const doBatchDelete = async () => {
    setConfirmBatch(false);
    const ids = Array.from(selected);
    if (ids.length === 0) return;
    try {
      for (const id of ids) await api.deleteMemory(id);
      setSelected(new Set());
      await load();
    } catch (e) { setErr(String(e)); }
  };

  const toggleSelect = (id: number) => setSelected((prev) => {
    const next = new Set(prev);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });
  const allSelected = items.length > 0 && items.every((m) => selected.has(m.id));
  const toggleSelectAll = () => setSelected(allSelected ? new Set() : new Set(items.map((m) => m.id)));

  /** S8：打开详情（回填可编辑文本） */
  const openDetail = (m: MemoryEntryOut) => {
    setDetail(m);
    setDetailText(m.text || "");
  };

  /** S8：保存详情编辑——内容有变化才请求；后端将清除候选标记 */
  const saveDetail = async () => {
    if (!detail) return;
    const text = detailText.trim();
    if (!text) { setErr("记忆内容不能为空"); return; }
    setDetailSaving(true);
    try {
      if (text !== detail.text) await api.updateMemory(detail.id, { text });
      setDetail(null);
      await load();
    } catch (e) { setErr(String(e)); }
    finally { setDetailSaving(false); }
  };

  const q = query.trim().toLowerCase();
  const visible = q ? items.filter((m) => (m.text || "").toLowerCase().includes(q) || (m.kind || "").toLowerCase().includes(q)) : items;
  const fmtTime = (iso?: string | null) => {
    if (!iso) return "";
    const t = parseServerTime(iso);
    return Number.isNaN(t) ? "" : new Date(t).toLocaleString(undefined, { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
  };
  // plan-91-393：空态统一为「图标 + 主文案 + 次文案」，搜索无结果与真正无记忆区分提示
  // plan-114-450：统一空状态（嗒嗒）——搜索无结果用「思考」表情
  const emptyState = (title: string) => (
    <Empty
      mood={q ? "think" : "calm"}
      title={q ? "没有匹配的记忆" : title}
      desc={q ? "换个关键词试试" : "对话里嗒嗒记下的事实与偏好会出现在这里"}
    />
  );

  // 按作用域分组（all 视图）或在过滤视图下平铺
  const grouped = (() => {
    const order: Array<"global" | "project" | "session"> = ["global", "project", "session"];
    const map: Record<string, MemoryEntryOut[]> = { global: [], project: [], session: [] };
    for (const m of visible) {
      const key = (m.scope as string) || "session";
      (map[key] || map.session).push(m);
    }
    return order.map((s) => ({ scope: s, list: map[s] })).filter((g) => g.list.length > 0);
  })();

  /** S8：单行摘要行——点击信息区打开详情，复选框用于批量管理；
   *  plan-91-393：改为卡片行（作用域角标 + 正文 + 元信息），操作悬停显现，选中态用底色而非边线 */
  const renderItem = (m: MemoryEntryOut) => {
    const scopeKey = (m.scope as string) || "session";
    const meta = SCOPE_META[scopeKey] || SCOPE_META.session;
    const checked = selected.has(m.id);
    const time = fmtTime(m.generated_at);
    return (
      <div key={m.id} className={"mem-card" + (checked ? " selected" : "")}>
        <span className="mem-card-check"><Checkbox checked={checked} onChange={() => toggleSelect(m.id)} /></span>
        <div
          className="mem-card-body"
          role="button"
          tabIndex={0}
          onClick={() => openDetail(m)}
          onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openDetail(m); } }}
        >
          <div className="mem-card-text">{m.text}</div>
          <div className="mem-card-meta">
            <span className={"mem-tag scope-" + scopeKey}>{meta.label}</span>
            {m.candidate && (
              <Tooltip content="低置信记忆：不注入 prompt，但可被检索；编辑或提升后转为正式记忆">
                <span className="mem-tag cand">候选</span>
              </Tooltip>
            )}
            {m.kind && <span className="mem-tag kind">{m.kind}</span>}
            {time && <span>{time}</span>}
            <span>使用 {m.usage_count} 次</span>
            {m.expires_at && <span>过期 {new Date(parseServerTime(m.expires_at)).toLocaleDateString()}</span>}
          </div>
        </div>
        <div className="mem-card-actions">
          <Tooltip content="查看 / 编辑">
            <button className="ui-icon-btn size-xs" aria-label="查看或编辑" onClick={() => openDetail(m)}><IconPencil size={13} /></button>
          </Tooltip>
          <Tooltip content="删除">
            <button className="ui-icon-btn size-xs tone-danger" aria-label="删除" onClick={() => setConfirmTarget(m)}><IconTrash size={13} /></button>
          </Tooltip>
        </div>
      </div>
    );
  };

  const scopeCount = (s: ScopeFilter) =>
    s === "all" ? items.length : items.filter((m) => ((m.scope as string) || "session") === s).length;
  const someSelected = selected.size > 0;

  /** 列表分组：全部视图按作用域分组（组头在卡片内），单作用域视图平铺 */
  const body = scopeFilter === "all"
    ? grouped.map((g) => (
        <div key={g.scope} className="mem-group">
          <div className="mem-group-title">
            <span className={"mem-group-dot scope-" + g.scope} aria-hidden />
            {SCOPE_META[g.scope].label}记忆<span className="mem-group-count">{g.list.length}</span>
            <span className="mem-group-desc">{SCOPE_META[g.scope].desc}</span>
          </div>
          {g.list.map(renderItem)}
        </div>
      ))
    : visible.map(renderItem);

  return (
    <div className="mem-page">
      {/* plan-114-452：页头（PageShell）→ AI 开关组 → 工具条 → 单张列表卡。
          列表卡自身是滚动容器边界：滚动条在卡片内侧，批量操作在卡片顶部固定行（不引起跳变）。 */}
      <PageActions>
        <Button variant="secondary" size="sm" icon={<IconRefresh size={13} />} onClick={() => void load()}>刷新</Button>
      </PageActions>
      <Section>
        <CardRow title="AI 主动生成记忆" desc="开启后每轮对话结束时，AI 会自主提取关键事实/偏好写入记忆库；关闭则不再自动生成记忆">
          <Sw checked={memoryEnabled} onChange={handleToggle} />
        </CardRow>
        <CardRow title="显示候选记忆" desc="低置信记忆不注入提示词，但可被检索；编辑或提升后转为正式记忆">
          <Sw checked={includeCandidate} onChange={setIncludeCandidate} />
        </CardRow>
      </Section>

      <div className="ui-toolbar is-nowrap">
        <Segmented
          value={scopeFilter}
          onChange={(v) => setScopeFilter(v as ScopeFilter)}
          aria-label="记忆作用域"
          options={(["all", "global", "project", "session"] as ScopeFilter[]).map((s) => ({
            value: s,
            label: s === "all" ? "全部" : SCOPE_META[s].label,
            count: scopeCount(s),
          }))}
        />
        <div className="ui-toolbar-end">
          <Input
            wrapClassName="ui-toolbar-search"
            prefix={<IconSearch size={13} />}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索记忆内容"
            aria-label="搜索记忆"
          />
        </div>
      </div>

      {err && <div className="sched-error">{err}</div>}

      <div className="ui-card is-flush mem-listcard">
        {/* 固定表头行：全选 + 计数在左，批量删除在右（无选中时仅隐藏、仍占位） */}
        <div className="mem-listhead">
          <Checkbox
            checked={allSelected}
            indeterminate={someSelected && !allSelected}
            onChange={toggleSelectAll}
            disabled={items.length === 0}
            aria-label="全选"
          />
          <span className="mem-listhead-text">{someSelected ? `已选 ${selected.size} 条` : `共 ${visible.length} 条`}</span>
          <span className="ui-batch-slot" data-empty={someSelected ? undefined : ""}>
            <span className="ui-batch-only">
              <Button variant="danger-ghost" size="sm" icon={<IconTrash size={12} />} onClick={() => setConfirmBatch(true)} disabled={!someSelected}>删除所选</Button>
            </span>
          </span>
        </div>
        <div className="mem-scroll">
          {visible.length === 0
            ? emptyState(scopeFilter === "all" ? "暂无记忆" : "该作用域暂无记忆")
            : body}
        </div>
      </div>

      {/* S8：详情弹窗（查看全文 + 编辑 + 提升/降级 + 删除） */}
      <Modal
        open={detail !== null}
        onClose={() => setDetail(null)}
        title="记忆详情"
        subtitle={detail
          ? `${SCOPE_META[(detail.scope as string) || "session"]?.label || "会话"}记忆 · ${detail.kind}`
          : undefined}
        width={620}
        footer={
          <>
            <button className="btn btn-ghost btn-sm" onClick={() => setDetail(null)}>取消</button>
            <button
              className="btn btn-primary btn-sm"
              disabled={detailSaving || !detailText.trim()}
              aria-busy={detailSaving}
              onClick={() => void saveDetail()}
            >
              {detailSaving ? "保存中…" : "保存"}
            </button>
          </>
        }
      >
        {detail && (
          <div className="mem-detail">
            <textarea
              className="ui-textarea mem-detail-text"
              value={detailText}
              onChange={(e) => setDetailText(e.target.value)}
              rows={6}
              aria-label="记忆内容"
              placeholder="记忆内容"
            />
            <div className="mem-detail-meta">
              <span>使用 {detail.usage_count} 次</span>
              {detail.candidate && <span className="mem-badge cand">候选</span>}
              {detail.expires_at && (
                <span>过期 {new Date(parseServerTime(detail.expires_at)).toLocaleDateString()}</span>
              )}
            </div>
            <div className="mem-detail-actions">
              {(detail.scope as string) !== "global" && (
                <button className="btn btn-ghost btn-sm" disabled={busyId === detail.id}
                  onClick={async () => { await promote(detail, detail.scope === "session" ? "project" : "global"); setDetail(null); }}>
                  <IconArrowUp size={12} /> {detail.scope === "session" ? "提升为项目记忆" : "提升为全局记忆"}
                </button>
              )}
              {(detail.scope as string) !== "session" && (
                <button className="btn btn-ghost btn-sm" disabled={busyId === detail.id}
                  onClick={async () => { await promote(detail, "session"); setDetail(null); }}>
                  降级为会话记忆
                </button>
              )}
              <button className="btn btn-danger-ghost btn-sm" onClick={() => { setConfirmTarget(detail); setDetail(null); }}>
                <IconTrash size={12} /> 删除
              </button>
            </div>
          </div>
        )}
      </Modal>

      <ConfirmDialog
        open={confirmTarget !== null}
        title="删除记忆"
        message={`删除该记忆？\n${confirmTarget?.text ?? ""}`}
        danger
        onCancel={() => setConfirmTarget(null)}
        onConfirm={doDelete}
      />

      <ConfirmDialog
        open={confirmBatch}
        title="批量删除记忆"
        message={`将删除选中的 ${selected.size} 条记忆，此操作不可恢复。`}
        danger
        onCancel={() => setConfirmBatch(false)}
        onConfirm={() => void doBatchDelete()}
      />
    </div>
  );
}
