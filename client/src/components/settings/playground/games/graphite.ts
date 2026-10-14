/** 石墨世界 · 灵感记忆（plan-playground-v2）。
 * 场景：方格稿纸桌面上的微缩工作台——巨型铅笔、刻度尺、双色橡皮、积木、马克杯（冒热气）、台灯、纸团、绕圈的纸飞机。
 * 玩法：六块带图形的方块按顺序亮起，嗒嗒看着记住；轮到你时让嗒嗒依次踩上去。
 *       全对过关（彩纸 + 欢呼），下一轮多记一个；踩错嗒嗒晕一下，重看本轮。 */
import {
  AdditiveBlending, CanvasTexture, Color, CylinderGeometry, DoubleSide, Group, LatheGeometry, Mesh, MeshBasicMaterial,
  MeshStandardMaterial, PlaneGeometry, PointLight, Shape, ShapeGeometry, SphereGeometry, Sprite, SpriteMaterial,
  TorusGeometry, Vector2, BufferGeometry, Float32BufferAttribute,
} from "three";
import { RoundedBoxGeometry } from "three/examples/jsm/geometries/RoundedBoxGeometry.js";
import { Bag, lumpy, rng, shadow, shaftTex } from "../kit";
import type { Game, GameCtx, GameDeps } from "./types";

const PAD_COLORS = ["#ff8a80", "#ffc861", "#74d3a4", "#7cc4ff", "#b79cff", "#ff9ccb"];
const PADS: Array<[number, number]> = [[-3.3, -1.45], [0, -1.45], [3.3, -1.45], [-3.3, 1.75], [0, 1.75], [3.3, 1.75]];
const HALF = 0.86;

/** 方块上的图形：圆 / 三角 / 方 / 星 / 心 / 菱形，便于用形状而不只是颜色记忆 */
function glyph(i: number): Shape {
  const s = new Shape();
  const r = 0.36;
  if (i === 0) { s.absarc(0, 0, r, 0, Math.PI * 2, false); }
  else if (i === 1) { s.moveTo(0, r * 1.05); s.lineTo(r * 0.98, -r * 0.7); s.lineTo(-r * 0.98, -r * 0.7); s.closePath(); }
  else if (i === 2) {
    const a = r * 0.82, c = 0.08;
    s.moveTo(-a + c, -a); s.lineTo(a - c, -a); s.quadraticCurveTo(a, -a, a, -a + c); s.lineTo(a, a - c); s.quadraticCurveTo(a, a, a - c, a);
    s.lineTo(-a + c, a); s.quadraticCurveTo(-a, a, -a, a - c); s.lineTo(-a, -a + c); s.quadraticCurveTo(-a, -a, -a + c, -a);
  } else if (i === 3) {
    for (let k = 0; k < 10; k++) {
      const a = Math.PI / 2 + (k * Math.PI) / 5, rr = k % 2 ? r * 0.45 : r * 1.08;
      if (k === 0) s.moveTo(Math.cos(a) * rr, Math.sin(a) * rr); else s.lineTo(Math.cos(a) * rr, Math.sin(a) * rr);
    }
    s.closePath();
  } else if (i === 4) {
    s.moveTo(0, -r * 0.95);
    s.bezierCurveTo(-r * 1.3, -r * 0.1, -r * 0.75, r * 1.05, 0, r * 0.45);
    s.bezierCurveTo(r * 0.75, r * 1.05, r * 1.3, -r * 0.1, 0, -r * 0.95);
  } else { s.moveTo(0, r * 1.1); s.lineTo(r * 0.8, 0); s.lineTo(0, -r * 1.1); s.lineTo(-r * 0.8, 0); s.closePath(); }
  return s;
}

/** 刻度尺贴图 */
function rulerTex(): CanvasTexture {
  const c = document.createElement("canvas");
  c.width = 1024; c.height = 128;
  const g = c.getContext("2d")!;
  g.fillStyle = "#f3e2b8"; g.fillRect(0, 0, 1024, 128);
  g.strokeStyle = "#6b5a3a"; g.lineWidth = 3;
  for (let i = 0; i <= 60; i++) {
    const x = 16 + i * 16.5, h = i % 10 === 0 ? 54 : i % 5 === 0 ? 38 : 22;
    g.beginPath(); g.moveTo(x, 0); g.lineTo(x, h); g.stroke();
  }
  g.fillStyle = "#6b5a3a"; g.font = "bold 26px sans-serif";
  for (let i = 0; i <= 6; i++) g.fillText(String(i), 10 + i * 165, 90);
  return new CanvasTexture(c);
}

/** 纸飞机：两片机翼 + 机身折线 */
function planeGeometry(): BufferGeometry {
  const v = [
    0, 0, 0.6, -0.42, 0.02, -0.38, 0, 0.02, -0.3,
    0, 0, 0.6, 0, 0.02, -0.3, 0.42, 0.02, -0.38,
    0, 0, 0.6, 0, 0.02, -0.3, 0, -0.14, -0.3,
  ];
  const g = new BufferGeometry();
  g.setAttribute("position", new Float32BufferAttribute(v, 3));
  g.computeVertexNormals();
  return g;
}

type Phase = "idle" | "show" | "input" | "pause";

export function createGraphite(d: GameDeps): Game {
  const bag = new Bag();
  const root = new Group();
  const r = rng("graphite-desk");
  const dark = d.dark;

  // ── 方块 ──
  const padGeo = bag.geo(new RoundedBoxGeometry(1.72, 0.22, 1.72, 4, 0.12));
  const baseGeo = bag.geo(new RoundedBoxGeometry(1.98, 0.12, 1.98, 3, 0.08));
  const baseMat = bag.std(dark ? "#3a3d48" : "#d9dbe2", { rough: 0.6 });
  const glyphMat = bag.mat(new MeshStandardMaterial({ color: 0xffffff, roughness: 0.4, transparent: true, opacity: 0.92 }));
  const shaft = bag.tex(shaftTex());
  const shaftGeo = bag.geo(new PlaneGeometry(1.5, 3.4));
  const pads = PADS.map(([x, z], i) => {
    const g = new Group();
    g.position.set(x, 0, z);
    const base = new Mesh(baseGeo, baseMat);
    base.position.y = 0.06;
    const col = new Color(PAD_COLORS[i]);
    const mat = bag.mat(new MeshStandardMaterial({ color: col, roughness: 0.35, metalness: 0, emissive: col, emissiveIntensity: 0.04 }));
    const top = new Group();
    const pad = new Mesh(padGeo, mat);
    pad.position.y = 0.11;
    const gl = new Mesh(bag.geo(new ShapeGeometry(glyph(i), 24)), glyphMat);
    gl.rotation.x = -Math.PI / 2;
    gl.position.y = 0.226;
    top.add(pad, gl);
    top.position.y = 0.1;
    const beamMat = bag.mat(new MeshBasicMaterial({ map: shaft, color: col, transparent: true, opacity: 0, blending: AdditiveBlending, depthWrite: false, side: DoubleSide }));
    const beam = new Group();
    for (const ry of [0, Math.PI / 2]) { const b = new Mesh(shaftGeo, beamMat); b.rotation.y = ry; beam.add(b); }
    beam.position.y = 1.7;
    g.add(base, top, beam);
    shadow(base, false, true); shadow(top, true, true);
    root.add(g);
    return { g, top, mat, beamMat, col, glow: 0, press: 0, x, z, flash: 0 };
  });

  // ── 桌面摆件 ──
  // 巨型铅笔（六棱杆 + 木削尖 + 笔芯 + 金属箍 + 橡皮头）
  const pencil = new Group();
  const pBody = new Mesh(bag.geo(new CylinderGeometry(0.32, 0.32, 5.2, 6)), bag.std("#f5c54a", { rough: 0.5 }));
  const pWood = new Mesh(bag.geo(new CylinderGeometry(0.06, 0.32, 0.9, 6)), bag.std("#f0cf9e", { rough: 0.8 }));
  pWood.position.y = 3.05;
  const pLead = new Mesh(bag.geo(new CylinderGeometry(0.0, 0.07, 0.22, 12)), bag.std("#3a3a44", { rough: 0.3 }));
  pLead.position.y = 3.6;
  const pBand = new Mesh(bag.geo(new CylinderGeometry(0.335, 0.335, 0.42, 20)), bag.std("#c9ccd6", { rough: 0.25, metal: 0.8 }));
  pBand.position.y = -2.8;
  const pRub = new Mesh(bag.geo(new CylinderGeometry(0.31, 0.31, 0.5, 20)), bag.std("#f59aa8", { rough: 0.7 }));
  pRub.position.y = -3.25;
  const pCap = new Mesh(bag.geo(new SphereGeometry(0.31, 20, 10, 0, Math.PI * 2, Math.PI / 2, Math.PI / 2)), pRub.material);
  pCap.position.y = -3.5;
  pencil.add(pBody, pWood, pLead, pBand, pRub, pCap);
  pencil.rotation.set(0, 0.42, Math.PI / 2);
  pencil.position.set(-5.6, 0.32, -5.0);
  root.add(shadow(pencil));

  // 刻度尺
  const ruler = new Mesh(bag.geo(new RoundedBoxGeometry(6.2, 0.07, 0.8, 2, 0.03)), [
    bag.std("#e9d5a6"), bag.std("#e9d5a6"), bag.mat(new MeshStandardMaterial({ map: bag.tex(rulerTex()), roughness: 0.6 })),
    bag.std("#e9d5a6"), bag.std("#e9d5a6"), bag.std("#e9d5a6"),
  ]);
  ruler.position.set(4.6, 0.035, -4.9);
  ruler.rotation.y = -0.12;
  root.add(shadow(ruler, true, true));

  // 双色橡皮
  const eraser = new Group();
  const e1 = new Mesh(bag.geo(new RoundedBoxGeometry(0.75, 0.42, 0.62, 3, 0.08)), bag.std("#f7a1ad", { rough: 0.85 }));
  const e2 = new Mesh(bag.geo(new RoundedBoxGeometry(0.55, 0.42, 0.62, 3, 0.08)), bag.std("#86b6f2", { rough: 0.85 }));
  e1.position.x = -0.3; e2.position.x = 0.36;
  eraser.add(e1, e2);
  eraser.position.set(7.1, 0.21, 2.4); eraser.rotation.y = 0.5;
  root.add(shadow(eraser));

  // 积木塔
  const blocks = new Group();
  const bGeo = bag.geo(new RoundedBoxGeometry(0.7, 0.7, 0.7, 3, 0.1));
  [["#ffc861", 0, 0, 0, 0.1], ["#74d3a4", 0.78, 0, 0.1, -0.2], ["#7cc4ff", 0.38, 0.7, 0.05, 0.35]].forEach(([c, x, y, z, ry]) => {
    const b = new Mesh(bGeo, bag.std(c as string, { rough: 0.55 }));
    b.position.set(x as number, (y as number) + 0.35, z as number); b.rotation.y = ry as number;
    blocks.add(b);
  });
  blocks.position.set(6.6, 0, -1.6);
  root.add(shadow(blocks));

  // 马克杯 + 热气
  const mug = new Group();
  const cupPts = [new Vector2(0, 0), new Vector2(0.5, 0), new Vector2(0.55, 0.05), new Vector2(0.58, 0.9), new Vector2(0.62, 1.0), new Vector2(0.54, 1.0), new Vector2(0.5, 0.15), new Vector2(0, 0.15)];
  const cup = new Mesh(bag.geo(new LatheGeometry(cupPts, 40)), bag.std("#e86f5c", { rough: 0.35 }));
  const handle = new Mesh(bag.geo(new TorusGeometry(0.26, 0.07, 12, 24, Math.PI * 1.2)), cup.material);
  handle.position.set(0.6, 0.52, 0); handle.rotation.z = -Math.PI * 0.6;
  const coffee = new Mesh(bag.geo(new CylinderGeometry(0.5, 0.5, 0.02, 32)), bag.std("#6b4430", { rough: 0.15 }));
  coffee.position.y = 0.86;
  mug.add(cup, handle, coffee);
  mug.position.set(-7.3, 0, 1.6);
  root.add(shadow(mug));
  const steamMat = bag.mat(new MeshBasicMaterial({ map: shaft, color: 0xffffff, transparent: true, opacity: dark ? 0.12 : 0.3, depthWrite: false, side: DoubleSide }));
  const steam: Mesh[] = [];
  for (let i = 0; i < 2; i++) {
    const s = new Mesh(bag.geo(new PlaneGeometry(0.4, 1.6, 1, 8)), steamMat);
    s.position.set(-7.3 + (i - 0.5) * 0.18, 1.8, 1.6);
    steam.push(s); root.add(s);
  }

  // 台灯：底座 + 两节臂 + 灯罩 + 暖光
  const lamp = new Group();
  const lampMat = bag.std(dark ? "#5c6a8a" : "#4f5d7e", { rough: 0.35, metal: 0.3 });
  const lb = new Mesh(bag.geo(new CylinderGeometry(0.7, 0.8, 0.16, 32)), lampMat); lb.position.y = 0.08;
  const arm1 = new Mesh(bag.geo(new CylinderGeometry(0.07, 0.07, 2.4, 10)), lampMat);
  arm1.position.set(0.3, 1.2, 0); arm1.rotation.z = -0.28;
  const j1 = new Mesh(bag.geo(new SphereGeometry(0.13, 16, 10)), lampMat); j1.position.set(0.63, 2.36, 0);
  const arm2 = new Mesh(arm1.geometry, lampMat);
  arm2.position.set(-0.2, 2.86, 0); arm2.rotation.z = 1.05; arm2.scale.y = 0.75;
  const shade = new Mesh(bag.geo(new CylinderGeometry(0.22, 0.7, 0.8, 32, 1, true)), bag.std(dark ? "#5c6a8a" : "#4f5d7e", { rough: 0.35, side: true }));
  shade.position.set(-1.05, 2.9, 0); shade.rotation.z = 0.6;
  const bulb = new Mesh(bag.geo(new SphereGeometry(0.2, 16, 12)), bag.mat(new MeshBasicMaterial({ color: "#fff3cf" })));
  bulb.position.set(-1.12, 2.78, 0);
  lamp.add(lb, arm1, j1, arm2, shade, bulb);
  lamp.position.set(-7.6, 0, -3.0); lamp.rotation.y = -0.5;
  root.add(shadow(lamp, true, false));
  bulb.castShadow = false;
  const lampLight = new PointLight("#ffd79a", dark ? 9 : 3.5, 8, 1.6);
  lampLight.position.set(-6.7, 2.5, -2.6);
  root.add(lampLight);
  const halo = new Sprite(bag.mat(new SpriteMaterial({ map: d.glowTex, color: "#ffd79a", blending: AdditiveBlending, transparent: true, depthWrite: false, opacity: dark ? 0.7 : 0.35 })));
  halo.scale.setScalar(2.4); halo.position.set(-6.75, 2.6, -2.55);
  root.add(halo);

  // 纸团 + 便签
  const paper = bag.std("#fbfbf7", { rough: 0.95, flat: true });
  for (const [x, z, s] of [[5.4, 3.6, 0.32], [-6.2, 3.4, 0.26], [7.6, 0.4, 0.24]] as const) {
    const b = new Mesh(lumpy(1, 1, 0.5, 2.4, x, true), paper);
    bag.geo(b.geometry);
    b.scale.setScalar(s); b.position.set(x, s * 0.85, z); b.rotation.set(r() * 3, r() * 3, 0);
    root.add(shadow(b));
  }
  const notes = new Group();
  ["#fff1a6", "#ffd8a6", "#c8f0d6"].forEach((c, i) => {
    const n = new Mesh(bag.geo(new RoundedBoxGeometry(1.0, 0.05, 1.0, 2, 0.02)), bag.std(c, { rough: 0.9 }));
    n.position.y = 0.025 + i * 0.05; n.rotation.y = (r() - 0.5) * 0.5;
    notes.add(n);
  });
  notes.position.set(-2.4, 0, -4.4);
  root.add(shadow(notes));

  // 绕圈的纸飞机
  const planeGeo = bag.geo(planeGeometry());
  const planeMat = bag.std("#ffffff", { rough: 0.7, side: true, flat: true });
  const planes = [0, 1].map((i) => {
    const m = new Mesh(planeGeo, planeMat);
    m.castShadow = true;
    m.scale.setScalar(1.1);
    root.add(m);
    return { m, a: i * Math.PI, rad: 5.6 + i * 1.2, h: 3.4 + i * 0.6, sp: 0.32 + i * 0.07 };
  });

  // ── 规则 ──
  let phase: Phase = "idle";
  let seq: number[] = [];
  let step = 0;
  let timer = 0;
  let round = 0;
  let lastPad = -1;
  let showLit = false;

  const padUnder = (x: number, z: number) => pads.findIndex((p) => Math.abs(p.x - x) < HALF && Math.abs(p.z - z) < HALF);
  const light = (i: number, k = 1) => { pads[i].glow = Math.max(pads[i].glow, k); };

  const newRound = (ctx: GameCtx, len: number) => {
    const s: number[] = [];
    for (let i = 0; i < len; i++) {
      let n = Math.floor(Math.random() * 6);
      if (s.length && n === s[s.length - 1]) n = (n + 1 + Math.floor(Math.random() * 5)) % 6;
      s.push(n);
    }
    seq = s; step = 0; phase = "show"; timer = 0.7; showLit = false;
    ctx.say(`第 ${round} 轮 · 记住 ${len} 个`, "info");
  };
  const begin = (ctx: GameCtx) => { round = 1; newRound(ctx, 3); };
  const showDur = () => Math.max(0.36, 0.62 - round * 0.03);

  return {
    root,
    bounds: { x: 5.6, zMin: -3.0, zMax: 3.2 },
    view: { elev: 32, lookY: 0.4, lookZ: 0.1, spanX: 7.4, spanZ: 4.9 },
    motes: { mode: "float", n: 36, color: dark ? "#9fb3ff" : "#ffffff" },
    light: { sun: "#fff4e6", exposure: dark ? 1.0 : 1.02 },
    spawn: { x: 1.65, z: 0.2 },
    poke() { return phase === "idle" ? false : undefined; },
    down(ctx) {
      if (phase === "idle") { begin(ctx); ctx.dada.cheer(); return true; }
      return false;
    },
    update(dt, ctx) {
      const t = ctx.time;
      // 方块：亮起 → 浮起发光 + 光柱；被踩 → 下沉
      const under = padUnder(ctx.pos.x, ctx.pos.z);
      pads.forEach((p, i) => {
        p.glow = Math.max(0, p.glow - dt * 2.6);
        p.flash = Math.max(0, p.flash - dt * 2);
        const pressed = under === i && !ctx.dada.airborne ? 1 : 0;
        p.press += (pressed - p.press) * Math.min(1, dt * 14);
        p.top.position.y = 0.1 - p.press * 0.07 + p.glow * 0.06;
        const bad = p.flash > 0 && Math.sin(p.flash * 30) > 0;
        p.mat.emissive.set(bad ? "#ff4d4d" : PAD_COLORS[i]);
        p.mat.emissiveIntensity = 0.04 + p.glow * 0.9 + (bad ? 0.8 : 0) + (phase === "idle" && !ctx.reduce ? (Math.sin(t * 2.4 - i * 0.7) * 0.5 + 0.5) * 0.18 : 0);
        p.beamMat.opacity = p.glow * 0.55;
      });

      if (phase === "show") {
        timer -= dt;
        if (timer <= 0) {
          if (showLit) { showLit = false; step++; timer = 0.16; if (step >= seq.length) { phase = "input"; step = 0; lastPad = under; ctx.say("轮到你了：按顺序踩上去", "info"); } }
          else { showLit = true; timer = showDur(); }
        }
        if (showLit && step < seq.length) {
          light(seq[step]);
          const p = pads[seq[step]];
          ctx.dada.lookAt(p.x, 0.3, p.z);
        }
      } else if (phase === "input") {
        if (under !== lastPad) {
          lastPad = under;
          if (under >= 0) {
            const p = pads[under];
            if (under === seq[step]) {
              light(under, 1);
              step++;
              ctx.sparks?.burst(p.x, 0.5, p.z, 14, p.col, 2.2, 34);
              ctx.ripple(p.x, p.z);
              ctx.pop(`${step}/${seq.length}`, { x: p.x, y: 1.2, z: p.z }, "good");
              if (step >= seq.length) {
                const pts = seq.length;
                ctx.score(pts, `+${pts}`, { x: ctx.pos.x, y: 2.6, z: ctx.pos.z }, "great");
                ctx.dada.cheer(); ctx.dada.pose("up"); ctx.dada.jump(1.0, 0.55);
                ctx.confetti?.burst(ctx.pos.x, 2.2, ctx.pos.z, 46, 5);
                ctx.say(`第 ${round} 轮完成！`, "great");
                pads.forEach((q) => { q.glow = 0.8; });
                phase = "pause"; timer = 1.6;
              }
            } else {
              p.flash = 1;
              ctx.dada.emote("dizzy", 1.3); ctx.dada.shake();
              ctx.pop("顺序不对", { x: p.x, y: 1.2, z: p.z }, "bad");
              ctx.say("顺序错了，再看一遍", "bad");
              phase = "pause"; timer = 1.4; step = -1;
            }
          }
        }
      } else if (phase === "pause") {
        timer -= dt;
        if (timer <= 0) {
          ctx.dada.pose("none");
          if (step === -1) { const len = seq.length; const keep = seq; newRound(ctx, len); seq = keep; }
          else { round++; newRound(ctx, Math.min(9, seq.length + 1)); }
        }
      }

      // 摆件动画
      if (!ctx.reduce) {
        planes.forEach((p) => {
          p.a += dt * p.sp;
          const x = Math.cos(p.a) * p.rad, z = -1 + Math.sin(p.a) * p.rad * 0.55;
          p.m.position.set(x, p.h + Math.sin(t * 1.3 + p.a) * 0.25, z);
          p.m.rotation.set(0, Math.atan2(-Math.sin(p.a) * p.rad, Math.cos(p.a) * p.rad * 0.55), 0);
          p.m.rotateZ(-0.35);
        });
        steam.forEach((s, i) => {
          s.position.y = 1.6 + ((t * 0.25 + i * 0.5) % 1) * 0.5;
          s.rotation.y = Math.sin(t * 0.8 + i) * 0.6;
          s.scale.x = 1 + Math.sin(t * 1.4 + i * 2) * 0.25;
        });
        lampLight.intensity = (dark ? 9 : 3.5) * (0.96 + Math.sin(t * 13) * 0.01);
      }
    },
    reset(ctx) {
      phase = "idle"; seq = []; step = 0; round = 0;
      pads.forEach((p) => { p.glow = 0; p.flash = 0; });
      ctx.dada.pose("none");
      ctx.say("点击舞台开始新一局", "info");
    },
    dispose() { bag.dispose(); },
  };
}
