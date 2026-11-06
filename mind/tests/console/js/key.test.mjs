import test from "node:test";
import assert from "node:assert/strict";
import { KEY_REFUSED, api, hashKey, keyGen, on, post, refused, setKey } from "../../../leeroy_mind/console/static/js/core.mjs";

// The console's key in the URL's fragment: at start and when he pastes a new URL into the open tab,
// and a refusal of a key no longer in use, which says nothing of the new one

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

// A fake fetch: each request's key header, answered when the test says
function fakeFetch() {
  var f = { sent: [], answers: [] };
  globalThis.fetch = function (path, opts) {
    f.sent.push(opts.headers["X-Console-Key"]);
    return new Promise(function (resolve) { f.answers.push(resolve); });
  };
  return f;
}
function forbidden() {
  return { status: 403, json: function () { return Promise.reject(new SyntaxError("not JSON")); } };
}
function tick() { return new Promise(function (r) { setTimeout(r, 0); }); }

test("a 403 to a request sent with an older key is not the new key's loss", async () => {
  var lost = 0;
  on("key-lost", function () { lost++; });
  var f = fakeFetch();
  setKey("old-key-0123456789");
  var gen = keyGen();
  var poll = api("/api/raid"), reply = post("/api/say", { channel: "guild", text: "hi" });
  setKey("new-key-0123456789");  // pasted while both were on their way
  assert.equal(keyGen(), gen + 1);
  f.answers[0](forbidden());
  f.answers[1](forbidden());
  assert.equal(refused(await poll), true);  // still a refusal: the poll's answer is dropped
  var j = await reply;
  assert.equal(j.ok, false);
  assert.equal(j.status, 403);
  assert.equal(j.error, KEY_REFUSED);
  assert.equal(lost, 0);
  // A refusal of the key in use now is the key's loss, from a poll or a change
  var now = api("/api/raid");
  f.answers[2](forbidden());
  assert.equal(refused(await now), true);
  assert.equal(lost, 1);
  var change = post("/api/say", { channel: "guild", text: "hi" });
  f.answers[3](forbidden());
  await change;
  await tick();
  assert.equal(lost, 2);
  assert.deepEqual(f.sent, ["old-key-0123456789", "old-key-0123456789", "new-key-0123456789", "new-key-0123456789"]);
  // Anything else is no refusal
  var fine = api("/api/raid");
  f.answers[4]({ status: 200 });
  assert.equal(refused(await fine), false);
  assert.equal(lost, 2);
});
