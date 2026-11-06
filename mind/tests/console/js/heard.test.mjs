import test from "node:test";
import assert from "node:assert/strict";
import { HEARD_WAIT_MS, Heard } from "../../../leeroy_mind/console/static/js/comms/heard.mjs";

// His push-to-talk the voice service recognised, until the mind's own voiced line shows it was
// matched. Invented text only.

var voiced = { you: true, channel: "voice:guild" };

test("the wait is six seconds", () => { assert.equal(HEARD_WAIT_MS, 6000); });

test("an entry with no own voiced line is due after the wait, once", () => {
  var h = new Heard();
  h.heardYou({ text: "hello there", ts: 1000 }, 1000);
  assert.deepEqual(h.due(1000 + HEARD_WAIT_MS - 1), []);
  assert.deepEqual(h.due(1000 + HEARD_WAIT_MS + 1), [{ text: "hello there", ts: 1000 }]);
  assert.deepEqual(h.due(1000 + 2 * HEARD_WAIT_MS), []);
});

test("an own voiced line within the wait clears it", () => {
  var h = new Heard();
  h.heardYou({ text: "hello there", ts: 1000 }, 1000);
  h.sawOwnVoiceLine(voiced, 3000);
  assert.deepEqual(h.due(20000), []);
});

test("a line on another channel, or not his, does not clear it", () => {
  var h = new Heard();
  h.heardYou({ text: "hello there", ts: 1000 }, 1000);
  h.sawOwnVoiceLine({ you: true, channel: "guild" }, 3000);
  h.sawOwnVoiceLine({ you: false, channel: "voice:guild" }, 3000);
  assert.equal(h.due(20000).length, 1);
});

test("empty text is ignored", () => {
  var h = new Heard();
  h.heardYou({ text: "   ", ts: 1000 }, 1000);
  h.heardYou({ text: "", ts: 1000 }, 1000);
  h.heardYou(null, 1000);
  assert.deepEqual(h.due(20000), []);
});

test("a sixth entry drops the oldest", () => {
  var h = new Heard();
  for (var i = 1; i <= 6; i++) { h.heardYou({ text: "t" + i, ts: i }, i); }
  assert.deepEqual(h.due(20000).map(function (e) { return e.text; }), ["t2", "t3", "t4", "t5", "t6"]);
});

test("an entry already due before a later line stays due", () => {
  var h = new Heard();
  h.heardYou({ text: "old", ts: 0 }, 0);
  h.heardYou({ text: "new", ts: 5000 }, 5000);
  h.sawOwnVoiceLine(voiced, 7000);
  assert.deepEqual(h.due(7000).map(function (e) { return e.text; }), ["old"]);
  assert.deepEqual(h.due(20000), []);
});
