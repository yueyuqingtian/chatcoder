/** plan-20261105-file-review：快速导航与会话切换不得被迟到请求覆盖。 */
import { usePanelStore } from "../src/store/panel";
import { api } from "../src/api/client";
import type { TurnFileReview } from "../src/store/panel";

let checks = 0;
function assert(value: unknown, message: string) { if (!value) throw new Error(message); checks++; }
const pending: { path: string; resolve: (value: any) => void; reject: (value: Error) => void }[] = [];
const original = api.getFileDiff;
let fullRequested = false;
api.getFileDiff = ((_turn, path, _call, _extra, full) => {
  fullRequested = !!full;
  return new Promise((resolve, reject) => pending.push({ path, resolve, reject }));
}) as typeof api.getFileDiff;
const review: TurnFileReview = { turnId: 7, extraTurnIds: [8], index: 0, mode: "diff", files: [
  { path: "a.py", action: "modified", additions: 1, deletions: 1, reviewed: false },
  { path: "b.py", action: "modified", additions: 1, deletions: 1, reviewed: false },
  { path: "gone.py", action: "deleted", additions: 0, deletions: 1, reviewed: false },
] };
const state = () => usePanelStore.getState();
state().setActiveSession(101);
usePanelStore.setState({ expanded: true });
const a = state().openTurnFile(review, 0, "diff");
const b = state().openTurnFile(review, 1, "diff");
assert(fullRequested, "审查必须请求完整上下文");
pending[1].resolve({ path: "b.py", before: "b0", after: "b1", truncated: false }); await b;
pending[0].resolve({ path: "a.py", before: "a0", after: "a1", truncated: false }); await a;
assert(state().diffPreview?.path === "b.py", "迟到的第一个文件不能覆盖第二个文件");
assert(state().turnFileReview?.index === 1, "导航仍位于第二个文件");
const old = state().openTurnFile(review, 0, "diff");
state().setPreviewPath("manual.md");
pending[2].resolve({ path: "a.py", before: "a0", after: "a1", truncated: false }); await old;
assert(state().previewPath === "manual.md" && !state().diffPreview && !state().turnFileReview, "普通入口退出本轮导航并拒绝旧响应");
const cross = state().openTurnFile(review, 0, "diff");
state().setActiveSession(102);
state().setPreviewPath("session2.md");
pending[3].resolve({ path: "a.py", before: "a0", after: "a1", truncated: false }); await cross;
assert(state().previewPath === "session2.md" && !state().diffPreview, "其他会话保持自身预览");
state().setActiveSession(101);
assert(state().diffPreview?.path === "a.py", "后台响应只写回所属会话桶");
const removed = state().openTurnFile(review, 2, "content");
assert(state().turnFileReview?.mode === "diff", "删除文件自动查看历史变更");
pending[4].resolve({ path: "gone.py", before: "old", after: null, action: "deleted", truncated: false }); await removed;
assert(state().diffPreview?.deleted, "删除标记不会丢失");
const fail = state().openTurnFile(review, 0, "diff");
pending[5].reject(new Error("测试读取失败")); await fail;
assert(!state().turnFileReview?.loading && !!state().turnFileReview?.error, "失败必须结束加载并提供错误");
await state().openTurnFile(review, 1, "content");
assert(state().previewPath === "b.py" && !state().diffPreview && !state().turnFileReview?.error, "切回内容不沿用失败状态");
const request = state().previewRequestId;
await state().openTurnFile(review, -1, "diff");
await state().openTurnFile(review, 3, "diff");
assert(state().previewRequestId === request, "文件首尾边界不生成非法请求");
api.getFileDiff = original;
console.log(`文件审查导航 ${checks} 项通过`);
