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

test("a reload that fails keeps the photo it had; only a miss (a 404) ends it", async () => {
  var t = rig(function (key, n) {
    if (n === 1) { return "blob:1"; }
    if (n === 2) { throw new Error("503"); }
    return null;
  });
  var p = t.cache.get("k"); await t.settle(); await p;
  t.now = 61000;
  var q = t.cache.get("k"); await t.settle();
  assert.equal(await q, "blob:1");
  assert.equal(t.cache.peek("k"), "blob:1");
  assert.deepEqual(t.dropped, []);
  t.now = 120000;  // kept for another 60 s from the failed reload
  assert.equal(await t.cache.get("k"), "blob:1");
  assert.equal(t.loads.length, 2);
  t.now = 122000;
  var r = t.cache.get("k"); await t.settle();
  assert.equal(await r, null);
  assert.equal(t.cache.peek("k"), null);
  assert.deepEqual(t.dropped, ["blob:1"]);
});

test("a load that ends after forget gives no photo and lets its photo go", async () => {
  var t = rig(function (key, n) { return "blob:" + n; });
  var p = t.cache.get("k");
  await tick();
  t.cache.forget();         // a new key: what the old one loaded is not shown
  await t.settle();
  assert.equal(await p, null);
  assert.deepEqual(t.dropped, ["blob:1"]);
  assert.equal(t.cache.peek("k"), undefined);
});

test("forgetting one key loads only that one again and lets only its photo go", async () => {
  var t = rig(function (key, n) { return "blob:" + key + "/" + n; });
  var a = t.cache.get("1:sm"); await t.settle(); await a;
  var b = t.cache.get("2:sm"); await t.settle(); await b;
  t.cache.forgetKey("1:sm");
  t.cache.forgetKey("9:sm");  // never asked for: nothing to let go
  assert.deepEqual(t.dropped, ["blob:1:sm/1"]);
  assert.equal(t.cache.peek("1:sm"), undefined);
  assert.equal(t.cache.peek("2:sm"), "blob:2:sm/2");
  var c = t.cache.get("1:sm"); await t.settle();
  assert.equal(await c, "blob:1:sm/3");     // within its 60 s, but asked for again
  assert.equal(await t.cache.get("2:sm"), "blob:2:sm/2");
  assert.equal(t.loads.length, 3);
});

test("a load that ends after its key was forgotten gives no photo and lets its photo go", async () => {
  var t = rig(function (key, n) { return "blob:" + n; });
  var p = t.cache.get("k");
  await tick();
  t.cache.forgetKey("k");
  await t.settle();
  assert.equal(await p, null);
  assert.deepEqual(t.dropped, ["blob:1"]);
  assert.equal(t.cache.peek("k"), undefined);
});

test("a failed load is remembered as no photo", async () => {
  var t = rig(function () { throw new Error("x"); });
  var p = t.cache.get("k"); await t.settle();
  assert.equal(await p, null);
});

test("with retryMs a first load with no answer is asked again while its askers wait", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  var loads = 0;
  var cache = new FaceCache({ retryMs: [1000, 2000, 5000],
    load: function () { loads++; if (loads < 5) { throw new Error("no answer"); } return "blob:" + loads; } });
  var answer = "waiting";
  cache.get("k").then(function (v) { answer = v; });
  await tick();
  assert.equal(loads, 1);
  assert.equal(answer, "waiting");             // not a miss: still waiting
  assert.equal(cache.peek("k"), undefined);
  t.mock.timers.tick(999); await tick();
  assert.equal(loads, 1);
  t.mock.timers.tick(1); await tick();
  assert.equal(loads, 2);
  t.mock.timers.tick(2000); await tick();
  assert.equal(loads, 3);
  t.mock.timers.tick(4999); await tick();
  assert.equal(loads, 3);
  t.mock.timers.tick(1); await tick();
  assert.equal(loads, 4);
  t.mock.timers.tick(5000); await tick();     // the last delay is repeated
  assert.equal(loads, 5);
  await tick();
  assert.equal(answer, "blob:5");
  assert.equal(cache.active, 0);
});

test("a retried load ends as a miss only on a 404, and forgetting the key stops its retries", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  var n = 0, cache = new FaceCache({ retryMs: [1000],
    load: function (key) { n++; if (key === "gone" && n > 1) { return null; } throw new Error("no answer"); } });
  var gone = cache.get("gone");
  await tick(); t.mock.timers.tick(1000); await tick(); await tick();
  assert.equal(await gone, null);
  assert.equal(cache.peek("gone"), null);
  n = 0;
  var later = cache.get("later");
  await tick();
  assert.equal(n, 1);
  cache.forgetKey("later");
  assert.equal(await later, null);
  t.mock.timers.tick(60000); await tick();
  assert.equal(n, 1);                          // no try after the key was forgotten
  assert.equal(cache.active, 0);
});

test("a retry waits without holding one of the active loads", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  var asked = [], cache = new FaceCache({ retryMs: [1000], maxActive: 1,
    load: function (key) { asked.push(key); if (key === "bad") { throw new Error("x"); } return "blob:" + key; } });
  var bad = cache.get("bad"), good = cache.get("good");
  await tick(); await tick();
  assert.equal(await good, "blob:good");
  assert.deepEqual(asked, ["bad", "good"]);
  cache.forgetKey("bad"); await bad;
});

test("a reload answering the photo it had keeps it: nothing is let go or replaced", async () => {
  var replaced = [], seen = [];
  var t = rig(function (key, n) { return n === 1 ? { url: "blob:1" } : "same"; });
  var load = t.cache.load;
  t.cache.load = function (key, previous) {
    seen.push(previous);
    return load(key).then(function (v) { return v === "same" ? previous : v; });
  };
  t.cache.replaced = function (key, v) { replaced.push([key, v]); };
  var p = t.cache.get("k"); await t.settle();
  var first = await p;
  t.now = 61000;
  var q = t.cache.get("k"); await t.settle();
  assert.equal(await q, first);
  assert.equal(seen[0], null);                 // the first load had nothing before it
  assert.equal(seen[1], first);
  assert.deepEqual(t.dropped, []);
  assert.deepEqual(replaced, []);
  assert.equal(t.cache.peek("k"), first);
});

test("a reload with another answer calls replaced with it in place before the old photo is let go", async () => {
  var order = [];
  var t = rig(function (key, n) { return n === 1 ? "blob:1" : n === 2 ? "blob:2" : null; });
  t.cache.drop = function (v) { order.push("drop " + v + ", peek " + t.cache.peek("k")); };
  t.cache.replaced = function (key, v) { order.push("replaced " + key + " " + v + ", peek " + t.cache.peek("k")); };
  var p = t.cache.get("k"); await t.settle(); await p;
  assert.deepEqual(order, []);                 // a first answer is no replacement
  t.now = 61000;
  var q = t.cache.get("k"); await t.settle(); await q;
  assert.deepEqual(order, ["replaced k blob:2, peek blob:2", "drop blob:1, peek blob:2"]);
  t.now = 122000;
  var r = t.cache.get("k"); await t.settle(); await r;   // the photo is gone: its faces are told too
  assert.deepEqual(order.slice(2), ["replaced k null, peek null", "drop blob:2, peek null"]);
});

test("a miss that turns into a photo calls replaced, so the initials drawn for it change", async () => {
  var replaced = [];
  var t = rig(function (key, n) { return n === 1 ? null : "blob:2"; });
  t.cache.replaced = function (key, v) { replaced.push(v); };
  var p = t.cache.get("k"); await t.settle(); await p;
  t.now = 300000;
  var q = t.cache.get("k"); await t.settle(); await q;
  assert.deepEqual(replaced, ["blob:2"]);
});

test("a photo still shown is not evicted beyond maxEntries; one no longer shown is", async () => {
  var shown = new Set(["a"]), dropped = [];
  var cache = new FaceCache({ maxEntries: 2, load: function (key) { return "blob:" + key; },
    drop: function (v) { dropped.push(v); }, inUse: function (key, v) { return shown.has(key) && v === "blob:" + key; } });
  await cache.get("a"); await cache.get("b"); await cache.get("c");
  assert.deepEqual(dropped, ["blob:b"]);       // "a" is older but still on screen
  assert.equal(cache.peek("a"), "blob:a");
  shown.clear();
  await cache.get("d");
  assert.deepEqual(dropped, ["blob:b", "blob:a"]);
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
    this.listeners = {}; this.src = ""; this.children = []; this.parentNode = null; this.dataset = {};
    this.style = { props: {}, setProperty: function (k, v) { this.props[k] = v; } };
    this.attrs = {};
  }
  // An image loads a moment after its src is set; bytes that are no image, or a revoked URL, fail
  get src() { return this._src; }
  set src(v) {
    this._src = v;
    var self = this;
    if (this.tagName === "IMG" && v) {
      setImmediate(function () { self.fire(v.indexOf("blob:broken") === 0 || page.revoked.indexOf(v) >= 0 ? "error" : "load"); });
    }
  }
  fire(type) { (this.listeners[type] || []).slice().forEach((fn) => { fn({ type: type, target: this }); }); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; }
  appendChild(c) { c.parentNode = this; this.children.push(c); return c; }
  replaceChild(n, o) {
    this.children[this.children.indexOf(o)] = n;
    n.parentNode = this; o.parentNode = null;
  }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  cloneNode() {
    var c = new FakeEl(this.tagName);
    c.src = this.src; c.alt = this.alt; c.className = this.className;
    return c;
  }
  decode() { return Promise.resolve(); }
}

function blob(tag, bytes) {
  return { tag: tag, size: bytes.length, arrayBuffer: function () { return Promise.resolve(Uint8Array.from(bytes).buffer); } };
}
function ok(tag, bytes) {
  return Promise.resolve({ status: 200, blob: function () { return Promise.resolve(blob(tag, bytes || [1, 2, 3])); } });
}

var page = { asked: [], made: 0, calls: {}, roots: [], revoked: [], revokedWhileShown: [] };
// The faces under the page's roots: one member's, or all of them (guid null)
function onPage(guid) {
  var found = [];
  function walk(node) {
    if (guid === null ? node.dataset.faceGuid : node.dataset.faceGuid === guid) { found.push(node); }
    node.children.forEach(walk);
  }
  page.roots.forEach(walk);
  return found;
}
async function stage() {
  if (page.face) { return page.face; }
  globalThis.document = {
    createElement: function (t) { return new FakeEl(t); },
    querySelectorAll: function (selector) {
      var one = /data-face-guid="(\d+)"/.exec(selector);
      return onPage(one ? one[1] : null);
    }
  };
  globalThis.fetch = function (path) {
    page.asked.push(path);
    var guid = /guid=(\d+)/.exec(path)[1], n = page.calls[guid] = (page.calls[guid] || 0) + 1;
    if (guid === "2201" || guid === "101" || guid === "2206" || guid === "2208") { return ok("ok"); }
    if (guid === "2203") { return ok("broken"); }
    if (guid === "2204") {  // a photo, then a server error, then no answer at all, then gone
      if (n === 1) { return ok("ok"); }
      if (n === 2) { return Promise.resolve({ status: 503 }); }
      if (n === 3) { return Promise.reject(new TypeError("Failed to fetch")); }
      return Promise.resolve({ status: 404 });
    }
    if (guid === "2205") {  // no answer, then a server error, then the photo
      if (n === 1) { return Promise.reject(new TypeError("Failed to fetch")); }
      if (n === 2) { return Promise.resolve({ status: 502 }); }
      return ok("ok");
    }
    if (guid === "2207") { return ok("ok", [n, 7]); }  // another photo at every load
    if (guid === "2209") { return n === 1 ? Promise.resolve({ status: 403 }) : ok("ok"); }  // the key refused once
    return Promise.resolve({ status: 404 });
  };
  URL.createObjectURL = function (b) { return "blob:" + b.tag + "/" + (++page.made); };
  URL.revokeObjectURL = function (url) {
    page.revoked.push(url);
    onPage(null).forEach(function (node) { if (node.src === url) { page.revokedWhileShown.push(url); } });
  };
  page.mod = await import("../../../leeroy_mind/console/static/js/faces.mjs");
  page.face = page.mod.face;
  return page.face;
}
async function settled() { for (var i = 0; i < 10; i++) { await tick(); } }
function askedFor(guid) { return page.asked.filter(function (p) { return p.indexOf("guid=" + guid + "&") >= 0; }).length; }
// fn run as if ms had passed since now
async function later(ms, fn) {
  var real = Date.now, t0 = real();
  Date.now = function () { return t0 + ms; };
  try { return await fn(); } finally { Date.now = real; }
}

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

test("one photo serves a member's faces of every size", async () => {
  var face = await stage();
  var before = askedFor(2201);
  ["list", "feed", "room", "card", "wall"].forEach(function (kind) {
    assert.equal(face(2201, "Arkon", kind).tagName, "IMG", kind);
  });
  assert.equal(face(2201, "Arkon", "wall").className, "av wall");
  assert.equal(face(2201, "Arkon", "card").className, "av xl");
  assert.equal(askedFor(2201), before);
  assert.match(page.asked.filter(function (p) { return p.indexOf("guid=2201&") >= 0; })[0], /&size=lg$/);
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

test("a photo that is no image keeps the initials and is not asked for again", async () => {
  var face = await stage();
  var row = document.createElement("div");
  row.appendChild(face(2203, "Corwyn", "feed"));
  await settled();
  assert.equal(row.children[0].tagName, "SPAN");
  assert.equal(face(2203, "Corwyn", "feed").tagName, "SPAN");
  await settled();
  assert.equal(askedFor(2203), 1);
});

test("a first load with no answer is asked again in seconds and the initials on the page turn into the photo", async (t) => {
  var face = await stage();
  t.mock.timers.enable({ apis: ["setTimeout"] });
  var row = document.createElement("div");
  page.roots = [row];
  row.appendChild(face(2205, "Eira", "wall"));
  await settled();
  assert.equal(row.children[0].tagName, "SPAN");   // no answer yet: still waiting, not a miss
  assert.equal(askedFor(2205), 1);
  t.mock.timers.tick(1000); await settled();       // a 502
  assert.equal(askedFor(2205), 2);
  assert.equal(row.children[0].tagName, "SPAN");
  t.mock.timers.tick(2000); await settled();       // the photo, with no redraw of the tab
  assert.equal(askedFor(2205), 3);
  assert.equal(row.children[0].tagName, "IMG");
  assert.equal(row.children[0].className, "av wall");
  page.roots = [];
});

test("a known photo whose reload fails with a 5xx or no answer is kept; a 404 ends it", async () => {
  var face = await stage();
  var row = document.createElement("div");
  page.roots = [row];
  row.appendChild(face(2204, "Dagny", "feed"));
  await settled();
  assert.equal(row.children[0].tagName, "IMG");
  var src = row.children[0].src;
  await later(599000, async function () {
    face(2204, "Dagny", "feed"); await settled();
    assert.equal(askedFor(2204), 1);                             // checked again only after 10 minutes
  });
  await later(601000, async function () {
    assert.equal(face(2204, "Dagny", "feed").tagName, "IMG");    // the old photo while it reloads
    await settled();
    var a = face(2204, "Dagny", "feed");                         // the 503: still the photo
    assert.equal(a.tagName, "IMG");
    assert.equal(a.src, src);
  });
  await later(1202000, async function () {
    face(2204, "Dagny", "feed");
    await settled();
    assert.equal(face(2204, "Dagny", "feed").src, src);          // no answer: still the photo
    assert.equal(row.children[0].src, src);
  });
  await later(1803000, async function () {
    face(2204, "Dagny", "feed");
    await settled();
    var gone = face(2204, "Dagny", "feed");                      // the 404 ends it
    assert.equal(gone.tagName, "SPAN");
    assert.equal(gone.textContent, "DA");
    assert.equal(row.children[0].tagName, "SPAN");               // on the page too, at once
  });
  assert.equal(askedFor(2204), 4);
  assert.deepEqual(page.revokedWhileShown, []);
  page.roots = [];
});

test("a reload that brings the same photo keeps the image shown and lets nothing go", async () => {
  var face = await stage();
  var row = document.createElement("div");
  page.roots = [row];
  row.appendChild(face(2206, "Fenna", "wall"));
  await settled();
  var src = row.children[0].src, revoked = page.revoked.length;
  await later(601000, async function () { face(2206, "Fenna", "list"); await settled(); });
  assert.equal(askedFor(2206), 2);
  assert.equal(row.children[0].src, src);
  assert.equal(face(2206, "Fenna", "wall").src, src);
  assert.equal(page.revoked.length, revoked);
  page.roots = [];
});

test("a reload that brings another photo draws the faces on the page with it before the old URL goes", async () => {
  var face = await stage();
  var row = document.createElement("div"), detail = document.createElement("div");
  page.roots = [row, detail];
  row.appendChild(face(2207, "Gwyn", "wall"));
  detail.appendChild(face(2207, "Gwyn", "card"));
  await settled();
  var old = row.children[0].src;
  assert.equal(detail.children[0].src, old);
  await later(601000, async function () { face(2207, "Gwyn", "list"); await settled(); });
  assert.notEqual(row.children[0].src, old);
  assert.equal(row.children[0].className, "av wall");
  assert.equal(detail.children[0].src, row.children[0].src);
  assert.equal(detail.children[0].className, "av xl");
  assert.ok(page.revoked.indexOf(old) >= 0);
  assert.deepEqual(page.revokedWhileShown, []);
  page.roots = [];
});

test("an image that fails on the page is drawn again and its member asked for again", async () => {
  var face = await stage();
  var row = document.createElement("div");
  page.roots = [row];
  row.appendChild(face(2208, "Hale", "wall"));
  await settled();
  var shown = row.children[0], before = askedFor(2208);
  assert.equal(shown.tagName, "IMG");
  shown.fire("error");
  assert.equal(row.children[0].tagName, "SPAN");   // the initials while it is asked for again
  await settled();
  assert.equal(askedFor(2208), before + 1);
  assert.equal(row.children[0].tagName, "IMG");
  assert.notEqual(row.children[0].src, shown.src);
  page.roots = [];
});

test("a member's face is a button that names them for the card; his own is no button", async () => {
  await stage();
  var faceButton = page.mod.faceButton;
  var b = faceButton(2202, "Bexley", "room");
  assert.equal(b.tagName, "BUTTON");
  assert.equal(b.type, "button");
  assert.equal(b.className, "facebtn");
  assert.deepEqual([b.dataset.card, b.dataset.name, b.dataset.speaker], ["2202", "Bexley", "2202"]);
  assert.equal(b.getAttribute("aria-label"), "Bexley");
  assert.equal(b.children[0].className, "av lg");
  var lit = faceButton(2202, "Bexley", "room", { speaking: true });
  assert.equal(lit.className, "facebtn speaking");       // lit on the button, so a toggle keeps it
  assert.equal(lit.children[0].className, "av lg");
  var inside = faceButton(2202, "Bexley", "list", { inButton: true });
  assert.equal(inside.tagName, "SPAN");                   // a face inside another button
  assert.equal(inside.dataset.card, "2202");
  var me = faceButton(1000, "Owner", "feed", { you: true });
  assert.equal(me.tagName, "SPAN");
  assert.equal(me.textContent, "YOU");
  assert.equal(me.dataset.card, undefined);
});

test("a line with no member (guid 0, below 0 or not a whole number) draws initials and asks for nothing", async () => {
  var face = await stage();
  var before = page.asked.length;
  [0, -3, 1.5, NaN, Infinity, "abc", "", null, undefined].forEach(function (g) {
    var f = face(g, "Stranger", "feed");
    assert.equal(f.tagName, "SPAN", String(g));
    assert.equal(f.textContent, "ST", String(g));
  });
  await settled();
  assert.equal(page.asked.length, before);
});

test("a photo picked or undone is asked for again at once; four more leaves the face as it is", async () => {
  var face = await stage();
  var core = await import("../../../leeroy_mind/console/static/js/core.mjs");
  var row = document.createElement("div");
  row.appendChild(face(2201, "Arkon", "list"));
  row.appendChild(face(2201, "Arkon", "card"));
  await settled();
  var before = askedFor(2201);
  assert.equal(face(2201, "Arkon", "list").tagName, "IMG");  // decoded and at hand
  core.emit("selfie", { action: "more", guid: 2201 });
  assert.equal(face(2201, "Arkon", "list").tagName, "IMG");
  assert.equal(askedFor(2201), before);
  core.emit("selfie", { action: "pick", guid: 2201 });
  var again = face(2201, "Arkon", "list");
  assert.equal(again.tagName, "SPAN");                       // the old photo is not shown again
  assert.equal(face(2201, "Arkon", "card").tagName, "SPAN");  // in any size
  await settled();
  assert.equal(askedFor(2201), before + 1);                  // one photo asked for, for every size
  assert.equal(face(2201, "Arkon", "card").tagName, "IMG");
  core.emit("selfie", { action: "undo", guid: 2201 });
  assert.equal(face(2201, "Arkon", "list").tagName, "SPAN");
  core.emit("selfie", { action: "pick", guid: "2201" });     // never a guid that is not a number
  core.emit("selfie", {});
});

test("a cover change refreshes faces already on the page without waiting for its poll", async () => {
  var face = await stage(), core = await import("../../../leeroy_mind/console/static/js/core.mjs");
  var row = document.createElement("div");
  row.appendChild(face(101, "Arla", "list"));
  row.appendChild(face(101, "Arla", "card"));
  page.roots = [row];
  await settled();
  var src = row.children[0].src, before = askedFor(101);
  core.emit("selfie", { action: "pick", guid: 101 });
  await settled();
  assert.equal(askedFor(101), before + 1);
  assert.equal(row.children[0].tagName, "IMG");
  assert.notEqual(row.children[0].src, src);
  assert.equal(row.children[1].className, "av xl");
  assert.equal(row.children[1].src, row.children[0].src);
  page.roots = [];
});

test("nameCell puts opts.build's node where the face goes, whatever the cell's shape", async () => {
  await stage();
  var tr = document.createElement("tr");
  var td = page.mod.nameCell(tr, 2201, "Arkon", { build: function (g, n) { var b = document.createElement("button"); b.dataset.opener = g + n; return b; } });
  assert.equal(td.children[0].children[0].dataset.opener, "2201Arkon");
  // the name beside a gallery opener is the one-click way to the member page
  assert.equal(td.children[0].children[1].dataset.profile, "2201");
  assert.equal(td.children[0].children[1].textContent, "Arkon");
  // without a build, the face itself opens the member page and the name is plain text
  var plain = page.mod.nameCell(tr, 2201, "Arkon", {});
  assert.equal(plain.children[0].children[0].dataset.profile, "2201");
  assert.equal(plain.children[0].children[0].getAttribute("aria-label"), "Profile of Arkon");
  assert.equal(plain.children[0].children[1].dataset.profile, undefined);
  // his own row and nobody's are never links
  var you = page.mod.nameCell(tr, 900, "Hero", { you: true });
  assert.equal(you.children[0].children[0].dataset.profile, undefined);
  assert.equal(page.mod.profileButton(0, "Nobody", "list").dataset.profile, undefined);
});

test("a refused key is not asked again on its own; the next key (forgetFaces) asks again and draws the page", async (t) => {
  var face = await stage();
  t.mock.timers.enable({ apis: ["setTimeout"] });
  var row = document.createElement("div");
  page.roots = [row];
  row.appendChild(face(2209, "Iona", "wall"));
  await settled();
  assert.equal(row.children[0].tagName, "SPAN");
  t.mock.timers.tick(120000); await settled();
  assert.equal(askedFor(2209), 1);                 // asking again with the same key cannot help
  page.mod.forgetFaces();
  await settled();
  assert.equal(askedFor(2209), 2);
  assert.equal(row.children[0].tagName, "IMG");    // drawn again without a poll of its tab
  page.roots = [];
});
