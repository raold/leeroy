import test from "node:test";
import assert from "node:assert/strict";
import { postCount, boardEmpty, qualityClass, itemText, minutes } from "../../../leeroy_mind/console/static/js/boardmodel.mjs";

test("posts are counted over every thread", () => {
  assert.equal(postCount([{ posts: [1, 2] }, { posts: [3] }, {}]), 3);
  assert.equal(postCount(undefined), 0);
});
test("the board is empty only with nothing in all three lists", () => {
  assert.equal(boardEmpty({ forum: [], letters: [], writeups: [] }), true);
  assert.equal(boardEmpty({ forum: [], letters: [{}], writeups: [] }), false);
  assert.equal(boardEmpty(null), true);
});
test("quality classes are clamped and default to common", () => {
  assert.equal(qualityClass(4), "q4");
  assert.equal(qualityClass(9), "q5");
  assert.equal(qualityClass(-1), "q0");
  assert.equal(qualityClass("x"), "q1");
});
test("an item chip shows its count only above one", () => {
  assert.equal(itemText({ name: "Blade", count: 2 }), "Blade x2");
  assert.equal(itemText({ name: "Blade", count: 1 }), "Blade");
});
test("minutes of an event", () => {
  assert.equal(minutes({ start: 0, end: 1800 }), 30);
  assert.equal(minutes({ start: 100, end: 50 }), 0);
});
