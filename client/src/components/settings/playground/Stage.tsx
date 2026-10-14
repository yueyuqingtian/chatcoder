/** 乐园 3D 舞台（plan-114-452；plan-playground-v2 重构为「舞台 + 玩法」）。
 * 本文件及 three 只经 React.lazy 加载（PlaygroundPanel），不进首屏包。
 *
 * 舞台负责：渲染器（ACES、软阴影、环境反射、雾）、相机（按玩法取景 + 指针视差）、指针 → 地面坐标、
 *          嗒嗒的追随移动、粒子 / 彩纸、得分浮字与头顶量表、生命周期。
 * 玩法（games/<world>.ts）负责：本世界的布景与规则，通过 GameCtx 读写角色、计分、播报。
 * 生命周期：离开页面 / 窗口隐藏 / 滚出视口即停帧；卸载时释放几何、材质、贴图、环境图、渲染器与 canvas。
 * 降级：不支持 WebGL 时回调 onUnsupported；prefers-reduced-motion 下去掉环境动画与粒子，只保留玩法必需动作。 */
import { useEffect, useRef } from "react";
import {
  ACESFilmicToneMapping, AmbientLight, Color, DirectionalLight, Fog, HemisphereLight, MathUtils, Mesh, MeshBasicMaterial,
  PCFSoftShadowMap, PMREMGenerator, PerspectiveCamera, Plane, PlaneGeometry, Raycaster, SRGBColorSpace, Scene, Vector2, Vector3,
  WebGLRenderer,
} from "three";
import { RoomEnvironment } from "three/examples/jsm/environments/RoomEnvironment.js";
import { makeColorResolver } from "./colors";
import { DADA_H, Dada3D } from "./dada3d";
import { Confetti, Motes, Sparks, makeGlowTexture } from "./fx";
import { GROUND_COLORS, Ground } from "./ground";
import { radialTex, windClock } from "./kit";
import { GAMES } from "./games";
import type { GameCtx, MeterSpec, Palette, Tone } from "./games/types";

const FOV = 32;

export default function Stage({ world, theme, resetKey, onScore, onSay, onInteract, onUnsupported }: {
  world: string;
  theme: string;
  /** 递增即清场重来 */
  resetKey: number;
  /** 得分回传 +n */
  onScore: (n: number) => void;
  /** 顶部播报 */
  onSay: (text: string, tone: Tone) => void;
  /** 第一次有交互时回调一次，供外层淡出起手提示 */
  onInteract: () => void;
  onUnsupported: () => void;
}) {
  const hostRef = useRef<HTMLDivElement>(null);
  const popsRef = useRef<HTMLDivElement>(null);
  const meterRef = useRef<HTMLDivElement>(null);
  const cbRef = useRef({ onScore, onSay, onInteract });
  cbRef.current = { onScore, onSay, onInteract };
  const resetRef = useRef<() => void>(() => {});

  useEffect(() => { resetRef.current(); }, [resetKey]);

  useEffect(() => {
    const host = hostRef.current, popsEl = popsRef.current, meterEl = meterRef.current;
    if (!host || !popsEl || !meterEl) return;
    const reduce = !!window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

    let renderer: WebGLRenderer;
    try {
      renderer = new WebGLRenderer({ antialias: true, alpha: true, powerPreference: "high-performance" });
    } catch {
      onUnsupported();
      return;
    }
    const dark = theme === "dark";
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.outputColorSpace = SRGBColorSpace;
    renderer.toneMapping = ACESFilmicToneMapping;
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = PCFSoftShadowMap;
    renderer.setClearColor(0x000000, 0);
    const cv = renderer.domElement;
    cv.className = "pg-canvas";
    cv.setAttribute("aria-label", "嗒嗒乐园：移动鼠标，嗒嗒会跟过去；点击与按住可以进行本世界的玩法");
    host.insertBefore(cv, host.firstChild);

    // ── 配色：读舞台上的 --pg-* 变量（随世界 / 明暗变化） ──
    const rgb = makeColorResolver(host);
    const pal: Palette = {
      skyTop: rgb("--pg-sky-top", "#f4f6fa"),
      skyBottom: rgb("--pg-sky-bottom", "#fafafa"),
      ground: rgb("--pg-ground", "#ececec"),
      groundLine: rgb("--pg-ground-line", "#cccccc"),
      prop: rgb("--pg-prop", "#dddddd"),
      cloud: rgb("--pg-cloud", "#ffffff"),
      accent: rgb("--accent", "#33aa66"),
      accent2: rgb("--world-2", "#ddaa55"),
    };
    host.style.color = "";

    const scene = new Scene();
    const camera = new PerspectiveCamera(FOV, 1, 0.1, 140);

    const pmrem = new PMREMGenerator(renderer);
    const envTex = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
    scene.environment = envTex;
    scene.environmentIntensity = dark ? 0.4 : 0.55;

    const glowTex = makeGlowTexture();
    const groundY = world === "tide" ? -0.62 : 0;
    const gc = (GROUND_COLORS[world] ?? GROUND_COLORS.graphite)[dark ? "dark" : "light"];
    const ground = new Ground(world, { a: gc[0], b: gc[1], line: gc[2], accent: pal.accent, accent2: pal.accent2 }, groundY);
    scene.add(ground.mesh);

    const game = (GAMES[world] ?? GAMES.graphite)({ world, pal, dark, reduce, glowTex, ground });
    scene.add(game.root);
    const L = game.light ?? {};
    renderer.toneMappingExposure = L.exposure ?? 1;
    // 雾从「相机到场地中心的距离」之后才开始，只吞掉远景，近处不发灰（resize 时按取景距离更新）
    const fog = new Fog(new Color(pal.skyBottom), 30, 60);
    scene.fog = fog;

    const hemi = new HemisphereLight(L.hemiSky ?? 0xffffff, L.hemiGround ?? (dark ? 0x5a6080 : 0xc9d1ea), L.hemiI ?? (dark ? 0.6 : 0.6));
    scene.add(hemi);
    const sun = new DirectionalLight(L.sun ?? 0xfff2e2, L.sunI ?? (dark ? 1.0 : 1.7));
    sun.position.set(-6, 11, 7);
    sun.castShadow = true;
    sun.shadow.mapSize.set(2048, 2048);
    const sc = sun.shadow.camera;
    sc.left = -14; sc.right = 14; sc.top = 12; sc.bottom = -12; sc.near = 1; sc.far = 40;
    sun.shadow.bias = -0.0004;
    sun.shadow.normalBias = 0.025;
    sun.shadow.radius = 4;
    scene.add(sun, sun.target);
    scene.add(new AmbientLight(0xffffff, dark ? 0.18 : 0.12));

    const dada = new Dada3D(reduce);
    if (world === "tide") dada.setFloat(true, "#ff8a6b", "#ffffff");
    scene.add(dada.root);

    // 粒子：事件粒子池 + 彩纸 + 环境光点（减弱动效档不创建）
    const sparks = reduce ? null : new Sparks(320);
    if (sparks) scene.add(sparks.obj);
    const confetti = reduce ? null : new Confetti(180);
    if (confetti) scene.add(confetti.obj);
    const moteCfg = game.motes ?? { mode: "float" as const, n: 0, color: pal.accent2 };
    const motes = new Motes(reduce ? 0 : moteCfg.n, new Color(moteCfg.color), moteCfg.mode);
    scene.add(motes.obj);
    const cHeart = new Color("#ff8fa8"), cTrail = new Color(pal.accent2).lerp(new Color(0xffffff), 0.55);

    // 角色脚下的接触阴影（实时阴影之外再压一层柔和的 AO，落地感更实）
    const blobTex = radialTex([[0, "rgba(0,0,0,0.85)"], [0.55, "rgba(0,0,0,0.3)"], [1, "rgba(0,0,0,0)"]], 64);
    const blobMat = new MeshBasicMaterial({ map: blobTex, transparent: true, opacity: 0.2, depthWrite: false });
    const blobGeo = new PlaneGeometry(2.2, 2.2);
    const blob = new Mesh(blobGeo, blobMat);
    blob.rotation.x = -Math.PI / 2;
    blob.position.y = 0.012;
    blob.visible = world !== "tide";
    scene.add(blob);

    // ── 状态 ──
    const B = game.bounds;
    const spawn = game.spawn ?? { x: 0, z: 0.8 };
    const pos = { x: spawn.x, z: spawn.z };
    const target = { x: pos.x, z: pos.z };
    const vel = { x: 0, z: 0 };
    const pointer = { x: 0, z: 0, has: false };
    dada.root.position.set(pos.x, 0, pos.z);
    const ndc = new Vector2();
    const ray = new Raycaster();
    const groundPlane = new Plane(new Vector3(0, 1, 0), 0);
    const hit = new Vector3();
    const cam = { x: 0, y: 0 }, camT = { x: 0, y: 0 };
    let interacted = false, ripTimer = 0, trailTimer = 0, time = 0;
    const touched = () => { if (!interacted) { interacted = true; cbRef.current.onInteract(); } };

    // ── 浮字与量表（DOM，按 3D 位置投影） ──
    const proj = new Vector3();
    const toScreen = (x: number, y: number, z: number) => {
      proj.set(x, y, z).project(camera);
      return { x: (proj.x * 0.5 + 0.5) * host.clientWidth, y: (-proj.y * 0.5 + 0.5) * host.clientHeight };
    };
    const pop = (text: string, at: { x: number; y: number; z: number }, tone: Tone = "good") => {
      const s = toScreen(at.x, at.y, at.z);
      const el = document.createElement("div");
      el.className = "pg-pop";
      el.dataset.tone = tone;
      el.textContent = text;
      el.style.left = `${s.x}px`;
      el.style.top = `${s.y}px`;
      popsEl.appendChild(el);
      const kill = () => el.remove();
      el.addEventListener("animationend", kill, { once: true });
      window.setTimeout(kill, 1800);
    };
    let meterSpec: MeterSpec | null = null;
    const meterFill = meterEl.querySelector<HTMLElement>(".pg-meter-needle");
    const meterBand = meterEl.querySelector<HTMLElement>(".pg-meter-band");
    const meterLabel = meterEl.querySelector<HTMLElement>(".pg-meter-label");

    const ctx: GameCtx = {
      dada, pos, vel, pointer, target, sparks, confetti, camera, reduce,
      get time() { return time; },
      lockMove: false,
      ripple: (x, z) => { ground.addRipple(x, z); game.onRipple?.(x, z); },
      score: (n, text, at, tone = "good") => { cbRef.current.onScore(n); pop(text, at, tone); },
      pop,
      say: (text, tone = "info") => cbRef.current.onSay(text, tone),
      meter: (spec) => {
        meterSpec = spec;
        meterEl.dataset.show = spec ? "1" : "";
        if (!spec) return;
        meterEl.dataset.kind = spec.kind;
        if (meterBand) { meterBand.style.left = `${spec.band[0] * 100}%`; meterBand.style.width = `${(spec.band[1] - spec.band[0]) * 100}%`; }
        if (meterFill) meterFill.style.left = `${MathUtils.clamp(spec.v, 0, 1) * 100}%`;
        if (meterLabel) meterLabel.textContent = spec.label ?? "";
      },
    };

    const clampT = () => {
      target.x = MathUtils.clamp(target.x, -B.x, B.x);
      target.z = MathUtils.clamp(target.z, B.zMin, B.zMax);
    };
    const setNdc = (e: PointerEvent) => {
      const r = cv.getBoundingClientRect();
      ndc.set(((e.clientX - r.left) / r.width) * 2 - 1, -(((e.clientY - r.top) / r.height) * 2 - 1));
      ray.setFromCamera(ndc, camera);
    };
    const toGround = (): boolean => !!ray.ray.intersectPlane(groundPlane, hit);
    const overDada = (): boolean => ray.intersectObject(dada.root, true).length > 0;

    const onMove = (e: PointerEvent) => {
      setNdc(e);
      camT.x = ndc.x; camT.y = ndc.y;
      if (!toGround()) return;
      pointer.x = hit.x; pointer.z = hit.z; pointer.has = true;
      if (!ctx.lockMove) { target.x = hit.x; target.z = hit.z; clampT(); }
    };
    const onDown = (e: PointerEvent) => {
      touched();
      try { cv.setPointerCapture(e.pointerId); } catch { /* 捕获失败不影响玩法 */ }
      setNdc(e);
      if (overDada()) {
        // 玩法可接管：true 已处理；false 当作普通点击（如篝火旁按住烤）；undefined 走默认的「戳一下冒小心心」
        const r = game.poke?.(ctx);
        if (r !== false) {
          if (r !== true) {
            dada.prod();
            if (sparks) for (let i = 0; i < 7; i++) sparks.emit(pos.x + (Math.random() - 0.5) * 0.7, DADA_H + 0.1 + Math.random() * 0.3, pos.z + 0.3, (Math.random() - 0.5) * 0.8, 1.2 + Math.random() * 0.8, 0.2, 0.9, 46, cHeart, 0.8);
          }
          return;
        }
      }
      const g = toGround() ? { x: hit.x, z: hit.z } : null;
      if (g) { pointer.x = g.x; pointer.z = g.z; pointer.has = true; }
      if (game.down?.(ctx, g)) return;
      if (g) { target.x = g.x; target.z = g.z; clampT(); dada.jump(); ctx.ripple(target.x, target.z); }
    };
    const onUp = () => { game.up?.(ctx); };
    const onLeave = () => { pointer.has = false; dada.look(0, 0); camT.x = 0; camT.y = 0; };
    cv.addEventListener("pointermove", onMove);
    cv.addEventListener("pointerdown", onDown);
    cv.addEventListener("pointerup", onUp);
    cv.addEventListener("pointercancel", onUp);
    cv.addEventListener("pointerleave", onLeave);

    resetRef.current = () => {
      pos.x = spawn.x; pos.z = spawn.z; target.x = pos.x; target.z = pos.z; vel.x = vel.z = 0;
      dada.root.position.set(pos.x, dada.root.position.y, pos.z);
      ctx.lockMove = false;
      ctx.meter(null);
      game.reset(ctx);
      ctx.ripple(pos.x, pos.z);
    };

    // ── 取景：保证玩法要求的范围都在画面内 ──
    const V = game.view ?? {};
    const elev = MathUtils.degToRad(V.elev ?? 27);
    let dist = 14;
    const resize = () => {
      const w = host.clientWidth, h = host.clientHeight;
      if (!w || !h) return;
      renderer.setSize(w, h, false);
      camera.aspect = w / h;
      const tanV = Math.tan(MathUtils.degToRad(FOV / 2));
      dist = Math.max((V.spanX ?? 7.6) / (tanV * camera.aspect), (V.spanZ ?? 5.0) / tanV);
      fog.near = dist + 8; fog.far = dist + 36;
      camera.updateProjectionMatrix();
      const ps = (h * renderer.getPixelRatio()) / (2 * tanV);
      sparks?.setScale(ps); motes.setScale(ps);
    };
    const ro = new ResizeObserver(resize);
    ro.observe(host);
    resize();
    game.init?.(ctx);

    const mv = game.move ?? {};
    const maxSpeed = mv.max ?? 5.4, accel = mv.accel ?? 7;

    // ── 主循环 ──
    let raf = 0, last = performance.now(), visible = true;
    const loop = (now: number) => {
      raf = 0;
      const dt = Math.min(0.05, (now - last) / 1000);
      last = now; time += dt;
      windClock.value = reduce ? 0 : time;

      // 追随：弹簧式（距离越远越快，封顶），到达后自然停下
      if (ctx.lockMove) { target.x = pos.x; target.z = pos.z; }
      const dx = target.x - pos.x, dz = target.z - pos.z;
      const dd = Math.hypot(dx, dz);
      const want = dd > 0.12 ? Math.min(maxSpeed, dd * 3.2) : 0;
      const wx = dd > 0 ? (dx / dd) * want : 0, wz = dd > 0 ? (dz / dd) * want : 0;
      const k = Math.min(1, dt * accel);
      vel.x += (wx - vel.x) * k; vel.z += (wz - vel.z) * k;
      pos.x += vel.x * dt; pos.z += vel.z * dt;
      pos.x = MathUtils.clamp(pos.x, -B.x - 0.3, B.x + 0.3);
      pos.z = MathUtils.clamp(pos.z, B.zMin - 0.3, B.zMax + 0.3);
      const speed = Math.hypot(vel.x, vel.z);
      dada.root.position.x = pos.x; dada.root.position.z = pos.z;

      if (pointer.has) dada.look(MathUtils.clamp((pointer.x - pos.x) / 3, -1, 1), MathUtils.clamp(-(pointer.z - pos.z) / 4, -1, 1));
      game.update(dt, ctx);
      dada.update(dt, speed, MathUtils.clamp(vel.x / maxSpeed, -1, 1), Math.atan2(vel.x, vel.z));

      // 移动留痕
      if (speed > 1) {
        ripTimer -= dt; trailTimer -= dt;
        if (ripTimer <= 0) { ripTimer = 0.34; ctx.ripple(pos.x, pos.z); }
        if (sparks && trailTimer <= 0) {
          trailTimer = 0.05;
          sparks.emit(pos.x - vel.x * 0.05 + (Math.random() - 0.5) * 0.4, 0.15 + Math.random() * 0.25, pos.z - vel.z * 0.05 + (Math.random() - 0.5) * 0.4,
            -vel.x * 0.1, 0.25 + Math.random() * 0.3, -vel.z * 0.1, 0.55, 22 + speed * 3, cTrail, 0);
        }
      }

      // 接触阴影随跳起高度缩小变淡
      blob.position.x = pos.x; blob.position.z = pos.z;
      const lift = Math.max(0, dada.root.position.y);
      blob.scale.setScalar(Math.max(0.5, 1 - lift * 0.3));
      blobMat.opacity = Math.max(0.05, 0.2 - lift * 0.09);

      ground.update(dt, reduce ? 0 : time, pos);
      sparks?.update(dt);
      confetti?.update(dt);
      if (!reduce) motes.update(dt);

      // 相机：看向场地中心，随指针轻微视差；阴影相机跟着取景中心
      const ck = Math.min(1, dt * 3);
      cam.x += (camT.x - cam.x) * ck; cam.y += (camT.y - cam.y) * ck;
      const px = reduce ? 0 : cam.x * 0.5, py = reduce ? 0 : cam.y * 0.2;
      const lookY = V.lookY ?? 0.7, lookZ = V.lookZ ?? 0;
      camera.position.set(px, lookY + Math.sin(elev) * dist + py, lookZ + Math.cos(elev) * dist);
      camera.lookAt(px * 0.3, lookY, lookZ);

      // 量表跟着嗒嗒头顶
      if (meterSpec) {
        const s = toScreen(pos.x, dada.root.position.y + DADA_H + 0.75, pos.z);
        meterEl.style.transform = `translate(${s.x}px, ${s.y}px) translate(-50%, -100%)`;
      }

      renderer.render(scene, camera);
      if (visible) raf = requestAnimationFrame(loop);
    };
    const kick = () => { if (visible && !raf) { last = performance.now(); raf = requestAnimationFrame(loop); } };
    raf = requestAnimationFrame(loop);
    const io = new IntersectionObserver(([e]) => { visible = e.isIntersecting && !document.hidden; kick(); });
    io.observe(host);
    const onVis = () => { visible = !document.hidden; kick(); };
    document.addEventListener("visibilitychange", onVis);

    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect(); io.disconnect();
      document.removeEventListener("visibilitychange", onVis);
      cv.removeEventListener("pointermove", onMove);
      cv.removeEventListener("pointerdown", onDown);
      cv.removeEventListener("pointerup", onUp);
      cv.removeEventListener("pointercancel", onUp);
      cv.removeEventListener("pointerleave", onLeave);
      resetRef.current = () => {};
      popsEl.replaceChildren();
      meterEl.dataset.show = "";
      game.dispose(); dada.dispose(); ground.dispose(); sparks?.dispose(); confetti?.dispose(); motes.dispose();
      blobGeo.dispose(); blobMat.dispose(); blobTex.dispose(); glowTex.dispose();
      sun.shadow.map?.dispose();
      envTex.dispose(); pmrem.dispose();
      renderer.dispose();
      renderer.forceContextLoss();
      cv.remove();
    };
    // 换世界 / 明暗：整场重建（分数在父组件按世界分别记）
  }, [world, theme, onUnsupported]);

  return (
    <div className="pg-host" ref={hostRef}>
      <div className="pg-pops" ref={popsRef} aria-hidden />
      <div className="pg-meter" ref={meterRef} aria-hidden>
        <div className="pg-meter-track"><div className="pg-meter-band" /><div className="pg-meter-needle" /></div>
        <div className="pg-meter-label" />
      </div>
    </div>
  );
}
