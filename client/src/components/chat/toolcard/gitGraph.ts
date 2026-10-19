/** Git 图谱的泳道布局（plan-20261019-toolcard-v2）。
 *
 * 输入：`git log` 的提交序列（新 → 旧，含每个提交的父提交）。
 * 输出：每个提交所在的泳道，以及「进入本行顶部 / 离开本行底部」的泳道占用，
 * 足够用 SVG 逐行画出主干、分叉与合并（见 GitGraphDialog）。
 *
 * 做法是经典的「等待队列」：`lanes[i]` 存泳道 i 上正在等待的下一个提交的 sha。
 *  · 提交出现时，落在等待它的第一条泳道；没有泳道等它（一个新的分支头）就占用第一条空泳道；
 *  · 等待它的其余泳道都在本行汇入（merges）；
 *  · 第一个父提交继续沿本泳道向下（若别的泳道已经在等它，则本泳道让出并连过去）；
 *  · 其余父提交各占一条泳道（已有泳道在等就直接连过去）——这就是合并提交的第二、第三条线。
 * 纯函数，无 DOM / store 依赖（client/tests/git-graph.test.ts）。
 */

export interface GraphCommit {
  sha: string;
  parents: string[];
}

export interface GraphRow {
  sha: string;
  /** 本行提交节点所在泳道 */
  lane: number;
  /** 进入本行顶部的泳道：值 = 该泳道在等待的提交 sha，null = 没有线 */
  top: (string | null)[];
  /** 离开本行底部的泳道 */
  bottom: (string | null)[];
  /** 在本行汇入节点的其它泳道（它们的线在本行从各自位置弯向节点） */
  merges: number[];
  /** 从节点连向下一行的泳道（第一个父提交的泳道，以及合并提交的其它父提交所在泳道） */
  links: number[];
  /** 本行用到的泳道总数（含上下两侧） */
  width: number;
}

export interface GraphLayout {
  rows: GraphRow[];
  /** 全图最大泳道数 */
  maxLanes: number;
}

function firstFree(lanes: (string | null)[]): number {
  const i = lanes.indexOf(null);
  return i >= 0 ? i : lanes.length;
}

export function layoutGraph(commits: GraphCommit[]): GraphLayout {
  const lanes: (string | null)[] = [];
  const rows: GraphRow[] = [];
  let maxLanes = 0;
  for (const c of commits) {
    const top = lanes.slice();
    const waiting: number[] = [];
    lanes.forEach((v, i) => { if (v === c.sha) waiting.push(i); });

    // 落在等待它的最左泳道；没有泳道在等它（一个新的分支头）就占第一条空泳道，顶部没有线
    const lane = waiting.length > 0 ? waiting[0] : firstFree(lanes);
    const merges = waiting.slice(1);
    for (const i of waiting) lanes[i] = null;

    const links: number[] = [];
    if (c.parents.length > 0) {
      // 第一个父提交沿本泳道继续；即使别的泳道也在等同一个提交也照常占用——
      // 两条线并行到该提交处再汇合（merges），与 `git log --graph` 的画法一致，提交总是落在最左的泳道
      lanes[lane] = c.parents[0];
      links.push(lane);
      for (const p of c.parents.slice(1)) {
        const e = lanes.indexOf(p);
        if (e >= 0) {
          if (!links.includes(e)) links.push(e);
        } else {
          const slot = firstFree(lanes);
          lanes[slot] = p;
          links.push(slot);
        }
      }
    }
    while (lanes.length > 0 && lanes[lanes.length - 1] === null) lanes.pop();
    const bottom = lanes.slice();
    const width = Math.max(top.length, bottom.length, lane + 1, ...links.map((l) => l + 1), ...merges.map((m) => m + 1));
    maxLanes = Math.max(maxLanes, width);
    rows.push({ sha: c.sha, lane, top, bottom, merges, links, width });
  }
  return { rows, maxLanes };
}
