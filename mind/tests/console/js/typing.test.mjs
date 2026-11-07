import test from "node:test";
import assert from "node:assert/strict";
import { STALE_S, typingByView, typingLabel, whisperTypers }
  from "../../../leeroy_mind/console/static/js/comms/typing.mjs";

// Invented members only (the test cast): he is 1; members are 101 to 105.
var HIM = 1, NOW = 1793304000;
var NAMES = { 101: "Arkon", 102: "Belra", 103: "Coros", 104: "Dunna", 105: "Ebbo" };

function typing(guid, o) {
  return Object.assign({ guid: guid, name: NAMES[guid], channel: "guild", to: null, since: NOW - 2 }, o);
}
function at(list, o) {
  return typingByView(list, Object.assign({ now: NOW, human: HIM }, o));
}
function names(byView, view) { return (byView.get(view) || []).map(function (t) { return t.name; }); }

test("the label names one or two and counts no further", () => {
  assert.equal(typingLabel([]), "");
  assert.equal(typingLabel(["Arkon"]), "Arkon is typing");
  assert.equal(typingLabel(["Arkon", "Belra"]), "Arkon and Belra are typing");
  assert.equal(typingLabel(["Arkon", "Belra", "Coros"]), "Several people are typing");
  assert.equal(typingLabel(["Arkon", "Belra", "Coros", "Dunna"]), "Several people are typing");
});

test("each turn shows where its line will appear", () => {
  var v = at([typing(101), typing(102, { channel: "officer" }), typing(103, { channel: "party" }),
              typing(104, { channel: "raid" }), typing(105, { channel: "whisper", to: HIM })]);
  assert.deepEqual(names(v, "guild"), ["Arkon"]);
  assert.deepEqual(names(v, "officer"), ["Belra"]);
  assert.deepEqual(names(v, "group"), ["Coros", "Dunna"]);
  assert.deepEqual(names(v, "w:105"), ["Ebbo"]);
  assert.deepEqual(Array.from(v.keys()).sort(), ["group", "guild", "officer", "w:105"]);
});

test("several in one place come oldest first, each member once", () => {
  var v = at([typing(103, { since: NOW - 1 }), typing(101, { since: NOW - 5 }),
              typing(102, { since: NOW - 3 }), typing(101, { since: NOW - 1 })]);
  assert.deepEqual(names(v, "guild"), ["Arkon", "Belra", "Coros"]);
  assert.equal(typingLabel(names(v, "guild")), "Several people are typing");
});

test("a stale entry is dropped: the mind may have died mid-turn", () => {
  assert.equal(STALE_S, 30);
  assert.deepEqual(names(at([typing(101, { since: NOW - 29 })]), "guild"), ["Arkon"]);
  assert.equal(at([typing(101, { since: NOW - 31 })]).size, 0);
  assert.deepEqual(names(at([typing(101, { since: NOW - 31 }), typing(102)]), "guild"), ["Belra"]);
});

test("nothing typed, absent or malformed shows nothing", () => {
  assert.equal(at(undefined).size, 0);
  assert.equal(at(null).size, 0);
  assert.equal(at("typing").size, 0);
  assert.equal(at([]).size, 0);
  assert.equal(at([null, 7, "x", { guid: "101", channel: "guild", since: NOW },
                   typing(0), typing(-3), typing(1.5), typing(101, { since: "now" }),
                   typing(101, { since: NaN }), typing(101, { channel: 7 })]).size, 0);
});

test("only the places Comms shows: no voice room, say or another's whisper", () => {
  var v = at([typing(101, { channel: "voice:kitchen" }), typing(102, { channel: "say" }),
              typing(103, { channel: "yell" }), typing(104, { channel: "whisper", to: 105 }),
              typing(101, { channel: "constructor" }), typing(102, { channel: "toString" }),
              typing(105, { channel: "whisper", to: null })]);
  assert.equal(v.size, 0);
});

test("a line that landed takes its writer's dots away until a later turn", () => {
  var lines = { guild: [{ id: 9, ts: NOW - 1, from_guid: 101, channel: "guild" }],
                "w:102": [{ id: 10, ts: NOW - 10, from_guid: 102, channel: "whisper", to_guid: HIM }] };
  var o = { lines: function (view) { return lines[view] || []; } };
  var v = at([typing(101, { since: NOW - 2 }), typing(103),
              typing(102, { channel: "whisper", to: HIM, since: NOW - 2 })], o);
  assert.deepEqual(names(v, "guild"), ["Coros"]);  // Arkon's line is in: no dots for that turn
  assert.deepEqual(names(v, "w:102"), ["Belra"]);  // her whisper came before this turn began
  v = at([typing(101, { since: NOW - 0.5 })], o);   // his next line, typed after the first one landed
  assert.deepEqual(names(v, "guild"), ["Arkon"]);
});

test("a nameless entry takes the page's name for the member", () => {
  var v = at([typing(101, { name: "" })], { name: function (g) { return "#" + g; } });
  assert.deepEqual(names(v, "guild"), ["#101"]);
  assert.deepEqual(names(at([typing(101, { name: undefined })]), "guild"), ["#101"]);
});

test("the whisper list marks each member writing to him", () => {
  var v = at([typing(101), typing(102, { channel: "whisper", to: HIM }),
              typing(104, { channel: "whisper", to: HIM, since: NOW - 9 })]);
  assert.deepEqual(whisperTypers(v), [{ guid: 104, name: "Dunna" }, { guid: 102, name: "Belra" }]);
  assert.deepEqual(whisperTypers(at([typing(101)])), []);
});
