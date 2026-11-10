import test from "node:test";
import assert from "node:assert/strict";
import { Backoff, BoundedMap, boundedBody, deadline, ReadHealth, validView } from "../../../leeroy_mind/console/static/js/resilience.mjs";
import { ReconnectingStream } from "../../../leeroy_mind/console/static/js/eventstream.mjs";
import { FaceCache } from "../../../leeroy_mind/console/static/js/facecache.mjs";
import { api, post } from "../../../leeroy_mind/console/static/js/core.mjs";

async function flush() { for (var i = 0; i < 20; i++) { await Promise.resolve(); } }

test("backoff grows, jitters, caps and resets after success", () => {
  var b = new Backoff({ random: () => 0 });
  assert.deepEqual(Array.from({length: 8}, () => b.next()), [500, 1000, 2000, 4000, 8000, 15000, 15000, 15000]);
  b.reset(); assert.equal(b.next(), 500);
  b.random = () => 1; assert.equal(b.next(), 2000);
});

test("a request deadline includes stalled response headers and aborts the request", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  var signal;
  t.mock.method(globalThis, "fetch", (_, o) => { signal = o.signal; return new Promise(() => {}); });
  var result = assert.rejects(api("/api/guild", { timeoutMs: 50 }), /did not answer/);
  t.mock.timers.tick(50); await result;
  assert.equal(signal.aborted, true);
});

test("the same deadline covers a response body that never finishes", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  var signal;
  t.mock.method(globalThis, "fetch", (_, o) => {
    signal = o.signal;
    return Promise.resolve(new Response(new ReadableStream({start(c) { c.enqueue(new TextEncoder().encode('{"pending":')); }}),
      {headers: {"content-type": "application/json"}}));
  });
  var result = assert.rejects(api("/api/guild", { timeoutMs: 50 }), /did not answer/);
  await flush(); t.mock.timers.tick(50); await result;
  assert.equal(signal.aborted, true);
});

test("empty, non-object and malformed successful JSON never replace a view", async (t) => {
  for (var body of ["", "{", "null", "[]", "{}", '{"roster":{}}', '{"other":true}'] ) {
    t.mock.method(globalThis, "fetch", () => Promise.resolve(new Response(body,
      {headers: {"content-type": "application/json"}})));
    await assert.rejects(api("/api/guild"));
  }
});

test("oversized chunked answers are cancelled without trusting Content-Length", async () => {
  var cancelled = false;
  var response = new Response(new ReadableStream({start(c) { c.enqueue(new Uint8Array(17)); }, cancel() { cancelled = true; }}));
  await assert.rejects(boundedBody(response, 16), /too large/);
  assert.equal(cancelled, true);
});

test("an uncertain action settles in words and is never retried automatically", async (t) => {
  var calls = 0;
  t.mock.method(globalThis, "fetch", () => { calls++; return Promise.reject(new Error("connection closed")); });
  var result = await post("/api/board", {action: "reply"});
  assert.equal(result.ok, false); assert.equal(result.status, 0);
  assert.match(result.error, /may have completed/); assert.equal(calls, 1);
});

test("many distinct faces bound both retained entries and waiting loads", async () => {
  var loads = [], dropped = [];
  var cache = new FaceCache({maxEntries: 8, maxActive: 2,
    load: (key) => new Promise(resolve => loads.push(() => resolve(key))), drop: v => dropped.push(v)});
  var promises = Array.from({length: 1000}, (_, n) => cache.get(String(n)));
  assert.equal(cache.entries.size, 8); assert.ok(cache.waiting.length <= 8); assert.equal(cache.active, 2);
  cache.forget(); assert.equal(cache.waiting.length, 0);
  await Promise.all(promises);  // even evicted/forgotten askers have an outcome
  while (loads.length) { loads.shift()(); await flush(); }
  assert.equal(cache.active, 0); assert.equal(dropped.length, 2);
});

test("failed streams back off and a stop cancels the single retry", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  var calls = 0;
  var s = new ReconnectingStream({base: 100, random: () => 1,
    open: () => { calls++; return Promise.reject(new Error("down")); },
    refused: () => false, connected() {}, retrying() {}, dispatch() {}});
  s.start(); await flush(); assert.equal(calls, 1);
  t.mock.timers.tick(99); await flush(); assert.equal(calls, 1);
  t.mock.timers.tick(1); await flush(); assert.equal(calls, 2);
  t.mock.timers.tick(199); await flush(); assert.equal(calls, 2);
  t.mock.timers.tick(1); await flush(); assert.equal(calls, 3);
  s.stop(); t.mock.timers.tick(30000); await flush(); assert.equal(calls, 3);
});

test("a silent half-open stream is cancelled and reconnects; stop wakes its read", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  var calls = 0, cancelled = 0;
  var s = new ReconnectingStream({base: 100, random: () => 1, idleMs: 40,
    open: () => { calls++; return Promise.resolve(new Response(new ReadableStream({cancel() { cancelled++; }}))); },
    refused: () => false, connected() {}, retrying() {}, dispatch() {}});
  s.start(); await flush();
  t.mock.timers.tick(40); await flush(); assert.equal(cancelled, 1);
  t.mock.timers.tick(100); await flush(); assert.equal(calls, 2);
  s.stop(); await flush(); assert.equal(cancelled, 2);
  t.mock.timers.tick(30000); await flush(); assert.equal(calls, 2);
});

test("one successful subroute cannot clear a sibling's stale state", () => {
  var health = new ReadHealth();
  health.update("/api/persona/insight?guid=101", "personas", true, 100);
  health.update("/api/persona/insight?guid=101", "personas", false, 200);
  health.update("/api/persona?guid=101", "personas", true, 300);
  assert.deepEqual(health.failed("personas"), [{tab: "personas", good: 100, failed: true}]);
  health.update("/api/persona/insight?guid=102", "personas", true, 400);
  assert.deepEqual(health.failed("personas"), []);
  assert.equal(health.routes.size, 2); // queries never grow the route map
  for (var i = 0; i < 1000; i++) { health.update("/api/persona/future" + i, "personas", true, i); }
  assert.equal(health.routes.size, 32);
});

test("Ops, Discord and Insight reject malformed outer shapes before drawing", () => {
  assert.equal(validView("/api/ops", {panels: {}, at: 1, counts: {}}), true);
  assert.equal(validView("/api/ops", {panels: []}), false);
  assert.equal(validView("/api/ops/badge", {at: 1, counts: {}}), true);
  assert.equal(validView("/api/ops/badge", {at: 1, counts: []}), false);
  assert.equal(validView("/api/ops/badge", {at: "yesterday", counts: {}}), false);
  var discord = {categories: [], dms: [], members: [], human: {guid: 1, name: "Owner"}, missing: []};
  assert.equal(validView("/api/discord", discord), true);
  assert.equal(validView("/api/discord?channel=hall", discord), false);
  assert.equal(validView("/api/discord?channel=hall", {...discord, channel: "hall", messages: [], threads: [], pins: [], quotes: []}), true);
  var insight = {guid: 101, claims: {}, awareness: {}, quality: {}, guards: {}};
  assert.equal(validView("/api/persona/insight?guid=101", insight), true);
  assert.equal(validView("/api/persona/insight?guid=101", {member: {}}), false);
  assert.equal(validView("/api/persona/insight?guid=101", {...insight, awareness: null}), false);
});

test("per-view drafts have a fixed bound and a recent edit survives eviction", () => {
  var drafts = new BoundedMap(3);
  drafts.set("guild", "one").set("w:101", "two").set("w:102", "three");
  drafts.set("guild", "edited"); drafts.set("w:103", "four");
  assert.deepEqual(Array.from(drafts.keys()), ["w:102", "guild", "w:103"]);
  assert.equal(drafts.get("guild"), "edited");
  for (var i = 0; i < 1000; i++) { drafts.set(String(i), "draft"); }
  assert.equal(drafts.size, 3);
});

test("Ops action status rejects malformed success and has independent freshness", () => {
  var status = {enabled: true, reason: "", operation: null};
  assert.equal(validView("/api/ops/action", status), true);
  for (var bad of [{...status, enabled: "true"}, {...status, operation: []},
    {...status, operation: undefined}, {...status, reason: null}, {...status, operation: {state: "done"}}]) {
    assert.equal(validView("/api/ops/action", bad), false);
  }
  assert.equal(validView("/api/ops/action", {...status, operation: {
    id: "fixture", action: "snapshot", state: "done", message: "Snapshot saved."}}), true);
  var health = new ReadHealth();
  health.update("/api/ops/action", "ops", true, 100);
  health.update("/api/ops/action", "ops", false, 200);
  health.update("/api/ops", "ops", true, 300);
  assert.equal(health.failed("ops").length, 1);
});

test("the always-on Ops badge cannot clear a failed Ops panel request", () => {
  var health = new ReadHealth();
  health.update("/api/ops", "ops", true, 100);
  health.update("/api/ops", "ops", false, 200);
  health.update("/api/ops/badge", "ops", true, 300);
  assert.deepEqual(health.failed("ops"), [{tab: "ops", good: 100, failed: true}]);
  health.update("/api/ops", "ops", true, 400);
  assert.deepEqual(health.failed("ops"), []);
});
