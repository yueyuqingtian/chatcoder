/** 首页「嗒嗒徽章」素材（plan-114-452）。
 * 一套 8 枚，每枚是「一次对话里办成的一件事」：大字是那件事的动词，浮签是一句俏皮标题 + 一两句话 + 示例对话。
 * 一天一枚，同一天内稳定；点击徽章换下一枚。插画不在这里——由主题世界决定（BadgeScenes.tsx）。 */
export interface BadgeDef {
  id: string;
  /** 大字：一个动词 */
  char: string;
  /** 英文题注 */
  caption: string;
  /** 你可以这样对软件说 */
  ask: string;
  title: string;
  body: string;
}

export const BADGES: BadgeDef[] = [
  { id: "radius", char: "改", caption: "Round Corners", ask: "把登录按钮改成圆角", title: "一句话的事", body: "改个圆角，不必再在三层样式表里翻来翻去。你负责说想要什么，翻找的体力活交给对话。" },
  { id: "http500", char: "修", caption: "Bug Hunt", ask: "接口又 500 了，帮我看看", title: "报错，直接丢过来", body: "不用先自己把堆栈读三遍。把报错贴进对话，找证据、改代码、跑验证，都在这一场聊天里办完。" },
  { id: "unit-test", char: "测", caption: "All Green", ask: "给 parse() 补上单元测试", title: "知道该写，就是不想写", body: "测试，是最适合「开口」的活儿——人人都说该写，人人都想晚点再写。" },
  { id: "explain", char: "懂", caption: "Read Along", ask: "这段调度逻辑我看不懂，讲讲", title: "不改代码也行", body: "把对话当成一位愿意被反复追问的同事：读得懂的讲给你，看不懂的，咱们一起读。" },
  { id: "refactor", char: "重", caption: "Same Behavior", ask: "重构一下，但行为别变", title: "「行为别变」", body: "这四个字，是对重构最精准的需求描述。小心翼翼的部分，交给测试和对话。" },
  { id: "night", char: "夜", caption: "Night Shift", ask: "每天早上帮我检查依赖更新", title: "下班了，对话还在上班", body: "说一句，自动化就排好了。明早你打开软件，结论已经在等你。" },
  { id: "scaffold", char: "建", caption: "Zero to Run", ask: "新建一个带登录的 Flask 小项目", title: "从零，到跑起来", body: "空目录也能开局：把想要的样子说出来，脚手架、依赖、启动命令，一口气替你备齐。" },
  { id: "review", char: "审", caption: "Final Check", ask: "上线前再帮我检查一遍有没有漏的", title: "最后一道关", body: "把「再看一眼」交给对话，眼睛留着看别的。关键改动依然会停下来，等你点头。" },
];

/** 一天一枚：按「当年第几天」轮换 */
export function todayBadgeIndex(now = new Date()): number {
  const start = new Date(now.getFullYear(), 0, 0);
  const day = Math.floor((now.getTime() - start.getTime()) / 86400000);
  return day % BADGES.length;
}
