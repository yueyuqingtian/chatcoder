/** 首页「对话邮票」（plan-20261009-ui U2）。
 *
 * 主题：在对话中即可完成编码——每枚邮票是「一次对话里办成的一件事」，大字是那件事的动词。
 * 造型：小而清爽——浅色纸张 + 细齿孔 + 一幅像素插画 + 一个大字 + 一枚淡淡的邮戳，再没有别的装饰（全部 SVG，无位图）。
 *
 * 机制（借鉴参考图，造型是本软件自己的）：
 *  · 闲置   —— 灰度：像一枚安静放在桌面上的黑白邮票；
 *  · 聚焦   —— 悬停 / 键盘聚焦：恢复彩色、轻轻抬起，邮戳「盖」下来；
 *             **鼠标指针变成这枚邮票故事里的小物件**（铅笔、扳手、烧瓶……见 stampCursors.ts）；
 *  · 浮签   —— 聚焦时出现在邮票上方，**随鼠标移动而倾斜**：像挂在邮票上的纸条，左右摇摆、上下俯仰，
 *             弹簧惯性，过冲后回落，鼠标移开即停；
 *  · 点击   —— 邮票绕竖轴翻转，在 90° 处换成下一枚（方向键、下方圆点同样可切换）；一天一枚，刷新不换。
 * 倾斜由 rAF 弹簧模拟逐帧写入 CSS 变量（--nx/--ny/--ax），只在悬停与回落期间运行，静止即停。 */
import { useEffect, useId, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { IconMessageSquare } from "../icons";
import { motionDisabled } from "../ui/usePresence";
import { STAMPS, stampRuns, todayStampIndex, type StampDef } from "./stampArt";
import { stampCursorCss } from "./stampCursors";

/* 版面常量（viewBox 单位；8 的倍数以保证齿孔对称） */
const W = 224;
const H = 128;
const TILE = 96; // 像素画占位边长 = 32 × 3（整数倍，1x 屏上每个像素点都是整齐的 3px 方块）
const TX = 16;
const TY = 16;
const CX = 166; // 右栏中线
const PCY = 88; // 邮戳圆心

const HOLES: Array<[number, number]> = (() => {
  const out: Array<[number, number]> = [];
  for (let x = 4; x < W; x += 8) out.push([x, 0], [x, H]);
  for (let y = 12; y < H - 8; y += 8) out.push([0, y], [W, y]);
  return out;
})();

/** 邮戳中央的迷你 logo（与 AppLogo 的线稿同形：对话气泡 + </>） */
function MiniLogo({ cx, cy, size }: { cx: number; cy: number; size: number }) {
  const s = size / 512;
  return (
    <g transform={`translate(${cx - size / 2} ${cy - size / 2}) scale(${s})`} fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round">
      <rect x="128" y="138" width="256" height="180" rx="46" strokeWidth="28" />
      <path d="M206 314 L168 372 L262 314 Z" fill="currentColor" strokeWidth="6" />
      <path d="M186 228 L218 204 M186 228 L218 252 M272 194 L240 262 M326 228 L294 204 M326 228 L294 252" strokeWidth="24" />
    </g>
  );
}

function StampSvg({ def }: { def: StampDef }) {
  const uid = useId().replace(/[^a-zA-Z0-9_-]/g, "");
  const runs = useMemo(() => stampRuns(def), [def]);
  const unit = TILE / 32;
  const R = 23;
  const textR = 18.6;
  const ringPath = `M ${CX - textR} ${PCY} a ${textR} ${textR} 0 1 1 ${textR * 2} 0 a ${textR} ${textR} 0 1 1 ${-textR * 2} 0`;
  return (
    <svg className="es-stamp-svg" viewBox={`0 0 ${W} ${H}`} width={W} height={H} aria-hidden>
      <defs>
        <mask id={`mk-${uid}`}>
          <rect width={W} height={H} fill="#fff" />
          {HOLES.map(([x, y], i) => <circle key={i} cx={x} cy={y} r={2.3} fill="#000" />)}
        </mask>
        <clipPath id={`ct-${uid}`}><rect x={TX} y={TY} width={TILE} height={TILE} rx="4" /></clipPath>
        <path id={`ring-${uid}`} d={ringPath} />
      </defs>

      {/* 纸张：只有一层，齿孔处自然透出背景；投影由 CSS drop-shadow 沿齿孔轮廓生成 */}
      <g mask={`url(#mk-${uid})`}><rect width={W} height={H} style={{ fill: "var(--stamp-paper)" }} /></g>

      {/* 像素插画 */}
      <g clipPath={`url(#ct-${uid})`}>
        <g transform={`translate(${TX} ${TY})`} shapeRendering="crispEdges">
          {runs.map((r, i) => (
            <rect key={i} x={r.x * unit} y={r.y * unit} width={r.w * unit + 0.3} height={unit + 0.3} fill={r.color} />
          ))}
        </g>
      </g>
      <rect x={TX} y={TY} width={TILE} height={TILE} rx="4" fill="none" stroke="#000" strokeOpacity="0.1" strokeWidth="1" />

      {/* 大字：这枚邮票的「面值」 */}
      <text
        x={CX}
        y="58"
        textAnchor="middle"
        fontSize="42"
        fontFamily="KaiTi, STKaiti, 'Kaiti SC', 'Songti SC', SimSun, serif"
        style={{ fill: "var(--stamp-ink)", stroke: "var(--stamp-ink)" }}
        strokeWidth="0.8"
        strokeLinejoin="round"
      >
        {def.char}
      </text>

      {/* 邮戳：双环 + 环形文字 + 中央 logo；正片叠底，像油墨压在纸上；聚焦时重新「盖」一次 */}
      <g className="es-postmark" style={{ color: "var(--stamp-mark)", transformOrigin: `${CX}px ${PCY}px`, mixBlendMode: "multiply" }}>
        <g transform={`rotate(-14 ${CX} ${PCY})`}>
          <circle cx={CX} cy={PCY} r={R} fill="none" stroke="currentColor" strokeWidth="1.2" />
          <circle cx={CX} cy={PCY} r={R - 8.4} fill="none" stroke="currentColor" strokeWidth="0.7" />
          <text fontSize="4.2" fontWeight="700" fill="currentColor" fontFamily="'Courier New', monospace" dominantBaseline="middle">
            <textPath href={`#ring-${uid}`} startOffset="0" textLength={textR * 2 * Math.PI - 2} lengthAdjust="spacing">
              {`${def.caption.toUpperCase()} · CHATCODER · 2026 ·`}
            </textPath>
          </text>
          <MiniLogo cx={CX} cy={PCY} size={14} />
        </g>
      </g>
    </svg>
  );
}

/** 弹簧跟手：把指针相对邮票中心的位置（-1..1）经弹簧模拟写入 CSS 变量，静止即停 */
function useSwing(wrapRef: React.RefObject<HTMLElement | null>) {
  const st = useRef({ x: 0, y: 0, vx: 0, vy: 0, tx: 0, ty: 0, raf: 0, last: 0 });

  const write = () => {
    const el = wrapRef.current;
    if (!el) return;
    const s = st.current;
    el.style.setProperty("--nx", s.x.toFixed(4));
    el.style.setProperty("--ny", s.y.toFixed(4));
    el.style.setProperty("--ax", Math.abs(s.x).toFixed(4));
  };

  const tick = (now: number) => {
    const s = st.current;
    const dt = Math.min(2.5, Math.max(0.2, (now - (s.last || now - 16.7)) / 16.7));
    s.last = now;
    // 偏软偏弹：K 决定追随速度，D 决定阻尼；约两三次衰减摆动后落定
    const K = 0.09;
    const D = 0.84;
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
      kick();
    },
    leave() {
      st.current.tx = 0;
      st.current.ty = 0;
      kick();
    },
  };
}

export function StampHero() {
  const [idx, setIdx] = useState(() => todayStampIndex());
  const [phase, setPhase] = useState<"idle" | "out" | "in">("idle");
  const wrapRef = useRef<HTMLDivElement>(null);
  const timers = useRef<number[]>([]);
  const swing = useSwing(wrapRef);
  const def = STAMPS[idx];
  /** 聚焦时的鼠标指针：这枚邮票故事里的小物件（取不到则保持默认手型） */
  const cursor = useMemo(() => stampCursorCss(def.id), [def.id]);

  useEffect(() => () => { timers.current.forEach((t) => window.clearTimeout(t)); }, []);

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
  const step = (delta: number) => goTo((idx + delta + STAMPS.length) % STAMPS.length);

  return (
    <div ref={wrapRef} className="es-stamp-wrap" data-phase={phase}>
      {/* 浮签：外层负责出现 / 消失，sway 层负责整体摇摆（绕邮票上方的支点），card 层负责 3D 俯仰 */}
      <div className="es-note" role="note">
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
        className="es-stamp"
        style={cursor ? { cursor } : undefined}
        onClick={() => step(1)}
        onPointerMove={swing.move}
        onPointerLeave={swing.leave}
        onKeyDown={(e) => {
          if (e.key === "ArrowRight") { e.preventDefault(); step(1); }
          else if (e.key === "ArrowLeft") { e.preventDefault(); step(-1); }
        }}
        aria-label={`对话邮票「${def.char}」：${def.title}。点击换一枚`}
      >
        <span className="es-stamp-tilt">
          <span className="es-stamp-flip">
            <StampSvg def={def} />
          </span>
        </span>
      </button>
      {/* 圆点：8 枚邮票的位置，点哪枚翻哪枚；聚焦邮票时才浮现，固定占位不让版面跳动 */}
      <div className="es-dots" role="group" aria-label="切换邮票">
        {STAMPS.map((s, i) => (
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
