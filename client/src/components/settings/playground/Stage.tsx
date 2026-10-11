/** 乐园 3D 舞台（plan-114-452）：three.js 场景 + 嗒嗒角色 + 鼠标驱动 + 特效。
 * 本文件及 three 只经 React.lazy 加载（PlaygroundPanel），不进首屏包。
 *
 * 画面：自定义 shader 地面（水面焦散 / 林间光斑 / 草浪 / 余烬 / 落英 / 网格，随主题世界变化）、
 *       RoomEnvironment 环境反射（嗒嗒是软胶质感）、ACES 色调映射、additive 光点（拖尾、爆发、环境萤火）、
 *       涟漪圆环、相机随指针轻微视差。
 * 交互：鼠标移动，嗒嗒跟着跑（弹簧追随、跳步、挤压回弹、眼睛看向指针，移动时留下涟漪与拖尾）；
 *       点击地面起跳并改去那里；点嗒嗒本人被「戳」一下冒出小心心；碰到发光星星即收集（爆发 + 笑眼）。
 * 生命周期：离开页面 / 窗口隐藏 / 滚出视口即停帧；卸载时释放几何、材质、贴图、环境图、渲染器与 canvas。
 * 降级：不支持 WebGL 时回调 onUnsupported；prefers-reduced-motion 下去掉跳步、呼吸、眨眨眼、漂浮、拖尾与粒子。 */
import { useEffect, useRef } from "react";
import {
  ACESFilmicToneMapping, AmbientLight, Color, DirectionalLight, HemisphereLight, MathUtils, Mesh, MeshBasicMaterial,
  PMREMGenerator, PerspectiveCamera, Plane, PlaneGeometry, Raycaster, SRGBColorSpace, Scene, Vector2, Vector3, WebGLRenderer,
} from "three";
import { RoomEnvironment } from "three/examples/jsm/environments/RoomEnvironment.js";
import { makeColorResolver } from "./colors";
import { Dada3D } from "./dada3d";
import { Motes, Sparks, makeGlowTexture } from "./fx";
import { BOUNDS, World3D, makeBlobTexture, type Palette } from "./scene3d";

const ELEVATION = (27 * Math.PI) / 180;
const FOV = 32;

/** 环境光点的运动方式与颜色取自哪个调色板槽 */
const MOTE: Record<string, { mode: "float" | "rise" | "fall"; n: number; color: keyof Palette }> = {
  graphite: { mode: "float", n: 46, color: "accent" },
  forest: { mode: "rise", n: 60, color: "accent2" },
  campfire: { mode: "rise", n: 70, color: "accent" },
  breeze: { mode: "float", n: 50, color: "cloud" },
  tide: { mode: "rise", n: 54, color: "cloud" },
  sakura: { mode: "fall", n: 56, color: "accent" },
};

export default function Stage({ world, theme, resetKey, onCollect, onInteract, onUnsupported }: {
  world: string;
  theme: string;
  /** 递增即清场：星星重新散落、嗒嗒回到中央 */
  resetKey: number;
  /** 每次收集到星星回传 +n */
  onCollect: (n: number) => void;
  /** 第一次有交互（移动 / 点击）时回调一次，供外层淡出起手提示 */
  onInteract: () => void;
  onUnsupported: () => void;
}) {
  const hostRef = useRef<HTMLDivElement>(null);
  const onCollectRef = useRef(onCollect);
  onCollectRef.current = onCollect;
  const onInteractRef = useRef(onInteract);
  onInteractRef.current = onInteract;
  const resetRef = useRef<() => void>(() => {});
  /** 角色位置跨「换主题重建场景」保留，避免换个世界就被传送回中央 */
  const posRef = useRef({ x: 0, z: 0.6 });

  useEffect(() => { resetRef.current(); }, [resetKey]);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const reduce = !!window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

    let renderer: WebGLRenderer;
    try {
      renderer = new WebGLRenderer({ antialias: true, alpha: true, powerPreference: "high-performance" });
    } catch {
      onUnsupported();
      return;
    }
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.outputColorSpace = SRGBColorSpace;
    renderer.toneMapping = ACESFilmicToneMapping;
    renderer.toneMappingExposure = theme === "dark" ? 1.0 : 1.0;
    renderer.setClearColor(0x000000, 0);
    const cv = renderer.domElement;
    cv.className = "pg-canvas";
    cv.setAttribute("aria-label", "嗒嗒乐园：移动鼠标，嗒嗒会跟过去；点击地面让它跳一下；碰到发光的小星星就能收集");
    host.appendChild(cv);

    // ── 配色：读舞台上的 --pg-* 变量（随世界 / 明暗变化） ──
    const rgb = makeColorResolver(host);
    const pal: Palette = {
      skyBottom: rgb("--pg-sky-bottom", "#fafafa"),
      ground: rgb("--pg-ground", "#ececec"),
      groundLine: rgb("--pg-ground-line", "#cccccc"),
      prop: rgb("--pg-prop", "#dddddd"),
      cloud: rgb("--pg-cloud", "#ffffff"),
      accent: rgb("--accent", "#33aa66"),
      accent2: rgb("--world-2", "#ddaa55"),
    };
    host.style.color = "";
    const dark = theme === "dark";

    const scene = new Scene();
    const camera = new PerspectiveCamera(FOV, 1, 0.1, 120);

    // 环境反射：房间式环境图，让软胶质感的嗒嗒有柔和的高光与边缘光
    const pmrem = new PMREMGenerator(renderer);
    const envTex = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
    scene.environment = envTex;
    scene.environmentIntensity = dark ? 0.55 : 0.9;

    scene.add(new HemisphereLight(0xffffff, dark ? 0x6a7090 : 0xc9d1ea, dark ? 0.9 : 1.0));
    const sun = new DirectionalLight(0xfff4e6, dark ? 1.3 : 1.7);
    sun.position.set(-4, 7, 6);
    scene.add(sun);
    scene.add(new AmbientLight(0xffffff, dark ? 0.25 : 0.15));

    const glowTex = makeGlowTexture();
    const worldObj = new World3D(world, pal, reduce, glowTex);
    scene.add(worldObj.root);
    const dada = new Dada3D(reduce);
    if (world === "tide") dada.setFloat(true, "#ff8a6b", "#ffffff");
    scene.add(dada.root);

    // 粒子：事件粒子池 + 环境光点（减弱动效档不创建）
    const sparks = reduce ? null : new Sparks(260);
    if (sparks) scene.add(sparks.obj);
    const moteCfg = MOTE[world] ?? MOTE.graphite;
    const motes = new Motes(reduce ? 0 : moteCfg.n, new Color(pal[moteCfg.color]), moteCfg.mode);
    scene.add(motes.obj);
    const cStar = new Color(pal.accent2), cTrail = new Color(pal.accent2).lerp(new Color(0xffffff), 0.5), cHeart = new Color("#ff8fa8");

    // 角色脚下的软阴影
    const blobTex = makeBlobTexture();
    const blobMat = new MeshBasicMaterial({ map: blobTex, transparent: true, opacity: 0.26, depthWrite: false });
    const blobGeo = new PlaneGeometry(2, 2);
    const blob = new Mesh(blobGeo, blobMat);
    blob.rotation.x = -Math.PI / 2;
    blob.position.y = 0.012;
    scene.add(blob);

    // ── 状态 ──
    const pos = posRef.current;
    dada.root.position.set(pos.x, 0, pos.z);
    const target = { x: pos.x, z: pos.z };
    const vel = { x: 0, z: 0 };
    let hasPointer = false, interacted = false;
    const ndc = new Vector2();
    const ray = new Raycaster();
    const ground = new Plane(new Vector3(0, 1, 0), 0);
    const hit = new Vector3();
    const cam = { x: 0, y: 0 }; // 相机视差（指针在舞台内的归一化位置，弹簧跟随）
    const camT = { x: 0, y: 0 };
    let ripTimer = 0, trailTimer = 0;
    const touched = () => { if (!interacted) { interacted = true; onInteractRef.current(); } };

    const clampT = () => {
      target.x = MathUtils.clamp(target.x, -BOUNDS.x, BOUNDS.x);
      target.z = MathUtils.clamp(target.z, BOUNDS.zMin, BOUNDS.zMax);
    };
    const setNdc = (e: PointerEvent) => {
      const r = cv.getBoundingClientRect();
      ndc.set(((e.clientX - r.left) / r.width) * 2 - 1, -(((e.clientY - r.top) / r.height) * 2 - 1));
    };
    const toGround = (e: PointerEvent): boolean => { setNdc(e); ray.setFromCamera(ndc, camera); return !!ray.ray.intersectPlane(ground, hit); };
    const overDada = (e: PointerEvent): boolean => { setNdc(e); ray.setFromCamera(ndc, camera); return ray.intersectObject(dada.root, true).length > 0; };

    const onMove = (e: PointerEvent) => {
      camT.x = ndc.x; camT.y = ndc.y;
      if (!toGround(e)) return;
      camT.x = ndc.x; camT.y = ndc.y;
      hasPointer = true; touched();
      target.x = hit.x; target.z = hit.z; clampT();
    };
    const onDown = (e: PointerEvent) => {
      touched();
      if (overDada(e)) {
        dada.prod();
        worldObj.addRipple(pos.x, pos.z);
        if (sparks) for (let i = 0; i < 6; i++) sparks.emit(pos.x + (Math.random() - 0.5) * 0.6, 1.9 + Math.random() * 0.3, pos.z + 0.3, (Math.random() - 0.5) * 0.8, 1.2 + Math.random() * 0.8, 0.2, 0.9, 46, cHeart, 0.8);
        return;
      }
      if (toGround(e)) {
        target.x = hit.x; target.z = hit.z; clampT(); dada.jump(); hasPointer = true;
        worldObj.addRipple(target.x, target.z);
      }
    };
    const onLeave = () => { hasPointer = false; dada.look(0, 0); camT.x = 0; camT.y = 0; };
    cv.addEventListener("pointermove", onMove);
    cv.addEventListener("pointerdown", onDown);
    cv.addEventListener("pointerleave", onLeave);

    resetRef.current = () => {
      pos.x = 0; pos.z = 0.6; target.x = 0; target.z = 0.6; vel.x = vel.z = 0;
      dada.root.position.set(pos.x, dada.root.position.y, pos.z);
      worldObj.reset();
      worldObj.addRipple(0, 0.6);
    };

    let dist = 14;
    const resize = () => {
      const w = host.clientWidth, h = host.clientHeight;
      if (!w || !h) return;
      renderer.setSize(w, h, false);
      camera.aspect = w / h;
      // 水平 ±7.4、纵向 ±5.0 的范围都在画面内：窄窗拉远、宽窗按高度定
      const tanV = Math.tan(MathUtils.degToRad(FOV / 2));
      dist = Math.max(7.4 / (tanV * camera.aspect), 5.0 / tanV);
      camera.updateProjectionMatrix();
      // 点精灵像素尺寸 = 世界尺寸 × 画布高 /（2·tan(fov/2)）/ 深度
      const ps = (h * renderer.getPixelRatio()) / (2 * tanV);
      sparks?.setScale(ps); motes.setScale(ps);
    };
    const ro = new ResizeObserver(resize);
    ro.observe(host);
    resize();

    // ── 主循环 ──
    let raf = 0, last = performance.now(), visible = true, time = 0;
    const loop = (now: number) => {
      raf = 0;
      const dt = Math.min(0.05, (now - last) / 1000);
      last = now; time += dt;

      // 追随：弹簧式（距离越远越快，封顶），到达后自然停下
      const dx = target.x - pos.x, dz = target.z - pos.z;
      const dd = Math.hypot(dx, dz);
      const maxSpeed = 5.4;
      const want = dd > 0.12 ? Math.min(maxSpeed, dd * 3.2) : 0;
      const wx = dd > 0 ? (dx / dd) * want : 0, wz = dd > 0 ? (dz / dd) * want : 0;
      const k = Math.min(1, dt * 7);
      vel.x += (wx - vel.x) * k; vel.z += (wz - vel.z) * k;
      pos.x += vel.x * dt; pos.z += vel.z * dt;
      const speed = Math.hypot(vel.x, vel.z);
      dada.root.position.x = pos.x; dada.root.position.z = pos.z;

      if (hasPointer) dada.look(MathUtils.clamp((target.x - pos.x) / 3, -1, 1), MathUtils.clamp(-(target.z - pos.z) / 4, -1, 1));
      dada.update(dt, speed, MathUtils.clamp(vel.x / maxSpeed, -1, 1));

      // 移动留痕：涟漪（慢）+ 拖尾光点（快）
      if (speed > 1) {
        ripTimer -= dt; trailTimer -= dt;
        if (ripTimer <= 0) { ripTimer = 0.32; worldObj.addRipple(pos.x, pos.z); }
        if (sparks && trailTimer <= 0) {
          trailTimer = 0.04;
          sparks.emit(pos.x - vel.x * 0.05 + (Math.random() - 0.5) * 0.4, 0.15 + Math.random() * 0.25, pos.z - vel.z * 0.05 + (Math.random() - 0.5) * 0.4,
            -vel.x * 0.1, 0.25 + Math.random() * 0.3, -vel.z * 0.1, 0.6, 26 + speed * 3, cTrail, 0);
        }
      }

      // 阴影跟随并随跳起高度缩小变淡
      blob.position.x = pos.x; blob.position.z = pos.z;
      const lift = dada.root.position.y;
      blob.scale.setScalar(Math.max(0.55, 1 - lift * 0.3));
      blobMat.opacity = Math.max(0.08, 0.26 - lift * 0.1);

      worldObj.setDada(pos.x, pos.z);
      const got = worldObj.update(dt, { x: pos.x, z: pos.z });
      for (const g of got) {
        dada.jump(); dada.cheer();
        sparks?.burst(g.x, g.y, g.z, 22, cStar, 2.8, 40);
        onCollectRef.current(1);
      }

      sparks?.update(dt);
      if (!reduce) motes.update(dt);

      // 相机：始终看向场地中心，随指针轻微平移（视差，弹簧跟随）
      const ck = Math.min(1, dt * 3);
      cam.x += (camT.x - cam.x) * ck; cam.y += (camT.y - cam.y) * ck;
      const px = reduce ? 0 : cam.x * 0.45, py = reduce ? 0 : cam.y * 0.18;
      camera.position.set(px, 0.5 + Math.sin(ELEVATION) * dist + py, Math.cos(ELEVATION) * dist);
      camera.lookAt(px * 0.3, 0.7, 0);

      renderer.render(scene, camera);
      if (visible) raf = requestAnimationFrame(loop);
    };
    const kick = () => { if (visible && !raf) { last = performance.now(); raf = requestAnimationFrame(loop); } };
    raf = requestAnimationFrame(loop);
    // 不可见（滚出视口 / 窗口隐藏）时停帧，CPU/GPU 归零
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
      cv.removeEventListener("pointerleave", onLeave);
      resetRef.current = () => {};
      dada.dispose(); worldObj.dispose(); sparks?.dispose(); motes.dispose();
      blobGeo.dispose(); blobMat.dispose(); blobTex.dispose(); glowTex.dispose();
      envTex.dispose(); pmrem.dispose();
      renderer.dispose();
      renderer.forceContextLoss();
      cv.remove();
    };
    // 换世界 / 明暗：整场重建（位置保留在 posRef，计数在父组件）
  }, [world, theme, onUnsupported]);

  return <div className="pg-host" ref={hostRef} />;
}
