import test from "node:test";
import assert from "node:assert/strict";
import { FaceCache } from "../../../leeroy_mind/console/static/js/facecache.mjs";
import { initials, paletteSlot } from "../../../leeroy_mind/console/static/js/names.mjs";

function tick() { return new Promise(function (r) { setImmediate(r); }); }

function rig(answers) {
  var t = { now: 0, loads: [], waiting: [], dropped: [] };
  t.cache = new FaceCache({
    now: function () { return t.now; },
    drop: function (v) { t.dropped.push(v); },
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

test("a 404 is answered from memory for 5 minutes and loaded again after", async () => {
  var t = rig(null);
  var p = t.cache.get("1:sm"); await t.settle();
  assert.equal(await p, null);
  assert.equal(await t.cache.get("1:sm"), null);
  t.now = 60000;
  assert.equal(await t.cache.get("1:sm"), null);
  t.now = 299999;
  assert.equal(await t.cache.get("1:sm"), null);
  assert.equal(t.loads.length, 1);
  t.now = 300000;
  var q = t.cache.get("1:sm"); await t.settle(); await q;
  assert.equal(t.loads.length, 2);
});

test("a failed load is a miss for 5 minutes too", async () => {
  var t = rig(function () { throw new Error("x"); });
  var p = t.cache.get("k"); await t.settle(); await p;
  t.now = 60000;
  assert.equal(await t.cache.get("k"), null);
  assert.equal(t.loads.length, 1);
});

test("a photo stays at hand while it is reloaded", async () => {
  var t = rig(function (key, n) { return "blob:" + n; });
  assert.equal(t.cache.peek("k"), undefined);
  var p = t.cache.get("k");
  assert.equal(t.cache.peek("k"), undefined);  // no answer yet
  await t.settle(); await p;
  assert.equal(t.cache.peek("k"), "blob:1");
  t.now = 61000;
  var q = t.cache.get("k");
  assert.equal(t.cache.peek("k"), "blob:1");  // the old one until the new one is in
  assert.deepEqual(t.dropped, []);
  await t.settle(); await q;
  assert.equal(t.cache.peek("k"), "blob:2");
  assert.deepEqual(t.dropped, ["blob:1"]);
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

// -- faces.mjs on a stand-in page: a face drawn again reuses the photo it already decoded --------

class FakeEl {
  constructor(tag) {
    this.tagName = tag.toUpperCase(); this.className = ""; this.textContent = ""; this.alt = "";
    this.src = ""; this.children = []; this.parentNode = null; this.dataset = {};
    this.style = { props: {}, setProperty: function (k, v) { this.props[k] = v; } };
  }
  appendChild(c) { c.parentNode = this; this.children.push(c); return c; }
  replaceChild(n, o) {
    this.children[this.children.indexOf(o)] = n;
    n.parentNode = this; o.parentNode = null;
  }
  addEventListener() {}
  cloneNode() {
    var c = new FakeEl(this.tagName);
    c.src = this.src; c.alt = this.alt; c.className = this.className;
    return c;
  }
  decode() { return this.src.indexOf("blob:broken") === 0 ? Promise.reject(new Error("bad")) : Promise.resolve(); }
}

var page = { asked: [], made: 0 };
async function stage() {
  if (page.face) { return page.face; }
  globalThis.document = { createElement: function (t) { return new FakeEl(t); } };
  globalThis.fetch = function (path) {
    page.asked.push(path);
    var guid = /guid=(\d+)/.exec(path)[1];
    if (guid === "2201") { return Promise.resolve({ status: 200, blob: function () { return Promise.resolve({ tag: "ok" }); } }); }
    if (guid === "2203") { return Promise.resolve({ status: 200, blob: function () { return Promise.resolve({ tag: "broken" }); } }); }
    return Promise.resolve({ status: 404 });
  };
  URL.createObjectURL = function (b) { return "blob:" + b.tag + "/" + (++page.made); };
  URL.revokeObjectURL = function () {};
  page.face = (await import("../../../leeroy_mind/console/static/js/faces.mjs")).face;
  return page.face;
}
async function settled() { for (var i = 0; i < 10; i++) { await tick(); } }
function askedFor(guid) { return page.asked.filter(function (p) { return p.indexOf("guid=" + guid + "&") >= 0; }).length; }

test("a face drawn again reuses the photo already decoded", async () => {
  var face = await stage();
  var row = document.createElement("div");
  var first = row.appendChild(face(2201, "Arkon", "feed"));
  assert.equal(first.tagName, "SPAN");  // the initials until the photo is in
  await settled();
  assert.equal(row.children[0].tagName, "IMG");
  var src = row.children[0].src;
  var again = face(2201, "Arkon", "feed");  // the next render: the photo at once, no circle first
  assert.equal(again.tagName, "IMG");
  assert.equal(again.src, src);
  assert.notEqual(again, row.children[0]);  // its own copy, so the feed can hold many
  assert.equal(again.className, "av");
  assert.equal(face(2201, "Arkon", "feed", { speaking: true }).className, "av speaking");
  assert.equal(askedFor(2201), 1);
});

test("a member without a photo is asked for once, not at every render", async () => {
  var face = await stage();
  var a = face(2202, "Bexley", "feed");
  await settled();
  var b = face(2202, "Bexley", "feed");
  await settled();
  assert.equal(a.tagName, "SPAN");
  assert.equal(b.tagName, "SPAN");
  assert.equal(b.textContent, "BE");
  assert.equal(askedFor(2202), 1);
});

test("a photo that does not decode keeps the initials and is not asked for again", async () => {
  var face = await stage();
  var row = document.createElement("div");
  row.appendChild(face(2203, "Corwyn", "feed"));
  await settled();
  assert.equal(row.children[0].tagName, "SPAN");
  assert.equal(face(2203, "Corwyn", "feed").tagName, "SPAN");
  await settled();
  assert.equal(askedFor(2203), 1);
});
