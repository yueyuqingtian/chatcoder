/** 任务摘要（plan-20261009-ui U8）：只显示真实任务区块、步骤状态和真实产物数据。
 *
 * 信息架构（自上而下）：
 *  ① 状态头   —— 状态图标 + 状态文案 + 一行说明 + 进度条。**不再展示用户最新消息原文**（那只是「你说了什么」，
 *               不是「任务现在怎样」；状态头改为实时回答「在做什么、做到哪了」）；
 *  ② 执行进度 —— 步骤列表（状态图标 + 标题 + 备注，点击定位到对应消息）；进度口径与输入框上方胶囊同源；
 *  ③ 产物     —— 原「产物」与「文件审查」合并为一段：顶部汇总「N 个文件 · 已审 x/N」+「全部通过」，
 *               文件行 = 审核勾选 + 文件类型图标 + 文件名 + 目录 + 悬停操作（打开 / 回滚）；
 *  ④ 浏览过的文件 —— 文件类型图标 chips；
 *  ⑤ 方案待确认 —— 确认执行 / 停止任务。
 * 数据源与审核 / 回滚行为不变（reviewedFiles / markFileReviewed / artifacts / requestRollbackPreview）。 */
import { useMemo } from "react";
import { useChatStore } from "../../store/chat";
import { usePanelStore } from "../../store/panel";
import { recordComponentRender } from "../../perf/metrics";
import { useProgressRows } from "../chat/taskProgress";
import { IconCheck, IconExternalLink, IconRefresh, IconRotateCcw } from "../icons";
import { Badge, Button, Empty, Tooltip } from "../ui";
import { FileIcon, baseName } from "../iconkit/FileIcon";
import { StatusIcon, type StatusKind } from "../iconkit/StatusIcon";

/** plan-75-334 阶段3：隐藏期的消息占位——稳定空引用，selector 结果不变 ⇒ 不触发重渲染。 */
const EMPTY_MESSAGES: never[] = [];

interface ProductFile {
  path: string;
  /** 产出该文件的轮次（可回滚）；仅后端产物清单里出现的文件为 null */
  turnId: number | null;
  /** 所属产物标题（后端产物清单提供） */
  title: string | null;
}

/** plan-404-1892（图4）：后端 fs.write 产物 title 形如「文件:<完整路径>」（见 orchestration/artifacts.py），
 *  这类标题只是重复了文件行已有的信息，还会把组头撑成两行。这里归一为「无标题」，
 *  文件行直接平铺；仅有人工归属的产物组名才保留组头。 */
function meaningfulTitle(title: string | null): string | null {
  if (!title) return null;
  const t = title.trim();
  if (!t || t.startsWith("文件:")) return null;
  return t;
}

export function TaskSummaryPanel({ visible = true }: { visible?: boolean }) {
  // plan-75-334 阶段0：记录组件渲染（仅采集期间统计，零开销）
  recordComponentRender("taskSummary");

  const tasks = useChatStore((state) => state.tasks);
  // plan-75-334 阶段3：不可见时不订阅真实 messages（读稳定空数组），
  // 隐藏标签不再随每个落库 delta 重算派生；恢复可见时 selector 立即切回并补算一次。
  const messages = useChatStore((state) => (visible ? state.messages : EMPTY_MESSAGES));
  // v38 (plan-482): 方案文档确认入口——等待确认时在任务面板提供确认/停止按钮
  const hasPendingPlan = useChatStore((state) => state.pendingPlan != null);
  const confirmPlanTurn = useChatStore((state) => state.confirmPlanTurn);
  const refreshTasks = useChatStore((state) => state.refreshTasks);
  const reviewedFiles = useChatStore((state) => state.reviewedFiles);
  const markFileReviewed = useChatStore((state) => state.markFileReviewed);
  // plan-404-1892（图3）：审核口径与消息流胶囊对齐——需读持久化状态与批量审核 action
  const reviewPath = useChatStore((state) => state.reviewPath);
  const reviewFiles = useChatStore((state) => state.reviewFiles);
  const turnChanges = useChatStore((state) => state.turnChanges);
  const artifacts = useChatStore((state) => state.artifacts);
  const isRunning = useChatStore((state) => state.isRunning);
  const setPreviewPath = usePanelStore((state) => state.setPreviewPath);
  const openTab = usePanelStore((state) => state.openTab);

  /** plan-75-334 阶段2：浏览过的文件改为稳定派生。
   *  原实现是 effect 扫描 messages 后 setVisitedFiles：消息每落库一次就先扫描、
   *  再触发一次额外组件更新；现在与本次渲染同拍算出，不再多一轮渲染。
   *  阶段3：visible=false 时 messages 为空引用，本派生自然退化为空数组（无重复扫描）。 */
  const visitedFiles = useMemo(() => {
    const files = new Set<string>();
    for (const message of messages) {
      const content = message.content as Record<string, unknown>;
      if (message.msg_type !== "tool_call" || content.tool !== "fs_read") continue;
      const args = content.args as Record<string, unknown> | undefined;
      if (typeof args?.path === "string" && args.path) files.add(args.path);
    }
    return [...files].slice(-20).reverse();
  }, [messages]);

  /** plan-282-1441（#3）：任务进度统一走共享模块（与输入框上方胶囊**同源**）。
   *  此前这里自有一套口径：只看 max(turn_id)，无步骤时还会回退到 request 任务
   *  （而 request 的标题就是用户消息文本），导致与胶囊显示不一致。
   *  plan-282-1492：展示口径（是否还该显示、要不要标"进行中"）已收进共享模块，
   *  面板不再自算运行态门控。 */
  const progress = useProgressRows(visible);
  const progressRows = progress.rows;
  const progressDone = progress.done;

  /** 产物文件 = 轮次产出的 artifact 消息（带 turnId，可回滚）∪ 后端产物清单（带标题），按路径去重合并。
   *  此前这是「产物」与「文件审查」两段互相重复的列表。 */
  const productFiles = useMemo<ProductFile[]>(() => {
    const map = new Map<string, ProductFile>();
    for (const message of messages) {
      const content = message.content as Record<string, unknown>;
      if (message.msg_type !== "artifact" || !Array.isArray(content.files)) continue;
      for (const file of content.files.map(String)) {
        if (!map.has(file)) map.set(file, { path: file, turnId: message.turn_id ?? null, title: null });
      }
    }
    for (const artifact of artifacts) {
      // plan-404-1892（图4）：标题归一后可能为空——不再回落为「产物 #N」，避免无意义的组头
      const title = meaningfulTitle(artifact.title);
      for (const file of artifact.files ?? []) {
        const hit = map.get(file);
        if (hit) { if (!hit.title && title) hit.title = title; }
        else map.set(file, { path: file, turnId: null, title });
      }
    }
    return [...map.values()];
  }, [messages, artifacts]);

  /** 按产物标题分组；全部没有标题时不显示组头（单组平铺） */
  const groups = useMemo(() => {
    const titled = productFiles.some((f) => f.title);
    if (!titled) return [{ title: "", files: productFiles }];
    const order: string[] = [];
    const by = new Map<string, ProductFile[]>();
    for (const f of productFiles) {
      const key = f.title ?? "其他文件";
      if (!by.has(key)) { by.set(key, []); order.push(key); }
      by.get(key)!.push(f);
    }
    // 「其他文件」（没有归到任何产物的零散文件）固定排在最后
    order.sort((a, b) => Number(a === "其他文件") - Number(b === "其他文件"));
    return order.map((title) => ({ title, files: by.get(title)! }));
  }, [productFiles]);

  /** plan-404-1892（图3）：审核口径以后端持久化状态为准（turnChanges[*].reviewed），
   *  reviewedFiles 仅兜底「无 turn 归属」的产物清单文件——此前只读 reviewedFiles，
   *  导致消息流里已审核的文件在面板仍显示未审。 */
  const reviewedSet = useMemo(() => {
    const set = new Set<string>();
    for (const list of Object.values(turnChanges)) {
      for (const c of list) if (c.reviewed) set.add(c.path);
    }
    for (const [p, ok] of Object.entries(reviewedFiles)) if (ok) set.add(p);
    return set;
  }, [turnChanges, reviewedFiles]);

  const reviewedCount = productFiles.filter((f) => reviewedSet.has(f.path)).length;
  const allReviewed = productFiles.length > 0 && reviewedCount === productFiles.length;

  /** plan-404-1892（图3）：「全部通过」按 turn 归组批量提交（每 turn 一次请求），
   *  无 turn 归属的产物清单文件走本地兜底。 */
  const approveAll = () => {
    const byTurn = new Map<number, string[]>();
    const localOnly: string[] = [];
    const tids = Object.keys(turnChanges).map(Number);
    for (const f of productFiles) {
      const hit = tids.filter((tid) => (turnChanges[tid] ?? []).some((c) => c.path === f.path));
      if (hit.length === 0) localOnly.push(f.path);
      else for (const tid of hit) byTurn.set(tid, [...(byTurn.get(tid) ?? []), f.path]);
    }
    for (const [tid, paths] of byTurn) void reviewFiles(tid, paths, true);
    for (const p of localOnly) markFileReviewed(p, true);
  };

  const openFile = (path: string) => {
    setPreviewPath(path);
    openTab("files");
  };

  /** 状态头：回答「任务现在怎样」。无用户消息原文。 */
  const head = useMemo<{ status: StatusKind; title: string; sub: string; tone: "info" | "warning" | "success" | "muted" }>(() => {
    const running = progressRows.find((r) => r.status === "running");
    if (hasPendingPlan) return { status: "paused", title: "等待确认方案", sub: "确认后开始执行，也可以停止任务", tone: "warning" };
    if (progress.phase === "interrupted") return { status: "paused", title: "已停止", sub: `${progress.unfinished} 项未完成，可在对话里继续`, tone: "warning" };
    if (isRunning || progress.phase === "running") {
      return {
        status: "running",
        title: "执行中",
        sub: progressRows.length > 0 ? (running ? `${progressDone}/${progressRows.length} 步 · ${running.title}` : `${progressDone}/${progressRows.length} 步`) : "正在处理你的请求",
        tone: "info",
      };
    }
    if (productFiles.length > 0 || tasks.length > 0 || visitedFiles.length > 0) {
      return { status: "done", title: "已完成", sub: productFiles.length > 0 ? `产出 ${productFiles.length} 个文件，已审 ${reviewedCount}/${productFiles.length}` : "本轮任务已结束", tone: "success" };
    }
    return { status: "pending", title: "空闲", sub: "等待你布置任务", tone: "muted" };
  }, [hasPendingPlan, progress.phase, progress.unfinished, isRunning, progressRows, progressDone, productFiles.length, reviewedCount, tasks.length, visitedFiles.length]);

  const ratio = progressRows.length > 0 ? progressDone / progressRows.length : 0;
  const hasAnything = progressRows.length > 0 || productFiles.length > 0 || visitedFiles.length > 0 || hasPendingPlan;

  return (
    <div className="rp-body ts-panel">
      {/* ① 状态头 */}
      <section className={`ts-head is-${head.tone}`}>
        <div className="ts-head-top">
          <StatusIcon status={head.status} size={20} />
          <div className="ts-head-main">
            <div className="ts-head-title">{head.title}</div>
            <div className="ts-head-sub">{head.sub}</div>
          </div>
          {tasks.length > 0 && (
            <Tooltip content="刷新任务" side="bottom">
              <button type="button" className="ui-icon-btn ts-head-refresh" aria-label="刷新任务" onClick={() => refreshTasks()}>
                <IconRefresh size={14} />
              </button>
            </Tooltip>
          )}
        </div>
        {progressRows.length > 0 && (
          <div className="ts-bar" role="progressbar" aria-valuemin={0} aria-valuemax={progressRows.length} aria-valuenow={progressDone} aria-label="任务进度">
            <span className="ts-bar-fill" style={{ ["--ratio" as string]: ratio }} />
          </div>
        )}
      </section>

      {!hasAnything && (
        <Empty title="还没有可展示的内容" desc="在对话里布置任务后，这里会实时显示执行步骤与产出的文件。" />
      )}

      {/* ② 执行进度 */}
      {progressRows.length > 0 && (
        <section className="ts-sec">
          <div className="ts-sec-head">
            <span className="ts-sec-title">执行进度</span>
            <Badge size="xs" tone="neutral">{progressDone}/{progressRows.length}</Badge>
          </div>
          <div className="ts-steps">
            {progressRows.map((row, i) => (
              <button
                key={`${row.key}-${i}`}
                type="button"
                className={`ts-step is-${row.status}`}
                disabled={row.agentId == null}
                onClick={() => { if (row.agentId != null) useChatStore.getState().requestScrollTo({ threadId: row.agentId }); }}
              >
                <StatusIcon status={row.status} size={15} />
                <span className="ts-step-title">{row.title}</span>
                {row.note && <span className="ts-step-note">{row.note}</span>}
              </button>
            ))}
          </div>
        </section>
      )}

      {/* ③ 方案待确认 */}
      {hasPendingPlan && (
        <div className="ts-plan-actions">
          <Button variant="primary" size="md" onClick={() => confirmPlanTurn(true)}>确认执行</Button>
          <Button variant="ghost" size="md" onClick={() => confirmPlanTurn(false)}>停止任务</Button>
        </div>
      )}

      {/* ④ 产物（产物与文件审核合并） */}
      {productFiles.length > 0 && (
        <section className="ts-sec">
          <div className="ts-sec-head">
            <span className="ts-sec-title">产物</span>
            <span className="ts-sec-meta">{productFiles.length} 个文件 · 已审 {reviewedCount}/{productFiles.length}</span>
            <Button
              size="xs"
              variant="subtle"
              className="ts-approve-all"
              icon={<IconCheck size={12} />}
              disabled={allReviewed}
              onClick={approveAll}
            >
              全部通过
            </Button>
          </div>
          {groups.map((g) => (
            <div className="ts-group" key={g.title || "all"}>
              {g.title && <div className="ts-group-title">{g.title}</div>}
              {g.files.map((f) => {
                const reviewed = reviewedSet.has(f.path);
                return (
                  <div className={`ts-file${reviewed ? " is-reviewed" : ""}`} key={f.path}>
                    <Tooltip content={reviewed ? "取消已审" : "标记为已审"} side="top">
                      <button
                        type="button"
                        role="checkbox"
                        aria-checked={reviewed}
                        aria-label={`${reviewed ? "取消已审" : "标记为已审"}：${baseName(f.path)}`}
                        className="ts-check"
                        onClick={() => reviewPath(f.path, !reviewed)}
                      >
                        <IconCheck size={11} />
                      </button>
                    </Tooltip>
                    {/* plan-404-1892（图4）：只显示文件名（超长省略、不换行），完整路径移到悬停提示 */}
                    <Tooltip content={f.path} side="top">
                      <button type="button" className="ts-file-main" onClick={() => openFile(f.path)}>
                        <FileIcon name={f.path} size={16} />
                        <span className="ts-file-name">{baseName(f.path)}</span>
                      </button>
                    </Tooltip>
                    <span className="ts-file-ops">
                      <Tooltip content="在文件面板中打开" side="top">
                        <button type="button" className="ui-icon-btn" aria-label="打开文件" onClick={() => openFile(f.path)}><IconExternalLink size={13} /></button>
                      </Tooltip>
                      {f.turnId != null && (
                        <Tooltip content="回滚这一轮的改动" side="top">
                          <button type="button" className="ui-icon-btn is-danger" aria-label="回滚" onClick={() => useChatStore.getState().requestRollbackPreview(f.turnId!)}><IconRotateCcw size={13} /></button>
                        </Tooltip>
                      )}
                    </span>
                  </div>
                );
              })}
            </div>
          ))}
        </section>
      )}

      {/* ⑤ 浏览过的文件 */}
      {visitedFiles.length > 0 && (
        <section className="ts-sec">
          <div className="ts-sec-head">
            <span className="ts-sec-title">浏览过的文件</span>
            <Badge size="xs" tone="neutral">{visitedFiles.length}</Badge>
          </div>
          <div className="ts-chips">
            {visitedFiles.map((file) => (
              <Tooltip key={file} content={file} side="top">
                <button type="button" className="ts-chip" onClick={() => openFile(file)}>
                  <FileIcon name={file} size={14} />
                  <span>{baseName(file)}</span>
                </button>
              </Tooltip>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
