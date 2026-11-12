import test from "node:test";
import assert from "node:assert/strict";
import { stale, hasDraft, reloadTarget, pageBuild } from "../../../leeroy_mind/console/static/js/update.mjs";

const doc = (fields, build) => ({
  querySelectorAll: () => fields.map((value) => ({ value })),
  querySelector: () => (build === undefined ? null : { getAttribute: () => build }),
});

test("a page reloads only when the server names another real build", () => {
  assert.equal(stale("0123456789abcdef", "fedcba9876543210"), true);
  assert.equal(stale("0123456789abcdef", "0123456789abcdef"), false);
  assert.equal(stale("0123456789abcdef", undefined), false);      // an older console sends none
  assert.equal(stale("0123456789abcdef", "not-a-build"), false);
  assert.equal(stale("", "fedcba9876543210"), false);              // no meta: never reload
  assert.equal(stale("__CONSOLE_BUILD__", "fedcba9876543210"), false);
});

test("a half-typed message holds the reload; blank fields do not", () => {
  assert.equal(hasDraft(doc(["", "   "])), false);
  assert.equal(hasDraft(doc(["", "hello"])), true);
  assert.equal(hasDraft(doc([])), false);
});

test("the reload carries the key back in the fragment", () => {
  assert.equal(reloadTarget("/", "abcDEF123_-xyzxyz"), "/#key=abcDEF123_-xyzxyz");
});

test("the page's own build comes from its meta", () => {
  assert.equal(pageBuild(doc([], "0123456789abcdef")), "0123456789abcdef");
  assert.equal(pageBuild(doc([])), "");
});
