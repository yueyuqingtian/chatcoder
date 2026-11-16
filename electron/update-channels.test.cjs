"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");
const { probeChannel, measureChannels } = require("./update-channels.cjs");
const { normalizeSources, createSourceController } = require("./update-source-policy.cjs");

function policy(tiers) {
  return createSourceController({
    sources: normalizeSources(tiers.map((tier, rank) => ({ url: `https://${tier}.test/file`, tier, rank }))),
    backoffSeqMs: [0], jitter: 0,
  });
}

test("显式优选 GitHub 后首选、失败轮换和回切都服从优先级", async () => {
  const control = policy(["github", "mirror", "primary"]);
  assert.equal(control.current().tier, "github");
  const lease = await control.acquire();
  control.reportFailure(lease, { kind: "network" });
  assert.equal(control.current().tier, "mirror");
  assert.deepEqual(control.higherThanCurrent().map((source) => source.tier), ["github"]);
  control.promote(control.sources[0]);
  assert.equal(control.current().tier, "github");
});

test("缺省下载顺序仍保留两轮镜像后再用 GitHub", async () => {
  const control = policy(["mirror", "primary", "github"]);
  const seen = [];
  for (let i = 0; i < 6; i++) {
    seen.push(control.current().tier);
    control.reportFailure(await control.acquire(), { kind: "network" });
  }
  assert.deepEqual(seen, ["mirror", "primary", "mirror", "primary", "github", "github"]);
});

test("显式排在中间的 GitHub 也不能被低优先级源越过", async () => {
  const control = policy(["primary", "github", "mirror"]);
  control.reportFailure(await control.acquire(), { kind: "network" });
  assert.equal(control.current().tier, "github");
});

async function serverFor(t, handler) {
  const server = http.createServer(handler);
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise((resolve) => { server.closeAllConnections(); server.close(resolve); }));
  return `http://127.0.0.1:${server.address().port}`;
}

test("响应头已到但响应体停滞时也会超时，整组测速可结束并再次运行", { timeout: 3000 }, async (t) => {
  const base = await serverFor(t, (req, res) => {
    res.writeHead(206, { "content-length": "16" });
    if (req.url === "/stall") { res.flushHeaders(); return; }
    res.end(Buffer.alloc(16));
  });
  const options = { urlsByChannel: { mirror1: [base + "/stall"], github: [base + "/ok"] }, fetchFor: () => fetch, timeoutMs: 120 };
  const report = await measureChannels(options);
  assert.equal(report.results.mirror1.ok, false);
  assert.equal(report.results.github.ok, true);
  assert.equal(report.best, "github");
  assert.equal((await probeChannel({ url: base + "/ok", fetchImpl: fetch, timeoutMs: 500 })).ok, true);
});

test("取消能中断已经开始读取的响应体", { timeout: 2000 }, async () => {
  const controller = new AbortController();
  let cancelled = false;
  const fetchImpl = async () => ({ status: 206, headers: new Map(), body: { getReader: () => ({
    read: () => { controller.abort(); return new Promise(() => {}); },
    cancel: async () => { cancelled = true; },
  }) } });
  const result = await probeChannel({ url: "https://fake.test", fetchImpl, signal: controller.signal, timeoutMs: 1000 });
  assert.equal(result.ok, false);
  assert.equal(cancelled, true);
});

test("收到响应头之前的停滞同样受超时保护", { timeout: 1000 }, async () => {
  const result = await probeChannel({ url: "https://fake.test", fetchImpl: () => new Promise(() => {}), timeoutMs: 15 });
  assert.equal(result.ok, false);
});
