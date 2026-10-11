/** 乐园特效（plan-114-452）：软边光点粒子池 + 环境漂浮光点 + 发光精灵贴图。
 * 全部是 additive 软圆点（一个 Points 一次 draw call），用于：移动拖尾、收集爆发、被戳冒心、环境尘光 / 萤火 / 花瓣 / 火星。 */
import {
  AdditiveBlending, BufferAttribute, BufferGeometry, CanvasTexture, Color, Points, ShaderMaterial, type Texture,
} from "three";

const VERT = /* glsl */ `
  attribute float aSize; attribute float aAlpha; attribute vec3 aColor;
  varying float vA; varying vec3 vC;
  uniform float uScale;
  void main() {
    vA = aAlpha; vC = aColor;
    vec4 mv = modelViewMatrix * vec4(position, 1.0);
    gl_PointSize = aSize * 0.01 * uScale / -mv.z; // aSize 单位：厘米（世界单位 / 100）
    gl_Position = projectionMatrix * mv;
  }`;
const FRAG = /* glsl */ `
  varying float vA; varying vec3 vC;
  void main() {
    float d = length(gl_PointCoord - 0.5) * 2.0;
    float a = smoothstep(1.0, 0.0, d);
    a = a * a * vA;
    if (a < 0.003) discard;
    gl_FragColor = vec4(vC * (0.6 + a), a);
  }`;

function makePoints(n: number, scale: number) {
  const geo = new BufferGeometry();
  const pos = new Float32Array(n * 3), size = new Float32Array(n), alpha = new Float32Array(n), col = new Float32Array(n * 3);
  geo.setAttribute("position", new BufferAttribute(pos, 3));
  geo.setAttribute("aSize", new BufferAttribute(size, 1));
  geo.setAttribute("aAlpha", new BufferAttribute(alpha, 1));
  geo.setAttribute("aColor", new BufferAttribute(col, 3));
  const mat = new ShaderMaterial({
    vertexShader: VERT, fragmentShader: FRAG, transparent: true, depthWrite: false, blending: AdditiveBlending,
    uniforms: { uScale: { value: scale } },
  });
  const pts = new Points(geo, mat);
  pts.frustumCulled = false;
  return { pts, geo, mat, pos, size, alpha, col };
}

interface P { life: number; max: number; vx: number; vy: number; vz: number; s0: number; g: number }

/** 事件粒子池：环形缓冲，满了覆盖最旧的 */
export class Sparks {
  readonly obj;
  private readonly d;
  private readonly ps: P[];
  private head = 0;
  constructor(private readonly n = 260, scale = 420) {
    this.d = makePoints(n, scale);
    this.obj = this.d.pts;
    this.ps = Array.from({ length: n }, () => ({ life: 0, max: 1, vx: 0, vy: 0, vz: 0, s0: 1, g: 0 }));
  }
  emit(x: number, y: number, z: number, vx: number, vy: number, vz: number, life: number, size: number, color: Color, gravity = 0) {
    const i = this.head; this.head = (this.head + 1) % this.n;
    const p = this.ps[i];
    p.life = life; p.max = life; p.vx = vx; p.vy = vy; p.vz = vz; p.s0 = size; p.g = gravity;
    const d = this.d;
    d.pos[i * 3] = x; d.pos[i * 3 + 1] = y; d.pos[i * 3 + 2] = z;
    d.col[i * 3] = color.r; d.col[i * 3 + 1] = color.g; d.col[i * 3 + 2] = color.b;
  }
  burst(x: number, y: number, z: number, count: number, color: Color, speed = 2.4, size = 34) {
    for (let k = 0; k < count; k++) {
      const a = Math.random() * Math.PI * 2, u = Math.random();
      this.emit(x, y, z, Math.cos(a) * speed * u, 1.2 + Math.random() * speed * 0.9, Math.sin(a) * speed * u, 0.55 + Math.random() * 0.45, size * (0.6 + Math.random() * 0.8), color, 5);
    }
  }
  update(dt: number) {
    const d = this.d;
    for (let i = 0; i < this.n; i++) {
      const p = this.ps[i];
      if (p.life <= 0) { d.alpha[i] = 0; continue; }
      p.life -= dt;
      p.vy -= p.g * dt;
      d.pos[i * 3] += p.vx * dt; d.pos[i * 3 + 1] += p.vy * dt; d.pos[i * 3 + 2] += p.vz * dt;
      const t = Math.max(0, p.life / p.max);
      d.alpha[i] = t; d.size[i] = p.s0 * (0.4 + t * 0.6);
    }
    (d.geo.attributes.position as BufferAttribute).needsUpdate = true;
    (d.geo.attributes.aAlpha as BufferAttribute).needsUpdate = true;
    (d.geo.attributes.aSize as BufferAttribute).needsUpdate = true;
    (d.geo.attributes.aColor as BufferAttribute).needsUpdate = true;
  }
  setScale(v: number) { this.d.mat.uniforms.uScale.value = v; }
  dispose() { this.d.geo.dispose(); this.d.mat.dispose(); }
}

/** 环境光点：在场地上空缓慢漂浮、循环（尘光 / 萤火 / 花瓣 / 火星，只是颜色与运动方式不同） */
export class Motes {
  readonly obj;
  private readonly d;
  private readonly seed: Float32Array;
  private t = 0;
  constructor(private readonly n: number, color: Color, private readonly mode: "float" | "rise" | "fall", scale = 420) {
    this.d = makePoints(n, scale);
    this.obj = this.d.pts;
    this.seed = new Float32Array(n * 4);
    for (let i = 0; i < n; i++) {
      this.seed.set([Math.random(), Math.random(), Math.random(), Math.random()], i * 4);
      this.d.col.set([color.r, color.g, color.b], i * 3);
    }
    this.update(0, true);
  }
  update(dt: number, still = false) {
    this.t += dt;
    const d = this.d, t = this.t;
    for (let i = 0; i < this.n; i++) {
      const a = this.seed[i * 4], b = this.seed[i * 4 + 1], c = this.seed[i * 4 + 2], e = this.seed[i * 4 + 3];
      const x = (a * 2 - 1) * 9 + Math.sin(t * 0.3 + c * 6.28) * 0.6;
      let y: number;
      if (this.mode === "rise") y = ((t * (0.18 + e * 0.25) + b * 5) % 5) + 0.1;
      else if (this.mode === "fall") y = 5 - ((t * (0.25 + e * 0.3) + b * 5) % 5);
      else y = 0.6 + b * 3.4 + Math.sin(t * 0.5 + c * 6.28) * 0.25;
      const z = -4.6 + b * 9.2 + Math.cos(t * 0.35 + a * 6.28) * 0.5;
      d.pos[i * 3] = x; d.pos[i * 3 + 1] = y; d.pos[i * 3 + 2] = z;
      const tw = 0.5 + 0.5 * Math.sin(t * (0.8 + e * 1.4) + c * 12);
      d.alpha[i] = (0.25 + 0.5 * tw) * (still ? 0.7 : 1);
      d.size[i] = 10 + e * 16;
    }
    (d.geo.attributes.position as BufferAttribute).needsUpdate = true;
    (d.geo.attributes.aAlpha as BufferAttribute).needsUpdate = true;
    (d.geo.attributes.aSize as BufferAttribute).needsUpdate = true;
    (d.geo.attributes.aColor as BufferAttribute).needsUpdate = true;
  }
  setScale(v: number) { this.d.mat.uniforms.uScale.value = v; }
  dispose() { this.d.geo.dispose(); this.d.mat.dispose(); }
}

/** 发光精灵贴图（径向渐变，additive 使用）：收集物光晕、篝火辉光 */
export function makeGlowTexture(): Texture {
  const c = document.createElement("canvas");
  c.width = c.height = 128;
  const g = c.getContext("2d")!;
  const grd = g.createRadialGradient(64, 64, 0, 64, 64, 64);
  grd.addColorStop(0, "rgba(255,255,255,1)");
  grd.addColorStop(0.25, "rgba(255,255,255,0.45)");
  grd.addColorStop(1, "rgba(255,255,255,0)");
  g.fillStyle = grd;
  g.fillRect(0, 0, 128, 128);
  return new CanvasTexture(c);
}
