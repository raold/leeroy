import test from "node:test";
import assert from "node:assert/strict";
import { conversationRows, hiddenWords, rowLabel, shortcut } from "../../../leeroy_mind/console/static/js/comms/convmodel.mjs";
import { feedHeight, FEED_MIN } from "../../../leeroy_mind/console/static/js/comms/layoutmodel.mjs";

const dms = [
  { guid: 101, name: "Arkon", last_id: 0, last_ts: 0, unread: 0 },
  { guid: 102, name: "Belra", last_id: 40, last_ts: 500, unread: 0 },
  { guid: 103, name: "Coros", last_id: 55, last_ts: 300, unread: 2 },
  { guid: 104, name: "Dessa", last_id: 0, last_ts: 0, unread: 0 },
];
const threads = [
  { guid: 105, name: "Elwin", unread: 0, last: { id: 9, ts: 900 } },
  { guid: 102, name: "Belra", unread: 1, last: { id: 7, ts: 100 } },
];

test("unread first, then the newest, across DMs and whispers", () => {
  const { rows, hidden } = conversationRows({ dms, threads, writing: [], open: "guild", find: "" });
  assert.deepEqual(rows.map(r => r.view), ["dm:103", "w:102", "w:105", "dm:102"]);
  assert.equal(hidden, 2);
  assert.match(hiddenWords(hidden), /^2 more members have no messages yet/);
  assert.equal(hiddenWords(0), "");
  assert.match(hiddenWords(1), /^1 more member has/);
});

test("a DM with no message shows once it is open or looked for", () => {
  const open = conversationRows({ dms, threads, writing: [], open: "dm:101", find: "" });
  assert.ok(open.rows.some(r => r.view === "dm:101"));
  assert.equal(open.hidden, 1);
  const found = conversationRows({ dms, threads, writing: [], open: "guild", find: "  DES " });
  assert.deepEqual(found.rows.map(r => r.view), ["dm:104"]);
  assert.equal(found.hidden, 0);
  const both = conversationRows({ dms, threads, writing: [], open: "guild", find: "belra" });
  assert.deepEqual(both.rows.map(r => r.view), ["w:102", "dm:102"]);
  assert.deepEqual(conversationRows({ dms, threads, writing: [], open: "guild", find: "zzz" }).rows, []);
});

test("a member writing a first whisper leads the list; one already listed gets the dots", () => {
  const { rows } = conversationRows({ dms, threads, open: "guild", find: "",
    writing: [{ guid: 110, name: "Juniper" }, { guid: 105, name: "Elwin" }] });
  assert.equal(rows[0].view, "w:110");
  assert.ok(rows[0].fresh && rows[0].typing);
  assert.ok(rows.find(r => r.view === "w:105").typing);
  assert.equal(rowLabel(rows.find(r => r.view === "dm:103")), "Coros, Discord DM, 2 unread");
  assert.equal(rowLabel(rows[0]), "Juniper, WoW whispers, typing");
});

test("malformed rows never throw", () => {
  assert.deepEqual(conversationRows({}).rows, []);
  const odd = conversationRows({ dms: [{ guid: 1 }], threads: [{ guid: 2, last: null }], open: "dm:1" });
  assert.equal(odd.rows.length, 2);
});

test("the shortcuts: slash for the reply box unless typing, Ctrl+K for the filter", () => {
  assert.equal(shortcut({ key: "/" }, false), "reply");
  assert.equal(shortcut({ key: "/" }, true), "");
  assert.equal(shortcut({ key: "k", ctrlKey: true }, true), "find");
  assert.equal(shortcut({ key: "k", metaKey: true }, false), "find");
  assert.equal(shortcut({ key: "k" }, false), "");
  assert.equal(shortcut({ key: "/", ctrlKey: true }, false), "");
});

test("the feed is as tall as the window has left, never shorter than its minimum", () => {
  assert.equal(feedHeight(768, 250, 18), 768 - 250 - 18 - 8);
  assert.equal(feedHeight(400, 380, 8), FEED_MIN);
  assert.equal(feedHeight(NaN, 0, 0), FEED_MIN);
});
