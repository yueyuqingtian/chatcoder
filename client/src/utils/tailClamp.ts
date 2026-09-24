/**
 * tailClamp（plan-334-1661 S2）：超长文本的**尾部**截断——仅用于渲染层。
 *
 * ── 为什么 ──
 * 运行中工具的实时输出（`runningToolOutput`）与落库 output 会原样渲染进 `<pre>`：
 * 一条打印上万行的命令会让 DOM 文本节点达到数 MB，每帧重排/绘制成本随输出规模线性放大。
 * 这里只保留尾部（最有信息量的最新输出），把 DOM 文本量钳制在有界范围。
 *
 * ── 约定 ──
 * - **只截渲染，不改数据**：数据源（store 缓冲 / message content）始终是全量，
 *   复制、diff、结构化解析（grep 命中、SQL 结果等）都必须读原始文本。
 * - 返回体给出 `omitted`（被省略的字符数），供调用方展示"已省略前 N 字符"提示。
 *
 * ── 成本（plan-334-1661 回归修复） ──
 * 本函数在流式期间随文本变化被反复调用（每次 flush 一次），因此扫描必须有界：
 * ① 短文本走零扫描快速路径；
 * ② 行边界只在「字符下限窗口 [len - maxChars, len)」内反向查找——窗口外更早的行边界
 *    最终都会被字符下限覆盖，扫描它们没有意义（避免对超大单行文本做 O(全文) 扫描）。
 */

export interface TailClampResult {
  /** 实际用于渲染的文本（尾部片段） */
  text: string;
  /** 被省略的前缀字符数；0 表示未截断 */
  omitted: number;
}

/** 默认上限：约 64K 字符 / 2000 行（两者先触者生效，取更靠后的起点）。 */
export const TAIL_CLAMP_MAX_CHARS = 64 * 1024;
export const TAIL_CLAMP_MAX_LINES = 2000;

/**
 * 截取文本尾部，使渲染量不超过字符数与行数上限。
 * 未超限时原样返回（`omitted === 0`），调用方可据此走零开销分支。
 */
export function tailClamp(
  text: string,
  maxChars: number = TAIL_CLAMP_MAX_CHARS,
  maxLines: number = TAIL_CLAMP_MAX_LINES,
): TailClampResult {
  const len = text.length;
  if (len === 0) return { text, omitted: 0 };
  // 快速路径：行数 ≤ 字符数，故 len 同时不超过两个上限时必无截断（零扫描返回）。
  if (len <= maxChars && len <= maxLines) return { text, omitted: 0 };

  // 字符下限：最终起点不会早于这里
  let start = len > maxChars ? len - maxChars : 0;

  // 行限定：仅在本窗口内反向找第 maxLines 个换行（窗口外无需扫描，见文件头注）
  if (maxLines > 0) {
    let idx = len;
    for (let seen = 0; seen < maxLines; ) {
      if (idx <= start) break; // 窗口内已无更多换行：行限定不生效
      const nl = text.lastIndexOf("\n", idx - 1);
      if (nl < start) break; // 该换行落在窗口之外
      seen += 1;
      if (seen >= maxLines) {
        start = nl + 1;
        break;
      }
      idx = nl;
    }
  }

  if (start <= 0) return { text, omitted: 0 };
  return { text: text.slice(start), omitted: start };
}
