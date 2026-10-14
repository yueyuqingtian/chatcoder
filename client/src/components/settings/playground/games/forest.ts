/** 森林世界 · 萤火捕手（plan-playground-v2）。
 * 场景：林间空地——外圈针叶 / 阔叶树、红伞白点蘑菇与夜光小蘑菇、倒木与苔石、蕨丛与随风草叶、斜照的林间光柱；
 *       左前方树桩上放着一只玻璃萤火罐，抓到的萤火会飞进去，罐子越来越亮。
 * 玩法：萤火在林间游荡，嗒嗒靠近时会躲开；高处的要点击起跳才够得到。金色萤火偶尔出现，更快也更值钱。 */
import {
  AdditiveBlending, Color, CylinderGeometry, DoubleSide, Group, LatheGeometry, Mesh, MeshBasicMaterial, MeshPhysicalMaterial,
  PlaneGeometry, PointLight, SphereGeometry, Sprite, SpriteMaterial, Vector2, Vector3,
} from "three";
import { DADA_H } from "../dada3d";
import { Bag, bladeGeometry, lumpy, rng, scatter, shadow, shaftTex, windify } from "../kit";
import { conifer, fernGeometry, mushroom, rock, roundTree } from "../props";
import type { Game, GameCtx, GameDeps } from "./types";

interface Fly {
  g: Group; core: Mesh; halo: Sprite;
  p: Vector3; v: Vector3; goal: Vector3;
  gold: boolean; state: "in" | "free" | "caught" | "gone";
  t: number; life: number; from: Vector3; seed: number;
}

const JAR = new Vector3(-6.1, 0, 2.5);

export function createForest(d: GameDeps): Game {
  const bag = new Bag();
  const root = new Group();
  const r = rng("forest-clearing");
  const dark = d.dark;

  const leafA = bag.std(dark ? "#2f6b4c" : "#5fae73", { rough: 0.85 });
  const leafB = bag.std(dark ? "#295c47" : "#4c9a68", { rough: 0.85 });
  const leafC = bag.std(dark ? "#3c7a52" : "#7cc47f", { rough: 0.85 });
  const pine = bag.std(dark ? "#22503f" : "#3f8a62", { rough: 0.85, flat: true });
  const bark = bag.std(dark ? "#5a4033" : "#8a6248", { rough: 0.95 });
  const stone = bag.std(dark ? "#5d6470" : "#b9bec6", { rough: 0.9 });
  const moss = bag.std(dark ? "#3d7a4e" : "#79b86a", { rough: 1 });

  // ── 树：后排两层 + 两侧 ──
  const avoid = (x: number, z: number) => Math.abs(x) < 7.4 && z > -4.4 && z < 4.6;
  for (let i = 0; i < 26; i++) {
    let x = 0, z = 0;
    for (let k = 0; k < 20; k++) {
      if (i < 14) { x = -12 + (i / 13) * 24 + (r() - 0.5) * 1.4; z = -5.2 - r() * 4.5; }
      else { const side = i % 2 ? 1 : -1; x = side * (8.2 + r() * 3.5); z = -4 + r() * 8; }
      if (!avoid(x, z)) break;
    }
    const s = 0.9 + r() * 0.7;
    const tree = r() > 0.45 ? conifer(bag, pine, bark, r, s * 1.15) : roundTree(bag, [leafA, leafB, leafC], bark, r, s);
    tree.position.set(x, 0, z);
    tree.rotation.y = r() * 6;
    root.add(tree);
  }

  // 蘑菇：红伞白点 + 夜光小蘑菇
  const capRed = bag.std("#e2574c", { rough: 0.45 });
  const stem = bag.std("#f4ecdf", { rough: 0.8 });
  const dot = bag.std("#fffaf2", { rough: 0.6 });
  const glowCap = bag.std("#7fe0ff", { rough: 0.4, emissive: "#57c8ff", ei: dark ? 1.4 : 0.4 });
  const glowCaps: Group[] = [];
  const shroomSpots: Array<[number, number, boolean]> = [[-6.8, -3.2, false], [-6.3, -3.6, false], [6.9, 3.4, false], [7.3, 3.0, true], [6.7, 2.7, true], [-7.4, 0.2, true], [4.6, -4.5, false], [-2.6, -4.7, true]];
  for (const [x, z, glow] of shroomSpots) {
    const m = mushroom(bag, glow ? glowCap : capRed, stem, glow ? null : dot, r, glow ? 0.55 + r() * 0.2 : 0.8 + r() * 0.5);
    m.position.set(x, 0, z);
    root.add(m);
    if (glow) glowCaps.push(m);
  }

  // 倒木：横放树干 + 年轮端面 + 苔藓
  const log = new Group();
  const lg = new Mesh(bag.geo(new CylinderGeometry(0.42, 0.48, 3.6, 18)), bark);
  lg.rotation.z = Math.PI / 2;
  const ring = bag.std(dark ? "#9b7a5c" : "#e2c39a", { rough: 0.9 });
  const endA = new Mesh(bag.geo(new CylinderGeometry(0.4, 0.4, 0.02, 18)), ring);
  endA.rotation.z = Math.PI / 2; endA.position.x = 1.8;
  const mossTop = new Mesh(bag.geo(lumpy(1, 2, 0.5, 2, 7)), moss);
  mossTop.scale.set(1.2, 0.22, 0.4); mossTop.position.set(-0.3, 0.42, 0);
  log.add(lg, endA, mossTop);
  log.position.set(5.2, 0.45, -4.2); log.rotation.y = 0.35;
  root.add(shadow(log));

  // 苔石
  for (const [x, z, s] of [[-4.6, -4.4, 0.7], [7.6, -1.4, 0.8], [-7.8, -1.8, 0.6], [3.2, 4.4, 0.45]] as const) {
    const k = rock(bag, stone, r, s, moss);
    k.position.set(x, 0, z);
    root.add(k);
  }

  // 蕨丛 + 草叶
  const fern = bag.geo(fernGeometry(9, 0.9, 0.22));
  const fernMat = windify(bag.std(dark ? "#2f7050" : "#58a865", { rough: 0.9, side: true }), 0.08, 0.9);
  const ferns = scatter(fern, fernMat, 26, r, { x0: -11, x1: 11, z0: -6, z1: 5 }, (x, z) => !avoid(x, z) || (Math.abs(x) > 6.6 && z > -3.6));
  ferns.castShadow = true;
  root.add(ferns);
  const grassMat = windify(bag.std(dark ? "#356c48" : "#6fb46c", { rough: 1, side: true }), 0.1, 0.42);
  const grass = scatter(bag.geo(bladeGeometry(0.42, 0.07)), grassMat, 1300, r, { x0: -12, x1: 12, z0: -8, z1: 6 }, (x, z) => {
    const c = Math.abs(x) < 5.6 && z > -3 && z < 3.4;
    return !c || r() < 0.12;
  }, [0.7, 1.4]);
  root.add(grass);

  // 林间光柱
  const shaft = bag.tex(shaftTex());
  const shaftMat = bag.mat(new MeshBasicMaterial({ map: shaft, color: dark ? "#9fd9ff" : "#fff0b8", transparent: true, opacity: dark ? 0.12 : 0.28, blending: AdditiveBlending, depthWrite: false, side: DoubleSide }));
  const shafts: Mesh[] = [];
  for (let i = 0; i < 5; i++) {
    const m = new Mesh(bag.geo(new PlaneGeometry(1.4 + r() * 1.2, 9)), shaftMat);
    m.position.set(-7 + i * 3.4 + r(), 3.8, -3 + r() * 3);
    m.rotation.set(0, 0.3, 0.42);
    shafts.push(m); root.add(m);
  }

  // 萤火罐：树桩 + 玻璃罐 + 木塞
  const stump = new Group();
  const st = new Mesh(bag.geo(new CylinderGeometry(0.62, 0.78, 0.75, 16)), bark);
  st.position.y = 0.375;
  const top = new Mesh(bag.geo(new CylinderGeometry(0.6, 0.6, 0.02, 16)), ring);
  top.position.y = 0.76;
  stump.add(st, top);
  stump.position.copy(JAR);
  root.add(shadow(stump));
  const jarPts = [new Vector2(0, 0), new Vector2(0.36, 0), new Vector2(0.42, 0.08), new Vector2(0.44, 0.7), new Vector2(0.34, 0.86), new Vector2(0.3, 0.98)];
  const glass = new Mesh(bag.geo(new LatheGeometry(jarPts, 32)), bag.mat(new MeshPhysicalMaterial({
    color: "#e8fbff", roughness: 0.05, metalness: 0, transparent: true, opacity: 0.28, clearcoat: 1, side: DoubleSide, depthWrite: false,
  })));
  glass.position.set(JAR.x, 0.77, JAR.z);
  const cork = new Mesh(bag.geo(new CylinderGeometry(0.3, 0.27, 0.16, 16)), bag.std("#c79a6b", { rough: 0.9 }));
  cork.position.set(JAR.x, 0.77 + 1.04, JAR.z);
  root.add(glass, shadow(cork));
  const jarLight = new PointLight(d.pal.accent2, 0, 5, 1.6);
  jarLight.position.set(JAR.x, 1.3, JAR.z);
  root.add(jarLight);
  const jarDots: Mesh[] = [];
  const dotGeo = bag.geo(new SphereGeometry(0.05, 8, 6));
  const dotMat = bag.mat(new MeshBasicMaterial({ color: new Color("#fff6a0") }));
  let inJar = 0;

  // ── 萤火 ──
  const flyGeo = bag.geo(new SphereGeometry(0.085, 14, 10));
  const coreMat = bag.mat(new MeshBasicMaterial({ color: new Color("#fbffb0") }));
  const goldMat = bag.mat(new MeshBasicMaterial({ color: new Color("#ffd166") }));
  const haloMat = bag.mat(new SpriteMaterial({ map: d.glowTex, color: "#d9ff7a", blending: AdditiveBlending, transparent: true, depthWrite: false, opacity: 0.9 }));
  const haloGold = bag.mat(new SpriteMaterial({ map: d.glowTex, color: "#ffc24a", blending: AdditiveBlending, transparent: true, depthWrite: false, opacity: 1 }));
  const B = { x: 5.8, zMin: -3.1, zMax: 3.3 };
  const flies: Fly[] = [];
  const cTrail = new Color("#e6ff8a"), cGold = new Color("#ffcf5a");

  const pickGoal = (f: Fly) => {
    const high = Math.random() < 0.4;
    f.goal.set((Math.random() * 2 - 1) * B.x, high ? 2.2 + Math.random() * 0.8 : 0.7 + Math.random() * 0.7, B.zMin + Math.random() * (B.zMax - B.zMin));
  };
  const spawn = (f: Fly, gold: boolean) => {
    f.gold = gold;
    f.core.material = gold ? goldMat : coreMat;
    f.halo.material = gold ? haloGold : haloMat;
    f.g.scale.setScalar(gold ? 1.5 : 1);
    const side = Math.random() < 0.5 ? -1 : 1;
    f.p.set(side * (7 + Math.random() * 2), 1 + Math.random() * 2, -4 + Math.random() * 6);
    f.v.set(0, 0, 0);
    pickGoal(f);
    f.state = "in"; f.t = 0; f.life = gold ? 10 : 999;
    f.g.visible = true;
  };
  const make = () => {
    const g = new Group();
    const core = new Mesh(flyGeo, coreMat);
    const halo = new Sprite(haloMat);
    halo.scale.setScalar(0.9);
    g.add(halo, core);
    root.add(g);
    const f: Fly = { g, core, halo, p: new Vector3(), v: new Vector3(), goal: new Vector3(), gold: false, state: "gone", t: 0, life: 0, from: new Vector3(), seed: Math.random() * 10 };
    flies.push(f);
    return f;
  };
  for (let i = 0; i < 6; i++) { const f = make(); spawn(f, false); f.p.set((Math.random() * 2 - 1) * B.x, 0.8 + Math.random() * 1.6, B.zMin + Math.random() * 5); }
  const gold = make(); gold.g.visible = false;
  let goldTimer = 9;
  const tmp = new Vector3(), bodyV = new Vector3(), jarTop = new Vector3(JAR.x, 1.4, JAR.z);

  const addToJar = () => {
    inJar++;
    if (jarDots.length < 40) {
      const m = new Mesh(dotGeo, dotMat);
      const a = Math.random() * Math.PI * 2, rr = Math.random() * 0.3;
      m.position.set(JAR.x + Math.cos(a) * rr, 0.9 + Math.random() * 0.7, JAR.z + Math.sin(a) * rr);
      m.userData.seed = Math.random() * 10;
      jarDots.push(m); root.add(m);
    }
  };

  const catchFly = (ctx: GameCtx, f: Fly) => {
    f.state = "caught"; f.t = 0; f.from.copy(f.p);
    const n = f.gold ? 3 : 1;
    ctx.score(n, f.gold ? "+3 金色萤火！" : "+1", { x: f.p.x, y: f.p.y + 0.4, z: f.p.z }, f.gold ? "great" : "good");
    ctx.sparks?.burst(f.p.x, f.p.y, f.p.z, f.gold ? 28 : 14, f.gold ? cGold : cTrail, 2.2, 36);
    ctx.dada.cheer();
    if (f.gold) { ctx.confetti?.burst(f.p.x, f.p.y, f.p.z, 30, 4); ctx.say("抓到金色萤火！", "great"); }
  };

  return {
    root,
    bounds: { x: 5.8, zMin: -3.0, zMax: 3.3 },
    view: { elev: 26, lookY: 0.9, spanX: 7.6, spanZ: 5.0 },
    motes: { mode: "rise", n: 50, color: dark ? "#b5ff9a" : "#fff3b0" },
    light: dark
      ? { sun: "#9fb8ff", sunI: 0.7, hemiSky: "#5a7aa8", hemiGround: "#203528", hemiI: 0.45, exposure: 1.05 }
      : { sun: "#fff0cc", sunI: 1.7, hemiSky: "#e8fff0", hemiGround: "#8fb48a", hemiI: 0.62, exposure: 1.0 },
    spawn: { x: 0, z: 1 },
    down(ctx, hit) {
      // 点击：朝落点起跳（比平时跳得更高，用来够高处的萤火）
      if (hit) { ctx.target.x = Math.max(-B.x, Math.min(B.x, hit.x)); ctx.target.z = Math.max(B.zMin, Math.min(B.zMax, hit.z)); }
      ctx.dada.jump(1.45, 0.7);
      ctx.ripple(ctx.pos.x, ctx.pos.z);
      return true;
    },
    update(dt, ctx) {
      const t = ctx.time;
      const body = bodyV.set(ctx.pos.x, ctx.dada.root.position.y + DADA_H * 0.5, ctx.pos.z);
      let nearest: Fly | null = null, nd = 1e9;

      goldTimer -= dt;
      if (goldTimer <= 0 && gold.state === "gone") { spawn(gold, true); goldTimer = 16 + Math.random() * 8; ctx.say("金色萤火出现了！", "info"); }

      for (const f of flies) {
        if (f.state === "gone") {
          // 普通萤火被抓后过一会儿从林边重新飞进来
          if (f !== gold) { f.t -= dt; if (f.t <= 0) spawn(f, false); }
          continue;
        }
        f.t += dt;
        if (f.state === "caught") {
          // 飞进罐子：抛物线
          const k = Math.min(1, f.t / 0.9);
          const e = 1 - Math.pow(1 - k, 2);
          f.p.lerpVectors(f.from, jarTop, e);
          f.p.y += Math.sin(k * Math.PI) * 1.6;
          f.g.position.copy(f.p);
          f.g.scale.setScalar((f.gold ? 1.5 : 1) * (1 - k * 0.6));
          if (k >= 1) {
            addToJar(); if (f.gold) { addToJar(); addToJar(); }
            ctx.sparks?.burst(JAR.x, 1.5, JAR.z, 8, cTrail, 1.2, 26);
            f.state = "gone"; f.g.visible = false; f.t = 1.1;
          }
          continue;
        }
        if (!f.g.visible) continue;
        // 漫游 + 躲闪
        f.life -= dt;
        const toGoal = tmp.copy(f.goal).sub(f.p);
        if (toGoal.length() < 0.4 || Math.random() < dt * 0.25) pickGoal(f);
        const steer = toGoal.normalize().multiplyScalar(f.gold ? 2.0 : 1.3);
        const fx = f.p.x - ctx.pos.x, fz = f.p.z - ctx.pos.z;
        const hd = Math.hypot(fx, fz);
        if (hd < 2.0 && f.state === "free") {
          const flee = (2.0 - hd) / 2.0 * (f.gold ? 3.4 : 2.3);
          steer.x += (fx / (hd || 1)) * flee; steer.z += (fz / (hd || 1)) * flee;
        }
        if (f.gold && f.life <= 0) { steer.set(f.p.x > 0 ? 3 : -3, 0.8, -1); }
        f.v.lerp(steer, Math.min(1, dt * 2.2));
        f.p.addScaledVector(f.v, dt);
        // 有点抖动的飞行
        f.p.y += Math.sin(t * 6 + f.seed) * dt * 0.4;
        if (f.state === "in") { if (Math.abs(f.p.x) < B.x + 0.5) f.state = "free"; }
        else if (!(f.gold && f.life <= 0)) {
          f.p.x = Math.max(-B.x - 0.4, Math.min(B.x + 0.4, f.p.x));
          f.p.z = Math.max(B.zMin - 0.3, Math.min(B.zMax + 0.3, f.p.z));
        }
        f.p.y = Math.max(0.5, Math.min(3.1, f.p.y));
        if (f.gold && f.life <= 0 && Math.abs(f.p.x) > 10) { f.state = "gone"; f.g.visible = false; continue; }
        f.g.position.copy(f.p);
        const pulse = 0.75 + Math.sin(t * 5 + f.seed) * 0.25;
        f.halo.material.opacity = (f.gold ? 1 : 0.85) * pulse;
        f.halo.scale.setScalar((f.gold ? 1.3 : 0.9) * (0.85 + pulse * 0.3));
        if (ctx.sparks && Math.random() < dt * 14) ctx.sparks.emit(f.p.x, f.p.y, f.p.z, 0, -0.05, 0, 0.5, f.gold ? 26 : 16, f.gold ? cGold : cTrail, 0);

        const dist = Math.hypot(f.p.x - body.x, f.p.y - body.y, f.p.z - body.z);
        if (dist < nd) { nd = dist; nearest = f; }
        if (f.state === "free" && dist < 1.08) catchFly(ctx, f);
      }
      // 嗒嗒盯着最近的萤火
      if (nearest && nd < 4.5 && !ctx.pointer.has) ctx.dada.lookAt(nearest.p.x, nearest.p.y, nearest.p.z);

      // 罐子：越满越亮，里面的光点慢慢浮动
      jarLight.intensity = Math.min(6, inJar * 0.35) * (0.85 + Math.sin(t * 3) * 0.15);
      if (!ctx.reduce) jarDots.forEach((m) => { m.position.y += Math.sin(t * 2 + m.userData.seed) * dt * 0.08; });

      if (!ctx.reduce) {
        shafts.forEach((m, i) => { (m.material as MeshBasicMaterial).opacity = (dark ? 0.1 : 0.24) * (0.75 + Math.sin(t * 0.5 + i * 1.7) * 0.25); });
        glowCaps.forEach((g, i) => { g.scale.y = g.scale.x * (1 + Math.sin(t * 2 + i) * 0.03); });
      }
    },
    reset() {
      flies.forEach((f) => { if (f !== gold) spawn(f, false); });
      gold.state = "gone"; gold.g.visible = false; goldTimer = 9;
      jarDots.forEach((m) => root.remove(m)); jarDots.length = 0; inJar = 0;
    },
    dispose() { bag.dispose(); },
  };
}
