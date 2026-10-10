/** 首页「对话邮票」素材（plan-20261009-ui U2）。
 *
 * 主题：在对话中即可完成编码。一套 8 枚邮票，每枚是「一次对话里办成的一件事」——
 * 大字是那件事的动词（改 / 修 / 测 / 懂 / 重 / 夜 / 建 / 审，相当于邮票的面值），
 * 插画是 32×32 像素画（pixel.ts 绘制，带明暗与描边），浮签是一句俏皮标题 + 一两句有内涵的话 +
 * 「你可以这样说」的示例对话。 */
import { Px, darken, fromRows, lighten, sky, sparkle, type PixelRun } from "./pixel";

export interface StampDef {
  id: string;
  /** 大字（邮票「面值」位置）：一个动词 */
  char: string;
  /** 英文说明（右下小字，≤ 2 行） */
  caption: string;
  /** 你可以这样对软件说（浮签里展示） */
  ask: string;
  title: string;
  body: string;
  art: () => Px;
}

/* ── 8 幅插画（32×32）─────────────────────────────────────── */

/** 改：登录页的按钮，光标点在圆角按钮上 */
function artRadius(): Px {
  const p = new Px();
  sky(p, "#D9F2EB", "#BFE5DA", 23);
  // 登录卡片
  const card = new Px();
  card.roundRect(3, 4, 24, 24, 2, "#FFFFFF");
  card.rect(6, 7, 9, 2, "#C3CFD7");
  card.roundRect(6, 11, 18, 3, 1, "#E9EFF2").rect(8, 12, 6, 1, "#B4C2CC");
  card.roundRect(6, 15, 18, 3, 1, "#E9EFF2").rect(8, 16, 9, 1, "#B4C2CC");
  p.blit(card.outlined("#6FA799"));
  // 圆角按钮（亮边 + 暗边 + 白色文字条）
  const btn = new Px();
  btn.roundRect(6, 20, 16, 6, 3, "#4C8DF6");
  btn.rect(8, 20, 12, 1, "#8DBAFF");
  btn.rect(8, 25, 12, 1, "#2D69CE");
  btn.rect(10, 22, 8, 2, "#FFFFFF");
  p.blit(btn.outlined("#1D4C9E"));
  // 鼠标指针
  p.blit(fromRows([
    "X.......",
    "XX......",
    "XWX.....",
    "XWWX....",
    "XWWWX...",
    "XWWWWX..",
    "XWWWXXX.",
    "XWXWX...",
    "XX.XWX..",
    "...XWX..",
    "....XX..",
  ], { X: "#1E2A33", W: "#FFFFFF" }), 18, 21);
  sparkle(p, 29, 8, "#FFD54A", 2);
  sparkle(p, 29, 8, "#FFFFFF", 0);
  return p;
}

/** 修：瓢虫（Bug）被打上绿色对勾 */
function artBug(): Px {
  const p = new Px();
  sky(p, "#FAF0CC", "#EFDDA8", 25);
  const leg = "#2A2A2E";
  for (const [x0, y0, x1, y1] of [[7, 13, 12, 16], [5, 19, 11, 19], [7, 26, 12, 23]] as const) {
    p.line(x0, y0, x1, y1, leg);
    p.line(31 - x0, y0, 31 - x1, y1, leg);
  }
  // 身体
  const body = new Px();
  body.ellipse(16, 19.5, 8.5, 9.5, "#E5483D");
  p.blit(body.outlined("#5B1712"));
  p.ellipse(12.8, 14.6, 3.2, 2.2, "#F7857B");
  p.ellipse(12.2, 14.2, 1.4, 1, "#FFB3AA");
  p.rect(15, 11, 2, 18, "#8E241D");
  for (const [x, y] of [[11, 17], [20, 17], [10, 23], [21, 23], [13, 27], [18, 27]]) p.disc(x + 0.5, y + 0.5, 1.7, "#241F21");
  // 头 + 触角
  const head = new Px();
  head.ellipse(16, 9.6, 4.8, 3.4, "#2A2A2E");
  p.blit(head.outlined("#111114"));
  p.set(14, 9, "#FFFFFF").set(18, 9, "#FFFFFF");
  p.line(14, 6, 11, 3, leg).line(18, 6, 21, 3, leg).set(10, 2, leg).set(22, 2, leg);
  // 已修复的对勾徽章
  const badge = new Px();
  badge.disc(26, 6, 4.4, "#35C46F");
  p.blit(badge.outlined("#176B3A"));
  for (const [x, y] of [[23, 6], [24, 7], [25, 6], [26, 5], [27, 4]]) p.set(x, y, "#FFFFFF");
  p.set(24, 6, "#FFFFFF").set(25, 7, "#FFFFFF");
  return p;
}

/** 测：清单板，三项全绿 */
function artTest(): Px {
  const p = new Px();
  sky(p, "#DEF2DC", "#C8E7C4", 24);
  const board = new Px();
  board.roundRect(5, 5, 22, 25, 2, "#C8935A");
  board.rect(6, 6, 20, 1, "#DDAE76");
  p.blit(board.outlined("#6F4C2A"));
  p.rect(7, 8, 18, 20, "#FFFFFF");
  p.rect(7, 27, 18, 1, "#DDE4EA");
  for (const y of [11, 16, 21]) {
    const box = new Px();
    box.roundRect(9, y, 5, 5, 1, "#35C46F");
    box.rect(10, y, 3, 1, "#6FE09C");
    p.blit(box.outlined("#176B3A"));
    for (const [dx, dy] of [[3, 1], [0, 2], [2, 2], [1, 3]]) p.set(9 + dx, y + dy, "#FFFFFF");
    p.rect(16, y + 1, 7, 1, "#C5CED6");
    p.rect(16, y + 3, 5, 1, "#DCE3E8");
  }
  const clip = new Px();
  clip.roundRect(11, 3, 10, 5, 1, "#8C98A6");
  clip.rect(14, 4, 4, 2, "#DFE5EA");
  p.blit(clip.outlined("#4D5663"));
  sparkle(p, 28, 4, "#FFD54A", 2);
  return p;
}

/** 懂：灯泡亮了，下面是翻开的书（代码行有颜色） */
function artExplain(): Px {
  const p = new Px();
  sky(p, "#E7E1F9", "#D3CBEF", 21);
  // 光线
  const ray = "#FFB300";
  for (const [x, y] of [[16, 1], [16, 2], [9, 3], [10, 4], [23, 3], [22, 4], [6, 9], [7, 9], [25, 9], [26, 9], [8, 14], [9, 13], [24, 14], [23, 13]]) p.set(x, y, ray);
  const bulb = new Px();
  bulb.disc(16, 9.5, 6, "#FFD24A");
  bulb.poly([[12, 12], [20, 12], [19, 16.5], [13, 16.5]], "#FFD24A");
  bulb.rect(13, 17, 6, 2, "#9AA5B1");
  bulb.rect(14, 19, 4, 1, "#7C8693");
  p.blit(bulb.outlined("#B9790A"));
  p.ellipse(13.8, 7.6, 1.8, 2.4, "#FFF3B0");
  p.line(15, 12, 15, 15, "#E09A12").line(17, 12, 17, 15, "#E09A12").set(16, 11, "#E09A12");
  // 书
  const book = new Px();
  book.rect(3, 22, 13, 7, "#FFFFFF");
  book.rect(16, 22, 13, 7, "#FFFFFF");
  book.rect(15, 22, 2, 7, "#CFC8EE");
  book.rect(2, 29, 28, 2, "#6C5CE7");
  p.blit(book.outlined("#3F3598"));
  p.rect(5, 24, 6, 1, "#4C8DF6").rect(5, 26, 8, 1, "#E15B8F");
  p.rect(18, 24, 7, 1, "#E15B8F").rect(18, 26, 5, 1, "#35C46F");
  p.rect(26, 24, 1, 1, "#FFB300");
  return p;
}

/** 重：左边是乱糟糟的积木，右边是整整齐齐的同一批——结构变了，行为没变 */
function artRefactor(): Px {
  const p = new Px();
  sky(p, "#DDECFC", "#CADFF6", 22);
  const colors = ["#F59E0B", "#3B82F6", "#35C46F", "#EC4899", "#8B5CF6"];
  // 乱：错位、不同尺寸
  const messy: Array<[number, number, number, number]> = [[2, 4, 7, 5], [6, 9, 6, 5], [2, 15, 5, 6], [7, 20, 7, 5], [3, 26, 6, 4]];
  messy.forEach(([x, y, w, h], i) => {
    const b = new Px();
    b.rect(x, y, w, h, colors[i]);
    b.rect(x, y, w, 1, lighten(colors[i], 0.35));
    p.blit(b.outlined(darken(colors[i], 0.55)));
  });
  // 整：同色同宽对齐
  colors.forEach((c, i) => {
    const b = new Px();
    const y = 3 + i * 6;
    b.rect(22, y, 9, 5, c);
    b.rect(22, y, 9, 1, lighten(c, 0.35));
    p.blit(b.outlined(darken(c, 0.55)));
  });
  // 箭头
  p.rect(14, 15, 4, 2, "#334155");
  p.poly([[18, 12], [18, 19], [21.2, 15.5]], "#334155");
  return p;
}

/** 夜：月亮、星星，笔记本屏幕还亮着——下班了，对话还在上班 */
function artNight(): Px {
  const p = new Px();
  const top = "#1D2760";
  sky(p, top, "#31408C", 19);
  for (const [x, y, c] of [[3, 3, "#FFFFFF"], [9, 7, "#FFE9A8"], [14, 2, "#FFFFFF"], [28, 13, "#FFFFFF"], [5, 13, "#8EA0E8"], [19, 10, "#8EA0E8"], [12, 11, "#FFFFFF"], [26, 17, "#FFE9A8"], [2, 8, "#8EA0E8"]] as const) p.set(x, y, c);
  // 月牙 + 一圈淡光
  p.disc(23, 8, 7.2, "#27327A");
  p.disc(23, 8, 5.6, "#FFE08A");
  p.disc(25.6, 6.4, 5.1, "#27327A");
  p.set(21, 10, "#F5C85A").set(20, 9, "#F5C85A");
  // 桌面
  p.rect(0, 29, 32, 3, "#262F70");
  for (let x = 0; x < 32; x += 2) p.set(x, 29, "#31408C");
  // 笔记本
  const lap = new Px();
  lap.roundRect(6, 15, 20, 11, 1, "#2A3042");
  lap.roundRect(3, 26, 26, 3, 1, "#B4BECB");
  p.blit(lap.outlined("#0D1233"));
  p.rect(8, 17, 16, 7, "#0E4A47");
  p.rect(10, 18, 5, 1, "#5EEAD4").rect(10, 20, 9, 1, "#34D399").rect(12, 22, 6, 1, "#5EEAD4").rect(19, 22, 1, 1, "#FFFFFF");
  p.rect(13, 26, 6, 1, "#8B96A5");
  // 屏幕漫出的微光
  for (const x of [8, 12, 16, 20, 23]) p.set(x, 14, "#3A4DA0");
  return p;
}

/** 建：火箭升空（从零到跑起来） */
function artScaffold(): Px {
  const p = new Px();
  sky(p, "#BDE3FB", "#E6F4FD", 22);
  // 云
  for (const [cx, cy] of [[6, 26], [25, 27]] as const) {
    p.disc(cx, cy, 3, "#FFFFFF").disc(cx + 3.5, cy - 1, 3.5, "#FFFFFF").disc(cx + 7, cy, 3, "#FFFFFF").rect(cx - 2, cy, 12, 3, "#FFFFFF");
  }
  const rocket = new Px();
  rocket.ellipse(16, 14, 5, 10, "#F4F7FB");
  for (let y = 0; y < rocket.h; y++) for (let x = 19; x < rocket.w; x++) if (rocket.get(x, y)) rocket.set(x, y, "#D3DCE8");
  rocket.poly([[16, 1], [11.2, 9], [20.8, 9]], "#E5483D");
  rocket.poly([[11, 16], [6, 22.5], [11, 22.5]], "#E5483D");
  rocket.poly([[21, 16], [26, 22.5], [21, 22.5]], "#C93A30");
  rocket.rect(14, 24, 4, 2, "#7C8794");
  p.blit(rocket.outlined("#27324A"));
  // 舷窗
  p.ring(16, 13, 3.4, 2.3, "#FFFFFF").disc(16, 13, 2.3, "#4C8DF6").set(15, 12, "#9CC7FF");
  // 火焰
  p.poly([[13, 26], [19, 26], [16, 31.5]], "#FF9A2E");
  p.poly([[14.4, 26], [17.6, 26], [16, 29.8]], "#FFE066");
  sparkle(p, 27, 6, "#FFFFFF", 1);
  sparkle(p, 4, 9, "#FFFFFF", 1);
  return p;
}

/** 审：盾牌 + 对勾（最后一道关） */
function artReview(): Px {
  const p = new Px();
  sky(p, "#FCEAD9", "#F6D7BF", 22);
  const shield = new Px();
  const pts: Array<[number, number]> = [[6, 5], [26, 5], [26, 16], [22.5, 23], [16, 28.5], [9.5, 23], [6, 16]];
  shield.poly(pts, "#3B82F6");
  for (let y = 0; y < shield.h; y++) for (let x = 0; x < 16; x++) if (shield.get(x, y)) shield.set(x, y, "#5B9BFA");
  p.blit(shield.outlined("#1B3F94"));
  // 内描边
  const inner = new Px();
  inner.poly([[8.5, 7.5], [23.5, 7.5], [23.5, 15.5], [20.8, 21], [16, 25], [11.2, 21], [8.5, 15.5]], "#FFFFFF");
  for (let y = 0; y < p.h; y++) for (let x = 0; x < p.w; x++) {
    if (inner.get(x, y) && !inner.get(x - 1, y) ) p.set(x, y, "#8CB9FF");
  }
  // 粗对勾
  for (const [x, y] of [[11, 16], [12, 17], [13, 18], [14, 19], [15, 18], [16, 17], [17, 16], [18, 15], [19, 14], [20, 13], [21, 12]]) {
    p.set(x, y, "#FFFFFF").set(x, y + 1, "#FFFFFF");
  }
  sparkle(p, 4, 25, "#FFD54A", 2);
  sparkle(p, 28, 9, "#FFFFFF", 1);
  return p;
}

/* ── 8 枚邮票 ─────────────────────────────────────────────── */

export const STAMPS: StampDef[] = [
  {
    id: "radius",
    char: "改",
    caption: "Round Corners",
    ask: "把登录按钮改成圆角",
    title: "一句话的事",
    body: "改个圆角，不必再在三层样式表里翻来翻去。你负责说想要什么，翻找的体力活交给对话。",
    art: artRadius,
  },
  {
    id: "http500",
    char: "修",
    caption: "Bug Hunt",
    ask: "接口又 500 了，帮我看看",
    title: "报错，直接丢过来",
    body: "不用先自己把堆栈读三遍。把报错贴进对话，找证据、改代码、跑验证，都在这一场聊天里办完。",
    art: artBug,
  },
  {
    id: "unit-test",
    char: "测",
    caption: "All Green",
    ask: "给 parse() 补上单元测试",
    title: "知道该写，就是不想写",
    body: "测试，是最适合「开口」的活儿——人人都说该写，人人都想晚点再写。",
    art: artTest,
  },
  {
    id: "explain",
    char: "懂",
    caption: "Read Along",
    ask: "这段调度逻辑我看不懂，讲讲",
    title: "不改代码也行",
    body: "把对话当成一位愿意被反复追问的同事：读得懂的讲给你，看不懂的，咱们一起读。",
    art: artExplain,
  },
  {
    id: "refactor",
    char: "重",
    caption: "Same Behavior",
    ask: "重构一下，但行为别变",
    title: "「行为别变」",
    body: "这四个字，是对重构最精准的需求描述。小心翼翼的部分，交给测试和对话。",
    art: artRefactor,
  },
  {
    id: "night",
    char: "夜",
    caption: "Night Shift",
    ask: "每天早上帮我检查依赖更新",
    title: "下班了，对话还在上班",
    body: "说一句，自动化就排好了。明早你打开软件，结论已经在等你。",
    art: artNight,
  },
  {
    id: "scaffold",
    char: "建",
    caption: "Zero to Run",
    ask: "新建一个带登录的 Flask 小项目",
    title: "从零，到跑起来",
    body: "空目录也能开局：把想要的样子说出来，脚手架、依赖、启动命令，一口气替你备齐。",
    art: artScaffold,
  },
  {
    id: "review",
    char: "审",
    caption: "Final Check",
    ask: "上线前再帮我检查一遍有没有漏的",
    title: "最后一道关",
    body: "把「再看一眼」交给对话，眼睛留着看别的。关键改动依然会停下来，等你点头。",
    art: artReview,
  },
];

const runCache = new Map<string, PixelRun[]>();
/** 渲染用的游程（每枚只算一次） */
export function stampRuns(def: StampDef): PixelRun[] {
  let r = runCache.get(def.id);
  if (!r) { r = def.art().runs(); runCache.set(def.id, r); }
  return r;
}

/** 一天一枚：按「当年第几天」轮换，同一天内稳定（刷新不换） */
export function todayStampIndex(now = new Date()): number {
  const start = new Date(now.getFullYear(), 0, 0);
  const day = Math.floor((now.getTime() - start.getTime()) / 86400000);
  return day % STAMPS.length;
}
