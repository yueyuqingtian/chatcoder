/** 嗒嗒 3D 角色（plan-114-452）。
 * 造型严格对应 v5 白色团子（见 mascot/Dada.tsx）：无描边的白色圆身体 + 受光渐变（由半球光 + 平行光产生）、
 * 头顶两撮短呆毛、一双小脚、黑椭圆大眼带高光、淡腮红、没有嘴。
 * 只做形体与动作，不做场景：由 Stage 驱动 `update()`，角色自己处理待机呼吸、眨眼、跳步、挤压回弹、看向。 */
import {
  CapsuleGeometry, CircleGeometry, Color, Group, Mesh, MeshBasicMaterial, MeshPhysicalMaterial, MeshStandardMaterial,
  SphereGeometry, TorusGeometry,
} from "three";

const BODY_RY = 0.96;
const BODY_RZ = 0.92;

export class Dada3D {
  readonly root = new Group();
  /** 挤压 / 拉伸只作用在 squash 组上：脚底位置不变，形变从地面向上长 */
  private readonly squash = new Group();
  private readonly body = new Mesh();
  private readonly face = new Group();
  private readonly eyes: Mesh[] = [];
  private readonly tufts: Group[] = [];
  private readonly feet: Mesh[] = [];
  private readonly geos: Array<{ dispose(): void }> = [];
  private readonly mats: Array<{ dispose(): void }> = [];

  // 运动状态
  private phase = 0;
  private hop = 0; // 0~1 的跳跃相位（点击 / 收集触发）
  private hopLift = 0;
  private poke = 0; // 被点一下的挤压余量
  private blinkT = 2.5;
  private blinkK = 0;
  private lookX = 0;
  private lookY = 0;
  private lean = 0;
  private yaw = 0;
  private wow = 0;
  private happy = 0;
  private ring: Group | null = null;
  private floatT = 0;
  private t = 0;

  constructor(private readonly reduce: boolean) {
    // 软胶质感：清漆层给高光，sheen 给边缘一圈柔和的冷色绒光（环境反射由 Stage 的 RoomEnvironment 提供）
    const white = this.mat(new MeshPhysicalMaterial({
      color: 0xffffff, roughness: 0.42, metalness: 0, clearcoat: 0.55, clearcoatRoughness: 0.32,
      sheen: 1, sheenColor: new Color(0xdbe4ff), sheenRoughness: 0.45, envMapIntensity: 0.55,
    }));
    const shade = this.mat(new MeshStandardMaterial({ color: 0xd9dfef, roughness: 0.8, metalness: 0 }));
    const ink = this.mat(new MeshPhysicalMaterial({ color: 0x1b1a24, roughness: 0.2, metalness: 0, clearcoat: 1, clearcoatRoughness: 0.08, envMapIntensity: 0.9 }));
    const glint = this.mat(new MeshBasicMaterial({ color: 0xffffff }));
    const blush = this.mat(new MeshBasicMaterial({ color: 0xff8fa8, transparent: true, opacity: 0.42, depthWrite: false }));

    // 身体：椭球，身体中心离地 BODY_RY，脚底刚好贴地
    const sphere = this.geo(new SphereGeometry(1, 56, 40));
    this.body.geometry = sphere;
    this.body.material = white;
    this.body.scale.set(1, BODY_RY, BODY_RZ);
    this.body.position.y = BODY_RY;
    this.squash.add(this.body);

    // 小脚：在身体后面只露一点，读作「站着」
    const footGeo = this.geo(new SphereGeometry(1, 20, 14));
    for (const sx of [-1, 1]) {
      const f = new Mesh(footGeo, shade);
      f.scale.set(0.2, 0.11, 0.26);
      f.position.set(sx * 0.36, 0.1, 0.16);
      this.feet.push(f);
      this.root.add(f);
    }

    // 呆毛：偏左两撮、短而圆（与 DADA_TUFT 一致）
    const tuftGeo = this.geo(new CapsuleGeometry(0.075, 0.2, 6, 12));
    const tuftSpec: Array<[number, number, number, number]> = [[-0.16, 0.34, 0.9, 1], [0.08, -0.5, 0.92, 0.85]];
    for (const [x, rz, y, s] of tuftSpec) {
      const g = new Group();
      const m = new Mesh(tuftGeo, white);
      m.position.y = 0.16 * s;
      m.scale.setScalar(s);
      g.add(m);
      g.position.set(x, BODY_RY * 2 * y - 0.06, 0.04);
      g.rotation.z = rz * 0.6;
      this.tufts.push(g);
      this.squash.add(g);
    }

    // 脸：挂在身体中心的组，整体小幅转动 ⇒ 五官沿球面滑动（看向的来源）
    this.face.position.y = BODY_RY;
    const eyeGeo = this.geo(new SphereGeometry(1, 28, 20));
    const glintGeo = this.geo(new SphereGeometry(1, 12, 8));
    const cheekGeo = this.geo(new CircleGeometry(1, 24));
    for (const sx of [-1, 1]) {
      const arm = new Group();
      arm.rotation.y = sx * 0.25;
      arm.rotation.x = -0.02;
      const eye = new Mesh(eyeGeo, ink);
      eye.scale.set(0.09, 0.135, 0.05);
      eye.position.z = 0.93;
      const hi = new Mesh(glintGeo, glint);
      // 高光画在眼球局部坐标里（眼球被压成椭球，高光随之变形，读作一小块反光）
      hi.scale.set(0.3, 0.2, 0.5);
      hi.position.set(0.35, 0.38, 0.9);
      eye.add(hi);
      const hi2 = new Mesh(glintGeo, glint);
      hi2.scale.set(0.14, 0.1, 0.4);
      hi2.position.set(-0.3, -0.42, 0.9);
      eye.add(hi2);
      arm.add(eye);
      this.eyes.push(eye);
      this.face.add(arm);

      const cheekArm = new Group();
      cheekArm.rotation.y = sx * 0.62;
      cheekArm.rotation.x = 0.1;
      const cheek = new Mesh(cheekGeo, blush);
      cheek.scale.set(0.11, 0.06, 1);
      cheek.position.z = 0.955;
      cheekArm.add(cheek);
      this.face.add(cheekArm);
    }
    this.squash.add(this.face);

    this.root.add(this.squash);
  }

  /** 看向目标（-1~1，相对角色） */
  look(x: number, y: number) {
    this.lookX = Math.max(-1, Math.min(1, x));
    this.lookY = Math.max(-1, Math.min(1, y));
  }

  /** 起跳（点击地面 / 收集到星星） */
  jump() {
    if (this.hop > 0) return;
    this.hop = 0.0001;
  }

  /** 开心：眼睛弯成笑眼一小会儿（收集到星星） */
  cheer() { this.happy = 1; }

  /** 水上模式：套一个泳圈，身体随波轻轻起伏、倾斜 */
  setFloat(on: boolean, color: string, stripe: string) {
    if (!on || this.ring) return;
    const ring = new Group();
    const torus = this.geo(new TorusGeometry(0.92, 0.26, 20, 56));
    const a = this.mat(new MeshPhysicalMaterial({ color, roughness: 0.35, clearcoat: 0.8, clearcoatRoughness: 0.2 }));
    const b = this.mat(new MeshPhysicalMaterial({ color: stripe, roughness: 0.35, clearcoat: 0.8, clearcoatRoughness: 0.2 }));
    const m1 = new Mesh(torus, a);
    m1.rotation.x = Math.PI / 2;
    ring.add(m1);
    // 一圈条纹：4 段短弧
    for (let i = 0; i < 4; i++) {
      const seg = new Mesh(this.geo(new TorusGeometry(0.92, 0.268, 12, 14, Math.PI / 6)), b);
      seg.rotation.x = Math.PI / 2;
      seg.rotation.z = (i * Math.PI) / 2;
      ring.add(seg);
    }
    ring.position.y = 0.2;
    this.ring = ring;
    this.root.add(ring);
  }

  /** 被点一下：挤一下 + 眼睛睁大 */
  prod() {
    this.poke = 1;
    this.wow = 1;
  }

  /** speed：当前水平速度（单位/秒）；dirX：运动方向（-1~1，用于侧倾与转向） */
  update(dt: number, speed: number, dirX: number) {
    this.t += dt;
    const moving = speed > 0.15;
    const r = this.reduce;

    // 跳步相位：移动时按速度推进；落地处（abs(sin)=0）挤压
    if (moving && !r) this.phase += dt * (6 + Math.min(speed, 5) * 1.6);
    else this.phase += (Math.round(this.phase / Math.PI) * Math.PI - this.phase) * Math.min(1, dt * 10);
    const stride = moving && !r ? Math.abs(Math.sin(this.phase)) : 0;

    // 点击起跳：0→1 的抛物线
    if (this.hop > 0) {
      this.hop += dt / (r ? 0.01 : 0.62);
      if (this.hop >= 1) this.hop = 0;
    }
    this.hopLift = this.hop > 0 ? Math.sin(this.hop * Math.PI) * 1.15 : 0;

    const lift = stride * 0.2 + this.hopLift;
    this.root.position.y = lift;

    // 挤压回弹：落地（lift 小）压扁，空中拉长；poke 额外压扁
    const air = Math.min(1, lift / 0.9);
    const landing = (this.hop > 0.85 ? (this.hop - 0.85) / 0.15 : 0) + (moving && stride < 0.12 ? (0.12 - stride) * 2.2 : 0);
    this.poke = Math.max(0, this.poke - dt * 3.2);
    const breath = r ? 0 : Math.sin(this.t * 2.1) * 0.012;
    const sy = 1 + air * 0.1 - Math.min(0.14, landing * 0.14) - this.poke * 0.16 * Math.sin(this.poke * Math.PI * 2.5 + 1) + breath;
    const sxz = 1 / Math.sqrt(Math.max(0.6, sy));
    this.squash.scale.set(sxz, sy, sxz);

    // 侧倾 + 转向：朝运动方向倾一点，脸朝向略转
    const targetLean = moving ? -dirX * 0.16 : 0;
    this.lean += (targetLean - this.lean) * Math.min(1, dt * 8);
    this.squash.rotation.z = this.lean;
    const targetYaw = moving ? dirX * 0.45 : 0;
    this.yaw += (targetYaw - this.yaw) * Math.min(1, dt * 6);
    this.root.rotation.y = this.yaw;

    // 看向：脸绕身体中心转动
    this.face.rotation.y += (this.lookX * 0.32 - this.face.rotation.y) * Math.min(1, dt * 9);
    this.face.rotation.x += (-this.lookY * 0.22 - this.face.rotation.x) * Math.min(1, dt * 9);

    // 小脚交替抬起
    const fl = moving && !r ? Math.max(0, Math.sin(this.phase)) * 0.08 : 0;
    this.feet[0].position.y = 0.1 + fl;
    this.feet[1].position.y = 0.1 + (moving && !r ? Math.max(0, -Math.sin(this.phase)) * 0.08 : 0);

    // 呆毛：被运动「甩」得向后摆
    const sway = (moving && !r ? -dirX * 0.35 : 0) + (r ? 0 : Math.sin(this.t * 1.7) * 0.05);
    this.tufts.forEach((g, i) => { g.rotation.z += ((i === 0 ? 0.54 : -0.3) + sway * (i === 0 ? 1 : 1.2) - g.rotation.z) * Math.min(1, dt * 10); });

    // 水上起伏
    if (this.ring) {
      this.floatT += dt;
      const bob = r ? 0 : Math.sin(this.floatT * 1.9) * 0.05;
      this.root.position.y += bob;
      this.ring.rotation.z = r ? 0 : Math.sin(this.floatT * 1.3) * 0.04;
    }

    // 眨眼：随机间隔，约 120ms 一次；reduce 下关闭
    this.wow = Math.max(0, this.wow - dt * 1.6);
    if (!r) {
      this.blinkT -= dt;
      if (this.blinkT <= 0) { this.blinkK = 1; this.blinkT = 2.6 + Math.random() * 3.4; }
    }
    this.blinkK = Math.max(0, this.blinkK - dt * 8);
    const closed = this.blinkK > 0 ? Math.sin(this.blinkK * Math.PI) : 0;
    this.happy = Math.max(0, this.happy - dt * 1.4);
    const smile = this.happy > 0 ? Math.min(1, this.happy * 3) : 0;
    this.eyes.forEach((e, i) => {
      const grow = 1 + this.wow * 0.22;
      // 笑眼：压扁 + 外侧上翘（左右眼对称）
      e.scale.set(0.09 * grow * (1 + smile * 0.35), 0.135 * grow * (1 - closed * 0.9) * (1 - smile * 0.68), 0.05);
      e.rotation.z = (i === 0 ? 1 : -1) * smile * 0.35;
    });
  }

  dispose() {
    this.geos.forEach((g) => g.dispose());
    this.mats.forEach((m) => m.dispose());
  }

  private geo<T extends { dispose(): void }>(g: T): T { this.geos.push(g); return g; }
  private mat<T extends { dispose(): void }>(m: T): T { this.mats.push(m); return m; }
}
