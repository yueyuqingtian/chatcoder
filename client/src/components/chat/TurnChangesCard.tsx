/** 每轮消息下方的文件变更卡片（plan-20261018-turn-changes）
 *
 * 为什么放在消息流里：变更清单此前只在输入框上方的任务面板出现，用户回看某一轮时
 * 无法直接确认「这一轮改了哪些文件、能不能回退」。卡片按 turn 自取数据
 * （store.turnChanges），折叠状态只影响本卡片，不写全局。
 *
 * 打开方式与标题栏右上角的文件夹菜单保持同一套应用与品牌图标：
 * 资源管理器 / IDEA / 终端 / VS Code；桌面版走 Electron IPC，网页调试环境
 * 交给本机后端代开（api.openPathOnHost），任何环境都能真正点开。
 */
import { useEffect, useMemo, useState } from "react";

import { api, type FileChangeOut } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { usePanelStore } from "../../store/panel";
import { useUiStore } from "../../store/ui";
import { ConfirmDialog } from "../ConfirmDialog";
import {
  IconBrandExplorer,
  IconBrandIdea,
  IconBrandVSCode,
  IconBrandWindowsTerminal,
  IconCheck,
  IconChevronDown,
  IconCopy,
  IconRotateCcw,
} from "../icons";
import { Button, Collapse, Dialog, Menu, Tooltip, type MenuEntry } from "../ui";
import { FileBadge, splitFilePath } from "./FileBadge";

/** 模块级 in-flight 去重：同一轮可能在消息流与折叠区同时挂载，只允许发一次请求 */
const inflight = new Set<number>();

/** 已经出现过的轮次卡片：入场动画只在「第一次出现」时播放。
 *  消息流是虚拟列表，卡片滚出视口再滚回会重新挂载——不记下来的话每次滚动都会重播一遍。 */
const enteredTurns = new Set<number>();

/** 列表首次展示的最大行数（其余收进「展开」）。
 *  默认 5 行：变更卡片是每轮都出现的辅助信息，默认铺 10 行会把消息流拉得很长。 */
const PAGE_SIZE = 5;

/** 打开目标：与标题栏右上角文件夹菜单一致 */
type OpenTarget = "explorer" | "idea" | "terminal" | "vscode";

const TARGET_LABEL: Record<OpenTarget, string> = {
  explorer: "资源管理器",
  idea: "IDEA",
  terminal: "终端",
  vscode: "VS Code",
};

/** 是否为绝对路径（Windows 盘符 / UNC / POSIX 根）。 */
function isAbsolutePath(p: string): boolean {
  return /^[A-Za-z]:[\\/]/.test(p) || p.startsWith("\\\\") || p.startsWith("/");
}

/** 项目根 + 相对路径：统一成 /，避免 Windows 反斜杠混进 URL 与剪贴板。
 *  写盘记录里的 path 由模型原样传入，可能本身就是绝对路径——此前无条件拼项目根，
 *  会得到 `D:\proj\D:\proj\a.ts` 这类非法路径：IDEA 报「无效路径」、资源管理器静默无反应。 */
function toAbsPath(root: string, rel: string): string {
  const clean = rel.replace(/\\/g, "/");
  if (isAbsolutePath(clean)) return clean;
  if (!root) return clean;
  return `${root.replace(/\\/g, "/").replace(/\/+$/, "")}/${clean}`;
}

/** 父目录（终端只按目录打开：文件在部分终端里无法直接打开）。 */
function parentDirPath(abs: string): string {
  const clean = abs.replace(/\\/g, "/").replace(/\/+$/, "");
  const i = clean.lastIndexOf("/");
  const dir = i > 0 ? clean.slice(0, i) : clean;
  // 盘符根（D:）补回分隔符，避免终端拿到裸盘符
  return /^[A-Za-z]:$/.test(dir) ? `${dir}/` : dir;
}

function targetIcon(target: OpenTarget, size = 15) {
  switch (target) {
    case "idea": return <IconBrandIdea size={size} />;
    case "terminal": return <IconBrandWindowsTerminal size={size} />;
    case "vscode": return <IconBrandVSCode size={size} />;
    default: return <IconBrandExplorer size={size} />;
  }
}

export function TurnChangesCard({ turnId }: { turnId: number }) {
  const files = useChatStore((s) => s.turnChanges[turnId]);
  const loadTurnChanges = useChatStore((s) => s.loadTurnChanges);
  const latestChangeTurnId = useChatStore((s) => s.latestChangeTurnId);
  /** 撤销锁是否已拉到：未拉到 ≠ 不允许撤销（切会话瞬间 / 请求失败都会是 null）。 */
  const revertLockLoaded = useChatStore((s) => s.revertLockLoaded);
  const revertTurnFiles = useChatStore((s) => s.revertTurnFiles);
  const currentProjectId = useChatStore((s) => s.currentProjectId);
  const projects = useChatStore((s) => s.projects);
  const setFlowError = useChatStore((s) => s.setFlowError);

  const setPreviewPath = usePanelStore((s) => s.setPreviewPath);
  const openDiffPreview = usePanelStore((s) => s.openDiffPreview);
  const openPanel = usePanelStore((s) => s.openPanel);
  const openTab = usePanelStore((s) => s.openTab);

  // 卡片内的图标与徽标随「设置 → 外观 → 对话字号」缩放：
  // 字号调小时只缩文字会让图标显得突兀，这里统一按同一比例派生。
  // 13px（默认档）时分别为 15 / 14 / 17，与原设计一致。
  const chatFontSize = useUiStore((s) => s.chatFontSize) || 13;
  const iconMain = Math.max(11, Math.round(chatFontSize * 1.15));
  const iconSmall = Math.max(10, Math.round(chatFontSize * 0.92));
  const badgeSize = Math.max(13, Math.round(chatFontSize * 1.3));

  const [collapsed, setCollapsed] = useState(false);
  const [showAll, setShowAll] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [reverting, setReverting] = useState(false);
  /** 撤销被严格拒绝时的原因（文件在本轮之后被改动）；非空时撤销弹窗显示「已取消」视图 */
  const [rejected, setRejected] = useState<{ message: string; conflicts: Array<{ path: string; reason: string }> } | null>(null);
  /** 刚复制的菜单项 key；2 秒后回退文案（不依赖全局 toast） */
  const [copied, setCopied] = useState<string | null>(null);
  /** 首次挂载时读一次：这张卡此前从未展示过才播放入场（见 enteredTurns） */
  const [playEnter] = useState(() => !enteredTurns.has(turnId));
  useEffect(() => {
    if (files && files.length > 0) enteredTurns.add(turnId);
  }, [files, turnId]);

  // 未加载时补拉：effect 内直接读最新 store，不依赖 files——否则「加载完成」会再触发一次拉取
  useEffect(() => {
    if (useChatStore.getState().turnChanges[turnId] !== undefined) return;
    if (inflight.has(turnId)) return;
    inflight.add(turnId);
    void loadTurnChanges(turnId).finally(() => inflight.delete(turnId));
  }, [turnId, loadTurnChanges]);

  // 复制反馈自动回退
  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(null), 2000);
    return () => window.clearTimeout(timer);
  }, [copied]);

  const totals = useMemo(() => {
    const list = files ?? [];
    return list.reduce(
      (acc, c) => ({ add: acc.add + c.additions, del: acc.del + c.deletions }),
      { add: 0, del: 0 },
    );
  }, [files]);

  // 未加载或本轮没有改动：不占位（虚拟滚动里空卡片会留下一条空白）
  if (!files || files.length === 0) return null;

  const root = projects.find((p) => p.id === currentProjectId)?.path ?? "";
  // plan-20261019-motion-sweep：首屏 PAGE_SIZE 行常显，其余行放进独立的折叠容器——
  // 「展开其余」是高度过渡而不是整块弹出（此前按 showAll 切片，列表瞬间变长）
  const headFiles = files.slice(0, PAGE_SIZE);
  const restFiles = files.slice(PAGE_SIZE);
  const restCount = restFiles.length;
  // 撤销顺序锁：只有最新有文件改动的轮次可撤销，更早的轮次要先撤销后面的。
  // 锁未拉到（切会话瞬间 / 请求失败）时按钮也禁用，但提示文案必须说实话——
  // 此前一律显示「请先撤销更新轮次的改动」，把加载态误报成「你可能还有更晚的改动」。
  const canRevert = latestChangeTurnId === turnId;
  const revertHint = canRevert
    ? ""
    : revertLockLoaded
      ? "请先撤销更晚轮次的改动"
      : "正在读取本会话的可撤销状态…";
  const hasDesktop = typeof window !== "undefined" && !!window.chatcoderAPI;

  /** 审查：先取该轮 diff，再一次性写入「预览路径 + diff」。
   *  分成 setPreviewPath / setDiffPreview 两步会让 DiffEditor 先卸载再挂载，
   *  Monaco 在切换 model 的瞬间抛 "TextModel got disposed"（见 panel.openDiffPreview）。 */
  async function openDiff(path: string) {
    openPanel();
    openTab("files");
    try {
      // 不传 callKey = 整轮累积口径，正是「该轮该文件」
      const d = await api.getFileDiff(turnId, path);
      openDiffPreview(d.path || path, {
        path: d.path, before: d.before, after: d.after, truncated: d.truncated,
        deleted: d.action === "deleted",
      });
    } catch {
      // 取 diff 失败时仍打开文件本身，让用户至少能看到当前内容
      setPreviewPath(path);
    }
  }

  /** 用外部应用打开：桌面版走 IPC，网页调试环境由本机后端代开 */
  async function openWith(target: OpenTarget, abs: string) {
    // 终端按目录打开：部分终端无法直接打开文件（用户口径）。
    const targetPath = target === "terminal" ? parentDirPath(abs) : abs;
    if (hasDesktop) {
      if (target === "explorer") {
        // 资源管理器：文件定位、目录直接打开。失败要提示——旧实现不看返回值，
        // 路径拼错 / 文件已被删除时图标静默无效，用户只会看到「点了没反应」。
        const res = await window.chatcoderAPI?.showItemInFolder?.(abs);
        if (res && res.ok === false) setFlowError(`打开失败：${res.error ?? "路径不存在"}`, turnId);
        return;
      }
      const res = await window.chatcoderAPI?.openInApp?.(target, targetPath);
      if (res && res.ok === false) setFlowError(`打开失败：${res.error ?? "未找到可用的应用"}`, turnId);
      return;
    }
    try {
      const res = await api.openPathOnHost(targetPath, target);
      if (!res.ok) setFlowError(`打开失败：${res.error ?? "未知原因"}`, turnId);
    } catch (e) {
      setFlowError(`打开失败：${String(e)}`, turnId);
    }
  }

  async function copyText(text: string, key: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(key);
    } catch {
      /* 剪贴板不可用（非安全上下文）时静默 */
    }
  }

  function menuEntries(rel: string, abs: string): MenuEntry[] {
    const absKey = `${rel}|abs`;
    const relKey = `${rel}|rel`;
    const targets: OpenTarget[] = ["explorer", "idea", "terminal", "vscode"];
    const entries: MenuEntry[] = targets.map((t) => ({
      key: t,
      label: TARGET_LABEL[t],
      icon: targetIcon(t, iconMain),
      onSelect: () => void openWith(t, abs),
      // 打开方式与复制路径分两组，复制项不会被误当成「打开」
      divider: t === "vscode",
    }));
    entries.push(
      {
        key: "copy-abs",
        label: copied === absKey ? "已复制" : "复制绝对路径",
        icon: copied === absKey ? <IconCheck size={iconSmall} /> : <IconCopy size={iconSmall} />,
        onSelect: () => void copyText(abs, absKey),
      },
      {
        key: "copy-rel",
        label: copied === relKey ? "已复制" : "复制相对路径",
        icon: copied === relKey ? <IconCheck size={iconSmall} /> : <IconCopy size={iconSmall} />,
        onSelect: () => void copyText(rel, relKey),
      },
    );
    return entries;
  }

  function closeRevert() {
    setConfirmOpen(false);
    // 关闭动画结束前保留「已取消」视图，避免淡出过程中内容瞬间切回确认文案
    window.setTimeout(() => setRejected(null), 300);
  }

  async function confirmRevert() {
    setReverting(true);
    try {
      const res = await revertTurnFiles(turnId, false);
      // 严格拒绝：弹窗切到「已取消」视图，逐文件说明原因（未动任何文件）；
      // 其余失败已由 store 写进消息流错误卡，这里只解除忙碌态、允许关闭弹窗
      if (res.ok) setConfirmOpen(false);
      else if (res.conflicts) setRejected({ message: res.message ?? "撤销已取消", conflicts: res.conflicts });
    } finally {
      // plan-20261018-turn-changes-fix：必须 finally——store 若在 setFlowError / 状态写入阶段
      // 抛出同步异常，reverting 会永久停在 true，按钮与弹窗一起钉死在「撤销中…」。
      setReverting(false);
    }
  }

  /** 单个文件行（首屏行与「展开其余」里的行共用） */
  const renderRow = (c: FileChangeOut) => {
    const rel = c.path.replace(/\\/g, "/");
    const abs = toAbsPath(root, c.path);
    const { dir, name } = splitFilePath(c.path);
    const deleted = c.action === "deleted";
    return (
      <div className={`tcc-row${deleted ? " is-deleted" : ""}`} key={c.path}>
        <FileBadge path={c.path} size={badgeSize} />
        {/* 文件名 / 目录被截断时，悬停显示完整相对路径（plan-20261019-toolcard-v2） */}
        <Tooltip content={rel} side="top">
          <span className="tcc-path">
            <span className="tcc-name">{name}</span>
            {dir && <span className="tcc-dir">{dir}</span>}
          </span>
        </Tooltip>
        {deleted && <span className="tcc-tag">已删除</span>}
        <span className="tcc-stat">
          <span className="tcc-add">+{c.additions}</span>
          {c.deletions > 0 && <span className="tcc-del">-{c.deletions}</span>}
        </span>
        <div className="tcc-row-ops">
          <Button variant="outline" size="sm" onClick={() => void openDiff(c.path)}>
            审查
          </Button>
          {/* 「打开 ⌄」是分体按钮：左侧直接打开，右侧角标选应用。已删除的文件无从打开，整组隐藏 */}
          {!deleted && (
            <span className="tcc-split">
              <Button variant="outline" size="sm" onClick={() => void openWith("explorer", abs)}>
                打开
              </Button>
              <Menu
                align="end"
                trigger={
                  <button type="button" className="tcc-split-caret" aria-label={`选择打开方式：${name}`}>
                    <IconChevronDown size={iconSmall} />
                  </button>
                }
                entries={menuEntries(rel, abs)}
              />
            </span>
          )}
        </div>
      </div>
    );
  };

  return (
    <div className={`tcc-card${collapsed ? " is-collapsed" : ""}${playEnter ? " is-entering" : ""}`}>
      {/* 表头整行都可点开/收起（用户直觉：点标题行就该展开），
          内部箭头与撤销按钮各自 stopPropagation，避免一次点击触发两次或误触撤销。 */}
      <div
        className="tcc-head"
        onClick={() => setCollapsed((v) => !v)}
      >
        <button
          type="button"
          className={`tcc-chev${collapsed ? "" : " is-open"}`}
          aria-label={collapsed ? "展开本轮文件变更" : "收起本轮文件变更"}
          aria-expanded={!collapsed}
          onClick={(e) => { e.stopPropagation(); setCollapsed((v) => !v); }}
        >
          <IconChevronDown size={iconMain} />
        </button>
        <span className="tcc-count">{files.length} 个文件已更改</span>
        <span className="tcc-stat">
          <span className="tcc-add">+{totals.add}</span>
          <span className="tcc-del">-{totals.del}</span>
        </span>
        <span className="tcc-spacer" />
        <Tooltip content={revertHint}>
          <span className="tcc-revert" onClick={(e) => e.stopPropagation()}>
            <Button
              variant="ghost"
              size="sm"
              icon={<IconRotateCcw size={iconSmall} />}
              disabled={!canRevert || reverting}
              onClick={() => setConfirmOpen(true)}
            >
              撤销
            </Button>
          </span>
        </Tooltip>
      </div>

      {/* plan-20261019-motion-sweep：整张列表的展开 / 收起走 Collapse（真实高度过渡，收起结束才卸载，
          children 用函数——折叠时不构造文件行），此前是 `{!collapsed && …}` 条件渲染，
          点表头列表瞬间出现 / 消失。 */}
      <Collapse open={!collapsed} lazy>
        {() => (
          <div className="tcc-list">
            {headFiles.map(renderRow)}
            {restCount > 0 && <Collapse open={showAll} lazy>{() => restFiles.map(renderRow)}</Collapse>}

            {restCount > 0 && (
              <div className="tcc-more">
                <Button variant="ghost" size="xs" onClick={() => setShowAll((v) => !v)}>
                  {showAll ? "收起" : `展开其余 ${restCount} 个文件`}
                </Button>
              </div>
            )}
          </div>
        )}
      </Collapse>

      {rejected ? (
        <Dialog
          open={confirmOpen}
          onClose={closeRevert}
          width={480}
          title="撤销已取消"
          footer={<Button variant="secondary" onClick={closeRevert}>知道了</Button>}
        >
          <div className="ui-dialog-message">{rejected.message}</div>
          <ul className="tcc-reject-list">
            {rejected.conflicts.map((c) => (
              <li key={c.path} className="tcc-reject-item">
                <span className="tcc-reject-path">{c.path}</span>
                {c.reason && <span className="tcc-reject-reason">{c.reason}</span>}
              </li>
            ))}
          </ul>
        </Dialog>
      ) : (
        <ConfirmDialog
          open={confirmOpen}
          danger
          title="撤销本轮文件改动"
          message={`将把本轮 ${files.length} 个文件恢复到本轮开始前的内容，消息记录不会删除，且不可恢复。若文件在本轮之后又被改动，撤销会整体取消。`}
          confirmLabel="撤销"
          busyLabel="撤销中…"
          busy={reverting}
          onCancel={closeRevert}
          onConfirm={() => void confirmRevert()}
        />
      )}
    </div>
  );
}
