/** 乐园玩法接口（plan-playground-v2）：Stage 负责渲染、相机、追随移动与粒子，玩法只关心「这个世界怎么玩」。 */
import type { Object3D, PerspectiveCamera, Texture } from "three";
import type { Dada3D } from "../dada3d";
import type { Confetti, Sparks } from "../fx";
import type { Ground } from "../ground";

export interface Palette {
  skyTop: string;
  skyBottom: string;
  ground: string;
  groundLine: string;
  prop: string;
  cloud: string;
  accent: string;
  accent2: string;
}

export type Tone = "good" | "great" | "bad" | "info";

/** 角色头顶的量表（烤制火候、投掷力度） */
export interface MeterSpec { v: number; band: [number, number]; kind: "roast" | "power"; label?: string }

export interface GameDeps {
  world: string;
  pal: Palette;
  dark: boolean;
  reduce: boolean;
  glowTex: Texture;
  ground: Ground;
}

export interface GameCtx {
  readonly dada: Dada3D;
  /** 角色地面位置（可读写：玩法可直接推动角色，如被浪推开） */
  readonly pos: { x: number; z: number };
  readonly vel: { x: number; z: number };
  /** 指针在地面上的落点（has=false 表示指针不在舞台上） */
  readonly pointer: { x: number; z: number; has: boolean };
  /** 移动目标点（玩法可改写，例如强制走到篝火旁） */
  readonly target: { x: number; z: number };
  readonly sparks: Sparks | null;
  readonly confetti: Confetti | null;
  readonly camera: PerspectiveCamera;
  readonly reduce: boolean;
  readonly time: number;
  /** 为 true 时角色原地不动（蓄力、烤制中） */
  lockMove: boolean;
  ripple(x: number, z: number): void;
  /** 得分：加到总分，并在 (x,y,z) 处冒出浮字 */
  score(n: number, text: string, at: { x: number; y: number; z: number }, tone?: Tone): void;
  /** 不计分的浮字 */
  pop(text: string, at: { x: number; y: number; z: number }, tone?: Tone): void;
  /** 顶部播报（回合、连击等较大的消息） */
  say(text: string, tone?: Tone): void;
  meter(spec: MeterSpec | null): void;
}

export interface Game {
  readonly root: Object3D;
  /** 相机：俯角（度）、注视高度、需要框住的半宽 / 半深 */
  readonly view?: { elev?: number; lookY?: number; lookZ?: number; spanX?: number; spanZ?: number };
  readonly bounds: { x: number; zMin: number; zMax: number };
  /** 移动手感：最大速度、加速度系数 */
  readonly move?: { max?: number; accel?: number };
  /** 环境光点（尘光 / 萤火 / 火星…）；n=0 不要 */
  readonly motes?: { mode: "float" | "rise" | "fall"; n: number; color: string };
  /** 灯光基调 */
  readonly light?: { sun?: string; sunI?: number; hemiSky?: string; hemiGround?: string; hemiI?: number; exposure?: number };
  /** 嗒嗒出生点 */
  readonly spawn?: { x: number; z: number };
  init?(ctx: GameCtx): void;
  update(dt: number, ctx: GameCtx): void;
  /** 指针按下；返回 true 表示已处理（不再走默认「起跳并走过去」） */
  down?(ctx: GameCtx, hit: { x: number; z: number } | null): boolean;
  up?(ctx: GameCtx): void;
  /** 被戳（点到嗒嗒本人）：true 已处理；false 当作普通按下交给 down()；undefined 走默认「戳一下」 */
  poke?(ctx: GameCtx): boolean | undefined;
  /** 涟漪广播（如水面也要起波纹） */
  onRipple?(x: number, z: number): void;
  reset(ctx: GameCtx): void;
  dispose(): void;
}

export type GameFactory = (deps: GameDeps) => Game;
