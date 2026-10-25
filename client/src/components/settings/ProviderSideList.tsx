/** 供应商列表（plan-41-225 S3）：无分组标题 + 拖拽排序。
 *
 * 设计要点（详见 ai/chatcoder-plan-41-225.md §2）：
 *  - **不分组**：列表里只有供应商一种实体，分组标题不提供任何区分信息，只是视觉噪声；
 *    内置与自建在操作上完全同级（都能改 URL / 加 Key / 加模型 / 启停 / 删除），
 *    拆成两组会制造"内置的不能删"这类不存在的差异。独立模型是**另一种实体**，
 *    用末端小标题做弱分隔（仅存在时渲染）。
 *  - **拖拽手柄**：行最左，hover / 键盘聚焦时淡入。绝对定位（不占文档流），
 *    保证出现/消失不引起布局位移 —— 列表不抖动。
 *  - **仅手柄可拖**：行主体仍是"点击选中"，两种意图物理隔离，避免点选手抖触发拖拽。
 *  - **4px 阈值**：按住手柄的轻微抖动不算拖拽（误操作防护）。
 *  - **乐观更新**：松手立即按本地顺序渲染（无 loading、不闪烁），后台静默提交，
 *    失败才回滚并提示 —— 否则一次网络往返内列表会跳回旧顺序，观感是"拖了没用"。
 *  - **键盘可达**：手柄可 Tab 聚焦，↑/↓ 移动一行、Enter 确认、Esc 取消。
 *  - 列表末尾不再放「添加供应商」：顶部工具栏已有同名主按钮，左列重复入口在长列表里还会被滚走。
 *  - 供应商多到溢出时，拖到可视区上下边缘自动滚动（位移含 scroll 增量，被拖行仍跟着指针）。
 */
import { useCallback, useEffect, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import { api, type ModelOut, type ProviderOut } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { IconGripVertical } from "../icons";
import { ProviderAvatar } from "./ProviderAvatar";

/** 行高与行距（与 settings-models.css 的 .models-side-item / .models-side-scroll 一致）——
 *  拖拽步距据此计算，两处必须同步修改，否则会出现"错位半行"。
 *  plan-114-450：行高 40，容纳 28px 头像。 */
const ROW_H = 40;
const ROW_GAP = 2;
const STEP = ROW_H + ROW_GAP;
/** 进入拖拽的纵向位移阈值（px） */
const DRAG_THRESHOLD = 4;
/** 拖到距列表上下边缘多少像素内开始自动滚动 */
const AUTOSCROLL_EDGE = 44;
/** 自动滚动每帧最大位移（px）：按贴近边缘的程度线性量增 */
const AUTOSCROLL_MAX_STEP = 14;

function notify(msg: string) { useChatStore.setState({ error: msg }); }

/** plan-565-2323 G2：左侧行尾凭据徽标文案。
 *  credential_count 是全部凭据（含停用），active_credential_count 只算当前可用
 *  （后端口径：is_active 且未暂缓）；差值为不可用数。两者相等（或后端未返回该字段）时
 *  不显示差值，只报总数。总数 ≤ 1 时不渲染徽标（单个账号不必占位）。 */
function credBadgeText(p: ProviderOut, oauthFormats: Set<string>): string | null {
  const total = p.credential_count ?? 0;
  if (total === 0) return null;
  const active = p.active_credential_count ?? total;
  const off = Math.max(0, total - active);
  // 单个凭据且可用时不占位（与既有行为一致）；单个却不可用时必须显示，
  // 否则左列完全看不出「唯一的账号被停用了」。
  if (total === 1 && off === 0) return null;
  const unit = oauthFormats.has(p.api_format) ? "账号" : "Key";
  return off > 0 ? `${total} ${unit} · ${off} 停用` : `${total} ${unit}`;
}

/** 把 from 位置的元素移动到 to 位置（返回新数组，不改原数组） */
function moveItem<T>(list: T[], from: number, to: number): T[] {
  const next = [...list];
  const [item] = next.splice(from, 1);
  next.splice(to, 0, item);
  return next;
}

interface ProviderSideListProps {
  providers: ProviderOut[];
  independentModels: ModelOut[];
  selectedId: number | null;
  /** 状态圆点类名（父组件持有，避免重复定义导致漂移） */
  statusDotClass: (p: ProviderOut) => string;
  /** 登录类供应商集合（决定凭据徽标文案是「Key」还是「账号」） */
  oauthFormats: Set<string>;
  onSelect: (id: number) => void;
  /** 本地顺序更新（乐观渲染 / 失败回滚共用） */
  onLocalReorder: (next: ProviderOut[]) => void;
}

export function ProviderSideList({
  providers, independentModels, selectedId, statusDotClass, oauthFormats,
  onSelect, onLocalReorder,
}: ProviderSideListProps) {
  // 拖拽态：用 state 驱动渲染（ref 不触发重渲染），另有 ref 保存最新值供异步收尾读取
  const [dragId, setDragId] = useState<number | null>(null);
  const [dragStart, setDragStart] = useState(0);
  const [dragOffset, setDragOffset] = useState(0);
  const [overIndex, setOverIndex] = useState<number | null>(null);

  const dragRef = useRef<{ id: number; startIndex: number; startY: number; moved: boolean; scroll0: number; lastY: number; tickTs: number } | null>(null);
  /** 列表滚动容器：溢出时拖到边缘要自动滚动 */
  const scrollRef = useRef<HTMLDivElement>(null);
  /** 自动滚动的 rAF 句柄（拖拽结束必须停，否则松手后还在滚） */
  const autoScrollRef = useRef<number | null>(null);
  /** 拖拽结束后立即到来的那次 click 需要被忽略（拖完不该顺带切换右列详情） */
  const suppressClickRef = useRef(false);
  /** 键盘排序：起始顺序快照（Esc 回滚用；Enter 确认后清空） */
  const kbBaseRef = useRef<ProviderOut[] | null>(null);

  const providersRef = useRef(providers);
  const overIndexRef = useRef<number | null>(null);
  useEffect(() => { providersRef.current = providers; }, [providers]);
  useEffect(() => { overIndexRef.current = overIndex; }, [overIndex]);

  /** 提交顺序：乐观渲染 → 静默保存 → 失败回滚 + 提示 */
  const commit = useCallback(async (next: ProviderOut[], rollbackTo: ProviderOut[]) => {
    onLocalReorder(next);
    try {
      await api.reorderProviders(next.map((p) => p.id));
    } catch (e) {
      onLocalReorder(rollbackTo);
      notify("保存顺序失败：" + String(e));
    }
  }, [onLocalReorder]);

  /** 停掉自动滚动（松手 / 取消 / 卸载都必须调用，否则 rAF 会一直跑） */
  const stopAutoScroll = useCallback(() => {
    if (autoScrollRef.current != null) {
      cancelAnimationFrame(autoScrollRef.current);
      autoScrollRef.current = null;
    }
  }, []);

  /** 按当前指针位置更新被拖行的位移与目标行。
   *  位移含容器滚动增量：列表自动滚过多少，指针相对内容就多走了多少——
   *  否则滚到边缘后目标行不再变化，拖拽会「卡在屏幕边缘」。 */
  const applyDrag = useCallback((clientY: number) => {
    const st = dragRef.current;
    if (!st) return;
    const box = scrollRef.current;
    const scrolled = box ? box.scrollTop - st.scroll0 : 0;
    const dy = clientY - st.startY + scrolled;
    if (!st.moved && Math.abs(dy) < DRAG_THRESHOLD) return;
    st.moved = true;
    setDragOffset(dy);
    const count = providersRef.current.length;
    const target = Math.max(0, Math.min(count - 1, st.startIndex + Math.round(dy / STEP)));
    setOverIndex(target);
  }, []);

  /** 自动滚动：指针停在上下边缘带内即持续滚动，越贴边越快。
   *  位移按真实经过时间换算（而不是每帧固定步长）：rAF 在窗口失焦 / 后台标签下会被节流到几 fps，
   *  固定步长会让「拖到边缘」几乎滚不动；这里以 60fps 为基准缩放，并把单帧系数封顶，
   *  节流时一次多滚一点、仍能滚到目标，正常时依旧平滑。
   *  只在真正滚动成功时重算目标行，避免到顶/到底后空转刷渲染。 */
  const autoScrollTick = useCallback((ts: number) => {
    const st = dragRef.current;
    const box = scrollRef.current;
    if (!st || !box) { autoScrollRef.current = null; return; }
    const prevTs = st.tickTs || ts;
    st.tickTs = ts;
    const frames = Math.min(3, Math.max(1, (ts - prevTs) / 16.67));
    const rect = box.getBoundingClientRect();
    const distTop = st.lastY - rect.top;
    const distBottom = rect.bottom - st.lastY;
    let delta = 0;
    if (distTop < AUTOSCROLL_EDGE) {
      delta = -AUTOSCROLL_MAX_STEP * frames * Math.min(1, (AUTOSCROLL_EDGE - distTop) / AUTOSCROLL_EDGE);
    } else if (distBottom < AUTOSCROLL_EDGE) {
      delta = AUTOSCROLL_MAX_STEP * frames * Math.min(1, (AUTOSCROLL_EDGE - distBottom) / AUTOSCROLL_EDGE);
    }
    if (Math.abs(delta) >= 1) {
      const before = box.scrollTop;
      const max = box.scrollHeight - box.clientHeight;
      box.scrollTop = Math.max(0, Math.min(max, before + Math.round(delta)));
      if (box.scrollTop !== before) applyDrag(st.lastY);
    }
    autoScrollRef.current = requestAnimationFrame(autoScrollTick);
  }, [applyDrag]);

  const cancelDrag = useCallback(() => {
    stopAutoScroll();
    dragRef.current = null;
    setDragId(null);
    setDragOffset(0);
    setOverIndex(null);
  }, [stopAutoScroll]);

  // 卸载兜底：组件被切走时 rAF 不能留
  useEffect(() => () => stopAutoScroll(), [stopAutoScroll]);

  // 拖拽中按 Esc 取消（回到起始顺序，且不产生任何请求）
  useEffect(() => {
    if (dragId == null) return;
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape") { e.preventDefault(); cancelDrag(); }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [dragId, cancelDrag]);

  const onHandlePointerDown = (e: PointerEvent<HTMLSpanElement>, index: number, p: ProviderOut) => {
    if (providers.length < 2) return;
    e.preventDefault();
    e.stopPropagation();
    // 指针捕获：指针移出列表/窗口后仍能收到 move/up，避免卡在拖拽态
    try { e.currentTarget.setPointerCapture(e.pointerId); } catch { /* 环境不支持时忽略 */ }
    dragRef.current = {
      id: p.id, startIndex: index, startY: e.clientY, moved: false,
      scroll0: scrollRef.current?.scrollTop ?? 0, lastY: e.clientY, tickTs: 0,
    };
    setDragId(p.id);
    setDragStart(index);
    setDragOffset(0);
    setOverIndex(index);
    stopAutoScroll();
    autoScrollRef.current = requestAnimationFrame(autoScrollTick);
  };

  const onHandlePointerMove = (e: PointerEvent<HTMLSpanElement>) => {
    const st = dragRef.current;
    if (!st) return;
    st.lastY = e.clientY;
    applyDrag(e.clientY);
  };

  const onHandlePointerUp = (e: PointerEvent<HTMLSpanElement>) => {
    const st = dragRef.current;
    if (!st) return;
    try { e.currentTarget.releasePointerCapture(e.pointerId); } catch { /* 忽略 */ }
    const moved = st.moved;
    const from = st.startIndex;
    const to = overIndexRef.current;
    cancelDrag();
    // 没真正移动（含原地松手）→ 不写库，避免无意义请求
    if (!moved || to == null || to === from) return;
    suppressClickRef.current = true;
    // plan-568-2333：手柄与可选按钮是兄弟节点，拖拽结束的 click 落在手柄上，
    // 按钮的 onClick 永远消费不到这个标志——此前会一直留 true，把之后第一次点击也吞掉
    // （用户每次拖完排序都要点两下）。主动在下一轮任务复位：仍挡住紧随其后的合成 click，
    // 又不影响后续正常点击。
    window.setTimeout(() => { suppressClickRef.current = false; }, 0);
    const prev = providersRef.current;
    void commit(moveItem(prev, from, to), prev);
  };

  /** 键盘排序：↑/↓ 移动一行（乐观提交）、Enter 确认、Esc 回滚 */
  const onHandleKeyDown = (e: KeyboardEvent<HTMLSpanElement>, index: number, p: ProviderOut) => {
    if (e.key === "ArrowUp" || e.key === "ArrowDown") {
      e.preventDefault();
      const dir = e.key === "ArrowUp" ? -1 : 1;
      const target = Math.max(0, Math.min(providers.length - 1, index + dir));
      if (target === index) return;
      if (kbBaseRef.current == null) kbBaseRef.current = providers;
      const prev = providersRef.current;
      void commit(moveItem(prev, index, target), prev);
      return;
    }
    if (e.key === "Enter") {
      e.preventDefault();
      kbBaseRef.current = null; // 顺序已实时保存，Enter 表示确认结束
      return;
    }
    if (e.key === "Escape") {
      e.preventDefault();
      const base = kbBaseRef.current;
      if (base) {
        kbBaseRef.current = null;
        void commit(base, providersRef.current);
      }
      return;
    }
    // 空格会滚动页面，拖拽手柄上无额外语义，拦掉避免误操作
    if (e.key === " ") e.preventDefault();
    void p;
  };

  /** 其余行的让位位移：被拖行跨越的行整体让出一格 */
  const shiftOf = (index: number): number => {
    if (dragId == null || overIndex == null) return 0;
    if (index === dragStart) return 0;
    if (dragStart < overIndex && index > dragStart && index <= overIndex) return -STEP;
    if (dragStart > overIndex && index < dragStart && index >= overIndex) return STEP;
    return 0;
  };

  return (
    <aside className="models-side">
      {/* user-select: none 由 .models-side-scroll 承担，防止原生文本拖拽抢事件 */}
      <div ref={scrollRef} className={"models-side-scroll" + (dragId != null ? " is-dragging" : "")}>
        {providers.map((p, index) => {
          const dragging = dragId === p.id;
          const shift = shiftOf(index);
          return (
            <div className="models-side-row" key={p.id}>
              {/* 单行供应商无可排序对象，不渲染手柄（渲染会让用户尝试后困惑）。
                  plan-114-450：手柄移到行尾（与状态点同槽位，悬停时互换），此前在行首与头像重叠 */}
              {providers.length > 1 && (
                <span
                  className="models-drag-handle"
                  role="button"
                  tabIndex={0}
                  aria-label={`拖动调整「${p.name}」的顺序`}
                  draggable={false}
                  onPointerDown={(e) => onHandlePointerDown(e, index, p)}
                  onPointerMove={onHandlePointerMove}
                  onPointerUp={onHandlePointerUp}
                  onPointerCancel={cancelDrag}
                  onKeyDown={(e) => onHandleKeyDown(e, index, p)}
                >
                  <IconGripVertical size={14} />
                </span>
              )}
              <button
                type="button"
                className={"models-side-item" + (p.id === selectedId ? " active" : "") + (dragging ? " dragging" : "")}
                style={
                  dragging
                    ? { transform: `translateY(${dragOffset}px)` }
                    : (shift ? { transform: `translateY(${shift}px)` } : undefined)
                }
                onClick={() => {
                  if (suppressClickRef.current) { suppressClickRef.current = false; return; }
                  onSelect(p.id);
                }}
              >
                {/* 首字母图标 + 名称 + 凭据徽标 + 状态圆点（右置：先"是谁"再"怎么样"） */}
                <ProviderAvatar name={p.name} size={26} muted={!p.is_active} />
                <span className="models-side-name">{p.name}</span>
                {/* plan-565-2323 G2：徽标同时体现停用数（「2 账号 · 1 停用」），两者相等时只显示「N 账号」。
                    flexShrink/nowrap + 等宽数字：文案变长只压缩名称区（本来就有省略号），
                    行尾状态点是固定 20px 槽位，不会被推得左右跳动。 */}
                {credBadgeText(p, oauthFormats) && (
                  <span
                    className="models-side-badge"
                    style={{ flexShrink: 0, whiteSpace: "nowrap", fontVariantNumeric: "tabular-nums" }}
                  >
                    {credBadgeText(p, oauthFormats)}
                  </span>
                )}
                <span className="models-side-tail"><span className={statusDotClass(p)} /></span>
              </button>
            </div>
          );
        })}
        {providers.length === 0 && <div className="models-side-empty">暂无供应商</div>}

        {/* 独立模型：与供应商是不同实体，末端小标题做弱分隔（仅存在时渲染） */}
        {independentModels.length > 0 && (
          <>
            <div className="models-side-group">独立模型（{independentModels.length}）</div>
            {independentModels.map((m) => (
              <div key={m.id} className="models-side-item static">
                <span className={"models-dot " + (m.is_active ? "on" : "off")} />
                <span className="models-side-name">{m.name}</span>
              </div>
            ))}
          </>
        )}
      </div>
    </aside>
  );
}
