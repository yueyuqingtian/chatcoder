/** 乐园地面（plan-playground-v2）：标准材质 + 注入程序纹理，因此能接收实时阴影、跟随场景雾色淡出。
 * 各世界换「表面」：石墨稿纸网格 / 森林苔地光斑 / 篝火泥土余烬 / 煦风草浪 / 潮汐海底焦散 / 樱时落英。
 * 共用：涟漪圆环（移动 / 点击 / 得分时扩散）、角色脚下的一圈柔光。 */
import { Color, Mesh, MeshStandardMaterial, PlaneGeometry, Vector3 } from "three";

export const MAX_RIPPLES = 12;
export const GROUND_MODE: Record<string, number> = { graphite: 0, forest: 1, tide: 2, campfire: 3, sakura: 4, breeze: 5 };

/** 3D 地面配色：界面用的 --pg-* 偏浅（平面插画用），打上光、过色调映射后会发白，
 * 这里给每个世界一套更饱和、明度更低的地面色（a 主色 / b 斑驳色 / line 纹理线）。 */
export const GROUND_COLORS: Record<string, { light: [string, string, string]; dark: [string, string, string] }> = {
  graphite: { light: ["#e6e3dc", "#dad6ce", "#b4afa5"], dark: ["#2c2e36", "#262830", "#454856"] },
  forest: { light: ["#8cc47a", "#76b26a", "#5e9a58"], dark: ["#2c4a34", "#243f2c", "#3f6848"] },
  campfire: { light: ["#d6b088", "#c49c72", "#a47c56"], dark: ["#4a3529", "#3d2b22", "#604536"] },
  breeze: { light: ["#a6d78c", "#90c87c", "#78b46a"], dark: ["#2f5440", "#284836", "#406c50"] },
  tide: { light: ["#d9cf9c", "#bfc98f", "#ffffff"], dark: ["#2e4458", "#283a4c", "#a8d8ff"] },
  sakura: { light: ["#b4d89c", "#a0cb8c", "#88b878"], dark: ["#34503e", "#2c4535", "#486c54"] },
};

const COMMON = /* glsl */ `
  uniform float uTime; uniform int uMode;
  uniform vec3 uA, uB, uLine, uAccent, uAccent2;
  uniform vec3 uDada; uniform vec3 uRip[${MAX_RIPPLES}];
  varying vec3 vPgW;
  float pgHash(vec2 p) { return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }
  float pgNoise(vec2 p) {
    vec2 i = floor(p), f = fract(p); f = f * f * (3.0 - 2.0 * f);
    return mix(mix(pgHash(i), pgHash(i + vec2(1, 0)), f.x), mix(pgHash(i + vec2(0, 1)), pgHash(i + vec2(1, 1)), f.x), f.y);
  }
  float pgFbm(vec2 p) { float a = 0.5, s = 0.0; for (int i = 0; i < 4; i++) { s += a * pgNoise(p); p *= 2.03; a *= 0.5; } return s; }
  float pgCaustic(vec2 p, float t) {
    vec2 i = p; float c = 1.0; const float inten = 0.0065;
    for (int n = 0; n < 4; n++) {
      float tt = t * (1.0 - (3.5 / float(n + 1)));
      i = p + vec2(cos(tt - i.x) + sin(tt + i.y), sin(tt - i.y) + cos(tt + i.x));
      c += 1.0 / length(vec2(p.x / (sin(i.x + tt) / inten), p.y / (cos(i.y + tt) / inten)));
    }
    c /= 4.0; c = max(1.17 - pow(max(c, 0.0), 1.4), 0.0);
    return min(pow(c, 8.0), 1.5);
  }
`;

/** 计算 diffuse 底色与自发光（glow），写入 diffuseColor / pgGlow */
const SURFACE = /* glsl */ `
  vec3 pgGlow = vec3(0.0);
  {
  vec2 p = vPgW.xz;
  float t = uTime;
  float n = pgFbm(p * 0.22 + 3.0);
  vec3 col = mix(uA, uB, clamp(0.3 + n * 0.7, 0.0, 1.0));

  if (uMode == 2) {                 // 潮汐：海底细沙 + 焦散光网
    float c = pgCaustic(p * 0.36, t * 0.5 + 23.0);
    float c2 = pgCaustic(p * 0.24 + 7.0, t * 0.36 + 5.0);
    float ripple = sin(p.x * 3.0 + pgFbm(p * 1.5) * 6.0) * 0.5 + 0.5;
    col = mix(col, uB, 0.25 * ripple);
    pgGlow += uLine * (c * 0.55 + c2 * 0.3);
  } else if (uMode == 1) {          // 森林：苔地 + 落叶斑点 + 林间光斑
    float leaf = smoothstep(0.5, 0.82, pgFbm(p * 1.1));
    col = mix(col, uLine, leaf * 0.35);
    vec2 cell = floor(p * 2.4); vec2 f = fract(p * 2.4) - 0.5;
    float h = pgHash(cell);
    float dot1 = step(0.86, h) * smoothstep(0.16, 0.05, length(f + (vec2(pgHash(cell + 2.3), pgHash(cell + 5.1)) - 0.5) * 0.5));
    col = mix(col, uAccent2 * 0.85 + col * 0.15, dot1 * 0.5);
    float d = pgCaustic(p * 0.16 + 11.0, t * 0.15 + 2.0);
    pgGlow += uAccent2 * d * 0.12;
  } else if (uMode == 5) {          // 煦风：草浪一道道扫过
    float wave = sin(p.x * 0.6 - t * 1.1 + pgFbm(p * 0.35) * 5.0) * 0.5 + 0.5;
    col = mix(col, uLine, wave * 0.2 * smoothstep(0.2, 0.8, pgFbm(p * 0.6)));
    col = mix(col, uB * 1.08, pow(wave, 8.0) * 0.35);
  } else if (uMode == 3) {          // 篝火：泥土 + 余烬星点 + 火光
    vec2 cp = p * 3.2; vec2 cc = floor(cp); float hh = pgHash(cc);
    vec2 ff = fract(cp) - 0.5 + (vec2(pgHash(cc + 3.1), pgHash(cc + 7.7)) - 0.5) * 0.5;
    float sp = step(0.95, hh) * smoothstep(0.12, 0.0, length(ff)) * (0.5 + 0.5 * sin(t * 2.0 + hh * 40.0));
    col = mix(col, uLine, 0.25 * pgFbm(p * 0.7));
    float fd = length(p - vec2(0.0, -1.6));
    float flick = 0.8 + 0.2 * sin(t * 7.0) * sin(t * 3.1);
    pgGlow += uAccent * exp(-fd * 0.55) * 0.42 * flick + uAccent2 * sp * 0.7 * smoothstep(5.0, 1.0, fd);
  } else if (uMode == 4) {          // 樱时：草地上铺落瓣
    vec2 cell = floor(p * 1.9); vec2 f = fract(p * 1.9) - 0.5;
    float h = pgHash(cell);
    float a = h * 6.28; mat2 rot = mat2(cos(a), -sin(a), sin(a), cos(a));
    vec2 q = rot * f;
    float petal = step(0.7, h) * smoothstep(0.17, 0.08, length(q * vec2(1.0, 1.8)) + abs(q.x) * 0.2);
    col = mix(col, uLine, 0.18 * pgFbm(p * 0.6));
    col = mix(col, uAccent, petal * 0.62);
  } else {                          // 石墨：方格稿纸，每第 5 格一条稍深的线
    vec2 g = abs(fract(p * 1.0) - 0.5);
    float line = smoothstep(0.485, 0.5, max(g.x, g.y));
    vec2 g5 = abs(fract(p * 0.2) - 0.5);
    float line5 = smoothstep(0.494, 0.5, max(g5.x, g5.y));
    col = mix(col, uLine, line * 0.42 + line5 * 0.35);
  }

  // 角色脚下的柔光
  float dd = length(p - uDada.xz);
  pgGlow += uAccent2 * exp(-dd * dd * 0.45) * 0.06;

  // 涟漪圆环
  float rip = 0.0;
  for (int i = 0; i < ${MAX_RIPPLES}; i++) {
    vec3 R = uRip[i];
    if (R.z < 0.0) continue;
    float rad = R.z * 2.4;
    float d = abs(length(p - R.xy) - rad);
    rip += smoothstep(0.14, 0.0, d) * exp(-R.z * 1.8);
  }
  pgGlow += (uMode == 2 ? uLine : uAccent2) * rip * 0.35;
  diffuseColor.rgb = col;
  }
`;

export class Ground {
  readonly mesh: Mesh;
  readonly mat: MeshStandardMaterial;
  private readonly geo: PlaneGeometry;
  private readonly rip = Array.from({ length: MAX_RIPPLES }, () => new Vector3(0, 0, -1));
  private head = 0;
  private readonly uniforms: Record<string, { value: unknown }>;

  constructor(world: string, c: { a: string; b: string; line: string; accent: string; accent2: string }, y = 0) {
    this.uniforms = {
      uTime: { value: 0 }, uMode: { value: GROUND_MODE[world] ?? 0 },
      uA: { value: new Color(c.a) }, uB: { value: new Color(c.b) }, uLine: { value: new Color(c.line) },
      uAccent: { value: new Color(c.accent) }, uAccent2: { value: new Color(c.accent2) },
      uDada: { value: new Vector3() }, uRip: { value: this.rip },
    };
    this.mat = new MeshStandardMaterial({ color: 0xffffff, roughness: 0.95, metalness: 0 });
    this.mat.onBeforeCompile = (s) => {
      Object.assign(s.uniforms, this.uniforms);
      s.vertexShader = s.vertexShader
        .replace("#include <common>", "#include <common>\nvarying vec3 vPgW;")
        .replace("#include <project_vertex>", "#include <project_vertex>\nvPgW = (modelMatrix * vec4(transformed, 1.0)).xyz;");
      s.fragmentShader = s.fragmentShader
        .replace("#include <common>", "#include <common>\n" + COMMON)
        .replace("#include <color_fragment>", "#include <color_fragment>\n" + SURFACE)
        .replace("#include <emissivemap_fragment>", "#include <emissivemap_fragment>\ntotalEmissiveRadiance += pgGlow;");
    };
    this.geo = new PlaneGeometry(120, 120, 1, 1);
    this.mesh = new Mesh(this.geo, this.mat);
    this.mesh.rotation.x = -Math.PI / 2;
    this.mesh.position.y = y;
    this.mesh.receiveShadow = true;
  }

  addRipple(x: number, z: number) {
    this.rip[this.head].set(x, z, 0);
    this.head = (this.head + 1) % MAX_RIPPLES;
  }
  /** 外部（如水面）读取当前涟漪，用同一份数据画波纹 */
  get ripples() { return this.rip; }

  update(dt: number, t: number, dada: { x: number; z: number }) {
    this.uniforms.uTime.value = t;
    (this.uniforms.uDada.value as Vector3).set(dada.x, 0, dada.z);
    for (const v of this.rip) if (v.z >= 0) { v.z += dt; if (v.z > 2.6) v.z = -1; }
  }

  dispose() { this.geo.dispose(); this.mat.dispose(); }
}
