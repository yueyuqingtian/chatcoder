/** 乐园场景（plan-114-452 精修版）：会呼吸的地面 shader、随主题世界变化的布景、可收集的发光星星。
 *
 * 地面是一张自定义 ShaderMaterial 的大平面，按世界切换「表面」：
 *   潮汐 = 水面焦散；森林 = 林间光斑；煦风 = 风吹草浪；篝火 = 余烬与火光；樱时 = 落英；石墨 = 发光网格。
 * 所有世界共用：涟漪（角色移动 / 点击 / 收集时扩散的圆环）、远处淡入天空渐变（无地平线硬边）。
 * 颜色来自舞台上的 --pg-* 变量（Stage 解析为 #rrggbb），布景摆位用世界名做种子的伪随机，同一世界每次一致。 */
import {
  AdditiveBlending, CanvasTexture, Color, CylinderGeometry, Group, Mesh, MeshBasicMaterial, MeshStandardMaterial,
  OctahedronGeometry, PlaneGeometry, PointLight, ShaderMaterial, Sprite, SpriteMaterial, SphereGeometry,
  TorusGeometry, Vector3, type Texture,
} from "three";

export interface Palette {
  skyBottom: string;
  ground: string;
  groundLine: string;
  prop: string;
  cloud: string;
  accent: string;
  accent2: string;
}

/** 玩家可活动范围（地面坐标，x 向右、z 向镜头） */
export const BOUNDS = { x: 6.4, zMin: -3.4, zMax: 3.6 };
export const MAX_RIPPLES = 10;

const MODE: Record<string, number> = { graphite: 0, forest: 1, tide: 2, campfire: 3, sakura: 4, breeze: 5 };

function rng(seed: string) {
  let h = 2166136261;
  for (let i = 0; i < seed.length; i++) { h ^= seed.charCodeAt(i); h = Math.imul(h, 16777619); }
  return () => { h = Math.imul(h ^ (h >>> 15), 2246822507); h = Math.imul(h ^ (h >>> 13), 3266489909); h ^= h >>> 16; return (h >>> 0) / 4294967296; };
}

/** 角色脚下的软阴影贴图：径向渐变，随跳起高度缩小变淡 */
export function makeBlobTexture(): Texture {
  const c = document.createElement("canvas");
  c.width = c.height = 64;
  const g = c.getContext("2d")!;
  const grd = g.createRadialGradient(32, 32, 2, 32, 32, 32);
  grd.addColorStop(0, "rgba(0,0,0,0.9)");
  grd.addColorStop(0.55, "rgba(0,0,0,0.35)");
  grd.addColorStop(1, "rgba(0,0,0,0)");
  g.fillStyle = grd;
  g.fillRect(0, 0, 64, 64);
  return new CanvasTexture(c);
}

const GROUND_VERT = /* glsl */ `
  varying vec3 vW;
  void main() {
    vec4 w = modelMatrix * vec4(position, 1.0);
    vW = w.xyz;
    gl_Position = projectionMatrix * viewMatrix * w;
  }`;

const GROUND_FRAG = /* glsl */ `
  precision highp float;
  varying vec3 vW;
  uniform float uTime; uniform int uMode;
  uniform vec3 uA, uB, uLine, uFog, uAccent, uAccent2;
  uniform vec3 uDada; uniform vec3 uRip[${MAX_RIPPLES}];

  float hash(vec2 p) { return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }
  float vnoise(vec2 p) {
    vec2 i = floor(p), f = fract(p); f = f * f * (3.0 - 2.0 * f);
    return mix(mix(hash(i), hash(i + vec2(1, 0)), f.x), mix(hash(i + vec2(0, 1)), hash(i + vec2(1, 1)), f.x), f.y);
  }
  float fbm(vec2 p) { float a = 0.5, s = 0.0; for (int i = 0; i < 4; i++) { s += a * vnoise(p); p *= 2.03; a *= 0.5; } return s; }

  // 焦散：迭代正弦扭曲（经典水底光网）
  float caustic(vec2 p, float t) {
    vec2 i = p; float c = 1.0; const float inten = 0.0065;
    for (int n = 0; n < 4; n++) {
      float tt = t * (1.0 - (3.5 / float(n + 1)));
      i = p + vec2(cos(tt - i.x) + sin(tt + i.y), sin(tt - i.y) + cos(tt + i.x));
      c += 1.0 / length(vec2(p.x / (sin(i.x + tt) / inten), p.y / (cos(i.y + tt) / inten)));
    }
    c /= 4.0; c = max(1.17 - pow(max(c, 0.0), 1.4), 0.0);
    return min(pow(c, 8.0), 1.5);
  }

  void main() {
    vec2 p = vW.xz;
    float r = length(p);
    float t = uTime;
    // 底色：中心略亮的双色渐变 + 低频噪声
    float n = fbm(p * 0.22 + 3.0);
    vec3 col = mix(uA, uB, clamp(0.35 + n * 0.6 + (p.y + 4.0) * 0.012, 0.0, 1.0));
    float light = 0.0;

    if (uMode == 2) {                 // 潮汐：水面焦散 + 缓慢涌动的明暗
      float c = caustic(p * 0.32, t * 0.45 + 23.0);
      float c2 = caustic(p * 0.22 + 7.0, t * 0.32 + 5.0);
      col = mix(col, uB, 0.35 * fbm(p * 0.35 + t * 0.05));
      light = c * 1.6 + c2 * 0.8;
      col += uLine * light * 0.8;
      col += uAccent * pow(max(0.0, vnoise(p * 0.8 + t * 0.2) - 0.7), 2.0) * 0.4;
    } else if (uMode == 1) {          // 森林：树叶间漏下的光斑 + 苔藓质感
      float d = caustic(p * 0.18 + 11.0, t * 0.18 + 2.0);
      float leaf = smoothstep(0.45, 0.8, fbm(p * 0.9 + t * 0.03));
      col = mix(col, uLine, leaf * 0.28);
      light = d * 0.7;
      col += uAccent2 * light * 0.22;
    } else if (uMode == 5) {          // 煦风：风吹草浪，一道道明暗扫过
      float wave = sin(p.x * 0.7 + t * 0.9 + fbm(p * 0.4) * 5.0) * 0.5 + 0.5;
      col = mix(col, uLine, wave * 0.22 * smoothstep(0.2, 0.8, fbm(p * 0.6)));
      light = pow(wave, 6.0) * 0.5;
      col += uAccent2 * light * 0.12;
    } else if (uMode == 3) {          // 篝火：暖色余烬点 + 火堆附近的橙光
      vec2 cp = p * 3.2; vec2 cc = floor(cp); float hh = hash(cc);
      vec2 ff = fract(cp) - 0.5 + (vec2(hash(cc + 3.1), hash(cc + 7.7)) - 0.5) * 0.5;
      float sp = step(0.94, hh) * smoothstep(0.13, 0.0, length(ff)) * (0.5 + 0.5 * sin(t * 2.0 + hh * 40.0));
      col = mix(col, uLine, 0.2 * fbm(p * 0.5));
      float fd = length(p - vec2(-5.2, -3.3));
      light = exp(-fd * 0.32) * (0.75 + 0.25 * sin(t * 7.0) * sin(t * 3.1));
      col += uAccent * light * 0.5 + uAccent2 * sp * 0.8;
    } else if (uMode == 4) {          // 樱时：铺地的落瓣
      vec2 cell = floor(p * 1.7); vec2 f = fract(p * 1.7) - 0.5;
      float h = hash(cell);
      float petal = step(0.72, h) * smoothstep(0.2, 0.0, length(f * vec2(1.0, 1.7)) - 0.06 * h);
      col = mix(col, uLine, 0.15 * fbm(p * 0.5));
      col = mix(col, uAccent, petal * 0.55);
      light = 0.0;
    } else {                          // 石墨：稿纸网格 + 经过的光
      vec2 g = abs(fract(p * 0.8) - 0.5);
      float line = smoothstep(0.49, 0.5, max(g.x, g.y));
      float scan = 0.5 + 0.5 * sin(t * 0.6 - p.x * 0.35);
      col = mix(col, uLine, line * (0.35 + 0.35 * scan));
      light = line * scan * 0.5;
    }

    // 跟随角色的柔光（地面被「照亮」一圈）
    float dd = length(p - uDada.xz);
    col += uAccent2 * exp(-dd * dd * 0.35) * 0.1;
    if (uMode == 2) col += uLine * exp(-dd * dd * 0.5) * 0.1;

    // 涟漪：角色移动 / 点击 / 收集时扩散的圆环
    float rip = 0.0;
    for (int i = 0; i < ${MAX_RIPPLES}; i++) {
      vec3 R = uRip[i];
      if (R.z < 0.0) continue;
      float rad = R.z * 2.6;
      float d = abs(length(p - R.xy) - rad);
      rip += smoothstep(0.16, 0.0, d) * exp(-R.z * 1.7) * 0.9;
    }
    col += (uMode == 2 ? uLine : uAccent2) * rip * 0.7;

    // 远处融入天空渐变（无地平线硬边），中心外一圈暗角让视线回到场地
    float fog = smoothstep(11.0, 34.0, r);
    col *= 1.0 - 0.1 * smoothstep(5.0, 12.0, r);
    col = mix(col, uFog, fog);
    gl_FragColor = vec4(col, 1.0);
  }`;

export class World3D {
  readonly root = new Group();
  readonly stars: Array<{ group: Group; mesh: Mesh; glow: Sprite; baseY: number; seed: number; alive: boolean; respawn: number }> = [];
  private readonly ground: ShaderMaterial;
  private readonly rip: Vector3[] = Array.from({ length: MAX_RIPPLES }, () => new Vector3(0, 0, -1));
  private ripHead = 0;
  private readonly spin: Mesh[] = [];
  private readonly bobbers: Array<{ o: Group; y: number; s: number }> = [];
  private flame: Group | null = null;
  private fireLight: PointLight | null = null;
  private readonly clouds: Group[] = [];
  private readonly geos: Array<{ dispose(): void }> = [];
  private readonly mats: Array<{ dispose(): void }> = [];
  private readonly rand: () => number;
  private time = 0;

  constructor(
    private readonly world: string,
    private readonly pal: Palette,
    private readonly reduce: boolean,
    glowTex: Texture,
  ) {
    this.rand = rng(world);
    const c = (s: string) => new Color(s);
    const A = c(pal.ground);
    const B = c(pal.ground).lerp(c(pal.prop), 0.45);
    this.ground = this.mat(new ShaderMaterial({
      vertexShader: GROUND_VERT, fragmentShader: GROUND_FRAG,
      uniforms: {
        uTime: { value: 0 }, uMode: { value: MODE[world] ?? 0 },
        uA: { value: A }, uB: { value: B },
        uLine: { value: c(pal.groundLine) }, uFog: { value: c(pal.skyBottom) },
        uAccent: { value: c(pal.accent) }, uAccent2: { value: c(pal.accent2) },
        uDada: { value: new Vector3() }, uRip: { value: this.rip },
      },
    }));
    const plane = new Mesh(this.geo(new PlaneGeometry(90, 90, 1, 1)), this.ground);
    plane.rotation.x = -Math.PI / 2;
    this.root.add(plane);

    this.buildProps();
    this.buildClouds();

    // 收集物：圆润八面体宝石 + 光晕精灵
    const starMat = this.mat(new MeshStandardMaterial({ color: pal.accent2, emissive: pal.accent2, emissiveIntensity: 0.7, roughness: 0.22, metalness: 0.1 }));
    const glowMat = this.mat(new SpriteMaterial({ map: glowTex, color: pal.accent2, blending: AdditiveBlending, transparent: true, depthWrite: false, opacity: 0.9 }));
    const starGeo = this.geo(new OctahedronGeometry(0.22, 1));
    for (let i = 0; i < 4; i++) {
      const group = new Group();
      const mesh = new Mesh(starGeo, starMat);
      mesh.scale.set(1, 1.35, 1);
      const glow = new Sprite(glowMat);
      glow.scale.setScalar(1.3);
      group.add(glow, mesh);
      group.visible = false;
      this.root.add(group);
      this.stars.push({ group, mesh, glow, baseY: 0.62, seed: this.rand() * 6.28, alive: false, respawn: i * 0.35 });
    }

    // 篝火：火堆 + 橙色点光（闪烁）
    if (world === "campfire") {
      const fire = new Group();
      const stoneMat = this.mat(new MeshStandardMaterial({ color: pal.prop, roughness: 0.9 }));
      const sph = this.geo(new SphereGeometry(1, 14, 10));
      for (let k = 0; k < 7; k++) {
        const s = new Mesh(sph, stoneMat);
        const a = (k / 7) * Math.PI * 2;
        s.position.set(Math.cos(a) * 0.62, 0.1, Math.sin(a) * 0.62);
        s.scale.set(0.22, 0.14, 0.2);
        fire.add(s);
      }
      const fl = new Group();
      const cone = this.geo(new CylinderGeometry(0, 0.34, 1.0, 12));
      const outer = this.mat(new MeshBasicMaterial({ color: pal.accent, transparent: true, opacity: 0.92 }));
      const inner = this.mat(new MeshBasicMaterial({ color: pal.accent2, transparent: true, opacity: 0.95 }));
      const o = new Mesh(cone, outer); o.position.y = 0.55;
      const i2 = new Mesh(cone, inner); i2.position.y = 0.42; i2.scale.set(0.55, 0.62, 0.55);
      fl.add(o, i2);
      fire.add(fl);
      const glow = new Sprite(this.mat(new SpriteMaterial({ map: glowTex, color: pal.accent, blending: AdditiveBlending, transparent: true, depthWrite: false, opacity: 0.55 })));
      glow.scale.setScalar(4); glow.position.y = 0.8;
      fire.add(glow);
      fire.position.set(-5.2, 0, -3.3);
      this.root.add(fire);
      this.flame = fl;
      const light = new PointLight(pal.accent, 8, 9, 1.6);
      light.position.set(-5.2, 1.1, -3.3);
      this.root.add(light);
      this.fireLight = light;
    }
  }

  setDada(x: number, z: number) { (this.ground.uniforms.uDada.value as Vector3).set(x, 0, z); }

  addRipple(x: number, z: number) {
    const r = this.rip[this.ripHead];
    r.set(x, z, 0);
    this.ripHead = (this.ripHead + 1) % MAX_RIPPLES;
  }

  /** 在可玩区域内取一个离角色/其它星星够远的点 */
  private spot(avoid: Array<{ x: number; z: number }>): { x: number; z: number } {
    for (let n = 0; n < 24; n++) {
      const x = (Math.random() * 2 - 1) * (BOUNDS.x - 0.6);
      const z = BOUNDS.zMin + 0.4 + Math.random() * (BOUNDS.zMax - BOUNDS.zMin - 0.8);
      if (avoid.every((a) => Math.hypot(a.x - x, a.z - z) > 1.9)) return { x, z };
    }
    return { x: 0, z: 0 };
  }

  private buildProps() {
    const r = this.rand, pal = this.pal, world = this.world;
    const propMat = this.mat(new MeshStandardMaterial({ color: new Color(pal.prop).multiplyScalar(0.85), roughness: 0.78 }));
    const propMat2 = this.mat(new MeshStandardMaterial({ color: new Color(pal.prop).lerp(new Color(pal.accent), 0.4).multiplyScalar(0.9), roughness: 0.78 }));
    const trunkMat = this.mat(new MeshStandardMaterial({ color: pal.groundLine, roughness: 1 }));
    const accentMat = this.mat(new MeshStandardMaterial({ color: pal.accent, roughness: 0.55 }));
    const lightMat = this.mat(new MeshStandardMaterial({ color: pal.cloud, roughness: 0.7 }));
    const sph = this.geo(new SphereGeometry(1, 24, 16));
    const trunk = this.geo(new CylinderGeometry(0.1, 0.14, 0.9, 10));
    const pad = this.geo(new CylinderGeometry(1, 1, 0.06, 28));
    const slots: Array<[number, number]> = [];
    for (let i = 0; i < 10; i++) slots.push([-8 + i * 1.78 + (r() - 0.5) * 0.8, -5.4 - r() * 1.8]);
    for (let i = 0; i < 3; i++) { slots.push([-7.8 - r() * 0.9, -2.2 + i * 2.8 + r()]); slots.push([7.8 + r() * 0.9, -2.2 + i * 2.8 + r()]); }
    slots.forEach(([x, z], idx) => {
      const g = new Group();
      const s = 0.75 + r() * 0.8;
      if (world === "forest" || world === "sakura") {
        const t = new Mesh(trunk, trunkMat); t.position.y = 0.45; g.add(t);
        const crown = world === "sakura" ? propMat : propMat2;
        for (let k = 0; k < 3; k++) {
          const c = new Mesh(sph, k === 1 ? crown : propMat);
          c.position.set((k - 1) * 0.5, 1.25 + (k === 1 ? 0.3 : 0), (r() - 0.5) * 0.3);
          c.scale.setScalar(0.62 + (k === 1 ? 0.18 : 0));
          g.add(c);
        }
        if (world === "sakura") { const d = new Mesh(sph, lightMat); d.scale.setScalar(0.07); d.position.set(0.3, 1.5, 0.6); g.add(d); }
      } else if (world === "tide") {
        const p = new Mesh(pad, idx % 3 === 0 ? accentMat : propMat); p.scale.set(0.8, 1, 0.8); p.position.y = 0.04; g.add(p);
        if (idx % 3 === 0) { const f = new Mesh(sph, lightMat); f.scale.setScalar(0.14); f.position.y = 0.16; g.add(f); }
        this.bobbers.push({ o: g, y: 0, s: r() * 6 });
      } else if (world === "breeze") {
        for (let k = 0; k < 3; k++) { const c = new Mesh(sph, k === 1 ? propMat2 : propMat); c.position.set((k - 1) * 0.46, 0.3, (r() - 0.5) * 0.2); c.scale.set(0.44, 0.34 + r() * 0.1, 0.42); g.add(c); }
        if (idx % 4 === 0) {                       // 风车
          const stick = new Mesh(this.geo(new CylinderGeometry(0.03, 0.03, 1.1, 6)), trunkMat); stick.position.y = 0.55; g.add(stick);
          const blades = new Mesh(this.geo(new TorusGeometry(0.22, 0.09, 6, 4)), accentMat); blades.position.set(0, 1.12, 0.05); g.add(blades);
          this.spin.push(blades);
        }
      } else if (world === "graphite") {
        const bx = new Mesh(this.geo(new PlaneGeometry(1, 1)), idx % 2 ? propMat : propMat2); bx.scale.set(0.9, 0.6, 1); bx.position.y = 0.9 + r() * 0.5; bx.rotation.y = (r() - 0.5) * 0.6; g.add(bx);
        this.bobbers.push({ o: bx as unknown as Group, y: bx.position.y, s: r() * 6 });
      } else {
        const c = new Mesh(sph, propMat); c.position.y = 0.22 * s; c.scale.set(0.58, 0.32 + r() * 0.2, 0.48); g.add(c);
        if (r() > 0.55) { const c2 = new Mesh(sph, propMat2); c2.position.set(0.5, 0.15, 0.1); c2.scale.set(0.3, 0.2, 0.28); g.add(c2); }
      }
      g.position.set(x, 0, z);
      g.scale.setScalar(s);
      g.rotation.y = r() * 6.28;
      this.root.add(g);
    });
  }

  private buildClouds() {
    const r = this.rand;
    const mat = this.mat(new MeshStandardMaterial({ color: this.pal.cloud, roughness: 1, transparent: true, opacity: 0.88 }));
    const sph = this.geo(new SphereGeometry(1, 22, 14));
    for (let i = 0; i < 4; i++) {
      const g = new Group();
      for (let k = 0; k < 4; k++) {
        const m = new Mesh(sph, mat);
        m.position.set((k - 1.5) * 0.8, k % 2 ? 0.22 : 0, 0);
        m.scale.set(0.75 + (k % 2 ? 0.2 : 0), 0.48, 0.48);
        g.add(m);
      }
      g.position.set(-10 + i * 6 + r() * 1.5, 5 + r() * 1.4, -12 - r() * 3);
      g.scale.setScalar(1.2 + r() * 0.7);
      this.clouds.push(g);
      this.root.add(g);
    }
  }

  /** 把一颗星星摆到新位置 */
  private place(s: World3D["stars"][number], avoid: Array<{ x: number; z: number }>) {
    const p = this.spot(avoid);
    s.group.position.set(p.x, s.baseY, p.z);
    s.group.visible = true;
    s.group.scale.setScalar(0.01);
    s.alive = true;
  }

  /** 推进场景；返回本帧被角色碰到的星星位置 */
  update(dt: number, dada: { x: number; z: number }): Array<{ x: number; y: number; z: number }> {
    this.time += dt;
    const r = this.reduce, t = this.time;
    this.ground.uniforms.uTime.value = r ? 0 : t;
    for (const v of this.rip) if (v.z >= 0) { v.z += dt; if (v.z > 2.6) v.z = -1; }
    const got: Array<{ x: number; y: number; z: number }> = [];
    const alivePos = this.stars.filter((s) => s.alive).map((s) => ({ x: s.group.position.x, z: s.group.position.z }));
    for (const s of this.stars) {
      if (!s.alive) {
        s.respawn -= dt;
        if (s.respawn <= 0) this.place(s, [dada, ...alivePos]);
        continue;
      }
      const g = s.group;
      const grow = Math.min(1, g.scale.x + dt * 3);
      g.scale.setScalar(grow);
      if (!r) {
        s.mesh.rotation.y += dt * 1.8;
        g.position.y = s.baseY + Math.sin(t * 2.4 + s.seed) * 0.09;
        s.glow.material.opacity = 0.65 + Math.sin(t * 3 + s.seed) * 0.2;
      }
      if (Math.hypot(g.position.x - dada.x, g.position.z - dada.z) < 0.9) {
        got.push({ x: g.position.x, y: g.position.y, z: g.position.z });
        s.alive = false; s.respawn = 0.9; g.visible = false;
        this.addRipple(g.position.x, g.position.z);
      }
    }
    if (!r) {
      this.clouds.forEach((c, i) => { c.position.x += dt * (0.14 + i * 0.03); if (c.position.x > 16) c.position.x = -16; });
      this.spin.forEach((m) => { m.rotation.z += dt * 2.2; });
      this.bobbers.forEach((b) => { b.o.position.y = b.y + Math.sin(t * 1.3 + b.s) * 0.06; });
      if (this.flame) {
        const f = 1 + Math.sin(t * 9) * 0.06 + Math.sin(t * 4.3) * 0.08;
        this.flame.scale.set(1 / Math.sqrt(f), f, 1 / Math.sqrt(f));
      }
      if (this.fireLight) this.fireLight.intensity = 7 + Math.sin(t * 11) * 1.2 + Math.sin(t * 5.1) * 1.4;
    }
    return got;
  }

  /** 清场重来：星星立即重新散落 */
  reset() { this.stars.forEach((s, i) => { s.alive = false; s.respawn = i * 0.2; s.group.visible = false; }); }

  dispose() {
    this.geos.forEach((g) => g.dispose());
    this.mats.forEach((m) => m.dispose());
  }

  private geo<T extends { dispose(): void }>(g: T): T { this.geos.push(g); return g; }
  private mat<T extends { dispose(): void }>(m: T): T { this.mats.push(m); return m; }
}

