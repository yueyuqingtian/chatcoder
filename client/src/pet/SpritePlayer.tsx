/** 精灵播放器（plan-73-342）。
 *
 * canvas 逐帧绘制 petdex 精灵图；帧数按「常量表 ∩ 运行时检测」截断
 * （各状态有效帧数不等，播到右侧空白帧会出现闪白）。
 *
 * plan-73-342 变更：**删除命中网格**。
 * 旧实现按 4px 网格采样 alpha，只让"非透明像素"可交互，结果精灵图边缘、动画换了姿态的
 * 肢体落到网格间隙就被判为"透明"而穿透 —— 用户点宠物某些位置拖不动正是此因。
 * 现在命中共用 PetApp 的矩形判定（宠物矩形 + 容差），任何位置都能抓到。
 */
import { useEffect, useRef, useState } from "react";
import type { PetInfo } from "./petApi";
import {
  FRAME_H,
  FRAME_MS,
  FRAME_W,
  GRID_COLS,
  ONESHOT_STATES,
  STATE_ROWS,
  detectFrameCounts,
  type PetState,
} from "./petAnim";

/** 宠物在屏幕上的基准显示尺寸（scale = 1 时；比例与单帧 192×208 一致） */
export const PET_DISPLAY_BASE = { w: 96, h: 104 } as const;

interface Props {
  pet: PetInfo | null;
  state: PetState;
  scale: number;
  motionLevel: "full" | "reduced" | "off";
}

export function SpritePlayer({ pet, state, scale, motionLevel }: Props) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const imgRef = useRef<HTMLImageElement | null>(null);
  const framesRef = useRef<Record<PetState, number> | null>(null);
  const [ready, setReady] = useState(false);

  const width = Math.round(PET_DISPLAY_BASE.w * scale);
  const height = Math.round(PET_DISPLAY_BASE.h * scale);

  // 加载精灵图 → 逐行检测有效帧数
  useEffect(() => {
    setReady(false);
    imgRef.current = null;
    framesRef.current = null;
    const url = pet?.spriteDataUrl;
    if (!url) return;
    let cancelled = false;
    const img = new Image();
    img.onload = () => {
      if (cancelled) return;
      const finalize = () => {
        if (cancelled) return;
        imgRef.current = img;
        framesRef.current = detectFrameCounts(img, pet?.rows ?? STATE_ROWS.length);
        setReady(true);
      };
      // decode() 保证像素可读（部分环境 onload 时位图尚未解码完成）
      if (typeof img.decode === "function") {
        img.decode().then(finalize).catch(finalize);
      } else {
        finalize();
      }
    };
    img.onerror = () => {
      if (!cancelled) setReady(false);
    };
    img.src = url;
    return () => {
      cancelled = true;
    };
  }, [pet?.spriteDataUrl, pet?.rows]);

  // 逐帧绘制
  useEffect(() => {
    const canvas = canvasRef.current;
    const img = imgRef.current;
    if (!ready || !canvas || !img) return;
    const g = canvas.getContext("2d");
    if (!g) return;

    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.max(1, Math.round(width * dpr));
    canvas.height = Math.max(1, Math.round(height * dpr));
    g.setTransform(dpr, 0, 0, dpr, 0, 0);

    const row = Math.max(0, STATE_ROWS.indexOf(state));
    const counts = framesRef.current;
    const count = Math.max(1, Math.min(GRID_COLS, counts ? counts[state] : GRID_COLS));
    // reduced：整体放慢节奏（保留动感但降低视觉干扰）；off：静态首帧
    const frameMs = FRAME_MS[state] * (motionLevel === "reduced" ? 1.6 : 1);
    const oneshot = ONESHOT_STATES.has(state);
    let frame = 0;
    let last = performance.now();
    let raf = 0;

    const draw = () => {
      g.clearRect(0, 0, width, height);
      g.drawImage(img, frame * FRAME_W, row * FRAME_H, FRAME_W, FRAME_H, 0, 0, width, height);
    };

    draw();
    if (motionLevel === "off") return;

    const loop = (t: number) => {
      raf = window.requestAnimationFrame(loop);
      if (document.hidden) return; // 窗口不可见/锁屏：停画不推进（省电）
      if (t - last >= frameMs) {
        last = t;
        frame = oneshot ? Math.min(frame + 1, count - 1) : (frame + 1) % count;
        draw();
      }
    };
    raf = window.requestAnimationFrame(loop);
    return () => window.cancelAnimationFrame(raf);
  }, [ready, state, width, height, motionLevel]);

  return (
    <canvas
      ref={canvasRef}
      className="pet-sprite"
      style={{ width: `${width}px`, height: `${height}px` }}
      aria-hidden="true"
    />
  );
}
