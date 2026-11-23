/** JumpDots v2：消息区左侧定位横线刻度条（plan-20261121-jumpdots-v2）。
 *  - 静止态极简：仅呈现柔和短横线，无多余上下控件或文字噪声；
 *  - 热区与间距：左缘留白 10px（横条起点 18px 对齐搜索钮），热区 42px 连续无盲区；
 *  - 跨分页全量：合并服务端轻量大纲，支持预览并点击跳转未加载分页；
 *  - 分区展示：常规每区 15 条（矮屏自适应 8~15 条），hover 时切区箭头沿横条中轴原地淡入，
 *    支持点击箭头或热区内滚轮上下切区；
 *  - 富预览浮窗：展示序号、时间、纯净正文摘要（剥离引用行）、图片缩略图、文件芯片与引用标签。 */
import { forwardRef, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { resolveFileUrl, type OutlineRef, type SessionOutlineItem } from "../../api/client";
import {
  IconBox, IconChevronDown, IconChevronUp, IconFileText, IconFolder,
  IconMessageSquare, IconPackage, IconPaperclip, IconPlug,
} from "../icons";
import {
  buildJumpDotItems, computeBlockSize, findActiveDot, formatBytesCompact,
  formatOutlineTime, isImageAttachment, resolveBlockWindow,
  type JumpDotItem,
} from "./jumpDotsModel";
import type { TimelineEntry } from "./timeline";

function refKindIcon(kind: OutlineRef["kind"]) {
  switch (kind) {
    case "dir": return <IconFolder size={11} />;
    case "session": return <IconMessageSquare size={11} />;
    case "mcp": return <IconPlug size={11} />;
    case "plugin": return <IconPackage size={11} />;
    case "file": return <IconFileText size={11} />;
    default: return <IconBox size={11} />;
  }
}

export function JumpDots({
  entries,
  outline = null,
  activeIndex,
  loadingMessageId = null,
  scrollReleaseSignal = 0,
  onJump,
}: {
  entries: TimelineEntry[];
  outline?: readonly SessionOutlineItem[] | null;
  /** scrollspy：消息流当前焦点 entry 在 entries 中的下标 */
  activeIndex?: number;
  /** 正在按需拉取未加载分页的消息 ID */
  loadingMessageId?: number | null;
  /** 用户滚动/拖拽消息流时递增，解除手动翻区的锁定并恢复焦点跟随 */
  scrollReleaseSignal?: number;
  onJump: (item: JumpDotItem) => void;
}) {
  const [hovered, setHovered] = useState<number | null>(null);
  const [hoverArrow, setHoverArrow] = useState<"prev" | "next" | null>(null);
  const [manualBlock, setManualBlock] = useState<number | null>(null);
  const [pagingActive, setPagingActive] = useState(false);
  const [containerHeight, setContainerHeight] = useState<number | null>(null);
  const [cardTop, setCardTop] = useState<number | null>(null);

  const outerRef = useRef<HTMLDivElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const trackRef = useRef<HTMLDivElement | null>(null);
  const cardRef = useRef<HTMLDivElement | null>(null);
  const centersRef = useRef<number[]>([]);
  const centersKeyRef = useRef("");
  const wheelStampRef = useRef(0);
  const pagingTimerRef = useRef(0);

  const items = useMemo(() => buildJumpDotItems(entries, outline), [entries, outline]);
  const activeDot = useMemo(() => findActiveDot(items, activeIndex), [items, activeIndex]);

  // 监听定位条外容器高度，矮窗口自动收缩单区刻度数（8~15 条）
  useEffect(() => {
    const el = outerRef.current;
    if (!el) return;
    const update = () => {
      const h = el.clientHeight;
      if (h > 0) setContainerHeight((prev) => (prev === h ? prev : h));
    };
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, [items.length]);

  useEffect(() => () => {
    window.clearTimeout(pagingTimerRef.current);
  }, []);

  // 用户滚动主消息流时，解除手动翻区锁定，恢复随阅读位置自动切换区块
  const prevReleaseRef = useRef(scrollReleaseSignal);
  useEffect(() => {
    if (prevReleaseRef.current === scrollReleaseSignal) return;
    prevReleaseRef.current = scrollReleaseSignal;
    setManualBlock(null);
  }, [scrollReleaseSignal]);

  const prevActiveDotRef = useRef(activeDot);
  useEffect(() => {
    if (prevActiveDotRef.current === activeDot) return;
    prevActiveDotRef.current = activeDot;
    setManualBlock(null);
  }, [activeDot]);

  const blockSize = useMemo(() => computeBlockSize(containerHeight), [containerHeight]);
  const blockWin = useMemo(
    () => resolveBlockWindow(items.length, blockSize, activeDot, manualBlock),
    [items.length, blockSize, activeDot, manualBlock],
  );
  const visibleItems = useMemo(
    () => items.slice(blockWin.start, blockWin.end),
    [items, blockWin.start, blockWin.end],
  );
  const visibleSignature = `${blockWin.start}:${visibleItems.length}`;

  const measureCenters = useCallback(() => {
    const track = trackRef.current;
    if (!track) return;
    const wraps = track.querySelectorAll<HTMLElement>(".jump-dot-wrap");
    const next: number[] = [];
    wraps.forEach((w) => {
      const r = w.getBoundingClientRect();
      next.push(r.top + r.height / 2);
    });
    centersRef.current = next;
    centersKeyRef.current = visibleSignature;
  }, [visibleSignature]);

  /** 按指针纵向位置二分取最近刻度（消除刻度间隙误判）。 */
  const hoverNearest = useCallback((clientY: number) => {
    if (centersKeyRef.current !== visibleSignature) measureCenters();
    const centers = centersRef.current;
    if (centers.length === 0) return;
    let lo = 0;
    let hi = centers.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (centers[mid] < clientY) lo = mid + 1;
      else hi = mid;
    }
    let best = lo;
    if (lo > 0 && Math.abs(centers[lo - 1] - clientY) <= Math.abs(centers[lo] - clientY)) {
      best = lo - 1;
    }
    setHoverArrow(null);
    setHovered(blockWin.start + best);
  }, [visibleSignature, measureCenters, blockWin.start]);

  const stepBlock = useCallback((dir: -1 | 1) => {
    setManualBlock((prev) => {
      const cur = prev ?? blockWin.focusBlock;
      return Math.max(0, Math.min(blockWin.blockCount - 1, cur + dir));
    });
    setHovered(null);
  }, [blockWin.focusBlock, blockWin.blockCount]);

  // 热区内滚轮切区（必须非 passive 阻止透传滚动消息流）
  useEffect(() => {
    const panel = panelRef.current;
    if (!panel || !blockWin.showPager) return;
    const onWheel = (e: WheelEvent) => {
      if (Math.abs(e.deltaY) < 2) return;
      e.preventDefault();
      e.stopPropagation();
      const now = performance.now();
      if (now - wheelStampRef.current < 90) return;
      wheelStampRef.current = now;
      stepBlock(e.deltaY > 0 ? 1 : -1);
      setPagingActive(true);
      window.clearTimeout(pagingTimerRef.current);
      pagingTimerRef.current = window.setTimeout(() => setPagingActive(false), 900);
    };
    panel.addEventListener("wheel", onWheel, { passive: false });
    return () => panel.removeEventListener("wheel", onWheel);
  }, [blockWin.showPager, stepBlock]);

  const hoveredLocalIdx = hovered != null && hovered >= blockWin.start && hovered < blockWin.end
    ? hovered - blockWin.start
    : -1;
  const hoveredItem = hoveredLocalIdx >= 0 ? visibleItems[hoveredLocalIdx] : null;

  // 浮窗纵向边界钳制：保证首条/末条触发时浮窗不被消息流外框裁剪
  useLayoutEffect(() => {
    const card = cardRef.current;
    const panel = panelRef.current;
    const track = trackRef.current;
    const outer = outerRef.current;
    if (!card || !panel || !track || !outer || hoveredLocalIdx < 0) {
      setCardTop(null);
      return;
    }
    const wraps = track.querySelectorAll<HTMLElement>(".jump-dot-wrap");
    const target = wraps[hoveredLocalIdx];
    const centerY = target
      ? target.offsetTop + track.offsetTop + target.offsetHeight / 2
      : track.offsetTop + hoveredLocalIdx * 11 + 5.5;
    const cardH = card.offsetHeight;
    const panelTopInOuter = panel.offsetTop;
    const minTop = -panelTopInOuter + 4;
    const maxTop = Math.max(minTop, outer.clientHeight - panelTopInOuter - cardH - 4);
    const clamped = Math.max(minTop, Math.min(maxTop, centerY - cardH / 2));
    setCardTop(clamped);
  }, [hoveredLocalIdx, hoveredItem?.key, hoveredItem?.attachments.length, hoveredItem?.refs.length]);

  if (items.length < 2) return null;

  // 右侧切区提示文案
  let pagerHintText = "";
  if (hoverArrow === "prev") {
    if (blockWin.currentBlock === 0) {
      pagerHintText = `已是第一区 (${blockWin.start + 1}–${blockWin.end} / ${items.length})`;
    } else {
      const ps = (blockWin.currentBlock - 1) * blockSize + 1;
      const pe = blockWin.currentBlock * blockSize;
      pagerHintText = `上一区 · ${ps}–${pe}（共 ${items.length} 条）`;
    }
  } else if (hoverArrow === "next") {
    if (blockWin.currentBlock >= blockWin.blockCount - 1) {
      pagerHintText = `已是最新一区 (${blockWin.start + 1}–${blockWin.end} / ${items.length})`;
    } else {
      const ns = (blockWin.currentBlock + 1) * blockSize + 1;
      const ne = Math.min(items.length, (blockWin.currentBlock + 2) * blockSize);
      pagerHintText = `下一区 · ${ns}–${ne}（共 ${items.length} 条）`;
    }
  } else if (pagingActive && blockWin.showPager) {
    pagerHintText = `第 ${blockWin.currentBlock + 1}/${blockWin.blockCount} 区 · ${blockWin.start + 1}–${blockWin.end} / ${items.length}`;
  }

  return (
    <div ref={outerRef} className="jump-dots">
      <div
        ref={panelRef}
        className={`jump-dots-panel${pagingActive ? " is-paging" : ""}`}
        onMouseLeave={() => {
          setHovered(null);
          setHoverArrow(null);
        }}
      >
        {blockWin.showPager && (
          <button
            type="button"
            className="jump-pager-btn is-prev"
            disabled={blockWin.currentBlock === 0}
            aria-label="上一区块"
            onMouseEnter={() => {
              setHovered(null);
              setHoverArrow("prev");
            }}
            onMouseLeave={() => setHoverArrow((cur) => (cur === "prev" ? null : cur))}
            onClick={() => stepBlock(-1)}
          >
            <IconChevronUp size={10} strokeWidth={2.2} />
          </button>
        )}

        <div
          ref={trackRef}
          className="jump-dots-track"
          onPointerEnter={measureCenters}
          onPointerMove={(e) => hoverNearest(e.clientY)}
        >
          {visibleItems.map((item, localIdx) => {
            const globalIdx = blockWin.start + localIdx;
            const isActive = globalIdx === activeDot;
            const isLoading = loadingMessageId != null && loadingMessageId === item.messageId;
            const diff = hovered !== null ? Math.abs(hovered - globalIdx) : null;

            let width = 8;
            let opacity = isActive && diff === null ? 1 : item.loaded ? 0.34 : 0.2;
            let barHeight = isActive && diff === null ? "2px" : "1.5px";

            if (diff === 0) {
              width = 18;
              opacity = 1;
              barHeight = "2px";
            } else if (diff === 1) {
              width = 13;
              opacity = 0.72;
            } else if (diff === 2) {
              width = 10;
              opacity = 0.5;
            }

            return (
              <div key={item.key} className="jump-dot-wrap">
                <button
                  type="button"
                  className="jump-tick-btn"
                  aria-label={`跳转到第 ${globalIdx + 1} 条`}
                  onClick={() => {
                    setManualBlock(null);
                    setHovered(null);
                    onJump(item);
                  }}
                >
                  <span
                    className={`jump-tick-bar${isLoading ? " is-loading" : ""}`}
                    style={{
                      width: `${width}px`,
                      opacity,
                      height: barHeight,
                      backgroundColor: isLoading
                        ? "var(--accent)"
                        : diff === 0 || (diff === null && isActive)
                          ? "var(--text-1)"
                          : "var(--text-3)",
                    }}
                  />
                </button>
              </div>
            );
          })}
        </div>

        {blockWin.showPager && (
          <button
            type="button"
            className="jump-pager-btn is-next"
            disabled={blockWin.currentBlock >= blockWin.blockCount - 1}
            aria-label="下一区块"
            onMouseEnter={() => {
              setHovered(null);
              setHoverArrow("next");
            }}
            onMouseLeave={() => setHoverArrow((cur) => (cur === "next" ? null : cur))}
            onClick={() => stepBlock(1)}
          >
            <IconChevronDown size={10} strokeWidth={2.2} />
          </button>
        )}

        {pagerHintText && hoveredItem == null && (
          <div className={`jump-pager-hint${hoverArrow === "prev" ? " is-top" : hoverArrow === "next" ? " is-bottom" : " is-mid"}`}>
            {pagerHintText}
          </div>
        )}

        {hoveredItem && (
          <JumpDotPreviewCard
            ref={cardRef}
            item={hoveredItem}
            ordinal={hovered! + 1}
            isActive={hovered === activeDot}
            isLoading={loadingMessageId != null && loadingMessageId === hoveredItem.messageId}
            top={cardTop}
          />
        )}
      </div>
    </div>
  );
}

const JumpDotPreviewCard = forwardRef<HTMLDivElement, {
  item: JumpDotItem;
  ordinal: number;
  isActive: boolean;
  isLoading: boolean;
  top: number | null;
}>(function JumpDotPreviewCard({ item, ordinal, isActive, isLoading, top }, ref) {
  const timeText = formatOutlineTime(item.createdAt);
  const images = item.attachments.filter(isImageAttachment);
  const files = item.attachments.filter((a) => !isImageAttachment(a));
  const hasExtras = images.length > 0 || files.length > 0 || item.refs.length > 0;

  let badgeLabel = "";
  let badgeClass = "";
  if (isLoading) {
    badgeLabel = "加载中…";
    badgeClass = "is-loading";
  } else if (isActive) {
    badgeLabel = "当前位置";
    badgeClass = "is-active";
  } else if (!item.loaded) {
    badgeLabel = "未加载 · 点击定位";
    badgeClass = "is-unloaded";
  }

  return (
    <div
      ref={ref}
      className="jump-dot-card"
      style={top != null ? { top: `${top}px` } : { top: "50%", transform: "translateY(-50%)" }}
    >
      <div className="jump-dot-card-head">
        <div className="jump-dot-card-meta">
          <span className="jump-dot-card-index">#{ordinal}</span>
          {badgeLabel && <span className={`jump-dot-card-badge ${badgeClass}`}>{badgeLabel}</span>}
        </div>
        {timeText && <span className="jump-dot-card-time">{timeText}</span>}
      </div>

      <div className={`jump-dot-card-text${!item.text ? " is-empty" : ""}`}>
        {item.text || "仅包含附件内容"}
      </div>

      {hasExtras && (
        <div className="jump-dot-card-extras">
          {images.length > 0 && (
            <div className="jump-dot-card-images">
              {images.slice(0, 3).map((img, idx) => (
                <div key={img.file_id || img.url || idx} className="jump-dot-card-thumb-wrap">
                  <img
                    className="jump-dot-card-thumb"
                    src={resolveFileUrl(img.url)}
                    alt={img.filename}
                    loading="lazy"
                    decoding="async"
                  />
                  {idx === 2 && images.length > 3 && (
                    <span className="jump-dot-card-thumb-more">+{images.length - 3}</span>
                  )}
                </div>
              ))}
              <span className="jump-dot-card-img-names">
                {images.map((i) => i.filename).filter(Boolean).join("、")}
              </span>
            </div>
          )}

          {files.length > 0 && (
            <div className="jump-dot-card-chips">
              {files.slice(0, 2).map((f, idx) => {
                const sz = formatBytesCompact(f.size);
                return (
                  <span key={f.file_id || f.url || idx} className="jump-dot-card-chip">
                    <IconPaperclip size={11} />
                    <span className="jump-dot-card-chip-name">{f.filename}</span>
                    {sz && <span className="jump-dot-card-chip-size">{sz}</span>}
                  </span>
                );
              })}
              {files.length > 2 && <span className="jump-dot-card-more">+{files.length - 2} 个文件</span>}
            </div>
          )}

          {item.refs.length > 0 && (
            <div className="jump-dot-card-chips">
              {item.refs.slice(0, 3).map((r, idx) => (
                <span key={`${r.kind}-${r.value}-${idx}`} className="jump-dot-card-ref">
                  {refKindIcon(r.kind)}
                  <span className="jump-dot-card-chip-name">{r.label || r.value}</span>
                </span>
              ))}
              {item.refs.length > 3 && <span className="jump-dot-card-more">+{item.refs.length - 3}</span>}
            </div>
          )}
        </div>
      )}
    </div>
  );
});
