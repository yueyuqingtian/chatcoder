/** 宠物动画常量与帧数检测（plan-73-323 阶段2）。
 *
 * 帧数表来自阶段0实测：petdex 的 9 个状态行**有效帧数并不相同**
 * （实测样本 waving 仅 4 帧、jumping 5 帧、idle/waiting/running/review 6 帧、其余 8 帧），
 * 8 列网格右侧的空白帧若被播放会出现闪烁/闪白——因此播放必须按帧数截断。
 *
 * 除常量表外再做一次「运行时检测」（detectFrameCounts）：不同作者的帧填充方式可能不同，
 * 检测结果与常量取较小值，保证对任意宠物都不会播到空白帧。
 */

/** 状态行顺序（行号即索引，petdex 官方定义；v2 网格多出的 2 行本期不使用） */
export const STATE_ROWS = [
  "idle",
  "running-right",
  "running-left",
  "waving",
  "jumping",
  "failed",
  "waiting",
  "running",
  "review",
] as const;

export type PetState = (typeof STATE_ROWS)[number];

/** 各状态有效帧数上限（阶段0-B 实测值；运行时检测会再取一次较小值） */
export const STATE_FRAMES: Record<PetState, number> = {
  idle: 6,
  "running-right": 8,
  "running-left": 8,
  waving: 4,
  jumping: 5,
  failed: 8,
  waiting: 6,
  running: 6,
  review: 6,
};

/** 每帧时长（ms）——按状态语义定节奏：等待/空闲慢、执行快、失败沉 */
export const FRAME_MS: Record<PetState, number> = {
  idle: 220,
  "running-right": 90,
  "running-left": 90,
  waving: 140,
  jumping: 110,
  failed: 220,
  waiting: 260,
  running: 110,
  review: 180,
};

/** 非循环状态：播完停在末帧（完成庆祝不给循环播放，避免长期抖动） */
export const ONESHOT_STATES: ReadonlySet<PetState> = new Set<PetState>(["jumping"]);

/** 单帧尺寸（petdex 格式固定） */
export const FRAME_W = 192;
export const FRAME_H = 208;

/** 网格列数（固定 8） */
export const GRID_COLS = 8;

/**
 * 运行时检测每行有效帧数（降采样 alpha 采样，一次性、无每帧成本）。
 *
 * 判定口径与阶段0-B 探针一致：把整帧缩到 48×52 采样，非透明像素占比 > 1% 视为有效帧；
 * 同一行从左到右遇到首个空白帧即停止（petdex 帧从左到右填充）。
 */
export function detectFrameCounts(
  img: HTMLImageElement,
  rows: number
): Record<PetState, number> {
  const out: Record<PetState, number> = { ...STATE_FRAMES };
  const SW = 48;
  const SH = 52;
  const canvas = document.createElement("canvas");
  canvas.width = SW;
  canvas.height = SH;
  const g = canvas.getContext("2d", { willReadFrequently: true });
  if (!g) return out;

  const rowCount = Math.min(rows, STATE_ROWS.length);
  for (let r = 0; r < rowCount; r++) {
    const name = STATE_ROWS[r];
    let valid = 0;
    for (let col = 0; col < GRID_COLS; col++) {
      g.clearRect(0, 0, SW, SH);
      g.drawImage(img, col * FRAME_W, r * FRAME_H, FRAME_W, FRAME_H, 0, 0, SW, SH);
      const data = g.getImageData(0, 0, SW, SH).data;
      let opaque = 0;
      for (let i = 3; i < data.length; i += 4) {
        if (data[i] > 12) opaque++;
      }
      if (opaque / (SW * SH) > 0.01) valid++;
      else break;
    }
    if (valid > 0) out[name] = Math.min(out[name], valid);
  }
  return out;
}
