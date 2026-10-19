/**
 * Git 图谱泳道布局测试（plan-20261019-toolcard-v2）。
 * 纯逻辑，Node 原生 TS 运行：`node tests/git-graph.test.ts`（client 目录）。
 */
import { layoutGraph, type GraphCommit } from "../src/components/chat/toolcard/gitGraph.ts";

let failures = 0;
function eq(actual: unknown, expected: unknown, msg: string) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) { failures++; console.error(`FAIL: ${msg}\n   actual:   ${a}\n   expected: ${e}`); }
  else { console.log(`ok: ${msg}`); }
}
const c = (sha: string, ...parents: string[]): GraphCommit => ({ sha, parents });

// ① 线性历史：全在泳道 0，一路向下
{
  const { rows, maxLanes } = layoutGraph([c("c", "b"), c("b", "a"), c("a")]);
  eq(rows.map((r) => r.lane), [0, 0, 0], "线性历史全在 0 号泳道");
  eq(maxLanes, 1, "只用 1 条泳道");
  eq(rows[0].top, [], "第一行顶部没有线");
  eq(rows[0].links, [0], "第一行连向下一行");
  eq(rows[2].links, [], "根提交没有向下的线");
  eq(rows[2].bottom, [], "根提交之后没有等待的泳道");
}

// ② 合并提交：m 有两个父提交（b 在主线、x 在旁支）
//    m ─┬─ b ─ a
//       └─ x ─┘
{
  const { rows, maxLanes } = layoutGraph([c("m", "b", "x"), c("x", "a"), c("b", "a"), c("a")]);
  eq(rows[0].lane, 0, "合并提交在主线");
  eq(rows[0].links, [0, 1], "合并提交连向两个父提交的泳道");
  eq(rows[1].lane, 1, "旁支提交 x 落在 1 号泳道");
  eq(rows[2].lane, 0, "主线提交 b 回到 0 号泳道");
  // a 同时被泳道 0（b 的父）与泳道 1（x 的父）等待：在 0 号泳道汇合，1 号泳道汇入
  eq(rows[3].lane, 0, "公共祖先落在 0 号泳道");
  eq(rows[3].merges, [1], "旁支泳道在公共祖先处汇入");
  eq(maxLanes, 2, "最多 2 条泳道");
}

// ③ 分叉后又复用空泳道：两条分支各自结束，之后的新分支头复用空出来的泳道
{
  const { rows } = layoutGraph([
    c("h1", "p"), c("h2", "p"), c("p", "q"), c("q"),
    c("z"),
  ]);
  eq(rows[0].lane, 0, "第一个分支头占 0 号泳道");
  eq(rows[1].lane, 1, "第二个分支头占 1 号泳道");
  eq(rows[2].lane, 0, "公共父提交落在较小的泳道");
  eq(rows[2].merges, [1], "另一条泳道在此汇入");
  eq(rows[4].lane, 0, "泳道全部空出后，新的分支头复用 0 号泳道");
}

// ④ 三个父提交（章鱼合并）：为第 2、3 个父提交各开一条泳道
{
  const { rows, maxLanes } = layoutGraph([c("o", "a", "b", "c"), c("a"), c("b"), c("c")]);
  eq(rows[0].links, [0, 1, 2], "章鱼合并连向三条泳道");
  eq(maxLanes, 3, "用到 3 条泳道");
  eq(rows[1].lane, 0, "a 在 0 号泳道");
  eq(rows[2].lane, 1, "b 在 1 号泳道");
  eq(rows[3].lane, 2, "c 在 2 号泳道");
}

// ⑤ 两条泳道等同一个父提交：并行到该提交处汇合，提交落在最左泳道
{
  const { rows } = layoutGraph([c("a", "p"), c("b", "p"), c("p")]);
  eq(rows[0].lane, 0, "a 占 0 号泳道并等待 p");
  eq(rows[1].lane, 1, "b 新开 1 号泳道");
  eq(rows[1].links, [1], "b 的第一个父提交沿本泳道继续");
  eq(rows[1].bottom, ["p", "p"], "两条泳道都在等 p");
  eq(rows[2].lane, 0, "p 落在最左的泳道");
  eq(rows[2].merges, [1], "另一条泳道在 p 处汇入");
}

// ⑤-b 孤立根提交（没有父提交）不会残留泳道
{
  const { rows } = layoutGraph([c("solo"), c("next", "x")]);
  eq(rows[0].bottom, [], "没有父提交的新分支头不留泳道");
  eq(rows[1].lane, 0, "下一个提交复用 0 号泳道");
}

// ⑥ 浅克隆 / 分页截断：父提交不在已加载范围内，泳道一直延伸到底部
{
  const { rows } = layoutGraph([c("c", "b"), c("b", "missing")]);
  eq(rows[1].bottom, ["missing"], "未加载到的父提交仍占着泳道（直到加载更多）");
}

// ⑦ 空输入
{
  const { rows, maxLanes } = layoutGraph([]);
  eq([rows.length, maxLanes], [0, 0], "空输入");
}

// ⑧ 不变量：随机 DAG 上，每行 bottom 与下一行 top 完全一致；节点泳道不越界
{
  let checked = 0;
  for (let seed = 1; seed <= 200; seed += 1) {
    let x = (seed * 2654435761) >>> 0;
    const rnd = () => { x = (Math.imul(x, 1664525) + 1013904223) >>> 0; return x >>> 8; };
    const n = 3 + (rnd() % 30);
    const commits: GraphCommit[] = [];
    for (let i = 0; i < n; i += 1) {
      const older = Array.from({ length: n - i - 1 }, (_, k) => `s${i + 1 + k}`);
      const parents: string[] = [];
      if (older.length > 0) {
        parents.push(older[rnd() % Math.min(older.length, 3)]);
        if (rnd() % 4 === 0 && older.length > 1) {
          const p2 = older[rnd() % Math.min(older.length, 4)];
          if (!parents.includes(p2)) parents.push(p2);
        }
      }
      commits.push({ sha: `s${i}`, parents });
    }
    const { rows, maxLanes } = layoutGraph(commits);
    for (let i = 0; i < rows.length; i += 1) {
      const r = rows[i];
      if (r.lane < 0 || r.lane >= r.width) { failures++; console.error(`FAIL: 节点泳道越界 seed=${seed} row=${i}`); }
      if (r.width > maxLanes) { failures++; console.error(`FAIL: width 超过 maxLanes seed=${seed}`); }
      if (i + 1 < rows.length && JSON.stringify(r.bottom) !== JSON.stringify(rows[i + 1].top)) {
        failures++; console.error(`FAIL: 相邻行泳道不衔接 seed=${seed} row=${i}`);
      }
      for (const l of r.links) {
        if (l >= r.width) { failures++; console.error(`FAIL: 连线泳道越界 seed=${seed} row=${i}`); }
      }
    }
    checked += 1;
  }
  console.log(`ok: 随机 DAG 不变量 ${checked} 组`);
}

if (failures > 0) {
  console.error(`\n${failures} 项失败`);
  process.exit(1);
}
console.log("\n全部通过");
