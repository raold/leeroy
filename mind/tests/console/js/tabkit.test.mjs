import test from "node:test";
import assert from "node:assert/strict";

// stubs for what tabs.mjs and tabkit.mjs touch at load; fetch is set per test
var banners = {};
function box(id) {
  return banners[id] || (banners[id] = { id: id, hidden: true, textContent: "", addEventListener() {} });
}
globalThis.document = {
  hidden: false,
  querySelector() { return { addEventListener() {} }; },
  querySelectorAll() { return []; },
  getElementById: box,
  addEventListener() {},
};
var core = await import("../../../leeroy_mind/console/static/js/core.mjs");
var { S } = await import("../../../leeroy_mind/console/static/js/state.mjs");
var { pollTab } = await import("../../../leeroy_mind/console/static/js/tabkit.mjs");

function tick() { return new Promise(function (r) { setImmediate(r); }); }
function answer(status, body) { return { status: status, json: function () { return Promise.resolve(body); } }; }

function rig(name, replies, render) {
  var t = { calls: 0, pending: [], rendered: [], lost: 0 };
  globalThis.fetch = function () {
    t.calls++;
    return new Promise(function (resolve) {
      t.pending.push(function () { resolve(replies.shift()); });
    });
  };
  core.on("key-lost", function () { t.lost++; });
  S.tab = name;
  document.hidden = false;
  box(name + "-banner").hidden = true;
  box(name + "-banner").textContent = "";
  t.once = pollTab(name, { path: "/api/" + name, ms: 1000, render: render || function (v) { t.rendered.push(v); } });
  t.finish = async function () { await tick(); while (t.pending.length) { t.pending.shift()(); await tick(); } await tick(); };
  return t;
}

test("a render that throws costs one poll, not the tab", async () => {
  var n = 0;
  var t = rig("a", [answer(200, { x: 1 }), answer(200, { x: 2 })], function (v) { n++; if (n === 1) { throw new Error("bad"); } t.rendered.push(v); });
  t.once(); await t.finish();
  assert.equal(box("a-banner").hidden, false);
  t.once(); await t.finish();
  assert.equal(t.calls, 2);
  assert.deepEqual(t.rendered, [{ x: 2 }]);
});
test("a 503 shows the banner with the error", async () => {
  var t = rig("b", [answer(503, { ok: false, error: "mind.db could not be read (X)" })]);
  t.once(); await t.finish();
  assert.equal(box("b-banner").hidden, false);
  assert.equal(box("b-banner").textContent, "mind.db could not be read (X)");
  assert.equal(t.rendered.length, 0);
});
test("a 403 says the key is lost", async () => {
  var t = rig("c", [answer(403, {})]);
  t.once(); await t.finish();
  assert.equal(t.lost, 1);
  assert.equal(t.rendered.length, 0);
});
test("no request while the page is hidden or another tab is open", async () => {
  var t = rig("d", [answer(200, {})]);
  document.hidden = true;
  t.once(); await t.finish();
  assert.equal(t.calls, 0);
  document.hidden = false; S.tab = "other";
  t.once(); await t.finish();
  assert.equal(t.calls, 0);
});
test("never two requests in flight", async () => {
  var t = rig("e", [answer(200, { n: 1 }), answer(200, { n: 2 })]);
  t.once(); t.once(); await tick();
  assert.equal(t.calls, 1);
  await t.finish();
  t.once(); await t.finish();
  assert.equal(t.calls, 2);
});
test("a failed request lets the next poll go", async () => {
  var t = rig("f", [answer(200, { n: 1 })]);
  var real = globalThis.fetch, first = true;
  globalThis.fetch = function (u, o) { if (first) { first = false; t.calls++; return Promise.reject(new Error("down")); } return real(u, o); };
  t.once(); await t.finish();
  t.once(); await t.finish();
  assert.equal(t.rendered.length, 1);
});
