/** ImageGallery（会话 229）：全局图片查看器——左右切换 / 缩放（− 100% +）/ 下载 / 关闭。
 *
 * 所有图片入口（消息附件、输入框附件、浏览器标注截图、Markdown 图片）共用同一查看器，
 * 打开方式：openGallery(images, index)（store/gallery.ts）。
 * 交互对齐参考图 2：右上角下载 + ×、两侧圆形箭头（多图）、底部中央缩放条、Esc/点遮罩关闭。
 */
import { useEffect, useRef, useState } from "react";
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

const ZOOM_MIN = 25;
const ZOOM_MAX = 400;
const ZOOM_STEP = 25;

export function ImageGallery() {
  const images = useGalleryStore((s) => s.images);
  const index = useGalleryStore((s) => s.index);
  const close = useGalleryStore((s) => s.close);
  const setIndex = useGalleryStore((s) => s.setIndex);

  const [zoom, setZoom] = useState(100);
  /** 图片自然尺寸（>100% 时按原始像素 × 缩放比展示，可滚动/拖拽查看） */
  const [natural, setNatural] = useState<{ w: number; h: number } | null>(null);
  const [dragging, setDragging] = useState(false);
  const stageRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<{ x: number; y: number; sl: number; st: number } | null>(null);

  const current = images[index] ?? null;
  const multi = images.length > 1;

  // 切换图片时重置缩放与拖拽状态
  useEffect(() => {
    setZoom(100);
    setNatural(null);
    dragRef.current = null;
    setDragging(false);
  }, [current?.url]);

  // 键盘：Esc 关闭 / ←→ 切换 / +/- 缩放
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
        setZoom((z) => Math.min(ZOOM_MAX, z + ZOOM_STEP));
      } else if (e.key === "-" || e.key === "_") {
        setZoom((z) => Math.max(ZOOM_MIN, z - ZOOM_STEP));
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [current, multi, index, close, setIndex]);

  if (!current) return null;

  const zoomed = zoom > 100;
  const imgStyle: React.CSSProperties =
    zoomed && natural
      ? {
          width: `${Math.round((natural.w * zoom) / 100)}px`,
          height: `${Math.round((natural.h * zoom) / 100)}px`,
          maxWidth: "none",
          maxHeight: "none",
        }
      : { maxWidth: "92vw", maxHeight: "82vh" };

  const download = () => {
    const a = document.createElement("a");
    a.href = current.url;
    a.download = current.name || "image";
    document.body.appendChild(a);
    a.click();
    a.remove();
  };

  // 放大后的拖拽平移（通过 stage 的滚动位置实现）
  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!zoomed) return;
    const st = stageRef.current;
    if (!st) return;
    dragRef.current = { x: e.clientX, y: e.clientY, sl: st.scrollLeft, st: st.scrollTop };
    setDragging(true);
    try { st.setPointerCapture(e.pointerId); } catch { /* ignore */ }
  };
  const onPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const d = dragRef.current;
    const st = stageRef.current;
    if (!d || !st) return;
    st.scrollLeft = d.sl - (e.clientX - d.x);
    st.scrollTop = d.st - (e.clientY - d.y);
  };
  const onPointerUp = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!dragRef.current) return;
    dragRef.current = null;
    setDragging(false);
    try { stageRef.current?.releasePointerCapture(e.pointerId); } catch { /* ignore */ }
  };

  return createPortal(
    <div className="gallery-overlay" onClick={close}>
      <div className="gallery-topbar" onClick={(e) => e.stopPropagation()}>
        <button className="gallery-btn" onClick={download} title="下载原图" type="button">
          <IconDownload size={16} />
        </button>
        <button className="gallery-btn" onClick={close} title="关闭 (Esc)" type="button">
          <IconX size={16} />
        </button>
      </div>

      {/* 点遮罩/按钮区域关闭，图片舞台内点击不关闭（避免拖拽后误关） */}
      <div
        ref={stageRef}
        className={`gallery-stage${zoomed ? " zoomed" : ""}${dragging ? " dragging" : ""}`}
        onClick={(e) => e.stopPropagation()}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
      >
        <img
          key={current.url}
          className="gallery-img"
          src={current.url}
          alt={current.name}
          style={imgStyle}
          draggable={false}
          onLoad={(e) =>
            setNatural({ w: e.currentTarget.naturalWidth, h: e.currentTarget.naturalHeight })
          }
        />
      </div>

      {multi && (
        <button
          className="gallery-nav prev"
          onClick={(e) => { e.stopPropagation(); setIndex(index - 1); }}
          title="上一张 (←)"
          type="button"
        >
          <IconChevronLeft size={18} />
        </button>
      )}
      {multi && (
        <button
          className="gallery-nav next"
          onClick={(e) => { e.stopPropagation(); setIndex(index + 1); }}
          title="下一张 (→)"
          type="button"
        >
          <IconChevronRight size={18} />
        </button>
      )}

      <div className="gallery-zoom-bar" onClick={(e) => e.stopPropagation()}>
        <button
          className="gallery-zoom-btn"
          onClick={() => setZoom((z) => Math.max(ZOOM_MIN, z - ZOOM_STEP))}
          title="缩小"
          type="button"
        >
          <IconMinus size={14} />
        </button>
        <button
          className="gallery-zoom-pct"
          onClick={() => setZoom(100)}
          title="恢复 100%"
          type="button"
        >
          {zoom}%
        </button>
        <button
          className="gallery-zoom-btn"
          onClick={() => setZoom((z) => Math.min(ZOOM_MAX, z + ZOOM_STEP))}
          title="放大"
          type="button"
        >
          <IconPlus size={14} />
        </button>
      </div>

      {multi && (
        <div className="gallery-counter">{index + 1} / {images.length}</div>
      )}
    </div>,
    document.body,
  );
}
