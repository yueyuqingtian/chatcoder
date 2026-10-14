/** 潮汐世界 · 打水漂（plan-playground-v2）。
 * 场景：浅湾——会起伏的半透明水面（随波折射环境光、涟漪处泛白沫）透出海底焦散；
 *       左后方沙洲上的椰子树、海星与贝壳，右后方礁石上的红白灯塔（旋转光束），摇晃的浮标、盘旋的海鸥、漂着的小黄鸭。
 * 玩法：嗒嗒套着泳圈漂在近处。按住蓄力（头顶力度表来回摆），对准方向松开，石片在水面一跳一跳；
 *       力度落在亮区跳得最多；石片擦过小黄鸭额外得分。 */
import {
  AdditiveBlending, BufferGeometry, Color, ConeGeometry, CylinderGeometry, DoubleSide, ExtrudeGeometry, Float32BufferAttribute,
  Group, LatheGeometry, Mesh, MeshBasicMaterial, MeshPhysicalMaterial, PlaneGeometry, PointLight, Shape, SphereGeometry,
  TorusGeometry, Vector2, Vector3,
} from "three";
import { DADA_H } from "../dada3d";
import { Bag, lumpy, rng, shadow } from "../kit";
import { rock } from "../props";
import type { Game, GameCtx, GameDeps } from "./types";
import { MAX_RIPPLES } from "../ground";

/** 水面起伏（与 shader 里的公式一致，不含涟漪） */
function waveH(x: number, z: number, t: number) {
  return 0.06 * Math.sin(0.9 * x + 1.2 * t) + 0.045 * Math.sin(1.3 * z - 0.9 * t + 0.7 * x) + 0.03 * Math.sin(2.1 * x + 1.7 * z + 2.1 * t);
}

const WATER_GLSL = /* glsl */ `
  uniform float uWT; uniform vec3 uRip[${MAX_RIPPLES}];
  float wH(vec2 p) {
    float t = uWT;
    float h = 0.06 * sin(0.9 * p.x + 1.2 * t) + 0.045 * sin(1.3 * p.y - 0.9 * t + 0.7 * p.x) + 0.03 * sin(2.1 * p.x + 1.7 * p.y + 2.1 * t);
    for (int i = 0; i < ${MAX_RIPPLES}; i++) {
      vec3 R = uRip[i];
      if (R.z < 0.0) continue;
      float d = length(p - R.xy) - R.z * 2.4;
      h += sin(d * 9.0) * exp(-d * d * 5.0) * exp(-R.z * 1.6) * 0.07;
    }
    return h;
  }
  float wFoam(vec2 p) {
    float f = 0.0;
    for (int i = 0; i < ${MAX_RIPPLES}; i++) {
      vec3 R = uRip[i];
      if (R.z < 0.0) continue;
      float d = abs(length(p - R.xy) - R.z * 2.4);
      f += smoothstep(0.12, 0.0, d) * exp(-R.z * 2.0);
    }
    return f;
  }
`;

/** 棕榈叶：沿 x 伸出、向下弯、中脉对折的叶片 */
function frondGeometry(len = 2.3, width = 0.42, n = 14) {
  const pos: number[] = [], idx: number[] = [];
  for (let i = 0; i <= n; i++) {
    const t = i / n, x = t * len, y = 0.35 * t - 0.95 * t * t;
    const w = width * Math.sin(Math.PI * Math.min(1, t * 1.05)) * (1 - t * 0.3);
    pos.push(x, y - w * 0.28, -w, x, y + 0.02, 0, x, y - w * 0.28, w);
  }
  for (let i = 0; i < n; i++) {
    const a = i * 3, b = a + 3;
    idx.push(a, b, a + 1, a + 1, b, b + 1, a + 1, b + 1, a + 2, a + 2, b + 1, b + 2);
  }
  const g = new BufferGeometry();
  g.setAttribute("position", new Float32BufferAttribute(pos, 3));
  g.setIndex(idx);
  g.computeVertexNormals();
  return g;
}

function starfishShape() {
  const s = new Shape();
  for (let k = 0; k < 10; k++) {
    const a = Math.PI / 2 + (k * Math.PI) / 5, rr = k % 2 ? 0.1 : 0.28;
    if (k === 0) s.moveTo(Math.cos(a) * rr, Math.sin(a) * rr); else s.lineTo(Math.cos(a) * rr, Math.sin(a) * rr);
  }
  s.closePath();
  return s;
}

interface Stone { m: Mesh; p: Vector3; v: Vector3; left: number; hops: number; alive: boolean; hitDuck: Set<number>; spin: number }

export function createTide(d: GameDeps): Game {
  const bag = new Bag();
  const root = new Group();
  const r = rng("tide-bay");
  const dark = d.dark;

  // ── 水面 ──
  const wU = { uWT: { value: 0 }, uRip: { value: d.ground.ripples } };
  const waterMat = bag.mat(new MeshPhysicalMaterial({
    color: dark ? "#1d5f8c" : "#2fb3d4", roughness: 0.12, metalness: 0, clearcoat: 0.6, clearcoatRoughness: 0.08,
    transparent: true, opacity: dark ? 0.66 : 0.5, envMapIntensity: dark ? 0.22 : 0.55, depthWrite: false,
  }));
  waterMat.onBeforeCompile = (s) => {
    Object.assign(s.uniforms, wU);
    s.vertexShader = s.vertexShader
      .replace("#include <common>", "#include <common>\n" + WATER_GLSL + "\nvarying float vFoam;")
      .replace("#include <beginnormal_vertex>", `
        vec3 wpN = (modelMatrix * vec4(position, 1.0)).xyz;
        float e = 0.08;
        float hx = wH(wpN.xz + vec2(e, 0.0)) - wH(wpN.xz - vec2(e, 0.0));
        float hz = wH(wpN.xz + vec2(0.0, e)) - wH(wpN.xz - vec2(0.0, e));
        vec3 nW = normalize(vec3(-hx / (2.0 * e), 1.0, -hz / (2.0 * e)));
        vec3 objectNormal = vec3(nW.x, -nW.z, nW.y);`)
      .replace("#include <begin_vertex>", `#include <begin_vertex>
        transformed.z += wH(wpN.xz);
        vFoam = wFoam(wpN.xz);`);
    s.fragmentShader = s.fragmentShader
      .replace("#include <common>", "#include <common>\nvarying float vFoam;")
      .replace("#include <color_fragment>", "#include <color_fragment>\ndiffuseColor.rgb = mix(diffuseColor.rgb, vec3(1.0), clamp(vFoam, 0.0, 1.0) * 0.75);\ndiffuseColor.a = min(1.0, diffuseColor.a + vFoam * 0.4);")
      // 菲涅尔：越斜看水越不透明，近处能看清海底、远处是一整片水色
      .replace("#include <opaque_fragment>", `float pgFr = pow(1.0 - saturate(dot(normalize(vViewPosition), normal)), 2.5);
        diffuseColor.a = mix(diffuseColor.a, 0.96, pgFr);
        #include <opaque_fragment>`);
  };
  waterMat.customProgramCacheKey = () => "pg-water";
  const water = new Mesh(bag.geo(new PlaneGeometry(46, 32, 220, 150)), waterMat);
  water.rotation.x = -Math.PI / 2;
  water.position.z = -3;
  water.renderOrder = 2;
  water.receiveShadow = true;
  root.add(water);

  // ── 沙洲 + 椰子树 ──
  const sand = bag.std(dark ? "#a89a7c" : "#f2dfb2", { rough: 1 });
  const island = new Group();
  const mound = new Mesh(bag.geo(lumpy(1, 3, 0.15, 0.9, 4)), sand);
  mound.scale.set(3.6, 0.9, 2.4); mound.position.y = -0.35;
  island.add(mound);
  const palm = new Group();
  const trunkMat = bag.std(dark ? "#7a6048" : "#a98257", { rough: 0.95 });
  const ringMat = bag.std(dark ? "#634c39" : "#8a6845", { rough: 0.95 });
  let px = 0, py = 0.3;
  for (let i = 0; i < 9; i++) {
    const seg = new Mesh(bag.geo(new CylinderGeometry(0.17 - i * 0.008, 0.2 - i * 0.008, 0.42, 10)), i % 2 ? ringMat : trunkMat);
    const lean = 0.06 + i * 0.035;
    seg.position.set(px, py + 0.2, 0);
    seg.rotation.z = -lean;
    palm.add(seg);
    px += Math.sin(lean) * 0.4; py += Math.cos(lean) * 0.4;
  }
  const leafMat = bag.std(dark ? "#2f7a55" : "#4fb06a", { rough: 0.8, side: true });
  const frond = bag.geo(frondGeometry());
  const crown = new Group();
  crown.position.set(px, py + 0.1, 0);
  for (let k = 0; k < 8; k++) {
    const f = new Mesh(frond, leafMat);
    f.rotation.y = (k / 8) * Math.PI * 2 + r() * 0.3;
    f.rotation.z = 0.15 + r() * 0.2;
    f.scale.setScalar(0.85 + r() * 0.3);
    crown.add(f);
  }
  const nut = bag.std("#6b4a2e", { rough: 0.6 });
  for (let k = 0; k < 3; k++) { const c = new Mesh(bag.geo(new SphereGeometry(0.14, 12, 10)), nut); const a = k * 2.1; c.position.set(Math.cos(a) * 0.16, -0.12, Math.sin(a) * 0.16); crown.add(c); }
  palm.add(crown);
  palm.position.set(-0.6, 0.15, 0);
  island.add(palm);
  const star = new Mesh(bag.geo(new ExtrudeGeometry(starfishShape(), { depth: 0.04, bevelEnabled: true, bevelThickness: 0.03, bevelSize: 0.03, bevelSegments: 2 })), bag.std("#ff8a5c", { rough: 0.7 }));
  star.rotation.x = -Math.PI / 2; star.position.set(1.4, 0.48, 0.9);
  const shell = new Mesh(bag.geo(new SphereGeometry(0.16, 16, 8, 0, Math.PI * 2, 0, Math.PI / 2)), bag.std("#ffd9e4", { rough: 0.5 }));
  shell.scale.set(1, 0.45, 1.2); shell.position.set(0.6, 0.5, 1.3);
  island.add(star, shell);
  island.position.set(-8.2, 0, -6.2);
  root.add(shadow(island));
  const rockMat = bag.std(dark ? "#4e5866" : "#9aa6b3", { rough: 0.9 });
  for (const [x, z, s] of [[-5.6, -5.4, 0.5], [-10.4, -4.6, 0.7], [6.8, -3.2, 0.45], [9.6, -1.4, 0.6], [-9.2, 1.8, 0.5]] as const) {
    const k = rock(bag, rockMat, r, s);
    k.position.set(x, -0.2, z);
    root.add(k);
  }

  // ── 灯塔 ──
  const lh = new Group();
  const base = rock(bag, rockMat, r, 1.6);
  base.position.y = -0.5;
  lh.add(base);
  const red = bag.std("#e2574c", { rough: 0.5 }), white = bag.std("#f7f4ee", { rough: 0.5 });
  for (let i = 0; i < 5; i++) {
    const seg = new Mesh(bag.geo(new CylinderGeometry(0.62 - (i + 1) * 0.06, 0.62 - i * 0.06, 0.8, 24)), i % 2 ? white : red);
    seg.position.y = 0.6 + i * 0.8;
    lh.add(seg);
  }
  const gallery = new Mesh(bag.geo(new TorusGeometry(0.4, 0.05, 8, 24)), bag.std("#3c3c46", { metal: 0.5, rough: 0.4 }));
  gallery.rotation.x = Math.PI / 2; gallery.position.y = 4.2;
  const glassRoom = new Mesh(bag.geo(new CylinderGeometry(0.28, 0.28, 0.5, 16)), bag.std("#fff3c4", { emissive: "#ffd36b", ei: dark ? 2.2 : 0.8, rough: 0.2 }));
  glassRoom.position.y = 4.48;
  const roof = new Mesh(bag.geo(new ConeGeometry(0.4, 0.5, 16)), red);
  roof.position.y = 4.98;
  lh.add(gallery, glassRoom, roof);
  const beamMat = bag.mat(new MeshBasicMaterial({ color: "#fff1b8", transparent: true, opacity: dark ? 0.22 : 0.08, blending: AdditiveBlending, depthWrite: false, side: DoubleSide }));
  const beam = new Mesh(bag.geo(new ConeGeometry(0.9, 7, 24, 1, true).translate(0, -3.5, 0).rotateZ(Math.PI / 2)), beamMat);
  const beamPivot = new Group();
  beamPivot.position.y = 4.48;
  beamPivot.add(beam);
  lh.add(beamPivot);
  lh.position.set(9.2, 0, -8.5);
  root.add(shadow(lh, true, false));
  beam.castShadow = false;
  if (dark) { const l = new PointLight("#ffd36b", 6, 8, 1.6); l.position.set(9.2, 4.5, -8.5); root.add(l); }

  // ── 浮标 ──
  const buoyGeo = bag.geo(new LatheGeometry([new Vector2(0, -0.3), new Vector2(0.28, -0.25), new Vector2(0.34, 0), new Vector2(0.2, 0.25), new Vector2(0.06, 0.62), new Vector2(0, 0.64)], 18));
  const buoySpec: Array<[number, number, typeof red]> = [[-6.4, -1.6, red], [6.6, -5.6, white], [-3.6, -7.2, red]];
  const buoys = buoySpec.map(([x, z, m], i) => {
    const b = new Mesh(buoyGeo, m);
    const band = new Mesh(bag.geo(new CylinderGeometry(0.26, 0.31, 0.12, 18)), m === red ? white : red);
    band.position.y = 0.06;
    b.add(band);
    const lamp = new Mesh(bag.geo(new SphereGeometry(0.06, 10, 8)), bag.mat(new MeshBasicMaterial({ color: "#ffe28a" })));
    lamp.position.y = 0.66; b.add(lamp);
    b.position.set(x, 0, z);
    root.add(shadow(b, true, false));
    return { b, seed: i * 1.7 };
  });

  // ── 海鸥 ──
  const gullMat = bag.std("#ffffff", { rough: 0.6, side: true });
  const gulls = [0, 1].map((i) => {
    const g = new Group();
    const body = new Mesh(bag.geo(new SphereGeometry(0.16, 14, 10)), gullMat);
    body.scale.set(0.8, 0.7, 1.6);
    const wingGeo = bag.geo(new PlaneGeometry(0.75, 0.22).translate(0.37, 0, 0));
    const wl = new Group(), wr = new Group();
    const L = new Mesh(wingGeo, gullMat), R = new Mesh(wingGeo, gullMat);
    L.rotation.x = R.rotation.x = -Math.PI / 2; R.scale.x = -1;
    wl.add(L); wr.add(R);
    const beak = new Mesh(bag.geo(new ConeGeometry(0.04, 0.14, 6)), bag.std("#ffb340"));
    beak.rotation.x = Math.PI / 2; beak.position.z = 0.28;
    g.add(body, wl, wr, beak);
    root.add(g);
    return { g, wl, wr, a: i * Math.PI, rad: 6 + i * 2, h: 5 + i * 0.8 };
  });

  // ── 小黄鸭 ──
  const duckY = bag.std("#ffd23f", { rough: 0.35 });
  const duckO = bag.std("#ff8a2a", { rough: 0.4 });
  const ink = bag.std("#1b1a24", { rough: 0.2 });
  const ducks = [0, 1].map((i) => {
    const g = new Group();
    const body = new Mesh(bag.geo(new SphereGeometry(0.42, 24, 16)), duckY);
    body.scale.set(1, 0.72, 1.25); body.position.y = 0.12;
    const tailM = new Mesh(bag.geo(new ConeGeometry(0.16, 0.3, 12)), duckY);
    tailM.rotation.x = -2.2; tailM.position.set(0, 0.32, -0.5);
    const head = new Mesh(bag.geo(new SphereGeometry(0.27, 20, 14)), duckY);
    head.position.set(0, 0.6, 0.26);
    const beak = new Mesh(bag.geo(new SphereGeometry(0.13, 14, 8)), duckO);
    beak.scale.set(1.1, 0.4, 1.2); beak.position.set(0, 0.55, 0.52);
    g.add(body, tailM, head, beak);
    for (const s of [-1, 1]) { const e = new Mesh(bag.geo(new SphereGeometry(0.04, 10, 8)), ink); e.position.set(s * 0.12, 0.68, 0.48); g.add(e); }
    root.add(shadow(g, true, false));
    const pos = new Vector3(i === 0 ? -3 : 3.4, 0, i === 0 ? -4.2 : -2.6);
    return { g, pos, home: pos.clone(), spin: 0, hop: 0, seed: i * 3 };
  });

  // ── 石片与瞄准 ──
  const stoneMat = bag.std(dark ? "#8b95a3" : "#8d96a2", { rough: 0.7 });
  const stoneGeo = bag.geo(lumpy(0.2, 2, 0.25, 3, 9));
  const stones: Stone[] = Array.from({ length: 3 }, () => {
    const m = new Mesh(stoneGeo, stoneMat);
    m.scale.set(1, 0.32, 0.8); m.castShadow = true; m.visible = false;
    root.add(m);
    return { m, p: new Vector3(), v: new Vector3(), left: 0, hops: 0, alive: false, hitDuck: new Set<number>(), spin: 0 };
  });
  const inHand = new Mesh(stoneGeo, stoneMat);
  inHand.scale.set(0.8, 0.26, 0.64);
  inHand.position.set(0, 0, 0.12);
  inHand.visible = false;
  const aimMat = bag.mat(new MeshBasicMaterial({ color: "#ffffff", transparent: true, opacity: 0, depthWrite: false, depthTest: false }));
  const aimDots = Array.from({ length: 9 }, () => {
    const m = new Mesh(bag.geo(new CylinderGeometry(0.07, 0.07, 0.01, 12)), aimMat);
    m.renderOrder = 3;
    root.add(m);
    return m;
  });

  const BAND: [number, number] = [0.72, 0.92];
  let charging = false, charge = 0, power = 0, aimYaw = Math.PI, attached = false;
  const cSplash = new Color("#e8f8ff"), cGold = new Color("#ffd166");

  const throwStone = (ctx: GameCtx) => {
    const s = stones.find((x) => !x.alive) ?? stones[0];
    const inBand = power >= BAND[0] && power <= BAND[1];
    const gap = inBand ? 0 : Math.min(power < BAND[0] ? BAND[0] - power : power - BAND[1], 0.45) / 0.45;
    const q = 1 - gap;
    s.left = Math.max(1, Math.round(1 + q * 4.6 + (inBand ? 1.4 : 0)));
    s.hops = 0;
    const sp = 4.2 + power * 5.5;
    ctx.dada.handR.getWorldPosition(s.p);
    s.v.set(Math.sin(aimYaw) * sp, 2.2 + power * 0.8, Math.cos(aimYaw) * sp);
    s.alive = true; s.m.visible = true; s.hitDuck.clear(); s.spin = 0;
    if (inBand) ctx.pop("好手感！", { x: ctx.pos.x, y: DADA_H + 1.1, z: ctx.pos.z }, "great");
  };

  const endStone = (ctx: GameCtx, s: Stone, sink: boolean) => {
    s.alive = false; s.m.visible = false;
    if (sink && ctx.sparks) for (let i = 0; i < 16; i++) ctx.sparks.emit(s.p.x, 0.1, s.p.z, (Math.random() - 0.5) * 1.6, 2 + Math.random() * 2, (Math.random() - 0.5) * 1.6, 0.8, 26, cSplash, 9);
    if (sink) ctx.ripple(s.p.x, s.p.z);
    const n = s.hops;
    const msg = n >= 7 ? `${n} 跳！水上漂！` : n >= 5 ? `${n} 跳，漂亮！` : n > 0 ? `打出 ${n} 跳` : "咕咚…沉了";
    ctx.say(msg, n >= 5 ? "great" : n > 0 ? "info" : "bad");
    if (n >= 6) { ctx.confetti?.burst(ctx.pos.x, 2.4, ctx.pos.z, 36, 4); ctx.dada.pose("up"); window.setTimeout(() => ctx.dada.pose("none"), 900); }
    if (n >= 4) ctx.dada.cheer(); else if (n === 0) ctx.dada.emote("dizzy", 1);
  };

  return {
    root,
    bounds: { x: 5.6, zMin: 0.2, zMax: 3.4 },
    view: { elev: 24, lookY: 0.4, lookZ: -1.2, spanX: 7.8, spanZ: 5.2 },
    move: { max: 3.4, accel: 2.4 },
    motes: { mode: "rise", n: 30, color: "#ffffff" },
    light: dark
      ? { sun: "#a9c4ff", sunI: 0.9, hemiSky: "#6a8ccc", hemiGround: "#1e3a52", hemiI: 0.52, exposure: 1.08 }
      : { sun: "#fff6e6", sunI: 1.7, hemiSky: "#e6f7ff", hemiGround: "#8fc7dd", hemiI: 0.65, exposure: 1.02 },
    spawn: { x: 0, z: 1.8 },
    poke() { return false; },
    down(ctx) {
      if (!attached) { ctx.dada.handR.add(inHand); attached = true; }
      charging = true; charge = 0;
      ctx.lockMove = true;
      ctx.dada.pose("wind");
      inHand.visible = true;
      return true;
    },
    up(ctx) {
      if (!charging) return;
      charging = false;
      ctx.lockMove = false;
      ctx.meter(null);
      throwStone(ctx);
      inHand.visible = false;
      ctx.dada.pose("throw");
      ctx.dada.faceTo(aimYaw);
      window.setTimeout(() => { ctx.dada.pose("none"); ctx.dada.faceTo(null); }, 420);
    },
    update(dt, ctx) {
      const t = ctx.reduce ? 0 : ctx.time;
      wU.uWT.value = t;
      ctx.dada.setFloatY(waveH(ctx.pos.x, ctx.pos.z, t));

      // 蓄力：力度来回摆，朝指针方向瞄准
      if (charging) {
        charge += dt;
        power = 0.5 - 0.5 * Math.cos(charge * 2.8);
        if (ctx.pointer.has) aimYaw = Math.atan2(ctx.pointer.x - ctx.pos.x, ctx.pointer.z - ctx.pos.z);
        ctx.dada.faceTo(aimYaw);
        ctx.meter({ v: power, band: BAND, kind: "power", label: "力度" });
        const len = 1.5 + power * 5;
        aimDots.forEach((m, i) => {
          const k = (i + 1) / aimDots.length;
          const x = ctx.pos.x + Math.sin(aimYaw) * (0.9 + k * len), z = ctx.pos.z + Math.cos(aimYaw) * (0.9 + k * len);
          m.position.set(x, waveH(x, z, t) + 0.03, z);
          m.scale.setScalar(1 - k * 0.5);
        });
        aimMat.opacity = 0.75;
      } else aimMat.opacity = Math.max(0, aimMat.opacity - dt * 4);

      // 石片：抛物线 → 触水弹起，速度与弹跳逐次衰减
      for (const s of stones) {
        if (!s.alive) continue;
        s.v.y -= 13 * dt;
        s.p.addScaledVector(s.v, dt);
        s.spin += dt * 30;
        s.m.position.copy(s.p);
        s.m.rotation.set(0.15, s.spin, 0.1);
        const wh = waveH(s.p.x, s.p.z, t);
        if (s.p.y <= wh && s.v.y < 0) {
          if (s.left > 0) {
            s.left--; s.hops++;
            s.p.y = wh;
            s.v.y = 2.6 * Math.pow(0.84, s.hops);
            s.v.x *= 0.86; s.v.z *= 0.86;
            ctx.ripple(s.p.x, s.p.z);
            ctx.score(1, `${s.hops}`, { x: s.p.x, y: 0.7, z: s.p.z }, "good");
            if (ctx.sparks) for (let i = 0; i < 6; i++) ctx.sparks.emit(s.p.x, 0.08, s.p.z, (Math.random() - 0.5) * 1.2, 1 + Math.random() * 1.2, (Math.random() - 0.5) * 1.2, 0.5, 20, cSplash, 8);
          } else { endStone(ctx, s, true); continue; }
        }
        // 擦过小黄鸭
        ducks.forEach((dk, i) => {
          if (s.hitDuck.has(i) || s.p.y > 1.1) return;
          if (Math.hypot(s.p.x - dk.pos.x, s.p.z - dk.pos.z) < 0.75) {
            s.hitDuck.add(i);
            dk.spin = 1; dk.hop = 1;
            ctx.score(3, "+3 命中小黄鸭！", { x: dk.pos.x, y: 1.4, z: dk.pos.z }, "great");
            ctx.sparks?.burst(dk.pos.x, 0.8, dk.pos.z, 20, cGold, 2.4, 36);
          }
        });
        if (Math.abs(s.p.x) > 16 || s.p.z < -14 || s.p.z > 8) endStone(ctx, s, false);
      }

      // 小黄鸭：慢慢漂，被打中跳一下转一圈，然后漂去别处
      ducks.forEach((dk, i) => {
        if (dk.hop > 0) {
          dk.hop = Math.max(0, dk.hop - dt * 1.6);
          if (dk.hop === 0) dk.home.set((Math.random() * 2 - 1) * 5, 0, -4.8 + Math.random() * 2.8);
        }
        dk.spin = Math.max(0, dk.spin - dt * 1.2);
        const drift = ctx.reduce ? 0 : 1;
        dk.pos.x += (dk.home.x + Math.sin(ctx.time * 0.3 + dk.seed) * 0.6 * drift - dk.pos.x) * Math.min(1, dt * 0.6);
        dk.pos.z += (dk.home.z + Math.cos(ctx.time * 0.25 + dk.seed) * 0.4 * drift - dk.pos.z) * Math.min(1, dt * 0.6);
        dk.g.position.set(dk.pos.x, waveH(dk.pos.x, dk.pos.z, t) - 0.12 + Math.sin(dk.hop * Math.PI) * 0.9, dk.pos.z);
        dk.g.rotation.set(Math.sin(t * 1.4 + i) * 0.08, 0.6 + i + dk.spin * Math.PI * 4 + Math.sin(ctx.time * 0.2 + i) * 0.5, Math.cos(t * 1.1 + i) * 0.06);
      });

      buoys.forEach((b) => {
        b.b.position.y = waveH(b.b.position.x, b.b.position.z, t) - 0.05;
        b.b.rotation.set(Math.sin(t * 1.3 + b.seed) * 0.12, 0, Math.cos(t * 1.1 + b.seed) * 0.12);
      });
      if (!ctx.reduce) {
        beamPivot.rotation.y = ctx.time * 0.8;
        crown.rotation.y = Math.sin(ctx.time * 0.6) * 0.06;
        crown.children.forEach((c, i) => { if (i < 8) c.rotation.z = 0.2 + Math.sin(ctx.time * 1.4 + i) * 0.06; });
        gulls.forEach((g) => {
          g.a += dt * 0.22;
          g.g.position.set(Math.cos(g.a) * g.rad, g.h + Math.sin(g.a * 3) * 0.3, -6 + Math.sin(g.a) * g.rad * 0.5);
          g.g.rotation.set(0, Math.atan2(-Math.sin(g.a) * g.rad, Math.cos(g.a) * g.rad * 0.5), 0.25);
          const flap = Math.sin(ctx.time * 5 + g.a) * 0.5;
          g.wl.rotation.z = flap; g.wr.rotation.z = -flap;
        });
      }
    },
    reset(ctx) {
      stones.forEach((s) => { s.alive = false; s.m.visible = false; });
      charging = false; inHand.visible = false;
      ctx.dada.pose("none"); ctx.dada.faceTo(null);
    },
    dispose() { inHand.removeFromParent(); bag.dispose(); },
  };
}
