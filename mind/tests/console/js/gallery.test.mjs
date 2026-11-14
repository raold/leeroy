import test from "node:test";
import assert from "node:assert/strict";
import { ordered, initial, step, swipe, coverWhy, lookWords, SWIPE_PX } from "../../../leeroy_mind/console/static/js/gallerymodel.mjs";

test("photos stay newest first and start at the cover without reordering", function () {
  const rows = [{ id: "a", rendered: 1, cover: true }, { id: "b", rendered: 3 }, { id: "c", rendered: 2 }];
  const photos = ordered(rows);
  assert.deepEqual(photos.map(p => p.id), ["b", "c", "a"]);
  assert.equal(initial(photos), 2);
  assert.equal(initial([]), 0);
  assert.equal(initial([{ id: "a" }]), 0);
  assert.equal(rows[0].id, "a");
});
test("navigation stops at both ends", function () {
  assert.equal(step(0, -1, 3), 0);
  assert.equal(step(2, 1, 3), 2);
  assert.equal(step(1, -1, 3), 0);
  assert.equal(step(0, 1, 0), 0);
});
test("swipes need a clear horizontal movement", function () {
  assert.equal(swipe(-SWIPE_PX, 0), 1);
  assert.equal(swipe(SWIPE_PX, 10), -1);
  assert.equal(swipe(SWIPE_PX - 1, 0), 0);
  assert.equal(swipe(100, 100), 0);
});
test("cover controls say why they are disabled", function () {
  const p = { kind: "candidate", cover: false }, state = { mind: { ok: true }, control: { enabled: true } };
  assert.equal(coverWhy(p, state), "");
  assert.match(coverWhy(p, null), /Reading/);
  assert.match(coverWhy(p, { mind: { ok: false } }), /mind isn't running/);
  assert.equal(coverWhy(p, { control: { enabled: false, reason: "off" } }), "off");
  assert.match(coverWhy({ kind: "snap" }, state), /cannot be a cover/);
  assert.match(coverWhy({ ...p, cover: true }, state), /current cover/);
  assert.match(coverWhy(p, state, true), /Saving/);
});
test("a varied set's candidate names its look, other photos nothing", function () {
  assert.equal(lookWords({ look: { category: "swim", view: "behind", label: "swimwear, from behind" } }), "swimwear, from behind");
  assert.equal(lookWords({ kind: "candidate" }), "");
  assert.equal(lookWords({ look: { label: 7 } }), "");
  assert.equal(lookWords(null), "");
});
