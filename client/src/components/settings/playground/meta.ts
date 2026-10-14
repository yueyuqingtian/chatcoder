/** 乐园玩法说明（plan-playground-v2）：外壳 HUD 用，不引用 three，可进设置页首屏包。 */
export interface GameMeta {
  /** 玩法名（HUD 左上） */
  name: string;
  /** 起手提示（舞台底部） */
  hint: string;
  /** 分数单位（「最佳 12 跳」） */
  unit: string;
}

export const GAME_META: Record<string, GameMeta> = {
  graphite: { name: "灵感记忆", hint: "记住方块亮起的顺序，再让嗒嗒依次踩上去 · 点击开始", unit: "分" },
  forest: { name: "萤火捕手", hint: "移动鼠标追萤火 · 点击起跳够到高处的 · 金色萤火更值钱", unit: "只" },
  campfire: { name: "篝火烤棉花糖", hint: "走到篝火旁，按住烤、松开吃 · 烤到金黄最好吃，别烤焦", unit: "分" },
  breeze: { name: "风筝高飞", hint: "带着嗒嗒跑，让身后的风筝碰到天上的星星 · 点击借一阵风", unit: "颗" },
  tide: { name: "打水漂", hint: "按住蓄力、对准方向松开 · 力度落在亮区跳得更远 · 打中小黄鸭有奖励", unit: "分" },
  sakura: { name: "接落樱", hint: "看地上的影子，站到花瓣落下的地方 · 连续接住会叠在嗒嗒头上", unit: "分" },
};
