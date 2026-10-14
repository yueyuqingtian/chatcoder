/** 乐园建模工具箱（plan-playground-v2）：各世界共用的几何、材质与资源回收。
 * 风格目标是「软胶玩具 + 微缩模型」：圆角、团块、低频噪声起伏，避免硬朗的几何拼接感。 */
import {
  BufferGeometry, CanvasTexture, Color, DoubleSide, Float32BufferAttribute, IcosahedronGeometry, InstancedMesh,
  Matrix4, MeshStandardMaterial, Object3D, Quaternion, Vector3, type Material, type Texture,
} from "three";
import { mergeVertices } from "three/examples/jsm/utils/BufferGeometryUtils.js";

/** 以字符串为种子的伪随机（同一世界每次布景一致） */
export function rng(seed: string) {
  let h = 2166136261;
  for (let i = 0; i < seed.length; i++) { h ^= seed.charCodeAt(i); h = Math.imul(h, 16777619); }
  return () => { h = Math.imul(h ^ (h >>> 15), 2246822507); h = Math.imul(h ^ (h >>> 13), 3266489909); h ^= h >>> 16; return (h >>> 0) / 4294967296; };
}

function hash3(x: number, y: number, z: number) {
  const s = Math.sin(x * 127.1 + y * 311.7 + z * 74.7) * 43758.5453;
  return s - Math.floor(s);
}
/** 三维值噪声（0~1），用于团块形变 */
export function noise3(x: number, y: number, z: number) {
  const xi = Math.floor(x), yi = Math.floor(y), zi = Math.floor(z);
  const xf = x - xi, yf = y - yi, zf = z - zi;
  const u = xf * xf * (3 - 2 * xf), v = yf * yf * (3 - 2 * yf), w = zf * zf * (3 - 2 * zf);
  const l = (a: number, b: number, t: number) => a + (b - a) * t;
  const c = (dx: number, dy: number, dz: number) => hash3(xi + dx, yi + dy, zi + dz);
  return l(
    l(l(c(0, 0, 0), c(1, 0, 0), u), l(c(0, 1, 0), c(1, 1, 0), u), v),
    l(l(c(0, 0, 1), c(1, 0, 1), u), l(c(0, 1, 1), c(1, 1, 1), u), v),
    w,
  );
}

/** 资源袋：几何 / 材质 / 贴图统一登记，dispose 一次释放 */
export class Bag {
  private readonly items: Array<{ dispose(): void }> = [];
  geo<T extends BufferGeometry>(g: T): T { this.items.push(g); return g; }
  mat<T extends Material>(m: T): T { this.items.push(m); return m; }
  tex<T extends Texture>(t: T): T { this.items.push(t); return t; }
  /** 常用：标准材质 */
  std(color: string | number | Color, opts: Partial<{ rough: number; metal: number; emissive: string | number | Color; ei: number; flat: boolean; side: boolean; opacity: number }> = {}) {
    const m = new MeshStandardMaterial({
      color, roughness: opts.rough ?? 0.78, metalness: opts.metal ?? 0, flatShading: opts.flat ?? false,
    });
    if (opts.emissive !== undefined) { m.emissive = new Color(opts.emissive); m.emissiveIntensity = opts.ei ?? 1; }
    if (opts.side) m.side = DoubleSide;
    if (opts.opacity !== undefined) { m.transparent = true; m.opacity = opts.opacity; m.depthWrite = opts.opacity > 0.6; }
    return this.mat(m);
  }
  dispose() { this.items.forEach((i) => i.dispose()); this.items.length = 0; }
}

/** 团块：二十面体细分后按噪声起伏，再合并顶点得到平滑法线（树冠、灌木、石头、云） */
export function lumpy(radius: number, detail: number, amp: number, freq: number, seed: number, flat = false): BufferGeometry {
  let g: BufferGeometry = new IcosahedronGeometry(radius, detail);
  g.deleteAttribute("normal"); g.deleteAttribute("uv");
  g = mergeVertices(g);
  const p = g.getAttribute("position");
  const v = new Vector3();
  for (let i = 0; i < p.count; i++) {
    v.fromBufferAttribute(p, i);
    const n = noise3(v.x * freq + seed, v.y * freq + seed * 0.7, v.z * freq - seed);
    const n2 = noise3(v.x * freq * 2.3 - seed, v.y * freq * 2.3, v.z * freq * 2.3 + seed);
    v.multiplyScalar(1 + (n - 0.5) * amp + (n2 - 0.5) * amp * 0.4);
    p.setXYZ(i, v.x, v.y, v.z);
  }
  if (flat) g = g.toNonIndexed();
  g.computeVertexNormals();
  return g;
}

/** 递归设置投射 / 接收阴影 */
export function shadow<T extends Object3D>(o: T, cast = true, receive = true): T {
  o.traverse((c) => { c.castShadow = cast; c.receiveShadow = receive; });
  return o;
}

/** 径向柔光贴图（腮红、光晕、投影点） */
export function radialTex(stops: Array<[number, string]>, size = 128): CanvasTexture {
  const c = document.createElement("canvas");
  c.width = c.height = size;
  const g = c.getContext("2d")!;
  const r = size / 2;
  const grd = g.createRadialGradient(r, r, 0, r, r, r);
  stops.forEach(([o, col]) => grd.addColorStop(o, col));
  g.fillStyle = grd;
  g.fillRect(0, 0, size, size);
  return new CanvasTexture(c);
}

/** 竖向渐变贴图（光柱、烟、水面反光条）：上下两端透明，中段最亮 */
export function shaftTex(): CanvasTexture {
  const c = document.createElement("canvas");
  c.width = 64; c.height = 256;
  const g = c.getContext("2d")!;
  const v = g.createLinearGradient(0, 0, 0, 256);
  v.addColorStop(0, "rgba(255,255,255,0)");
  v.addColorStop(0.35, "rgba(255,255,255,0.75)");
  v.addColorStop(1, "rgba(255,255,255,0)");
  g.fillStyle = v; g.fillRect(0, 0, 64, 256);
  g.globalCompositeOperation = "destination-in";
  const h = g.createLinearGradient(0, 0, 64, 0);
  h.addColorStop(0, "rgba(0,0,0,0)"); h.addColorStop(0.5, "rgba(0,0,0,1)"); h.addColorStop(1, "rgba(0,0,0,0)");
  g.fillStyle = h; g.fillRect(0, 0, 64, 256);
  return new CanvasTexture(c);
}

/** 一片草叶：底宽顶尖、略向前弯的三角带（实例化铺草用） */
export function bladeGeometry(h = 0.42, w = 0.07, segs = 4): BufferGeometry {
  const pos: number[] = [], idx: number[] = [];
  for (let i = 0; i <= segs; i++) {
    const t = i / segs, y = t * h, half = (w / 2) * (1 - t * 0.92), z = t * t * h * 0.22;
    pos.push(-half, y, z, half, y, z);
  }
  for (let i = 0; i < segs; i++) { const a = i * 2; idx.push(a, a + 1, a + 2, a + 1, a + 3, a + 2); }
  const g = new BufferGeometry();
  g.setAttribute("position", new Float32BufferAttribute(pos, 3));
  g.setIndex(idx);
  g.computeVertexNormals();
  return g;
}

/** 共享的风场时钟：所有挂了风摆的材质读同一个 uniform */
export const windClock = { value: 0 };

/** 让材质的顶点随风摆动（按高度加权，实例各自错相）；strength 越大摆得越开 */
export function windify(m: MeshStandardMaterial, strength = 0.12, heightRef = 0.45) {
  m.onBeforeCompile = (s) => {
    s.uniforms.uWindT = windClock;
    s.vertexShader = s.vertexShader
      .replace("#include <common>", "#include <common>\nuniform float uWindT;")
      .replace("#include <begin_vertex>", `#include <begin_vertex>
        {
          vec3 wp = vec3(0.0);
          #ifdef USE_INSTANCING
            wp = (instanceMatrix * vec4(0.0, 0.0, 0.0, 1.0)).xyz;
          #endif
          wp += (modelMatrix * vec4(0.0, 0.0, 0.0, 1.0)).xyz;
          float k = clamp(position.y / ${heightRef.toFixed(3)}, 0.0, 1.5); k *= k;
          float w = sin(uWindT * 1.7 + wp.x * 0.55 + wp.z * 0.3) * 0.6 + sin(uWindT * 3.1 + wp.x * 1.3) * 0.25;
          transformed.x += w * ${strength.toFixed(3)} * k;
          transformed.z += cos(uWindT * 1.3 + wp.z * 0.7) * ${(strength * 0.5).toFixed(3)} * k;
        }`);
  };
  m.customProgramCacheKey = () => `wind-${strength}-${heightRef}`;
  return m;
}

/** 在区域里撒实例（草叶、花、蕨），accept 决定某点是否可放（避开玩耍区等） */
export function scatter(geo: BufferGeometry, mat: Material, n: number, r: () => number,
  area: { x0: number; x1: number; z0: number; z1: number }, accept: (x: number, z: number) => boolean,
  size: [number, number] = [0.8, 1.3], tilt = 0.25): InstancedMesh {
  const im = new InstancedMesh(geo, mat, n);
  const m = new Matrix4(), q = new Quaternion(), s = new Vector3(), p = new Vector3(), e = new Object3D();
  let k = 0, guard = 0;
  while (k < n && guard++ < n * 12) {
    const x = area.x0 + r() * (area.x1 - area.x0), z = area.z0 + r() * (area.z1 - area.z0);
    if (!accept(x, z)) continue;
    e.rotation.set((r() - 0.5) * tilt, r() * Math.PI * 2, (r() - 0.5) * tilt);
    q.setFromEuler(e.rotation);
    const sc = size[0] + r() * (size[1] - size[0]);
    s.set(sc, sc * (0.8 + r() * 0.4), sc);
    p.set(x, 0, z);
    m.compose(p, q, s);
    im.setMatrixAt(k++, m);
  }
  im.count = k;
  im.instanceMatrix.needsUpdate = true;
  return im;
}

/** 缓动 */
export const ease = {
  outBack: (t: number) => { const c = 1.70158; return 1 + (c + 1) * Math.pow(t - 1, 3) + c * Math.pow(t - 1, 2); },
  outCubic: (t: number) => 1 - Math.pow(1 - t, 3),
  inOut: (t: number) => (t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2),
};
