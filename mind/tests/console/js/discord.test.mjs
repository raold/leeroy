import test from "node:test";
import assert from "node:assert/strict";
import { Discord, channelOf, groupMessages, memberGroups, mentionsHim, presenceDot, quoteOf, reactionBar, unread }
  from "../../../leeroy_mind/console/static/js/comms/discord.mjs";

function message(id, author = 101, ts = 1000, extra = {}) {
  return Object.assign({ id, author, ts, text: "Invented message", thread: null, reply_to: null, deleted: null }, extra);
}
test("Discord and WoW view ids stay separate", () => {
  assert.equal(channelOf("d:general"), "general"); assert.equal(channelOf("dm:101"), "dm:101");
  ["guild", "w:101", "d:", "dm:0", "d:<img>", "dm:101x"].forEach(v => assert.equal(channelOf(v), ""));
});
test("same author within seven minutes groups, quotes and deletions split", () => {
  const rows = [message(1), message(2, 101, 1100), message(3, 102, 1101), message(4, 102, 1600),
    message(5, 102, 1601, { reply_to: 1 }), message(6, 102, 1602, { deleted: 1602 }), message(7, 102, 1603)];
  assert.deepEqual(groupMessages(rows).map(x => x.grouped), [false, true, false, false, false, false, false]);
});
test("reply quotes are text and deleted parents never disclose old text", () => {
  const child = message(3, 102, 1001, { reply_to: 1 });
  assert.deepEqual(quoteOf(child, [message(1)], () => "Arkon"), { name: "Arkon", text: "Invented message", deleted: false });
  assert.equal(quoteOf(child, [message(1, 101, 1000, { deleted: 1002, text: "old" })]).text, "Original message was deleted");
  assert.equal(quoteOf(child, new Map([[1, message(1, 101, 1000, { deleted: 1002 })]])).deleted, true);
  assert.equal(quoteOf(child, []).text, "Message outside this window");
});
test("reactions carry their real distinct people and whether mine", () => {
  assert.deepEqual(reactionBar([{ emoji: "👍", members: [1, 101, 101], count: 999 }], 1, g => "#" + g),
    [{ emoji: "👍", count: 2, mine: true, who: "#1, #101" }]);
});
test("unread counts actual rows, not gaps in global ids or his own/deleted messages", () => {
  assert.equal(unread([message(10), message(800), message(900, 1), message(1000, 102, 1000, { deleted: 1001 })], 10, 1), 1);
});
test("members group by role online first and all offline last", () => {
  const rows = [{ guid: 101, role: "raider", presence: { state: "online" } }, { guid: 102, role: "officer", presence: { state: "dnd" } },
    { guid: 103, role: "officer", presence: { state: "offline" } }];
  assert.deepEqual(memberGroups(rows).map(g => [g.role, g.members[0].guid]), [["officer", 102], ["raider", 101], ["offline", 103]]);
  assert.equal(presenceDot("dnd"), "Do not disturb"); assert.equal(presenceDot("unknown"), "Offline");
});
test("mentions match a whole owner name without regex injection", () => {
  assert.ok(mentionsHim("hey @TESTMAGE", "Testmage")); assert.ok(!mentionsHim("@TestmageAlt", "Testmage"));
  assert.ok(!mentionsHim("anything", ".*"));
});
function server(extra = {}) {
  return Object.assign({ categories: [{ channels: [{ id: "general", name: "general", last_id: 100, unread: 2 }] }],
    dms: [{ guid: 101, name: "Arkon", last_id: 102, unread: 1 }], members: [], human: { guid: 1, name: "Hero" }, missing: [] }, extra);
}
test("read marks survive reload independently and first history is read", () => {
  const d = new Discord({ "guild": 200, "d:general": 10, "dm:101": 50, "d:bad": -1 });
  d.adopt(server()); assert.deepEqual(d.marks(), { general: 10, "dm:101": 50 });
  assert.equal(d.unread("d:general"), 2); d.markRead("d:general"); assert.equal(d.unread("d:general"), 0);
  assert.equal(d.unread("dm:101"), 1); d.markRead("dm:101"); assert.equal(d.unread("dm:101"), 0);
  assert.deepEqual(new Discord(d.read).marks(), { general: 100, "dm:101": 102 });
  const fresh = new Discord(); fresh.adopt(server()); assert.equal(fresh.read["d:general"], 100);
  assert.equal(fresh.unread("d:general"), 0);
  const stale = new Discord({ "d:removed": 99, "dm:999": 12 }); stale.adopt(server());
  assert.ok(!("d:removed" in stale.read)); assert.ok(!("dm:999" in stale.read));
});
test("channel and thread windows cannot replace one another", () => {
  const d = new Discord(); d.adopt(server({ channel: "general", messages: [message(1)] }), 0);
  d.adopt(server({ channel: "general", messages: [message(2)] }), 7);
  assert.equal(d.window("d:general", 0).messages[0].id, 1); assert.equal(d.window("d:general", 7).messages[0].id, 2);
});
