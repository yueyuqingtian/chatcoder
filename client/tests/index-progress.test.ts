/**
 * 索引库进度展示逻辑测试（Node 原生 TS 运行：`node tests/index-progress.test.ts`）。
 *
 * 复现缺陷（用户反馈）：实际正在索引时，页面徽标显示「解析中」，
 * 但下方仍是旧的「8000 文件 · 35337 符号」，看不到进度与已扫描文件数。
 *
 * 根因：前端仅在 status === "indexing" 时渲染进度，而 worker 实际写入的是
 * queued/scanning/parsing，永远匹配不上，于是走了 else 分支显示陈旧数据。
 *
 * 本测试直接校验从 IndexLibraryPanel 抽出的判定/文案逻辑。
 */
import { isIndexBusy, progressText, resolveSearchTarget, sameWorkspace, type IndexProgressStat } from "../src/components/settings/indexProgress.ts";

let failures = 0;
function assert(cond: boolean, msg: string) {
  if (!cond) { failures++; console.error(`FAIL: ${msg}`); }
  else { console.log(`ok: ${msg}`); }
}

function stat(p: Partial<IndexProgressStat>): IndexProgressStat {
  return {
    status: "off", progress: 0, files: 0, symbols: 0,
    files_scanned: 0, files_total: 0, ...p,
  } as IndexProgressStat;
}

function main() {
  // —— 进行中状态必须全部识别（此前只认 indexing，导致实际索引时无进度）——
  for (const s of ["queued", "scanning", "parsing", "indexing"]) {
    assert(isIndexBusy(s), `进行中状态被识别: ${s}`);
  }
  for (const s of ["off", "ready", "cancelled", "error"]) {
    assert(!isIndexBusy(s), `非进行中状态不被当作索引中: ${s}`);
  }

  // —— 解析阶段：显示「已解析 x / 共 y 个文件」——
  assert(
    progressText(stat({ status: "parsing", files_scanned: 2272, files_total: 16617 }))
      === "已解析 2272 / 共 16617 个文件",
    "解析阶段显示已解析/总数",
  );

  // —— 扫描阶段：总量未知，只报已发现数（不能显示成 "0 / 0"）——
  const scanning = progressText(stat({ status: "scanning", files_scanned: 14336, files_total: 0 }));
  assert(scanning === "已发现 14336 个文件…", `扫描阶段显示已发现数，实际: ${scanning}`);

  // —— 排队阶段：明确提示等待启动 ——
  assert(
    progressText(stat({ status: "queued" })) === "等待启动索引进程…",
    "排队阶段显示等待提示",
  );

  // —— 工作区路径等价比较（分隔符/末尾斜杠/大小写差异都要视为同一个）——
  assert(sameWorkspace("F:\\project\\yipinCode", "F:/project/yipinCode"), "斜杠方向差异视为同一工作区");
  assert(sameWorkspace("D:\\aiChat\\chatcoderchat\\", "d:\\aichat\\chatcoderchat"), "末尾斜杠 + 大小写差异视为同一工作区");
  assert(!sameWorkspace("F:\\project\\yipinCode", "D:\\aiChat\\chatcoderchat"), "不同工作区不相等");
  assert(!sameWorkspace(null, "F:\\project\\yipinCode"), "空值不匹配任何工作区");

  // —— 检索目标解析（核心回归：显示与请求必须一致）——
  const ws = (workspace: string, enabled: boolean) => ({ workspace, enabled });
  const chatcoderchat = ws("D:\\aiChat\\chatcoderchat", false);
  const yipin = ws("F:\\project\\yipinCode", true);

  // 旧 bug：target 落到未启用的首个项目，而下拉框只列 enabled → 显示 A、查 B
  assert(
    resolveSearchTarget("D:\\aiChat\\chatcoderchat", [chatcoderchat, yipin], null) === "F:\\project\\yipinCode",
    "target 指向未启用项时，重定向到已启用项（避免显示/请求不一致）",
  );
  // 用户已选的启用项必须保持
  assert(
    resolveSearchTarget("F:\\project\\yipinCode", [chatcoderchat, yipin], null) === "F:\\project\\yipinCode",
    "已选且有效时保持不变",
  );
  // 多个启用项时优先落到当前项目
  const other = ws("D:\\myProject\\chatcoder", true);
  assert(
    resolveSearchTarget(null, [yipin, other], "D:/myProject/chatcoder") === "D:\\myProject\\chatcoder",
    "多启用项时优先命中当前项目（容忍路径写法差异）",
  );
  // 当前项目未启用索引 → 退回第一个启用项
  assert(
    resolveSearchTarget(null, [chatcoderchat, yipin], "D:\\aiChat\\chatcoderchat") === "F:\\project\\yipinCode",
    "当前项目未启用时退回第一个已启用工作区",
  );
  // 无任何启用项 → null（按钮应禁用）
  assert(
    resolveSearchTarget(null, [chatcoderchat], null) === null,
    "无启用项时返回 null",
  );

  console.log(failures === 0 ? "\nALL PASS" : `\n${failures} FAILURE(S)`);
  process.exit(failures === 0 ? 0 : 1);
}

main();
