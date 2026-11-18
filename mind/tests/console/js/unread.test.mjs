import test from "node:test";
import assert from "node:assert/strict";
import { HIDDEN_POLL_MS, TITLE, adoptTops, iconHref, notifyBody, shouldNotify, titleText, total, unreadPath }
  from "../../../leeroy_mind/console/static/js/unreadmodel.mjs";

test("the title counts what was unread plus what came since", () => {
  assert.equal(total(2, { whispers: 1, dms: 3, mentions: 1 }), 7);
  assert.equal(total(0, null), 0);
  assert.equal(total(-4, { whispers: "9", dms: NaN, mentions: -1 }), 0);
  assert.equal(titleText(0), TITLE);
  assert.equal(titleText(3), "(3) Guild Console");
  assert.equal(titleText(250), "(99+) Guild Console");
});

test("the icon gains a dot only while something is unread", () => {
  assert.ok(iconHref(true).startsWith("data:image/svg+xml,"));
  assert.ok(decodeURIComponent(iconHref(true)).includes("<circle"));
  assert.ok(!decodeURIComponent(iconHref(false)).includes("<circle"));
});

test("a notification names who wrote and never what", () => {
  assert.equal(notifyBody({ name: "Belra", text: "the private words" }), "Belra sent a message");
  assert.equal(notifyBody({ name: "" }), "A member sent a message");
  assert.equal(notifyBody(null), "A member sent a message");
  assert.equal(notifyBody({ name: "Be\nlra\u0007" }), "Belra sent a message");
});

test("one notification per newest message, only when the count rose", () => {
  assert.equal(shouldNotify({ key: "d:5", name: "Belra" }, "", 1, 0), true);
  assert.equal(shouldNotify({ key: "d:5", name: "Belra" }, "d:5", 2, 1), false);
  assert.equal(shouldNotify({ key: "d:6", name: "Belra" }, "d:5", 1, 1), false);
  assert.equal(shouldNotify(null, "", 3, 0), false);
});

test("the hidden page's one read carries only the ids it holds", () => {
  assert.equal(unreadPath({ chat: null, discord: null }), "/api/unread");
  assert.equal(unreadPath({ chat: 12, discord: null }), "/api/unread?chat=12");
  assert.equal(unreadPath({ chat: 12, discord: 0 }), "/api/unread?chat=12&discord=0");
  assert.equal(unreadPath({ chat: -1, discord: 1.5 }), "/api/unread");
  assert.deepEqual(adoptTops({ chat: null, discord: 7 }, { chat_top: 40, discord_top: 90 }), { chat: 40, discord: 7 });
  assert.ok(HIDDEN_POLL_MS >= 60000);
});

test("the feed's top is the newest line it holds", async () => {
  const { Feed } = await import("../../../leeroy_mind/console/static/js/comms/model.mjs");
  const feed = new Feed({ human: 900 });
  assert.equal(feed.top(), 0);
  feed.upsert([{ id: 4, channel: "guild", from_guid: 101, ts: 1, text: "a" },
               { id: 9, channel: "whisper", from_guid: 101, to_guid: 900, ts: 2, text: "b" }]);
  assert.equal(feed.top(), 9);
});
