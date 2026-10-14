/** 煦风世界 · 风筝高飞（plan-playground-v2）。
 * 场景：草坡牧场——远处层叠的丘陵、转动的风车、木栅栏、雏菊与郁金香花丛、随风起伏的草叶、
 *       扑翅的蝴蝶、掠过的纸飞机、天边的热气球与云、一道道风线。
 * 玩法：嗒嗒牵着风筝跑，风筝拖在身后的空中；操控嗒嗒的位置，让风筝去碰天上的星星。
 *       点击借一阵风，风筝猛地升高，能够到更高处的彩虹星。 */
import {
  AdditiveBlending, BufferGeometry, CanvasTexture, Color, ConeGeometry, CylinderGeometry, DoubleSide, ExtrudeGeometry,
  Float32BufferAttribute, Group, InstancedMesh, LatheGeometry, Line, LineBasicMaterial, Matrix4, Mesh, MeshBasicMaterial,
  MeshStandardMaterial, Object3D, PlaneGeometry, Shape, SphereGeometry, Sprite, SpriteMaterial, Vector2, Vector3,
  BoxGeometry, type Material,
} from "three";
import { Bag, bladeGeometry, lumpy, rng, scatter, shadow, windify } from "../kit";
import { cloud, fence } from "../props";
import type { Game, GameDeps } from "./types";

function starShape(R = 0.32, r = 0.14) {
  const s = new Shape();
  for (let k = 0; k < 10; k++) {
    const a = Math.PI / 2 + (k * Math.PI) / 5, rr = k % 2 ? r : R;
    if (k === 0) s.moveTo(Math.cos(a) * rr, Math.sin(a) * rr); else s.lineTo(Math.cos(a) * rr, Math.sin(a) * rr);
  }
  s.closePath();
  return s;
}

/** 菱形风筝面：四块三角，两两同色 */
function kiteGeometry(c1: string, c2: string) {
  const T = [0, 0.78], R = [0.52, 0.12], Bt = [0, -0.7], L = [-0.52, 0.12], C = [0, 0.12];
  const tris = [[T, R, C, c1], [R, Bt, C, c2], [Bt, L, C, c1], [L, T, C, c2]] as const;
  const pos: number[] = [], col: number[] = [];
  const c = new Color();
  for (const [a, b, m, cc] of tris) {
    pos.push(a[0], a[1], 0, b[0], b[1], 0, m[0], m[1], 0.06);
    c.set(cc);
    for (let i = 0; i < 3; i++) col.push(c.r, c.g, c.b);
  }
  const g = new BufferGeometry();
  g.setAttribute("position", new Float32BufferAttribute(pos, 3));
  g.setAttribute("color", new Float32BufferAttribute(col, 3));
  g.computeVertexNormals();
  return g;
}

/** 蝴蝶翅膀贴图 */
function wingTex(c1: string, c2: string): CanvasTexture {
  const c = document.createElement("canvas");
  c.width = c.height = 128;
  const g = c.getContext("2d")!;
  g.fillStyle = c1;
  g.beginPath(); g.ellipse(70, 46, 52, 38, -0.4, 0, Math.PI * 2); g.fill();
  g.beginPath(); g.ellipse(58, 96, 34, 26, 0.5, 0, Math.PI * 2); g.fill();
  g.fillStyle = c2;
  g.beginPath(); g.arc(84, 40, 12, 0, Math.PI * 2); g.fill();
  g.beginPath(); g.arc(60, 98, 8, 0, Math.PI * 2); g.fill();
  return new CanvasTexture(c);
}

/** 同一组变换矩阵摆多个部件（花茎 / 花瓣 / 花心各一份 InstancedMesh） */
function placeParts(parts: Array<{ geo: BufferGeometry; mat: Material }>, mats: Matrix4[], cast = false) {
  return parts.map(({ geo, mat }) => {
    const im = new InstancedMesh(geo, mat, mats.length);
    mats.forEach((m, i) => im.setMatrixAt(i, m));
    im.castShadow = cast;
    return im;
  });
}

export function createBreeze(d: GameDeps): Game {
  const bag = new Bag();
  const root = new Group();
  const r = rng("breeze-meadow");
  const dark = d.dark;
  const B = { x: 6.0, zMin: -2.4, zMax: 3.2 };

  // ── 远山 ──
  const hillCols = dark ? ["#2f5a4a", "#2a5044", "#24463d"] : ["#a7d79a", "#8fc98a", "#7bbd84"];
  for (let i = 0; i < 9; i++) {
    const m = new Mesh(bag.geo(lumpy(1, 3, 0.12, 0.8, i * 3.1)), bag.std(hillCols[i % 3], { rough: 1 }));
    const w = 6 + r() * 6;
    m.scale.set(w, 2 + r() * 2.2, w * 0.6);
    m.position.set(-24 + i * 6 + (r() - 0.5) * 3, -0.4, -13 - (i % 3) * 4 - r() * 2);
    m.receiveShadow = true;
    root.add(m);
  }

  // ── 风车 ──
  const mill = new Group();
  const towerPts = [new Vector2(0.9, 0), new Vector2(0.78, 1.5), new Vector2(0.62, 3.2), new Vector2(0.55, 3.6), new Vector2(0, 3.62)];
  const tower = new Mesh(bag.geo(new LatheGeometry(towerPts, 24)), bag.std(dark ? "#c9c2b6" : "#f6efe2", { rough: 0.8 }));
  const roof = new Mesh(bag.geo(new ConeGeometry(0.78, 1.0, 24)), bag.std("#e0685a", { rough: 0.6 }));
  roof.position.y = 4.05;
  const door = new Mesh(bag.geo(new BoxGeometry(0.42, 0.7, 0.1)), bag.std("#8a5f43"));
  door.position.set(0, 0.35, 0.86);
  const win = new Mesh(bag.geo(new BoxGeometry(0.3, 0.3, 0.1)), bag.std("#7cc4ff", { emissive: "#ffd79a", ei: dark ? 1.2 : 0 }));
  win.position.set(0, 2.2, 0.68);
  const hub = new Group();
  hub.position.set(0, 3.55, 0.78);
  const sailMat = bag.std(dark ? "#d8d2c6" : "#fffaf0", { rough: 0.85, side: true });
  const sparMat = bag.std("#8a5f43", { rough: 0.9 });
  for (let k = 0; k < 4; k++) {
    const arm = new Group();
    const spar = new Mesh(bag.geo(new BoxGeometry(0.08, 2.6, 0.06)), sparMat);
    spar.position.y = 1.3;
    const sail = new Mesh(bag.geo(new PlaneGeometry(0.62, 2.0)), sailMat);
    sail.position.set(0.36, 1.5, 0.02);
    for (let j = 0; j < 4; j++) { const rib = new Mesh(bag.geo(new BoxGeometry(0.66, 0.035, 0.04)), sparMat); rib.position.set(0.36, 0.6 + j * 0.6, 0.04); arm.add(rib); }
    arm.add(spar, sail);
    arm.rotation.z = (k * Math.PI) / 2;
    hub.add(arm);
  }
  const cap = new Mesh(bag.geo(new SphereGeometry(0.16, 14, 10)), sparMat);
  hub.add(cap);
  mill.add(tower, roof, door, win, hub);
  mill.position.set(8.4, 0, -6.4); mill.rotation.y = -0.45;
  root.add(shadow(mill));

  // ── 栅栏 ──
  const wood = bag.std(dark ? "#8a7058" : "#c79a6b", { rough: 0.9 });
  for (let i = 0; i < 6; i++) {
    const f = fence(bag, wood, 3.6, 4);
    f.position.set(-10 + i * 3.7, 0, -4.6 - Math.sin(i * 0.9) * 0.4);
    f.rotation.y = Math.sin(i * 1.3) * 0.08;
    root.add(f);
  }

  // ── 花丛：雏菊 + 郁金香（同一组矩阵摆花茎 / 花瓣 / 花心） ──
  const flowerArea = (x: number, z: number) => !(Math.abs(x) < 6.2 && z > -2.8 && z < 3.6);
  const mats: Matrix4[] = [], mats2: Matrix4[] = [];
  const e = new Object3D();
  for (let i = 0; i < 260 && mats.length + mats2.length < 160; i++) {
    const x = -12 + r() * 24, z = -8 + r() * 13;
    if (!flowerArea(x, z)) continue;
    e.position.set(x, 0, z); e.rotation.set((r() - 0.5) * 0.3, r() * 6, (r() - 0.5) * 0.3); e.scale.setScalar(0.8 + r() * 0.6);
    e.updateMatrix();
    (r() < 0.6 ? mats : mats2).push(e.matrix.clone());
  }
  const stemGeo = bag.geo(new CylinderGeometry(0.015, 0.02, 0.5, 5).translate(0, 0.25, 0));
  const stemMat = windify(bag.std(dark ? "#3c7a4e" : "#5aa65e"), 0.06, 0.5);
  const petal = bag.geo((() => {
    const g = new BufferGeometry();
    const pos: number[] = [];
    for (let k = 0; k < 10; k++) {
      const a = (k / 10) * Math.PI * 2, a2 = a + 0.25, a3 = a - 0.25;
      pos.push(0, 0.52, 0, Math.cos(a2) * 0.13, 0.53, Math.sin(a2) * 0.13, Math.cos(a3) * 0.13, 0.53, Math.sin(a3) * 0.13);
      pos.push(Math.cos(a2) * 0.13, 0.53, Math.sin(a2) * 0.13, Math.cos(a) * 0.16, 0.54, Math.sin(a) * 0.16, Math.cos(a3) * 0.13, 0.53, Math.sin(a3) * 0.13);
    }
    g.setAttribute("position", new Float32BufferAttribute(pos, 3));
    g.computeVertexNormals();
    return g;
  })());
  const petalMat = windify(bag.std("#ffffff", { rough: 0.7, side: true }), 0.06, 0.5);
  const centerGeo = bag.geo(new SphereGeometry(0.05, 10, 6).scale(1, 0.5, 1).translate(0, 0.54, 0));
  const centerMat = windify(bag.std("#ffcc3d", { rough: 0.6 }), 0.06, 0.5);
  placeParts([{ geo: stemGeo, mat: stemMat }, { geo: petal, mat: petalMat }, { geo: centerGeo, mat: centerMat }], mats).forEach((m) => root.add(m));
  const tulipGeo = bag.geo(new LatheGeometry([new Vector2(0, 0), new Vector2(0.07, 0.02), new Vector2(0.1, 0.1), new Vector2(0.085, 0.2), new Vector2(0.06, 0.2)], 10).translate(0, 0.46, 0));
  const tulipMat = windify(bag.std("#ffffff", { rough: 0.55, side: true }), 0.06, 0.5);
  const tulips = placeParts([{ geo: stemGeo, mat: stemMat }, { geo: tulipGeo, mat: tulipMat }], mats2);
  const tc = new Color();
  const tulipCols = ["#ff7a8a", "#ffb340", "#ff5a6e", "#f6a6ff", "#ffd75e"];
  for (let i = 0; i < mats2.length; i++) tulips[1].setColorAt(i, tc.set(tulipCols[i % tulipCols.length]));
  tulips.forEach((m) => root.add(m));

  // 草
  const grassMat = windify(bag.std(dark ? "#3c7454" : "#7cc47a", { rough: 1, side: true }), 0.16, 0.45);
  root.add(scatter(bag.geo(bladeGeometry(0.5, 0.07)), grassMat, 1500, r, { x0: -13, x1: 13, z0: -9, z1: 6 },
    (x, z) => flowerArea(x, z) || r() < 0.18, [0.7, 1.5]));

  // ── 云、热气球 ──
  const cloudMat = bag.std(dark ? "#5c6a88" : "#ffffff", { rough: 1 });
  const clouds = Array.from({ length: 5 }, (_, i) => {
    const c = cloud(bag, cloudMat, r);
    c.position.set(-16 + i * 8 + r() * 3, 6.5 + r() * 2.5, -14 - r() * 6);
    c.scale.setScalar(1.3 + r() * 0.9);
    root.add(c);
    return c;
  });
  const balloon = new Group();
  const env = new Mesh(bag.geo(new LatheGeometry([new Vector2(0, -1.1), new Vector2(0.35, -0.95), new Vector2(0.9, -0.2), new Vector2(1.05, 0.4), new Vector2(0.85, 1.0), new Vector2(0.4, 1.3), new Vector2(0, 1.36)], 24)), bag.std("#ff8a6b", { rough: 0.7 }));
  const band = new Mesh(bag.geo(new CylinderGeometry(1.06, 1.0, 0.36, 24, 1, true)), bag.std("#ffd166", { rough: 0.7, side: true }));
  band.position.y = 0.3;
  const basket = new Mesh(bag.geo(new CylinderGeometry(0.28, 0.24, 0.32, 12)), bag.std("#a3764d"));
  basket.position.y = -1.6;
  balloon.add(env, band, basket);
  balloon.position.set(-10, 7.5, -18);
  root.add(balloon);

  // ── 蝴蝶 ──
  const butterflies = [["#ffb3c7", "#ffffff"], ["#ffd166", "#ff8a6b"], ["#a6d8ff", "#ffffff"]].map(([c1, c2], i) => {
    const g = new Group();
    const tex = bag.tex(wingTex(c1, c2));
    const wm = bag.mat(new MeshBasicMaterial({ map: tex, transparent: true, side: DoubleSide, alphaTest: 0.3 }));
    const wingGeo = bag.geo(new PlaneGeometry(0.42, 0.42).translate(0.21, 0, 0));
    const L = new Mesh(wingGeo, wm), R = new Mesh(wingGeo, wm);
    R.scale.x = -1;
    L.rotation.x = R.rotation.x = -Math.PI / 2;
    const wl = new Group(), wr = new Group();
    wl.add(L); wr.add(R);
    g.add(wl, wr);
    root.add(g);
    return { g, wl, wr, seed: i * 2.1, cx: (i - 1) * 7, cz: i === 1 ? -5.5 : 1 };
  });

  // ── 纸飞机 + 风线 ──
  const wind = Array.from({ length: 5 }, () => {
    const m = new Mesh(bag.geo(new PlaneGeometry(3.2, 0.05)), bag.mat(new MeshBasicMaterial({ color: "#ffffff", transparent: true, opacity: 0, depthWrite: false, side: DoubleSide })));
    root.add(m);
    return { m, t: Math.random(), y: 2 + Math.random() * 3, z: -4 + Math.random() * 3 };
  });

  // ── 风筝 ──
  const kite = new Group();
  const kiteMat = bag.mat(new MeshStandardMaterial({ vertexColors: true, roughness: 0.6, side: DoubleSide }));
  const sail = new Mesh(bag.geo(kiteGeometry("#ff6b6b", "#ffd166")), kiteMat);
  sail.castShadow = true;
  const sparV = new Mesh(bag.geo(new CylinderGeometry(0.015, 0.015, 1.48, 5)), sparMat);
  sparV.position.set(0, 0.04, 0.03);
  const sparH = new Mesh(bag.geo(new CylinderGeometry(0.015, 0.015, 1.04, 5)), sparMat);
  sparH.rotation.z = Math.PI / 2; sparH.position.set(0, 0.12, 0.03);
  kite.add(sail, sparV, sparH);
  kite.scale.setScalar(1.45);
  root.add(kite);
  // 尾巴：一串小蝴蝶结，跟随前一节（链式弹簧）
  const bowGeo = bag.geo((() => {
    const g = new BufferGeometry();
    g.setAttribute("position", new Float32BufferAttribute([0, 0, 0, -0.14, 0.07, 0, -0.14, -0.07, 0, 0, 0, 0, 0.14, -0.07, 0, 0.14, 0.07, 0], 3));
    g.computeVertexNormals();
    return g;
  })());
  const bowMats = ["#7cc4ff", "#ff9ccb", "#74d3a4"].map((c) => bag.std(c, { side: true }));
  const tail = Array.from({ length: 9 }, (_, i) => {
    const m = new Mesh(bowGeo, bowMats[i % 3]);
    root.add(m);
    return { m, p: new Vector3(), v: new Vector3() };
  });
  const tailLine = new Line(bag.geo(new BufferGeometry().setFromPoints(Array.from({ length: 10 }, () => new Vector3()))), bag.mat(new LineBasicMaterial({ color: dark ? "#d0d4e0" : "#7a7f8f" })));
  root.add(tailLine);
  const stringLine = new Line(bag.geo(new BufferGeometry().setFromPoints(Array.from({ length: 24 }, () => new Vector3()))), bag.mat(new LineBasicMaterial({ color: dark ? "#e6e8f0" : "#5f6474", transparent: true, opacity: 0.8 })));
  stringLine.frustumCulled = false; tailLine.frustumCulled = false;
  root.add(stringLine);

  const kp = new Vector3(0, 3.2, -1.5), kv = new Vector3();
  const hand = new Vector3();

  // ── 星星 ──
  const starGeo = bag.geo(new ExtrudeGeometry(starShape(), { depth: 0.1, bevelEnabled: true, bevelThickness: 0.05, bevelSize: 0.04, bevelSegments: 3 }).center());
  const starMat = bag.std("#ffd166", { rough: 0.25, metal: 0.2, emissive: "#ffb000", ei: 0.55 });
  const rainbowMat = bag.mat(new MeshStandardMaterial({ color: "#ffffff", roughness: 0.2, emissive: "#ff7ad9", emissiveIntensity: 0.8 }));
  const haloMat = bag.mat(new SpriteMaterial({ map: d.glowTex, color: "#ffd166", blending: AdditiveBlending, transparent: true, depthWrite: false, opacity: 0.8 }));
  const haloR = bag.mat(new SpriteMaterial({ map: d.glowTex, color: "#ff9ef0", blending: AdditiveBlending, transparent: true, depthWrite: false, opacity: 0.9 }));
  const stars = Array.from({ length: 6 }, (_, i) => {
    const g = new Group();
    const m = new Mesh(starGeo, starMat);
    m.castShadow = true;
    const h = new Sprite(haloMat);
    h.scale.setScalar(1.4);
    g.add(h, m);
    root.add(g);
    return { g, m, h, alive: false, wait: i * 0.4, rainbow: false, seed: Math.random() * 6 };
  });
  const place = (s: (typeof stars)[number]) => {
    s.rainbow = Math.random() < 0.18;
    s.m.material = s.rainbow ? rainbowMat : starMat;
    s.h.material = s.rainbow ? haloR : haloMat;
    for (let n = 0; n < 20; n++) {
      const x = (Math.random() * 2 - 1) * 6.6, z = -4.2 + Math.random() * 4.4, y = s.rainbow ? 4.8 + Math.random() * 0.5 : 2.8 + Math.random() * 1.5;
      s.g.position.set(x, y, z);
      if (stars.every((o) => o === s || !o.alive || o.g.position.distanceTo(s.g.position) > 2)) break;
    }
    s.g.scale.setScalar(0.01);
    s.alive = true; s.g.visible = true;
  };

  let gust = 0, gustCd = 0;
  const cStar = new Color("#ffd166"), cRain = new Color("#ff9ef0"), cWhite = new Color("#ffffff");
  let windX = 0.6;

  const stringPts = Array.from({ length: 24 }, () => new Vector3());
  const tailPts = Array.from({ length: 10 }, () => new Vector3());
  const tmp = new Vector3();

  return {
    root,
    bounds: B,
    view: { elev: 17, lookY: 2.0, lookZ: -0.6, spanX: 7.8, spanZ: 4.4 },
    motes: { mode: "float", n: 40, color: "#ffffff" },
    light: dark
      ? { sun: "#b8c6ff", sunI: 0.9, hemiSky: "#6d86c0", hemiGround: "#2c4a3c", hemiI: 0.52, exposure: 1.05 }
      : { sun: "#fff6e0", sunI: 1.7, hemiSky: "#dff1ff", hemiGround: "#9fcf90", hemiI: 0.65, exposure: 1.0 },
    spawn: { x: 0, z: 1.6 },
    init(ctx) { ctx.dada.pose("string"); kp.set(ctx.pos.x + 1.5, 3.2, ctx.pos.z - 2.5); tail.forEach((t) => t.p.copy(kp)); },
    down(ctx, hit) {
      if (hit) { ctx.target.x = Math.max(-B.x, Math.min(B.x, hit.x)); ctx.target.z = Math.max(B.zMin, Math.min(B.zMax, hit.z)); }
      if (gustCd > 0) return true;
      gust = 1; gustCd = 1.1;
      kv.y += 4.5;
      ctx.dada.stretchUp(); ctx.dada.jump(0.5, 0.4);
      wind.forEach((w) => { w.t = Math.random() * 0.3; });
      return true;
    },
    update(dt, ctx) {
      const t = ctx.time;
      gust = Math.max(0, gust - dt * 0.8); gustCd = Math.max(0, gustCd - dt);
      windX = Math.sin(t * 0.09) * 0.8;

      // 风筝：目标位于嗒嗒身后上方（顺风侧），跑得越快升得越高
      const speed = Math.hypot(ctx.vel.x, ctx.vel.z);
      const tx = ctx.pos.x + windX * 2.2 - ctx.vel.x * 0.42;
      const tz = ctx.pos.z - 2.5 - ctx.vel.z * 0.35;
      const ty = 3.3 + Math.min(1, speed / 5) * 0.8 + gust * 1.9 + (ctx.reduce ? 0 : Math.sin(t * 1.3) * 0.25);
      tmp.set(tx, ty, tz).sub(kp);
      kv.addScaledVector(tmp, dt * 7);
      kv.multiplyScalar(Math.pow(0.12, dt));
      kp.addScaledVector(kv, dt);
      kite.position.copy(kp);
      // 朝向：大致面对镜头，随速度侧倾、俯仰
      kite.rotation.set(-0.25 + kv.z * 0.06, Math.sin(t * 0.7) * 0.2, -kv.x * 0.12 + (ctx.reduce ? 0 : Math.sin(t * 2.1) * 0.08));

      // 风筝线：手 → 风筝，略带下垂
      ctx.dada.handR.getWorldPosition(hand);
      for (let i = 0; i < 24; i++) {
        const k = i / 23;
        stringPts[i].lerpVectors(hand, kp, k);
        stringPts[i].y -= Math.sin(k * Math.PI) * 0.45 * (1 - gust * 0.6);
      }
      stringLine.geometry.setFromPoints(stringPts);

      // 尾巴
      kite.localToWorld(tmp.set(0, -0.7, 0));
      let prev = tmp.clone();
      tail.forEach((s, i) => {
        s.v.y -= 2.4 * dt;
        s.v.x += windX * dt * 1.5;
        s.v.multiplyScalar(Math.pow(0.05, dt));
        s.p.addScaledVector(s.v, dt);
        const dir = s.p.clone().sub(prev);
        const len = dir.length() || 1;
        s.p.copy(prev).addScaledVector(dir, 0.3 / len);
        s.m.position.copy(s.p);
        s.m.lookAt(ctx.camera.position);
        if (!ctx.reduce) s.m.rotateZ(Math.sin(t * 6 + i) * 0.4);
        tailPts[i + 1].copy(s.p);
        prev = s.p;
      });
      tailPts[0].copy(tmp.set(0, -0.7, 0)); kite.localToWorld(tailPts[0]);
      tailLine.geometry.setFromPoints(tailPts);

      // 星星
      for (const s of stars) {
        if (!s.alive) { s.wait -= dt; if (s.wait <= 0) place(s); continue; }
        s.g.scale.setScalar(Math.min(1, s.g.scale.x + dt * 2.5));
        if (!ctx.reduce) { s.m.rotation.y += dt * 1.6; s.g.position.y += Math.sin(t * 2 + s.seed) * dt * 0.15; }
        s.h.material.opacity = 0.65 + Math.sin(t * 3 + s.seed) * 0.2;
        if (s.g.position.distanceTo(kp) < 0.9) {
          const n = s.rainbow ? 3 : 1;
          const p = s.g.position;
          ctx.score(n, s.rainbow ? "+3 彩虹星！" : "+1", { x: p.x, y: p.y + 0.5, z: p.z }, s.rainbow ? "great" : "good");
          ctx.sparks?.burst(p.x, p.y, p.z, s.rainbow ? 30 : 18, s.rainbow ? cRain : cStar, 2.6, 40);
          if (s.rainbow) ctx.confetti?.burst(p.x, p.y, p.z, 30, 3);
          ctx.dada.cheer();
          s.alive = false; s.g.visible = false; s.wait = 0.8;
        }
      }

      // 环境
      if (!ctx.reduce) {
        hub.rotation.z -= dt * (0.8 + Math.abs(windX) * 0.6 + gust * 2);
        clouds.forEach((c, i) => { c.position.x += dt * (0.25 + i * 0.04); if (c.position.x > 24) c.position.x = -24; });
        balloon.position.y = 7.5 + Math.sin(t * 0.4) * 0.5;
        balloon.position.x = -10 + Math.sin(t * 0.05) * 3;
        butterflies.forEach((b) => {
          const a = t * 0.35 + b.seed;
          b.g.position.set(b.cx + Math.sin(a) * 2.2, 1.2 + Math.sin(a * 2.3) * 0.4, b.cz + Math.cos(a * 0.8) * 1.4);
          b.g.rotation.y = Math.atan2(Math.cos(a) * 2.2, -Math.sin(a * 0.8) * 1.1);
          const flap = Math.sin(t * 16 + b.seed) * 0.9;
          b.wl.rotation.z = flap; b.wr.rotation.z = -flap;
        });
        wind.forEach((w) => {
          w.t += dt * (0.18 + gust * 0.4);
          if (w.t > 1) { w.t = 0; w.y = 1.6 + Math.random() * 3.5; w.z = -5 + Math.random() * 4; }
          w.m.position.set(-12 + w.t * 24, w.y + Math.sin(w.t * 9) * 0.2, w.z);
          w.m.lookAt(ctx.camera.position.x, w.m.position.y, ctx.camera.position.z);
          (w.m.material as MeshBasicMaterial).opacity = Math.sin(w.t * Math.PI) * (0.35 + gust * 0.4) * (dark ? 0.5 : 0.9);
        });
        if (gust > 0.5 && ctx.sparks && Math.random() < dt * 30) ctx.sparks.emit(kp.x + (Math.random() - 0.5), kp.y - 0.3, kp.z, -windX, -0.5, 0, 0.6, 20, cWhite, 0);
      }
    },
    reset(ctx) {
      stars.forEach((s, i) => { s.alive = false; s.g.visible = false; s.wait = i * 0.25; });
      gust = 0;
      ctx.dada.pose("string");
    },
    dispose() { bag.dispose(); },
  };
}
