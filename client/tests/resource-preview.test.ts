/** plan-20261103-attachment-preview：打开入口的会话隔离与旧预览兼容。 */
import assert from "node:assert/strict";
import { usePanelStore } from "../src/store/panel.ts";
const store = usePanelStore;
// plan-20261122-right-panel：真实执行面板布局操作，模拟浏览器事件并同步收尾。
Object.assign(globalThis, {
  window: Object.assign(new EventTarget(), { matchMedia: () => ({ matches: true }) }),
  document: { body: { classList: { add() {}, remove() {} } }, documentElement: { getAttribute: () => null } },
});
store.getState().reset();
store.getState().setActiveSession(1);
const a = {
  source: "attachment" as const,
  session_id: 1,
  message_id: 10,
  file_id: "same",
};
store.getState().openResource(a);
assert.equal(store.getState().expanded, true);
assert.deepEqual(store.getState().previewTarget, a);
assert.equal(store.getState().tabs.filter((t) => t.id === "files").length, 1);
store.getState().openResource({ ...a, file_id: "another" });
assert.equal(store.getState().tabs.filter((t) => t.id === "files").length, 1);
store.getState().setActiveSession(2);
assert.equal(store.getState().expanded, false);
assert.equal(store.getState().previewTarget, null);
store.getState().openResource({ ...a, session_id: 1, file_id: "background" });
assert.equal(store.getState().expanded, false);
assert.equal(store.getState().previewTarget, null);
store.getState().setPreviewPath("code.ts", 42);
assert.equal(store.getState().previewPath, "code.ts");
assert.equal(store.getState().previewLine, 42);
store.getState().setActiveSession(1);
assert.equal(store.getState().expanded, true);
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

const panel = () => store.getState();
panel().toggleFullscreen();
assert.equal(panel().fullscreen, true);
panel().setActiveSession(2);
assert.equal(panel().fullscreen, false);
assert.equal(panel().expanded, false);
const terminal = () => panel().tabs.find((tab) => tab.id === "terminal");
panel().openNewTab("terminal");
assert.ok(terminal());
assert.equal(panel().expanded, true);
panel().togglePanel();
assert.equal(panel().expanded, false);
panel().ensureTabIn(2, "browser");
panel().ensureMicroAppTab(2, "demo", "1.0");
assert.equal(panel().expanded, false);
panel().setActiveSession(1);
assert.equal(panel().expanded, true);
assert.equal(panel().fullscreen, true);
assert.equal(panel().tabs.some((tab) => tab.id === "terminal"), false);
panel().setFullscreen(false);
assert.equal(panel().fullscreen, false);
assert.equal(panel().expanded, true);

panel().setActiveSession(2);
panel().openSubagent(3);
assert.equal(panel().expanded, true);
panel().togglePanel();
panel().openSubagent(3);
assert.equal(panel().expanded, true);
assert.equal(panel().tabs.filter((tab) => tab.id === "subagent").length, 1);
panel().togglePanel();
panel().openMicroApp("demo", "2.0");
assert.equal(panel().expanded, true);
assert.equal(panel().tabs.find((tab) => tab.id === "microapp")?.meta?.version, "2.0");
panel().togglePanel();
panel().setFullscreen(true);
assert.equal(panel().expanded, true);
assert.equal(panel().fullscreen, true);
panel().closePanel();
assert.equal(panel().expanded, false);
assert.equal(panel().fullscreen, false);
panel().setActiveSession(null);
assert.equal(panel().expanded, false);
panel().setActiveSession(1);
assert.equal(panel().expanded, true);
assert.equal(panel().fullscreen, false);
panel().setActiveSession(2);
assert.equal(panel().expanded, false);
assert.ok(terminal());
panel().setFullscreen(true);
panel().dropBucket(2);
panel().setActiveSession(2);
assert.equal(panel().expanded, false);
assert.equal(panel().fullscreen, false);
assert.equal(panel().tabs.length, 0);
console.log("会话独立恢复面板状态、工具主动展开、后台标签与会话清理通过");
