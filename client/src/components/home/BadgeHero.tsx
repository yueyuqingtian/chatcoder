/** 首页「嗒嗒徽章」（plan-114-452，取代旧的像素邮票）。
 *
 * 一枚横向的小徽章：左边是随主题世界变化的插画（六个世界六幅，嗒嗒从前景后面探头），
 * 右边是今天这件事的「面值」——一个动词大字 + 英文题注；右下角压一枚圆形印章（环形文字 + 嗒嗒），
 * 把吉祥物和软件自己的语言（聊着聊着，代码就写好了）收在同一个物件里。
 *
 * 交互（全部是指针跟手，静止即停）：
 *  · 整枚徽章 3D 微倾 + 一层高光随指针移动；插画三层按景深视差；嗒嗒的眼睛看向指针；
 *  · 悬停 / 键盘聚焦才出现浮签（今天这件事的说明 + 示例对话），并且只在**上方空间够**时显示——
 *    窗口矮到放不下就直接不弹，不会被顶出可视区；
 *  · 点击换下一枚（绕竖轴翻转），下方圆点同样可切换；一天一枚，刷新不换。
 * 减弱动效档：不做倾斜 / 视差 / 翻转，只保留淡入淡出。 */
import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { IconMessageSquare } from "../icons";
import { Dada } from "../mascot/Dada";
import { motionDisabled } from "../ui/usePresence";
import { useThemeStore } from "../../store/theme";
import { BADGES, todayBadgeIndex } from "./badgeData";
import { makeScene } from "./BadgeScenes";

/** 弹簧跟手：把指针相对徽章中心的位置（-1..1）经弹簧模拟写入 CSS 变量，静止即停 */
function useSwing(wrapRef: React.RefObject<HTMLElement | null>, onLook: (x: number, y: number) => void) {
  const st = useRef({ x: 0, y: 0, vx: 0, vy: 0, tx: 0, ty: 0, raf: 0, last: 0 });

  const write = () => {
    const el = wrapRef.current;
    if (!el) return;
    const s = st.current;
    el.style.setProperty("--nx", s.x.toFixed(4));
    el.style.setProperty("--ny", s.y.toFixed(4));
  };

  const tick = (now: number) => {
    const s = st.current;
    const dt = Math.min(2.5, Math.max(0.2, (now - (s.last || now - 16.7)) / 16.7));
    s.last = now;
    const K = 0.09, D = 0.82;
    s.vx = (s.vx + (s.tx - s.x) * K * dt) * Math.pow(D, dt);
    s.vy = (s.vy + (s.ty - s.y) * K * dt) * Math.pow(D, dt);
    s.x += s.vx * dt;
    s.y += s.vy * dt;
    write();
    const rest = Math.abs(s.vx) < 0.0006 && Math.abs(s.vy) < 0.0006 && Math.abs(s.tx - s.x) < 0.0006 && Math.abs(s.ty - s.y) < 0.0006;
    if (rest) { s.raf = 0; s.last = 0; s.x = s.tx; s.y = s.ty; write(); return; }
    s.raf = requestAnimationFrame(tick);
  };
  const kick = () => { if (!st.current.raf) st.current.raf = requestAnimationFrame(tick); };

  useEffect(() => () => { if (st.current.raf) cancelAnimationFrame(st.current.raf); }, []);

  return {
    move(e: ReactPointerEvent<HTMLElement>) {
      if (motionDisabled() || e.pointerType === "touch") return;
      const r = e.currentTarget.getBoundingClientRect();
      const clamp = (v: number) => Math.max(-1, Math.min(1, v));
      st.current.tx = clamp((e.clientX - (r.left + r.width / 2)) / (r.width / 2));
      st.current.ty = clamp((e.clientY - (r.top + r.height / 2)) / (r.height / 2));
      onLook(st.current.tx, st.current.ty);
      kick();
    },
    leave() {
      st.current.tx = 0;
      st.current.ty = 0;
      onLook(0, 0);
      kick();
    },
  };
}

/** 印章：环形文字 + 中央嗒嗒（颜色走 --bd-seal，随世界变化） */
function Seal({ uid }: { uid: string }) {
  const id = `bd-ring-${uid}`;
  return (
    <span className="es-badge-seal" aria-hidden>
      <svg viewBox="0 0 80 80" width="100%" height="100%">
        <defs><path id={id} d="M40 40m-28 0a28 28 0 1 1 56 0a28 28 0 1 1-56 0" /></defs>
        <circle cx="40" cy="40" r="38.5" className="bd-seal-face" />
        <circle cx="40" cy="40" r="36" fill="none" className="bd-seal-rim" strokeWidth="1" />
        <circle cx="40" cy="40" r="21.5" fill="none" className="bd-seal-rim" strokeWidth="1" opacity=".6" />
        <text className="bd-seal-text" fontSize="6.6" letterSpacing="0.6">
          <textPath href={`#${id}`} textLength="170" lengthAdjust="spacing">CHATCODER · 聊着聊着，代码就写好了 ·</textPath>
        </text>
      </svg>
      <span className="es-badge-seal-dada"><Dada variant="icon" size={30} blink={false} /></span>
    </span>
  );
}

export function BadgeHero() {
  const world = useThemeStore((s) => s.world);
  const uid = useId().replace(/[^a-zA-Z0-9_-]/g, "");
  const [idx, setIdx] = useState(() => todayBadgeIndex());
  const [phase, setPhase] = useState<"idle" | "out" | "in">("idle");
  const [look, setLook] = useState({ x: 0, y: 0 });
  /** 浮签上方是否放得下：放不下就不弹（data-room="no"） */
  const [room, setRoom] = useState(true);
  const wrapRef = useRef<HTMLDivElement>(null);
  const noteRef = useRef<HTMLDivElement>(null);
  const timers = useRef<number[]>([]);
  const swing = useSwing(wrapRef, (x, y) => setLook({ x, y }));
  const def = BADGES[idx];
  const scene = useMemo(() => makeScene(world, uid), [world, uid]);

  useEffect(() => () => { timers.current.forEach((t) => window.clearTimeout(t)); }, []);

  /** 量一下徽章上方到可视区（滚动容器）顶部的距离，够放浮签才显示 */
  const measureRoom = () => {
    const wrap = wrapRef.current, note = noteRef.current;
    if (!wrap || !note) return;
    const host = wrap.closest(".es-hero") as HTMLElement | null;
    const top = host ? host.getBoundingClientRect().top : 0;
    const need = note.offsetHeight + 22 + 12;
    setRoom(wrap.getBoundingClientRect().top - top >= need);
  };
  useLayoutEffect(() => {
    measureRoom();
    const onResize = () => measureRoom();
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [idx]);

  const goTo = (target: number) => {
    if (phase !== "idle" || target === idx) return;
    if (motionDisabled()) { setIdx(target); return; }
    setPhase("out");
    timers.current.push(window.setTimeout(() => {
      setIdx(target);
      setPhase("in");
      timers.current.push(window.setTimeout(() => setPhase("idle"), 480));
    }, 150));
  };
  const step = (delta: number) => goTo((idx + delta + BADGES.length) % BADGES.length);
  const d = scene.dada;

  return (
    <div ref={wrapRef} className="es-badge-wrap" data-phase={phase} data-room={room ? "yes" : "no"} data-world={world} onPointerEnter={measureRoom}>
      {/* 浮签：只在悬停 / 聚焦且上方放得下时出现 */}
      <div ref={noteRef} className="es-note" role="note">
        <div className="es-note-sway">
          <div className="es-note-card">
            <div className="es-note-title">{def.title}</div>
            <p className="es-note-body">{def.body}</p>
            <div className="es-note-say">
              <IconMessageSquare size={13} />
              <span>试试说：「{def.ask}」</span>
            </div>
          </div>
        </div>
      </div>

      <button
        type="button"
        className="es-badge"
        onClick={() => step(1)}
        onPointerMove={swing.move}
        onPointerLeave={swing.leave}
        onKeyDown={(e) => {
          if (e.key === "ArrowRight") { e.preventDefault(); step(1); }
          else if (e.key === "ArrowLeft") { e.preventDefault(); step(-1); }
        }}
        aria-label={`嗒嗒徽章「${def.char}」：${def.title}。点击换一枚`}
      >
        <span className="es-badge-tilt">
          <span className="es-badge-flip">
            <span className="es-badge-plaque">
              <span className="es-badge-tile">
                <span className="bd-layer" style={{ ["--d" as string]: -2 }}>{scene.back}</span>
                <span className="bd-layer" style={{ ["--d" as string]: -5 }}>{scene.mid}</span>
                <span
                  className="bd-dada"
                  style={{ left: `${d.x}%`, top: `${d.y}%`, width: `${d.size}%`, ["--d" as string]: 2 }}
                >
                  <Dada variant="rich" mood={d.mood} size={200} look={look} style={{ width: "100%", height: "auto" }} />
                </span>
                <span className="bd-layer" style={{ ["--d" as string]: 6 }}>{scene.front}</span>
              </span>
              <span className="es-badge-info">
                <span className="eb-no">No.{String(idx + 1).padStart(2, "0")}<i>/{String(BADGES.length).padStart(2, "0")}</i></span>
                <span className="eb-char">{def.char}</span>
                <span className="eb-cap">{def.caption}</span>
              </span>
              <span className="es-badge-sheen" />
            </span>
          </span>
          <Seal uid={uid} />
        </span>
      </button>

      {/* 圆点：8 枚的位置；聚焦徽章时才浮现，固定占位不让版面跳动 */}
      <div className="es-dots" role="group" aria-label="切换徽章">
        {BADGES.map((s, i) => (
          <button
            key={s.id}
            type="button"
            className={"es-dot" + (i === idx ? " is-on" : "")}
            aria-label={`第 ${i + 1} 枚：${s.char}`}
            aria-current={i === idx}
            onClick={() => goTo(i)}
          />
        ))}
      </div>
    </div>
  );
}
