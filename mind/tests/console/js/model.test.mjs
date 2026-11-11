import test from "node:test";
import assert from "node:assert/strict";
import { Feed, KEEP, VIEW_LIMIT, READ_LIMIT, kpis, lineChips, viewOf } from "../../../leeroy_mind/console/static/js/comms/model.mjs";

// Invented members and lines only. He is 1000; members are 1101 and up.
var HIM = 1000;
var NAMES = { 1101: "Arkon", 1102: "Bexley", 1103: "Corwyn", 1104: "Dunmore", 1105: "Elsa" };

function mk(id, o) {
  return Object.assign({ id: id, ts: 5000 + id, channel: "guild", from: NAMES[1101], from_guid: 1101,
                         to: "", to_guid: 0, text: "line " + id, you: false, voice: null, heard: null }, o);
}
function said(id, guid, o) { return mk(id, Object.assign({ from: NAMES[guid], from_guid: guid }, o)); }
function mine(id, o) { return mk(id, Object.assign({ from: "Owner", from_guid: HIM, you: true }, o)); }
function toHim(id, guid, o) {
  return said(id, guid, Object.assign({ channel: "whisper", to_guid: HIM }, o));
}
function fromHim(id, guid, o) {
  return mine(id, Object.assign({ channel: "whisper", to: NAMES[guid], to_guid: guid }, o));
}
function ids(lines) { return lines.map(function (l) { return l.id; }); }

test("days of changing whisper views bound every index and preserve the active view", () => {
  var f = new Feed({human: 1}); f.active = "w:101";
  f.upsert([mk(1), mk(2, {channel: "whisper", from_guid: 101})]);
  var marks = {};
  for (var i = 200; i < 1200; i++) { marks["w:" + i] = i; }
  f.loadRead(marks);
  assert.equal(Object.keys(f.marks).length, READ_LIMIT);
  for (var i = 200; i < 1200; i++) {
    f.upsert([mk(i, {channel: "whisper", from_guid: i})]);
    f.openThread(i, "Fixture"); f.markRead("w:" + i);
  }
  assert.equal(f.views.size, VIEW_LIMIT); assert.equal(f.opened.size, VIEW_LIMIT);
  assert.ok(f.where.size <= VIEW_LIMIT * KEEP);
  assert.ok(Object.keys(f.readState()).length <= READ_LIMIT);
  assert.deepEqual(ids(f.lines("w:101")), [2]); assert.deepEqual(ids(f.lines("guild")), [1]);
  var retained = new Set(Array.from(f.views.values()).flatMap(m => Array.from(m.keys())));
  assert.deepEqual(new Set(f.where.keys()), retained);
  assert.ok(!f.where.has(200));
});

test("each channel has its view", () => {
  assert.equal(viewOf(mk(1, { channel: "guild" }), HIM), "guild");
  assert.equal(viewOf(mk(1, { channel: "voice:kitchen", voice: "kitchen" }), HIM), "guild");
  assert.equal(viewOf(mk(1, { channel: "voice:guild_hall", voice: "guild_hall" }), HIM), "guild");
  assert.equal(viewOf(mk(1, { channel: "officer" }), HIM), "officer");
  assert.equal(viewOf(mk(1, { channel: "voice:officers", voice: "officers" }), HIM), "officer");
  assert.equal(viewOf(mk(1, { channel: "party" }), HIM), "group");
  assert.equal(viewOf(mk(1, { channel: "raid" }), HIM), "group");
  assert.equal(viewOf(mk(1, { channel: "raid_warning" }), HIM), "group");
  assert.equal(viewOf(toHim(1, 1101), HIM), "w:1101");
  assert.equal(viewOf(fromHim(1, 1102), HIM), "w:1102");
  assert.equal(viewOf(fromHim(1, 1102, { you: false }), HIM), "w:1102");  // known by his guid too
  assert.equal(viewOf(mk(1, { channel: "say" }), HIM), "");
  assert.equal(viewOf(mk(1, { channel: "whisper", from_guid: 0, to_guid: 0 }), HIM), "");
});

test("a line on its way is hidden until it has played, and drop removes it", () => {
  var f = new Feed();
  f.upsert([said(1, 1102, { channel: "voice:kitchen", voice: "kitchen", heard: false })]);
  assert.deepEqual(f.lines("guild"), []);
  f.upsert([said(1, 1102, { channel: "voice:kitchen", voice: "kitchen", heard: true })]);
  assert.deepEqual(ids(f.lines("guild")), [1]);
  assert.equal(f.lines("guild")[0].heard, true);
  f.drop([1]);
  assert.deepEqual(f.lines("guild"), []);
  f.drop([99]);  // a line it never had
});

test("lines come oldest first and a repeated id replaces the line", () => {
  var f = new Feed();
  f.upsert([mk(3), mk(1), mk(2)]);
  assert.deepEqual(ids(f.lines("guild")), [1, 2, 3]);
  f.upsert([mk(2, { text: "fixed" })]);
  assert.equal(f.lines("guild")[1].text, "fixed");
  assert.equal(f.lines("guild").length, 3);
  assert.deepEqual(f.lines("officer"), []);
  f.upsert([null, { id: "4" }, mk(5, { channel: "say" })]);
  assert.deepEqual(f.ids(), [1, 2, 3]);
});

test("history seen on first open is read", () => {
  var f = new Feed();
  f.loadRead({});
  var history = [];
  for (var i = 1; i <= 50; i++) {
    var kind = i % 5;
    history.push(kind === 0 ? said(i, 1102, { channel: "officer" })
                 : kind === 1 ? said(i, 1103, { channel: "raid" })
                 : kind === 2 ? toHim(i, 1104)
                 : said(i, 1101, { channel: i % 2 ? "voice:kitchen" : "guild", voice: i % 2 ? "kitchen" : null }));
  }
  f.upsert(history);
  ["guild", "officer", "group", "w:1104"].forEach(function (v) { assert.equal(f.unread(v), 0, v); });
  assert.equal(f.lines("guild").length > 0, true);
  f.upsert([said(51, 1101)]);
  assert.equal(f.unread("guild"), 1);
  f.upsert([toHim(52, 1105)]);  // a member who never whispered before
  assert.equal(f.unread("w:1105"), 1);
  assert.equal(f.unread("officer"), 0);
});

test("a snapshot replaces the feed, and one whose read failed keeps the lines and the floor", () => {
  var f = new Feed();
  assert.equal(f.adopt(null), false);  // the first snapshot could not read the feed
  assert.equal(f.floor, null);
  assert.equal(f.adopt([mk(1), mk(2), toHim(3, 1101)]), true);  // the next one could
  assert.equal(f.floor, 3);
  ["guild", "w:1101"].forEach(function (v) { assert.equal(f.unread(v), 0, v); });
  assert.equal(f.adopt(null), false);  // a reconnect whose read failed
  assert.deepEqual(f.ids(), [1, 2, 3]);
  assert.equal(f.floor, 3);
  f.adopt([mk(2), mk(3, { channel: "guild" }), mk(4)]);  // one gone, one moved, one new
  assert.deepEqual(f.ids(), [2, 3, 4]);
  assert.deepEqual(ids(f.lines("guild")), [2, 3, 4]);
  assert.equal(f.unread("guild"), 1);
  f.adopt([]);  // an empty feed is a feed
  assert.deepEqual(f.ids(), []);
});

test("his own lines are never unread, and reading moves the mark", () => {
  var f = new Feed();
  f.upsert([said(1, 1101)]);
  f.upsert([mine(2), mine(3, { channel: "voice:guild_hall", voice: "guild_hall", heard: true })]);
  assert.equal(f.unread("guild"), 0);
  f.upsert([said(4, 1102), said(5, 1103)]);
  assert.equal(f.unread("guild"), 2);
  assert.equal(f.markRead("guild"), true);
  assert.equal(f.unread("guild"), 0);
  assert.equal(f.markRead("guild"), false);  // nothing new
  f.upsert([said(6, 1101, { channel: "voice:kitchen", voice: "kitchen", heard: false })]);
  assert.equal(f.unread("guild"), 0);  // not shown yet
  f.upsert([said(7, 1101)]);
  assert.equal(f.unread("guild"), 1);
});

test("the read marks survive a reload", () => {
  var history = [said(1, 1101), said(2, 1102, { channel: "officer" }), toHim(3, 1101)];
  var first = new Feed();
  first.loadRead({});
  first.upsert(history);
  var later = [said(4, 1102), said(5, 1103), said(6, 1101, { channel: "officer" }), toHim(7, 1102)];
  first.upsert(later);
  first.markRead("officer");
  var stored = JSON.parse(JSON.stringify(first.readState()));
  var again = new Feed();
  again.loadRead(stored);
  again.upsert(history.concat(later));  // the page's next snapshot holds them all
  assert.equal(again.unread("guild"), 2);
  assert.equal(again.unread("officer"), 0);
  assert.equal(again.unread("w:1101"), 0);
  assert.equal(again.unread("w:1102"), 1);
});

test("stored marks that make no sense are ignored", () => {
  var f = new Feed();
  f.loadRead({ guild: "7", officer: -2, group: 2.5, "w:x": 3, "w:1101": 0, junk: 4, "w:1102": 3 });
  f.loadRead(null);
  f.loadRead("guild");
  f.upsert([said(1, 1101), toHim(2, 1102), toHim(4, 1102)]);
  assert.equal(f.unread("guild"), 0);
  assert.equal(f.unread("w:1102"), 1);  // its stored mark (3) wins over the first load
  assert.deepEqual(Object.keys(f.readState()).sort(), ["guild", "w:1102"]);
});

test("a busy guild channel never pushes the whispers out", () => {
  var f = new Feed();
  f.upsert([toHim(1, 1101), fromHim(2, 1101), toHim(3, 1102)]);
  var busy = [];
  for (var i = 4; i < 4 + 250; i++) { busy.push(said(i, 1103)); }
  f.upsert(busy);
  assert.equal(f.lines("w:1101").length, 2);
  assert.equal(f.lines("w:1102").length, 1);
  assert.equal(f.lines("guild").length, KEEP);
  assert.equal(f.lines("guild")[0].id, 4 + 250 - KEEP);
});

test("threads come newest first and an opened thread is kept empty", () => {
  var clock = 1e9;
  var f = new Feed({ now: function () { return clock; } });
  f.upsert([toHim(1, 1103), toHim(2, 1101), fromHim(3, 1102), fromHim(4, 1101, { heard: false })]);
  var t = f.threads();
  assert.deepEqual(t.map(function (x) { return x.guid; }), [1102, 1101, 1103]);
  assert.deepEqual(t.map(function (x) { return x.name; }), ["Bexley", "Arkon", "Corwyn"]);
  assert.equal(t[1].last.id, 2);
  f.upsert([toHim(5, 1103)]);
  t = f.threads();
  assert.equal(t[0].guid, 1103);
  assert.equal(t[0].unread, 1);
  assert.equal(f.openThread(1105, "Elsa"), "w:1105");
  t = f.threads();
  assert.deepEqual(t[0], { guid: 1105, name: "Elsa", last: null, unread: 0 });
  assert.equal(t.length, 4);
  assert.deepEqual(f.lines("w:1105"), []);
  f.drop([1, 5]);  // every line of a thread gone: it goes too, unless he opened it
  assert.deepEqual(f.threads().map(function (x) { return x.guid; }), [1105, 1102, 1101]);
});

test("the KPI band counts tonight's lines, whispers, voice and the mood", () => {
  var f = new Feed();
  f.upsert([
    said(1, 1101, { ts: 900 }),  // before tonight
    toHim(2, 1104, { ts: 950 }),  // an old whisper, still unanswered, not tonight's
    said(3, 1101, { ts: 1000 }),
    said(4, 1102, { ts: 1100, channel: "voice:kitchen", voice: "kitchen", heard: true }),
    said(5, 1103, { ts: 1150, channel: "voice:kitchen", voice: "kitchen", heard: false }),  // not played
    toHim(6, 1101, { ts: 1200 }),
    fromHim(7, 1101, { ts: 1210 }),
    toHim(8, 1102, { ts: 1300, voice: "whisper", heard: true }),
    mine(9, { ts: 1400, channel: "officer" })]);
  var voice = { where: { 1101: "guild_hall", 1102: "guild_hall", 1103: "kitchen", 1000: "guild_hall", 1105: "" },
                room: "guild_hall", human: HIM, names: {} };
  var mood = { night: "2026-11-05", value: 0.4, reasons: ["after the first kill", "loot went well"] };
  assert.deepEqual(kpis(f, voice, mood, 1000), [
    { k: "Said tonight", v: "6", d: "2 on voice" },
    { k: "Whispers to you", v: "2", d: "1 unanswered" },
    { k: "In voice", v: "4", d: "across 2 rooms" },
    { k: "Guild mood", v: "+0.40", d: "after the first kill" }]);
});

test("the KPI band without voice, mood or lines", () => {
  var f = new Feed();
  assert.deepEqual(kpis(f, null, null, 0), [
    { k: "Said tonight", v: "0", d: "0 on voice" },
    { k: "Whispers to you", v: "0", d: "0 unanswered" },
    { k: "In voice", v: "-", d: "voice not connected" },
    { k: "Guild mood", v: "-", d: "not recorded yet" }]);
  var alone = { where: {}, room: "kitchen", human: 0 };
  assert.deepEqual(kpis(f, alone, { value: -0.25, reasons: [] }, 0).slice(2), [
    { k: "In voice", v: "1", d: "across 1 room" },
    { k: "Guild mood", v: "-0.25", d: "" }]);
});

test("a whisper he did not hear (deafened, or the member muted) is text with its marker, and unread", () => {
  var f = new Feed();
  f.upsert([toHim(1, 1101)]);  // the first load: history is read
  f.upsert([toHim(2, 1101, { unheard: "deafened" }), toHim(3, 1102, { unheard: "muted" })]);
  assert.deepEqual(ids(f.lines("w:1101")), [1, 2]);
  assert.equal(f.unread("w:1101"), 1);
  assert.equal(f.unread("w:1102"), 1);
  assert.equal(f.threads().reduce(function (n, t) { return n + t.unread; }, 0), 2);
  assert.deepEqual(lineChips(f.lines("w:1101")[1]), [{ cls: "chip wow", text: "whisper" },
                                                       { cls: "chip unheard", text: "not heard (deafened)" }]);
  assert.deepEqual(lineChips(f.lines("w:1102")[0]), [{ cls: "chip wow", text: "whisper" },
                                                       { cls: "chip unheard", text: "not heard (muted)" }]);
  assert.deepEqual(lineChips(f.lines("w:1101")[0]), [{ cls: "chip wow", text: "whisper" }]);
  assert.deepEqual(lineChips(toHim(4, 1101, { unheard: "expired" })), [{ cls: "chip wow", text: "whisper" }]);
  // It was not said on voice: the band counts it as a whisper to him, not as spoken
  assert.deepEqual(kpis(f, null, null, 0).slice(0, 2), [
    { k: "Said tonight", v: "3", d: "0 on voice" },
    { k: "Whispers to you", v: "3", d: "2 unanswered" }]);
});

test("each line's chips say where it was said", () => {
  var room = function (id) { return id === "kitchen" ? "Kitchen" : id; };
  assert.deepEqual(lineChips(said(1, 1101, { channel: "voice:kitchen", voice: "kitchen", heard: true }), room),
                   [{ cls: "chip voice", text: "Kitchen" }]);
  assert.deepEqual(lineChips(toHim(2, 1101, { voice: "whisper", heard: true }), room),
                   [{ cls: "chip voice", text: "voice" }]);
  assert.deepEqual(lineChips(mk(3, { channel: "raid_warning" }), room), [{ cls: "chip wow", text: "raid warning" }]);
  assert.deepEqual(lineChips(mk(4, { channel: "guild" }), room), [{ cls: "chip wow", text: "guild" }]);
});
