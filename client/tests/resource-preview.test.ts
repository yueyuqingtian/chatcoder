/** plan-20261103-attachment-preview：打开入口的会话隔离与旧预览兼容。 */
import assert from "node:assert/strict";
import { usePanelStore } from "../src/store/panel.ts";
const store = usePanelStore;
store.getState().reset();
store.setState({ openPanel: () => store.setState({ expanded: true }) });
store.getState().setActiveSession(1);
const a = {
  source: "attachment" as const,
  session_id: 1,
  message_id: 10,
  file_id: "same",
};
store.getState().openResource(a);
assert.deepEqual(store.getState().previewTarget, a);
assert.equal(store.getState().tabs.filter((t) => t.id === "files").length, 1);
store.getState().openResource({ ...a, file_id: "another" });
assert.equal(store.getState().tabs.filter((t) => t.id === "files").length, 1);
store.getState().setActiveSession(2);
assert.equal(store.getState().previewTarget, null);
store.getState().openResource({ ...a, session_id: 1, file_id: "background" });
assert.equal(store.getState().previewTarget, null);
store.getState().setPreviewPath("code.ts", 42);
assert.equal(store.getState().previewPath, "code.ts");
assert.equal(store.getState().previewLine, 42);
store.getState().setActiveSession(1);
assert.equal(store.getState().previewTarget?.source, "attachment");
store.getState().openDiffPreview("gone.ts", {
  path: "gone.ts",
  before: "a",
  after: null,
  truncated: false,
  deleted: true,
});
assert.equal(store.getState().previewTarget, null);
assert.equal(store.getState().diffPreview?.deleted, true);
store.getState().openPlanPreview("plan.md");
assert.equal(store.getState().diffPreview, null);
assert.equal(store.getState().planCollapsePending, true);
store.getState().setActiveSession(2);
assert.equal(store.getState().previewLine, 42);
console.log("附件入口、标签复用、后台会话隔离、差异与计划兼容通过");

const { wheelZoom } = await import("../src/components/panel/wordZoom.ts");
assert.ok(wheelZoom(100, -1) > 100 && wheelZoom(100, -1) < 101);
assert.ok(wheelZoom(100, 1) < 100);
assert.equal(wheelZoom(10, 1000), 10);
assert.equal(wheelZoom(400, -1000), 400);
assert.ok(Math.abs(wheelZoom(wheelZoom(100, -40), 40) - 100) < 0.000001);
assert.equal(wheelZoom(100, 1, 1), wheelZoom(100, 16, 0));
console.log("Word 滚轮连续缩放、反向恢复与滚动单位兼容通过");

store.getState().setActiveSession(1);
store.getState().openResource({ ...a, session_id: 99 }, 1);
assert.equal(store.getState().previewTarget?.session_id, 99);
assert.equal(store.getState().activeBucket, "s1");
