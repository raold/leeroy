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
  assert.equal(viewOf(mk(1, { channel: "voice:kitchen", voice: "kitchen" }), HIM), "voice");
  assert.equal(viewOf(mk(1, { channel: "voice:guild_hall", voice: "guild_hall" }), HIM), "voice");
  assert.equal(viewOf(mk(1, { channel: "officer" }), HIM), "officer");
  assert.equal(viewOf(mk(1, { channel: "voice:officers", voice: "officers" }), HIM), "voice");
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
  assert.deepEqual(f.lines("voice"), []);
  f.upsert([said(1, 1102, { channel: "voice:kitchen", voice: "kitchen", heard: true })]);
  assert.deepEqual(ids(f.lines("voice")), [1]);
  assert.equal(f.lines("voice")[0].heard, true);
  f.drop([1]);
  assert.deepEqual(f.lines("voice"), []);
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
  ["guild", "officer", "group", "voice", "w:1104"].forEach(function (v) { assert.equal(f.unread(v), 0, v); });
  assert.equal(f.lines("voice").length > 0, true);
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
  assert.equal(f.unread("voice"), 0);  // his own spoken line
  f.upsert([said(6, 1101, { channel: "voice:kitchen", voice: "kitchen", heard: false })]);
  assert.equal(f.unread("voice"), 0);  // not shown yet
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

test("the voice transcript is its own view: every room he heard, who said it, never # guild", () => {
  var f = new Feed({ human: HIM });
  f.upsert([said(1, 1101), said(2, 1102, { channel: "voice:guild_hall", voice: "guild_hall", heard: true }),
            said(3, 1103, { channel: "voice:officers", voice: "officers", heard: true }),
            mine(4, { channel: "voice:guild_hall", voice: "guild_hall", heard: true })]);
  assert.deepEqual(ids(f.lines("guild")), [1]);
  assert.deepEqual(ids(f.lines("officer")), []);
  assert.deepEqual(ids(f.lines("voice")), [2, 3, 4]);
  assert.deepEqual(f.lines("voice").map(function (l) { return l.from; }), ["Bexley", "Corwyn", "Owner"]);
  f.upsert([said(5, 1104, { channel: "voice:kitchen", voice: "kitchen", heard: true })]);
  assert.equal(f.unread("voice"), 1);
  assert.equal(f.markRead("voice"), true);
  assert.deepEqual(Object.keys(f.readState()).sort(), ["guild", "voice"]);
});

test("a view reads back through its history and a later snapshot keeps what it read", () => {
  var f = new Feed();
  assert.equal(f.adopt([mk(10), mk(11), toHim(12, 1101)], 5010), true);  // ts is 5000 + id
  assert.equal(f.oldest("guild"), 10);
  assert.equal(f.oldest("officer"), 0);
  assert.equal(f.mayHaveMore("guild"), true);
  assert.equal(f.known("guild"), false);
  f.older("guild", { lines: [mk(3), mk(4)], more: true });
  f.older("w:1101", { lines: [toHim(2, 1101)], more: false });
  assert.deepEqual(ids(f.lines("guild")), [3, 4, 10, 11]);
  assert.equal(f.oldest("guild"), 3);
  assert.equal(f.known("guild"), true);
  assert.equal(f.mayHaveMore("w:1101"), false);
  ["guild", "w:1101"].forEach(function (v) { assert.equal(f.unread(v), 0, v); });  // history is read
  f.older("w:1102", { lines: [toHim(1, 1102)], more: false });
  f.upsert([toHim(14, 1103)]);
  // A reconnect: 10 and 14 went (from the snapshot's start on); what it does not cover stays
  f.adopt([mk(11), toHim(12, 1101), mk(13)], 5010);
  assert.deepEqual(ids(f.lines("guild")), [3, 4, 11, 13]);
  assert.deepEqual(ids(f.lines("w:1101")), [2, 12]);
  assert.deepEqual(ids(f.lines("w:1102")), [1]);
  assert.deepEqual(ids(f.lines("w:1103")), []);
  assert.equal(f.mayHaveMore("w:1103"), true);
  // A line older than the snapshot's start but newer than its view's oldest line there went too
  f.adopt([mk(3), mk(11), mk(13), toHim(12, 1101)], 5011);
  assert.deepEqual(ids(f.lines("guild")), [3, 11, 13]);
  // An older console's snapshot (no start) is the whole feed
  f.adopt([mk(13)]);
  assert.deepEqual(f.ids(), [13]);
  assert.equal(f.mayHaveMore("guild"), true);
});

test("threads list every member he ever whispered with, read back when one opens", () => {
  var f = new Feed({ human: HIM });
  f.adopt([toHim(10, 1101)], 5010);
  assert.equal(f.setThreads(null), false);
  assert.equal(f.setThreads([{ guid: 1101, name: "Arkon", last: toHim(10, 1101) },
                             { guid: 1104, name: "Dunmore", last: fromHim(2, 1104) },
                             { guid: 0, name: "nobody", last: mk(1) }, { guid: 1105, name: "Elsa" }]), true);
  var t = f.threads();
  assert.deepEqual(t.map(function (x) { return x.guid; }), [1101, 1104]);
  assert.deepEqual(t[1], { guid: 1104, name: "Dunmore", last: fromHim(2, 1104), unread: 0 });
  assert.deepEqual(f.lines("w:1104"), []);
  assert.equal(f.known("w:1104"), false);
  f.older("w:1104", { lines: [toHim(1, 1104), fromHim(2, 1104)], more: false });
  assert.deepEqual(ids(f.lines("w:1104")), [1, 2]);
  assert.equal(f.unread("w:1104"), 0);
  f.setThreads([]);  // the index is replaced, the lines read stay
  assert.deepEqual(f.threads().map(function (x) { return x.guid; }), [1101, 1104]);
});

test("the open view keeps every line read back; another goes back to its newest lines", () => {
  var f = new Feed();
  f.active = "guild";
  f.adopt([mk(300)], 5300);
  var page = [];
  for (var i = 1; i <= 250; i++) { page.push(mk(i)); }
  f.older("guild", { lines: page, more: false });
  assert.equal(f.lines("guild").length, 251);
  f.active = "officer";
  f.upsert([mk(301)]);
  assert.equal(f.lines("guild").length, KEEP);
  assert.equal(f.lines("guild")[KEEP - 1].id, 301);
  assert.equal(f.mayHaveMore("guild"), true);
});

test("a reconnect keeps history a busy view's snapshot did not reach", () => {
  var f = new Feed(), snap = [];
  for (var i = 101; i <= 100 + KEEP; i++) { snap.push(mk(i)); }
  f.adopt(snap, 5000);  // its window reaches past them all: the view was cut at KEEP lines
  f.active = "guild";
  var page = [];
  for (var j = 1; j <= 100; j++) { page.push(mk(j)); }  // read back, inside the window too
  f.older("guild", { lines: page, more: false });
  f.upsert([said(400, 1102, { channel: "officer" }), said(401, 1102, { channel: "officer" })]);
  f.adopt(snap.slice(1).concat([mk(301), said(401, 1102, { channel: "officer" })]), 5000, ["guild"]);
  assert.equal(f.lines("guild").length, 301);
  assert.equal(f.lines("guild")[0].id, 1);
  // A quiet view's snapshot sent all of it from the window's start: a line it lacks went
  assert.deepEqual(ids(f.lines("officer")), [401]);
  // A whisper thread the snapshot cut keeps its older lines too
  f.older("w:1101", { lines: [toHim(350, 1101, { ts: 5001 })], more: false });
  f.adopt(snap.slice(1).concat([mk(301), toHim(402, 1101)]), 5000, ["guild", "w:1101"]);
  assert.deepEqual(ids(f.lines("w:1101")), [350, 402]);
});

test("a history page whose window changed before it came back is dropped", () => {
  var f = new Feed();
  f.adopt([toHim(101, 1101), toHim(102, 1101)], 0);
  var from = f.oldest("w:1101");
  for (var g = 2000; g < 2000 + VIEW_LIMIT; g++) { f.upsert([toHim(g, g)]); }  // evicted meanwhile
  assert.equal(f.views.has("w:1101"), false);
  assert.equal(f.older("w:1101", { lines: [toHim(1, 1101)], more: false }, from), false);
  assert.deepEqual(f.lines("w:1101"), []);
  assert.equal(f.known("w:1101"), false);  // opened again, it reads its newest lines back
  // The same window: the page joins on
  f.adopt([toHim(101, 1101), toHim(102, 1101)], 0);
  assert.equal(f.older("w:1101", { lines: [toHim(1, 1101)], more: false }, f.oldest("w:1101")), true);
  assert.deepEqual(ids(f.lines("w:1101")), [1, 101, 102]);
  // A view that held nothing takes any page
  assert.equal(f.older("w:1102", { lines: [toHim(5, 1102)], more: false }, 0), true);
});

test("history that comes back after he left the view keeps Load earlier", () => {
  var f = new Feed();
  f.adopt([], 0);
  f.active = "guild";
  var live = [];
  for (var i = 101; i <= 300; i++) { live.push(mk(i)); }
  f.upsert(live);
  f.active = "officer";  // he left before the page came back
  var page = [];
  for (var j = 1; j <= 100; j++) { page.push(mk(j)); }
  f.older("guild", { lines: page, more: false });
  assert.equal(f.lines("guild").length, KEEP);
  assert.equal(f.lines("guild")[0].id, 101);
  assert.equal(f.mayHaveMore("guild"), true);
});

test("live whispers keep the thread list current, also after a thread's lines are evicted", () => {
  var f = new Feed({ human: HIM });
  f.adopt([toHim(1, 1101)], 0);
  f.setThreads([{ guid: 1101, name: "Arkon", last: toHim(1, 1101) }]);
  f.upsert([fromHim(2, 1101)]);  // he answered
  for (var g = 2000; g < 2000 + VIEW_LIMIT; g++) { f.upsert([toHim(g, g)]); }
  assert.equal(f.views.has("w:1101"), false);
  var t = f.threads().filter(function (x) { return x.guid === 1101; })[0];
  assert.equal(t.last.id, 2);
  assert.equal(t.name, "Arkon");
  // A line on its way is not its thread's newest yet; a thread whose every line went leaves
  f.upsert([toHim(3001, 1104, { heard: false })]);
  assert.equal(f.threads().some(function (x) { return x.guid === 1104; }), false);
  f.upsert([toHim(3002, 1105)]);
  f.drop([3002]);
  assert.equal(f.threads().some(function (x) { return x.guid === 1105; }), false);
});

test("a thread the snapshot left out while it moved on is read again from its newest line", () => {
  var f = new Feed({ human: HIM });
  f.adopt([toHim(1, 1101)], 0);
  f.older("w:1101", { lines: [], more: false });
  // Reconnect: 1101 wrote again (id 2), then other threads filled the snapshot's whisper budget
  f.setThreads([{ guid: 1101, name: "Arkon", last: toHim(2, 1101) }, { guid: 1102, name: "Bexley", last: toHim(9, 1102) }]);
  f.adopt([toHim(9, 1102)], 0, ["w:1101", "w:1102"]);
  assert.deepEqual(ids(f.lines("w:1101")), [1]);  // kept, with a gap read from its newest line
  assert.equal(f.gapped("w:1101"), true);
  assert.equal(f.oldest("w:1101"), 0);
  assert.equal(f.threads()[1].last.id, 2);       // still listed, with its newest line
  assert.equal(f.older("w:1101", { lines: [toHim(1, 1101), toHim(2, 1101)], more: false }, 0), true);
  assert.equal(f.gapped("w:1101"), false);
  assert.deepEqual(ids(f.lines("w:1101")), [1, 2]);
  // A cut thread that is not behind keeps its lines and opens no gap
  f.adopt([toHim(9, 1102)], 0, ["w:1101", "w:1102"]);
  assert.deepEqual(ids(f.lines("w:1101")), [1, 2]);
  assert.equal(f.gapped("w:1101"), false);
});

test("a gap between read-back history and a newer cut snapshot is read back until it closes", () => {
  var f = new Feed();
  f.active = "guild";
  f.adopt([mk(300)], 0);
  var page = [];
  for (var i = 1; i < 300; i++) { page.push(mk(i)); }
  f.older("guild", { lines: page, more: false }, f.oldest("guild"));
  assert.equal(f.mayHaveMore("guild"), false);
  // Disconnected while 300 more were said; the snapshot holds the newest 200 (401-600)
  var snap = [];
  for (var j = 401; j <= 600; j++) { snap.push(mk(j)); }
  f.adopt(snap, 5401, ["guild"]);
  assert.equal(f.gapped("guild"), true);
  assert.equal(f.oldest("guild"), 401);
  assert.equal(f.mayHaveMore("guild"), true);
  var gap = [];
  for (var k = 351; k <= 400; k++) { gap.push(mk(k)); }
  assert.equal(f.older("guild", { lines: gap, more: true }, 401), true);  // not down to 300 yet
  assert.equal(f.gapped("guild"), true);
  assert.equal(f.oldest("guild"), 351);
  var rest = [];
  for (var n = 251; n <= 350; n++) { rest.push(mk(n)); }
  f.older("guild", { lines: rest, more: true }, 351);  // reaches the history: closed
  assert.equal(f.gapped("guild"), false);
  assert.equal(f.mayHaveMore("guild"), false);       // the history's own state again
  assert.equal(f.lines("guild").length, 600);
  // A cut snapshot with nothing read back below it opens no gap
  var g = new Feed();
  g.adopt(snap, 5401, ["guild"]);
  assert.equal(g.gapped("guild"), false);
  assert.equal(g.oldest("guild"), 401);
});

test("a reconnect while a gap fills keeps the gap's floor", () => {
  var f = new Feed();
  f.active = "guild";
  f.adopt([mk(300)], 0);
  var hist = [];
  for (var i = 1; i < 300; i++) { hist.push(mk(i)); }
  f.older("guild", { lines: hist, more: false }, f.oldest("guild"));
  var snap = [];
  for (var j = 1001; j <= 1200; j++) { snap.push(mk(j)); }
  f.adopt(snap, 6001, ["guild"]);
  var part = [];
  for (var n = 901; n <= 1000; n++) { part.push(mk(n)); }
  f.older("guild", { lines: part, more: true }, 1001);
  f.adopt(snap, 6001, ["guild"]);                   // again, before the gap is filled
  assert.equal(f.gapped("guild"), true);
  assert.equal(f.gaps.get("guild").floor, 300);
  f.older("guild", { lines: part, more: true }, f.oldest("guild"));  // 901-1000 again: not closed
  assert.equal(f.gapped("guild"), true);
  assert.equal(f.mayHaveMore("guild"), true);
});

test("history from before a long disconnect gets a gap the snapshot's start cannot see", () => {
  var f = new Feed();
  f.active = "guild";
  f.adopt([mk(1, { ts: 100 })], 0);
  f.older("guild", { lines: [], more: false }, f.oldest("guild"));
  assert.equal(f.mayHaveMore("guild"), false);
  // Line 2 was said on day 1 while away; the snapshot on day 5 starts after it
  f.adopt([mk(3, { ts: 5 * 86400 })], 2 * 86400);
  assert.deepEqual(ids(f.lines("guild")), [1, 3]);
  assert.equal(f.gapped("guild"), true);
  assert.equal(f.oldest("guild"), 3);
  f.older("guild", { lines: [mk(1, { ts: 100 }), mk(2, { ts: 86400 })], more: false }, 3);
  assert.equal(f.gapped("guild"), false);
  assert.deepEqual(ids(f.lines("guild")), [1, 2, 3]);
  assert.equal(f.mayHaveMore("guild"), false);
});

test("an empty view known complete is asked again after a reconnect", () => {
  var f = new Feed();
  f.adopt([], 0);
  f.older("officer", { lines: [], more: false }, 0);
  assert.equal(f.known("officer"), true);
  assert.equal(f.mayHaveMore("officer"), false);
  f.adopt([], 5 * 86400);  // days later, nothing in the window
  assert.equal(f.known("officer"), false);
  assert.equal(f.mayHaveMore("officer"), true);
  f.adopt([], 0);
  f.older("officer", { lines: [], more: false }, 0);
  f.adopt([]);             // an older console's snapshot leaves it as it was
  assert.equal(f.known("officer"), true);
});

test("a history page never replaces a line a live event already settled", () => {
  var f = new Feed({ human: HIM });
  f.adopt([], 0);
  f.upsert([toHim(5, 1101, { voice: "whisper", heard: true })]);  // played, from the live feed
  f.older("w:1101", { lines: [toHim(4, 1101), toHim(5, 1101, { voice: "whisper", heard: false })], more: false }, 0);
  assert.deepEqual(ids(f.lines("w:1101")), [4, 5]);
  assert.equal(f.lines("w:1101")[1].heard, true);
});
