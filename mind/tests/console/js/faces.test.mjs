import test from "node:test";
import assert from "node:assert/strict";
import { FaceCache } from "../../../leeroy_mind/console/static/js/facecache.mjs";
import { initials, paletteSlot } from "../../../leeroy_mind/console/static/js/names.mjs";

function tick() { return new Promise(function (r) { setImmediate(r); }); }

function rig(answers) {
  var t = { now: 0, loads: [], waiting: [] };
  t.cache = new FaceCache({
    now: function () { return t.now; },
    load: function (key) {
      t.loads.push(key);
      return new Promise(function (resolve, reject) {
        t.waiting.push(function () {
          try { resolve(typeof answers === "function" ? answers(key, t.loads.length) : answers); }
          catch (e) { reject(e); }
        });
      });
    },
  });
  t.settle = async function () {
    await tick();
    while (t.waiting.length) { t.waiting.shift()(); await tick(); }
  };
  return t;
}

test("a 404 is answered from memory for 60 s and loaded again after", async () => {
  var t = rig(null);
  var p = t.cache.get("1:sm"); await t.settle();
  assert.equal(await p, null);
  assert.equal(await t.cache.get("1:sm"), null);
  t.now = 59999;
  assert.equal(await t.cache.get("1:sm"), null);
  assert.equal(t.loads.length, 1);
  t.now = 60000;
  var q = t.cache.get("1:sm"); await t.settle(); await q;
  assert.equal(t.loads.length, 2);
});

test("five keys at once start only four loads", async () => {
  var t = rig("blob:x");
  var ps = ["a", "b", "c", "d", "e"].map(function (k) { return t.cache.get(k); });
  await tick();
  assert.equal(t.loads.length, 4);
  await t.settle();
  await Promise.all(ps);
  assert.equal(t.loads.length, 5);
});

test("a photo reloaded after 60 s replaces the entry", async () => {
  var t = rig(function (key, n) { return "blob:" + n; });
  var p = t.cache.get("k"); await t.settle();
  assert.equal(await p, "blob:1");
  assert.equal(await t.cache.get("k"), "blob:1");
  t.now = 61000;
  var q = t.cache.get("k"); await t.settle();
  assert.equal(await q, "blob:2");
  assert.equal(await t.cache.get("k"), "blob:2");
});

test("two askers of one key share one load", async () => {
  var t = rig("blob:1");
  var a = t.cache.get("k"), b = t.cache.get("k"); await t.settle();
  assert.equal(await a, "blob:1");
  assert.equal(await b, "blob:1");
  assert.equal(t.loads.length, 1);
});

test("a failed load is remembered as no photo", async () => {
  var t = rig(function () { throw new Error("x"); });
  var p = t.cache.get("k"); await t.settle();
  assert.equal(await p, null);
});

test("initials and palette", () => {
  assert.equal(initials("arkon"), "AR");
  assert.equal(initials(""), "?");
  assert.equal(initials("  "), "?");
  assert.equal(initials("x"), "X");
  assert.equal(paletteSlot(1109), 5);
  assert.equal(paletteSlot(8), 0);
});
