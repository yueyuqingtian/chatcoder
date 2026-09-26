/** 供应商列表（plan-41-225 S3）：无分组标题 + 拖拽排序 + 末尾添加入口。
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
 */
import { useCallback, useEffect, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import { api, type ModelOut, type ProviderOut } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { IconGripVertical, IconPlus } from "../icons";

/** 行高与行距（与 model-picker.css 的 .models-side-item / .models-side-scroll 一致）——
 *  拖拽步距据此计算，两处必须同步修改，否则会出现"错位半行"。 */
const ROW_H = 36;
const ROW_GAP = 2;
const STEP = ROW_H + ROW_GAP;
/** 进入拖拽的纵向位移阈值（px） */
const DRAG_THRESHOLD = 4;

function notify(msg: string) { useChatStore.setState({ error: msg }); }

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
  onAdd: () => void;
}

export function ProviderSideList({
  providers, independentModels, selectedId, statusDotClass, oauthFormats,
  onSelect, onLocalReorder, onAdd,
}: ProviderSideListProps) {
  // 拖拽态：用 state 驱动渲染（ref 不触发重渲染），另有 ref 保存最新值供异步收尾读取
  const [dragId, setDragId] = useState<number | null>(null);
  const [dragStart, setDragStart] = useState(0);
  const [dragOffset, setDragOffset] = useState(0);
  const [overIndex, setOverIndex] = useState<number | null>(null);

  const dragRef = useRef<{ id: number; startIndex: number; startY: number; moved: boolean } | null>(null);
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

  const cancelDrag = useCallback(() => {
    dragRef.current = null;
    setDragId(null);
    setDragOffset(0);
    setOverIndex(null);
  }, []);

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
    dragRef.current = { id: p.id, startIndex: index, startY: e.clientY, moved: false };
    setDragId(p.id);
    setDragStart(index);
    setDragOffset(0);
    setOverIndex(index);
  };

  const onHandlePointerMove = (e: PointerEvent<HTMLSpanElement>) => {
    const st = dragRef.current;
    if (!st) return;
    const dy = e.clientY - st.startY;
    if (!st.moved && Math.abs(dy) < DRAG_THRESHOLD) return;
    st.moved = true;
    setDragOffset(dy);
    const target = Math.max(0, Math.min(providers.length - 1, st.startIndex + Math.round(dy / STEP)));
    setOverIndex(target);
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
      <div className={"models-side-scroll" + (dragId != null ? " is-dragging" : "")}>
        {providers.map((p, index) => {
          const dragging = dragId === p.id;
          const shift = shiftOf(index);
          return (
            <div className="models-side-row" key={p.id}>
              {/* 单行供应商无可排序对象，不渲染手柄（渲染会让用户尝试后困惑） */}
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
                <span className="models-side-icon" aria-hidden>
                  {(p.name || "?").trim().slice(0, 1).toUpperCase()}
                </span>
                <span className="models-side-name" title={p.name}>{p.name}</span>
                {(p.credential_count ?? 0) > 1 && (
                  <span className="models-side-badge">
                    {p.credential_count} {oauthFormats.has(p.api_format) ? "账号" : "Key"}
                  </span>
                )}
                <span className={statusDotClass(p)} />
              </button>
            </div>
          );
        })}
        {providers.length === 0 && <div className="models-side-empty">暂无供应商</div>}

        {/* 添加入口：列表流末尾、随列表滚动 —— "添加"是列表的延续（加在最后） */}
        <button type="button" className="models-side-add" onClick={onAdd}>
          <IconPlus size={13} /> 添加供应商
        </button>

        {/* 独立模型：与供应商是不同实体，末端小标题做弱分隔（仅存在时渲染） */}
        {independentModels.length > 0 && (
          <>
            <div className="models-side-group">独立模型（{independentModels.length}）</div>
            {independentModels.map((m) => (
              <div key={m.id} className="models-side-item static">
                <span className={"models-dot " + (m.is_active ? "on" : "off")} />
                <span className="models-side-name" title={m.name}>{m.name}</span>
              </div>
            ))}
          </>
        )}
      </div>
    </aside>
  );
}
