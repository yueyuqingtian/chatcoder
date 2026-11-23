import assert from "node:assert/strict";
import { api, type MessageOut, type MessagePage } from "../src/api/client.ts";
import {
  aroundMessages,
  clearMessageHistory,
  getMessageHistoryState,
  initialMessages,
  olderMessages,
  refreshMessageWindow,
} from "../src/store/messageHistory.ts";
import { mergeRefreshedMessages } from "../src/store/chat.ts";

function makeMsg(id: number, sender: "user" | "assistant" = "user"): MessageOut {
  return {
    id,
    session_id: 1,
    turn_id: Math.ceil(id / 2),
    thread_id: null,
    sender_type: sender,
    sender_id: sender === "user" ? 0 : 1,
    msg_type: "text",
    content: { text: `msg-${id}` },
    token_usage: null,
    created_at: `2026-11-20T12:00:${String(id % 60).padStart(2, "0")}Z`,
  };
}

function makePage(items: MessageOut[], opts: Partial<MessagePage> = {}): MessagePage {
  return {
    items,
    context: [],
    before_id: items.length ? items[0].id : (opts.before_id ?? null),
    after_id: items.length ? items[items.length - 1].id : (opts.after_id ?? null),
    has_before: false,
    has_after: false,
    revision: 1,
    boundary_turn_ids: [],
    ...opts,
  };
}

const origMessagePage = api.messagePage;

try {
  // ── 场景 1：refreshMessageWindow 未 commit（例如用户刚切走会话），游标不得提前推进 ──
  {
    const sid = 101;
    clearMessageHistory(sid);
    const db: MessageOut[] = [makeMsg(1, "user"), makeMsg(2, "assistant")];
    const requestedAfters: Array<number | undefined> = [];

    api.messagePage = async (_sid, params) => {
      requestedAfters.push(params?.after_id);
      if (params?.after_id == null) return makePage([...db], { revision: 1 });
      const items = db.filter((m) => m.id > params.after_id!);
      return makePage(items, {
        before_id: items[0]?.id ?? null,
        after_id: items.length ? items[items.length - 1].id : params.after_id,
        revision: 1,
      });
    };

    const init = await initialMessages(sid);
    init.commit();
    const local = [...init.items];

    // 中午新增两条消息（用户 + AI 回复）
    db.push(makeMsg(3, "user"), makeMsg(4, "assistant"));

    // 第一次刷新返回了 [3, 4]，但模拟用户此时切走会话，调用方未调用 commit() 且未写入 local
    const abandoned = await refreshMessageWindow(sid, local);
    assert.deepEqual(abandoned.items.map((m) => m.id), [3, 4]);

    // 下午又新增两条消息 [5, 6]
    db.push(makeMsg(5, "user"), makeMsg(6, "assistant"));

    // 切回会话再次刷新：因为上次未 commit，起点仍是 2，必须完整补回中午的 [3, 4] 与下午的 [5, 6]
    const recovered = await refreshMessageWindow(sid, local);
    recovered.commit();
    assert.deepEqual(recovered.items.map((m) => m.id), [3, 4, 5, 6]);
    assert.equal(requestedAfters[requestedAfters.length - 1], 2);
  }

  // ── 场景 2：olderMessages 未 commit（往回滑瞬间切走），loading 复位且 before 不跳过该页 ──
  {
    const sid = 102;
    clearMessageHistory(sid);
    const all = [makeMsg(1, "user"), makeMsg(2, "assistant"), makeMsg(3, "user"), makeMsg(4, "assistant")];
    const requestedBefores: Array<number | undefined> = [];

    api.messagePage = async (_sid, params) => {
      requestedBefores.push(params?.before_id);
      if (params?.before_id == null) {
        return makePage(all.slice(2), { has_before: true, revision: 1 });
      }
      const older = all.filter((m) => m.id < params.before_id!);
      return makePage(older, { has_before: false, revision: 1 });
    };

    const init = await initialMessages(sid);
    init.commit();

    // 往回滑加载更早历史，但未 commit（模拟切走）
    const abandonedOlder = await olderMessages(sid);
    assert.deepEqual(abandonedOlder.items.map((m) => m.id), [1, 2]);

    // 切回后再往回滑：loading 已复位且 before 仍是 3，不会卡死也不会跳过 [1, 2]
    const retryOlder = await olderMessages(sid);
    retryOlder.commit();
    assert.deepEqual(retryOlder.items.map((m) => m.id), [1, 2]);
    assert.deepEqual(requestedBefores, [undefined, 3, 3]);
  }

  // ── 场景 3：clearMessageHistory 清除残留游标（冷缓存淘汰后重进不会按旧游标漏拉） ──
  {
    const sid = 103;
    clearMessageHistory(sid);
    const all = [makeMsg(1, "user"), makeMsg(2, "assistant"), makeMsg(3, "user"), makeMsg(4, "assistant")];
    let hitInitial = 0;

    api.messagePage = async (_sid, params) => {
      if (params?.after_id == null && params?.before_id == null) {
        hitInitial += 1;
        return makePage(all, { revision: 1 });
      }
      const items = all.filter((m) => m.id > (params?.after_id ?? 0));
      return makePage(items, { after_id: items[items.length - 1]?.id ?? params?.after_id ?? null, revision: 1 });
    };

    const init = await initialMessages(sid);
    init.commit();
    clearMessageHistory(sid);

    // 清除后即使本地仅残留 1 条新消息，refreshMessageWindow 也应走首屏全量对齐而非旧游标增量
    const refreshed = await refreshMessageWindow(sid, [makeMsg(4, "assistant")]);
    refreshed.commit();
    assert.equal(hitInitial, 2);
    assert.deepEqual(refreshed.items.map((m) => m.id), [1, 2, 3, 4]);
  }

  // ── 场景 4：自愈防线——若本地最大 ID 落后于 state.after，增量起点自动回退补齐空洞 ──
  {
    const sid = 104;
    clearMessageHistory(sid);
    const db = [makeMsg(1, "user"), makeMsg(2, "assistant"), makeMsg(3, "user"), makeMsg(4, "assistant")];
    const seenAfters: Array<number | undefined> = [];

    api.messagePage = async (_sid, params) => {
      seenAfters.push(params?.after_id);
      if (params?.after_id == null) return makePage(db, { revision: 1 });
      const items = db.filter((m) => m.id > params.after_id!);
      return makePage(items, { after_id: items[items.length - 1]?.id ?? params.after_id, revision: 1 });
    };

    const init = await initialMessages(sid);
    init.commit(); // state.after = 4

    // 模拟本地意外只保留到 id=2（落后于 state.after=4）
    const repaired = await refreshMessageWindow(sid, [makeMsg(1, "user"), makeMsg(2, "assistant")]);
    repaired.commit();
    assert.equal(seenAfters[seenAfters.length - 1], 2);
    assert.deepEqual(repaired.items.map((m) => m.id), [3, 4]);
  }

  // ── 场景 5：mergeRefreshedMessages 结合 checkedIds 保留空快照保护与尾部未落库保护 ──
  {
    const existing = [makeMsg(1, "user"), makeMsg(2, "assistant"), makeMsg(3, "user"), makeMsg(4, "assistant")];
    const checkedIds = new Set([1, 2, 3, 4]);

    // 5a: 后端异常空返时，不清空本地消息
    const emptyGuard = mergeRefreshedMessages(existing, [], checkedIds);
    assert.deepEqual(emptyGuard.map((m) => m.id), [1, 2, 3, 4]);

    // 5b: id=2 真被删除（id <= maxFetchedId=3）应删除；id=4 大于 maxFetchedId（未落库尾部）应保留
    const partial = mergeRefreshedMessages(existing, [makeMsg(1, "user"), makeMsg(3, "user")], checkedIds);
    assert.deepEqual(partial.map((m) => m.id), [1, 3, 4]);
  }

  // ── 场景 6：page.context 携带跨页更早消息不误判为跳页断层；around 跳页后才置位并在连续页追上后清除 ──
  {
    const sid = 106;
    clearMessageHistory(sid);
    api.messagePage = async (_sid, params) => {
      if (params?.around_id != null) {
        return makePage([makeMsg(10, "user"), makeMsg(11, "assistant")], { has_before: true, has_after: true, revision: 1 });
      }
      if (params?.before_id === 50) {
        return makePage([makeMsg(10, "user"), makeMsg(30, "assistant")], { before_id: 10, after_id: 30, has_before: false, revision: 1 });
      }
      // 首屏页：主区间 [50, 51]，但 context 带入了跨页更早消息 id=20
      return makePage([makeMsg(50, "user"), makeMsg(51, "assistant")], {
        context: [makeMsg(20, "assistant")],
        before_id: 50,
        after_id: 51,
        has_before: true,
        revision: 1,
      });
    };

    const init = await initialMessages(sid);
    init.commit();
    assert.deepEqual(init.items.map((m) => m.id), [20, 50, 51]);
    // 关键回归：context 中 id=20 < before_id=50 不应置位 disjointBefore，上滚必须允许自动加载
    assert.equal(getMessageHistoryState(sid).disjointBefore, false);

    // 真正通过 aroundMessages 跳到更早区间 [10, 11] 后，disjointBefore 置为 true
    await aroundMessages(sid, 10);
    assert.equal(getMessageHistoryState(sid).disjointBefore, true);

    // 当连续窗口 olderMessages 追上并覆盖到 id=10 时，disjointBefore 自动清除
    const older = await olderMessages(sid);
    older.commit();
    assert.equal(getMessageHistoryState(sid).disjointBefore, false);
  }

  console.log("消息流历史窗口与游标一致性回归全部通过");
} finally {
  api.messagePage = origMessagePage;
}
