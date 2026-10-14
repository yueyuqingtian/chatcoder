/** 嗒嗒 3D 角色（plan-playground-v2 精修）。
 * 造型严格对应 v5 白色团子（mascot/Dada.tsx）：上窄下宽的白色团身（按剖面车削）、头顶两撮渐细呆毛、
 * 一双小脚、黑椭圆大眼带两点高光、柔边腮红、没有嘴。
 * 表情：睁眼 / 眨眼 / 笑眼（^ ^）/ 惊讶（大眼）/ 晕（> <），由玩法通过 emote() 驱动。
 * 小手默认不出现，只有拿道具（棍子、风筝线、石片）时才伸出来——不改变用户选定的默认造型。
 * 只做形体与动作：由 Stage 每帧驱动 update()。 */
import {
  CanvasTexture, CapsuleGeometry, CircleGeometry, Color, Group, LatheGeometry, Mesh, MeshBasicMaterial,
  MeshPhysicalMaterial, MeshStandardMaterial, SphereGeometry, TorusGeometry, Vector2, Vector3,
} from "three";
import { Bag, radialTex } from "./kit";

/** 身体总高（脚底到头顶）与正面半径（z 方向压扁） */
export const DADA_H = 1.94;
const BODY_CY = 0.94;
const BODY_SZ = 0.9;
/** 眼睛半宽 / 半高：与 v5 SVG 同比例（眼高约为身高的 0.23） */
const EYE_W = 0.135, EYE_H = 0.2;

/** 呆毛静止时的倾角：左撮微左倾，右撮明显右倒（对应 DADA_TUFT） */
const TUFT_BASE = [0.38, -0.95];

export type Emote = "open" | "happy" | "wow" | "dizzy";
export type HandPose = "none" | "hold" | "reach" | "up" | "wind" | "throw" | "string";

const HAND: Record<Exclude<HandPose, "none">, [[number, number, number], [number, number, number]]> = {
  hold: [[-0.62, 0.72, 0.72], [0.66, 0.78, 0.72]],
  reach: [[-0.56, 0.86, 0.84], [0.5, 1.02, 1.0]],
  up: [[-0.84, 1.62, 0.3], [0.84, 1.62, 0.3]],
  wind: [[-0.66, 0.74, 0.7], [0.96, 1.32, -0.3]],
  throw: [[-0.7, 0.8, 0.5], [0.62, 1.08, 1.02]],
  string: [[-0.64, 0.82, 0.74], [0.6, 1.16, 0.82]],
};

/** v5 剖面：下半更饱满、上半略收，脚底微平 */
function bodyProfile(): Vector2[] {
  const pts: Vector2[] = [];
  const N = 44;
  for (let i = 0; i <= N; i++) {
    const th = -Math.PI / 2 + (i / N) * Math.PI;
    const s = Math.sin(th), c = Math.cos(th);
    const lower = s < 0;
    const r = lower ? Math.pow(c, 0.82) * 1.0 : c * (1 - 0.1 * s);
    const y = BODY_CY + (lower ? -Math.pow(-s, 1.1) * 0.92 : s * 1.0);
    pts.push(new Vector2(Math.max(0.0001, r), y));
  }
  return pts;
}

/** 呆毛：渐细、向外弯的小芽（胶囊变形） */
function tuftGeometry(bag: Bag) {
  const g = bag.geo(new CapsuleGeometry(0.105, 0.12, 10, 16));
  const p = g.getAttribute("position");
  const v = new Vector3();
  for (let i = 0; i < p.count; i++) {
    v.fromBufferAttribute(p, i);
    const t = (v.y + 0.165) / 0.33; // 0 底 ~ 1 顶
    // 叶芽：中段最胖、顶端收成圆尖，整体向外弯
    const taper = 0.78 + Math.sin(Math.min(1, Math.max(0, t)) * Math.PI * 0.8) * 0.3 - Math.max(0, t - 0.75) * 0.9;
    v.x *= taper; v.z *= taper * 0.85;
    v.x += t * t * 0.09;
    p.setXYZ(i, v.x, v.y + 0.15, v.z);
  }
  g.computeVertexNormals();
  return g;
}

export class Dada3D {
  readonly root = new Group();
  /** 挤压 / 拉伸作用在 squash 组上：脚底不动，形变从地面向上长 */
  readonly squash = new Group();
  /** 头顶挂点（樱时叠花瓣、得分小皇冠等） */
  readonly crown = new Group();
  /** 右手挂点：道具挂在这里随手移动 */
  readonly handR = new Group();
  readonly handL = new Group();
  private readonly bag = new Bag();
  private readonly face = new Group();
  private readonly eyes: Mesh[] = [];
  private readonly happyEyes: Mesh[] = [];
  private readonly dizzyEyes: Group[] = [];
  private readonly cheeks: Mesh[] = [];
  private readonly tufts: Group[] = [];
  private readonly feet: Mesh[] = [];
  private readonly body: Mesh;

  // 运动状态
  private phase = 0;
  private hop = 0;
  private hopDur = 0.62;
  private hopH = 1.15;
  private hopLift = 0;
  private poke = 0;
  private blinkT = 2.5;
  private blinkK = 0;
  private lookX = 0;
  private lookY = 0;
  private lean = 0;
  private yaw = 0;
  private faceYaw: number | null = null;
  private emoteK: Emote = "open";
  private emoteT = 0;
  private handPose: HandPose = "none";
  private handVis = 0;
  private ring: Group | null = null;
  private floatY = 0;
  private idleT = 0;
  private idleLook = { x: 0, y: 0, t: 0 };
  private t = 0;
  private wobble = 0;
  private stretch = 0;

  constructor(private readonly reduce: boolean) {
    const b = this.bag;
    // 软胶质感：清漆给高光，sheen 给边缘一圈冷色绒光（环境反射由 Stage 的 RoomEnvironment 提供）
    const white = b.mat(new MeshPhysicalMaterial({
      color: 0xffffff, roughness: 0.46, metalness: 0, clearcoat: 0.6, clearcoatRoughness: 0.3,
      sheen: 1, sheenColor: new Color(0xdfe7ff), sheenRoughness: 0.5, envMapIntensity: 0.6,
    }));
    const shade = b.mat(new MeshStandardMaterial({ color: 0xdde3f1, roughness: 0.7, metalness: 0 }));
    const ink = b.mat(new MeshPhysicalMaterial({ color: 0x17161f, roughness: 0.16, metalness: 0, clearcoat: 1, clearcoatRoughness: 0.06, envMapIntensity: 1.1 }));
    const glint = b.mat(new MeshBasicMaterial({ color: 0xffffff }));
    const blushTex: CanvasTexture = b.tex(radialTex([[0, "rgba(255,143,168,0.85)"], [0.5, "rgba(255,143,168,0.4)"], [1, "rgba(255,143,168,0)"]], 64));
    const blush = b.mat(new MeshBasicMaterial({ map: blushTex, transparent: true, depthWrite: false, opacity: 0.9 }));

    // 身体：v5 剖面车削
    this.body = new Mesh(b.geo(new LatheGeometry(bodyProfile(), 72)), white);
    this.body.scale.z = BODY_SZ;
    this.body.castShadow = true;
    this.squash.add(this.body);

    // 小脚：在身体前下方露出一点，读作「站着」
    const footGeo = b.geo(new SphereGeometry(1, 24, 16));
    for (const sx of [-1, 1]) {
      const f = new Mesh(footGeo, shade);
      f.scale.set(0.22, 0.12, 0.28);
      f.position.set(sx * 0.36, 0.1, 0.22);
      f.castShadow = true;
      this.feet.push(f);
      this.root.add(f);
    }

    // 呆毛：偏左两撮，渐细向外弯（与 DADA_TUFT 一致）
    const tg = tuftGeometry(b);
    const spec: Array<[number, number, number, number]> = [[-0.13, TUFT_BASE[0], DADA_H - 0.1, 1], [0.1, TUFT_BASE[1], DADA_H - 0.09, 0.9]];
    for (const [x, rz, y, s] of spec) {
      const g = new Group();
      const m = new Mesh(tg, white);
      m.scale.setScalar(s);
      if (rz < 0) m.scale.x *= -1;
      m.castShadow = true;
      g.add(m);
      g.position.set(x, y, 0.02);
      g.rotation.z = rz;
      this.tufts.push(g);
      this.squash.add(g);
    }

    // 脸：挂在身体中心的组，整体小幅转动 ⇒ 五官沿球面滑动（看向）
    this.face.position.y = BODY_CY + 0.06;
    const eyeGeo = b.geo(new SphereGeometry(1, 32, 22));
    const glintGeo = b.geo(new SphereGeometry(1, 12, 8));
    const arcGeo = b.geo(new TorusGeometry(0.1, 0.03, 10, 28, Math.PI));
    const barGeo = b.geo(new CapsuleGeometry(0.028, 0.13, 4, 8));
    const cheekGeo = b.geo(new CircleGeometry(1, 28));
    for (const sx of [-1, 1]) {
      const arm = new Group();
      arm.rotation.y = sx * 0.29;
      const eye = new Mesh(eyeGeo, ink);
      eye.scale.set(EYE_W, EYE_H, 0.05);
      eye.position.z = 0.89;
      const hi = new Mesh(glintGeo, glint);
      hi.scale.set(0.32, 0.22, 0.5);
      hi.position.set(0.32, 0.4, 0.88);
      eye.add(hi);
      const hi2 = new Mesh(glintGeo, glint);
      hi2.scale.set(0.15, 0.1, 0.4);
      hi2.position.set(-0.28, -0.44, 0.9);
      eye.add(hi2);
      arm.add(eye);
      this.eyes.push(eye);

      // 笑眼：上弯的弧
      const hp = new Mesh(arcGeo, ink);
      hp.position.set(0, -0.03, 0.9);
      hp.visible = false;
      arm.add(hp);
      this.happyEyes.push(hp);

      // 晕：> <
      const dz = new Group();
      for (const k of [-1, 1]) {
        const bar = new Mesh(barGeo, ink);
        bar.rotation.z = Math.PI / 2 + k * 0.55 * sx;
        bar.position.set(sx * 0.04, k * 0.045, 0);
        dz.add(bar);
      }
      dz.position.z = 0.9;
      dz.visible = false;
      arm.add(dz);
      this.dizzyEyes.push(dz);
      this.face.add(arm);

      const cheekArm = new Group();
      cheekArm.rotation.y = sx * 0.58;
      cheekArm.rotation.x = 0.24;
      const cheek = new Mesh(cheekGeo, blush);
      cheek.scale.set(0.15, 0.09, 1);
      cheek.position.z = 0.975;
      cheekArm.add(cheek);
      this.cheeks.push(cheek);
      this.face.add(cheekArm);
    }
    this.squash.add(this.face);

    // 小手：默认收起
    const handGeo = b.geo(new SphereGeometry(1, 20, 14));
    for (const h of [this.handL, this.handR]) {
      const m = new Mesh(handGeo, white);
      m.scale.set(0.15, 0.13, 0.14);
      m.castShadow = true;
      h.add(m);
      h.scale.setScalar(0.001);
      h.position.set(0, 0.8, 0.6);
      this.squash.add(h);
    }

    this.crown.position.y = DADA_H - 0.06;
    this.squash.add(this.crown);
    this.root.add(this.squash);
  }

  /** 看向（-1~1，相对角色） */
  look(x: number, y: number) {
    this.lookX = Math.max(-1, Math.min(1, x));
    this.lookY = Math.max(-1, Math.min(1, y));
    this.idleT = 0;
  }

  /** 看向世界坐标中的一点 */
  lookAt(x: number, y: number, z: number) {
    const p = this.root.position;
    const dx = x - p.x, dz = z - p.z, dy = y - (p.y + BODY_CY);
    const yaw = this.root.rotation.y;
    // 转到角色局部（面朝 +z）
    const lx = dx * Math.cos(yaw) - dz * Math.sin(yaw);
    const lz = dx * Math.sin(yaw) + dz * Math.cos(yaw);
    this.look(lx / Math.max(1.2, Math.abs(lz) + 1.2), dy / (Math.hypot(dx, dz) + 1.5) - (lz < 0 ? 0.3 : 0));
  }

  /** 起跳 */
  jump(height = 1.15, dur = 0.62) {
    if (this.hop > 0) return false;
    this.hopH = height; this.hopDur = dur;
    this.hop = 0.0001;
    return true;
  }
  get airborne() { return this.hop > 0; }

  /** 表情，持续 dur 秒后回到睁眼 */
  emote(k: Emote, dur = 1.2) { this.emoteK = k; this.emoteT = dur; }
  cheer() { this.emote("happy", 1.1); }
  /** 被点一下：挤一下 + 眼睛睁大 */
  prod() { this.poke = 1; this.emote("wow", 0.7); }
  /** 甩一下（樱时抖落花瓣、失败时摇头） */
  shake() { this.wobble = 1; }
  /** 伸展（风筝借风、伸懒腰） */
  stretchUp() { this.stretch = 1; }

  /** 手势；none 收起小手 */
  pose(p: HandPose) { this.handPose = p; }
  /** 固定朝向（弧度，0 = 面向镜头；null 交还给运动方向） */
  faceTo(yaw: number | null) { this.faceYaw = yaw; }

  /** 水上模式：套一个泳圈，随水面起伏 */
  setFloat(on: boolean, color: string, stripe: string) {
    if (!on || this.ring) return;
    const b = this.bag;
    const ring = new Group();
    const a = b.mat(new MeshPhysicalMaterial({ color, roughness: 0.32, clearcoat: 0.9, clearcoatRoughness: 0.15 }));
    const s = b.mat(new MeshPhysicalMaterial({ color: stripe, roughness: 0.32, clearcoat: 0.9, clearcoatRoughness: 0.15 }));
    const m1 = new Mesh(b.geo(new TorusGeometry(0.98, 0.28, 24, 64)), a);
    m1.rotation.x = Math.PI / 2;
    m1.castShadow = true;
    ring.add(m1);
    const segGeo = b.geo(new TorusGeometry(0.98, 0.286, 16, 16, Math.PI / 6));
    for (let i = 0; i < 4; i++) {
      const seg = new Mesh(segGeo, s);
      seg.rotation.x = Math.PI / 2;
      seg.rotation.z = (i * Math.PI) / 2 + 0.3;
      ring.add(seg);
    }
    ring.position.y = 0.32;
    this.ring = ring;
    this.root.add(ring);
  }
  /** 水面高度（潮汐世界由玩法每帧给出） */
  setFloatY(y: number) { this.floatY = y; }

  /** speed：水平速度（单位/秒）；dirX：运动方向 x 分量（-1~1）；heading：运动朝向（弧度） */
  update(dt: number, speed: number, dirX: number, heading: number) {
    this.t += dt;
    const moving = speed > 0.15;
    const r = this.reduce;

    // 跳步相位：移动时按速度推进；落地处挤压
    if (moving && !r) this.phase += dt * (6 + Math.min(speed, 5) * 1.6);
    else this.phase += (Math.round(this.phase / Math.PI) * Math.PI - this.phase) * Math.min(1, dt * 10);
    const stride = moving && !r && !this.ring ? Math.abs(Math.sin(this.phase)) : 0;

    if (this.hop > 0) {
      this.hop += dt / this.hopDur;
      if (this.hop >= 1) { this.hop = 0; this.poke = Math.max(this.poke, 0.5); }
    }
    this.hopLift = this.hop > 0 ? Math.sin(this.hop * Math.PI) * this.hopH : 0;

    const lift = stride * 0.2 + this.hopLift;
    this.root.position.y = lift + (this.ring ? this.floatY - 0.2 : 0);

    // 挤压回弹
    const air = Math.min(1, lift / 0.9);
    const landing = (this.hop > 0.86 ? (this.hop - 0.86) / 0.14 : 0) + (moving && stride < 0.12 ? (0.12 - stride) * 2.2 : 0);
    this.poke = Math.max(0, this.poke - dt * 3);
    this.stretch = Math.max(0, this.stretch - dt * 2.2);
    const breath = r ? 0 : Math.sin(this.t * 2.1) * 0.014;
    const st = this.stretch > 0 ? Math.sin(this.stretch * Math.PI) * 0.12 : 0;
    const sy = 1 + air * 0.1 + st - Math.min(0.14, landing * 0.14) - this.poke * 0.16 * Math.sin(this.poke * Math.PI * 2.5 + 1) + breath;
    const sxz = 1 / Math.sqrt(Math.max(0.6, sy));
    this.squash.scale.set(sxz, sy, sxz);

    // 侧倾 + 摇晃
    this.wobble = Math.max(0, this.wobble - dt * 1.8);
    const wob = this.wobble > 0 ? Math.sin(this.wobble * 26) * 0.2 * this.wobble : 0;
    const targetLean = (moving ? -dirX * 0.14 : 0) + wob;
    this.lean += (targetLean - this.lean) * Math.min(1, dt * 10);
    this.squash.rotation.z = this.lean;

    // 朝向：优先玩法指定，否则偏向运动方向（只转一部分，脸始终大致朝镜头）
    let targetYaw = 0;
    if (this.faceYaw !== null) targetYaw = this.faceYaw;
    else if (moving) targetYaw = Math.max(-1.1, Math.min(1.1, heading * 0.55));
    let d = targetYaw - this.yaw;
    while (d > Math.PI) d -= Math.PI * 2;
    while (d < -Math.PI) d += Math.PI * 2;
    this.yaw += d * Math.min(1, dt * 7);
    this.root.rotation.y = this.yaw;

    // 待机：一会儿不动就东张西望
    this.idleT += dt;
    let lx = this.lookX, ly = this.lookY;
    if (!r && !moving && this.idleT > 4) {
      this.idleLook.t -= dt;
      if (this.idleLook.t <= 0) { this.idleLook = { x: (Math.random() - 0.5) * 1.6, y: (Math.random() - 0.3) * 0.8, t: 1.2 + Math.random() * 1.8 }; if (Math.random() < 0.25) this.stretchUp(); }
      lx = this.idleLook.x; ly = this.idleLook.y;
    }
    const fk = Math.min(1, dt * 9);
    this.face.rotation.y += (lx * 0.34 - this.face.rotation.y) * fk;
    this.face.rotation.x += (-ly * 0.22 - this.face.rotation.x) * fk;

    // 小脚交替抬起
    const fl = moving && !r && !this.ring ? Math.sin(this.phase) * 0.09 : 0;
    this.feet[0].position.y = 0.1 + Math.max(0, fl);
    this.feet[1].position.y = 0.1 + Math.max(0, -fl);
    this.feet.forEach((f) => { f.visible = !this.ring; });

    // 呆毛：被运动甩向后，空中向上飘
    const sway = (moving && !r ? -dirX * 0.32 : 0) + (r ? 0 : Math.sin(this.t * 1.7) * 0.05) + wob * 1.5;
    const airK = this.hop > 0 ? Math.cos(this.hop * Math.PI) * 0.25 : 0;
    this.tufts.forEach((g, i) => {
      const base = TUFT_BASE[i];
      g.rotation.z += (base + sway * (i === 0 ? 1 : 1.2) - airK * (i === 0 ? 1 : -1) - g.rotation.z) * Math.min(1, dt * 10);
    });

    // 小手
    const showHands = this.handPose !== "none";
    this.handVis += ((showHands ? 1 : 0) - this.handVis) * Math.min(1, dt * 10);
    const hv = Math.max(0.001, this.handVis);
    if (showHands) {
      const [L, R] = HAND[this.handPose as Exclude<HandPose, "none">];
      const k = Math.min(1, dt * 14);
      const swing = this.handPose === "up" && !r ? Math.sin(this.t * 9) * 0.12 : 0;
      this.handL.position.x += (L[0] - this.handL.position.x) * k;
      this.handL.position.y += (L[1] + swing - this.handL.position.y) * k;
      this.handL.position.z += (L[2] - this.handL.position.z) * k;
      this.handR.position.x += (R[0] - this.handR.position.x) * k;
      this.handR.position.y += (R[1] - swing - this.handR.position.y) * k;
      this.handR.position.z += (R[2] - this.handR.position.z) * k;
    }
    this.handL.scale.setScalar(hv);
    this.handR.scale.setScalar(hv);

    // 泳圈轻晃
    if (this.ring) this.ring.rotation.z = r ? 0 : Math.sin(this.t * 1.3) * 0.05;

    // 表情
    this.emoteT = Math.max(0, this.emoteT - dt);
    const em: Emote = this.emoteT > 0 ? this.emoteK : "open";
    if (!r) {
      this.blinkT -= dt;
      if (this.blinkT <= 0) { this.blinkK = 1; this.blinkT = 2.6 + Math.random() * 3.4; }
    }
    this.blinkK = Math.max(0, this.blinkK - dt * 8);
    const closed = this.blinkK > 0 ? Math.sin(this.blinkK * Math.PI) : 0;
    const grow = em === "wow" ? 1.18 : 1;
    this.eyes.forEach((e) => {
      e.visible = em === "open" || em === "wow";
      const gx = EYE_W * grow, gy = EYE_H * grow * (1 - closed * 0.92);
      e.scale.x += (gx - e.scale.x) * Math.min(1, dt * 18);
      e.scale.y = em === "open" && closed > 0 ? gy : e.scale.y + (gy - e.scale.y) * Math.min(1, dt * 18);
    });
    this.happyEyes.forEach((h) => { h.visible = em === "happy"; });
    this.dizzyEyes.forEach((h, i) => { h.visible = em === "dizzy"; if (h.visible && !r) h.rotation.z = Math.sin(this.t * 10 + i) * 0.15; });
    const blushK = em === "happy" ? 1.25 : em === "wow" ? 1.1 : 1;
    this.cheeks.forEach((c) => { c.scale.x += (0.15 * blushK - c.scale.x) * Math.min(1, dt * 8); c.scale.y = c.scale.x * 0.6; });
  }

  dispose() { this.bag.dispose(); }
}
