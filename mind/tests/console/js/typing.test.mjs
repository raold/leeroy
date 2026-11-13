import test from "node:test";
import assert from "node:assert/strict";
import { STALE_S, staleIn, typingByView, typingLabel, whisperTypers }
  from "../../../leeroy_mind/console/static/js/comms/typing.mjs";

// Invented members only (the test cast): he is 1; members are 101 to 105. SINCE is the mind's
// clock (epoch seconds); GOT is when this device received the strip, on its own clock (ms).
var HIM = 1, SINCE = 1793304000, GOT = 5000000, HOUR_MS = 3600 * 1000;
var NAMES = { 101: "Arkon", 102: "Belra", 103: "Coros", 104: "Dunna", 105: "Ebbo" };

test("Discord typing stays in its channel or private DM, separate from game whispers", () => {
  const found = at([typing(101, { channel: "discord:general" }), typing(102, { channel: "dm:102", to: HIM }),
    typing(103, { channel: "dm:102", to: HIM }), typing(104, { channel: "dm:104", to: 999 })]);
  assert.deepEqual(names(found, "d:general"), ["Arkon"]);
  assert.deepEqual(names(found, "dm:102"), ["Belra"]);
  assert.equal(found.size, 2); assert.deepEqual(whisperTypers(found), []);
});

test("group typing stays with its group, expires and clears after delivery", () => {
  const list = [typing(101, { channel: "gdm:1" }), typing(102, { channel: "gdm:2" }),
    typing(103, { channel: "gdm:0" }), typing(104, { channel: "gdm:1", age: 31 })];
  const found = at(list);
  assert.deepEqual(names(found, "gdm:1"), ["Arkon"]);
  assert.deepEqual(names(found, "gdm:2"), ["Belra"]);
  assert.equal(found.size, 2);
  const delivered = at(list, { lines: view => view === "gdm:1"
    ? [{ from_guid: 101, ts: SINCE - 1 }] : [] });
  assert.deepEqual(names(delivered, "gdm:1"), []);
  assert.deepEqual(names(delivered, "gdm:2"), ["Belra"]);
});

function typing(guid, o) {
  return Object.assign({ guid: guid, name: NAMES[guid], channel: "guild", to: null, since: SINCE - 2,
                         age: 2 }, o);
}
function at(list, o) {
  return typingByView(list, Object.assign({ receivedAt: GOT, now: GOT, human: HIM }, o));
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
  var v = at([typing(103, { since: SINCE - 1, age: 1 }), typing(101, { since: SINCE - 5, age: 5 }),
              typing(102, { since: SINCE - 3, age: 3 }), typing(101, { since: SINCE - 1, age: 1 })]);
  assert.deepEqual(names(v, "guild"), ["Arkon", "Belra", "Coros"]);
  assert.equal(typingLabel(names(v, "guild")), "Several people are typing");
});

test("an entry goes stale by its age plus the time since the strip came", () => {
  assert.equal(STALE_S, 30);
  assert.deepEqual(names(at([typing(101, { age: 29 })]), "guild"), ["Arkon"]);
  assert.deepEqual(names(at([typing(101, { age: 30 })]), "guild"), ["Arkon"]);
  assert.equal(at([typing(101, { age: 31 })]).size, 0);
  assert.deepEqual(names(at([typing(101, { age: 31 }), typing(102)]), "guild"), ["Belra"]);
  assert.deepEqual(names(at([typing(101, { age: 20 })], { now: GOT + 9000 }), "guild"), ["Arkon"]);
  assert.equal(at([typing(101, { age: 20 })], { now: GOT + 11000 }).size, 0);  // the mind died mid-turn
});

test("the device clock's own time never matters, only time since the strip came", () => {
  var list = [typing(101, { age: 5 }), typing(103, { channel: "whisper", to: HIM, age: 25 })];
  var plain = at(list, { now: GOT + 3000 });
  [-HOUR_MS, HOUR_MS, -24 * HOUR_MS, 365 * 24 * HOUR_MS].forEach(function (off) {
    var v = at(list, { receivedAt: GOT + off, now: GOT + off + 3000 });
    assert.deepEqual(v, plain, "device clock off by " + off + " ms");
  });
  assert.deepEqual(names(plain, "guild"), ["Arkon"]);
  assert.deepEqual(names(plain, "w:103"), ["Coros"]);
  // the mind's since is far from this device's clock: it is only matched against lines
  assert.deepEqual(names(at([typing(101, { since: 12, age: 1 })], { now: GOT + 1000 }), "guild"), ["Arkon"]);
  // a device clock set back after the strip came counts as no time passed
  assert.deepEqual(names(at([typing(101, { age: 29 })], { now: GOT - HOUR_MS }), "guild"), ["Arkon"]);
});

test("staleIn says when the next entry goes stale", () => {
  assert.equal(staleIn(at([])), Infinity);
  var v = at([typing(101, { age: 20 }), typing(102, { channel: "officer", age: 10 })], { now: GOT + 4000 });
  assert.equal(staleIn(v), 6);
});

test("nothing typed, absent or malformed shows nothing", () => {
  assert.equal(at(undefined).size, 0);
  assert.equal(at(null).size, 0);
  assert.equal(at("typing").size, 0);
  assert.equal(at([]).size, 0);
  assert.equal(at([null, 7, "x", { guid: "101", channel: "guild", since: SINCE, age: 1 },
                   typing(0), typing(-3), typing(1.5), typing(101, { since: "now" }),
                   typing(101, { since: NaN }), typing(101, { channel: 7 }),
                   typing(101, { age: undefined }), typing(101, { age: -1 }), typing(101, { age: "2" }),
                   typing(101, { age: NaN }), typing(101, { age: Infinity })]).size, 0);
});

test("only the places Comms shows: no voice room, say or another's whisper", () => {
  var v = at([typing(101, { channel: "voice:kitchen" }), typing(102, { channel: "say" }),
              typing(103, { channel: "yell" }), typing(104, { channel: "whisper", to: 105 }),
              typing(101, { channel: "constructor" }), typing(102, { channel: "toString" }),
              typing(105, { channel: "whisper", to: null })]);
  assert.equal(v.size, 0);
});

test("a line that landed after the turn began takes its writer's dots away", () => {
  var lines = { guild: [{ id: 9, ts: SINCE - 1, from_guid: 101, channel: "guild" },
                        { id: 10, ts: SINCE - 4, from_guid: 104, channel: "guild" }],
                "w:102": [{ id: 11, ts: SINCE - 10, from_guid: 102, channel: "whisper", to_guid: HIM }] };
  var o = { lines: function (view) { return lines[view] || []; } };
  var v = at([typing(101, { since: SINCE - 2 }), typing(103),
              typing(104, { since: SINCE - 4 }),  // his line has the turn's own second: not yet
              typing(102, { channel: "whisper", to: HIM, since: SINCE - 2 })], o);
  assert.deepEqual(names(v, "guild"), ["Dunna", "Coros"]);  // Arkon's line is in: no dots for that turn
  assert.deepEqual(names(v, "w:102"), ["Belra"]);  // her whisper came before this turn began
  v = at([typing(101, { since: SINCE - 1, age: 1 })], o);  // his next line, begun as the first one landed
  assert.deepEqual(names(v, "guild"), ["Arkon"]);
});

test("a nameless entry takes the page's name for the member", () => {
  var v = at([typing(101, { name: "" })], { name: function (g) { return "#" + g; } });
  assert.deepEqual(names(v, "guild"), ["#101"]);
  assert.deepEqual(names(at([typing(101, { name: undefined })]), "guild"), ["#101"]);
});

test("the whisper list marks each member writing to him", () => {
  var v = at([typing(101), typing(102, { channel: "whisper", to: HIM }),
              typing(104, { channel: "whisper", to: HIM, since: SINCE - 9, age: 9 })]);
  assert.deepEqual(whisperTypers(v), [{ guid: 104, name: "Dunna" }, { guid: 102, name: "Belra" }]);
  assert.deepEqual(whisperTypers(at([typing(101)])), []);
});
