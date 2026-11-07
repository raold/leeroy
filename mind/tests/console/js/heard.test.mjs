import test from "node:test";
import assert from "node:assert/strict";
import { HEARD_WAIT_MS, Heard } from "../../../leeroy_mind/console/static/js/comms/heard.mjs";

// His push-to-talk the voice service recognised, until the mind's own voiced line shows it was
// matched. Invented text only.

var voiced = { you: true, channel: "voice:guild", voice: "guild" };  // as the feed sends it

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
  h.sawOwnVoiceLine({ you: false, channel: "voice:guild", voice: "guild" }, 3000);
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

test("an older own line replayed within the wait does not clear it", () => {
  var h = new Heard();
  h.heardYou({ text: "hello there", ts: 10000 }, 10000);
  h.sawOwnVoiceLine({ you: true, channel: "voice:guild", voice: "guild", ts: 5 }, 12000);
  assert.equal(h.due(20000).length, 1);
});

test("a line with a time near or after the entry clears it; one without a time uses receipt", () => {
  var h = new Heard();
  h.heardYou({ text: "a", ts: 0 }, 10000);
  h.sawOwnVoiceLine({ you: true, channel: "voice:guild", voice: "guild", ts: 9 }, 12000);
  assert.deepEqual(h.due(20000), []);
  h.heardYou({ text: "b", ts: 0 }, 30000);
  h.sawOwnVoiceLine({ you: true, channel: "voice:guild", voice: "guild", ts: 31 }, 32000);
  assert.deepEqual(h.due(40000), []);
});

test("the voice service's ts, future or past, does not move the due time", () => {
  var h = new Heard();
  h.heardYou({ text: "future", ts: 9e12 }, 1000);
  h.heardYou({ text: "past", ts: 0 }, 100000);
  assert.deepEqual(h.due(1000 + HEARD_WAIT_MS + 1).map(function (e) { return e.text; }), ["future"]);
  assert.deepEqual(h.due(100000 + HEARD_WAIT_MS - 1), []);
  assert.deepEqual(h.due(100000 + HEARD_WAIT_MS + 1).map(function (e) { return e.text; }), ["past"]);
});

test("clear drops pending entries", () => {
  var h = new Heard();
  h.heardYou({ text: "x", ts: 1 }, 1000);
  h.clear();
  assert.deepEqual(h.due(20000), []);
});

test("his spoken line in a private room (stored as a whisper) clears it", () => {
  var h = new Heard();
  h.heardYou({ text: "hey you there" }, 1000);
  h.sawOwnVoiceLine({ id: 9, channel: "whisper", you: true, voice: "whisper", heard: true, ts: 1.5 }, 1500);
  assert.deepEqual(h.due(1000 + HEARD_WAIT_MS + 1), []);
});

test("a typed line of his, whispered or in guild, does not clear it", () => {
  var h = new Heard();
  h.heardYou({ text: "typed instead" }, 1000);
  h.sawOwnVoiceLine({ id: 10, channel: "whisper", you: true, voice: null, heard: null, ts: 1.5 }, 1500);
  h.sawOwnVoiceLine({ id: 11, channel: "guild", you: true, voice: null, heard: null, ts: 1.5 }, 1500);
  assert.equal(h.due(1000 + HEARD_WAIT_MS + 1).length, 1);
});
