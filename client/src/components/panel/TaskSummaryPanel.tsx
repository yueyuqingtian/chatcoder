/** 摘要（plan-128-518 重做）：四个固定模块，始终显示，不做动态增删。
 *
 *  ① 进度   —— 状态行（图标 + 状态 + 百分比）+ 细进度条 + 步骤列表；方案待确认的「确认执行 / 停止」、目标行也在这里；
 *  ② 产物   —— 本会话产出的文件（点击在文件面板打开），只读展示：不在这里做审核、回滚（那些操作留在消息流的改动条里）；
 *  ③ 子代理 —— 本会话派出的子代理（运行中置顶），点击打开其详情标签；
 *  ④ 拓展   —— 本会话用到的技能 / 插件 / 连接器（来自用户引用与模型调用）。
 *  每个模块是一块封闭的圆角面（一层、不嵌套），模块间只靠留白分隔；无内容时模块内给一行说明，版面保持不变。
 *  折叠状态按「会话 + 模块」写 localStorage（store/tsFold.ts）。 */
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useChatStore } from "../../store/chat";
import { selectSessionWorkflowBusy, useWorkflowStore } from "../../store/workflow";
import { usePanelStore } from "../../store/panel";
import { useTsFoldStore } from "../../store/tsFold";
import { openExtensionsManager, type ExtKind } from "../../store/navIntent";
import { api } from "../../api/client";
import { recordComponentRender } from "../../perf/metrics";
import { useProgressRows } from "../chat/taskProgress";
import { isListedSubagent, subagentState, type SubagentState } from "../chat/subagentStatus";
import { refsOf } from "../chat/AttachmentCard";
import { IconChevronDown, IconTarget } from "../icons";
import { Badge, Button, Collapse, Tooltip } from "../ui";
import { extKindIcon } from "../composer/ExtensionPicker";
import { FileIcon, baseName } from "../iconkit/FileIcon";
import { IxBot, IxExtensions, IxFiles, IxTasks } from "../iconkit/StackIcons";
import { StatusIcon, type StatusKind } from "../iconkit/StatusIcon";

/** plan-75-334 阶段3：隐藏期的消息占位——稳定空引用，selector 结果不变 ⇒ 不触发重渲染。 */
const EMPTY_MESSAGES: never[] = [];
/** 子代理展示状态 → 状态图标（已停止不是错误，用中性的暂停态；与工具卡同一口径） */
const SUBAGENT_ICON: Record<SubagentState, StatusKind> = {
  running: "running", done: "done", failed: "failed", stopped: "paused", pending: "pending",
};

/** 产物路径的归一键（plan-20261020-path-dedupe）：模型写同一个文件时会混用 `a\b.md` 与 `a/b.md`，
 *  后端聚合已统一，这里再兜一层——接口失败回退到消息产物时同样按归一键去重，避免同一文件两行。
 *  只统一分隔符与前导 `./`，不改大小写（Linux 工作区里 A.md 与 a.md 是两个文件）。 */
function pathKey(p: string): string {
  return p.replace(/\\/g, "/").replace(/^\.\//, "").replace(/\/{2,}/g, "/").replace(/\/+$/, "");
}

/** 模块卡片：整条表头可点折叠。计数与箭头收在表头右侧（计数用统一徽标，与拓展行的计数同款），
 *  折叠只转角度，不挤标题（plan-20261014-summary） */
function SummaryCard({ id, title, icon, count, children }: {
  id: string;
  title: string;
  icon: ReactNode;
  count?: ReactNode;
  children: ReactNode;
}) {
  const sid = useChatStore((s) => s.currentSessionId);
  const key = `${sid ?? "new"}:${id}`;
  const folded = useTsFoldStore((s) => !!s.folded[key]);
  const toggle = useTsFoldStore((s) => s.toggle);
  return (
    <section className="ts-card" data-fold={folded ? "1" : "0"}>
      <button type="button" className="ts-card-head" aria-expanded={!folded} onClick={() => toggle(key)}>
        <span className="ts-card-ic">{icon}</span>
        <span className="ts-card-title">{title}</span>
        <span className="ts-card-end">
          {count != null && <Badge size="sm" count>{count}</Badge>}
          <span className="ts-chev" aria-hidden><IconChevronDown size={14} /></span>
        </span>
      </button>
      <Collapse open={!folded}><div className="ts-card-body">{children}</div></Collapse>
    </section>
  );
}

/** 子代理用时：运行中按当前时刻估算（随面板重渲染刷新） */
function fmtDuration(startedAt?: string | null, endedAt?: string | null): string {
  if (!startedAt) return "";
  const s = Date.parse(startedAt);
  const e = endedAt ? Date.parse(endedAt) : Date.now();
  if (Number.isNaN(s) || Number.isNaN(e)) return "";
  const sec = Math.max(0, Math.round((e - s) / 1000));
  return sec < 60 ? `${sec} 秒` : `${Math.floor(sec / 60)} 分 ${sec % 60} 秒`;
}

interface ExtUse { kind: ExtKind; name: string; count: number }
const EXT_LABEL: Record<ExtKind, string> = { skill: "技能", plugin: "插件", connector: "连接器" };

export function TaskSummaryPanel({ visible = true }: { visible?: boolean }) {
  // plan-75-334 阶段0：记录组件渲染（仅采集期间统计，零开销）
  recordComponentRender("taskSummary");

  // plan-75-334 阶段3：不可见时不订阅真实 messages（读稳定空数组），隐藏标签不再随每个落库 delta 重算派生
  const messages = useChatStore((state) => (visible ? state.messages : EMPTY_MESSAGES));
  const hasPendingPlan = useChatStore((state) => state.pendingPlan != null);
  const confirmPlanTurn = useChatStore((state) => state.confirmPlanTurn);
  const artifacts = useChatStore((state) => state.artifacts);
  const isRunning = useChatStore((state) => state.isRunning);
  /** 后台子代理未结束也算本会话执行中（整轮口径，与输入框停止按钮同源） */
  const pendingSubagents = useChatStore((state) => state.pendingSubagents);
  const subagentMeta = useChatStore((state) => state.subagentMeta);
  const session = useChatStore((state) => state.sessions.find((x) => x.id === state.currentSessionId));
  // 后台工作流在跑（含结束后等待唤醒主代理的空档）时，本会话即为「执行中」（工作流可能已不占用任何轮次）
  const workflowRunning = useWorkflowStore((s) => selectSessionWorkflowBusy(s, session?.id));
  const setPreviewPath = usePanelStore((state) => state.setPreviewPath);
  const openTab = usePanelStore((state) => state.openTab);
  const openSubagent = usePanelStore((state) => state.openSubagent);

  /** plan-282-1441：任务进度走共享模块（与输入框上方胶囊同源） */
  const progress = useProgressRows(visible);
  const progressRows = progress.rows;
  const progressDone = progress.done;

  /** 产物文件：以后端按写盘记录折算的「本会话写过且现在仍存在」清单为准（与变更卡片同一口径，
   *  被删除的文件、本轮新建又删除的临时文件都不算产物）；刷新时机跟随变更清单（写盘 / 轮次结束都会重拉）。
   *  清单请求失败时才回退到 artifact 消息并集，且不展示已知被删除的文件。 */
  const turnChanges = useChatStore((state) => state.turnChanges);
  const [productPaths, setProductPaths] = useState<string[] | null>(null);
  const productSessionId = session?.id ?? null;
  useEffect(() => {
    if (!visible || productSessionId == null) return;
    let dead = false;
    const timer = window.setTimeout(() => {
      api.listSessionProducts(productSessionId)
        .then((r) => { if (!dead) setProductPaths(r.files); })
        .catch(() => { if (!dead) setProductPaths(null); });
    }, 300);
    return () => { dead = true; window.clearTimeout(timer); };
  }, [visible, productSessionId, turnChanges, isRunning]);
  useEffect(() => { setProductPaths(null); }, [productSessionId]);
  const productFiles = useMemo<string[]>(() => {
    if (productPaths) {
      // 后端已按归一路径去重；这里再按同一口径收一次，防止旧版本后端返回混合写法
      const uniq = new Map<string, string>();
      for (const p of productPaths) {
        const k = pathKey(p);
        if (!uniq.has(k)) uniq.set(k, k);
      }
      return [...uniq.values()];
    }
    const deleted = new Set<string>();
    for (const list of Object.values(turnChanges)) for (const c of list ?? []) if (c.action === "deleted") deleted.add(pathKey(c.path));
    // 回退口径与变更卡片一致：同一文件的两种斜杠写法合并成一行（plan-20261020-path-dedupe）
    const set = new Map<string, string>();
    for (const message of messages) {
      const content = message.content as Record<string, unknown>;
      if (message.msg_type !== "artifact" || !Array.isArray(content.files)) continue;
      for (const file of content.files.map(String)) set.set(pathKey(file), pathKey(file));
    }
    for (const artifact of artifacts) {
      for (const file of artifact.files ?? []) set.set(pathKey(file), pathKey(file));
    }
    return [...set.values()].filter((f) => !deleted.has(f));
  }, [productPaths, messages, artifacts, turnChanges]);

  const openFile = (path: string) => {
    setPreviewPath(path);
    openTab("files");
  };

  /** 子代理：运行中置顶；工作流的节点按所属程序分组（标注“工作流 · 节点名”），其余按 id 升序。
   * 列入与状态的判定与工具卡共用（subagentStatus.ts），两处数量、状态保持一致 */
  const subagents = useMemo(() => {
    const rows = Object.entries(subagentMeta).filter(([, m]) => isListedSubagent(m)).map(([id, m]) => {
      return {
        id: Number(id),
        meta: m,
        status: SUBAGENT_ICON[subagentState(m.status)],
        programId: m.workflow?.program_id ?? 0,
      };
    });
    rows.sort(
      (a, b) =>
        Number(b.status === "running") - Number(a.status === "running") ||
        a.programId - b.programId ||
        a.id - b.id,
    );
    return rows;
  }, [subagentMeta]);
  const runningSubagents = subagents.filter((r) => r.status === "running").length;

  /** 拓展使用：用户引用（refs）∪ 模型调用（skill_view / mcp_*），按种类 + 名称去重计数 */
  const hasMcpCall = useMemo(
    () => messages.some((m) => m.msg_type === "tool_call" && typeof (m.content as Record<string, unknown>).tool === "string"
      && ((m.content as Record<string, unknown>).tool as string).startsWith("mcp_")),
    [messages],
  );
  // MCP 包装工具名形如 mcp_<server>_<tool>（server 中的 . - 已替换为 _）——取已配置连接器名称做前缀匹配还原
  const [mcpNames, setMcpNames] = useState<string[]>([]);
  useEffect(() => {
    if (!visible || !hasMcpCall) return;
    let alive = true;
    void api.listMcpServers().then((list) => { if (alive) setMcpNames(list.map((s) => s.name)); }).catch(() => { /* 非阻塞 */ });
    return () => { alive = false; };
  }, [visible, hasMcpCall]);

  const extUses = useMemo<ExtUse[]>(() => {
    const map = new Map<string, ExtUse>();
    const add = (kind: ExtKind, name: string) => {
      if (!name) return;
      const k = `${kind}:${name}`;
      const hit = map.get(k);
      if (hit) hit.count += 1; else map.set(k, { kind, name, count: 1 });
    };
    const safe = mcpNames.map((n) => ({ name: n, prefix: `mcp_${n.replace(/[.-]/g, "_")}_` }))
      .sort((a, b) => b.prefix.length - a.prefix.length);
    for (const m of messages) {
      const content = m.content as Record<string, unknown>;
      if (m.msg_type === "tool_call") {
        const tool = typeof content.tool === "string" ? content.tool : "";
        if (tool === "skill_view") {
          const a = content.args as Record<string, unknown> | undefined;
          if (typeof a?.name === "string") add("skill", a.name);
        } else if (tool.startsWith("mcp_")) {
          const hit = safe.find((s) => tool.startsWith(s.prefix));
          add("connector", hit ? hit.name : (tool.split("_")[1] ?? tool));
        }
        continue;
      }
      for (const r of refsOf(content)) {
        if (r.kind === "skill") add("skill", r.label || r.value);
        else if (r.kind === "plugin") add("plugin", r.label || r.value);
        else if (r.kind === "mcp") add("connector", r.label || r.value);
      }
    }
    return [...map.values()];
  }, [messages, mcpNames]);

  const goalActive = !!session && session.goal_status === "active" && !!session.goal_text;

  /** 总览行：回答「现在怎样」。标题之外的说明只在需要时给出（空闲 / 无步骤的执行中 / 待确认），
   *  避免每种状态都挤一段文字（plan-20261014-summary） */
  const head = useMemo<{ status: StatusKind; title: string; hint?: string }>(() => {
    if (hasPendingPlan) return { status: "paused", title: "等待确认方案", hint: "确认后按方案执行，或停止这次任务。" };
    if (isRunning || workflowRunning || pendingSubagents > 0 || progress.phase === "running") {
      // 主代理在跑 → 执行中；只剩后台工作流 / 子代理时点明是谁在跑（与输入框停止按钮同一整轮口径）
      const title = isRunning || progress.phase === "running" ? "执行中"
        : workflowRunning ? "工作流执行中" : "子代理执行中";
      const hint = progressRows.length === 0 ? "正在执行，步骤会陆续出现在这里。"
        : progress.carried ? `上一轮有 ${progress.unfinished} 项待续接，模型核对后继续。`
        : progress.fromPrevious ? "以下是之前的步骤，本轮制定新步骤后刷新。"
        : undefined;
      return { status: "running", title, hint };
    }
    if (progress.carried) return { status: "paused", title: `待续接 · ${progress.unfinished} 项` };
    if (progress.phase === "interrupted") return { status: "paused", title: `已停止 · ${progress.unfinished} 项未完成` };
    if (progressRows.length > 0) {
      // 本轮已结束：清单保留到之后的轮次制定新清单（plan-20261025-progress-keep）；
      // 模型漏标的项如实计为未完成，不冒充已完成
      return progress.unfinished === 0
        ? { status: "done", title: "已完成" }
        : { status: "pending", title: `已结束 · ${progress.unfinished} 项未完成` };
    }
    return { status: "pending", title: "空闲", hint: "布置任务后，这里会显示执行步骤。" };
  }, [hasPendingPlan, progress.phase, progress.unfinished, progress.carried, progress.fromPrevious, isRunning, workflowRunning, pendingSubagents, progressRows]);

  const ratio = progressRows.length > 0 ? progressDone / progressRows.length : 0;

  return (
    <div className="rp-body ts-panel">
      {/* ① 进度 */}
      <SummaryCard id="progress" icon={<IxTasks size={16} />} title="进度" count={progressRows.length > 0 ? `${progressDone}/${progressRows.length}` : undefined}>
        {goalActive && (
          <div className="ts-row is-goal">
            <span className="ts-row-lead"><IconTarget size={14} /></span>
            <span className="ts-row-title">{session!.goal_text}</span>
            <span className="ts-row-note">{`第 ${session!.goal_turns_used ?? 0} 轮`}</span>
          </div>
        )}
        {/* 进度组：状态行与进度条同属一组，步骤列表在其下（plan-20261014-summary）。
         *  执行中的总览用静态圆点——步骤行已有转圈，总览再转会重复（plan-128-518 的约定）。 */}
        <div className="ts-progress">
          <div className="ts-status">
            <span className="ts-status-lead">
              {head.status === "running" ? <span className="ts-status-dot" aria-hidden /> : <StatusIcon status={head.status} size={14} />}
            </span>
            <span className={`ts-status-title is-${head.status}`}>{head.title}</span>
            {progressRows.length > 0 && <span className="ts-status-note">{Math.round(ratio * 100)}%</span>}
            {head.hint && <p className="ts-status-hint">{head.hint}</p>}
          </div>
          {progressRows.length > 0 && (
            <div className="ts-bar" data-tone={head.status} role="progressbar" aria-valuemin={0} aria-valuemax={progressRows.length} aria-valuenow={progressDone} aria-label="任务进度">
              <span className="ts-bar-fill" style={{ ["--ratio" as string]: ratio }} />
            </div>
          )}
        </div>
        {progressRows.length > 0 && (
          <div className="ts-rows">
            {progressRows.map((row, i) => (
              <button
                key={`${row.key}-${i}`}
                type="button"
                className={`ts-row is-${row.status}`}
                disabled={row.agentId == null}
                onClick={() => { if (row.agentId != null) useChatStore.getState().requestScrollTo({ threadId: row.agentId }); }}
              >
                <span className="ts-row-lead"><StatusIcon status={row.status} size={14} /></span>
                <span className="ts-row-title">{row.title}</span>
                {row.note && <span className="ts-row-note">{row.note}</span>}
              </button>
            ))}
          </div>
        )}
        {hasPendingPlan && (
          <div className="ts-plan-actions">
            <Button variant="primary" size="md" onClick={() => confirmPlanTurn(true)}>确认执行</Button>
            <Button variant="ghost" size="md" onClick={() => confirmPlanTurn(false)}>停止任务</Button>
          </div>
        )}
      </SummaryCard>

      {/* ② 产物 */}
      <SummaryCard id="products" icon={<IxFiles size={16} />} title="产物" count={productFiles.length > 0 ? productFiles.length : undefined}>
        {productFiles.length > 0 ? (
          <div className="ts-rows">
            {productFiles.map((path) => (
              <Tooltip key={path} content={path} side="top">
                <button type="button" className="ts-row" onClick={() => openFile(path)}>
                  <span className="ts-row-lead"><FileIcon name={path} size={14} /></span>
                  <span className="ts-row-title">{baseName(path)}</span>
                </button>
              </Tooltip>
            ))}
          </div>
        ) : (
          <p className="ts-empty">本会话还没有产出文件。</p>
        )}
      </SummaryCard>

      {/* ③ 子代理 */}
      <SummaryCard
        id="subagents"
        icon={<IxBot size={16} />}
        title="子代理"
        count={subagents.length > 0 ? (runningSubagents > 0 ? `运行 ${runningSubagents} / 共 ${subagents.length}` : subagents.length) : undefined}
      >
        {subagents.length > 0 ? (
          <div className="ts-rows">
            {subagents.map((r) => (
              <button key={r.id} type="button" className={`ts-row is-${r.status}`} onClick={() => openSubagent(r.id, r.meta.name)}>
                <span className="ts-row-lead"><StatusIcon status={r.status} size={14} /></span>
                <span className="ts-row-title">
                  {r.meta.workflow ? `工作流 · ${r.meta.workflow.node_title}` : r.meta.name}
                </span>
                <span className="ts-row-note">{fmtDuration(r.meta.startedAt, r.meta.endedAt)}</span>
              </button>
            ))}
          </div>
        ) : (
          <p className="ts-empty">本会话还没有使用子代理。</p>
        )}
      </SummaryCard>

      {/* ④ 拓展 */}
      <SummaryCard id="extensions" icon={<IxExtensions size={16} />} title="拓展" count={extUses.length > 0 ? extUses.length : undefined}>
        {extUses.length > 0 ? (
          <div className="ts-rows">
            {extUses.map((e) => (
              <Tooltip key={`${e.kind}:${e.name}`} content={`在设置中管理${EXT_LABEL[e.kind]}`} side="top">
                <button type="button" className="ts-row" onClick={() => openExtensionsManager(e.kind)}>
                  <span className="ts-row-lead">{extKindIcon(e.kind, 14)}</span>
                  <span className="ts-row-title">{e.name}</span>
                  <span className="ts-row-note">{EXT_LABEL[e.kind]}</span>
                  {e.count > 1 && <Badge size="xs" tone="neutral">{e.count}</Badge>}
                </button>
              </Tooltip>
            ))}
          </div>
        ) : (
          <p className="ts-empty">本会话还没有用到技能、插件或连接器。</p>
        )}
      </SummaryCard>
    </div>
  );
}
