/** 篝火世界 · 烤棉花糖（plan-playground-v2）。
 * 场景：黄昏营地——石圈篝火（交叉木柴、分层火焰 shader、火光闪烁、余烬与轻烟）、帐篷（门帘半掀、拉绳地钉、三角旗）、
 *       圆木长凳、挂在木桩上的提灯、一盘棉花糖、外圈松林剪影。
 * 玩法：走到篝火旁，嗒嗒自动举起插着棉花糖的小棍；按住伸进火里烤，量表显示火候，松开就吃。
 *       金黄区间得分最高、连续完美有加成；烤过头会着火变黑，嗒嗒被呛得晕乎乎。 */
import {
  AdditiveBlending, Color, ConeGeometry, CylinderGeometry, DoubleSide, Group, Mesh, MeshBasicMaterial, PlaneGeometry,
  PointLight, ShaderMaterial, Shape, ShapeGeometry, SphereGeometry, Sprite, SpriteMaterial, CapsuleGeometry, BoxGeometry,
  LineBasicMaterial, BufferGeometry, Line, Vector3,
} from "three";
import { RoundedBoxGeometry } from "three/examples/jsm/geometries/RoundedBoxGeometry.js";
import { DADA_H } from "../dada3d";
import { Bag, lumpy, radialTex, rng, scatter, shadow, windify, bladeGeometry } from "../kit";
import { conifer, rock } from "../props";
import type { Game, GameCtx, GameDeps } from "./types";

const FIRE = new Vector3(0, 0, -1.7);

const FLAME_VERT = /* glsl */ `
  varying vec2 vUv;
  void main() { vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`;
const FLAME_FRAG = /* glsl */ `
  varying vec2 vUv;
  uniform float uTime, uSeed, uPower;
  uniform vec3 uC1, uC2, uC3;
  float h(vec2 p) { return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }
  float n2(vec2 p) { vec2 i = floor(p), f = fract(p); f = f * f * (3.0 - 2.0 * f);
    return mix(mix(h(i), h(i + vec2(1, 0)), f.x), mix(h(i + vec2(0, 1)), h(i + vec2(1, 1)), f.x), f.y); }
  float fbm(vec2 p) { float a = 0.5, s = 0.0; for (int i = 0; i < 4; i++) { s += a * n2(p); p *= 2.1; a *= 0.5; } return s; }
  void main() {
    vec2 uv = vUv;
    float n = fbm(vec2(uv.x * 3.2 + uSeed, uv.y * 2.4 - uTime * 2.6));
    vec2 p = uv - vec2(0.5, 0.12);
    p.x += (n - 0.5) * 0.42 * uv.y;
    float w = 0.34 * (1.0 - smoothstep(0.0, 0.82, p.y)) + 0.015;
    float body = smoothstep(w, w * 0.25, abs(p.x)) * smoothstep(-0.1, 0.04, p.y);
    body *= 1.0 - smoothstep(0.42, 0.86, p.y + (n - 0.5) * 0.36);
    float core = body * smoothstep(0.55, 0.0, p.y + abs(p.x) * 0.8);
    vec3 col = mix(uC3, uC2, smoothstep(0.0, 0.6, body));
    col = mix(col, uC1, core * core);
    gl_FragColor = vec4(col * uPower, body);
  }`;

export function createCampfire(d: GameDeps): Game {
  const bag = new Bag();
  const root = new Group();
  const r = rng("campfire-dusk");
  const dark = d.dark;

  // ── 篝火 ──
  const fire = new Group();
  fire.position.copy(FIRE);
  const stoneMat = bag.std(dark ? "#5b5a63" : "#a6a2a8", { rough: 0.95 });
  for (let k = 0; k < 10; k++) {
    const a = (k / 10) * Math.PI * 2;
    const s = rock(bag, stoneMat, r, 0.28 + r() * 0.08);
    s.position.set(Math.cos(a) * 0.98, -0.04, Math.sin(a) * 0.98);
    fire.add(s);
  }
  const char = bag.std("#4a3326", { rough: 0.95, emissive: "#ff5a1f", ei: 0.25 });
  const logGeo = bag.geo(new CylinderGeometry(0.11, 0.13, 1.5, 10));
  for (let k = 0; k < 5; k++) {
    const a = (k / 5) * Math.PI * 2 + 0.3;
    const lg = new Mesh(logGeo, char);
    lg.position.set(Math.cos(a) * 0.32, 0.42, Math.sin(a) * 0.32);
    lg.rotation.set(Math.sin(a) * 0.62, 0, -Math.cos(a) * 0.62);
    lg.castShadow = true;
    fire.add(lg);
  }
  const embersBed = new Mesh(bag.geo(lumpy(0.5, 2, 0.4, 3, 3)), bag.std("#3b2219", { rough: 1, emissive: "#ff6a2a", ei: 1.2 }));
  embersBed.scale.set(1, 0.18, 1); embersBed.position.y = 0.05;
  fire.add(embersBed);
  const flameUniforms = (seed: number, power: number) => ({
    uTime: { value: 0 }, uSeed: { value: seed }, uPower: { value: power },
    uC1: { value: new Color("#fff4c2") }, uC2: { value: new Color("#ffb03a") }, uC3: { value: new Color("#ff4a1c") },
  });
  const flames: Array<{ m: Mesh; u: ReturnType<typeof flameUniforms> }> = [];
  [[1.9, 2.6, 0, 1.0], [1.3, 2.0, 3.3, 1.25], [1.6, 2.2, 7.1, 0.9]].forEach(([w, h, seed, pw], i) => {
    const u = flameUniforms(seed, pw);
    const m = new Mesh(bag.geo(new PlaneGeometry(w, h)), bag.mat(new ShaderMaterial({
      vertexShader: FLAME_VERT, fragmentShader: FLAME_FRAG, uniforms: u,
      transparent: true, depthWrite: false, blending: AdditiveBlending, side: DoubleSide,
    })));
    m.position.set((i - 1) * 0.12, h / 2 - 0.05, (i - 1) * 0.08);
    fire.add(m);
    flames.push({ m, u });
  });
  const fireGlow = new Sprite(bag.mat(new SpriteMaterial({ map: d.glowTex, color: "#ff8a3a", blending: AdditiveBlending, transparent: true, depthWrite: false, opacity: dark ? 0.75 : 0.45 })));
  fireGlow.scale.setScalar(5.5); fireGlow.position.y = 0.9;
  fire.add(fireGlow);
  root.add(fire);
  const fireLight = new PointLight("#ff8f45", dark ? 30 : 14, 14, 1.5);
  fireLight.position.set(FIRE.x, 1.4, FIRE.z);
  root.add(fireLight);

  // 轻烟
  const smokeTex = bag.tex(radialTex([[0, "rgba(255,255,255,0.55)"], [0.6, "rgba(255,255,255,0.18)"], [1, "rgba(255,255,255,0)"]], 64));
  const smokes = Array.from({ length: 6 }, (_, i) => {
    const s = new Sprite(bag.mat(new SpriteMaterial({ map: smokeTex, color: dark ? "#6e6a78" : "#c9c3cf", transparent: true, depthWrite: false, opacity: 0 })));
    root.add(s);
    return { s, t: i / 6 };
  });

  // ── 帐篷 ──
  const tent = new Group();
  const canvasA = bag.std("#e9875a", { rough: 0.9, side: true });
  const canvasB = bag.std("#f4b07a", { rough: 0.9, side: true });
  const roofGeo = bag.geo(new BoxGeometry(2.0, 0.04, 2.6));
  for (const sx of [-1, 1]) {
    const p = new Mesh(roofGeo, canvasA);
    p.rotation.z = -sx * 0.88;
    p.position.set(sx * 0.62, 0.78, 0);
    tent.add(p);
  }
  const tri = new Shape(); tri.moveTo(-1.22, 0); tri.lineTo(1.22, 0); tri.lineTo(0, 1.55); tri.closePath();
  const back = new Mesh(bag.geo(new ShapeGeometry(tri)), canvasB);
  back.position.z = -1.28;
  tent.add(back);
  const flap = new Shape(); flap.moveTo(0, 0); flap.lineTo(0.62, 0); flap.lineTo(0, 1.55); flap.closePath();
  const flapGeo = bag.geo(new ShapeGeometry(flap));
  const fl = new Mesh(flapGeo, canvasB); fl.position.set(-1.22, 0, 1.28); fl.rotation.y = -0.5;
  const fr = new Mesh(flapGeo, canvasB); fr.scale.x = -1; fr.position.set(1.22, 0, 1.28); fr.rotation.y = 0.5;
  const inside = new Mesh(bag.geo(new ShapeGeometry(tri)), bag.mat(new MeshBasicMaterial({ color: dark ? "#2a1a1a" : "#5c3a2c" })));
  inside.position.z = 1.2; inside.scale.set(0.98, 0.98, 1);
  tent.add(fl, fr, inside);
  const ridge = new Mesh(bag.geo(new CylinderGeometry(0.04, 0.04, 2.9, 8)), bag.std("#8a6248"));
  ridge.rotation.x = Math.PI / 2; ridge.position.y = 1.56;
  tent.add(ridge);
  const flag = new Mesh(bag.geo(new ShapeGeometry((() => { const s = new Shape(); s.moveTo(0, 0); s.lineTo(0.5, -0.14); s.lineTo(0, -0.28); s.closePath(); return s; })())), bag.std("#ffd166", { side: true }));
  const pole = new Mesh(bag.geo(new CylinderGeometry(0.02, 0.02, 0.7, 6)), bag.std("#8a6248"));
  pole.position.set(0, 1.9, 1.3); flag.position.set(0.02, 2.22, 1.3);
  tent.add(pole, flag);
  // 拉绳 + 地钉
  const ropeMat = bag.mat(new LineBasicMaterial({ color: dark ? "#b0a090" : "#8b7b6a" }));
  for (const [sx, sz] of [[-1, 1], [1, 1], [-1, -1], [1, -1]]) {
    const g = bag.geo(new BufferGeometry().setFromPoints([new Vector3(sx * 0.3, 1.45, sz * 1.25), new Vector3(sx * 1.9, 0.02, sz * 1.7)]));
    tent.add(new Line(g, ropeMat));
    const peg = new Mesh(bag.geo(new ConeGeometry(0.04, 0.22, 6)), bag.std("#c9ccd6", { metal: 0.6, rough: 0.4 }));
    peg.rotation.x = Math.PI; peg.position.set(sx * 1.9, 0.08, sz * 1.7);
    tent.add(peg);
  }
  tent.position.set(-5.6, 0, -4.2); tent.rotation.y = 0.55;
  root.add(shadow(tent));
  const tentGlow = new PointLight("#ffb46a", dark ? 3 : 0, 3, 2);
  tentGlow.position.set(-5.2, 0.6, -3.5);
  root.add(tentGlow);

  // 圆木长凳
  const barkMat = bag.std(dark ? "#5a3f30" : "#8a5f43", { rough: 0.95 });
  const ringMat = bag.std(dark ? "#9b7a5c" : "#e2c39a", { rough: 0.9 });
  for (const [x, z, ry] of [[3.7, -3.6, -0.45], [-2.6, -4.3, 0.15]] as const) {
    const b = new Group();
    const lg = new Mesh(bag.geo(new CylinderGeometry(0.34, 0.36, 2.4, 16)), barkMat);
    lg.rotation.z = Math.PI / 2;
    for (const s of [-1, 1]) { const e = new Mesh(bag.geo(new CylinderGeometry(0.33, 0.33, 0.02, 16)), ringMat); e.rotation.z = Math.PI / 2; e.position.x = s * 1.205; b.add(e); }
    b.add(lg);
    b.position.set(x, 0.34, z); b.rotation.y = ry;
    root.add(shadow(b));
  }

  // 一盘棉花糖
  const plate = new Group();
  const pl = new Mesh(bag.geo(new CylinderGeometry(0.5, 0.42, 0.06, 28)), bag.std("#f2efe9", { rough: 0.4 }));
  pl.position.y = 0.03;
  plate.add(pl);
  const mmGeo = bag.geo(new CapsuleGeometry(0.1, 0.1, 4, 12));
  const mmCols = ["#fffaf2", "#ffd9e4", "#fffaf2", "#e4f3ff", "#fffaf2"];
  mmCols.forEach((c, i) => {
    const m = new Mesh(mmGeo, bag.std(c, { rough: 0.9 }));
    const a = i * 1.3;
    m.position.set(Math.cos(a) * 0.22, 0.16, Math.sin(a) * 0.22);
    m.rotation.set(Math.PI / 2, 0, a);
    plate.add(m);
  });
  plate.position.set(4.9, 0, -2.4);
  root.add(shadow(plate));

  // 提灯
  const lantern = new Group();
  const post = new Mesh(bag.geo(new CylinderGeometry(0.07, 0.09, 2.2, 8)), barkMat); post.position.y = 1.1;
  const armL = new Mesh(bag.geo(new CylinderGeometry(0.04, 0.04, 0.7, 6)), barkMat); armL.rotation.z = Math.PI / 2; armL.position.set(0.3, 2.1, 0);
  const box = new Mesh(bag.geo(new RoundedBoxGeometry(0.32, 0.42, 0.32, 2, 0.05)), bag.std("#ffe9b0", { emissive: "#ffc861", ei: dark ? 2 : 0.8, rough: 0.3 }));
  const capL = new Mesh(bag.geo(new ConeGeometry(0.26, 0.18, 4)), bag.std("#3c3c46", { metal: 0.5, rough: 0.4 }));
  capL.rotation.y = Math.PI / 4;
  const lanternHang = new Group();
  box.position.y = -0.28; capL.position.y = -0.02;
  lanternHang.add(box, capL);
  lanternHang.position.set(0.58, 2.04, 0);
  lantern.add(post, armL, lanternHang);
  lantern.position.set(-3.9, 0, 2.9);
  root.add(shadow(lantern, true, false));
  const lanternLight = new PointLight("#ffc861", dark ? 6 : 2, 6, 1.8);
  lanternLight.position.set(-3.3, 1.7, 2.9);
  root.add(lanternLight);
  const lanternHalo = new Sprite(bag.mat(new SpriteMaterial({ map: d.glowTex, color: "#ffc861", blending: AdditiveBlending, transparent: true, depthWrite: false, opacity: dark ? 0.8 : 0.4 })));
  lanternHalo.scale.setScalar(1.6); lanternHalo.position.set(-3.32, 1.76, 2.9);
  root.add(lanternHalo);

  // 松林剪影 + 草
  const pine = bag.std(dark ? "#2a3b3c" : "#5b7d6c", { rough: 0.9, flat: true });
  const pineBark = bag.std("#5a4033");
  for (let i = 0; i < 22; i++) {
    const back = i < 14;
    const x = back ? -13 + (i / 13) * 26 + (r() - 0.5) : (i % 2 ? 1 : -1) * (8.6 + r() * 3);
    const z = back ? -6.5 - r() * 4 : -3 + r() * 7;
    const t = conifer(bag, pine, pineBark, r, 1.1 + r() * 0.8);
    t.position.set(x, 0, z);
    root.add(t);
  }
  const grassMat = windify(bag.std(dark ? "#4a5a3c" : "#a3a06a", { rough: 1, side: true }), 0.08, 0.42);
  root.add(scatter(bag.geo(bladeGeometry(0.36, 0.06)), grassMat, 700, r, { x0: -12, x1: 12, z0: -8, z1: 6 },
    (x, z) => !(Math.abs(x) < 6 && z > -3.6 && z < 3.6) && Math.hypot(x + 5.6, z + 4.2) > 1.8, [0.7, 1.3]));

  // 月亮（暗色主题下更明显）
  const moon = new Sprite(bag.mat(new SpriteMaterial({ map: d.glowTex, color: "#fff4d6", transparent: true, depthWrite: false, opacity: dark ? 0.95 : 0.55, blending: AdditiveBlending })));
  moon.scale.setScalar(4); moon.position.set(8, 9.5, -22);
  const moonDisc = new Mesh(bag.geo(new SphereGeometry(0.7, 24, 16)), bag.mat(new MeshBasicMaterial({ color: "#fff8e6", fog: false })));
  moonDisc.position.copy(moon.position);
  root.add(moon, moonDisc);

  // ── 棉花糖与小棍 ──
  const stick = new Group();
  const stickMesh = new Mesh(bag.geo(new CylinderGeometry(0.022, 0.03, 1.7, 8)), bag.std("#a37650", { rough: 0.9 }));
  stickMesh.position.y = 0.85;
  const mmMat = bag.std("#fffaf2", { rough: 0.85, emissive: "#ff7a2a", ei: 0 });
  const mm = new Mesh(bag.geo(new CapsuleGeometry(0.13, 0.14, 6, 14)), mmMat);
  mm.position.y = 1.62;
  mm.castShadow = true;
  const burnFlame = new Sprite(bag.mat(new SpriteMaterial({ map: d.glowTex, color: "#ff7a2a", blending: AdditiveBlending, transparent: true, depthWrite: false, opacity: 0 })));
  burnFlame.scale.setScalar(0.7); burnFlame.position.y = 1.75;
  stick.add(stickMesh, mm, burnFlame);
  stick.rotation.x = 1.0;
  stick.visible = false;
  let attached = false;

  const RAW = new Color("#fffaf2"), LIGHT = new Color("#f8e0a8"), GOLD = new Color("#e8a54a"), BROWN = new Color("#9a5a2a"), BURNT = new Color("#2b1d17");
  const shade = (v: number, out: Color) => {
    if (v < 0.45) return out.lerpColors(RAW, LIGHT, v / 0.45);
    if (v < 0.72) return out.lerpColors(LIGHT, GOLD, (v - 0.45) / 0.27);
    if (v < 0.95) return out.lerpColors(GOLD, BROWN, (v - 0.72) / 0.23);
    return out.lerpColors(BROWN, BURNT, Math.min(1, (v - 0.95) / 0.2));
  };

  let roast = 0;
  let holding = false;
  let eating = 0;
  let streak = 0;
  let near = false;
  let toldNear = false;
  const BAND: [number, number] = [0.58, 0.82];
  const fireYaw = (ctx: GameCtx) => Math.atan2(FIRE.x - ctx.pos.x, FIRE.z - ctx.pos.z);

  const eat = (ctx: GameCtx) => {
    const v = roast;
    const at = { x: ctx.pos.x, y: DADA_H + 0.7, z: ctx.pos.z };
    if (v < 0.25) { ctx.pop("还没热呢，再烤会儿", at, "info"); return; }
    let n = 0, txt = "", tone: "good" | "great" | "bad" = "good";
    if (v > 1.0) { txt = "烤焦啦…"; tone = "bad"; streak = 0; }
    else if (v >= BAND[0] && v <= BAND[1]) { streak++; n = 3 + (streak >= 3 ? 2 : 0); txt = streak >= 3 ? `完美金黄 ×${streak}！` : "完美金黄！"; tone = "great"; }
    else if (v >= 0.45 && v < BAND[0]) { n = 2; txt = "香香的"; streak = 0; }
    else if (v > BAND[1]) { n = 1; txt = "有点焦，还行"; streak = 0; }
    else { n = 1; txt = "软软的，有点生"; streak = 0; }
    eating = 0.6;
    if (n > 0) ctx.score(n, `+${n} ${txt}`, at, tone); else ctx.pop(txt, at, tone);
    if (tone === "bad") {
      ctx.dada.emote("dizzy", 1.4); ctx.dada.shake();
      if (ctx.sparks) for (let i = 0; i < 10; i++) ctx.sparks.emit(ctx.pos.x + (Math.random() - 0.5) * 0.6, 2.0, ctx.pos.z, 0, 0.8, 0, 1.2, 50, new Color("#555"), -0.3);
    } else {
      ctx.dada.cheer(); ctx.dada.prod(); ctx.dada.emote("happy", 1.2);
      if (tone === "great") { ctx.confetti?.burst(ctx.pos.x, 2.3, ctx.pos.z, 26, 3.5); if (streak === 3) ctx.say("三连完美！之后每个多加 2 分", "great"); }
    }
    roast = 0;
  };

  return {
    root,
    bounds: { x: 5.8, zMin: -3.2, zMax: 3.3 },
    view: { elev: 27, lookY: 0.8, lookZ: -0.4, spanX: 7.6, spanZ: 5.0 },
    motes: { mode: "float", n: 60, color: dark ? "#fff4d6" : "#ffe2b0" },
    light: dark
      ? { sun: "#8ea0ff", sunI: 0.45, hemiSky: "#5b5a8a", hemiGround: "#3a2a28", hemiI: 0.36, exposure: 1.05 }
      : { sun: "#ffc59a", sunI: 1.4, hemiSky: "#e9d9ff", hemiGround: "#b98a6a", hemiI: 0.55, exposure: 1.0 },
    spawn: { x: 2.6, z: 1.8 },
    poke() { return near ? false : undefined; },
    down(ctx) {
      if (!near) return false;
      // 上一颗还没吃完就接着烤：直接换一颗新的
      if (eating > 0) { eating = 0; mm.scale.setScalar(0.3); }
      holding = true;
      ctx.lockMove = true;
      ctx.dada.pose("reach");
      return true;
    },
    up(ctx) {
      if (!holding) return;
      holding = false;
      ctx.lockMove = false;
      ctx.dada.pose(near ? "hold" : "none");
      eat(ctx);
    },
    update(dt, ctx) {
      const t = ctx.time;
      // 不让嗒嗒走进火堆
      const fx = ctx.pos.x - FIRE.x, fz = ctx.pos.z - FIRE.z, fd = Math.hypot(fx, fz);
      if (fd < 1.45) { ctx.pos.x = FIRE.x + (fx / (fd || 1)) * 1.45; ctx.pos.z = FIRE.z + (fz / (fd || 1)) * 1.45; }
      const dist = Math.hypot(ctx.pos.x - FIRE.x, ctx.pos.z - FIRE.z);
      const wasNear = near;
      near = dist < 2.9;
      if (near !== wasNear) {
        if (near) {
          if (!attached) { ctx.dada.handR.add(stick); attached = true; }
          ctx.dada.pose("hold");
          if (!toldNear) { toldNear = true; ctx.say("按住开始烤，松开就吃", "info"); }
        } else { ctx.dada.pose("none"); if (holding) { holding = false; ctx.lockMove = false; } ctx.meter(null); }
      }
      stick.visible = near;
      ctx.dada.faceTo(near ? fireYaw(ctx) : null);
      if (near) ctx.dada.lookAt(FIRE.x, 0.9, FIRE.z);

      // 小棍角度：举着 → 伸进火里
      const targetTilt = holding ? 1.35 : 0.65;
      stick.rotation.x += (targetTilt - stick.rotation.x) * Math.min(1, dt * 8);

      if (holding) {
        const rate = 0.34 * Math.max(0.5, Math.min(1.3, (3.1 - dist) / 1.3));
        roast += dt * rate;
        if (roast > 1.25) { holding = false; ctx.lockMove = false; ctx.dada.pose("hold"); eat(ctx); }
      }
      if (eating > 0) {
        eating -= dt;
        mm.scale.setScalar(Math.max(0.01, eating / 0.6));
        if (eating <= 0) { mm.scale.setScalar(0.01); }
      } else if (mm.scale.x < 1) mm.scale.setScalar(Math.min(1, mm.scale.x + dt * 2.5));
      shade(roast, mmMat.color);
      mmMat.emissiveIntensity = holding ? 0.25 + Math.max(0, roast - 0.9) * 2 : 0;
      (burnFlame.material as SpriteMaterial).opacity = roast > 0.95 ? Math.min(1, (roast - 0.95) * 4) * (0.7 + Math.sin(t * 20) * 0.3) : 0;
      if (near && (holding || roast > 0)) ctx.meter({ v: Math.min(1, roast / 1.1), band: [BAND[0] / 1.1, BAND[1] / 1.1], kind: "roast", label: holding ? "火候" : "按住继续烤" });
      else if (near) ctx.meter(null);

      // 火焰 / 火光 / 余烬 / 烟
      const cam = ctx.camera.position;
      flames.forEach((f, i) => {
        f.u.uTime.value = ctx.reduce ? 1.0 + i : t + i * 0.37;
        f.m.rotation.y = Math.atan2(cam.x - FIRE.x, cam.z - FIRE.z) + (i - 1) * 0.5;
      });
      if (!ctx.reduce) {
        const fk = 0.85 + Math.sin(t * 11) * 0.06 + Math.sin(t * 5.3) * 0.08 + Math.sin(t * 23) * 0.03;
        fireLight.intensity = (dark ? 30 : 14) * fk;
        fireGlow.scale.setScalar(5.5 * (0.95 + fk * 0.08));
        if (ctx.sparks && Math.random() < dt * 22) {
          ctx.sparks.emit(FIRE.x + (Math.random() - 0.5) * 0.5, 0.8, FIRE.z + (Math.random() - 0.5) * 0.5,
            (Math.random() - 0.5) * 0.5, 1.2 + Math.random() * 1.4, (Math.random() - 0.5) * 0.5, 1.4 + Math.random(), 18 + Math.random() * 14, new Color(Math.random() < 0.5 ? "#ffb03a" : "#ff6a2a"), -0.2);
        }
        smokes.forEach((s) => {
          s.t = (s.t + dt * 0.18) % 1;
          s.s.position.set(FIRE.x + Math.sin(s.t * 6 + t * 0.3) * 0.4 * s.t, 2.2 + s.t * 4.2, FIRE.z - s.t * 0.8);
          s.s.scale.setScalar(0.8 + s.t * 2.6);
          (s.s.material as SpriteMaterial).opacity = Math.sin(s.t * Math.PI) * (dark ? 0.16 : 0.22);
        });
        lanternHang.rotation.z = Math.sin(t * 1.2) * 0.06;
        flag.rotation.y = Math.sin(t * 3) * 0.3;
        lanternLight.intensity = (dark ? 6 : 2) * (0.92 + Math.sin(t * 9) * 0.04);
      }
    },
    reset(ctx) {
      roast = 0; holding = false; eating = 0; streak = 0; near = false; toldNear = false;
      ctx.dada.pose("none"); ctx.dada.faceTo(null); ctx.meter(null);
      stick.visible = false;
    },
    dispose() { stick.removeFromParent(); bag.dispose(); },
  };
}
