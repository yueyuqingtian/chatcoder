/** ImageGallery（会话 229）：全局图片查看器——左右切换 / 缩放（− 100% +）/ 下载 / 关闭。
 *
 * 所有图片入口（消息附件、输入框附件、浏览器标注截图、Markdown 图片）共用同一查看器，
 * 打开方式：openGallery(images, index)（store/gallery.ts）。
 * 交互对齐参考图 2：右上角下载 + ×、两侧圆形箭头（多图）、底部中央缩放条、Esc/点遮罩关闭。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  IconChevronLeft,
  IconChevronRight,
  IconDownload,
  IconMinus,
  IconPlus,
  IconX,
} from "../icons";
import { useGalleryStore } from "../../store/gallery";
import { Tooltip } from "../ui";

/** 缩放阈值：支持 20% ~ 1000% 无极缩放 */
const ZOOM_MIN = 20;
const ZOOM_MAX = 1000;

/** 加减按钮标准档位：在档位间步进，提供平滑工整的加减体验 */
const ZOOM_STOPS = [
  20, 25, 33, 50, 67, 75, 90, 100, 125, 150, 175, 200, 250, 300, 400, 500, 600, 800, 1000,
];

export function ImageGallery() {
  const images = useGalleryStore((s) => s.images);
  const index = useGalleryStore((s) => s.index);
  const close = useGalleryStore((s) => s.close);
  const setIndex = useGalleryStore((s) => s.setIndex);

  /** 当前缩放百分比（100 表示初始自适应视口完整呈现的基准尺寸） */
  const [zoom, setZoom] = useState(100);
  /** 平移偏移量（像素） */
  const [offset, setOffset] = useState<{ x: number; y: number }>({ x: 0, y: 0 });
  /** 图片自然尺寸 */
  const [natural, setNatural] = useState<{ w: number; h: number } | null>(null);
  /** 舞台视口尺寸（用于计算 100% 时的自适应基准宽高） */
  const [stageSize, setStageSize] = useState<{ w: number; h: number }>({
    w: typeof window !== "undefined" ? window.innerWidth : 1200,
    h: typeof window !== "undefined" ? window.innerHeight : 800,
  });
  /** 是否正在平移拖拽 */
  const [dragging, setDragging] = useState(false);
  /** 是否启用平滑过渡动画（加减按钮、双击、恢复 100% 时开启，滚轮与拖拽时关闭以保证 0 延迟实时跟手） */
  const [animating, setAnimating] = useState(false);

  const stageRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<{ startX: number; startY: number; originX: number; originY: number } | null>(null);
  /** 是否构成拖拽位移：拖拽结束后的 click 不触发背景关闭 */
  const draggedRef = useRef(false);
  const animTimerRef = useRef<number | null>(null);
  const closeBtnRef = useRef<HTMLButtonElement>(null);
  const prevFocusRef = useRef<HTMLElement | null>(null);

  const current = images[index] ?? null;
  const multi = images.length > 1;

  // 触发平滑过渡动画（短暂保持后自动复位）
  const triggerAnimation = useCallback(() => {
    setAnimating(true);
    if (animTimerRef.current) window.clearTimeout(animTimerRef.current);
    animTimerRef.current = window.setTimeout(() => setAnimating(false), 240);
  }, []);

  // 切换图片时重置缩放、平移与测量状态
  useEffect(() => {
    setZoom(100);
    setOffset({ x: 0, y: 0 });
    setNatural(null);
    dragRef.current = null;
    draggedRef.current = false;
    setDragging(false);
    setAnimating(false);
  }, [current?.url]);

  // 测量舞台尺寸（包括窗口 resize）
  useEffect(() => {
    if (!current) return;
    const updateStageSize = () => {
      const st = stageRef.current;
      if (st) {
        setStageSize({ w: st.clientWidth, h: st.clientHeight });
      } else {
        setStageSize({ w: window.innerWidth, h: window.innerHeight });
      }
    };
    updateStageSize();
    window.addEventListener("resize", updateStageSize);
    return () => window.removeEventListener("resize", updateStageSize);
  }, [current]);

  // 打开时把焦点移入弹窗（关闭按钮），关闭时归还给原元素
  useEffect(() => {
    if (!current) return;
    prevFocusRef.current = (document.activeElement as HTMLElement) ?? null;
    const raf = requestAnimationFrame(() => closeBtnRef.current?.focus());
    return () => {
      cancelAnimationFrame(raf);
      const prev = prevFocusRef.current;
      if (prev && document.contains(prev)) {
        try { prev.focus({ preventScroll: true }); } catch { /* 忽略 */ }
      }
    };
  }, [current != null]);

  // 计算 100% 时的基准显示尺寸（自适应视口最佳呈现：最大不超过视口 92vw/84vh，且小图保持 1:1 不拉伸失真）
  const baseSize = useMemo(() => {
    if (!natural) return null;
    const availW = Math.max(300, stageSize.w * 0.92);
    const availH = Math.max(200, stageSize.h * 0.84);
    let scale = 1;
    if (natural.w > availW || natural.h > availH) {
      scale = Math.min(availW / natural.w, availH / natural.h);
    }
    return {
      w: Math.round(natural.w * scale),
      h: Math.round(natural.h * scale),
      fitScale: scale,
    };
  }, [natural, stageSize]);

  // 放大（点击加号或键盘快捷键）
  const zoomIn = useCallback(() => {
    triggerAnimation();
    setZoom((curr) => {
      const next = ZOOM_STOPS.find((s) => s > curr + 0.5) ?? Math.min(ZOOM_MAX, Math.round(curr + 25));
      const ratio = next / curr;
      // 以中心为基准平移补偿
      setOffset((prev) => ({ x: Math.round(prev.x * ratio), y: Math.round(prev.y * ratio) }));
      return next;
    });
  }, [triggerAnimation]);

  // 缩小（点击减号或键盘快捷键）
  const zoomOut = useCallback(() => {
    triggerAnimation();
    setZoom((curr) => {
      const stops = [...ZOOM_STOPS].reverse();
      const next = stops.find((s) => s < curr - 0.5) ?? Math.max(ZOOM_MIN, Math.round(curr - 25));
      const ratio = next / curr;
      setOffset((prev) => ({ x: Math.round(prev.x * ratio), y: Math.round(prev.y * ratio) }));
      return next;
    });
  }, [triggerAnimation]);

  // 恢复 100% 居中
  const resetZoom = useCallback(() => {
    triggerAnimation();
    setZoom(100);
    setOffset({ x: 0, y: 0 });
  }, [triggerAnimation]);

  // 双击：若非 100% 居中则复原；若已是 100% 居中则放大到细节查看态（200% 或原图 1:1 分辨率）
  const onDoubleClick = useCallback((e: React.MouseEvent) => {
    e.stopPropagation();
    triggerAnimation();
    if (zoom !== 100 || offset.x !== 0 || offset.y !== 0) {
      resetZoom();
    } else {
      // 放大目标：如果有 baseSize 且原图分辨率较高，优先放大到原图 1:1 像素，否则 200%
      const targetZoom = baseSize && baseSize.fitScale < 1
        ? Math.min(ZOOM_MAX, Math.max(200, Math.round((1 / baseSize.fitScale) * 100)))
        : 200;
      
      const st = stageRef.current;
      if (st) {
        const rect = st.getBoundingClientRect();
        const mouseX = e.clientX - rect.left;
        const mouseY = e.clientY - rect.top;
        const cx = rect.width / 2;
        const cy = rect.height / 2;
        const r = targetZoom / 100;
        setOffset({
          x: Math.round((mouseX - cx) * (1 - r)),
          y: Math.round((mouseY - cy) * (1 - r)),
        });
      }
      setZoom(targetZoom);
    }
  }, [zoom, offset, baseSize, resetZoom, triggerAnimation]);

  // 鼠标滚轮滑动：无极放大/缩小并以光标所在内容为中心锚定
  useEffect(() => {
    const stage = stageRef.current;
    if (!stage || !current) return;

    const handleWheel = (e: WheelEvent) => {
      // 必须阻止默认滚动
      e.preventDefault();
      if (!e.deltaY) return;

      const rect = stage.getBoundingClientRect();
      const mouseX = e.clientX - rect.left;
      const mouseY = e.clientY - rect.top;
      const centerX = rect.width / 2;
      const centerY = rect.height / 2;

      // 标准化 deltaY
      let dy = e.deltaY;
      if (e.deltaMode === 1) dy *= 20;
      else if (e.deltaMode === 2) dy *= 100;
      dy = Math.max(-120, Math.min(120, dy));

      // 缩放系数（向上滚动 dy < 0 放大，向下滚动 dy > 0 缩小）
      const factor = Math.exp(-dy * 0.002);

      // 滚轮无极缩放时禁用 CSS transition 保证 0 延迟实时跟手
      setAnimating(false);

      setZoom((currentZoom) => {
        let nextZoom = currentZoom * factor;
        // 在 100% 极小邻域给予轻微吸附感，便于随手滚回 100%
        if (Math.abs(nextZoom - 100) < 1.8 && Math.abs(currentZoom - 100) >= 1.8) {
          nextZoom = 100;
        }
        nextZoom = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, Math.round(nextZoom * 10) / 10));
        if (nextZoom === currentZoom) return currentZoom;

        const ratio = nextZoom / currentZoom;

        setOffset((currentOffset) => {
          // 当前图片中心点屏幕坐标
          const currentImgCenterX = centerX + currentOffset.x;
          const currentImgCenterY = centerY + currentOffset.y;
          // 保持光标下的图像点位置不变
          const nextX = currentOffset.x + (mouseX - currentImgCenterX) * (1 - ratio);
          const nextY = currentOffset.y + (mouseY - currentImgCenterY) * (1 - ratio);
          return { x: nextX, y: nextY };
        });

        return nextZoom;
      });
    };

    stage.addEventListener("wheel", handleWheel, { passive: false });
    return () => stage.removeEventListener("wheel", handleWheel);
  }, [current]);

  // 键盘快捷键：Esc 关闭 / ←→ 切换 / +/-/0 缩放
  useEffect(() => {
    if (!current) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        close();
      } else if (multi && e.key === "ArrowLeft") {
        setIndex(index - 1);
      } else if (multi && e.key === "ArrowRight") {
        setIndex(index + 1);
      } else if (e.key === "+" || e.key === "=") {
        e.preventDefault();
        zoomIn();
      } else if (e.key === "-" || e.key === "_") {
        e.preventDefault();
        zoomOut();
      } else if (e.key === "0") {
        e.preventDefault();
        resetZoom();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [current, multi, index, close, setIndex, zoomIn, zoomOut, resetZoom]);

  if (!current) return null;

  const download = () => {
    const a = document.createElement("a");
    a.href = current.url;
    a.download = current.name || "image";
    document.body.appendChild(a);
    a.click();
    a.remove();
  };

  // 鼠标拖拽平移（按住左键自由平移，不限缩放比例）
  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    // 仅响应鼠标主键（左键）
    if (e.button !== 0) return;
    const st = stageRef.current;
    if (!st) return;
    draggedRef.current = false;
    dragRef.current = {
      startX: e.clientX,
      startY: e.clientY,
      originX: offset.x,
      originY: offset.y,
    };
    setDragging(true);
    setAnimating(false);
    try { st.setPointerCapture(e.pointerId); } catch { /* ignore */ }
  };

  const onPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const d = dragRef.current;
    if (!d) return;
    const dx = e.clientX - d.startX;
    const dy = e.clientY - d.startY;
    if (Math.hypot(dx, dy) > 4) {
      draggedRef.current = true;
    }
    setOffset({
      x: d.originX + dx,
      y: d.originY + dy,
    });
  };

  const onPointerUp = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!dragRef.current) return;
    dragRef.current = null;
    setDragging(false);
    try { stageRef.current?.releasePointerCapture(e.pointerId); } catch { /* ignore */ }
  };

  // 点击背景遮罩关闭（发生过拖拽则拦截本次 click）
  const onStageClick = (e: React.MouseEvent<HTMLDivElement>) => {
    if (draggedRef.current) {
      draggedRef.current = false;
      return;
    }
    // 点击舞台空白区域关闭预览
    if (e.target === stageRef.current) {
      close();
    }
  };

  const scale = zoom / 100;
  const isShifted = zoom !== 100 || offset.x !== 0 || offset.y !== 0;

  return createPortal(
    <div className="gallery-overlay" onClick={close}>
      {/* 左上角显示文件名与序号 */}
      <div className="gallery-caption" onClick={(e) => e.stopPropagation()}>
        <span className="gallery-caption-name">{current.name}</span>
        {images.length > 1 && <span className="gallery-caption-idx">{index + 1} / {images.length}</span>}
      </div>

      {/* 右上角工具栏：下载原图与关闭 */}
      <div className="gallery-topbar" onClick={(e) => e.stopPropagation()}>
        <Tooltip content="下载原图">
          <button className="gallery-btn" onClick={download} aria-label="下载原图" type="button">
            <IconDownload size={16} />
          </button>
        </Tooltip>
        <Tooltip content="关闭 (Esc)">
          <button ref={closeBtnRef} className="gallery-btn" onClick={close} aria-label="关闭预览 (Esc)" type="button">
            <IconX size={16} />
          </button>
        </Tooltip>
      </div>

      {/* 舞台区：支持滚轮无极缩放、双击缩放/复位、拖拽平移、点击空白关闭 */}
      <div
        ref={stageRef}
        className={`gallery-stage${isShifted ? " shifted" : ""}${dragging ? " dragging" : ""}`}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        onClick={onStageClick}
      >
        {/* 变换容器层：承担 scale 缩放与 translate3d 平移，不触发布局回流（Layout-free）且与内部入场动画解耦 */}
        <div
          className={`gallery-transform-layer${animating ? " animating" : ""}`}
          style={{
            transform: `translate3d(${offset.x}px, ${offset.y}px, 0px) scale(${scale})`,
          }}
          onClick={(e) => {
            // 点击图片内容区不冒泡到舞台触发背景关闭
            if (draggedRef.current) {
              draggedRef.current = false;
              e.stopPropagation();
              return;
            }
            e.stopPropagation();
          }}
          onDoubleClick={onDoubleClick}
        >
          <img
            key={current.url}
            className="gallery-img"
            src={current.url}
            alt={current.name}
            style={
              baseSize
                ? {
                    width: `${baseSize.w}px`,
                    height: `${baseSize.h}px`,
                    maxWidth: "none",
                    maxHeight: "none",
                  }
                : { maxWidth: "92vw", maxHeight: "84vh" }
            }
            draggable={false}
            onLoad={(e) => {
              setNatural({ w: e.currentTarget.naturalWidth, h: e.currentTarget.naturalHeight });
            }}
          />
        </div>
      </div>

      {/* 左右翻页导航按钮 */}
      {multi && (
        <Tooltip content="上一张 (←)">
          <button
            className="gallery-nav prev"
            onClick={(e) => { e.stopPropagation(); setIndex(index - 1); }}
            aria-label="上一张"
            type="button"
          >
            <IconChevronLeft size={18} />
          </button>
        </Tooltip>
      )}
      {multi && (
        <Tooltip content="下一张 (→)">
          <button
            className="gallery-nav next"
            onClick={(e) => { e.stopPropagation(); setIndex(index + 1); }}
            aria-label="下一张"
            type="button"
          >
            <IconChevronRight size={18} />
          </button>
        </Tooltip>
      )}

      {/* 底部缩放控制条：[-] 实时百分比 [+] */}
      <div className="gallery-zoom-bar" onClick={(e) => e.stopPropagation()}>
        <Tooltip content="缩小 (-)">
          <button
            className="gallery-zoom-btn"
            onClick={zoomOut}
            disabled={zoom <= ZOOM_MIN}
            aria-label="缩小"
            type="button"
          >
            <IconMinus size={14} />
          </button>
        </Tooltip>
        <Tooltip content="恢复 100%（快捷键 0）">
          <button
            className="gallery-zoom-pct"
            onClick={resetZoom}
            aria-label="恢复 100%"
            type="button"
          >
            {Math.round(zoom)}%
          </button>
        </Tooltip>
        <Tooltip content="放大 (+)">
          <button
            className="gallery-zoom-btn"
            onClick={zoomIn}
            disabled={zoom >= ZOOM_MAX}
            aria-label="放大"
            type="button"
          >
            <IconPlus size={14} />
          </button>
        </Tooltip>
      </div>

    </div>,
    document.body,
  );
}

