/** 樱时世界 · 接落樱（plan-playground-v2）。
 * 场景：春日庭院——后方一株大樱花树（弯折树干 + 枝杈 + 团簇花冠 + 点点小花）、两侧小樱树、石灯笼、朱红鸟居、
 *       锦鲤池（荷叶、两尾锦鲤）、踏石小径、木长椅、挂着纸灯笼的绳串，四处飘落的花瓣。
 * 玩法：发光的花瓣从树上飘下，地上的影子标出它正落向哪里；让嗒嗒站到影子上接住，点击起跳能更早接到。
 *       连续接住会一片片叠在嗒嗒头上并提高倍数；漏接一片，嗒嗒会把头上的花瓣抖落。 */
import {
  AdditiveBlending, BoxGeometry, Color, ConeGeometry, CylinderGeometry, Group, IcosahedronGeometry,
  InstancedMesh, Line, LineBasicMaterial, BufferGeometry, Matrix4, Mesh, MeshBasicMaterial, MeshPhysicalMaterial, Object3D,
  RingGeometry, Shape, ShapeGeometry, SphereGeometry, Sprite, SpriteMaterial, Vector3, CircleGeometry,
} from "three";
import { DADA_H } from "../dada3d";
import { Bag, bladeGeometry, lumpy, rng, scatter, shadow, windify } from "../kit";
import { rock, roundTree, trunk } from "../props";
import type { Game, GameCtx, GameDeps } from "./types";

/** 花瓣：圆润、顶端有个小缺口 */
function petalShape(s = 1) {
  const p = new Shape();
  p.moveTo(0, -0.16 * s);
  p.bezierCurveTo(0.16 * s, -0.08 * s, 0.15 * s, 0.12 * s, 0.05 * s, 0.17 * s);
  p.lineTo(0, 0.12 * s);
  p.lineTo(-0.05 * s, 0.17 * s);
  p.bezierCurveTo(-0.15 * s, 0.12 * s, -0.16 * s, -0.08 * s, 0, -0.16 * s);
  return p;
}

interface Drop { g: Group; mark: Mesh; p: Vector3; x0: number; z0: number; vy: number; ph: number; big: boolean; state: "fall" | "land" | "off"; t: number }

export function createSakura(d: GameDeps): Game {
  const bag = new Bag();
  const root = new Group();
  const r = rng("sakura-garden");
  const dark = d.dark;

  const pinkA = bag.std(dark ? "#d886a6" : "#ffc4d6", { rough: 0.85 });
  const pinkB = bag.std(dark ? "#c76f93" : "#ffadc6", { rough: 0.85 });
  const pinkC = bag.std(dark ? "#e8a3bd" : "#ffdbe6", { rough: 0.85 });
  const bark = bag.std(dark ? "#4a3434" : "#6e4e48", { rough: 0.95 });

  // ── 大樱花树 ──
  const big = new Group();
  const tk = trunk(bag, bark, 2.6, 0.5, 0.26, 0.5, r);
  big.add(tk.group);
  const blossomPts: Vector3[] = [];
  const branchTips: Vector3[] = [];
  for (let k = 0; k < 5; k++) {
    const a = (k / 5) * Math.PI * 2 + 0.4;
    const len = 1.6 + r() * 0.8;
    const br = new Mesh(bag.geo(new CylinderGeometry(0.08, 0.16, len, 8)), bark);
    const dir = new Vector3(Math.cos(a), 0.9 + r() * 0.5, Math.sin(a) * 0.6).normalize();
    const mid = tk.top.clone().addScaledVector(dir, len / 2);
    br.position.copy(mid);
    br.quaternion.setFromUnitVectors(new Vector3(0, 1, 0), dir);
    big.add(br);
    branchTips.push(tk.top.clone().addScaledVector(dir, len));
  }
  branchTips.push(tk.top.clone().add(new Vector3(0, 1.2, 0)));
  const pinks = [pinkA, pinkB, pinkC];
  branchTips.forEach((tip, i) => {
    for (let k = 0; k < 4; k++) {
      const m = new Mesh(bag.geo(lumpy(0.9 + r() * 0.4, 2, 0.35, 1.5, i * 7 + k)), pinks[(i + k) % 3]);
      m.position.copy(tip).add(new Vector3((r() - 0.5) * 1.4, (r() - 0.3) * 0.8, (r() - 0.5) * 1.0));
      m.scale.set(1.15, 0.85, 1);
      big.add(m);
      for (let j = 0; j < 22; j++) {
        const v = new Vector3(r() - 0.5, r() - 0.3, r() - 0.5).normalize().multiplyScalar(0.95 + r() * 0.35);
        blossomPts.push(m.position.clone().add(v.multiply(m.scale)));
      }
    }
  });
  // 点点小花（实例化）
  const flowerGeo = bag.geo(new IcosahedronGeometry(0.07, 0));
  const flowerMat = bag.std(dark ? "#ffe0ec" : "#ffffff", { rough: 0.6, emissive: "#ffd0e0", ei: dark ? 0.3 : 0.05 });
  const flowers = new InstancedMesh(flowerGeo, flowerMat, blossomPts.length);
  const fm = new Matrix4();
  blossomPts.forEach((p, i) => { fm.makeScale(1, 0.5 + r() * 0.5, 1).setPosition(p); flowers.setMatrixAt(i, fm); });
  big.add(flowers);
  big.position.set(0.6, 0, -6.6);
  big.scale.setScalar(1.15);
  root.add(shadow(big));
  flowers.castShadow = false;

  // 两侧小樱树
  for (const [x, z, s] of [[-9.2, -3.6, 0.95], [9.6, -2.4, 1.0], [-11, 1.6, 0.85], [-4.6, -8.8, 1.1], [6.2, -8.4, 1.05]] as const) {
    const t = roundTree(bag, pinks, bark, r, s, 5);
    t.position.set(x, 0, z);
    root.add(t);
  }

  // ── 石灯笼 ──
  const stone = bag.std(dark ? "#6b6f78" : "#c3c1bb", { rough: 0.95 });
  const toro = new Group();
  const parts: Array<[Mesh, number]> = [
    [new Mesh(bag.geo(new CylinderGeometry(0.5, 0.58, 0.22, 6)), stone), 0.11],
    [new Mesh(bag.geo(new CylinderGeometry(0.17, 0.2, 1.1, 10)), stone), 0.77],
    [new Mesh(bag.geo(new CylinderGeometry(0.46, 0.38, 0.16, 6)), stone), 1.4],
    [new Mesh(bag.geo(new BoxGeometry(0.56, 0.48, 0.56)), stone), 1.72],
    [new Mesh(bag.geo(new ConeGeometry(0.78, 0.42, 6)), stone), 2.17],
    [new Mesh(bag.geo(new SphereGeometry(0.12, 12, 8)), stone), 2.44],
  ];
  parts.forEach(([m, y]) => { m.position.y = y; toro.add(m); });
  const toroWin = new Mesh(bag.geo(new BoxGeometry(0.3, 0.26, 0.6)), bag.std("#ffe2a8", { emissive: "#ffc861", ei: dark ? 2.2 : 0.7 }));
  toroWin.position.y = 1.72;
  toro.add(toroWin);
  toro.position.set(-6.4, 0, -2.6); toro.rotation.y = 0.4;
  root.add(shadow(toro));
  const toroLight = new Sprite(bag.mat(new SpriteMaterial({ map: d.glowTex, color: "#ffc861", blending: AdditiveBlending, transparent: true, depthWrite: false, opacity: dark ? 0.8 : 0.3 })));
  toroLight.scale.setScalar(1.8); toroLight.position.set(-6.4, 1.72, -2.6);
  root.add(toroLight);

  // ── 鸟居 ──
  const vermilion = bag.std("#e0523f", { rough: 0.55 });
  const blackTop = bag.std("#2b2a30", { rough: 0.5 });
  const torii = new Group();
  for (const s of [-1, 1]) {
    const p = new Mesh(bag.geo(new CylinderGeometry(0.15, 0.18, 3.2, 14)), vermilion); p.position.set(s * 1.25, 1.6, 0);
    const foot = new Mesh(bag.geo(new CylinderGeometry(0.22, 0.24, 0.25, 14)), blackTop); foot.position.set(s * 1.25, 0.12, 0);
    torii.add(p, foot);
  }
  const nuki = new Mesh(bag.geo(new BoxGeometry(3.2, 0.16, 0.18)), vermilion); nuki.position.y = 2.55;
  const kasagi = new Mesh(bag.geo(new BoxGeometry(3.8, 0.22, 0.32)), vermilion); kasagi.position.y = 3.22;
  const kTop = new Mesh(bag.geo(new BoxGeometry(4.1, 0.12, 0.38)), blackTop); kTop.position.y = 3.38;
  for (const s of [-1, 1]) { const tip = new Mesh(bag.geo(new BoxGeometry(0.5, 0.12, 0.38)), blackTop); tip.position.set(s * 2.2, 3.44, 0); tip.rotation.z = s * 0.25; torii.add(tip); }
  const plaque = new Mesh(bag.geo(new BoxGeometry(0.34, 0.5, 0.06)), blackTop); plaque.position.set(0, 2.88, 0.1);
  torii.add(nuki, kasagi, kTop, plaque);
  torii.position.set(7.6, 0, -5.4); torii.rotation.y = -0.4;
  root.add(shadow(torii));

  // ── 锦鲤池 ──
  const pond = new Group();
  const water = new Mesh(bag.geo(new CircleGeometry(1, 40)), bag.mat(new MeshPhysicalMaterial({ color: dark ? "#2f5c7a" : "#7cc8e6", roughness: 0.05, clearcoat: 1, transparent: true, opacity: 0.85 })));
  water.rotation.x = -Math.PI / 2; water.scale.set(2.3, 1.5, 1); water.position.y = 0.03;
  water.receiveShadow = true;
  pond.add(water);
  for (let k = 0; k < 14; k++) {
    const a = (k / 14) * Math.PI * 2;
    const s = rock(bag, stone, r, 0.28 + r() * 0.12);
    s.position.set(Math.cos(a) * 2.35, -0.05, Math.sin(a) * 1.6);
    pond.add(s);
  }
  const padMat = bag.std(dark ? "#3c7a4e" : "#6cbf6a", { rough: 0.6, side: true });
  for (const [x, z, s] of [[-0.9, 0.4, 0.34], [0.8, -0.5, 0.28], [1.2, 0.6, 0.22]] as const) {
    const pd = new Mesh(bag.geo(new CircleGeometry(1, 20, 0.3, Math.PI * 2 - 0.3)), padMat);
    pd.rotation.x = -Math.PI / 2; pd.scale.setScalar(s); pd.position.set(x, 0.05, z);
    pond.add(pd);
  }
  const lotus = new Mesh(bag.geo(lumpy(0.12, 1, 0.5, 6, 3)), bag.std("#ffd6e4"));
  lotus.position.set(-0.85, 0.12, 0.38);
  pond.add(lotus);
  const koiMat = [bag.std("#ff7a3a", { rough: 0.4 }), bag.std("#fff6ee", { rough: 0.4 })];
  const kois = [0, 1].map((i) => {
    const g = new Group();
    const body = new Mesh(bag.geo(new SphereGeometry(0.12, 14, 10)), koiMat[i]);
    body.scale.set(0.8, 0.5, 2.2);
    const patch = new Mesh(bag.geo(new SphereGeometry(0.1, 12, 8)), koiMat[1 - i]);
    patch.scale.set(0.75, 0.45, 1); patch.position.set(0, 0.02, 0.08);
    const tail = new Mesh(bag.geo(new ConeGeometry(0.1, 0.2, 3)), koiMat[i]);
    tail.rotation.x = Math.PI / 2; tail.scale.set(1, 1, 0.3); tail.position.z = -0.32;
    g.add(body, patch, tail);
    pond.add(g);
    return { g, tail, a: i * Math.PI };
  });
  pond.position.set(-6.2, 0, -6.4);
  root.add(shadow(pond, true, true));
  water.castShadow = false;

  // 踏石
  for (let i = 0; i < 6; i++) {
    const s = new Mesh(bag.geo(lumpy(1, 2, 0.2, 1.4, i * 3)), stone);
    s.scale.set(0.42, 0.06, 0.34);
    s.position.set(-7.6 + i * 0.25 + Math.sin(i * 1.4) * 0.3, 0.03, 3.4 - i * 1.25);
    s.rotation.y = r() * 3;
    root.add(shadow(s, false, true));
  }

  // 木长椅
  const woodMat = bag.std(dark ? "#7a5a40" : "#b6845a", { rough: 0.85 });
  const bench = new Group();
  for (let i = 0; i < 3; i++) { const s = new Mesh(bag.geo(new BoxGeometry(2.2, 0.08, 0.18)), woodMat); s.position.set(0, 0.6, (i - 1) * 0.21); bench.add(s); }
  for (let i = 0; i < 2; i++) { const s = new Mesh(bag.geo(new BoxGeometry(2.2, 0.08, 0.14)), woodMat); s.position.set(0, 0.95 + i * 0.2, -0.32); s.rotation.x = -0.12; bench.add(s); }
  for (const sx of [-0.95, 0.95]) for (const sz of [-0.22, 0.22]) { const l = new Mesh(bag.geo(new BoxGeometry(0.1, 0.6, 0.1)), woodMat); l.position.set(sx, 0.3, sz); bench.add(l); }
  bench.position.set(7.4, 0, 2.4); bench.rotation.y = -1.25;
  root.add(shadow(bench));

  // 纸灯笼绳串：树 → 鸟居
  const ropeA = new Vector3(-3.4, 3.9, -5.0), ropeB = new Vector3(6.6, 3.0, -5.2);
  const ropePts = Array.from({ length: 24 }, (_, i) => { const k = i / 23; return new Vector3().lerpVectors(ropeA, ropeB, k).setY(ropeA.y + (ropeB.y - ropeA.y) * k - Math.sin(k * Math.PI) * 0.9); });
  root.add(new Line(bag.geo(new BufferGeometry().setFromPoints(ropePts)), bag.mat(new LineBasicMaterial({ color: dark ? "#9a8a80" : "#6e5a50" }))));
  const lanCols = ["#ff9ec0", "#fff2c6", "#ffb38a"];
  const lanterns = [3, 7, 11, 15, 19].map((idx, i) => {
    const g = new Group();
    const m = new Mesh(bag.geo(new SphereGeometry(0.24, 16, 12)), bag.std(lanCols[i % 3], { emissive: lanCols[i % 3], ei: dark ? 1.2 : 0.35, rough: 0.7 }));
    m.scale.set(1, 1.2, 1); m.position.y = -0.36;
    const capT = new Mesh(bag.geo(new CylinderGeometry(0.1, 0.12, 0.06, 10)), blackTop); capT.position.y = -0.06;
    g.add(m, capT);
    g.position.copy(ropePts[idx]);
    root.add(g);
    return { g, seed: i };
  });

  // 草
  const grassMat = windify(bag.std(dark ? "#3c6a4a" : "#86c48a", { rough: 1, side: true }), 0.08, 0.38);
  root.add(scatter(bag.geo(bladeGeometry(0.36, 0.06)), grassMat, 900, r, { x0: -13, x1: 13, z0: -9, z1: 6 },
    (x, z) => !(Math.abs(x) < 6 && z > -3 && z < 3.6) && Math.hypot(x + 6.2, z + 6.4) > 2.8, [0.7, 1.3]));

  // ── 飘落花瓣（氛围） ──
  const petalGeo = bag.geo(new ShapeGeometry(petalShape(1), 6));
  const petalMat = bag.std(dark ? "#f2a8c4" : "#ffc2d4", { rough: 0.7, side: true });
  const N = d.reduce ? 0 : 110;
  const ambient = new InstancedMesh(petalGeo, petalMat, Math.max(1, N));
  ambient.count = N;
  const amb = Array.from({ length: N }, () => ({ x: (Math.random() * 2 - 1) * 11, y: Math.random() * 7, z: -8 + Math.random() * 12, s: Math.random() * 6, v: 0.4 + Math.random() * 0.5, rx: Math.random() * 6, ry: Math.random() * 6 }));
  const eo = new Object3D();
  root.add(ambient);

  // ── 可接的花瓣 ──
  const dropMat = bag.std("#ff9ec0", { rough: 0.5, side: true, emissive: "#ff7aa8", ei: 0.45 });
  const clusterMat = bag.std("#ffffff", { rough: 0.5, emissive: "#ffc0d8", ei: 0.5 });
  const markMat = bag.mat(new MeshBasicMaterial({ color: dark ? "#ffd0e0" : "#c2577a", transparent: true, opacity: 0, depthWrite: false }));
  const ringGeo = bag.geo(new RingGeometry(0.42, 0.5, 40));
  const dotGeo = bag.geo(new CircleGeometry(0.2, 24));
  const haloMat = bag.mat(new SpriteMaterial({ map: d.glowTex, color: "#ffb3cf", blending: AdditiveBlending, transparent: true, depthWrite: false, opacity: 0.7 }));
  const drops: Drop[] = Array.from({ length: 5 }, () => {
    const g = new Group();
    const m = new Mesh(bag.geo(new ShapeGeometry(petalShape(1.9), 10)), dropMat);
    m.castShadow = true;
    const h = new Sprite(haloMat); h.scale.setScalar(1.1);
    g.add(h, m);
    g.visible = false;
    g.userData.petalGeo = m.geometry;
    root.add(g);
    const mark = new Mesh(ringGeo, markMat.clone());
    bag.mat(mark.material as MeshBasicMaterial);
    const dot = new Mesh(dotGeo, mark.material);
    mark.add(dot);
    mark.rotation.x = -Math.PI / 2; mark.position.y = 0.03; mark.visible = false;
    root.add(mark);
    return { g, mark, p: new Vector3(), x0: 0, z0: 0, vy: 1.4, ph: 0, big: false, state: "off" as const, t: 0 };
  });
  const clusterGeo = bag.geo(lumpy(0.28, 1, 0.5, 4, 5));

  // 头顶叠花瓣
  const stackGeo = bag.geo(new ShapeGeometry(petalShape(1.2), 6));
  const stack: Mesh[] = [];

  let combo = 0, spawnT = 1.2;
  const cPink = new Color("#ffb3cf"), cWhite = new Color("#fff0f6");

  const spawnDrop = () => {
    const dr = drops.find((x) => x.state === "off");
    if (!dr) return;
    dr.big = Math.random() < 0.15;
    const m = dr.g.children[1] as Mesh;
    m.geometry = dr.big ? clusterGeo : dr.g.userData.petalGeo;
    m.material = dr.big ? clusterMat : dropMat;
    dr.x0 = (Math.random() * 2 - 1) * 5.2; dr.z0 = -2.6 + Math.random() * 5.6;
    dr.p.set(dr.x0, 6.4, dr.z0);
    dr.vy = dr.big ? 2.0 : 1.25 + Math.random() * 0.45 + Math.min(0.5, combo * 0.03);
    dr.ph = Math.random() * 6; dr.state = "fall"; dr.t = 0;
    dr.g.visible = true; dr.mark.visible = true;
  };

  const dropStack = (ctx: GameCtx) => {
    if (!stack.length) return;
    ctx.dada.shake();
    stack.forEach((m) => {
      m.getWorldPosition(eo.position);
      ctx.sparks?.emit(eo.position.x, eo.position.y, eo.position.z, (Math.random() - 0.5) * 2, 1.5, (Math.random() - 0.5) * 2, 0.9, 30, cPink, 5);
      m.removeFromParent();
    });
    stack.length = 0;
  };

  return {
    root,
    bounds: { x: 5.6, zMin: -2.8, zMax: 3.2 },
    view: { elev: 22, lookY: 1.7, lookZ: -0.8, spanX: 7.6, spanZ: 5.0 },
    motes: { mode: "float", n: 24, color: "#fff0f6" },
    light: dark
      ? { sun: "#c9b6ff", sunI: 0.9, hemiSky: "#8a7ab0", hemiGround: "#3a2e3a", hemiI: 0.52, exposure: 1.05 }
      : { sun: "#fff0f0", sunI: 1.7, hemiSky: "#fff0f6", hemiGround: "#b9d6a6", hemiI: 0.65, exposure: 1.0 },
    spawn: { x: 0, z: 1.2 },
    update(dt, ctx) {
      const t = ctx.time;
      spawnT -= dt;
      const live = drops.filter((x) => x.state === "fall").length;
      if (spawnT <= 0 && live < 3 + Math.min(1, Math.floor(combo / 6))) { spawnDrop(); spawnT = Math.max(0.7, 2.0 - combo * 0.07) * (0.7 + Math.random() * 0.6); }

      const headY = ctx.dada.root.position.y + DADA_H + 0.05;
      for (const dr of drops) {
        if (dr.state === "off") continue;
        dr.t += dt;
        if (dr.state === "fall") {
          dr.p.y -= dr.vy * dt;
          dr.p.x = dr.x0 + Math.sin(dr.t * 1.4 + dr.ph) * (dr.big ? 0.2 : 0.55);
          dr.p.z = dr.z0 + Math.cos(dr.t * 1.1 + dr.ph) * 0.25;
          dr.g.position.copy(dr.p);
          const m = dr.g.children[1];
          m.rotation.set(Math.sin(dr.t * 2.2 + dr.ph) * 0.9, dr.t * 1.3, Math.cos(dr.t * 1.7) * 0.6);
          // 落点影子：越近越小越实
          const k = 1 - Math.min(1, dr.p.y / 6.4);
          dr.mark.position.set(dr.p.x, 0.03, dr.p.z);
          dr.mark.scale.setScalar(1.6 - k * 0.8);
          (dr.mark.material as MeshBasicMaterial).opacity = 0.18 + k * 0.5;
          const hd = Math.hypot(dr.p.x - ctx.pos.x, dr.p.z - ctx.pos.z);
          if (dr.p.y <= headY + 0.25 && dr.p.y >= headY - 0.9 && hd < 0.8) {
            // 接住！
            combo++;
            const mult = 1 + Math.floor(combo / 4);
            const n = (dr.big ? 3 : 1) * mult;
            ctx.score(n, mult > 1 ? `+${n} 连击 ×${mult}` : `+${n}`, { x: ctx.pos.x, y: headY + 0.6, z: ctx.pos.z }, dr.big || mult > 1 ? "great" : "good");
            ctx.sparks?.burst(dr.p.x, dr.p.y, dr.p.z, dr.big ? 24 : 12, dr.big ? cWhite : cPink, 2.0, 30);
            ctx.dada.cheer(); ctx.dada.prod();
            if (combo % 4 === 0) ctx.say(`连接 ${combo} 片！倍数 ×${mult}`, "great");
            if (combo % 10 === 0) ctx.confetti?.burst(ctx.pos.x, headY + 0.5, ctx.pos.z, 36, 4);
            if (stack.length < 7) {
              const s = new Mesh(stackGeo, dr.big ? clusterMat : dropMat);
              const a = stack.length * 2.4;
              s.position.set(Math.cos(a) * 0.22 * Math.min(1, stack.length), 0.02 + stack.length * 0.03, Math.sin(a) * 0.18 * Math.min(1, stack.length));
              s.rotation.set(-Math.PI / 2 + 0.3, 0, a);
              ctx.dada.crown.add(s);
              stack.push(s);
            }
            dr.state = "off"; dr.g.visible = false; dr.mark.visible = false;
          } else if (dr.p.y <= 0.05) {
            dr.state = "land"; dr.t = 0; dr.p.y = 0.04;
            dr.mark.visible = false;
            ctx.ripple(dr.p.x, dr.p.z);
            if (combo >= 2) { ctx.say("漏接了一片，头上的花瓣被抖落", "bad"); ctx.dada.emote("wow", 0.8); }
            combo = 0;
            dropStack(ctx);
          }
        } else if (dr.state === "land") {
          dr.g.position.set(dr.p.x, 0.04, dr.p.z);
          dr.g.children[1].rotation.set(-Math.PI / 2, 0, dr.ph);
          dr.g.scale.setScalar(Math.max(0.01, 1 - dr.t));
          if (dr.t >= 1) { dr.state = "off"; dr.g.visible = false; dr.g.scale.setScalar(1); }
        }
      }

      // 氛围花瓣
      for (let i = 0; i < N; i++) {
        const a = amb[i];
        a.y -= a.v * dt; a.x += Math.sin(t * 0.8 + a.s) * dt * 0.4 + dt * 0.15; a.rx += dt * 1.5; a.ry += dt;
        if (a.y < 0) { a.y = 6 + Math.random() * 2; a.x = (Math.random() * 2 - 1) * 11; }
        eo.position.set(a.x, a.y, a.z);
        eo.rotation.set(a.rx, a.ry, Math.sin(t + a.s));
        eo.scale.setScalar(0.6);
        eo.updateMatrix();
        ambient.setMatrixAt(i, eo.matrix);
      }
      if (N) ambient.instanceMatrix.needsUpdate = true;

      if (!ctx.reduce) {
        kois.forEach((k, i) => {
          k.a += dt * (0.35 + i * 0.1);
          const s = i ? -1 : 1;
          k.g.position.set(Math.cos(k.a * s) * 1.3, 0.0, Math.sin(k.a * s) * 0.8);
          k.g.rotation.y = Math.atan2(-Math.sin(k.a * s) * 1.3 * s, Math.cos(k.a * s) * 0.8 * s);
          k.tail.rotation.y = Math.sin(t * 6 + i) * 0.4;
        });
        lanterns.forEach((l) => { l.g.rotation.z = Math.sin(t * 1.1 + l.seed) * 0.08; l.g.rotation.x = Math.cos(t * 0.9 + l.seed) * 0.05; });
      }
    },
    reset(ctx) {
      drops.forEach((dr) => { dr.state = "off"; dr.g.visible = false; dr.mark.visible = false; });
      combo = 0; spawnT = 1;
      stack.forEach((m) => m.removeFromParent()); stack.length = 0;
      ctx.dada.pose("none");
    },
    dispose() { stack.forEach((m) => m.removeFromParent()); bag.dispose(); },
  };
}
