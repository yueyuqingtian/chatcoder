/** 首页「嗒嗒徽章」（plan-114-452，取代旧的像素邮票）。
 *
 * 一枚横向的小徽章：左边是随主题世界变化的插画（六个世界六幅，嗒嗒从前景后面探头），
 * 右边是今天这件事的「面值」——一个动词大字 + 英文题注；右下角压一枚圆形印章（环形文字 + 嗒嗒），
 * 把吉祥物和软件自己的语言（聊着聊着，代码就写好了）收在同一个物件里。
 *
 * 交互（全部是指针跟手，静止即停）：
 *  · 整枚徽章 3D 微倾 + 一层高光随指针移动；插画三层按景深视差；嗒嗒的眼睛看向指针；
 *  · 悬停 / 键盘聚焦、且徽章上方放得下时才出现浮签（今天这件事的说明 + 示例对话）；
 *  · 浮签挂在 body 下、用 fixed 定位，不会被首页滚动容器裁切，恒浮在徽章上方：上方放得下时首页为它
 *    预留空间（卡片与热力图下移），放不下则整张隐藏、绝不叠在卡片上；水平方向钳制在首页可视区内；
 *  · 点击换下一枚（绕竖轴翻转），下方圆点同样可切换；一天一枚，刷新不换。
 * 减弱动效档：不做倾斜 / 视差 / 翻转，只保留淡入淡出。 */
import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { createPortal } from "react-dom";
import { IconMessageSquare } from "../icons";
import { Dada } from "../mascot/Dada";
import { motionDisabled } from "../ui/usePresence";
import { useThemeStore } from "../../store/theme";
import { BADGES, todayBadgeIndex } from "./badgeData";
import { makeScene } from "./BadgeScenes";

/** 浮签与可视区边缘的留白、与徽章之间的间距（CSS 像素） */
const NOTE_MARGIN = 8;
const NOTE_GAP = 14;

function clamp(v: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, v));
}

/** 弹簧跟手：把指针相对徽章中心的位置（-1..1）经弹簧模拟写入 CSS 变量，静止即停。
 *  变量写到每个目标元素上：徽章与浮签分属两棵子树（浮签经 portal 挂在 body 下），各自都要拿到。 */
function useSwing(targets: Array<React.RefObject<HTMLElement | null>>, onLook: (x: number, y: number) => void) {
  const st = useRef({ x: 0, y: 0, vx: 0, vy: 0, tx: 0, ty: 0, raf: 0, last: 0 });

  const write = () => {
    const s = st.current;
    for (const ref of targets) {
      const el = ref.current;
      if (!el) continue;
      el.style.setProperty("--nx", s.x.toFixed(4));
      el.style.setProperty("--ny", s.y.toFixed(4));
    }
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
      st.current.tx = clamp((e.clientX - (r.left + r.width / 2)) / (r.width / 2), -1, 1);
      st.current.ty = clamp((e.clientY - (r.top + r.height / 2)) / (r.height / 2), -1, 1);
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
  /** 悬停（指针在徽章组上）与键盘聚焦（focus-visible）分开记 */
  const [hover, setHover] = useState(false);
  const [kbd, setKbd] = useState(false);
  /** 徽章上方放得下浮签（由 measureRoom 量得）。放不下时浮签整张隐藏，不叠在卡片上 */
  const [room, setRoom] = useState(false);
  /** 浮签显示条件：悬停或键盘聚焦任一为真，且上方放得下 */
  const open = (hover || kbd) && room;
  const wrapRef = useRef<HTMLDivElement>(null);
  const badgeRef = useRef<HTMLButtonElement>(null);
  const noteRef = useRef<HTMLDivElement>(null);
  const timers = useRef<number[]>([]);
  const swing = useSwing([wrapRef, noteRef], (x, y) => setLook({ x, y }));
  const def = BADGES[idx];
  const scene = useMemo(() => makeScene(world, uid), [world, uid]);

  useEffect(() => () => { timers.current.forEach((t) => window.clearTimeout(t)); }, []);

  /** 浮签定位：恒在徽章上方（不翻到下方），水平钳制在首页可视区内。
   *  上方的空间由 measureRoom 预留；预留不下时浮签根本不打开（见 open），这里的钳制只作兜底。
   *  可视区 = 首页滚动容器 .es-hero（它的边缘就是裁切线）；拿不到时退回视口。
   *  浮签 fixed 定位在 body 下，位置在打开、换枚、滚动、缩放时重算。 */
  const placeNote = () => {
    const note = noteRef.current;
    const badge = badgeRef.current;
    if (!note || !badge) return;
    const host = badge.closest(".es-hero") as HTMLElement | null;
    const bounds = host
      ? host.getBoundingClientRect()
      : new DOMRect(0, 0, document.documentElement.clientWidth, document.documentElement.clientHeight);
    const r = badge.getBoundingClientRect();
    const w = note.offsetWidth;
    const h = note.offsetHeight;
    const left = clamp(r.left + r.width / 2 - w / 2, bounds.left + NOTE_MARGIN, bounds.right - NOTE_MARGIN - w);
    const top = clamp(r.top - NOTE_GAP - h, bounds.top + NOTE_MARGIN, Math.max(bounds.top + NOTE_MARGIN, bounds.bottom - NOTE_MARGIN - h));
    note.style.left = `${left}px`;
    note.style.top = `${top}px`;
  };

  /** 首页留白：徽章上方放得下浮签时，预留「浮签高度 + 间距」，卡片、标语、热力图随之整体下移。
   *  判据：不含预留的内容高度 + 预留 ≤ 首页可用高度。放不下则不预留，同时 room 置假，浮签整张隐藏（见 open）。
   *  判据刻意不读取预留本身：徽章包裹层带 transition，写入 margin 后立即读取会拿到过渡起点的值，
   *  决定会随读取时机摇摆；这里只累加其余内容的高度与间距，结果与当前是否预留无关。
   *  预留量写在徽章包裹层的 --note-slot 上（margin-top），不改变悬停区域。 */
  const measureRoom = useCallback(() => {
    const wrap = wrapRef.current;
    const note = noteRef.current;
    const inner = wrap?.closest(".es-hero-inner") as HTMLElement | null;
    const hero = wrap?.closest(".es-hero") as HTMLElement | null;
    if (!wrap || !note || !inner || !hero) return;
    let content = 0;
    for (const child of Array.from(inner.children) as HTMLElement[]) {
      const cs = getComputedStyle(child);
      const margin = (child === wrap ? 0 : parseFloat(cs.marginTop) || 0) + (parseFloat(cs.marginBottom) || 0);
      content += child.offsetHeight + margin;
    }
    const ics = getComputedStyle(inner);
    content += (parseFloat(ics.paddingTop) || 0) + (parseFloat(ics.paddingBottom) || 0);
    content += (parseFloat(ics.rowGap) || 0) * Math.max(0, inner.children.length - 1);
    const hcs = getComputedStyle(hero);
    const avail = hero.clientHeight - (parseFloat(hcs.paddingTop) || 0) - (parseFloat(hcs.paddingBottom) || 0);
    const need = note.offsetHeight + NOTE_GAP;
    const fits = content + need <= avail + 1;
    wrap.style.setProperty("--note-slot", fits ? `${need}px` : "0px");
    setRoom(fits);
  }, []);

  // 换枚（浮签文字变化）与首页视口尺寸变化后重算预留；须先于定位执行，定位才基于新的徽章位置
  useLayoutEffect(() => {
    measureRoom();
  }, [idx, world, measureRoom]);

  // 视口变化，或内容撑出 / 收回首页高度（热力图加载、预留增减）时重算：溢出会让内容列的高度变化，故两者都观察
  useEffect(() => {
    const hero = wrapRef.current?.closest(".es-hero") as HTMLElement | null;
    const inner = wrapRef.current?.closest(".es-hero-inner") as HTMLElement | null;
    if (!hero || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => measureRoom());
    ro.observe(hero);
    if (inner) ro.observe(inner);
    return () => ro.disconnect();
  }, [measureRoom]);

  // 打开 / 换枚后定位（布局阶段执行，首帧就在正确位置）
  useLayoutEffect(() => {
    if (open) placeNote();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, idx]);

  // 打开期间：滚动（任何滚动容器）与缩放都要重新定位，否则浮签会离开徽章
  useEffect(() => {
    if (!open) return;
    let raf = 0;
    const onMove = () => {
      window.cancelAnimationFrame(raf);
      raf = window.requestAnimationFrame(placeNote);
    };
    window.addEventListener("resize", onMove);
    window.addEventListener("scroll", onMove, true);
    return () => {
      window.cancelAnimationFrame(raf);
      window.removeEventListener("resize", onMove);
      window.removeEventListener("scroll", onMove, true);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

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

  // 浮签挂到 body 下：不受首页滚动容器与其 overflow 裁切，位置由 placeNote 决定
  const note = createPortal(
    <div ref={noteRef} className="es-note" role="note" data-open={open ? "true" : "false"} aria-hidden={!open}>
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
    </div>,
    document.body,
  );

  return (
    <div
      ref={wrapRef}
      className="es-badge-wrap"
      data-phase={phase}
      data-world={world}
      onPointerEnter={() => setHover(true)}
      onPointerLeave={() => setHover(false)}
    >
      {note}

      <button
        ref={badgeRef}
        type="button"
        className="es-badge"
        onClick={() => step(1)}
        onPointerMove={swing.move}
        onPointerLeave={swing.leave}
        onFocus={(e) => setKbd(e.currentTarget.matches(":focus-visible"))}
        onBlur={() => setKbd(false)}
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
