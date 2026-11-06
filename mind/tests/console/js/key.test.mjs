import test from "node:test";
import assert from "node:assert/strict";
import { hashKey } from "../../../leeroy_mind/console/static/js/core.mjs";

// The console's key in the URL's fragment: at start and when he pastes a new URL into the open tab

var KEY = "a1B2c3D4e5F6g7H8i9J0k_-LmNoPqRsT";

test("a key fragment gives its key", () => {
  assert.equal(hashKey("#key=" + KEY), KEY);
  assert.equal(hashKey("#key=" + "x".repeat(16)), "x".repeat(16));
  assert.equal(hashKey("#key=" + "x".repeat(128)), "x".repeat(128));
});

test("a malformed key fragment gives no key", () => {
  ["#key=", "#key=short", "#key=" + "x".repeat(129), "#key=" + KEY + "&x", "#key=" + KEY.slice(0, 20) + " z"]
    .forEach(function (h) { assert.equal(hashKey(h), "", h); });
});

test("any other fragment is not a key's", () => {
  ["", "#", "#comms", "#personas", "#keys", "key=" + KEY, null, undefined].forEach(function (h) {
    assert.equal(hashKey(h), null, String(h));
  });
});
